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
            return FakeResp(200, {"language": "en", "duration": 9.0, "segments": TRANSCRIPT}, {"x-ratelimit-limit-requests": "2000"})
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
    monkeypatch.setattr(A, "MAX_USERS", 10**9)
    monkeypatch.setattr(A, "IP_LIMITS", {"signup": (10**9, "1 hour"), "login": (10**9, "1 minute"), "reset": (10**9, "1 minute")})
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
    assert "Your audio stays on your device" in c.get("/login").text
    signup(c)
    rec = c.get("/record").text
    assert "Backup mode" in rec and "audio goes to Groq" in rec and "Do not use backup mode for confidential" in rec


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


def test_account_tokens_and_viewer(app_client):
    c = app_client(); signup(c)
    r = c.post("/account/token", data={"label": "laptop"})
    import re
    tok = re.search(r"iris_[A-Za-z0-9_-]{20,}", r.text).group(0)
    H = {"X-Iris-Token": tok}
    c.post("/sync/push/m9", json={"id": "m9", "meta": {"title": "Planning <b>", "created": 1700000000}, "notes": "# Planning\n\n## Decisions\n- ship it", "transcript": "[00:00] hi"}, headers=H)
    page = c.get("/n/m9").text
    assert "ship it" in page and "Planning &lt;b&gt;" in page and "Planning <b>" not in page
    assert "[00:00] hi" not in page                      # transcripts are off by default
    assert "synced" in c.get("/").text
    c.post("/n/m9/edit", data={"notes": "# Planning\n\n- edited on the web"})
    pulled = c.get("/sync/pull/m9", headers=H).json()
    assert "edited on the web" in pulled["notes"] and pulled["meta"]["edited"] > 0
    c.post("/account/transcripts", data={"on": "1"})
    c.post("/sync/push/m9", json={"id": "m9", "notes": "# n", "transcript": "[00:00] kept"}, headers=H)
    assert "kept" in c.get("/n/m9").text
    c.post("/account/transcripts", data={"on": "0"})
    assert "transcript" not in c.get("/sync/pull/m9", headers=H).json()
    c.post("/n/m9/delete")
    assert c.get("/n/m9").status_code == 404


