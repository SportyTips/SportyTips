"""Web frontend for SportyTips.
"""

import base64
import functools
import hashlib
import hmac
import html
import importlib
import json
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, Response, abort, g, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RATE_LIMIT = 8
RATE_WINDOW = 60
TOOL_LIMIT = 30
E_FAIL = "\u274C"

# ===============================================================
# IMPORT MODULES
# ===============================================================
try:
    import sportybet_provider
    print("OK: sportybet_provider.py loaded successfully")
except Exception as exc:
    print("FAILED: sportybet_provider.py failed to load:", str(exc))

try:
    bot = importlib.import_module("main")
    print("OK: main.py loaded successfully")
except Exception as exc:
    print("FAILED: main.py failed to load:", str(exc))
    raise

SMART_TICKET_MODULE = None
try:
    SMART_TICKET_MODULE = importlib.import_module("smart_ticket")
    print("OK: smart_ticket.py loaded successfully")
except Exception as exc:
    print("FAILED: smart_ticket.py failed to load:", str(exc))

# We delete launcher.py and upgrades.py from here to clean up the code.

# ===============================================================
# WEB OUTPUT
# ===============================================================
_ctx = threading.local()

def _emit(item):
    q = getattr(_ctx, "q", None)
    if q is not None:
        q.put(item)

HIDE_LINES = re.compile(r"API-Football|API limit|API problem|API requests|Daily API|Booking code failed|SportyBet: |plan only allows", re.I)

def _rebrand(text):
    return text.replace("SamuelBet AI", "SportyTips").replace("SamuelBet", "SportyTips")

def web_send_message(chat_id, text):
    kept = [line for line in text.split("\n") if not HIDE_LINES.search(line)]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if not cleaned:
        cleaned = "I couldn't get that right now. Please try again in a minute."
    cleaned = cleaned.replace("&#x27;", "'").replace("&#39;", "'").replace("&quot;", '"')
    _emit({"type": "text", "html": _rebrand(cleaned)})

def web_send_photo(chat_id, png, caption=""):
    _emit({"type": "image", "data": base64.b64encode(png).decode("ascii"), "caption": caption})

bot.send_message = web_send_message
bot.telegram_request = lambda method, params=None: {"ok": True, "result": []}

# ===============================================================
# FLASK
# ===============================================================
app = Flask(__name__)

def _load_secret_key():
    env_key = os.getenv("SECRET_KEY")
    if env_key: return env_key
    return secrets.token_hex(32)

app.secret_key = _load_secret_key()
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1")

ACCOUNT_DB = os.getenv("ACCOUNT_DB", os.path.join(BASE_DIR, "sportytips_users.db"))

# ===============================================================
# PREMIUM SETTINGS
# ===============================================================
PLANS = {"monthly": {"label": "Monthly", "amount": 3000, "days": 30}, "yearly": {"label": "Yearly", "amount": 30000, "days": 365}}
ADMIN_EMAILS = {"adedayo3436@gmail.com", "enikejy@gmail.com", "yolamide1709@gmail.com"} | {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
try: PROMO_PREMIUM_UNTIL = float(os.getenv("PROMO_PREMIUM_UNTIL", "0") or 0)
except ValueError: PROMO_PREMIUM_UNTIL = 0.0
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")

# ===============================================================
# DATABASE
# ===============================================================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip().strip("\"'")
if DATABASE_URL.startswith("postgres://"): DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_PG = DATABASE_URL.startswith("postgresql://")

if USE_PG:
    import psycopg2
    import psycopg2.extras
    DB_INTEGRITY_ERROR = psycopg2.IntegrityError
    print("OK: using Postgres (Neon) for accounts")
else:
    DB_INTEGRITY_ERROR = sqlite3.IntegrityError

class _PgDb:
    def __init__(self): self.raw = psycopg2.connect(DATABASE_URL, connect_timeout=15)
    def execute(self, sql, params=()):
        cur = self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), tuple(params))
        return cur
    def commit(self): self.raw.commit()
    def close(self):
        try: self.raw.close()
        except: pass

