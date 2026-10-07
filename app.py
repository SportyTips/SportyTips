"""Web frontend for SportyTips.

Does NOT edit your bot files. It imports them and connects the
SportyTips ticket builder to the web frontend.

Pages:
  /       homepage
  /chat   chat page

Production:
  gunicorn app:app -w 1 --threads 8 --timeout 600

Environment:
  USE_SPORTYBET=1
  SECRET_KEY=...                 (recommended)
  ADMIN_EMAILS=a@x.com,b@x.com   (optional extra admins; the built-in admins are in this file)
  BREVO_API_KEY / BREVO_SENDER_EMAIL / BREVO_SENDER_NAME   (emails)
  PAYSTACK_SECRET_KEY=sk_live_...                           (Premium payments)
  APP_BASE_URL=https://yourdomain.com                       (payment return URL)
  COOKIE_SECURE=1                (recommended on HTTPS)
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

from flask import Flask, Response, g, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RATE_LIMIT = 8
RATE_WINDOW = 60
TOOL_LIMIT = 30

LAUNCHER_MODULE = os.getenv("LAUNCHER_MODULE", "launcher")

E_FAIL = "\u274C"   # cross mark


# ===============================================================
# IMPORTANT IMPORT ORDER
# ===============================================================
# SportyBet provider MUST be completely loaded before main.py
# and smart_ticket.py are imported.
# ===============================================================

SPORTYBET_PROVIDER_MODULE = None
SPORTYBET_PROVIDER_ERROR = None

try:
    SPORTYBET_PROVIDER_MODULE = importlib.import_module("sportybet_provider")
    print("OK: sportybet_provider.py loaded successfully")

except Exception as exc:
    SPORTYBET_PROVIDER_ERROR = f"{type(exc).__name__}: {exc}"
    print("FAILED: sportybet_provider.py failed to load:", SPORTYBET_PROVIDER_ERROR)


# ===============================================================
# LOAD MAIN
# ===============================================================

try:
    bot = importlib.import_module("main")
    print("OK: main.py loaded successfully")

except Exception as exc:
    print("FAILED: main.py failed to load:", f"{type(exc).__name__}: {exc}")
    raise


# ===============================================================
# LOAD SMART TICKET BUILDER
# ===============================================================

SMART_TICKET_MODULE = None
SMART_TICKET_ERROR = None

try:
    SMART_TICKET_MODULE = importlib.import_module("smart_ticket")
    print("OK: smart_ticket.py loaded successfully")

except Exception as exc:
    SMART_TICKET_ERROR = f"{type(exc).__name__}: {exc}"
    print("FAILED: smart_ticket.py failed to load:", SMART_TICKET_ERROR)


# ===============================================================
# LOAD LAUNCHER
# ===============================================================

try:
    launcher = importlib.import_module(LAUNCHER_MODULE)
    print(f"OK: {LAUNCHER_MODULE}.py loaded successfully")

except Exception as exc:
    print(
        f"FAILED: {LAUNCHER_MODULE}.py failed to load:",
        f"{type(exc).__name__}: {exc}",
    )
    raise


# ===============================================================
# OPTIONAL UPGRADES
# ===============================================================

try:
    upgrades = importlib.import_module("upgrades")
except ImportError:
    upgrades = None


# ===============================================================
# WEB OUTPUT
# ===============================================================

_ctx = threading.local()

def _emit(item):
    q = getattr(_ctx, "q", None)

    if q is not None:
        q.put(item)


HIDE_LINES = re.compile(
    r"API-Football|API limit|API problem|API requests|Daily API|"
    r"Booking code failed|SportyBet: |plan only allows",
    re.I,
)


def _rebrand(text):
    """Always display SportyTips instead of older branding."""

    text = re.sub(
        r"SamuelBet\s*(<span[^>]*>)\s*AI\s*(</span>)",
        r"Sporty\1Tips\2",
        text,
    )

    return (
        text
        .replace("SamuelBet AI", "SportyTips")
        .replace("SamuelBet", "SportyTips")
    )


def web_send_message(chat_id, text):
    kept = [
        line
        for line in text.split("\n")
        if not HIDE_LINES.search(line)
    ]

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

    if not cleaned:
        cleaned = "I couldn't get that right now. Please try again in a minute."

    cleaned = (
        cleaned
        .replace("&#x27;", "'")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )

    _emit({"type": "text", "html": _rebrand(cleaned)})


def web_send_photo(chat_id, png, caption=""):
    _emit(
        {
            "type": "image",
            "data": base64.b64encode(png).decode("ascii"),
            "caption": caption,
        }
    )


# ===============================================================
# REPLACE TELEGRAM OUTPUT WITH WEB OUTPUT
# ===============================================================

bot.send_message = web_send_message

bot.telegram_request = lambda method, params=None: {"ok": True, "result": []}

launcher.send_photo = web_send_photo


# ===============================================================
# FLASK
# ===============================================================

app = Flask(__name__)


def _load_secret_key():
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key

    path = os.path.join(BASE_DIR, ".sportytips_secret")
    try:
        if os.path.exists(path):
            value = open(path, "r", encoding="utf-8").read().strip()
            if value:
                return value

        value = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value
    except OSError:
        return secrets.token_hex(32)


app.secret_key = _load_secret_key()
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
)

ACCOUNT_DB = os.getenv(
    "ACCOUNT_DB",
    os.path.join(BASE_DIR, "sportytips_users.db"),
)


# ===============================================================
# PREMIUM SETTINGS
# ===============================================================

# Prices are decided here on the server. The browser never sends an amount.
PLANS = {
    "monthly": {"label": "Monthly", "amount": 3000, "days": 30},
    "yearly": {"label": "Yearly", "amount": 30000, "days": 365},
}

# Admin accounts: Premium free for life. They still have to verify their
# email with the 6-digit code, so nobody can claim these by typing the address.
ADMIN_EMAILS = {
    "adedayo3436@gmail.com",
    "enikejy@gmail.com",
    "yolamide1709@gmail.com",
} | {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")


# ===============================================================
# DATABASE
# ===============================================================

def _db():
    conn = sqlite3.connect(ACCOUNT_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_account_db():
    conn = _db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                identifier TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_code_hash TEXT,
                verification_expires_at TEXT,
                verification_attempts INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        had_verified_column = "verified" in columns
        migrations = {
            "verified": "ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0",
            "verification_code_hash": "ALTER TABLE users ADD COLUMN verification_code_hash TEXT",
            "verification_expires_at": "ALTER TABLE users ADD COLUMN verification_expires_at TEXT",
            "verification_attempts": "ALTER TABLE users ADD COLUMN verification_attempts INTEGER NOT NULL DEFAULT 0",
            "reset_code_hash": "ALTER TABLE users ADD COLUMN reset_code_hash TEXT",
            "reset_expires_at": "ALTER TABLE users ADD COLUMN reset_expires_at TEXT",
            "reset_attempts": "ALTER TABLE users ADD COLUMN reset_attempts INTEGER NOT NULL DEFAULT 0",
            "session_version": "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0",
            "premium_until": "ALTER TABLE users ADD COLUMN premium_until REAL NOT NULL DEFAULT 0",
            "premium_plan": "ALTER TABLE users ADD COLUMN premium_plan TEXT",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)

        if not had_verified_column:
            conn.execute("UPDATE users SET verified = 1")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                reference TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                amount_kobo INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


_init_account_db()


# ===============================================================
# ACCOUNT HELPERS
# ===============================================================

def _normalise_identifier(value):
    return str(value or "").strip().lower()


def _valid_identifier(identifier):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", identifier)) and len(identifier) <= 254


def _premium_info(row):
    admin = row["identifier"] in ADMIN_EMAILS
    until = float(row["premium_until"] or 0)
    active = admin or until > time.time()

    info = {"active": active, "admin": admin, "plan": None, "expires_at": None}

    if admin:
        info["plan"] = "admin"
    elif active:
        info["plan"] = row["premium_plan"]
        info["expires_at"] = datetime.fromtimestamp(until, timezone.utc).isoformat()

    return info


def _public_user(row):
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "identifier": row["identifier"],
        "created_at": row["created_at"],
        "premium": _premium_info(row),
    }


def _set_session(row):
    session.clear()
    session.permanent = True
    session["user_id"] = int(row["id"])
    session["sv"] = int(row["session_version"] or 0)


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None

    conn = _db()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (int(uid),)).fetchone()
    finally:
        conn.close()

    if row is None or int(row["verified"] or 0) != 1:
        return None

    # Changing or resetting a password signs out every other device.
    if int(row["session_version"] or 0) != int(session.get("sv", 0)):
        return None

    return row


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify(error="Please log in to continue.", login_required=True), 401
        g.user = user
        return fn(*args, **kwargs)

    return wrapper


# ===============================================================
# EMAIL (BREVO)
# ===============================================================

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "").strip()
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "SportyTips").strip() or "SportyTips"
CODE_TTL_SECONDS = 10 * 60


def _send_code_email(email, name, code, purpose="verify"):
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        raise RuntimeError("Brevo email is not configured on the server.")

    if purpose == "reset":
        subject = "Reset your SportyTips password"
        intro = "We received a request to reset your SportyTips password."
        action = "Enter this code to choose a new password:"
        ignore = "If you did not ask for this, ignore this email. Your password will not change."
    else:
        subject = "Verify your SportyTips account"
        intro = "Thanks for creating your SportyTips account."
        action = "Enter this verification code to activate your account:"
        ignore = "If you did not create a SportyTips account, you can safely ignore this email."

    safe_name = html.escape(name, quote=True)

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": email, "name": name}],
        "subject": subject,
        "htmlContent": (
            "<!doctype html><html><body style=\"font-family:Arial,sans-serif;line-height:1.6;color:#222\">"
            f"<h2>SportyTips</h2><p>Hi {safe_name},</p>"
            f"<p>{intro}</p><p>{action}</p>"
            f"<p style=\"font-size:30px;font-weight:700;letter-spacing:6px\">{code}</p>"
            "<p>This code expires in 10 minutes.</p>"
            f"<p>{ignore}</p>"
            "<p>- SportyTips Team</p></body></html>"
        ),
        "textContent": (
            f"SportyTips\n\nHi {name},\n\n{intro}\n\n{action}\n{code}\n\n"
            f"This code expires in 10 minutes.\n\n{ignore}\n\n- SportyTips Team"
        ),
    }

    req = Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "accept": "application/json",
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=20) as response:
            if response.status not in (200, 201, 202):
                raise RuntimeError(f"Brevo returned HTTP {response.status}.")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Brevo could not send the email ({exc.code}). {detail}") from exc
    except URLError as exc:
        raise RuntimeError("Could not reach Brevo. Please try again.") from exc


def _new_code():
    return f"{secrets.randbelow(1000000):06d}"


def _expiry_text():
    ts = datetime.now(timezone.utc).timestamp() + CODE_TTL_SECONDS
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _is_expired(text):
    try:
        return datetime.fromisoformat(text or "").timestamp() < datetime.now(timezone.utc).timestamp()
    except ValueError:
        return True


RUN_LOCK = threading.Lock()

_hits = {}


# ===============================================================
# RATE LIMIT
# ===============================================================

def too_fast(key, limit=RATE_LIMIT):
    now = time.time()

    recent = [t for t in _hits.get(key, []) if now - t < RATE_WINDOW]

    if len(recent) >= limit:
        _hits[key] = recent
        return True

    recent.append(now)
    _hits[key] = recent

    return False


def client_ip():
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "?"
    )


# ===============================================================
# FREE ACCOUNTS: DAILY 2 ODDS ONLY
# ===============================================================

PREMIUM_MESSAGE = (
    "Free accounts can only use Daily 2 odds. "
    "Upgrade to Premium to build any other ticket, edit codes and make tickets safer."
)

# The one thing a free account can ask for. Anything else needs Premium.
FREE_REQUESTS = {"daily2odds", "daily2odd"}


def _is_free_request(text):
    compact = re.sub(r"[^a-z0-9]", "", str(text).lower())
    return compact in FREE_REQUESTS


def _is_premium(user):
    return _premium_info(user)["active"]


# ===============================================================
# RUN ONE CHAT TURN
# ===============================================================

def run_turn(sid, text, q):
    _ctx.q = q

    try:

        with RUN_LOCK:
            bot.handle_text(sid, text)

    except Exception as exc:

        print(f"Web turn failed: {type(exc).__name__}: {exc}")

        q.put(
            {
                "type": "text",
                "html": (
                    f"{E_FAIL} I could not complete that request right now. "
                    "Please try again in a moment."
                ),
            }
        )

    finally:

        q.put(None)


# ===============================================================
# CHAT EXTRAS
# ===============================================================

CHAT_EXTRAS = r"""
<style>
  .code-copy { margin-left: 10px; border: 0; background: #ff1f1f; color: #fff; font: inherit; font-size: 14px; font-weight: 700; padding: 6px 15px; border-radius: 999px; cursor: pointer; vertical-align: middle; }
  .code-copy:active { transform: scale(.96); }
  .code-copy.done { background: #16a765; }
  .content strong.is-code { font-size: 20px; letter-spacing: .04em; }
</style>

<script>
(function () {
  var box = document.getElementById("messages");
  if (!box) return;
  var CODE = /^[A-Z0-9]{5,12}$/;

  function copyText(text, btn) {
    function done() {
      btn.textContent = "Copied";
      btn.classList.add("done");
      setTimeout(function () { btn.textContent = "Copy"; btn.classList.remove("done"); }, 1500);
    }
    function fallback() {
      var t = document.createElement("textarea");
      t.value = text;
      t.style.position = "fixed";
      t.style.opacity = "0";
      document.body.appendChild(t);
      t.select();
      try { document.execCommand("copy"); } catch (e) {}
      t.remove();
      done();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  function scan() {
    box.querySelectorAll(".content strong").forEach(function (s) {
      if (s.dataset.cc) return;
      var text = s.textContent.trim();
      if (!CODE.test(text)) return;
      var prev = s.previousSibling;
      var before = prev && prev.nodeType === 3 ? prev.textContent : "";
      if (!/code:\s*$/i.test(before)) return;
      s.dataset.cc = "1";
      s.classList.add("is-code");
      var b = document.createElement("button");
      b.type = "button";
      b.className = "code-copy";
      b.textContent = "Copy";
      b.onclick = function () { copyText(text, b); };
      s.after(b);
    });
  }

  new MutationObserver(scan).observe(box, { childList: true, subtree: true });
  scan();
})();
</script>
"""


# ===============================================================
# PAGES
# ===============================================================

@app.get("/")
def home_page():

    path = os.path.join(BASE_DIR, "home.html")

    if not os.path.exists(path):
        return redirect("/chat")

    with open(path, encoding="utf-8") as fh:
        page = _rebrand(fh.read())

    return Response(
        page,
        mimetype="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/index.html")
def chat_page_alias():
    return redirect("/chat")


@app.get("/chat")
def chat_page():

    path = os.path.join(BASE_DIR, "index.html")

    if not os.path.exists(path):
        return "index.html is missing next to app.py.", 404

    with open(path, encoding="utf-8") as fh:
        page = _rebrand(fh.read())

    if "</body>" in page:
        page = page.replace("</body>", CHAT_EXTRAS + "</body>", 1)
    else:
        page += CHAT_EXTRAS

    return Response(
        page,
        mimetype="text/html",
        headers={"Cache-Control": "no-cache"},
    )


# ===============================================================
# ACCOUNT: SIGN UP
# ===============================================================

@app.post("/api/auth/signup")
def auth_signup():
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()[:80]
    identifier = _normalise_identifier(data.get("identifier", ""))
    password = str(data.get("password", ""))

    if len(name) < 2:
        return jsonify(error="Please enter your name."), 400

    if not _valid_identifier(identifier):
        return jsonify(error="Enter a valid email address."), 400

    if len(password) < 8:
        return jsonify(error="Password must be at least 8 characters."), 400

    if len(password) > 128:
        return jsonify(error="Password is too long."), 400

    if too_fast("auth:" + client_ip(), 12):
        return jsonify(error="Too many attempts. Try again in a minute."), 429

    password_hash = generate_password_hash(password)
    code = _new_code()
    code_hash = generate_password_hash(code)
    created_at = datetime.now(timezone.utc).isoformat()
    expires_at = _expiry_text()
    created_new = False

    conn = _db()
    try:
        existing = conn.execute(
            "SELECT id, verified FROM users WHERE identifier = ?", (identifier,)
        ).fetchone()

        if existing and int(existing["verified"] or 0) == 1:
            return jsonify(error="An account with that email address already exists. Try logging in."), 409

        if existing:
            # An unverified signup for this email is retried, not blocked.
            conn.execute(
                """UPDATE users SET name = ?, password_hash = ?, verification_code_hash = ?,
                   verification_expires_at = ?, verification_attempts = 0 WHERE id = ?""",
                (name, password_hash, code_hash, expires_at, int(existing["id"])),
            )
            user_id = int(existing["id"])
        else:
            cur = conn.execute(
                """INSERT INTO users
                   (name, identifier, password_hash, created_at, verified,
                    verification_code_hash, verification_expires_at, verification_attempts)
                   VALUES (?, ?, ?, ?, 0, ?, ?, 0)""",
                (name, identifier, password_hash, created_at, code_hash, expires_at),
            )
            user_id = cur.lastrowid
            created_new = True

        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="An account with that email address already exists. Try logging in."), 409
    finally:
        conn.close()

    try:
        _send_code_email(identifier, name, code, "verify")
    except Exception as exc:
        if created_new:
            conn = _db()
            try:
                conn.execute("DELETE FROM users WHERE id = ? AND verified = 0", (int(user_id),))
                conn.commit()
            finally:
                conn.close()
        print(f"Verification email error: {exc}")
        return jsonify(error="I couldn't send the verification email. Please try again."), 502

    session.clear()
    return jsonify(
        verification_required=True,
        email=identifier,
        message="Check your email for your 6-digit verification code.",
    ), 201


# ===============================================================
# ACCOUNT: VERIFY EMAIL
# ===============================================================

@app.post("/api/auth/verify")
def auth_verify():
    data = request.get_json(silent=True) or {}
    identifier = _normalise_identifier(data.get("identifier", ""))
    code = re.sub(r"\D", "", str(data.get("code", "")))[:6]

    if not _valid_identifier(identifier) or len(code) != 6:
        return jsonify(error="Enter the 6-digit verification code."), 400

    if too_fast("verify:" + client_ip(), 10):
        return jsonify(error="Too many verification attempts. Try again in a minute."), 429

    conn = _db()
    try:
        row = conn.execute("SELECT * FROM users WHERE identifier = ?", (identifier,)).fetchone()

        if row is None:
            return jsonify(error="That account could not be found."), 404

        if int(row["verified"] or 0) == 1:
            return jsonify(error="This email is already verified. Log in instead."), 400

        if int(row["verification_attempts"] or 0) >= 5:
            return jsonify(error="Too many incorrect codes. Request a new code."), 429

        if _is_expired(row["verification_expires_at"]):
            return jsonify(error="That verification code has expired. Request a new code."), 400

        if not check_password_hash(row["verification_code_hash"] or "", code):
            conn.execute(
                "UPDATE users SET verification_attempts = verification_attempts + 1 WHERE id = ?",
                (int(row["id"]),),
            )
            conn.commit()
            return jsonify(error="Incorrect verification code."), 400

        conn.execute(
            """UPDATE users SET verified = 1, verification_code_hash = NULL,
               verification_expires_at = NULL, verification_attempts = 0 WHERE id = ?""",
            (int(row["id"]),),
        )
        conn.commit()

        row = conn.execute("SELECT * FROM users WHERE id = ?", (int(row["id"]),)).fetchone()
    finally:
        conn.close()

    _set_session(row)
    return jsonify(user=_public_user(row), verified=True)


@app.post("/api/auth/resend")
def auth_resend():
    data = request.get_json(silent=True) or {}
    identifier = _normalise_identifier(data.get("identifier", ""))

    if not _valid_identifier(identifier):
        return jsonify(error="Enter a valid email address."), 400

    if too_fast("resend:" + client_ip(), 3):
        return jsonify(error="Too many resend requests. Try again in a minute."), 429

    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, name, identifier, verified FROM users WHERE identifier = ?",
            (identifier,),
        ).fetchone()
        if row is None:
            return jsonify(error="That account could not be found."), 404
        if int(row["verified"] or 0) == 1:
            return jsonify(error="This email is already verified."), 400

        code = _new_code()
        conn.execute(
            """UPDATE users SET verification_code_hash = ?, verification_expires_at = ?,
               verification_attempts = 0 WHERE id = ?""",
            (generate_password_hash(code), _expiry_text(), int(row["id"])),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        _send_code_email(identifier, row["name"], code, "verify")
    except Exception as exc:
        print(f"Verification resend error: {exc}")
        return jsonify(error="I couldn't resend the verification email. Please try again."), 502

    return jsonify(ok=True, message="A new verification code has been sent.")


# ===============================================================
# ACCOUNT: CHANGE EMAIL (WRONG EMAIL TYPED AT SIGN UP)
# ===============================================================
# Only works while the account is still unverified, and needs the
# account password so nobody else can redirect the code.

@app.post("/api/auth/change_email")
def auth_change_email():
    data = request.get_json(silent=True) or {}

    old = _normalise_identifier(data.get("identifier", ""))
    new = _normalise_identifier(data.get("new_identifier", ""))
    password = str(data.get("password", ""))

    if not _valid_identifier(old) or not password:
        return jsonify(error="Enter your password to change the email."), 400

    if not _valid_identifier(new):
        return jsonify(error="Enter a valid email address."), 400

    if too_fast("chmail:" + client_ip(), 5):
        return jsonify(error="Too many attempts. Try again in a minute."), 429

    conn = _db()
    try:
        row = conn.execute("SELECT * FROM users WHERE identifier = ?", (old,)).fetchone()

        if row is None or not check_password_hash(row["password_hash"], password):
            return jsonify(error="Incorrect password."), 401

        if int(row["verified"] or 0) == 1:
            return jsonify(error="This account is already verified."), 400

        if new == old:
            return jsonify(error="That is the same email address."), 400

        if conn.execute("SELECT 1 FROM users WHERE identifier = ?", (new,)).fetchone():
            return jsonify(error="An account with that email address already exists."), 409

        code = _new_code()
        conn.execute(
            """UPDATE users SET identifier = ?, verification_code_hash = ?,
               verification_expires_at = ?, verification_attempts = 0 WHERE id = ?""",
            (new, generate_password_hash(code), _expiry_text(), int(row["id"])),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="An account with that email address already exists."), 409
    finally:
        conn.close()

    try:
        _send_code_email(new, row["name"], code, "verify")
    except Exception as exc:
        # Put the old email back so the person is not locked out.
        conn = _db()
        try:
            conn.execute(
                "UPDATE users SET identifier = ? WHERE id = ? AND verified = 0",
                (old, int(row["id"])),
            )
            conn.commit()
        finally:
            conn.close()
        print(f"Change email error: {exc}")
        return jsonify(error="I couldn't send the code to that email. Check it and try again."), 502

    return jsonify(
        verification_required=True,
        email=new,
        message="We sent a new code to your corrected email.",
    )


# ===============================================================
# ACCOUNT: LOGIN / LOGOUT / ME
# ===============================================================

@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(silent=True) or {}

    identifier = _normalise_identifier(data.get("identifier", ""))
    password = str(data.get("password", ""))

    if not _valid_identifier(identifier) or not password:
        return jsonify(error="Enter your email address and password."), 400

    if too_fast("auth:" + client_ip(), 12):
        return jsonify(error="Too many attempts. Try again in a minute."), 429

    conn = _db()
    try:
        row = conn.execute("SELECT * FROM users WHERE identifier = ?", (identifier,)).fetchone()
    finally:
        conn.close()

    if row is None or not check_password_hash(row["password_hash"], password):
        return jsonify(error="Incorrect email address or password."), 401

    if int(row["verified"] or 0) != 1:
        return jsonify(
            error="Please verify your email before logging in.",
            verification_required=True,
            email=identifier,
        ), 403

    _set_session(row)
    return jsonify(user=_public_user(row))


@app.get("/api/auth/me")
def auth_me():
    user = current_user()
    if user is None:
        session.clear()
        return jsonify(user=None)

    return jsonify(user=_public_user(user))


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify(ok=True)


# ===============================================================
# ACCOUNT: FORGOT / RESET PASSWORD
# ===============================================================

GENERIC_FORGOT = "If an account exists for that email, a reset code is on its way."


@app.post("/api/auth/forgot")
def auth_forgot():
    data = request.get_json(silent=True) or {}
    identifier = _normalise_identifier(data.get("identifier", ""))

    if not _valid_identifier(identifier):
        return jsonify(error="Enter a valid email address."), 400

    if too_fast("forgot:" + client_ip(), 5):
        return jsonify(error="Too many requests. Try again in a minute."), 429

    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, name, identifier, verified FROM users WHERE identifier = ?",
            (identifier,),
        ).fetchone()

        if row is not None and int(row["verified"] or 0) == 1:
            code = _new_code()
            conn.execute(
                """UPDATE users SET reset_code_hash = ?, reset_expires_at = ?,
                   reset_attempts = 0 WHERE id = ?""",
                (generate_password_hash(code), _expiry_text(), int(row["id"])),
            )
            conn.commit()
        else:
            row = None
    finally:
        conn.close()

    if row is not None:
        try:
            _send_code_email(identifier, row["name"], code, "reset")
        except Exception as exc:
            print(f"Reset email error: {exc}")

    # Same answer whether or not the account exists.
    return jsonify(ok=True, message=GENERIC_FORGOT)


@app.post("/api/auth/reset")
def auth_reset():
    data = request.get_json(silent=True) or {}

    identifier = _normalise_identifier(data.get("identifier", ""))
    code = re.sub(r"\D", "", str(data.get("code", "")))[:6]
    password = str(data.get("password", ""))

    if not _valid_identifier(identifier) or len(code) != 6:
        return jsonify(error="Enter the 6-digit reset code."), 400

    if len(password) < 8:
        return jsonify(error="Password must be at least 8 characters."), 400

    if len(password) > 128:
        return jsonify(error="Password is too long."), 400

    if too_fast("reset:" + client_ip(), 10):
        return jsonify(error="Too many attempts. Try again in a minute."), 429

    invalid = jsonify(error="That code is invalid or has expired. Request a new one."), 400

    conn = _db()
    try:
        row = conn.execute("SELECT * FROM users WHERE identifier = ?", (identifier,)).fetchone()

        if row is None or not row["reset_code_hash"]:
            return invalid

        if int(row["reset_attempts"] or 0) >= 5:
            return jsonify(error="Too many incorrect codes. Request a new one."), 429

        if _is_expired(row["reset_expires_at"]):
            return invalid

        if not check_password_hash(row["reset_code_hash"], code):
            conn.execute(
                "UPDATE users SET reset_attempts = reset_attempts + 1 WHERE id = ?",
                (int(row["id"]),),
            )
            conn.commit()
            return jsonify(error="Incorrect code."), 400

        conn.execute(
            """UPDATE users SET password_hash = ?, reset_code_hash = NULL,
               reset_expires_at = NULL, reset_attempts = 0,
               session_version = session_version + 1 WHERE id = ?""",
            (generate_password_hash(password), int(row["id"])),
        )
        conn.commit()
    finally:
        conn.close()

    session.clear()
    return jsonify(ok=True, message="Password updated. Log in with your new password.")


# ===============================================================
# ACCOUNT: CHANGE PASSWORD (SETTINGS)
# ===============================================================

@app.post("/api/account/password")
@login_required
def account_password():
    data = request.get_json(silent=True) or {}

    current = str(data.get("current", ""))
    new = str(data.get("new", ""))
    user = g.user

    if too_fast("pw:" + str(user["id"]), 6):
        return jsonify(error="Too many attempts. Try again in a minute."), 429

    if not check_password_hash(user["password_hash"], current):
        return jsonify(error="Your current password is not correct."), 400

    if len(new) < 8:
        return jsonify(error="New password must be at least 8 characters."), 400

    if len(new) > 128:
        return jsonify(error="Password is too long."), 400

    if new == current:
        return jsonify(error="Choose a password different from your current one."), 400

    new_version = int(user["session_version"] or 0) + 1

    conn = _db()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ?, session_version = ? WHERE id = ?",
            (generate_password_hash(new), new_version, int(user["id"])),
        )
        conn.commit()
    finally:
        conn.close()

    # Keep this device logged in. Other devices are signed out.
    session["sv"] = new_version

    return jsonify(ok=True, message="Password updated.")


# ===============================================================
# PREMIUM: PAYSTACK
# ===============================================================

def _paystack(method, path, payload=None):
    req = Request(
        "https://api.paystack.co" + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + PAYSTACK_SECRET_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Paystack error {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError("Could not reach Paystack.") from exc


def _finalize_payment(reference):
    """Verify a payment with Paystack and switch Premium on once.

    Returns 'success', 'pending', 'failed' or None (unknown reference).
    Safe to call many times: Premium is only added the first time.
    """
    conn = _db()
    try:
        pay = conn.execute("SELECT * FROM payments WHERE reference = ?", (reference,)).fetchone()
    finally:
        conn.close()

    if pay is None:
        return None

    if pay["status"] == "success":
        return "success"

    res = _paystack("GET", "/transaction/verify/" + quote(reference, safe=""))
    data = res.get("data") or {}
    status = str(data.get("status", "")).lower()

    if status != "success":
        return "failed" if status in ("failed", "abandoned", "reversed") else "pending"

    # Never trust the browser: the amount and currency must match what we asked for.
    if int(data.get("amount") or 0) != int(pay["amount_kobo"]) or str(data.get("currency", "")).upper() != "NGN":
        print(f"Payment {reference} amount/currency mismatch")
        return "failed"

    days = PLANS[pay["plan"]]["days"]

    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE payments SET status = 'success', paid_at = ? WHERE reference = ? AND status = 'pending'",
            (datetime.now(timezone.utc).isoformat(), reference),
        )

        if cur.rowcount == 1:
            user = conn.execute(
                "SELECT premium_until FROM users WHERE id = ?", (int(pay["user_id"]),)
            ).fetchone()
            base = max(time.time(), float(user["premium_until"] or 0)) if user else time.time()
            conn.execute(
                "UPDATE users SET premium_until = ?, premium_plan = ? WHERE id = ?",
                (base + days * 86400, pay["plan"], int(pay["user_id"])),
            )

        conn.commit()
    finally:
        conn.close()

    return "success"


@app.post("/api/premium/checkout")
@login_required
def premium_checkout():
    user = g.user
    data = request.get_json(silent=True) or {}

    plan_id = str(data.get("plan", "")).lower()
    plan = PLANS.get(plan_id)

    if plan is None:
        return jsonify(error="Choose monthly or yearly."), 400

    if user["identifier"] in ADMIN_EMAILS:
        return jsonify(error="Admin accounts already have free Premium for life."), 400

    if not PAYSTACK_SECRET_KEY:
        return jsonify(error="Payments are not set up yet. Please try again later."), 503

    if too_fast("checkout:" + str(user["id"]), 10):
        return jsonify(error="Too many attempts. Try again in a minute."), 429

    reference = "st_" + secrets.token_hex(12)
    amount_kobo = plan["amount"] * 100
    base = APP_BASE_URL or request.url_root.rstrip("/")

    try:
        res = _paystack(
            "POST",
            "/transaction/initialize",
            {
                "email": user["identifier"],
                "amount": amount_kobo,
                "currency": "NGN",
                "reference": reference,
                "callback_url": base + "/chat",
                "metadata": {"user_id": int(user["id"]), "plan": plan_id},
            },
        )
    except Exception as exc:
        print(f"Checkout error: {exc}")
        return jsonify(error="I couldn't open checkout. Please try again."), 502

    url = (res.get("data") or {}).get("authorization_url")

    if not url:
        return jsonify(error="I couldn't open checkout. Please try again."), 502

    conn = _db()
    try:
        conn.execute(
            """INSERT INTO payments (reference, user_id, plan, amount_kobo, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (reference, int(user["id"]), plan_id, amount_kobo, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify(authorization_url=url, reference=reference)


@app.post("/api/premium/verify")
@login_required
def premium_verify():
    user = g.user
    data = request.get_json(silent=True) or {}
    reference = str(data.get("reference", "")).strip()[:80]

    if not re.fullmatch(r"[A-Za-z0-9_\-]+", reference):
        return jsonify(error="Missing payment reference."), 400

    conn = _db()
    try:
        pay = conn.execute(
            "SELECT user_id FROM payments WHERE reference = ?", (reference,)
        ).fetchone()
    finally:
        conn.close()

    if pay is None or int(pay["user_id"]) != int(user["id"]):
        return jsonify(error="That payment was not found on your account."), 404

    try:
        status = _finalize_payment(reference)
    except Exception as exc:
        print(f"Verify payment error: {exc}")
        return jsonify(error="I couldn't confirm the payment yet. Try again in a moment."), 502

    conn = _db()
    try:
        fresh = conn.execute("SELECT * FROM users WHERE id = ?", (int(user["id"]),)).fetchone()
    finally:
        conn.close()

    return jsonify(status=status, user=_public_user(fresh))


@app.post("/api/premium/webhook")
def premium_webhook():
    """Paystack calls this when a payment succeeds, even if the person closed the page."""
    if not PAYSTACK_SECRET_KEY:
        return "", 503

    raw = request.get_data()
    signature = request.headers.get("X-Paystack-Signature", "")
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode("utf-8"), raw, hashlib.sha512).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return "", 401

    try:
        event = json.loads(raw.decode("utf-8"))
    except ValueError:
        return "", 400

    if event.get("event") == "charge.success":
        reference = str((event.get("data") or {}).get("reference", ""))
        try:
            _finalize_payment(reference)
        except Exception as exc:
            print(f"Webhook error: {exc}")
            return "", 500

    return "", 200


# ===============================================================
# HEALTH CHECK
# ===============================================================

@app.get("/health")
def health():

    provider_loaded = getattr(bot, "SPORTYBET_PROVIDER", None) is not None

    return jsonify(
        status="ok",
        sportybet=provider_loaded,
        smart_ticket=SMART_TICKET_MODULE is not None,
    )


# ===============================================================
# STREAM RESPONSE
# ===============================================================

def _stream_response(q):

    def stream():

        while True:

            try:
                item = q.get(timeout=15)

            except queue.Empty:
                yield "\n"
                continue

            if item is None:
                break

            yield json.dumps(item) + "\n"

    return Response(
        stream(),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _start_chat_turn(sid, text):
    """Login check, Premium check, then run the turn."""

    user = current_user()

    if user is None:
        return jsonify(error="Please log in to continue.", login_required=True), 401

    if too_fast(client_ip()):
        return jsonify(error="Slow down. Try again in a minute."), 429

    # Free accounts can only ask for Daily 2 odds.
    if not _is_free_request(text) and not _is_premium(user):
        return jsonify(error=PREMIUM_MESSAGE, upgrade=True), 402

    q = queue.Queue()

    threading.Thread(
        target=run_turn,
        args=(sid, text, q),
        daemon=True,
    ).start()

    return _stream_response(q)


# ===============================================================
# CHAT API
# ===============================================================

@app.post("/api/chat")
def chat():

    data = request.get_json(silent=True) or {}

    text = str(data.get("message", "")).strip()[:500]
    sid = str(data.get("session", ""))[:64]

    if not text or not sid:
        return jsonify(error="Empty message."), 400

    return _start_chat_turn(sid, text)


# ===============================================================
# STRAIGHT WIN
# ===============================================================

@app.post("/api/straight_win")
def api_straight_win():

    data = request.get_json(silent=True) or {}

    window = str(data.get("window", "today")).lower()
    sid = str(data.get("session", ""))[:64]

    if window not in ("today", "long"):
        return jsonify(error="Unknown window."), 400

    if not sid:
        return jsonify(error="Missing session."), 400

    text = "straight win long ticket" if window == "long" else "straight win today"

    return _start_chat_turn(sid, text)


# ===============================================================
# SPORTYBET CODE TOOLS
# ===============================================================

CODE_OK = re.compile(r"^[A-Z0-9]{5,12}$")


def _ticket_tool(work, premium_only=False):

    user = current_user()

    if user is None:
        return jsonify(error="Please log in to continue.", login_required=True), 401

    g.user = user

    if premium_only and not _is_premium(user):
        return jsonify(error=PREMIUM_MESSAGE, upgrade=True), 402

    data = request.get_json(silent=True) or {}

    code = str(data.get("code", "")).strip().upper()

    if not CODE_OK.match(code):
        return jsonify(error="That does not look like a SportyBet code."), 400

    if too_fast("tool:" + client_ip(), TOOL_LIMIT):
        return jsonify(error="Slow down. Try again in a minute."), 429

    provider = getattr(bot, "SPORTYBET_PROVIDER", None)

    if provider is None:
        return jsonify(
            error="SportyBet mode is off. Set USE_SPORTYBET to 1 on the server."
        ), 503

    try:
        return jsonify(work(provider, code, data))

    except Exception as exc:

        if exc.__class__.__name__ == "SaferError":
            return jsonify(error=str(exc)), 422

        print(f"Ticket tool error: {type(exc).__name__}: {exc}")

        return jsonify(
            error="I could not read SportyBet right now. Please try again in a moment."
        ), 502


def _ints(value):

    return [
        int(x)
        for x in (value or [])
        if str(x).isdigit()
    ][:30]


@app.post("/api/check")
def api_check():

    return _ticket_tool(lambda p, code, data: p.check_code(code))


@app.post("/api/edit")
def api_edit():

    return _ticket_tool(
        lambda p, code, data: p.edit_code(
            code,
            remove=_ints(data.get("remove")),
            swap=_ints(data.get("swap")),
            picker=None,
        ),
        premium_only=True,
    )


# ===============================================================
# MAKE SAFER (Premium only)
# ===============================================================

@app.post("/api/safer")
def api_safer():

    def work(provider, code, data):

        if SMART_TICKET_MODULE is None:
            raise RuntimeError("smart_ticket.py is not loaded")

        return SMART_TICKET_MODULE.safer_rebuild(provider, code)

    return _ticket_tool(work, premium_only=True)


# ===============================================================
# WHY THIS PICK
# ===============================================================

@app.post("/api/why")
def api_why():

    def work(provider, code, data):

        legs = provider.load_code(code)

        try:
            index = int(data.get("index"))
            leg = legs[index]
        except (TypeError, ValueError, IndexError):
            raise RuntimeError("bad pick number")

        odd = leg.get("odd") or 0.0

        if odd > 1:
            chance = round(min(0.95 / odd, 0.97) * 100)
            reason = (
                f"SportyBet's odds of {odd:.2f} put this pick at about "
                f"{chance}%. Higher odds mean SportyBet thinks it is less "
                "likely to win."
            )
        else:
            reason = "SportyBet has no live odds for this pick right now."

        return {"reason": reason}

    return _ticket_tool(work, premium_only=True)


# ===============================================================
# START
# ===============================================================

if __name__ == "__main__":

    if getattr(bot, "SPORTYBET_PROVIDER", None) is None:
        print("WARNING: SportyBet provider is not active.")

    if SMART_TICKET_MODULE is None:
        print("WARNING: smart_ticket.py is not loaded.")

    if not PAYSTACK_SECRET_KEY:
        print("NOTE: PAYSTACK_SECRET_KEY is not set, so Premium checkout is off.")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        threaded=True,
    )