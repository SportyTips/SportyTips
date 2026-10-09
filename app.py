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
  SECRET_KEY=...                 (long random text, keeps people logged in after a redeploy)
  DATABASE_URL=postgresql://...  (Neon database. Keeps accounts and Premium forever)
  ADMIN_EMAILS=a@x.com,b@x.com   (optional extra admins; the built-in admins are in this file)
  BREVO_API_KEY / BREVO_SENDER_EMAIL / BREVO_SENDER_NAME   (emails)
  PAYSTACK_SECRET_KEY=sk_live_...                           (Premium payments)
  APP_BASE_URL=https://yourdomain.com                       (payment return URL)
  COOKIE_SECURE=1                (recommended on HTTPS)

Install as an app (PWA): /manifest.webmanifest, /sw.js and /icons/... are served
by this file. No extra files are needed.
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


def _add_football_note(text):
    """Under each finished ticket, say whether real football stats were used."""
    try:
        if upgrades is None or "Total odds" not in text or "Estimated combined chance" not in text:
            return text

        note = getattr(getattr(upgrades, "CTX", None), "football_note", None)
        if not note:
            return text

        studied = int(note[0] or 0)

        # Only good news is shown. When no football stats were found, nothing is added.
        if studied <= 0:
            return text

        line = (
            "\U0001F4C8 Football stats (recent form and head-to-head) were used for "
            f"{studied} match" + ("es" if studied != 1 else "") + "."
        )

        lines = text.split("\n")
        for index, value in enumerate(lines):
            if "Estimated combined chance" in value:
                lines.insert(index + 1, line)
                break

        return "\n".join(lines)
    except Exception:
        return text


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

    cleaned = _add_football_note(cleaned)

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

    print("NOTE: SECRET_KEY is not set. Everyone is logged out after each redeploy. Set it on Render.")

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
# DATABASE_URL set to a Neon (Postgres) address -> accounts live there and
# survive every redeploy.
# No DATABASE_URL -> the old local SQLite file is used (it is wiped on
# Render's free plan every time you redeploy).

DATABASE_URL = os.getenv("DATABASE_URL", "").strip().strip("\"'")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

USE_PG = DATABASE_URL.startswith("postgresql://")

if DATABASE_URL and not USE_PG:
    print("WARNING: DATABASE_URL is set but does not start with postgresql://. Using SQLite instead.")

if USE_PG:
    import psycopg2
    import psycopg2.extras

    DB_INTEGRITY_ERROR = psycopg2.IntegrityError
    print("OK: using Postgres (Neon) for accounts")
else:
    DB_INTEGRITY_ERROR = sqlite3.IntegrityError
    print("NOTE: using local SQLite for accounts (set DATABASE_URL to keep accounts after redeploys)")


class _PgDb:
    """Small wrapper so the rest of the code works the same on Postgres."""

    def __init__(self):
        self.raw = psycopg2.connect(DATABASE_URL, connect_timeout=15)

    def execute(self, sql, params=()):
        cur = self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), tuple(params))
        return cur

    def commit(self):
        self.raw.commit()

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass


def _db():
    if USE_PG:
        return _PgDb()

    conn = sqlite3.connect(ACCOUNT_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_returning_id(conn, sql, params):
    if USE_PG:
        return conn.execute(sql + " RETURNING id", params).fetchone()["id"]

    return conn.execute(sql, params).lastrowid


def _init_account_db():
    if USE_PG:
        _init_postgres()
    else:
        _init_sqlite()


def _init_postgres():
    conn = _db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                identifier TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_code_hash TEXT,
                verification_expires_at TEXT,
                verification_attempts INTEGER NOT NULL DEFAULT 0,
                reset_code_hash TEXT,
                reset_expires_at TEXT,
                reset_attempts INTEGER NOT NULL DEFAULT 0,
                session_version INTEGER NOT NULL DEFAULT 0,
                premium_until DOUBLE PRECISION NOT NULL DEFAULT 0,
                premium_plan TEXT
            )
            """
        )
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


def _init_sqlite():
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

def _release_memory():
    """Give memory back after a big ticket so Render's 512 MB limit is not hit."""
    try:
        import gc

        gc.collect()

        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


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
        _release_memory()


# ===============================================================
# INSTALL AS AN APP (PWA)
# ===============================================================

# Icons are built into this file (red S on navy), so there is nothing to upload.
_ICON_B64 = {
    "icon-192.png": "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",
    "icon-512.png": "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",
    "icon-maskable-512.png": "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",
    "apple-touch-icon.png": "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",
}

_ICON_BYTES = {name: base64.b64decode(data) for name, data in _ICON_B64.items()}

PWA_HEAD = (
    '<link rel="manifest" href="/manifest.webmanifest">\n'
    '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">\n'
    '<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-title" content="SportyTips">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black">\n'
)


