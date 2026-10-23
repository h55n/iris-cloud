"""Hosted Iris: accounts, a Postgres database, and the call -> transcript -> notes
path on free-tier services. Whisper runs at Groq; notes are written by an open
model at Groq, then checked by the same verifier the self-hosted Iris uses."""
import hashlib, hmac, html, json, os, re, secrets, sys, time
from urllib.parse import urlparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx, psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from iris import verify, render, pwa as W
from iris.mascot import mascot
from iris.transcribe import transcript_markdown
from iris.webui import PAGE, esc, render_notes_md, render_transcript_line, GRAPH_JS
from iris.links import extract as extract_links

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

GROQ = "https://api.groq.com/openai/v1"
STT_MODEL = os.environ.get("STT_MODEL", "whisper-large-v3-turbo")
NOTES_MODEL = os.environ.get("NOTES_MODEL", "openai/gpt-oss-120b")
MAX_SEGMENT = 3_500_000          # bytes per upload (Vercel allows 4.5 MB per request)
MAX_USERS = int(os.environ.get("MAX_USERS", "200"))
MAX_MEETINGS_PER_DAY = int(os.environ.get("MAX_MEETINGS_PER_DAY", "20"))
MAX_SEGMENTS_PER_MEETING = 40    # 40 x 5 min = 3h20
COOKIE = "iris_s"

NOTES_PROMPT = """You are the notes engine for a meeting recorder. Given a transcript with [MM:SS] timestamps, produce JSON only:
{"summary": "2-3 sentences", "decisions": [{"text": "...", "evidence": "verbatim transcript quote", "at": "MM:SS"}], "action_items": [{"text": "...", "owner": "name or null", "evidence": "verbatim transcript quote", "at": "MM:SS"}]}
Rules: evidence must be copied word-for-word from the transcript. If nothing was decided, decisions=[]. If no tasks, action_items=[]. Never invent names, dates or tasks.

TRANSCRIPT:
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id bigserial PRIMARY KEY, email text UNIQUE NOT NULL, pw_hash text NOT NULL, created timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS sessions(token_hash text PRIMARY KEY, user_id bigint REFERENCES users(id) ON DELETE CASCADE, expires timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS login_fail(email text, ts timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS meetings(id text PRIMARY KEY, user_id bigint REFERENCES users(id) ON DELETE CASCADE, title text NOT NULL DEFAULT '',
  created timestamptz DEFAULT now(), status text NOT NULL DEFAULT 'recording', segs jsonb NOT NULL DEFAULT '[]', seg_count int NOT NULL DEFAULT 0,
  notes_md text NOT NULL DEFAULT '', integrity jsonb, error text);
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_hash text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS keep_transcripts boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS api_tokens(token_hash text PRIMARY KEY, user_id bigint REFERENCES users(id) ON DELETE CASCADE, label text NOT NULL DEFAULT '', created timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS synced(user_id bigint REFERENCES users(id) ON DELETE CASCADE, id text NOT NULL, bundle jsonb NOT NULL, server_ts double precision NOT NULL, PRIMARY KEY(user_id,id));
ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS expires timestamptz NOT NULL DEFAULT now() + interval '90 days';
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS parts jsonb NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS meetings_user ON meetings(user_id, created DESC);
"""
_ready = False

def db():
    global _ready
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    c = psycopg.connect(url, row_factory=dict_row, autocommit=True, connect_timeout=10)
    if not _ready:
        c.execute(SCHEMA); _ready = True
    return c

# ---------- passwords and sessions ----------
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"

def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False

def new_recovery():
    raw = secrets.token_hex(8).upper()
    code = "-".join(raw[i:i+4] for i in range(0, 16, 4))
    return code, hash_pw(code)

def norm_code(c): return re.sub(r"[^0-9A-F]", "", (c or "").upper())

def recovery_page(code, dest="/record"):
    return ("<h1 class=title style='font-size:28px'>Save your recovery code</h1>"
            "<p>There is no email on this free version, so this code is the only way to reset a forgotten password. "
            "It is shown once. Copy it somewhere safe.</p>"
            f"<p style='font:600 22px ui-monospace,monospace;letter-spacing:.06em;padding:14px;border:1px dashed var(--border);border-radius:10px;text-align:center' id=rc>{code}</p>"
            f"<p><a class=cta href='{dest}' style='display:inline-block;padding:10px 20px;border-radius:999px;background:var(--accent);color:#fff;text-decoration:none;font-weight:600'>I saved it, continue</a></p>")

def reset_form(err="", email="", ok=""):
    msg = (f"<p style='color:var(--danger,#b3261e)'>{esc(err)}</p>" if err else "") + (f"<p>{esc(ok)}</p>" if ok else "")
    return ("<h1 class=title style='font-size:30px'>Reset password</h1>" + msg
            + '<form method=post action="/reset" style="display:flex;flex-direction:column;gap:10px;max-width:340px">'
              f'<input name=email type=email required placeholder="Email" value="{esc(email)}" class=f>'
              '<input name=code required placeholder="Recovery code (XXXX-XXXX-XXXX-XXXX)" autocomplete=off class=f>'
              '<input name=password type=password required minlength=8 placeholder="New password (8+ characters)" autocomplete=new-password class=f>'
              '<button class=cta style="border:0;cursor:pointer;font:inherit;font-weight:600">Reset password</button></form>'
              "<style>.f{font:inherit;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text)}"
              ".cta{display:inline-block;padding:10px 20px;border-radius:999px;background:var(--accent);color:#fff}</style>"
              "<p style='color:var(--muted)'>Lost the code too? There is no way to recover the account. Create a new one.</p>")

def _th(tok): return hashlib.sha256(tok.encode()).hexdigest()

def start_session(c, user_id, resp):
    tok = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions VALUES(%s,%s, now() + interval '30 days')", (_th(tok), user_id))
    resp.set_cookie(COOKIE, tok, max_age=30 * 86400, httponly=True, secure=True, samesite="lax", path="/")

def current_user(c, req: Request):
    tok = req.cookies.get(COOKIE)
    if not tok: return None
    return c.execute("SELECT u.id, u.email FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires>now()", (_th(tok),)).fetchone()

# ---------- security: headers, cross-site request refusal, per-IP throttles ----------
CSP_BASE = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")

def client_ip(req: Request) -> str:
    # Vercel sets these itself, so a caller cannot choose them
    for h in ("x-vercel-forwarded-for", "x-real-ip", "x-forwarded-for"):
        v = (req.headers.get(h) or "").split(",")[0].strip()
        if v: return v[:64]
    return req.client.host if req.client else "unknown"

@app.middleware("http")
async def security_layer(req: Request, call_next):
    if req.method in ("POST", "PUT", "PATCH", "DELETE") and req.cookies.get(COOKIE) and not req.headers.get("x-iris-token") and not req.headers.get("x-convo-token"):
        origin = req.headers.get("origin"); host = req.headers.get("host") or ""
        bad = (origin is not None and (origin == "null" or urlparse(origin).netloc != host)) or \
              (origin is None and req.headers.get("sec-fetch-site") == "cross-site")
        if bad: return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    resp = await call_next(req)
    if "text/html" in resp.headers.get("content-type", ""):
        nonce = secrets.token_urlsafe(16)
        raw = b"".join([bytes(c) async for c in resp.body_iterator])
        body = raw.replace(b"<script", b'<script nonce="' + nonce.encode() + b'"')
        async def _one(b): yield b
        resp.body_iterator = _one(body)
        resp.headers["content-length"] = str(len(body))
        resp.headers["Content-Security-Policy"] = CSP_BASE + f"; script-src 'self' 'nonce-{nonce}'"
    else:
        resp.headers["Content-Security-Policy"] = CSP_BASE + "; script-src 'self'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"   # "no-referrer" makes browsers send Origin: null on our own forms, which the CSRF check refuses
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
    if "text/html" in resp.headers.get("content-type", "") or req.url.path.startswith(("/sync", "/api")):
        resp.headers["Cache-Control"] = "no-store"
    return resp

IP_LIMITS = {"signup": (10, "1 hour"), "login": (40, "10 minutes"), "reset": (20, "10 minutes")}

def ip_limited(c, req: Request, kind: str) -> bool:
    limit, window = IP_LIMITS[kind]
    k = f"ip-{kind}:{client_ip(req)}"
    n = c.execute("SELECT count(*) AS n FROM login_fail WHERE email=%s AND ts>now()-%s::interval", (k, window)).fetchone()["n"]
    if n >= limit: return True
    c.execute("INSERT INTO login_fail(email) VALUES(%s)", (k,))
    return False

_DUMMY = None
def burn_hash(pw: str):
    """Spend the same time on an unknown email as on a real one, so response time does not reveal which emails exist."""
    global _DUMMY
    if _DUMMY is None: _DUMMY = hash_pw("not-a-real-password")
    check_pw(pw, _DUMMY)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

