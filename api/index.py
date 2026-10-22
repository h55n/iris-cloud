"""Hosted Iris: accounts, a Postgres database, and the call -> transcript -> notes
path on free-tier services. Whisper runs at Groq; notes are written by an open
model at Groq, then checked by the same verifier the self-hosted Iris uses."""
import hashlib, hmac, html, json, os, re, secrets, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx, psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from iris import verify, render, pwa as W
from iris.mascot import mascot
from iris.transcribe import transcript_markdown
from iris.webui import PAGE, esc, render_notes_md, render_transcript_line

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

def _th(tok): return hashlib.sha256(tok.encode()).hexdigest()

def start_session(c, user_id, resp):
    tok = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions VALUES(%s,%s, now() + interval '30 days')", (_th(tok), user_id))
    resp.set_cookie(COOKIE, tok, max_age=30 * 86400, httponly=True, secure=True, samesite="lax", path="/")

def current_user(c, req: Request):
    tok = req.cookies.get(COOKIE)
    if not tok: return None
    return c.execute("SELECT u.id, u.email FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires>now()", (_th(tok),)).fetchone()

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
    who = (f'<div class=nav-sec>Signed in</div><div class=nav-meta style="padding:2px 12px 8px">{esc(user["email"])}</div>'
           '<form method=post action="/logout" style="padding:0 12px"><button class=mi>Log out</button></form>') if user else \
          '<div style="padding:8px 12px"><a href="/login">Log in</a> &middot; <a href="/signup">Sign up</a></div>'
    return ('<input type=checkbox id=navtoggle><label for=navtoggle class=menubtn>Menu</label>'
            '<a href="/" style="text-decoration:none;color:inherit"><div class=brand>Iris<span id=tagline>meeting notes that keep receipts</span></div></a>'
            + ('<a class="folder" href="/record" style="margin:6px 0"><span class=chev></span>Record a meeting</a>' if user else '')
            + '<div class=nav>' + (f'<div class=nav-sec>Meetings</div>{items or "<div class=nav-meta style=padding:8px>Nothing recorded yet</div>"}' if user else '') + '</div>'
            + who
            + '<div class=themes><span class=lbl>Theme</span>' + ''.join(
                f'<button class=dot data-t="{t}" title="{t or "green"}" onclick="pickTheme(\'{t}\',this)"></button>'
                for t in ("", "blue", "purple", "orange", "pink", "red")) + '</div>'
            + '<script>function pickTheme(t,b){if(t)document.documentElement.dataset.theme=t;else delete document.documentElement.dataset.theme;'
              'try{localStorage.setItem("iris-theme",t)}catch(e){}document.querySelectorAll(".dot").forEach(function(d){d.classList.remove("sel")});b.classList.add("sel")}'
              '(function(){var cur="";try{cur=localStorage.getItem("iris-theme")||""}catch(e){}var d=document.querySelector(\'.dot[data-t="\'+cur+\'"]\');if(d)d.classList.add("sel")})()</script>')

def render_page(c, user, body, active=None, status=200):
    from iris import pwa as W
    inner = (f'<div class=layout><nav class=sidebar>{sidebar(c, user, active)}</nav>'
             f'<main class=content><div class=doc>{body}</div></main></div>')
    doc = (PAGE.replace("<!--PWA-->", W.HEAD_TAGS) + inner + W.SW_REGISTER)
    return HTMLResponse(doc, status_code=status)

PRIVACY = ("<p style='color:var(--muted);font-size:13.5px;line-height:1.55'><b>How hosted mode handles your meetings.</b> "
           "Your recording is sent to Groq to be transcribed (Whisper), and the transcript is sent to an open model at Groq to write the notes. "
           "We keep the transcript and notes in our database, tied to your account. We do not keep the audio. "
           "Do not use hosted mode for confidential meetings. For fully private use, "
           "<a href='https://github.com/h55n/iris'>self-host Iris</a>: there, audio never leaves your own machine.</p>")

def landing(c, user):
    cta = ('<a class=cta href="/record">Record a meeting</a>' if user else
           '<a class=cta href="/signup">Create an account</a> <a class=cta2 href="/login">Log in</a>')
    return (mascot(120, "celebrate") + "<h1 class=title style='text-align:center;font-size:34px'>Iris</h1>"
            "<p style='text-align:center;color:var(--muted);margin:0 0 18px'>Record a meeting. Get notes where every decision shows the line it came from.</p>"
            f"<div style='text-align:center;margin-bottom:26px'>{cta}</div>"
            "<style>.cta,.cta2{display:inline-block;padding:10px 20px;border-radius:999px;text-decoration:none;font-weight:600;margin:0 4px}"
            ".cta{background:var(--accent);color:#fff}.cta2{border:1px solid var(--border);color:var(--text)}</style>" + PRIVACY)

def auth_form(kind, err="", email=""):
    t = "Create your account" if kind == "signup" else "Log in"
    other = ('<a href="/login">Have an account? Log in</a>' if kind == "signup" else '<a href="/signup">New here? Create an account</a>')
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
            "<p style='text-align:center'><a class=cta href=/record style='display:inline-block;padding:10px 20px;border-radius:999px;background:var(--accent);color:#fff;text-decoration:none;font-weight:600'>Record a meeting</a></p>"
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
        row = c.execute("INSERT INTO users(email,pw_hash) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING id", (email, hash_pw(pw))).fetchone()
        if not row: return bad("An account with that email already exists. Try logging in.")
        resp = RedirectResponse("/record", status_code=303); start_session(c, row["id"], resp); return resp