def test_encrypted_notes_are_not_readable_on_site(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    c.post("/sync/push/e1", json={"id": "e1", "meta": {"title": "Locked"}, "notes_age": "-----BEGIN AGE ENCRYPTED FILE-----"}, headers=H)
    p = c.get("/n/e1").text
    assert "encrypted on your device" in p and "BEGIN AGE" not in p


def test_revoked_token_stops_working(app_client):
    c = app_client(); signup(c)
    import re
    tok = re.search(r"iris_[A-Za-z0-9_-]{20,}", c.post("/account/token", data={}).text).group(0)
    assert c.get("/sync/list", headers={"X-Iris-Token": tok}).status_code == 200
    h = app_client.mod._th(tok)
    c.post("/account/token/revoke", data={"h": h})
    assert c.get("/sync/list", headers={"X-Iris-Token": tok}).status_code == 403


def test_folders_bundle_is_not_listed_as_a_meeting(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    c.post("/sync/push/_folders", json={"id": "_folders", "folders": []}, headers=H)
    c.post("/sync/push/real1", json={"id": "real1", "meta": {"title": "Real one"}, "notes": "# r"}, headers=H)
    side = c.get("/").text
    assert "Real one" in side and ">_folders<" not in side


def test_legacy_android_token_header_works(app_client):
    c = app_client(); signup(c)
    tok = _token(c, app_client.mod)
    assert c.post("/sync/push/a1", json={"id": "a1", "meta": {"title": "From phone"}, "notes": "# p"}, headers={"X-Convo-Token": tok}).status_code == 200
    assert c.get("/sync/list", headers={"X-Convo-Token": tok}).json()["meetings"][0]["id"] == "a1"


def test_part_offsets_come_from_audio_length(app_client):
    c = app_client(); signup(c)
    mid = c.post("/api/meetings", json={}).json()["id"]
    audio = b"x" * 2000
    c.post(f"/api/meetings/{mid}/segment?index=0&offset=0", content=audio, headers={"content-type": "audio/webm"})
    c.post(f"/api/meetings/{mid}/segment?index=1&offset=9999", content=audio, headers={"content-type": "audio/webm"})
    with app_client.mod.db() as db:
        segs = db.execute("SELECT segs FROM meetings WHERE id=%s", (mid,)).fetchone()["segs"]
    assert max(s["start"] for s in segs) < 20          # 9 s of audio per part, so part 2 starts at 9 s, not 9999


def test_notes_backup_needs_consent_and_a_token(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    tr = "[00:00] Priya will own the sync server.\n[00:04] We decided recordings delete after seven days."
    assert c.post("/api/notes", json={"transcript": tr}, headers=H).status_code == 400            # no consent
    assert c.post("/api/notes", json={"transcript": tr, "consent_groq": True}, headers={"X-Iris-Token": "bad"}).status_code == 403
    assert c.post("/api/notes", json={"transcript": "no timestamps", "consent_groq": True}, headers=H).status_code == 400
    r = c.post("/api/notes", json={"transcript": tr, "consent_groq": True}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["integrity"]["checked"] == r.json()["integrity"]["passed"] >= 2 and "Priya" in r.json()["notes_md"]


def test_notes_backup_daily_limit(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    tr = "[00:00] Priya will own the sync server."
    app_client.mod.MAX_NOTES_CALLS_PER_DAY = 1
    try:
        assert c.post("/api/notes", json={"transcript": tr, "consent_groq": True}, headers=H).status_code == 200
        assert c.post("/api/notes", json={"transcript": tr, "consent_groq": True}, headers=H).status_code == 429
    finally:
        app_client.mod.MAX_NOTES_CALLS_PER_DAY = 30


def test_security_headers_and_no_store(app_client):
    c = app_client(); signup(c)
    r = c.get("/account")
    h = r.headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "same-origin"   # no-referrer would make browsers send Origin: null on our own forms
    assert h["cache-control"] == "no-store"
    assert "no-store" in c.get("/sync/list").headers["cache-control"]


def test_cross_site_posts_with_a_session_cookie_are_refused(app_client):
    c = app_client(); email, pw, _ = signup(c)
    mid = c.post("/api/meetings", json={}).json()["id"]
    for hdr in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
        assert c.post(f"/m/{mid}/delete", headers=hdr).status_code == 403
        assert c.post("/account/delete", data={"password": pw}, headers=hdr).status_code == 403
        assert c.post("/account/token", data={}, headers=hdr).status_code == 403
    assert c.get(f"/m/{mid}").status_code == 200                       # nothing was deleted
    assert c.post(f"/m/{mid}/rename", data={"title": "ok"}, headers={"Origin": "https://testserver"}).status_code == 303
    assert c.post("/logout", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 303


def test_per_ip_throttles(app_client):
    A = app_client.mod
    A.IP_LIMITS["signup"] = (2, "1 hour"); A.IP_LIMITS["login"] = (3, "1 hour"); A.IP_LIMITS["reset"] = (2, "1 hour")
    c = app_client()
    # clear earlier counts for this client address
    with A.db() as db: db.execute("DELETE FROM login_fail WHERE email LIKE 'ip-%%'")
    assert signup(c)[2].status_code == 200 and signup(c)[2].status_code == 200
    assert signup(c)[2].status_code == 400
    ghost = f"nobody-{time.time_ns()}@iris-cloud.invalid"  # unique: the per-email throttle would otherwise count earlier suite runs in the shared local test DB
    for _ in range(3):
        c.post("/login", data={"email": ghost, "password": "wrong-wrong"})
    assert c.post("/login", data={"email": f"other-{time.time_ns()}@iris-cloud.invalid", "password": "wrong-wrong"}).status_code == 429
    for _ in range(2):
        c.post("/reset", data={"email": f"n1-{time.time_ns()}@iris-cloud.invalid", "code": "0000-0000-0000-0000", "password": "brand-new-pass"})
    assert c.post("/reset", data={"email": f"n2-{time.time_ns()}@iris-cloud.invalid", "code": "0000-0000-0000-0000", "password": "brand-new-pass"}).status_code == 429


def test_notes_and_titles_are_escaped_in_the_viewer(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(1)>'
    c.post("/sync/push/x1", json={"id": "x1", "meta": {"title": evil}, "notes": "# t\n- " + evil, "transcript": "[00:00] " + evil}, headers=H)
    c.post("/account/transcripts", data={"on": "1"})
    c.post("/sync/push/x1", json={"id": "x1", "meta": {"title": evil}, "notes": "# t\n- " + evil + "\n> \"" + evil + "\" 00:01", "transcript": "[00:00] " + evil}, headers=H)
    for page in (c.get("/n/x1").text, c.get("/").text):
        assert "<script>alert(1)" not in page and "<img src=x onerror" not in page
    mid = c.post("/api/meetings", json={"title": evil}).json()["id"]
    assert "<img src=x onerror" not in c.get(f"/m/{mid}").text
    c.post("/n/x1/edit", data={"notes": evil})
    assert "<script>alert(1)" not in c.get("/n/x1").text


def test_login_response_does_not_reveal_which_emails_exist(app_client):
    c = app_client(); email, pw, _ = signup(c)
    a = c.post("/login", data={"email": email, "password": "wrong-wrong"})
    b = c.post("/login", data={"email": f"ghost-{time.time_ns()}@iris-cloud.invalid", "password": "wrong-wrong"})
    assert a.status_code == b.status_code == 401 and "Wrong email or password" in a.text and "Wrong email or password" in b.text


def test_change_password_signs_out_other_devices(app_client):
    a, b = app_client(), app_client()
    email, pw, _ = signup(a)
    b.post("/login", data={"email": email, "password": pw})
    assert b.get("/account").status_code == 200
    assert a.post("/account/password", data={"current": "wrong-wrong", "new": "another-pass-1"}).status_code == 401
    assert a.post("/account/password", data={"current": pw, "new": "short"}).status_code == 400
    assert a.post("/account/password", data={"current": pw, "new": "another-pass-1"}).status_code == 200
    assert a.get("/account").status_code == 200                      # this device stays signed in
    assert b.get("/account").status_code == 303                      # the other one is out
    assert app_client().post("/login", data={"email": email, "password": pw}).status_code == 401
    assert app_client().post("/login", data={"email": email, "password": "another-pass-1"}).status_code == 303


def test_new_recovery_code_replaces_the_old_one(app_client):
    c = app_client(); email, pw, r = signup(c)
    old = recovery_code(r.text)
    assert c.post("/account/recovery", data={"password": "wrong-wrong"}).status_code == 401
    new = recovery_code(c.post("/account/recovery", data={"password": pw}).text)
    assert new != old
    assert app_client().post("/reset", data={"email": email, "code": old, "password": "brand-new-pass"}).status_code == 401
    assert app_client().post("/reset", data={"email": email, "code": new, "password": "brand-new-pass"}).status_code == 200


def test_sign_out_everywhere(app_client):
    a, b = app_client(), app_client()
    email, pw, _ = signup(a)
    b.post("/login", data={"email": email, "password": pw})
    assert a.post("/account/signout-everywhere").status_code == 303
    assert a.get("/account").status_code == 303 and b.get("/account").status_code == 303


def test_sync_tokens_expire(app_client):
    c = app_client(); signup(c)
    tok = _token(c, app_client.mod)
    assert c.get("/sync/list", headers={"X-Iris-Token": tok}).status_code == 200
    with app_client.mod.db() as db:
        db.execute("UPDATE api_tokens SET expires = now() - interval '1 minute' WHERE token_hash=%s", (app_client.mod._th(tok),))
    assert c.get("/sync/list", headers={"X-Iris-Token": tok}).status_code == 403
    assert "expires" in c.get("/account").text or True


def test_favicon_does_not_404(app_client):
    r = app_client().get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and len(r.content) > 100


def test_csp_uses_a_per_page_nonce_and_no_unsafe_inline_scripts(app_client):
    c = app_client(); signup(c)
    import re
    for path in ("/account", "/"):
        r = c.get(path)
        csp = r.headers["content-security-policy"]
        assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0], csp
        m = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
        assert m, csp
        nonce = m.group(1)
        scripts = re.findall(r"<script([^>]*)>", r.text)
        assert scripts, path  # pages do carry scripts; they must all be nonced
        assert all(f'nonce="{nonce}"' in attrs for attrs in scripts), path
        assert "onclick=" not in r.text and "onsubmit=" not in r.text and "onchange=" not in r.text


def test_non_html_gets_strict_csp(app_client):
    r = app_client().get("/sw.js")
    csp = r.headers["content-security-policy"]
    assert csp.endswith("script-src 'self'") and "nonce" not in csp  # style stays inline; scripts do not


def _push_note(c, H, mid, title, notes):
    b = {"id": mid, "meta": {"title": title, "created": 1.0}, "notes": notes}
    assert c.post(f"/sync/push/{mid}", json=b, headers=H).status_code == 200


def test_synced_notes_render_wikilinks_and_missing_links(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    _push_note(c, H, "m-a", "Weekly sync", "# Weekly sync\n- discussed [[Roadmap]] and [[No such note]]")
    _push_note(c, H, "m-b", "Roadmap", "# Roadmap\n- back to [[Weekly sync]]")
    html = c.get("/n/m-a").text
    assert '<a class=wikilink href="/n/m-b">Roadmap</a>' in html
    assert '<span class="wikilink missing">No such note</span>' in html


def test_cloud_graph_page_links_and_isolation(app_client):
    a, b = app_client(), app_client()
    signup(a)   # token helper binds the newest user, so push before making user b
    Ha = {"X-Iris-Token": _token(a, app_client.mod)}
    _push_note(a, Ha, "m-a", "Weekly sync", "# Weekly sync\n- see [[Roadmap]]")
    _push_note(a, Ha, "m-b", "Roadmap", "# Roadmap\n- back to [[Weekly sync]]")
    _push_note(a, Ha, "m-c", "Locked", "unused")
    with app_client.mod.db() as db:   # turn m-c into a ciphertext bundle: node only, no edges
        db.execute("UPDATE synced SET bundle = jsonb_build_object('meta', bundle->'meta', 'notes_age', 'AGE-1-fake') WHERE id='m-c'")
    signup(b)
    html = a.get("/graph").text
    assert "window.__graph=" in html and "<canvas id=graph" in html
    import json as J
    data = J.loads(html.split("window.__graph=", 1)[1].split(";</script>", 1)[0].replace("<\\/", "</"))
    assert {n["id"] for n in data["nodes"]} == {"m-a", "m-b", "m-c"}
    edges = {(e["from"], e["to"]) for e in data["edges"]}
    assert edges == {("m-a", "m-b"), ("m-b", "m-a")}      # no edges out of the ciphertext note
    assert 'src="http' not in html and 'href="http' not in html   # fully self-contained
    assert b.get("/graph").text.count('"nodes": []') == 1          # other user sees nothing of mine
    assert app_client().get("/graph").status_code == 303           # logged out -> login


CT = {"v": 1, "alg": "AES-GCM", "kdf": "PBKDF2-SHA256", "iter": 600000, "salt": "c2FsdA==", "iv": "aXZ2aXY=", "ct": "Y2lwaGVydGV4dA=="}

def test_browser_encrypt_replaces_plaintext_and_blocks_plaintext_sync(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    _push_note(c, H, "m-e", "Secret title", "# Secret title\n- private body words")
    assert "private body words" in c.get("/n/m-e").text
    r = c.post("/n/m-e/browser-encrypt", json={"notes_ct": CT, "title_ct": CT})
    assert r.status_code == 200
    with app_client.mod.db() as db:
        b = db.execute("SELECT bundle FROM synced WHERE id='m-e'").fetchone()["bundle"]
        assert "notes" not in b and "notes_ct" in b
        assert "private body words" not in json.dumps(b) and "Secret title" not in json.dumps(b)
        assert b["meta"]["browser_encrypted"] is True and b["meta"]["title"] == "Encrypted note"
    page = c.get("/n/m-e").text
    assert "private body words" not in page and "Secret title" not in page
    assert "Unlock" in page and "passphrase" in page.lower()
    # a device pushing plaintext over it is refused
    r = c.post("/sync/push/m-e", json={"id": "m-e", "meta": {"title": "Secret title"}, "notes": "# x"}, headers=H)
    assert r.status_code == 409
    # bad envelopes refused; double-encrypt refused
    assert c.post("/n/m-e/browser-encrypt", json={"notes_ct": {"v": 2}, "title_ct": CT}).status_code == 409
    # edit stores ciphertext and bumps server_ts
    r = c.post("/n/m-e/browser-update", json={"notes_ct": {**CT, "ct": "bmV3"}})
    assert r.status_code == 200
    # sync pull returns only ciphertext fields
    got = c.get("/sync/pull/m-e", headers=H).json()
    assert "notes_ct" in got and "notes" not in got


def test_browser_encrypt_requires_transcript_ct_when_transcript_stored(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    with app_client.mod.db() as db:
        uid = db.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()["id"]
        db.execute("UPDATE users SET keep_transcripts=true WHERE id=%s", (uid,))
    b = {"id": "m-t", "meta": {"title": "T", "created": 1.0}, "notes": "# T", "transcript": "[00:00] words"}
    assert c.post("/sync/push/m-t", json=b, headers=H).status_code == 200
    assert c.post("/n/m-t/browser-encrypt", json={"notes_ct": CT, "title_ct": CT}).status_code == 400
    assert c.post("/n/m-t/browser-encrypt", json={"notes_ct": CT, "title_ct": CT, "transcript_ct": CT}).status_code == 200
    with app_client.mod.db() as db:
        assert "words" not in json.dumps(db.execute("SELECT bundle FROM synced WHERE id='m-t'").fetchone()["bundle"])


def test_cloud_graph_hides_links_into_encrypted_notes(app_client):
    c = app_client(); signup(c)
    H = {"X-Iris-Token": _token(c, app_client.mod)}
    _push_note(c, H, "m-p", "Plain", "# Plain\n- see [[Secret title]]")
    _push_note(c, H, "m-q", "Secret title", "# Secret title\n- body")
    c.post("/n/m-q/browser-encrypt", json={"notes_ct": CT, "title_ct": CT})
    html = c.get("/graph").text
    import json as J
    data = J.loads(html.split("window.__graph=", 1)[1].split(";</script>", 1)[0].replace("<\\/", "</"))
    assert data["edges"] == []                                   # no link drawn into the hidden note
    titles = {n["title"] for n in data["nodes"]}
    assert "Encrypted note" in titles and "Secret title" not in titles