# ---------- pages ----------
def sidebar(c, user, active=None):
    rows = c.execute("SELECT id,title,created,status,integrity FROM meetings WHERE user_id=%s ORDER BY created DESC LIMIT 60", (user["id"],)).fetchall() if user else []
    items = ""
    for m in rows:
        integ = m["integrity"] or {}
        iv = f' &middot; {integ.get("passed")}/{integ.get("checked")} verified' if integ else ""
        when = m["created"].strftime("%b %-d, %H:%M UTC")
        st = "" if m["status"] == "done" else f' &middot; {esc(m["status"])}'
        items += (f'<a class="nav-item{" active" if m["id"] == active else ""}" href="/m/{m["id"]}">'
                  f'<div class=nav-title>{esc(m["title"] or "Untitled meeting")}</div><div class=nav-meta>{when}{st}{iv}</div></a>')
    srows = c.execute("SELECT id, bundle->'meta'->>'title' AS title, bundle->'meta'->>'created' AS created, server_ts FROM synced WHERE user_id=%s AND id NOT LIKE '\\_%%' ORDER BY server_ts DESC LIMIT 60", (user["id"],)).fetchall() if user else []
    if srows:
        items += "<div class=nav-sec style='margin-top:10px'>From your devices</div>"
        for m in srows:
            try: when = time.strftime("%b %-d, %H:%M UTC", time.gmtime(float(m["created"] or m["server_ts"])))
            except Exception: when = "synced"
            items += (f'<a class="nav-item{" active" if "n:" + m["id"] == active else ""}" href="/n/{esc(m["id"])}">'
                      f'<div class=nav-title>{esc(m["title"] or m["id"])}</div><div class=nav-meta>{when} &middot; synced</div></a>')
    who = (f'<div class=nav-sec>Signed in</div><div class=nav-meta style="padding:2px 12px 8px">{esc(user["email"])}</div>'
           '<a class=wl href="/account">Account</a><form method=post action="/logout" style="margin:0"><button class=wl>Log out</button></form>'
           '<style>.wl{display:block;width:100%;text-align:left;padding:6px 12px;border:0;background:transparent;color:var(--text);font:inherit;font-size:14px;text-decoration:none;border-radius:6px;cursor:pointer}.wl:hover{background:var(--active-bg)}'
           'form+form,.doc form button{margin-top:0}.doc p+form,.doc form+form{margin-top:12px}.doc form button{margin-top:10px}</style>') if user else \
          '<div style="padding:8px 12px"><a href="/login">Log in</a> &middot; <a href="/signup">Sign up</a></div>'
    return ('<input type=checkbox id=navtoggle><label for=navtoggle class=menubtn>Menu</label>'
            '<a href="/" style="text-decoration:none;color:inherit"><div class=brand>Iris<span id=tagline>meeting notes that keep receipts</span></div></a>'
            + ('<a class="folder" href="/record" style="margin:6px 0"><span class=chev></span>Backup recorder</a><a class="folder" href="/graph" style="margin:0 0 6px"><span class=chev></span>Graph</a>' if user else '')
            + '<div class=nav>' + (f'<div class=nav-sec>Meetings</div>{items or "<div class=nav-meta style=padding:8px>Nothing recorded yet</div>"}' if user else '') + '</div>'
            + who
            + '<div class=themes><span class=lbl>Theme</span>' + ''.join(
                f'<button class=dot data-t="{t}" title="{t or "green"}"></button>'
                for t in ("", "blue", "purple", "orange", "pink", "red")) + '</div>'
            + '<script>function pickTheme(t,b){if(t)document.documentElement.dataset.theme=t;else delete document.documentElement.dataset.theme;'
              'try{localStorage.setItem("iris-theme",t)}catch(e){}document.querySelectorAll(".dot").forEach(function(d){d.classList.remove("sel")});b.classList.add("sel")}'
              '(function(){var cur="";try{cur=localStorage.getItem("iris-theme")||""}catch(e){}var d=document.querySelector(\'.dot[data-t="\'+cur+\'"]\');if(d)d.classList.add("sel")})();'
              'document.querySelectorAll(".dot").forEach(function(b){b.addEventListener("click",function(){pickTheme(b.dataset.t,b)})})</script>')

DELEGATE_JS = ("<script>document.addEventListener('change',function(e){if(e.target.dataset&&e.target.dataset.autosubmit!==undefined)e.target.form.submit()});"
               "document.addEventListener('submit',function(e){var m=e.target.dataset&&e.target.dataset.confirm;if(m&&!confirm(m))e.preventDefault()});"
               "</script>")

def render_page(c, user, body, active=None, status=200):
    from iris import pwa as W
    inner = (f'<div class=layout><nav class=sidebar>{sidebar(c, user, active)}</nav>'
             f'<main class=content><div class=doc>{body}</div></main></div>')
    doc = (PAGE.replace("<!--PWA-->", W.HEAD_TAGS) + inner + DELEGATE_JS + W.SW_REGISTER)
    return HTMLResponse(doc, status_code=status)

def render_page_msg(msg):
    with db() as c:
        return render_page(c, None, f"<p>{esc(msg)}</p><p><a href='/account'>Back to account</a></p>")

PRIVACY = ("<p style='color:var(--muted);font-size:13.5px;line-height:1.55'><b>Your audio stays on your device.</b> "
           "The local Iris app records, transcribes and writes the notes on your own machine, with models that run there. "
           "Only the finished notes sync to this site, tied to your account. Transcripts stay off unless you turn them on in Account, and audio is never uploaded. "
           "Want ciphertext only? Run <code>iris encrypt</code> locally first and this site will hold notes it cannot read. "
           "<a href='https://github.com/h55n/iris'>Get the local app</a>.</p>")

BACKUP_PRIVACY = ("<p style='color:var(--muted);font-size:13.5px;line-height:1.55'><b>Backup mode, for devices that cannot run Iris locally.</b> "
                  "In this mode your recording is sent to Groq to be transcribed (Whisper), and the transcript is sent to an open model at Groq to write the notes. "
                  "We keep the transcript and notes in our database, tied to your account. We do not keep the audio. "
                  "Do not use backup mode for confidential meetings. For private use, "
                  "<a href='https://github.com/h55n/iris'>run Iris locally</a>, where audio never leaves your own machine.</p>")

def landing(c, user):
    cta = ('<a class=cta href="/account">Set up sync</a> <a class=cta2 href="/record">Backup recorder</a>' if user else
           '<a class=cta href="/signup">Create an account</a> <a class=cta2 href="/login">Log in</a>')
    steps = ("<div style='max-width:520px;margin:0 auto 22px'><p class=eyebrow>How it works</p>"
             "<ol style='padding-left:20px;line-height:1.7;margin:0'>"
             "<li>Run Iris on your computer or phone. It records, transcribes and writes the notes there.</li>"
             "<li>Make a sync token on the Account page.</li>"
             "<li>Run <code>iris sync --notes-only</code>. Only the notes come here, never the audio.</li>"
             "<li>Read and edit them from any device, like Notion.</li></ol></div>")
    return (mascot(120, "celebrate") + "<h1 class=title style='text-align:center;font-size:34px'>Iris</h1>"
            "<p style='text-align:center;color:var(--muted);margin:0 0 18px'>Meeting notes made on your device, readable anywhere. Every decision shows the line it came from.</p>"
            f"<div style='text-align:center;margin-bottom:26px'>{cta}</div>" + steps +
            "<style>.cta,.cta2{display:inline-block;padding:10px 20px;border-radius:999px;text-decoration:none;font-weight:600;margin:0 4px}"
            ".cta{background:var(--accent);color:#fff}.cta2{border:1px solid var(--border);color:var(--text)}</style>" + PRIVACY)

def auth_form(kind, err="", email=""):
    t = "Create your account" if kind == "signup" else "Log in"
    other = ('<a href="/login">Have an account? Log in</a>' if kind == "signup" else '<a href="/signup">New here? Create an account</a> &middot; <a href="/reset">Forgot password?</a>')
    return (f"<h1 class=title style='font-size:30px'>{t}</h1>"
            + (f"<p style='color:var(--danger,#b3261e)'>{esc(err)}</p>" if err else "")
            + f'<form method=post action="/{kind}" style="display:flex;flex-direction:column;gap:10px;max-width:340px">'
              f'<input name=email type=email required placeholder="Email" value="{esc(email)}" autocomplete=email class=f>'
              f'<input name=password type=password required minlength=8 placeholder="Password (8+ characters)" autocomplete="{"new-password" if kind=="signup" else "current-password"}" class=f>'
              f'<button class=cta style="border:0;cursor:pointer;font:inherit;font-weight:600">{t}</button></form>'
              "<style>.f{font:inherit;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text)}"
              ".cta{display:inline-block;padding:10px 20px;border-radius:999px;background:var(--accent);color:#fff}</style>"
            + f"<p style='margin-top:16px'>{other}</p>" + PRIVACY)

