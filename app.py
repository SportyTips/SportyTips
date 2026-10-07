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
"""

import base64
import importlib
import json
import os
import queue
import re
import threading
import time
import sqlite3
import secrets
import html
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, redirect, request, session
from werkzeug.security import generate_password_hash, check_password_hash


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
#
# This prevents:
# "partially initialized module 'sportybet_provider'"
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
# smart_ticket.py replaces main.prediction_ticket_flow with the
# real SportyTips ticket-building logic.
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

# Account sessions use a signed, HTTP-only Flask cookie. Set SECRET_KEY in
# production if you want the same session key to survive server replacements.
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
        # Last-resort fallback for read-only deployments. A redeploy can
        # invalidate existing logins, but the account database is unaffected.
        return secrets.token_hex(32)


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
)

ACCOUNT_DB = os.getenv(
    "ACCOUNT_DB",
    os.path.join(BASE_DIR, "sportytips_users.db"),
)


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

        # Upgrade the existing database without deleting existing accounts.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        had_verified_column = "verified" in columns
        migrations = {
            "verified": "ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0",
            "verification_code_hash": "ALTER TABLE users ADD COLUMN verification_code_hash TEXT",
            "verification_expires_at": "ALTER TABLE users ADD COLUMN verification_expires_at TEXT",
            "verification_attempts": "ALTER TABLE users ADD COLUMN verification_attempts INTEGER NOT NULL DEFAULT 0",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)

        # Accounts created by the old account system were already usable.
        # If this is the first migration adding the verified column, preserve that status.
        if not had_verified_column:
            conn.execute("UPDATE users SET verified = 1")
        conn.commit()
    finally:
        conn.close()


def _normalise_identifier(value):
    return str(value or "").strip().lower()


def _valid_identifier(identifier):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", identifier))


def _public_user(row):
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "identifier": row["identifier"],
        "created_at": row["created_at"],
    }


_init_account_db()

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "").strip()
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "SportyTips").strip() or "SportyTips"
VERIFICATION_TTL_SECONDS = 10 * 60


def _send_verification_email(email, name, code):
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        raise RuntimeError("Brevo email is not configured on the server.")

    safe_name = html.escape(name, quote=True)
    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": email, "name": name}],
        "subject": "Verify your SportyTips account",
        "htmlContent": (
            "<!doctype html><html><body style=\"font-family:Arial,sans-serif;line-height:1.6;color:#222\">"
            f"<h2>SportyTips</h2><p>Hi {safe_name},</p>"
            "<p>Thanks for creating your SportyTips account.</p>"
            "<p>Enter this verification code to activate your account:</p>"
            f"<p style=\"font-size:30px;font-weight:700;letter-spacing:6px\">{code}</p>"
            "<p>This code expires in 10 minutes.</p>"
            "<p>If you did not create a SportyTips account, you can safely ignore this email.</p>"
            "<p>â SportyTips Team</p></body></html>"
        ),
        "textContent": (
            f"SportyTips\n\nHi {name},\n\n"
            "Thanks for creating your SportyTips account.\n\n"
            f"Your verification code is: {code}\n\n"
            "This code expires in 10 minutes.\n\n"
            "If you did not create a SportyTips account, you can safely ignore this email.\n\n"
            "â SportyTips Team"
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


def _new_verification_code():
    return f"{secrets.randbelow(1000000):06d}"


def _verification_expiry():
    return datetime.now(timezone.utc).timestamp() + VERIFICATION_TTL_SECONDS


def _verification_expiry_text(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


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
# RUN ONE CHAT TURN
# ===============================================================

def run_turn(session, text, q):
    _ctx.q = q

    try:

        with RUN_LOCK:
            bot.handle_text(session, text)

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
  .code-copy {
    margin-left: 10px;
    border: 0;
    background: #ff1f1f;
    color: #fff;
    font: inherit;
    font-size: 14px;
    font-weight: 700;
    padding: 6px 15px;
    border-radius: 999px;
    cursor: pointer;
    vertical-align: middle;
  }

  .code-copy:active {
    transform: scale(.96);
  }

  .code-copy.done {
    background: #16a765;
  }

  .content strong.is-code {
    font-size: 20px;
    letter-spacing: .04em;
  }
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

      setTimeout(function () {
        btn.textContent = "Copy";
        btn.classList.remove("done");
      }, 1500);
    }

    function fallback() {

      var t = document.createElement("textarea");

      t.value = text;
      t.style.position = "fixed";
      t.style.opacity = "0";

      document.body.appendChild(t);

      t.select();

      try {
        document.execCommand("copy");
      } catch (e) {}

      t.remove();

      done();
    }

    if (
      navigator.clipboard &&
      navigator.clipboard.writeText
    ) {

      navigator.clipboard
        .writeText(text)
        .then(done, fallback);

    } else {

      fallback();

    }
  }


  function scan() {

    box
      .querySelectorAll(".content strong")
      .forEach(function (s) {

        if (s.dataset.cc) return;

        var text = s.textContent.trim();

        if (!CODE.test(text)) return;

        var prev = s.previousSibling;

        var before =
          prev && prev.nodeType === 3
            ? prev.textContent
            : "";

        if (!/code:\s*$/i.test(before)) return;

        s.dataset.cc = "1";

        s.classList.add("is-code");

        var b = document.createElement("button");

        b.type = "button";
        b.className = "code-copy";
        b.textContent = "Copy";

        b.onclick = function () {
          copyText(text, b);
        };

        s.after(b);

      });

  }


  new MutationObserver(scan).observe(
    box,
    {
      childList: true,
      subtree: true
    }
  );

  scan();

})();
</script>
"""


