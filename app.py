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


def _corners_title(text):
    """Corners tickets say CORNERS in their title line."""
    try:
        if upgrades is None or not getattr(getattr(upgrades, "CTX", None), "corners", False):
            return text
        return text.replace("SPORTYTIPS \u2022 ", "SPORTYTIPS \u2022 CORNERS \u2022 ", 1)
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
    cleaned = _corners_title(cleaned)

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
    "Upgrade to Premium to build any other ticket and check or edit codes."
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
    "apple-touch-icon.png": "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"
}


def _decode_icons():
    """Decode the built-in icons. A bad icon is skipped, it never stops the site from starting."""
    out = {}
    for name, data in _ICON_B64.items():
        try:
            out[name] = base64.b64decode(data)
        except Exception as exc:
            print(f"NOTE: icon {name} could not be decoded and was skipped: {exc}")
    return out


_ICON_BYTES = _decode_icons()

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

    # Top leagues card: 14 leagues (the European cups have their own card). Norway, Saudi Arabia and Denmark flags are
    # added after Greece (England already has its flag for the Championship).
    page = page.replace("13 top competitions", "14 top competitions")
    page = page.replace(
        "&#x1F1EC;&#x1F1F7; &#x1F3C6;",
        "&#x1F1EC;&#x1F1F7; &#x1F1F3;&#x1F1F4; &#x1F1F8;&#x1F1E6; &#x1F1E9;&#x1F1F0;",
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


_reconcile_seen = {}


def _reconcile_pending(user):
    try:
        if _premium_info(user)["active"]:
            return user

        user_id = int(user["id"])
        now = time.time()

        if now - _reconcile_seen.get(user_id, 0) < 90:
            return user

        _reconcile_seen[user_id] = now

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        conn = _db()
        try:
            rows = conn.execute(
                """SELECT reference FROM payments
                   WHERE user_id = ? AND status = 'pending' AND created_at > ?
                   ORDER BY created_at DESC LIMIT 5""",
                (user_id, cutoff),
            ).fetchall()
        finally:
            conn.close()

        changed = False

        for row in rows:
            try:
                if _finalize_payment(row["reference"]) == "success":
                    changed = True
            except Exception as exc:
                print(f"Reconcile error: {exc}")

        if changed:
            conn = _db()
            try:
                fresh = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            finally:
                conn.close()

            if fresh is not None:
                return fresh

    except Exception as exc:
        print(f"Reconcile failed: {exc}")

    return user


@app.get("/api/auth/me")
def auth_me():
    user = current_user()
    if user is None:
        session.clear()
        return jsonify(user=None)

    user = _reconcile_pending(user)

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


def _recover_payment(reference, expect_user_id=None):
    """A paid Paystack payment that has no row in our payments table (for example the
    row could not be saved when checkout opened). The payment is checked with Paystack
    and Premium is switched on from the details we sent when checkout opened."""
    if not str(reference).startswith("st_"):
        return None

    res = _paystack("GET", "/transaction/verify/" + quote(reference, safe=""))
    data = res.get("data") or {}
    status = str(data.get("status", "")).lower()

    if status != "success":
        return "failed" if status in ("failed", "abandoned", "reversed") else "pending"

    meta = data.get("metadata") or {}
    plan = PLANS.get(str(meta.get("plan", "")).lower())

    try:
        user_id = int(meta.get("user_id"))
    except (TypeError, ValueError):
        return None

    if plan is None:
        return None

    if expect_user_id is not None and int(expect_user_id) != user_id:
        return None

    amount_kobo = plan["amount"] * 100

    if int(data.get("amount") or 0) != amount_kobo or str(data.get("currency", "")).upper() != "NGN":
        print(f"Recovered payment {reference} amount/currency mismatch")
        return "failed"

    conn = _db()
    try:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()

        if user is None:
            return None

        try:
            conn.execute(
                """INSERT INTO payments (reference, user_id, plan, amount_kobo, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (
                    reference,
                    user_id,
                    str(meta.get("plan", "")).lower(),
                    amount_kobo,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except DB_INTEGRITY_ERROR:
            pass  # another request saved it a moment ago
    finally:
        conn.close()

    print(f"Recovered missing payment row for {reference}")
    return _finalize_payment(reference, _retry=False)


def _finalize_payment(reference, expect_user_id=None, _retry=True):
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
        if not _retry:
            return None

        return _recover_payment(reference, expect_user_id)

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

    # 1) Save the pending payment first.
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO payments (reference, user_id, plan, amount_kobo, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (reference, int(user["id"]), plan_id, amount_kobo, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception as exc:
        print(f"Checkout could not save the payment row: {exc}")
        return jsonify(error="I couldn't open checkout. Please try again."), 502
    finally:
        conn.close()

    # 2) Then open Paystack.
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

        url = (res.get("data") or {}).get("authorization_url")

        if not url:
            raise RuntimeError("Paystack returned no checkout link.")

    except Exception as exc:
        print(f"Checkout error: {exc}")

        conn = _db()
        try:
            conn.execute("DELETE FROM payments WHERE reference = ? AND status = 'pending'", (reference,))
            conn.commit()
        finally:
            conn.close()

        return jsonify(error="I couldn't open checkout. Please try again."), 502

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

    if pay is not None and int(pay["user_id"]) != int(user["id"]):
        return jsonify(error="That payment was not found on your account."), 404

    try:
        status = _finalize_payment(reference, expect_user_id=int(user["id"]))
    except Exception as exc:
        print(f"Verify payment error: {exc}")
        return jsonify(error="I couldn't confirm the payment yet. Try again in a moment."), 502

    if status is None:
        return jsonify(error="That payment was not found on your account."), 404

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
            status = _finalize_payment(reference)
        except Exception as exc:
            print(f"Webhook error: {exc}")
            return "", 500

        if status == "pending":
            return "", 500  # not confirmed yet: ask Paystack to send it again

    return "", 200


# ===============================================================
# HEALTH CHECK
# ===============================================================

@app.get("/health")
def health():

    # Quick check for Uptimer. It does NOT touch the database,
    # so Neon can sleep. Use /health?db=1 to test the database.
    if request.args.get("db") != "1":
        return jsonify(status="ok")

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