@app.post("/login")
async def login(req: Request):
    f = await _form(req); email = (f.get("email") or "").strip().lower(); pw = f.get("password") or ""
    with db() as c:
        fails = c.execute("SELECT count(*) AS n FROM login_fail WHERE email=%s AND ts>now()-interval '10 minutes'", (email,)).fetchone()["n"]
        if fails >= 8: return render_page(c, None, auth_form("login", "Too many attempts. Wait ten minutes.", email), status=429)
        u = c.execute("SELECT id,pw_hash FROM users WHERE email=%s", (email,)).fetchone()
        if not u or not check_pw(pw, u["pw_hash"]):
            c.execute("INSERT INTO login_fail(email) VALUES(%s)", (email,))
            return render_page(c, None, auth_form("login", "Wrong email or password.", email), status=401)
        resp = RedirectResponse("/", status_code=303); start_session(c, u["id"], resp); return resp

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
                "<h1 class=title style='font-size:30px'>Record a meeting</h1>" + PRIVACY +
                '<input id=rtitle placeholder="Title (optional)" maxlength=120>'
                '<div style="display:flex;gap:10px;align-items:center"><button id=rbtn type=button>Start recording</button>'
                '<span id=rtime style="font-variant-numeric:tabular-nums;color:var(--muted)">00:00</span></div>'
                '<p id=rmsg style="color:var(--muted);margin-top:14px"></p><p id=rlog style="color:var(--faint);font-size:13px"></p>' + RECORD_JS)
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
        m = c.execute("SELECT status, seg_count FROM meetings WHERE id=%s AND user_id=%s", (mid, u["id"])).fetchone()
        if not m: return _err("no such meeting", 404)
        if m["status"] not in ("recording", "error"): return _err("meeting already finished", 409)
        if m["seg_count"] >= MAX_SEGMENTS_PER_MEETING: return _err("meeting too long for hosted mode", 413)
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
    with db() as c:
        c.execute("UPDATE meetings SET segs = segs || %s::jsonb, seg_count = seg_count + 1 WHERE id=%s AND user_id=%s", (json.dumps(segs), mid, u["id"]))
        total = c.execute("SELECT jsonb_array_length(segs) AS n FROM meetings WHERE id=%s", (mid,)).fetchone()["n"]
    return {"segments": total, "added": len(segs), "limits": limits}

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
    tmd = transcript_markdown(segs)
    try:
        async with httpx.AsyncClient(timeout=55) as h:
            r = await h.post(f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                             json={"model": NOTES_MODEL, "temperature": 0.2, "response_format": {"type": "json_object"},
                                   "messages": [{"role": "user", "content": NOTES_PROMPT + tmd[:60000]}]})
        if r.status_code != 200: raise RuntimeError(f"notes model error {r.status_code}: {r.text[:160]}")
        notes = verify.verify_notes(_parse_notes(r.json()["choices"][0]["message"]["content"]), segs)
        md = render.render_notes(mid, notes, segs)
        with db() as c:
            c.execute("UPDATE meetings SET status='done', notes_md=%s, integrity=%s, error=NULL WHERE id=%s", (md, json.dumps(notes.get("integrity")), mid))
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
        notes_html = render_notes_md(m["notes_md"]) if m["notes_md"] else (
            f"<p style='color:var(--muted)'>Status: {esc(m['status'])}" + (f" - {esc(m['error'])}" if m["error"] else "") + "</p>")
        tr = "".join(render_transcript_line(l) for l in tmd.splitlines() if l.strip())
        body = ((mascot(110, "celebrate") + "<div class=empty style='padding-top:0'><div class=t>Meeting saved</div></div>") if m["status"] == "done" and (time.time() - m["created"].timestamp()) < 600 else "")
        body += (f'<form method=post action="/m/{mid}/rename" style="margin:0"><input name=title class=title value="{esc(m["title"] or "Untitled meeting")}" maxlength=120 style="width:100%" aria-label="Title" onchange="this.form.submit()"></form>'
                 f'<div class=props><span>{m["created"].strftime("%B %-d, %Y %H:%M UTC")}</span></div>'
                 + notes_html
                 + (f'<details class=drawer><summary>Transcript ({len(segs)} lines)</summary><div class=tr>{tr}</div></details>' if segs else "")
                 + f'<form method=post action="/m/{mid}/delete" onsubmit="return confirm(\'Delete this meeting for good?\')" style="margin-top:24px"><button class=danger>Delete meeting</button></form>')
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
@app.get("/manifest.webmanifest")
def manifest():
    m = json.loads(W.MANIFEST); m["shortcuts"] = [{"name": "Record a meeting", "url": "/record"}]
    return Response(json.dumps(m), media_type="application/manifest+json")

@app.get("/sw.js")
def sw(): return Response(W.SW, media_type="text/javascript", headers={"Service-Worker-Allowed": "/"})

@app.get("/icon-{size}.png")
def icon(size: str):
    d = W.asset(f"icon-{size}.png")
    return Response(d, media_type="image/png") if d else Response(status_code=404)

@app.get("/healthz")
def healthz(): return {"ok": True}