# ===============================================================
# HOME PAGE
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


# ===============================================================
# INDEX.HTML
# ===============================================================

@app.get("/index.html")
def chat_page_alias():
    return redirect("/chat")


# ===============================================================
# CHAT PAGE
# ===============================================================

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
# ACCOUNT AUTHENTICATION
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

    if too_fast("auth:" + client_ip(), 12):
        return jsonify(error="Too many attempts. Try again in a minute."), 429

    password_hash = generate_password_hash(password)
    code = _new_verification_code()
    code_hash = generate_password_hash(code)
    created_at = datetime.now(timezone.utc).isoformat()
    expires_at = _verification_expiry_text(_verification_expiry())

    conn = _db()
    try:
        cur = conn.execute(
            """INSERT INTO users
               (name, identifier, password_hash, created_at, verified,
                verification_code_hash, verification_expires_at, verification_attempts)
               VALUES (?, ?, ?, ?, 0, ?, ?, 0)""",
            (name, identifier, password_hash, created_at, code_hash, expires_at),
        )
        conn.commit()
        user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return jsonify(error="An account with that email address already exists. Try logging in."), 409
    finally:
        conn.close()

    try:
        _send_verification_email(identifier, name, code)
    except Exception as exc:
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
        row = conn.execute(
            """SELECT id, name, identifier, password_hash, created_at, verified,
                      verification_code_hash, verification_expires_at, verification_attempts
               FROM users WHERE identifier = ?""",
            (identifier,),
        ).fetchone()

        if row is None:
            return jsonify(error="That account could not be found."), 404

        if int(row["verified"] or 0) == 1:
            return jsonify(user=_public_user(row), already_verified=True)

        attempts = int(row["verification_attempts"] or 0)
        if attempts >= 5:
            return jsonify(error="Too many incorrect codes. Request a new code."), 429

        expires_at = row["verification_expires_at"] or ""
        try:
            expired = datetime.fromisoformat(expires_at).timestamp() < datetime.now(timezone.utc).timestamp()
        except ValueError:
            expired = True

        if expired:
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

        session.clear()
        session["user_id"] = int(row["id"])
        return jsonify(user=_public_user(row), verified=True)
    finally:
        conn.close()


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

        code = _new_verification_code()
        code_hash = generate_password_hash(code)
        expires_at = _verification_expiry_text(_verification_expiry())
        conn.execute(
            """UPDATE users SET verification_code_hash = ?, verification_expires_at = ?,
               verification_attempts = 0 WHERE id = ?""",
            (code_hash, expires_at, int(row["id"])),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        _send_verification_email(identifier, row["name"], code)
    except Exception as exc:
        print(f"Verification resend error: {exc}")
        return jsonify(error="I couldn't resend the verification email. Please try again."), 502

    return jsonify(ok=True, message="A new verification code has been sent.")


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
        row = conn.execute(
            "SELECT id, name, identifier, password_hash, created_at, verified FROM users WHERE identifier = ?",
            (identifier,),
        ).fetchone()
    finally:
        conn.close()

    if row is None or not check_password_hash(row["password_hash"], password):
        return jsonify(error="Incorrect email address or password."), 401

    if int(row["verified"] or 0) != 1:
        return jsonify(error="Please verify your email before logging in.", verification_required=True, email=identifier), 403

    session.clear()
    session["user_id"] = int(row["id"])

    return jsonify(user=_public_user(row))


@app.get("/api/auth/me")
def auth_me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(user=None)

    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, name, identifier, created_at, verified FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    finally:
        conn.close()

    if row is None or int(row["verified"] or 0) != 1:
        session.clear()
        return jsonify(user=None)

    return jsonify(user=_public_user(row))


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify(ok=True)


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


# ===============================================================
# CHAT API
# ===============================================================

@app.post("/api/chat")
def chat():

    data = request.get_json(silent=True) or {}

    text = str(data.get("message", "")).strip()[:500]
    session = str(data.get("session", ""))[:64]

    if not text or not session:
        return jsonify(error="Empty message."), 400

    if too_fast(client_ip()):
        return jsonify(error="Slow down. Try again in a minute."), 429

    q = queue.Queue()

    threading.Thread(
        target=run_turn,
        args=(session, text, q),
        daemon=True,
    ).start()

    return _stream_response(q)


# ===============================================================
# STRAIGHT WIN
# ===============================================================

@app.post("/api/straight_win")
def api_straight_win():

    data = request.get_json(silent=True) or {}

    window = str(data.get("window", "today")).lower()
    session = str(data.get("session", ""))[:64]

    if window not in ("today", "long"):
        return jsonify(error="Unknown window."), 400

    if not session:
        return jsonify(error="Missing session."), 400

    if too_fast(client_ip()):
        return jsonify(error="Slow down. Try again in a minute."), 429

    text = "straight win long ticket" if window == "long" else "straight win today"

    q = queue.Queue()

    threading.Thread(
        target=run_turn,
        args=(session, text, q),
        daemon=True,
    ).start()

    return _stream_response(q)


# ===============================================================
# SPORTYBET CODE TOOLS
# ===============================================================

CODE_OK = re.compile(r"^[A-Z0-9]{5,12}$")


def _ticket_tool(work):

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

        # Messages written for the user (for example "nothing could be
        # made safer") are shown as they are.
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


# ===============================================================
# CHECK CODE
# ===============================================================

@app.post("/api/check")
def api_check():

    return _ticket_tool(lambda p, code, data: p.check_code(code))


# ===============================================================
# EDIT CODE
# ===============================================================

@app.post("/api/edit")
def api_edit():

    return _ticket_tool(
        lambda p, code, data: p.edit_code(
            code,
            remove=_ints(data.get("remove")),
            swap=_ints(data.get("swap")),
            picker=None,
        )
    )


# ===============================================================
# MAKE SAFER
# ===============================================================
# Every pick is swapped for a more likely pick on the SAME match,
# using SportyBet's own odds. A pick that cannot be improved is
# removed. The new ticket gets a new SportyBet code.

@app.post("/api/safer")
def api_safer():

    def work(provider, code, data):

        if SMART_TICKET_MODULE is None:
            raise RuntimeError("smart_ticket.py is not loaded")

        return SMART_TICKET_MODULE.safer_rebuild(provider, code)

    return _ticket_tool(work)


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

    return _ticket_tool(work)


# ===============================================================
# START
# ===============================================================

if __name__ == "__main__":

    if getattr(bot, "SPORTYBET_PROVIDER", None) is None:
        print("WARNING: SportyBet provider is not active.")

    if SMART_TICKET_MODULE is None:
        print("WARNING: smart_ticket.py is not loaded.")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        threaded=True,
    )