def _db():
    if USE_PG: return _PgDb()
    conn = sqlite3.connect(ACCOUNT_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def _insert_returning_id(conn, sql, params):
    if USE_PG: return conn.execute(sql + " RETURNING id", params).fetchone()["id"]
    return conn.execute(sql, params).lastrowid

def _init_account_db():
    conn = _db()
    try:
        if USE_PG:
            conn.execute("""CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, name TEXT, identifier TEXT UNIQUE, password_hash TEXT, created_at TEXT, verified INTEGER DEFAULT 0, verification_code_hash TEXT, verification_expires_at TEXT, verification_attempts INTEGER DEFAULT 0, reset_code_hash TEXT, reset_expires_at TEXT, reset_attempts INTEGER DEFAULT 0, session_version INTEGER DEFAULT 0, premium_until DOUBLE PRECISION DEFAULT 0, premium_plan TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS payments (reference TEXT PRIMARY KEY, user_id INTEGER, plan TEXT, amount_kobo INTEGER, status TEXT DEFAULT 'pending', created_at TEXT, paid_at TEXT)""")
        else:
            conn.execute("""CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, identifier TEXT UNIQUE, password_hash TEXT, created_at TEXT, verified INTEGER DEFAULT 0, verification_code_hash TEXT, verification_expires_at TEXT, verification_attempts INTEGER DEFAULT 0, reset_code_hash TEXT, reset_expires_at TEXT, reset_attempts INTEGER DEFAULT 0, session_version INTEGER DEFAULT 0, premium_until REAL DEFAULT 0, premium_plan TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS payments (reference TEXT PRIMARY KEY, user_id INTEGER, plan TEXT, amount_kobo INTEGER, status TEXT DEFAULT 'pending', created_at TEXT, paid_at TEXT)""")
        conn.commit()
    finally: conn.close()

try: _init_account_db()
except: pass

# ===============================================================
# ACCOUNT HELPERS
# ===============================================================
def _normalise_identifier(value): return str(value or "").strip().lower()
def _valid_identifier(identifier): return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", identifier)) and len(identifier) <= 254

def _premium_info(row):
    admin = row["identifier"] in ADMIN_EMAILS
    until = float(row["premium_until"] or 0)
    active = admin or (PROMO_PREMIUM_UNTIL > time.time()) or until > time.time()
    return {"active": active, "admin": admin, "plan": "admin" if admin else ("promo" if active else None), "expires_at": datetime.fromtimestamp(max(until, PROMO_PREMIUM_UNTIL), timezone.utc).isoformat() if active else None}

def _public_user(row): return {"id": int(row["id"]), "name": row["name"], "identifier": row["identifier"], "created_at": row["created_at"], "premium": _premium_info(row)}
def _set_session(row): session.clear(); session.permanent = True; session["user_id"] = int(row["id"]); session["sv"] = int(row["session_version"] or 0)

def current_user():
    uid = session.get("user_id")
    if not uid: return None
    conn = _db()
    try: row = conn.execute("SELECT * FROM users WHERE id = ?", (int(uid),)).fetchone()
    finally: conn.close()
    if row is None or int(row["verified"] or 0) != 1 or int(row["session_version"] or 0) != int(session.get("sv", 0)): return None
    return row

def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None: return jsonify(error="Please log in to continue.", login_required=True), 401
        g.user = user
        return fn(*args, **kwargs)
    return wrapper

# ===============================================================
# EMAIL
# ===============================================================
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "").strip()
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "SportyTips").strip() or "SportyTips"
CODE_TTL_SECONDS = 10 * 60