@app.get("/", response_class=HTMLResponse)
def home(req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return render_page(c, None, landing(c, None))
        n = c.execute("SELECT count(*) AS n FROM meetings WHERE user_id=%s", (u["id"],)).fetchone()["n"]
        body = landing(c, u) if n == 0 else (
            mascot(96, "happy") + "<h1 class=title style='text-align:center;font-size:28px'>Welcome back</h1>"
            "<p style='text-align:center'><a class=cta href=/record style='display:inline-block;padding:10px 20px;border-radius:999px;background:var(--accent);color:#fff;text-decoration:none;font-weight:600'>Backup recorder</a></p>"
            + PRIVACY)
        return render_page(c, u, body)

@app.get("/signup", response_class=HTMLResponse)
def signup_page(req: Request):
    with db() as c: return render_page(c, current_user(c, req), auth_form("signup"))

@app.get("/login", response_class=HTMLResponse)
def login_page(req: Request):
    with db() as c: return render_page(c, current_user(c, req), auth_form("login"))

async def _form(req: Request):
    from urllib.parse import parse_qs
    raw = (await req.body()).decode()
    return {k: v[0] for k, v in parse_qs(raw).items()}

@app.post("/signup")
async def signup(req: Request):
    f = await _form(req); email = (f.get("email") or "").strip().lower(); pw = f.get("password") or ""
    with db() as c:
        def bad(msg): return render_page(c, None, auth_form("signup", msg, email), status=400)
        if not EMAIL_RE.match(email): return bad("That does not look like an email address.")
        if len(pw) < 8 or len(pw) > 200: return bad("Use a password of 8 to 200 characters.")
        if c.execute("SELECT count(*) AS n FROM users").fetchone()["n"] >= MAX_USERS:
            return bad("Signups are full for now (free-tier limit). Self-hosting Iris is always open.")
        if ip_limited(c, req, "signup"): return bad("Too many signups from this network. Try again later.")
        rcode, rhash = new_recovery()
        row = c.execute("INSERT INTO users(email,pw_hash,recovery_hash) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id", (email, hash_pw(pw), rhash)).fetchone()
        if not row: return bad("An account with that email already exists. Try logging in.")
        resp = render_page(c, None, recovery_page(rcode)); start_session(c, row["id"], resp); return resp

@app.post("/login")
async def login(req: Request):
    f = await _form(req); email = (f.get("email") or "").strip().lower(); pw = f.get("password") or ""
    with db() as c:
        fails = c.execute("SELECT count(*) AS n FROM login_fail WHERE email=%s AND ts>now()-interval '10 minutes'", (email,)).fetchone()["n"]
        if fails >= 8: return render_page(c, None, auth_form("login", "Too many attempts. Wait ten minutes.", email), status=429)
        if ip_limited(c, req, "login"):
            return render_page(c, None, auth_form("login", "Too many attempts from this network. Wait ten minutes.", email), status=429)
        c.execute("DELETE FROM sessions WHERE expires<now()"); c.execute("DELETE FROM login_fail WHERE ts<now()-interval '1 day'")
        u = c.execute("SELECT id,pw_hash FROM users WHERE email=%s", (email,)).fetchone()
        if not u: burn_hash(pw)
        if not u or not check_pw(pw, u["pw_hash"]):
            c.execute("INSERT INTO login_fail(email) VALUES(%s)", (email,))
            return render_page(c, None, auth_form("login", "Wrong email or password.", email), status=401)
        resp = RedirectResponse("/", status_code=303); start_session(c, u["id"], resp); return resp

@app.get("/reset", response_class=HTMLResponse)
def reset_page(req: Request):
    with db() as c: return render_page(c, current_user(c, req), reset_form())

@app.post("/reset")
async def reset(req: Request):
    f = await _form(req); email = (f.get("email") or "").strip().lower(); pw = f.get("password") or ""
    code = norm_code(f.get("code"))
    with db() as c:
        key = "reset:" + email
        if c.execute("SELECT count(*) AS n FROM login_fail WHERE email=%s AND ts>now()-interval '10 minutes'", (key,)).fetchone()["n"] >= 5:
            return render_page(c, None, reset_form("Too many attempts. Wait ten minutes.", email), status=429)
        if len(pw) < 8 or len(pw) > 200:
            return render_page(c, None, reset_form("Use a password of 8 to 200 characters.", email), status=400)
        if ip_limited(c, req, "reset"):
            return render_page(c, None, reset_form("Too many attempts from this network. Wait ten minutes.", email), status=429)
        u = c.execute("SELECT id,recovery_hash FROM users WHERE email=%s", (email,)).fetchone()
        if not u: burn_hash(code)
        if not u or not u["recovery_hash"] or len(code) != 16 or not check_pw("-".join(code[i:i+4] for i in range(0, 16, 4)), u["recovery_hash"]):
            c.execute("INSERT INTO login_fail(email) VALUES(%s)", (key,))
            return render_page(c, None, reset_form("Email or recovery code is wrong.", email), status=401)
        rcode, rhash = new_recovery()
        c.execute("UPDATE users SET pw_hash=%s, recovery_hash=%s WHERE id=%s", (hash_pw(pw), rhash, u["id"]))
        c.execute("DELETE FROM sessions WHERE user_id=%s", (u["id"],))
        resp = render_page(c, None, "<p>Password changed. Your old code is used up, so here is a new one.</p>" + recovery_page(rcode, "/"))
        start_session(c, u["id"], resp); return resp

@app.post("/account/delete")
async def account_delete(req: Request):
    f = await _form(req)
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        row = c.execute("SELECT pw_hash FROM users WHERE id=%s", (u["id"],)).fetchone()
        if not check_pw(f.get("password") or "", row["pw_hash"]):
            return render_page(c, u, "<p style='color:var(--danger,#b3261e)'>Wrong password. Nothing was deleted.</p>", status=401)
        c.execute("DELETE FROM users WHERE id=%s", (u["id"],))
        resp = RedirectResponse("/", status_code=303); resp.delete_cookie(COOKIE, path="/"); return resp

@app.get("/account", response_class=HTMLResponse)
def account(req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        keep = c.execute("SELECT keep_transcripts FROM users WHERE id=%s", (u["id"],)).fetchone()["keep_transcripts"]
        toks = c.execute("SELECT token_hash,label,created,expires FROM api_tokens WHERE user_id=%s ORDER BY created", (u["id"],)).fetchall()
        btn = "border:0;cursor:pointer;font:inherit;font-weight:600;padding:9px 18px;border-radius:999px;background:var(--accent);color:#fff"
        inp = "font:inherit;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text)"
        rows = "".join(f'<li style="margin:6px 0">{esc(t["label"] or "token")} <span style="color:var(--muted)">&middot; made {t["created"].strftime("%b %-d")}, expires {t["expires"].strftime("%b %-d")}</span> '
                       f'<form method=post action="/account/token/revoke" style="display:inline"><input type=hidden name=h value="{t["token_hash"]}"><button style="border:0;background:none;color:#b3261e;cursor:pointer;font:inherit">Revoke</button></form></li>' for t in toks)
        return render_page(c, u, "<h1 class=title style='font-size:28px'>Account</h1>"
            "<h2 class=big>Sync from your devices</h2>"
            "<p>Your local Iris app turns audio into notes on your own machine, then sends only the notes here. Make a token for each device.</p>"
            f"<ul style='padding-left:18px'>{rows or '<li style=color:var(--muted)>No tokens yet.</li>'}</ul>"
            f'<form method=post action="/account/token" style="display:flex;gap:8px;flex-wrap:wrap;max-width:420px"><input name=label placeholder="Device name (optional)" style="{inp};flex:1;min-width:160px"><button style="{btn}">Make a token</button></form>'
            "<h2 class=big style='margin-top:28px'>Transcripts</h2>"
            f"<p>Off by default: only notes are stored. Turn on to also keep transcripts for your synced meetings. Turning it off deletes the stored transcripts. Currently <b>{'on' if keep else 'off'}</b>.</p>"
            f'<form method=post action="/account/transcripts"><input type=hidden name=on value="{0 if keep else 1}"><button style="{btn}">{"Turn off and delete stored transcripts" if keep else "Turn on"}</button></form>'
            "<h2 class=big style='margin-top:28px'>Password and sign-in</h2>"
            f'<form method=post action="/account/password" style="display:flex;flex-direction:column;gap:8px;max-width:340px"><input name=current type=password required placeholder="Current password" autocomplete=current-password style="{inp}"><input name=new type=password required minlength=8 placeholder="New password (8+ characters)" autocomplete=new-password style="{inp}"><button style="{btn}">Change password</button></form>'
            f'<form method=post action="/account/recovery" style="display:flex;gap:8px;flex-wrap:wrap;max-width:420px;margin-top:14px"><input name=password type=password required placeholder="Password" autocomplete=current-password style="{inp};flex:1;min-width:160px"><button style="{btn}">Make a new recovery code</button></form>'
            '<form method=post action="/account/signout-everywhere" style="margin-top:14px"><button style="border:1px solid var(--border);background:transparent;color:var(--text);cursor:pointer;font:inherit;padding:9px 18px;border-radius:999px">Sign out on all devices</button></form>'
            "<h2 class=big style='margin-top:28px'>Delete account</h2>"
            "<p>Removes your meetings, synced notes and tokens from the database. This cannot be undone.</p>"
            f'<form method=post action="/account/delete" style="display:flex;flex-direction:column;gap:10px;max-width:340px"><input name=password type=password required placeholder="Confirm password" style="{inp}">'
            '<button style="border:0;cursor:pointer;font:inherit;font-weight:600;padding:10px 20px;border-radius:999px;background:#b3261e;color:#fff">Delete my account</button></form>')

@app.post("/account/password")
async def account_password(req: Request):
    f = await _form(req); new = f.get("new") or ""
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        row = c.execute("SELECT pw_hash FROM users WHERE id=%s", (u["id"],)).fetchone()
        if ip_limited(c, req, "login"): return render_page(c, u, "<p>Too many attempts. Wait ten minutes.</p>", status=429)
        if not check_pw(f.get("current") or "", row["pw_hash"]):
            return render_page(c, u, "<p style='color:var(--danger,#b3261e)'>Current password is wrong. Nothing changed.</p><p><a href='/account'>Back</a></p>", status=401)
        if len(new) < 8 or len(new) > 200:
            return render_page(c, u, "<p style='color:var(--danger,#b3261e)'>Use a new password of 8 to 200 characters.</p><p><a href='/account'>Back</a></p>", status=400)
        c.execute("UPDATE users SET pw_hash=%s WHERE id=%s", (hash_pw(new), u["id"]))
        keep = _th(req.cookies.get(COOKIE) or "")
        c.execute("DELETE FROM sessions WHERE user_id=%s AND token_hash<>%s", (u["id"], keep))   # other devices sign out
    return render_page_msg("Password changed. Other devices were signed out.")

@app.post("/account/recovery")
async def account_recovery(req: Request):
    f = await _form(req)
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        row = c.execute("SELECT pw_hash FROM users WHERE id=%s", (u["id"],)).fetchone()
        if not check_pw(f.get("password") or "", row["pw_hash"]):
            return render_page(c, u, "<p style='color:var(--danger,#b3261e)'>Wrong password. Nothing changed.</p><p><a href='/account'>Back</a></p>", status=401)
        code, h = new_recovery()
        c.execute("UPDATE users SET recovery_hash=%s WHERE id=%s", (h, u["id"]))
        return render_page(c, u, "<p>Your old recovery code no longer works. Here is the new one.</p>" + recovery_page(code, "/account"))

@app.post("/account/signout-everywhere")
def account_signout_everywhere(req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        c.execute("DELETE FROM sessions WHERE user_id=%s", (u["id"],))
    resp = RedirectResponse("/login", status_code=303); resp.delete_cookie(COOKIE, path="/"); return resp

@app.get("/favicon.ico")
def favicon():
    return Response(W.asset("icon-192.png") or b"", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.post("/logout")
def logout(req: Request):
    with db() as c:
        tok = req.cookies.get(COOKIE)
        if tok: c.execute("DELETE FROM sessions WHERE token_hash=%s", (_th(tok),))
    resp = RedirectResponse("/", status_code=303); resp.delete_cookie(COOKIE, path="/"); return resp

RECORD_JS = r"""<script>(function(){
var btn=document.getElementById('rbtn'),msg=document.getElementById('rmsg'),tm=document.getElementById('rtime'),log=document.getElementById('rlog');
var SEG=300, rec=null, stream=null, mid=null, t0=0, segStart=0, idx=0, queue=Promise.resolve(), tick=null, stopping=false, chunks=[];
function fmt(s){return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function say(t){msg.textContent=t}
function upload(blob,index,offset){
  queue=queue.then(async function(){
    for(var a=0;a<3;a++){
      try{var r=await fetch('/api/meetings/'+mid+'/segment?index='+index+'&offset='+offset,{method:'POST',headers:{'Content-Type':blob.type||'audio/webm'},body:blob});
        var j=await r.json(); if(r.ok){log.textContent='Transcribed part '+(index+1)+' ('+j.segments+' lines so far)';return}
        if(r.status===429||r.status>=500){await new Promise(function(z){setTimeout(z,2500*(a+1))});continue}
        throw new Error(j.error||r.status)}catch(e){if(a==2)throw e}}
  });
}
function startRec(){
  chunks=[];segStart=Math.floor((Date.now()-t0)/1000);var my=idx++;
  rec=new MediaRecorder(stream,{audioBitsPerSecond:32000});
  rec.ondataavailable=function(e){if(e.data.size)chunks.push(e.data)};
  rec.onstop=function(){var blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});var off=segStart;
    if(blob.size>1000)upload(blob,my,off);
    if(!stopping){startRec()}else{finish()}};
  rec.start(1000);
  setTimeout(function(){if(rec&&rec.state==='recording'&&!stopping)rec.stop()},SEG*1000);
}
async function finish(){
  stream.getTracks().forEach(function(t){t.stop()});clearInterval(tick);btn.textContent='Writing notes...';
  say('Transcribing the last part and writing notes. This can take up to a minute.');
  try{await queue;var r=await fetch('/api/meetings/'+mid+'/finish',{method:'POST'});var j=await r.json();
    if(r.ok){location.href='/m/'+mid}else{say('Notes failed: '+(j.error||r.status)+'. Your transcript is saved.');btn.disabled=false;btn.textContent='Open meeting';btn.onclick=function(){location.href='/m/'+mid}}}
  catch(e){say('Something failed: '+e.message+'. Part of your transcript may be saved.');btn.disabled=false;btn.textContent='Open meeting';btn.onclick=function(){location.href='/m/'+mid}}
}
btn.onclick=async function(){
  if(rec){stopping=true;btn.disabled=true;rec.stop();return}
  if(!navigator.mediaDevices||!window.MediaRecorder){say('This browser cannot record. Use Chrome, Edge, Firefox or Safari.');return}
  try{stream=await navigator.mediaDevices.getUserMedia({audio:true})}catch(e){say('Microphone blocked: '+e.message);return}
  var r=await fetch('/api/meetings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:document.getElementById('rtitle').value})});
  var j=await r.json();if(!r.ok){say(j.error||'Could not start');return}
  mid=j.id;t0=Date.now();idx=0;stopping=false;startRec();btn.textContent='Stop and make notes';say('Recording. Leave this tab open. Audio is sent in 5-minute parts.');
  tick=setInterval(function(){tm.textContent=fmt(Math.floor((Date.now()-t0)/1000))},500);
};})()</script>"""

@app.get("/record", response_class=HTMLResponse)
def record(req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        body = ("<style>#rtitle{font:inherit;padding:9px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);width:100%;margin-bottom:12px}"
                "#rbtn{font:inherit;font-weight:600;padding:10px 18px;border:0;border-radius:999px;background:var(--accent);color:#fff;cursor:pointer}#rbtn:disabled{opacity:.6}</style>"
                "<h1 class=title style='font-size:30px'>Backup recorder</h1>" + BACKUP_PRIVACY +
                '<label style="display:flex;gap:8px;align-items:flex-start;margin:0 0 14px;font-size:14px"><input type=checkbox id=ok style="margin-top:4px"> I understand my audio goes to Groq in this mode.</label>'
                '<input id=rtitle placeholder="Title (optional)" maxlength=120>'
                '<div style="display:flex;gap:10px;align-items:center"><button id=rbtn type=button disabled>Start recording</button>'
                '<span id=rtime style="font-variant-numeric:tabular-nums;color:var(--muted)">00:00</span></div>'
                '<p id=rmsg style="color:var(--muted);margin-top:14px"></p><p id=rlog style="color:var(--faint);font-size:13px"></p>' + RECORD_JS + "<script>(function(){var ok=document.getElementById('ok'),b=document.getElementById('rbtn');ok.onchange=function(){b.disabled=!ok.checked}})()</script>")
        return render_page(c, u, body)

# ---------- API ----------
def _need_user(c, req):
    u = current_user(c, req)
    return u

def _err(msg, code=400, **extra): return JSONResponse({"error": msg, **extra}, status_code=code)

@app.post("/api/meetings")
async def api_new(req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return _err("not signed in", 401)
        try: title = ((await req.json()).get("title") or "").strip()[:120]
        except Exception: title = ""
        n = c.execute("SELECT count(*) AS n FROM meetings WHERE user_id=%s AND created>now()-interval '1 day'", (u["id"],)).fetchone()["n"]
        if n >= MAX_MEETINGS_PER_DAY: return _err(f"Daily limit of {MAX_MEETINGS_PER_DAY} meetings reached (free-tier protection).", 429)
        mid = secrets.token_urlsafe(9)
        c.execute("INSERT INTO meetings(id,user_id,title) VALUES(%s,%s,%s)", (mid, u["id"], title))
        return {"id": mid}

def _fmt_ts(sec): return f"{int(sec)//60:02d}:{int(sec)%60:02d}"

@app.post("/api/meetings/{mid}/segment")
async def api_segment(mid: str, req: Request, index: int = 0, offset: float = 0):
    key = os.environ.get("GROQ_API_KEY", "")
    if not key: return _err("transcription is not configured", 503)
    audio = await req.body()
    if len(audio) < 1000: return _err("audio part too small")
    if len(audio) > MAX_SEGMENT: return _err("audio part too large", 413)
    with db() as c:
        u = current_user(c, req)
        if not u: return _err("not signed in", 401)
        m = c.execute("SELECT status, seg_count, parts FROM meetings WHERE id=%s AND user_id=%s", (mid, u["id"])).fetchone()
        if not m: return _err("no such meeting", 404)
        if m["status"] not in ("recording", "error"): return _err("meeting already finished", 409)
        if m["seg_count"] >= MAX_SEGMENTS_PER_MEETING: return _err("meeting too long for hosted mode", 413)
        # time labels come from the length of the audio already received, never from the browser's clock
        offset = sum({p.get("index", 0): float(p.get("dur") or 300) for p in m["parts"] if p.get("index", 0) < index}.values())
    ctype = req.headers.get("content-type", "audio/webm")
    ext = "mp4" if "mp4" in ctype else "ogg" if "ogg" in ctype else "webm"
    async with httpx.AsyncClient(timeout=55) as h:
        r = await h.post(f"{GROQ}/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                         files={"file": (f"part.{ext}", audio, ctype.split(";")[0])},
                         data={"model": STT_MODEL, "response_format": "verbose_json", "temperature": "0"})
    if r.status_code == 429: return _err("transcription is busy, retrying", 429)
    if r.status_code != 200: return _err(f"transcription failed ({r.status_code}): {r.text[:200]}", 502)
    limits = {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"}
    segs = [{"start": float(s["start"]) + offset, "end": float(s["end"]) + offset, "text": s["text"].strip(), "lang": r.json().get("language")}
            for s in r.json().get("segments", []) if s.get("text", "").strip()]
    part = {"index": index, "t0": offset, "t1": max([x["end"] for x in segs], default=offset), "dur": float(r.json().get("duration") or 300)}
    if segs:
        try: part["notes"] = await _part_notes(segs, key)
        except Exception as e: part["error"] = str(e)[:140]
    with db() as c:
        c.execute("UPDATE meetings SET segs = segs || %s::jsonb, parts = parts || %s::jsonb, seg_count = seg_count + 1 WHERE id=%s AND user_id=%s", (json.dumps(segs), json.dumps([part]), mid, u["id"]))
        total = c.execute("SELECT jsonb_array_length(segs) AS n FROM meetings WHERE id=%s", (mid,)).fetchone()["n"]
    return {"segments": total, "added": len(segs), "limits": limits}

async def _chat(prompt: str, key: str, json_mode: bool = True) -> str:
    body = {"model": NOTES_MODEL, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]}
    if json_mode: body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=50) as h:
        r = await h.post(f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code == 429: raise RuntimeError("notes model is busy (free-tier rate limit)")
    if r.status_code != 200: raise RuntimeError(f"notes model error {r.status_code}: {r.text[:140]}")
    return r.json()["choices"][0]["message"]["content"]

async def _part_notes(segs: list, key: str) -> dict:
    return _parse_notes(await _chat(NOTES_PROMPT + transcript_markdown(segs)[:16000], key))

def _parse_notes(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    notes = json.loads(raw)
    for k in ("summary", "decisions", "action_items"):
        notes.setdefault(k, "" if k == "summary" else [])
    return notes

@app.post("/api/meetings/{mid}/finish")
async def api_finish(mid: str, req: Request):
    key = os.environ.get("GROQ_API_KEY", "")
    with db() as c:
        u = current_user(c, req)
        if not u: return _err("not signed in", 401)
        m = c.execute("SELECT * FROM meetings WHERE id=%s AND user_id=%s", (mid, u["id"])).fetchone()
        if not m: return _err("no such meeting", 404)
        segs = sorted(m["segs"], key=lambda s: s["start"])
        if not segs:
            c.execute("UPDATE meetings SET status='done', notes_md=%s WHERE id=%s", ("_No speech was detected in this recording._", mid))
            return {"id": mid, "empty": True}
        c.execute("UPDATE meetings SET status='writing notes' WHERE id=%s", (mid,))
    try:
        parts = sorted(m["parts"], key=lambda p: p["t0"])
        notes_list = []
        for p in parts:
            if "notes" not in p:   # retry a part whose notes call was rate-limited
                sub = [x for x in segs if p["t0"] - 0.01 <= x["start"] <= p["t1"] + 0.01]
                if not sub: continue
                p["notes"] = await _part_notes(sub, key)
            notes_list.append(p["notes"])
        summaries = [n.get("summary", "") for n in notes_list if n.get("summary")]
        if len(summaries) > 1:
            try:
                summary = (await _chat("Write a 2-3 sentence summary of this meeting from its section summaries. Plain text only.\n\n" + "\n".join(summaries)[:12000], key, json_mode=False)).strip()
            except Exception:
                summary = " ".join(summaries)[:600]
        else:
            summary = summaries[0] if summaries else ""
        merged = {"summary": summary, "decisions": [d for n in notes_list for d in n.get("decisions", [])],
                  "action_items": [a for n in notes_list for a in n.get("action_items", [])]}
        notes = verify.verify_notes(merged, segs)
        md = render.render_notes(mid, notes, segs)
        with db() as c:
            c.execute("UPDATE meetings SET status='done', notes_md=%s, integrity=%s, error=NULL, parts=%s WHERE id=%s", (md, json.dumps(notes.get("integrity")), json.dumps(parts), mid))
        return {"id": mid, "integrity": notes.get("integrity")}
    except Exception as e:
        with db() as c:
            c.execute("UPDATE meetings SET status='error', error=%s WHERE id=%s", (str(e)[:300], mid))
        return _err(str(e)[:200], 502, id=mid)

# ---------- meeting pages ----------
@app.get("/m/{mid}", response_class=HTMLResponse)
def meeting(mid: str, req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        m = c.execute("SELECT * FROM meetings WHERE id=%s AND user_id=%s", (mid, u["id"])).fetchone()
        if not m: return render_page(c, u, "<p>Not found.</p>", status=404)
        segs = sorted(m["segs"], key=lambda s: s["start"])
        tmd = transcript_markdown(segs) if segs else ""
        retry = ("<p><button id=retry class=cta style='border:0;cursor:pointer;font:inherit;font-weight:600;padding:9px 18px;border-radius:999px;background:var(--accent);color:#fff'>Try writing the notes again</button></p>"
                 f"<script>document.getElementById('retry').onclick=async function(){{this.disabled=true;this.textContent='Working...';var r=await fetch('/api/meetings/{mid}/finish',{{method:'POST'}});location.reload()}}</script>") if (m["status"] in ("error", "writing notes") and segs) else ""
        notes_html = render_notes_md(m["notes_md"]) if m["notes_md"] else (
            f"<p style='color:var(--muted)'>Status: {esc(m['status'])}" + (f" - {esc(m['error'])}" if m["error"] else "") + "</p>" + retry)
        tr = "".join(render_transcript_line(l) for l in tmd.splitlines() if l.strip())
        body = ((mascot(110, "celebrate") + "<div class=empty style='padding-top:0'><div class=t>Meeting saved</div></div>") if m["status"] == "done" and (time.time() - m["created"].timestamp()) < 600 else "")
        body += (f'<form method=post action="/m/{mid}/rename" style="margin:0"><input name=title class=title value="{esc(m["title"] or "Untitled meeting")}" maxlength=120 style="width:100%" aria-label="Title" data-autosubmit></form>'
                 f'<div class=props><span>{m["created"].strftime("%B %-d, %Y %H:%M UTC")}</span></div>'
                 + notes_html
                 + (f'<details class=drawer><summary>Transcript ({len(segs)} lines)</summary><div class=tr>{tr}</div></details>' if segs else "")
                 + f'<form method=post action="/m/{mid}/delete" data-confirm="Delete this meeting for good?" style="margin-top:24px"><button class=danger>Delete meeting</button></form>')
        return render_page(c, u, body, active=mid)

@app.post("/m/{mid}/rename")
async def rename(mid: str, req: Request):
    f = await _form(req)
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        c.execute("UPDATE meetings SET title=%s WHERE id=%s AND user_id=%s", ((f.get("title") or "").strip()[:120], mid, u["id"]))
    return RedirectResponse(f"/m/{mid}", status_code=303)

@app.post("/m/{mid}/delete")
def delete(mid: str, req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        c.execute("DELETE FROM meetings WHERE id=%s AND user_id=%s", (mid, u["id"]))
    return RedirectResponse("/", status_code=303)

# ---------- installable app ----------

# ---------- device sync: the same relay protocol the local Iris client speaks ----------
MAX_BUNDLE = 600_000            # bytes per synced meeting
MAX_SYNCED = 500                # synced meetings per user
TRANSCRIPT_KEYS = ("transcript", "transcript_age")
AUDIO_KEYS = ("audio", "audio_b64", "audio_files_b64")

def token_user(c, req: Request):
    tok = req.headers.get("x-iris-token") or req.headers.get("x-convo-token") or ""   # older Android builds send the legacy header
    if not tok: return None
    return c.execute("SELECT u.id, u.email, u.keep_transcripts FROM api_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=%s AND t.expires>now()", (_th(tok),)).fetchone()

def _sid_ok(mid): return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", mid)) and ".." not in mid

@app.post("/sync/push/{mid}")
async def sync_push(mid: str, req: Request):
    raw = await req.body()
    if len(raw) > MAX_BUNDLE: return _err("meeting too large to sync", 413)
    with db() as c:
        u = token_user(c, req)
        if not u: return _err("bad token", 403)
        if not _sid_ok(mid): return _err("bad id", 400)
        try: b = json.loads(raw)
        except Exception: return _err("bad json", 400)
        if not isinstance(b, dict): return _err("bad bundle", 400)
        if any(k in b for k in AUDIO_KEYS): return _err("audio never syncs", 400)
        dropped = False
        if not u["keep_transcripts"]:
            for k in TRANSCRIPT_KEYS:
                if k in b: b.pop(k); dropped = True
        have = c.execute("SELECT 1 FROM synced WHERE user_id=%s AND id=%s", (u["id"], mid)).fetchone()
        if not have and c.execute("SELECT count(*) AS n FROM synced WHERE user_id=%s", (u["id"],)).fetchone()["n"] >= MAX_SYNCED:
            return _err("synced meeting limit reached", 429)
        ts = time.time(); b["_server_ts"] = ts
        old = c.execute("SELECT bundle FROM synced WHERE user_id=%s AND id=%s", (u["id"], mid)).fetchone()
        if old and (old["bundle"].get("meta") or {}).get("browser_encrypted") and "notes_ct" not in b:
            return _err("this note is browser-encrypted on the site; unlock and remove encryption there before syncing plaintext over it", 409)
        c.execute("INSERT INTO synced(user_id,id,bundle,server_ts) VALUES(%s,%s,%s,%s) ON CONFLICT (user_id,id) DO UPDATE SET bundle=EXCLUDED.bundle, server_ts=EXCLUDED.server_ts",
                  (u["id"], mid, json.dumps(b), ts))
        return {"ok": True, "id": mid, "transcript_dropped": dropped}

@app.get("/sync/list")
def sync_list(req: Request, since: float = 0.0):
    with db() as c:
        u = token_user(c, req)
        if not u: return _err("bad token", 403)
        rows = c.execute("SELECT id, server_ts FROM synced WHERE user_id=%s AND server_ts>%s ORDER BY id", (u["id"], since)).fetchall()
        return {"meetings": [{"id": r["id"], "ts": r["server_ts"]} for r in rows]}

@app.get("/sync/pull/{mid}")
def sync_pull(mid: str, req: Request):
    with db() as c:
        u = token_user(c, req)
        if not u: return _err("bad token", 403)
        r = c.execute("SELECT bundle FROM synced WHERE user_id=%s AND id=%s", (u["id"], mid)).fetchone()
        return r["bundle"] if r else _err("not found", 404)

def synced_linkmap(c, user_id):
    """{normalized title: id} over a user's synced notes."""
    import re as _re
    rows = c.execute("SELECT id, bundle->'meta'->>'title' AS title FROM synced WHERE user_id=%s AND id NOT LIKE '\\_%%'", (user_id,)).fetchall()
    out = {}
    for r in rows:
        t = _re.sub(r"\s+", " ", (r["title"] or r["id"]).strip().lower())
        out.setdefault(t, r["id"])
    return out

@app.get("/graph", response_class=HTMLResponse)
def graph_page(req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        rows = c.execute("SELECT id, bundle FROM synced WHERE user_id=%s AND id NOT LIKE '\\_%%'", (u["id"],)).fetchall()
        lmap = synced_linkmap(c, u["id"])
        import re as _re
        nodes, edges, seen = [], [], set()
        for r in rows:
            b = r["bundle"]; meta = b.get("meta") or {}
            nodes.append({"id": r["id"], "title": meta.get("title") or r["id"]})
            if "notes" not in b:   # notes_age ciphertext: node only, we cannot read links
                continue
            hidden = {r2["id"] for r2 in rows if (r2["bundle"].get("meta") or {}).get("browser_encrypted")}
            for target, _label in extract_links(b["notes"]):
                to = lmap.get(_re.sub(r"\s+", " ", target.strip().lower()))
                if to in hidden:   # do not draw links into a hidden note; the source note's own text still shows what the author wrote
                    continue
                if to and to != r["id"] and (r["id"], to) not in seen:
                    seen.add((r["id"], to)); edges.append({"from": r["id"], "to": to})
        g = {"nodes": nodes, "edges": edges}
        meta_txt = (f'{len(nodes)} synced notes, {len(edges)} links. Drag nodes, click one to open it.' if edges
                    else "No links yet. Write [[another note's title]] in a note on your device and sync.")
        body = ('<h1 class=title style="font-size:30px">Note graph</h1>'
                f"<p style='color:var(--muted);font-size:13.5px'>{meta_txt}</p>"
                '<canvas id=graph style="width:100%;height:520px;border:1px solid var(--border);border-radius:12px;margin-top:6px"></canvas>'
                '<script>window.__graph=' + json.dumps(g).replace("</", "<\\/") + ';</script>'
                + GRAPH_JS.replace("/meeting/", "/n/"))
        return render_page(c, u, body)



ENC_SETUP_JS = """<script>(function(){
var root=document.getElementById('encsetup');if(!root)return;
var b64s=function(a){return btoa(String.fromCharCode.apply(null,new Uint8Array(a)))};
var te=new TextEncoder();
function derive(pp,salt,iter){return crypto.subtle.importKey('raw',te.encode(pp),'PBKDF2',false,['deriveKey']).then(function(base){
 return crypto.subtle.deriveKey({name:'PBKDF2',hash:'SHA-256',salt:salt,iterations:iter},base,{name:'AES-GCM',length:256},false,['encrypt','decrypt'])})}
function encText(k,salt,iter,plain){var iv=crypto.getRandomValues(new Uint8Array(12));
 return crypto.subtle.encrypt({name:'AES-GCM',iv:iv},k,te.encode(plain)).then(function(b){
  return {v:1,alg:'AES-GCM',kdf:'PBKDF2-SHA256',iter:iter,salt:b64s(salt),iv:b64s(iv),ct:b64s(b)}})}
document.getElementById('encgo').addEventListener('click',function(){
 var pp=document.getElementById('encnewpp').value,msg=document.getElementById('encmsg');
 if(pp.length<8){msg.textContent='Use at least 8 characters.';return}
 var notes='',ta=document.querySelector('textarea[name=notes]');
 if(ta)notes=ta.value;
 if(!notes){msg.textContent='Open Edit notes first so this tab has the text to encrypt.';return}
 var title=(document.querySelector('h1.title')||{}).textContent||'Encrypted note';
 var salt=crypto.getRandomValues(new Uint8Array(16)),iter=600000;
 msg.textContent='Encrypting in this tab...';
 derive(pp,salt,iter).then(function(k){
  var jobs=[encText(k,salt,iter,notes),encText(k,salt,iter,title)];
  if(root.dataset.tr)jobs.push(encText(k,salt,iter,root.dataset.tr));
  return Promise.all(jobs)}).then(function(r){
  var body={notes_ct:r[0],title_ct:r[1]};if(r[2])body.transcript_ct=r[2];
  return fetch('/n/'+encodeURIComponent(root.dataset.mid)+'/browser-encrypt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})})
 .then(function(resp){if(resp.ok){msg.textContent='Locked. Reloading...';location.reload()}
  else resp.text().then(function(t){msg.textContent='Failed: '+t})})
 .catch(function(e){msg.textContent='Failed: '+e.message})});
})()</script>"""

ENC_JS = """<script>(function(){
var root=document.getElementById('enc');if(!root)return;
var env=JSON.parse(root.dataset.env);
var b64=function(s){return Uint8Array.from(atob(s),function(c){return c.charCodeAt(0)})};
var b64s=function(a){return btoa(String.fromCharCode.apply(null,new Uint8Array(a)))};
var te=new TextEncoder(),td=new TextDecoder();
var key=null;
function derive(pp,env){return crypto.subtle.importKey('raw',te.encode(pp),'PBKDF2',false,['deriveKey']).then(function(base){
 return crypto.subtle.deriveKey({name:'PBKDF2',hash:'SHA-256',salt:b64(env.salt),iterations:env.iter},base,{name:'AES-GCM',length:256},false,['encrypt','decrypt'])})}
function dec(env,k){return crypto.subtle.decrypt({name:'AES-GCM',iv:b64(env.iv)},k,b64(env.ct)).then(function(b){return td.decode(b)})}
function encText(k,env,plain){var iv=crypto.getRandomValues(new Uint8Array(12));
 return crypto.subtle.encrypt({name:'AES-GCM',iv:iv},k,te.encode(plain)).then(function(b){
  return {v:1,alg:'AES-GCM',kdf:'PBKDF2-SHA256',iter:env.iter,salt:env.salt,iv:b64s(iv),ct:b64s(b)}})}
function esc(t){var d=document.createElement('div');d.textContent=t;return d.innerHTML}
function inline(t){t=esc(t);
 t=t.replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/~~([^~]+)~~/g,'<del>$1</del>');
 return t}
function renderMd(md,skipTitle){var out=[],first=true;
 md.split('\n').forEach(function(line){
  if(first&&line.trim()){first=false;if(skipTitle&&line.indexOf('# ')===0)return}
  if(line.indexOf('## ')===0)out.push('<h2>'+inline(line.slice(3))+'</h2>');
  else if(line.indexOf('# ')===0)out.push('<h2 class=big>'+inline(line.slice(2))+'</h2>');
  else if(line.indexOf('### ')===0)out.push('<h3>'+inline(line.slice(4))+'</h3>');
  else if(/^\s*- \[[xX]\] /.test(line))out.push('<div class="todo done"><span class="box on"></span><span>'+inline(line.split('] ')[1])+'</span></div>');
  else if(/^\s*- \[ \] /.test(line))out.push('<div class=todo><span class=box></span><span>'+inline(line.split('] ')[1])+'</span></div>');
  else if(/^\s*- /.test(line))out.push('<div class=bullet>'+inline(line.replace(/^\s*- /,''))+'</div>');
  else if(line.trim())out.push('<p>'+inline(line)+'</p>')});
 return out.join('\n')}
document.getElementById('unlock').addEventListener('submit',function(e){e.preventDefault();
 var pp=document.getElementById('pp').value,err=document.getElementById('encerr');err.textContent='';
 derive(pp,env.notes_ct).then(function(k){
  return dec(env.notes_ct,k).then(function(notes){
   key=k;document.getElementById('plain').hidden=false;document.getElementById('unlock').style.display='none';
   document.getElementById('encbody').innerHTML=renderMd(notes,true);
   document.getElementById('encedit').value=notes;
   var showTitle=function(t){document.getElementById('enctitle').textContent=t};
   if(env.title_ct)dec(env.title_ct,k).then(showTitle,function(){});
   if(env.transcript_ct){dec(env.transcript_ct,k).then(function(tr){
    document.getElementById('encdrawer').hidden=false;
    document.getElementById('enctr').textContent=tr},function(){})}
  })}).catch(function(){err.textContent='Wrong passphrase, or this ciphertext is damaged.'})});
document.getElementById('encsave').addEventListener('click',function(){
 if(!key)return;var notes=document.getElementById('encedit').value,st=document.getElementById('encsaved');
 var first=notes.split('\n').find(function(l){return l.trim()})||'';
 var title=first.indexOf('# ')===0?first.slice(2).trim().slice(0,120):'Encrypted note';
 Promise.all([encText(key,env.notes_ct,notes),encText(key,env.notes_ct,title)]).then(function(r){
  fetch('/n/'+encodeURIComponent(env.mid)+'/browser-update',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({notes_ct:r[0],title_ct:r[1]})}).then(function(resp){
   if(resp.ok){env.notes_ct=r[0];document.getElementById('encbody').innerHTML=renderMd(notes,true);
    document.getElementById('enctitle').textContent=title;st.textContent='Saved. Only ciphertext left this tab.'}
   else st.textContent='Save failed ('+resp.status+')'})})});
})()</script>"""

# ---------- synced notes (viewer and editor) ----------
@app.get("/n/{mid}", response_class=HTMLResponse)
def synced_note(mid: str, req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        r = c.execute("SELECT bundle FROM synced WHERE user_id=%s AND id=%s", (u["id"], mid)).fetchone()
        if not r: return render_page(c, u, "<p>Not found.</p>", status=404)
        b = r["bundle"]; meta = b.get("meta") or {}
        title = meta.get("title") or mid
        if "notes" in b:
            lmap = synced_linkmap(c, u["id"])
            def _resolve(target):
                import re as _re
                m2 = lmap.get(_re.sub(r"\s+", " ", target.strip().lower()))
                return (f"/n/{m2}", True) if m2 else ("#", False)
            body = render_notes_md(b["notes"], link_resolver=_resolve)
            body += ('<details class=drawer style="margin-top:18px"><summary>Encrypt this note in the browser</summary>'
                     '<div id=encsetup data-mid="' + esc(mid) + "' data-tr='" + esc(b.get("transcript") or "").replace("'", "&#39;") + "'>"
                     '<p style="font-size:13.5px;color:var(--muted)">Locks the stored copy with a passphrase that never leaves this tab. '
                     'Afterwards the site stores only ciphertext for the body, title and transcript. '
                     'Still visible: that the note exists, when it was made, and the label "Encrypted note". '
                     'Search and the graph stop reading it. <b>Lose the passphrase and the note cannot be recovered.</b></p>'
                     '<input type=password id=encnewpp placeholder="New passphrase" autocomplete=off style="font:inherit;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);width:100%;max-width:340px"> '
                     '<button id=encgo style="margin-top:8px;border:0;cursor:pointer;font:inherit;font-weight:600;padding:9px 18px;border-radius:999px;background:var(--accent);color:#fff">Encrypt the stored copy</button>'
                     '<p id=encmsg style="font-size:13.5px;color:var(--muted)"></p></div></details>' + ENC_SETUP_JS)
            edit = (f'<details class=drawer><summary>Edit notes</summary><form method=post action="/n/{esc(mid)}/edit">'
                    f'<textarea name=notes rows=16 style="width:100%;font:13.5px/1.5 ui-monospace,monospace;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text)">{esc(b["notes"])}</textarea>'
                    '<p><button style="border:0;cursor:pointer;font:inherit;font-weight:600;padding:9px 18px;border-radius:999px;background:var(--accent);color:#fff">Save</button> '
                    '<span style="color:var(--muted);font-size:13px">Your devices get the edit on their next sync.</span></p></form></details>')
        elif "notes_ct" in b:
            env = json.dumps({"mid": mid, "notes_ct": b["notes_ct"], "title_ct": b.get("title_ct"),
                              "transcript_ct": b.get("transcript_ct"), "can_edit": True}).replace("</", "<\\/")
            body = ('<div id=enc data-env=\'' + env.replace("'", "&#39;") + '\'>'
                    '<p style="color:var(--muted)">This note is encrypted in your browser. The passphrase never leaves this tab; '
                    'the site stores only ciphertext. Lose the passphrase and the note cannot be recovered.</p>'
                    '<form id=unlock style="display:flex;gap:8px;max-width:420px;margin-top:10px">'
                    '<input type=password id=pp required placeholder="Passphrase" autocomplete=off style="flex:1;font:inherit;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text)">'
                    '<button style="border:0;cursor:pointer;font:inherit;font-weight:600;padding:9px 18px;border-radius:999px;background:var(--accent);color:#fff">Unlock</button></form>'
                    '<p id=encerr style="color:var(--danger,#b3261e);font-size:13.5px"></p>'
                    '<div id=plain hidden><h1 class=title style="font-size:26px" id=enctitle></h1><div id=encbody></div>'
                    '<details class=drawer id=encdrawer hidden><summary>Transcript</summary><div class=tr id=enctr></div></details>'
                    '<details class=drawer><summary>Edit notes</summary>'
                    '<textarea id=encedit rows=14 style="width:100%;font:13.5px/1.5 ui-monospace,monospace;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text)"></textarea>'
                    '<p><button id=encsave style="border:0;cursor:pointer;font:inherit;font-weight:600;padding:9px 18px;border-radius:999px;background:var(--accent);color:#fff">Save encrypted</button> '
                    '<span id=encsaved style="color:var(--muted);font-size:13px"></span></p></details></div></div>'
                    + ENC_JS)
            edit = ""
        elif "notes_age" in b:
            body = ("<p style='color:var(--muted)'>These notes were encrypted on your device before syncing, so this site cannot read them. "
                    "Open them in the local Iris app or the Android app, which hold your key.</p>"); edit = ""
        else:
            body = "<p style='color:var(--muted)'>No notes in this synced meeting yet.</p>"; edit = ""
        tr = ""
        if b.get("transcript"):
            tr = (f'<details class=drawer><summary>Transcript</summary><div class=tr>'
                  + "".join(render_transcript_line(l) for l in b["transcript"].splitlines() if l.strip()) + "</div></details>")
        try: when = time.strftime("%B %-d, %Y %H:%M UTC", time.gmtime(float(meta.get("created") or b.get("_server_ts"))))
        except Exception: when = ""
        page = (f"<h1 class=title style='font-size:28px'>{esc(title)}</h1><div class=props><span>{when}</span><span>Synced from your device. No audio is stored here.</span></div>"
                + body + tr + edit
                + f'<form method=post action="/n/{esc(mid)}/delete" style="margin-top:24px" data-confirm="Remove from the cloud? Your local copy stays."><button style="border:1px solid var(--border);background:transparent;color:#b3261e;cursor:pointer;font:inherit;font-size:13px;padding:6px 14px;border-radius:999px">Remove from cloud</button></form>')
        return render_page(c, u, page, active="n:" + mid)

CT_KEYS = ("v", "alg", "kdf", "iter", "salt", "iv", "ct")
def _valid_ct(d, max_len=400_000):
    return (isinstance(d, dict) and d.get("v") == 1 and d.get("alg") == "AES-GCM"
            and d.get("kdf") == "PBKDF2-SHA256" and 100_000 <= int(d.get("iter", 0)) <= 2_000_000
            and all(isinstance(d.get(k), str) for k in ("salt", "iv", "ct")) and len(d.get("ct", "")) <= max_len)

@app.post("/n/{mid}/browser-encrypt")
async def browser_encrypt(mid: str, req: Request):
    """Replace a synced note's plaintext with browser-made ciphertext. The passphrase
    never leaves the user's tab; after this, the database holds only ciphertext for
    body, title and transcript. Visible afterwards: that the note exists, its id,
    timestamps and the placeholder 'Encrypted note'."""
    body = await req.json()
    with db() as c:
        u = current_user(c, req)
        if not u: return _err("login", 401)
        r = c.execute("SELECT bundle FROM synced WHERE user_id=%s AND id=%s", (u["id"], mid)).fetchone()
        if not r: return _err("not found", 404)
        b = r["bundle"]
        if (b.get("meta") or {}).get("browser_encrypted"): return _err("already encrypted", 409)
        if "notes" not in b: return _err("nothing readable to encrypt", 400)
        if not _valid_ct(body.get("notes_ct")) or not _valid_ct(body.get("title_ct"), 4_000):
            return _err("bad ciphertext envelope", 400)
        if b.get("transcript") and not _valid_ct(body.get("transcript_ct")):
            return _err("this note has a stored transcript; encrypt it too", 400)
        meta = b.get("meta") or {}
        meta["title"] = "Encrypted note"
        meta["browser_encrypted"] = True
        nb = {"meta": meta, "notes_ct": body["notes_ct"], "title_ct": body["title_ct"], "_server_ts": b.get("_server_ts", time.time())}
        if b.get("transcript"): nb["transcript_ct"] = body["transcript_ct"]
        c.execute("UPDATE synced SET bundle=%s WHERE user_id=%s AND id=%s", (json.dumps(nb), u["id"], mid))
        return {"ok": True}

@app.post("/n/{mid}/browser-update")
async def browser_update(mid: str, req: Request):
    """Store a new browser-made ciphertext for an already-encrypted note (edit flow)."""
    body = await req.json()
    with db() as c:
        u = current_user(c, req)
        if not u: return _err("login", 401)
        r = c.execute("SELECT bundle FROM synced WHERE user_id=%s AND id=%s", (u["id"], mid)).fetchone()
        if not r or not (r["bundle"].get("meta") or {}).get("browser_encrypted"):
            return _err("not found", 404)
        if not _valid_ct(body.get("notes_ct")): return _err("bad ciphertext envelope", 400)
        b = r["bundle"]; b["notes_ct"] = body["notes_ct"]
        if _valid_ct(body.get("title_ct"), 4_000): b["title_ct"] = body["title_ct"]
        c.execute("UPDATE synced SET bundle=%s, server_ts=%s WHERE user_id=%s AND id=%s",
                  (json.dumps(b), time.time(), u["id"], mid))
        return {"ok": True}

@app.post("/n/{mid}/edit")
async def synced_edit(mid: str, req: Request):
    f = await _form(req); text = (f.get("notes") or "")[:200_000]
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        r = c.execute("SELECT bundle FROM synced WHERE user_id=%s AND id=%s", (u["id"], mid)).fetchone()
        if not r or "notes" not in r["bundle"]: return render_page(c, u, "<p>Not found.</p>", status=404)
        b = r["bundle"]; now = time.time(); b["notes"] = text
        b.setdefault("meta", {})["edited"] = now; b["_server_ts"] = now
        c.execute("UPDATE synced SET bundle=%s, server_ts=%s WHERE user_id=%s AND id=%s", (json.dumps(b), now, u["id"], mid))
    return RedirectResponse(f"/n/{mid}", status_code=303)

@app.post("/n/{mid}/delete")
def synced_delete(mid: str, req: Request):
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        c.execute("DELETE FROM synced WHERE user_id=%s AND id=%s", (u["id"], mid))
    return RedirectResponse("/", status_code=303)

@app.post("/account/token")
async def account_token(req: Request):
    f = await _form(req)
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        if c.execute("SELECT count(*) AS n FROM api_tokens WHERE user_id=%s", (u["id"],)).fetchone()["n"] >= 5:
            return render_page(c, u, "<p>You already have 5 sync tokens. Revoke one on the Account page first.</p>", status=400)
        tok = "iris_" + secrets.token_urlsafe(30)
        c.execute("INSERT INTO api_tokens(token_hash,user_id,label) VALUES(%s,%s,%s)", (_th(tok), u["id"], (f.get("label") or "")[:60]))
        return render_page(c, u, "<h1 class=title style='font-size:26px'>Your sync token</h1><p>Shown once. Put it in the local app as <code>IRIS_SYNC_TOKEN</code>.</p>"
            f"<p style='font:600 15px ui-monospace,monospace;word-break:break-all;padding:12px;border:1px dashed var(--border);border-radius:10px'>{tok}</p>"
            f"<p><code>python3 iris/cli.py sync --server {esc(str(req.base_url).rstrip('/'))}/sync --notes-only</code></p><p><a href='/account'>Back to account</a></p>")

@app.post("/account/token/revoke")
async def account_token_revoke(req: Request):
    f = await _form(req)
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        c.execute("DELETE FROM api_tokens WHERE user_id=%s AND token_hash=%s", (u["id"], f.get("h") or ""))
    return RedirectResponse("/account", status_code=303)

@app.post("/account/transcripts")
async def account_transcripts(req: Request):
    f = await _form(req); on = f.get("on") == "1"
    with db() as c:
        u = current_user(c, req)
        if not u: return RedirectResponse("/login", status_code=303)
        c.execute("UPDATE users SET keep_transcripts=%s WHERE id=%s", (on, u["id"]))
        if not on:
            c.execute("UPDATE synced SET bundle = bundle - 'transcript' - 'transcript_age' WHERE user_id=%s", (u["id"],))
    return RedirectResponse("/account", status_code=303)

# ---------- opt-in backup: write notes from a transcript made on your own device ----------
MAX_NOTES_CALLS_PER_DAY = 30
TS_LINE = re.compile(r"^\[(\d+):(\d\d)\]\s*(?:\*\*[^*]+\*\*:?\s*)?(.+)$")

def parse_transcript(text: str) -> list:
    segs = []
    for line in text.splitlines():
        m = TS_LINE.match(line.strip())
        if m:
            t = int(m.group(1)) * 60 + int(m.group(2))
            segs.append({"start": float(t), "end": float(t) + 3.0, "text": m.group(3).strip()})
    for a, b in zip(segs, segs[1:]):
        a["end"] = max(a["start"] + 1.0, b["start"])
    return segs

@app.post("/api/notes")
async def api_notes(req: Request):
    """Backup path for a device that can make a transcript but not the notes. The transcript text goes to Groq,
    so the caller must say so. Audio is never involved. Nothing is stored."""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key: return _err("notes backup is not configured", 503)
    try: body = await req.json()
    except Exception: return _err("bad json", 400)
    if body.get("consent_groq") is not True: return _err("send consent_groq: true; the transcript text is sent to Groq to write the notes", 400)
    text = (body.get("transcript") or "")
    if len(text) > 16000: return _err("transcript too long for the backup path (16000 characters)", 413)
    segs = parse_transcript(text)
    if not segs: return _err("no [MM:SS] transcript lines found", 400)
    with db() as c:
        u = token_user(c, req)
        if not u: return _err("bad token", 403)
        k = f"notes:{u['id']}"
        if c.execute("SELECT count(*) AS n FROM login_fail WHERE email=%s AND ts>now()-interval '1 day'", (k,)).fetchone()["n"] >= MAX_NOTES_CALLS_PER_DAY:
            return _err("daily limit for the notes backup reached", 429)
        c.execute("INSERT INTO login_fail(email) VALUES(%s)", (k,))
    try:
        notes = verify.verify_notes(await _part_notes(segs, key), segs)
    except Exception as e:
        return _err(str(e)[:200], 502)
    return {"notes_md": render.render_notes("notes", notes, segs), "integrity": notes.get("integrity")}

@app.get("/manifest.webmanifest")
def manifest():
    m = json.loads(W.MANIFEST); m["shortcuts"] = [{"name": "My notes", "url": "/"}]
    return Response(json.dumps(m), media_type="application/manifest+json")

@app.get("/sw.js")
def sw(): return Response(W.SW, media_type="text/javascript", headers={"Service-Worker-Allowed": "/"})

@app.get("/icon-{size}.png")
def icon(size: str):
    d = W.asset(f"icon-{size}.png")
    return Response(d, media_type="image/png") if d else Response(status_code=404)

@app.get("/healthz")
def healthz(): return {"ok": True}
