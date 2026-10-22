"""Hosted Iris tests. They need a Postgres database in TEST_DATABASE_URL and
skip without one. Groq is replaced by a fake, so no network or key is used."""
import json, os, sys, time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

TRANSCRIPT = [
    {"start": 0.0, "end": 4.0, "text": "Priya will own the sync server."},
    {"start": 4.0, "end": 9.0, "text": "We decided recordings delete after seven days."},
]
NOTES = {"summary": "Short sync.",
         "decisions": [{"text": "Recordings delete after seven days", "evidence": "We decided recordings delete after seven days.", "at": "00:04"}],
         "action_items": [{"text": "Own the sync server", "owner": "Priya", "evidence": "Priya will own the sync server.", "at": "00:00"}]}


class FakeResp:
    def __init__(self, code, data, headers=None):
        self.status_code, self._d, self.headers, self.text = code, data, headers or {}, json.dumps(data)
    def json(self): return self._d


class FakeClient:
    chat_calls = 0
    chat_fail = False
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, **kw):
        if url.endswith("/audio/transcriptions"):
            return FakeResp(200, {"language": "en", "segments": TRANSCRIPT}, {"x-ratelimit-limit-requests": "2000"})
        FakeClient.chat_calls += 1
        if FakeClient.chat_fail: return FakeResp(429, {})
        body = kw["json"]
        if body.get("response_format"):
            return FakeResp(200, {"choices": [{"message": {"content": json.dumps(NOTES)}}]})
        return FakeResp(200, {"choices": [{"message": {"content": "Joined summary."}}]})


@pytest.fixture()
def app_client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    from fastapi.testclient import TestClient
    import api.index as A
    A._ready = False
    monkeypatch.setattr(A.httpx, "AsyncClient", FakeClient)
    FakeClient.chat_calls = 0; FakeClient.chat_fail = False
    def make():
        return TestClient(A.app, base_url="https://testserver", follow_redirects=False)
    make.mod = A
    return make


def signup(c, email=None, pw="correct-horse-battery"):
    email = email or f"t{time.time_ns()}@iris-cloud.invalid"
    r = c.post("/signup", data={"email": email, "password": pw})
    return email, pw, r


def recovery_code(html):
    import re
    return re.search(r"[0-9A-F]{4}(?:-[0-9A-F]{4}){3}", html).group(0)


def test_unit_helpers():
    import api.index as A
    h = A.hash_pw("secret-pass")
    assert A.check_pw("secret-pass", h) and not A.check_pw("wrong", h)
    code, ch = A.new_recovery()
    assert len(A.norm_code(code)) == 16 and A.check_pw(code, ch)


def test_signup_login_logout(app_client):
    c = app_client()
    email, pw, r = signup(c)
    assert r.status_code == 200 and "Save your recovery code" in r.text
    assert c.get("/record").status_code == 200
    c.post("/logout")
    assert c.get("/m/anything").status_code == 303
    assert app_client().post("/login", data={"email": email, "password": "nope-nope"}).status_code == 401
    assert app_client().post("/login", data={"email": email, "password": pw}).status_code == 303
    assert signup(app_client(), email)[2].status_code == 400   # duplicate


def test_login_throttle(app_client):
    c = app_client()
    email = f"thr{time.time_ns()}@iris-cloud.invalid"
    for _ in range(8):
        c.post("/login", data={"email": email, "password": "x" * 9})
    assert c.post("/login", data={"email": email, "password": "x" * 9}).status_code == 429


def test_password_reset_with_recovery_code(app_client):
    c = app_client()
    email, pw, r = signup(c)
    code = recovery_code(r.text)
    c2 = app_client()
    bad = c2.post("/reset", data={"email": email, "code": "0000-0000-0000-0000", "password": "brand-new-pass"})
    assert bad.status_code == 401
    ok = c2.post("/reset", data={"email": email, "code": code.lower(), "password": "brand-new-pass"})
    assert ok.status_code == 200 and recovery_code(ok.text) != code
    assert app_client().post("/login", data={"email": email, "password": pw}).status_code == 401
    assert app_client().post("/login", data={"email": email, "password": "brand-new-pass"}).status_code == 303
    # old code is used up
    assert app_client().post("/reset", data={"email": email, "code": code, "password": "another-pass1"}).status_code == 401


def test_reset_throttle(app_client):
    c = app_client()
    email, _, r = signup(c)
    for _ in range(5):
        c.post("/reset", data={"email": email, "code": "1111-1111-1111-1111", "password": "brand-new-pass"})
    good = recovery_code(r.text)
    assert c.post("/reset", data={"email": email, "code": good, "password": "brand-new-pass"}).status_code == 429


def test_call_path_two_parts(app_client):
    c = app_client()
    signup(c)
    mid = c.post("/api/meetings", json={"title": "sync"}).json()["id"]
    audio = b"x" * 2000
    r0 = c.post(f"/api/meetings/{mid}/segment?index=0&offset=0", content=audio, headers={"content-type": "audio/webm"})
    r1 = c.post(f"/api/meetings/{mid}/segment?index=1&offset=300", content=audio, headers={"content-type": "audio/webm"})
    assert r0.status_code == 200 and r1.status_code == 200 and r1.json()["segments"] == 4
    f = c.post(f"/api/meetings/{mid}/finish")
    assert f.status_code == 200, f.text
    integ = f.json()["integrity"]
    assert integ["checked"] == integ["passed"] and integ["checked"] >= 4
    page = c.get(f"/m/{mid}").text
    assert "Priya" in page and "claims verified" in page