def _send_code_email(email, name, code, purpose="verify"):
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL: raise RuntimeError("Brevo email is not configured on the server.")
    subject = "Reset your SportyTips password" if purpose == "reset" else "Verify your SportyTips account"
    payload = {"sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL}, "to": [{"email": email, "name": name}], "subject": subject, "htmlContent": f"<h2>SportyTips</h2><p>Hi {html.escape(name)},</p><p>Your code is: <b>{code}</b></p>", "textContent": f"SportyTips\n\nHi {name},\n\nYour code is: {code}"}
    req = Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode("utf-8"), method="POST", headers={"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"})
    try:
        with urlopen(req, timeout=20) as response: pass
    except Exception as exc: raise RuntimeError(f"Brevo could not send the email: {exc}")

def _new_code(): return f"{secrets.randbelow(1000000):06d}"
def _expiry_text(): return (datetime.now(timezone.utc) + timedelta(seconds=CODE_TTL_SECONDS)).isoformat()
def _is_expired(text):
    try: return datetime.fromisoformat(text or "").timestamp() < datetime.now(timezone.utc).timestamp()
    except: return True

RUN_LOCK = threading.Lock()
_hits = {}

def too_fast(key, limit=RATE_LIMIT):
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < RATE_WINDOW]
    if len(recent) >= limit: _hits[key] = recent; return True
    recent.append(now); _hits[key] = recent; return False

def client_ip(): return request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?"

PREMIUM_MESSAGE = "Free accounts can only use Daily 2 odds. Upgrade to Premium to build any other ticket and check or edit codes."
FREE_REQUESTS = {"daily2odds", "daily2odd"}
def _is_free_request(text): return re.sub(r"[^a-z0-9]", "", str(text).lower()) in FREE_REQUESTS
def _is_premium(user): return _premium_info(user)["active"]

# ===============================================================
# RUN TURN (FORCED TO USE SMART TICKET)
# ===============================================================
def _release_memory():
    try:
        import gc; gc.collect(); import ctypes; ctypes.CDLL("libc.so.6").malloc_trim(0)
    except: pass

def run_turn(sid, text, q):
    _ctx.q = q
    try:
        with RUN_LOCK:
            # Force the request to go to smart_ticket.py so it uses SportAPI stats
            if SMART_TICKET_MODULE is not None:
                SMART_TICKET_MODULE.flow(sid, text)
            else:
                bot.handle_text(sid, text)
    except Exception as exc:
        print(f"Web turn failed: {type(exc).__name__}: {exc}")
        q.put({"type": "text", "html": f"{E_FAIL} I could not complete that request right now."})
    finally:
        q.put(None)
        _release_memory()

# ===============================================================
# PAGES & PWA
# ===============================================================
@app.get("/")
def home_page():
    return redirect("/chat")

@app.get("/chat")
def chat_page():
    path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(path): return "index.html is missing next to app.py.", 404
    with open(path, encoding="utf-8") as fh: page = _rebrand(fh.read())
    return Response(page, mimetype="text/html", headers={"Cache-Control": "no-cache"})

# ===============================================================
# ACCOUNT ROUTES
# ===============================================================
@app.post("/api/auth/signup")
def auth_signup():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:80]
    identifier = _normalise_identifier(data.get("identifier", ""))
    password = str(data.get("password", ""))
    if len(name) < 2: return jsonify(error="Please enter your name."), 400
    if not _valid_identifier(identifier): return jsonify(error="Enter a valid email address."), 400
    if len(password) < 8: return jsonify(error="Password must be at least 8 characters."), 400
    password_hash = generate_password_hash(password)
    code = _new_code()
    conn = _db()
    try:
        existing = conn.execute("SELECT id, verified FROM users WHERE identifier = ?", (identifier,)).fetchone()
        if existing and int(existing["verified"] or 0) == 1: return jsonify(error="Account exists. Log in."), 409
        if existing:
            conn.execute("UPDATE users SET name = ?, password_hash = ?, verification_code_hash = ?, verification_expires_at = ? WHERE id = ?", (name, password_hash, generate_password_hash(code), _expiry_text(), int(existing["id"])))
            user_id = int(existing["id"])
        else:
            user_id = _insert_returning_id(conn, "INSERT INTO users (name, identifier, password_hash, created_at, verified, verification_code_hash, verification_expires_at) VALUES (?, ?, ?, ?, 0, ?, ?)", (name, identifier, password_hash, datetime.now(timezone.utc).isoformat(), generate_password_hash(code), _expiry_text()))
        conn.commit()
    finally: conn.close()
    _send_code_email(identifier, name, code, "verify")
    return jsonify(verification_required=True, email=identifier), 201

