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

# Free Premium for everyone until this time (Unix timestamp). 0 = off.
try:
    PROMO_PREMIUM_UNTIL = float(os.getenv("PROMO_PREMIUM_UNTIL", "0") or 0)
except ValueError:
    PROMO_PREMIUM_UNTIL = 0.0
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


try:
    _init_account_db()
except Exception as exc:
    print(f"WARNING: could not set up the account database at startup: {exc}")


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
    promo = PROMO_PREMIUM_UNTIL > time.time()
    active = admin or promo or until > time.time()

    info = {"active": active, "admin": admin, "plan": None, "expires_at": None}

    if admin:
        info["plan"] = "admin"
    elif active:
        end = max(until, PROMO_PREMIUM_UNTIL)
        info["plan"] = row["premium_plan"] or "promo"
        info["expires_at"] = datetime.fromtimestamp(end, timezone.utc).isoformat()

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
    "icon-512.png": "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAaR0lEQVR42u3deZDX9X3H8dceXKIIeCIUQcUgKktQsEtMKooRBcErpjEVknRMtelMM7WJHW1RmklsUm0m9WgSa7XR8Yw1JhCNxAtRAQWF9QqRQERFQI64XMIe/SNMjWmMF7t+f7/v4zE7zo6TjN99f7+/z/P3+Z019X32DwDlU2sEAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAIAgAAAIAAAVLl6Iyia7b3MgKrV5XUzEACs9bjmVUEAXP3gdqEHAmDRB95ykxEDAbDuQ9lvR0ogANZ9UALDEABLP5T4hiYDAmDdBxsCwxAASz/YECAAln6QAQTA0g8ygABY+kEGEABLP8gA8WmgVn9w+7UDwKUDtgIC4Iqx9IMMlICHgKz+4NYtALg+wG28TDwE5LKAMt7YPRxkB2D1B7d6AXAdAG77JVPv9AMlXwRK+3BQbclPPEBpV4Na5xugnGtCrTMNUM6VodY5Bijn+lDr7AKUc5WodV4ByrlW1DqjAOVcMWqdS4Byrhu1ziJAOVePWucPoJxrSK0zB1DOlaTWOQMo53ri00ABSqo6A+DuP2BVKWMArP6AtaWMAbD6A1aY8u4AAChdANz9B6wzZQyA1R+w2pQxAFZ/wJpT3h0AAKULgLv/gJXHDgCA0gTA3X/A+lPGAFj9AatQeXcAAJQuAO7+A9YiOwAAShMAd/8BK5IdAAClCYC7/4B1yQ4AgNIEwN1/wOpkBwBAaQLg7j9gjbIDAEAAAKjuAHj8B7BS2QEAUJoAuPsPWK/sAAAQAACqOwAe/wEqS0WsWnYAAHYAAAiAnRRA1a9ddgAAdgAACAAAAvAh8wQAULkKvoLZAQDYAQAgAAAIAAAC0Ok8AwxUuiKvY3YAAHYAAAgAAAIAgAAAIAAACEBH8RpQoDoUdjWzAwCwAwBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAN6deiOgsvXpk332yb77pl+/7Lln+vRJ797p2ze77ZZddtnx07176upSX5/a2tTVpbU1LS1pacn27W/5ZdOmbNyYjRvT3PyWX9aty5o1ee21rFmT5mYjRwCg09XVZdCgHHJIPvKRDB6cQYMyeHB69nzvV3196t/vlb9t244SvPZaVq3KihV56aUdP6+9lvZ2Z4kKUlPfZ/9iHtn2Xs4OyYABGTkyI0fmox/N0KHp1q24h/rGG3nllbz0Ul58MUuXZunSvPBCXnlFFUjS5XUBEADejb59c/TR+fjHc/TR6devsv+WLVuybNmOGCxdmuefz7JlaW11kgVAAASA37H//vnkJ3PCCRk5MrXV+/KErVvz/PN59tk880yeeSa/+EW2bHHyBUAABKCs9/cnTsxpp2XEiDL++W1tWbYszzyTp57KggV59tls3+6iEAABcM1Uu1GjMmVKTjzx/T8lW322bUtTU558MgsX5skns3KlkQiAAFBF6uoyaVK++MUccohhvIOVK7NgQebNy9y5+eUvzUMABICKVV+fM8/Muedm4EDDeM/Wrs3cuTt+xEAABIBKMmFCvvKVDBpkEjstBldckeefNwwBeN98FAQdb9iw3HlnrrrK6r/T7LFHJkzwGBofdE9uBHSgnj1z/vmZOjV1dYYBAkBpjBqVb387AwaYBAgA5bms6nP++fmrv6rm93OBAMDv23PPXH11Ro82CRAAyqShId//fvbZxySg+OzQ2XmOPz633GL1BwGgZM46K9/9bnr0MAkQAMpk6tR84xte6wkCQPlW/+nTjQEEgJKZNCmXXGIMIACUzJgxufzy1NSYBAgAZTJwYP7jP9Kli0mAAFAmPXrke9/L7rubBAgAJTN9uo+iBAGgfI4/PmeeaQwgAJRM37755jeNAQSA8rnoovTtawwgAJTM6NE57TRjAAGgZGpqcvHFXvUPAkD5TJyYQw81BhAASqauLuefbwwgAJTPhAkZNMgYQAAon3POMQMQAMrnqKNy+OHGAAJA+XzmM2YA1ceXwvNOevXK+PGVevBvvJHly7N8eVatyquvZtWqrF2bjRvT3Jzm5mzenJaWHT9tbenaNd26pVu3dO++45dddslee/3+z957Z++9XRcIACUwYUK6d6+Yo92yJYsX58kns3Bhnn8+L72UtrZ3+//dujVbt76r/2XPnjnggB0/Bx6YAw7IoEHZZRcXCwJAdTnppAo4yJUrc++9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",
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