def _add_pwa_head(page):
    if "rel=\"manifest\"" in page:
        return page

    if "</head>" in page:
        return page.replace("</head>", PWA_HEAD + "</head>", 1)

    return PWA_HEAD + page


SERVICE_WORKER = r"""
/* SportyTips service worker.
   It only helps the browser treat the site as an installable app and shows a
   small offline page when there is no internet. It never touches /api/ requests,
   login or payments, so nothing can get stuck. */
const CACHE = "sportytips-shell-v1";

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function (cache) { return cache.add("/offline"); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== CACHE; })
          .map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET" || req.mode !== "navigate") return;
  event.respondWith(
    fetch(req).catch(function () { return caches.match("/offline"); })
  );
});
"""

OFFLINE_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SportyTips offline</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#04111f;color:#fff;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;text-align:center;padding:24px}
.logo{width:64px;height:64px;border-radius:18px;background:#ff1f1f;display:grid;place-items:center;
font-size:34px;font-weight:800;margin:0 auto 16px}
h1{font-size:24px;margin:0 0 8px}p{color:#c4ccd8;margin:0 0 20px}
button{border:0;background:#ff1f1f;color:#fff;font-weight:800;font-size:17px;padding:14px 28px;border-radius:999px}
</style></head><body><div>
<div class="logo">S</div>
<h1>You are offline</h1>
<p>Check your internet connection, then try again.</p>
<button onclick="location.href='/chat'">Try again</button>
</div></body></html>"""


@app.get("/manifest.webmanifest")
def pwa_manifest():
    data = {
        "name": "SportyTips",
        "short_name": "SportyTips",
        "description": "Smarter slips, built in seconds. Build SportyBet tickets and codes.",
        "id": "/chat",
        "start_url": "/chat",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#04111f",
        "theme_color": "#04111f",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }

    return Response(
        json.dumps(data),
        mimetype="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/icons/<name>")
def pwa_icon(name):
    data = _ICON_BYTES.get(name)

    if data is None:
        abort(404)

    return Response(
        data,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/sw.js")
def pwa_service_worker():
    return Response(
        SERVICE_WORKER,
        mimetype="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/offline")
def pwa_offline():
    return Response(
        OFFLINE_PAGE,
        mimetype="text/html",
        headers={"Cache-Control": "no-cache"},
    )


# ===============================================================
# CHAT EXTRAS
# ===============================================================

CHAT_EXTRAS = r"""
<style>
  .code-copy { margin-left: 10px; border: 0; background: #ff1f1f; color: #fff; font: inherit; font-size: 14px; font-weight: 700; padding: 6px 15px; border-radius: 999px; cursor: pointer; vertical-align: middle; }
  .code-copy:active { transform: scale(.96); }
  .code-copy.done { background: #16a765; }
  .content strong.is-code { font-size: 20px; letter-spacing: .04em; }

  .install-bar { position: fixed; left: 12px; right: 12px; bottom: calc(84px + env(safe-area-inset-bottom)); z-index: 60; max-width: 560px; margin: 0 auto; background: #04111f; color: #fff; border: 1px solid rgba(255,255,255,.18); border-radius: 20px; padding: 12px 12px 12px 14px; display: flex; align-items: center; gap: 12px; box-shadow: 0 14px 40px rgba(0,0,0,.45); font-family: inherit; }
  .install-bar[hidden] { display: none; }
  .install-bar .ib-logo { width: 38px; height: 38px; border-radius: 11px; background: #ff1f1f; display: grid; place-items: center; font-weight: 800; font-size: 21px; flex: none; }
  .install-bar .ib-text { flex: 1; min-width: 0; font-size: 14px; line-height: 1.3; }
  .install-bar .ib-text b { display: block; font-size: 15px; }
  .install-bar .ib-text span { color: #c4ccd8; }
  .install-bar .ib-go { border: 0; background: #ff1f1f; color: #fff; font-weight: 800; font-size: 14px; padding: 10px 16px; border-radius: 999px; cursor: pointer; flex: none; }
  .install-bar .ib-x { border: 0; background: none; color: #aab4c3; font-size: 22px; cursor: pointer; padding: 4px 6px; flex: none; }
</style>

<div class="install-bar" id="installbar" hidden>
  <div class="ib-logo">S</div>
  <div class="ib-text"><b>Install SportyTips</b><span id="ibtext">Add the app to your home screen.</span></div>
  <button class="ib-go" id="ibgo" type="button">Install</button>
  <button class="ib-x" id="ibx" type="button" aria-label="Close">&times;</button>
</div>

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

/* ---------- Install as an app (PWA) ---------- */
(function () {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }

  var standalone = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  if (standalone) return;

  var KEY = "st_install_hidden";
  try { if (localStorage.getItem(KEY) === "1") return; } catch (e) {}

  var bar = document.getElementById("installbar");
  var go = document.getElementById("ibgo");
  var text = document.getElementById("ibtext");
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  var deferred = null;
  var shown = false;

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
  });

  window.addEventListener("appinstalled", function () {
    bar.hidden = true;
    shown = true;
  });

  document.getElementById("ibx").onclick = function () {
    bar.hidden = true;
    try { localStorage.setItem(KEY, "1"); } catch (e) {}
  };

  go.onclick = function () {
    if (deferred) {
      deferred.prompt();
      deferred.userChoice.then(function () { deferred = null; bar.hidden = true; });
    } else {
      bar.hidden = true;
    }
  };

  function screensClear() {
    var home = document.getElementById("home");
    var auth = document.getElementById("auth");
    return (!home || home.hidden) && (!auth || auth.hidden);
  }

  var tries = 0;
  var timer = setInterval(function () {
    tries++;
    if (shown || tries > 40) { clearInterval(timer); return; }
    if (!screensClear()) return;

    if (ios) {
      text.textContent = "Tap Share, then Add to Home Screen.";
      go.hidden = true;
      bar.hidden = false;
      shown = true;
    } else if (deferred) {
      bar.hidden = false;
      shown = true;
    }
  }, 2500);
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
        page = _add_pwa_head(_rebrand(fh.read()))

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

    # Top leagues card: Norway (Eliteserien) is now included, so 14 competitions
    # and the Norway flag after Greece.
    page = page.replace("13 top competitions", "14 top competitions")
    page = page.replace(
        "&#x1F1EC;&#x1F1F7; &#x1F3C6;",
        "&#x1F1EC;&#x1F1F7; &#x1F1F3;&#x1F1F4; &#x1F3C6;",
        1,
    )

    page = _add_pwa_head(page)

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
            user_id = _insert_returning_id(
                conn,
                """INSERT INTO users
                   (name, identifier, password_hash, created_at, verified,
                    verification_code_hash, verification_expires_at, verification_attempts)
                   VALUES (?, ?, ?, ?, 0, ?, ?, 0)""",
                (name, identifier, password_hash, created_at, code_hash, expires_at),
            )
            created_new = True

        conn.commit()
    except DB_INTEGRITY_ERROR:
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
    except DB_INTEGRITY_ERROR:
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
            "User-Agent": "SportyTips/1.0",
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

    try:
        conn = _db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        database_ok = True
    except Exception as exc:
        print(f"Health: database check failed: {exc}")
        database_ok = False

    return jsonify(
        status="ok",
        sportybet=provider_loaded,
        smart_ticket=SMART_TICKET_MODULE is not None,
        database="postgres" if USE_PG else "sqlite",
        database_ok=database_ok,
        secret_key_set=bool(os.getenv("SECRET_KEY")),
        football_data_key_set=bool(getattr(bot, "FOOTBALL_DATA_API_KEY", "")),
        paystack_key_set=bool(PAYSTACK_SECRET_KEY),
        paystack_mode=(
            "live" if PAYSTACK_SECRET_KEY.startswith("sk_live_")
            else "test" if PAYSTACK_SECRET_KEY.startswith("sk_test_")
            else "unknown" if PAYSTACK_SECRET_KEY else None
        ),
        app_base_url=APP_BASE_URL or None,
    )


@app.get("/health/football")
def health_football():
    """Shows whether the football-data.org connection really works."""

    if too_fast("fdhealth:" + client_ip(), 4):
        return jsonify(error="Slow down. Try again in a minute."), 429

    if not getattr(bot, "FOOTBALL_DATA_API_KEY", ""):
        return jsonify(
            football_data_key_set=False,
            ok=False,
            hint="Add FOOTBALL_DATA_API_KEY on Render (Environment), then redeploy.",
        )

    result = {"football_data_key_set": True, "ok": False}

    try:
        today = datetime.now(timezone.utc).date().isoformat()
        fixtures = bot.get_fixtures_for_date(today)
        names = sorted({str((f.get("competition") or {}).get("name", "?")) for f in fixtures})
        result.update(ok=True, matches_today=len(fixtures), competitions_today=names[:30])
    except Exception as exc:
        result["error"] = str(exc)[:200]
        return jsonify(result)

    try:
        listing = bot.football_request("competitions")
        comps = listing.get("competitions", []) if isinstance(listing, dict) else []
        result["competitions_on_your_plan"] = len(comps)
        result["plan_competitions"] = sorted(str(c.get("name", "?")) for c in comps)[:60]
    except Exception as exc:
        result["competitions_error"] = str(exc)[:200]

    return jsonify(result)


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

    if PAYSTACK_SECRET_KEY and not APP_BASE_URL:
        print("NOTE: APP_BASE_URL is not set. Paystack will return people to the address the request came from.")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        threaded=True,
    )