@app.post("/api/auth/verify")
def auth_verify():
    data = request.get_json(silent=True) or {}
    identifier = _normalise_identifier(data.get("identifier", ""))
    code = re.sub(r"\D", "", str(data.get("code", "")))[:6]
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM users WHERE identifier = ?", (identifier,)).fetchone()
        if row is None: return jsonify(error="Account not found."), 404
        if int(row["verified"] or 0) == 1: return jsonify(error="Email already verified."), 400
        if _is_expired(row["verification_expires_at"]): return jsonify(error="Code expired."), 400
        if not check_password_hash(row["verification_code_hash"] or "", code): return jsonify(error="Incorrect code."), 400
        conn.execute("UPDATE users SET verified = 1, verification_code_hash = NULL WHERE id = ?", (int(row["id"]),))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (int(row["id"]),)).fetchone()
    finally: conn.close()
    _set_session(row)
    return jsonify(user=_public_user(row), verified=True)

@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(silent=True) or {}
    identifier = _normalise_identifier(data.get("identifier", ""))
    password = str(data.get("password", ""))
    conn = _db()
    try: row = conn.execute("SELECT * FROM users WHERE identifier = ?", (identifier,)).fetchone()
    finally: conn.close()
    if row is None or not check_password_hash(row["password_hash"], password): return jsonify(error="Incorrect credentials."), 401
    if int(row["verified"] or 0) != 1: return jsonify(error="Please verify your email.", verification_required=True, email=identifier), 403
    _set_session(row)
    return jsonify(user=_public_user(row))

@app.get("/api/auth/me")
def auth_me():
    user = current_user()
    if user is None: session.clear(); return jsonify(user=None)
    return jsonify(user=_public_user(user))

@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify(ok=True)

# ===============================================================
# CHAT API
# ===============================================================
def _stream_response(q):
    def stream():
        while True:
            try: item = q.get(timeout=15)
            except queue.Empty: yield "\n"; continue
            if item is None: break
            yield json.dumps(item) + "\n"
    return Response(stream(), mimetype="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

def _start_chat_turn(sid, text):
    user = current_user()
    if user is None: return jsonify(error="Please log in to continue.", login_required=True), 401
    if too_fast(client_ip()): return jsonify(error="Slow down. Try again in a minute."), 429
    if not _is_free_request(text) and not _is_premium(user): return jsonify(error=PREMIUM_MESSAGE, upgrade=True), 402
    q = queue.Queue()
    threading.Thread(target=run_turn, args=(sid, text, q), daemon=True).start()
    return _stream_response(q)

@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()[:500]
    sid = str(data.get("session", ""))[:64]
    if not text or not sid: return jsonify(error="Empty message."), 400
    return _start_chat_turn(sid, text)

@app.get("/health")
def health(): return jsonify(status="ok")

# ===============================================================
# DEBUG ROUTE
# ===============================================================
@app.route('/debug-sportapi/<int:team_id>')
def debug_sportapi(team_id):
    import sportapi_data as sd
    import json
    try:
        result = sd._api_get(f"/teams/{team_id}/stats")
        return f"<pre>{json.dumps(result, indent=2)}</pre>"
    except Exception as e:
        return f"Error: {str(e)}"