def test_finish_retries_failed_part(app_client):
    c = app_client()
    signup(c)
    mid = c.post("/api/meetings", json={}).json()["id"]
    FakeClient.chat_fail = True
    r = c.post(f"/api/meetings/{mid}/segment?index=0&offset=0", content=b"x" * 2000, headers={"content-type": "audio/webm"})
    assert r.status_code == 200                          # transcript kept even though notes were busy
    assert c.post(f"/api/meetings/{mid}/finish").status_code == 502
    assert "Try writing the notes again" in c.get(f"/m/{mid}").text
    FakeClient.chat_fail = False
    assert c.post(f"/api/meetings/{mid}/finish").status_code == 200


def test_fabricated_quote_is_flagged(app_client, monkeypatch):
    A = app_client.mod
    bad = dict(NOTES, decisions=[{"text": "Ship Friday", "evidence": "We will ship on Friday.", "at": "00:01"}])
    async def fake(segs, key): return bad
    monkeypatch.setattr(A, "_part_notes", fake)
    c = app_client()
    signup(c)
    mid = c.post("/api/meetings", json={}).json()["id"]
    c.post(f"/api/meetings/{mid}/segment?index=0&offset=0", content=b"x" * 2000, headers={"content-type": "audio/webm"})
    f = c.post(f"/api/meetings/{mid}/finish").json()
    assert f["integrity"]["passed"] < f["integrity"]["checked"]


def test_users_cannot_see_each_others_meetings(app_client):
    a, b = app_client(), app_client()
    signup(a); signup(b)
    mid = a.post("/api/meetings", json={"title": "private"}).json()["id"]
    assert a.get(f"/m/{mid}").status_code == 200
    assert b.get(f"/m/{mid}").status_code == 404
    assert b.post(f"/api/meetings/{mid}/finish").status_code == 404
    assert b.post(f"/m/{mid}/delete").status_code in (303, 404)
    assert a.get(f"/m/{mid}").status_code == 200
    assert app_client().post("/api/meetings", json={}).status_code == 401


def test_limits(app_client):
    c = app_client()
    signup(c)
    c.post("/api/meetings", json={})
    app_client.mod.MAX_MEETINGS_PER_DAY = 1
    try:
        assert c.post("/api/meetings", json={}).status_code == 429
    finally:
        app_client.mod.MAX_MEETINGS_PER_DAY = 20
    assert c.post("/api/meetings/zzz/segment", content=b"x" * 10).status_code == 400
    assert c.post("/api/meetings/zzz/segment", content=b"x" * (app_client.mod.MAX_SEGMENT + 1)).status_code == 413


def test_account_delete(app_client):
    c = app_client()
    email, pw, _ = signup(c)
    mid = c.post("/api/meetings", json={}).json()["id"]
    assert c.post("/account/delete", data={"password": "wrong-wrong"}).status_code == 401
    assert c.post("/account/delete", data={"password": pw}).status_code == 303
    assert app_client().post("/login", data={"email": email, "password": pw}).status_code == 401
    with app_client.mod.db() as db:
        assert db.execute("SELECT count(*) AS n FROM meetings WHERE id=%s", (mid,)).fetchone()["n"] == 0


def test_static_routes(app_client):
    c = app_client()
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/manifest.webmanifest").status_code == 200
    assert c.get("/sw.js").status_code == 200
    assert c.get("/icon-192.png").status_code == 200
    assert "Do not use hosted mode for confidential" in c.get("/login").text


def _token(c, mod):
    """Make an API token directly, the way the account page does."""
    import secrets
    tok = secrets.token_urlsafe(24)
    with mod.db() as db:
        uid = db.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()["id"]
        db.execute("INSERT INTO api_tokens(token_hash,user_id) VALUES(%s,%s)", (mod._th(tok), uid))
    return tok


def test_sync_protocol_notes_only(app_client):
    c = app_client(); signup(c)
    tok = _token(c, app_client.mod); H = {"X-Iris-Token": tok}
    b = {"id": "20261022-1000-abc123", "meta": {"title": "Standup", "created": 1.0}, "notes": "# Standup\n\n- decided x",
         "transcript": "[00:00] secret words"}
    r = c.post("/sync/push/20261022-1000-abc123", json=b, headers=H)
    assert r.status_code == 200 and r.json()["transcript_dropped"] is True
    lst = c.get("/sync/list?since=0", headers=H).json()["meetings"]
    assert [m["id"] for m in lst] == ["20261022-1000-abc123"]
    got = c.get("/sync/pull/20261022-1000-abc123", headers=H).json()
    assert got["notes"].startswith("# Standup") and "transcript" not in got
    assert c.get(f"/sync/list?since={lst[0]['ts'] + 1}", headers=H).json()["meetings"] == []


def test_sync_rejects_audio_bad_token_and_bad_ids(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    assert c.post("/sync/push/x1", json={"id": "x1", "audio_b64": "AAAA"}, headers=H).status_code == 400
    assert c.post("/sync/push/..%2Fx", json={"id": "x"}, headers=H).status_code in (400, 404)
    assert c.post("/sync/push/x2", json={"id": "x2"}, headers={"X-Iris-Token": "nope"}).status_code == 403
    assert c.get("/sync/list").status_code == 403


def test_sync_is_per_user(app_client):
    a, b = app_client(), app_client()
    signup(a); ta = _token(a, app_client.mod)
    signup(b); tb = _token(b, app_client.mod)
    a.post("/sync/push/m1", json={"id": "m1", "notes": "# A only"}, headers={"X-Iris-Token": ta})
    assert b.get("/sync/pull/m1", headers={"X-Iris-Token": tb}).status_code == 404
    assert b.get("/sync/list", headers={"X-Iris-Token": tb}).json()["meetings"] == []
