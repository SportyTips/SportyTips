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
  MAX_PARALLEL_TICKETS=3   (optional, how many tickets build at once)
"""

import base64
import html
import importlib
import json
import os
import queue
import re
import threading
import time
import traceback

from flask import Flask, Response, jsonify, redirect, request


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RATE_LIMIT = 8
RATE_WINDOW = 60
TOOL_LIMIT = 30

# How many tickets may be built at the same time.
MAX_PARALLEL_TICKETS = max(
    1,
    int(os.getenv("MAX_PARALLEL_TICKETS", "3")),
)

LAUNCHER_MODULE = os.getenv("LAUNCHER_MODULE", "launcher")


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
    SPORTYBET_PROVIDER_MODULE = importlib.import_module(
        "sportybet_provider"
    )
    print("✅ sportybet_provider.py loaded successfully")

except Exception as exc:
    SPORTYBET_PROVIDER_ERROR = f"{type(exc).__name__}: {exc}"

    print(
        "❌ sportybet_provider.py failed to load:",
        SPORTYBET_PROVIDER_ERROR,
    )


# ===============================================================
# LOAD MAIN
# ===============================================================

try:
    bot = importlib.import_module("main")
    print("✅ main.py loaded successfully")

except Exception as exc:
    print(
        "❌ main.py failed to load:",
        f"{type(exc).__name__}: {exc}",
    )
    raise


# ===============================================================
# LOAD SMART TICKET BUILDER
# ===============================================================
#
# smart_ticket.py replaces main.prediction_ticket_flow with the
# real SportyTips ticket-building logic.
# ===============================================================

SMART_TICKET_MODULE = None
SMART_TICKET_ERROR = None

try:
    SMART_TICKET_MODULE = importlib.import_module("smart_ticket")

    print("✅ smart_ticket.py loaded successfully")

except Exception as exc:
    SMART_TICKET_ERROR = f"{type(exc).__name__}: {exc}"

    print(
        "❌ smart_ticket.py failed to load:",
        SMART_TICKET_ERROR,
    )


# ===============================================================
# LOAD LAUNCHER
# ===============================================================

try:
    launcher = importlib.import_module(LAUNCHER_MODULE)
    print(f"✅ {LAUNCHER_MODULE}.py loaded successfully")

except Exception as exc:
    print(
        f"❌ {LAUNCHER_MODULE}.py failed to load:",
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

    cleaned = re.sub(
        r"\n{3,}",
        "\n\n",
        "\n".join(kept),
    ).strip()

    if not cleaned:
        cleaned = (
            "I couldn't get that right now. "
            "Please try again in a minute."
        )

    cleaned = (
        cleaned
        .replace("&#x27;", "'")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )

    _emit(
        {
            "type": "text",
            "html": _rebrand(cleaned),
        }
    )


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

bot.telegram_request = (
    lambda method, params=None:
    {
        "ok": True,
        "result": [],
    }
)

launcher.send_photo = web_send_photo


# ===============================================================
# FLASK
# ===============================================================

app = Flask(__name__)

# Allows a few tickets to build at once instead of one at a time.
RUN_LOCK = threading.Semaphore(MAX_PARALLEL_TICKETS)

_hits = {}


# ===============================================================
# RATE LIMIT
# ===============================================================

def too_fast(key, limit=RATE_LIMIT):
    now = time.time()

    recent = [
        t
        for t in _hits.get(key, [])
        if now - t < RATE_WINDOW
    ]

    if len(recent) >= limit:
        _hits[key] = recent
        return True

    recent.append(now)

    _hits[key] = recent

    return False


def client_ip():
    return (
        request.headers.get("X-Forwarded-For", "")
        .split(",")[0]
        .strip()
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
            bot.handle_text(
                session,
                text,
            )

    except Exception as exc:

        # Full details go to the server log only.
        print(f"Chat turn failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()

        q.put(
            {
                "type": "text",
                "html": (
                    "❌ Something went wrong on my side. "
                    "Please try again in a minute."
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

        return (
            "index.html is missing next to app.py.",
            404,
        )

    with open(path, encoding="utf-8") as fh:
        page = _rebrand(fh.read())

    if "</body>" in page:

        page = page.replace(
            "</body>",
            CHAT_EXTRAS + "</body>",
            1,
        )

    else:

        page += CHAT_EXTRAS

    return Response(
        page,
        mimetype="text/html",
        headers={"Cache-Control": "no-cache"},
    )


# ===============================================================
# HEALTH CHECK
# ===============================================================

@app.get("/health")
def health():

    provider_loaded = (
        getattr(bot, "SPORTYBET_PROVIDER", None)
        is not None
    )

    return jsonify(
        status="ok",
        sportybet=provider_loaded,
        sportybet_error=SPORTYBET_PROVIDER_ERROR,
        smart_ticket=SMART_TICKET_MODULE is not None,
        smart_ticket_error=SMART_TICKET_ERROR,
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

        return jsonify(
            error="Slow down. Try again in a minute."
        ), 429

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

        return jsonify(
            error="Slow down. Try again in a minute."
        ), 429

    if window == "long":
        text = "straight win long ticket"
    else:
        text = "straight win today"

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

        return jsonify(
            error="That does not look like a SportyBet code."
        ), 400

    if too_fast("tool:" + client_ip(), TOOL_LIMIT):

        return jsonify(
            error="Slow down. Try again in a minute."
        ), 429

    provider = getattr(bot, "SPORTYBET_PROVIDER", None)

    if provider is None:

        return jsonify(
            error=(
                "SportyBet mode is off. "
                "Set USE_SPORTYBET to 1 on the server."
            )
        ), 503

    try:

        return jsonify(work(provider, code, data))

    except Exception as exc:

        print(f"Ticket tool error: {type(exc).__name__}: {exc}")
        traceback.print_exc()

        return jsonify(
            error=(
                "I could not read SportyBet right now. "
                "Please try again in a minute."
            )
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

    return _ticket_tool(
        lambda p, code, data: p.check_code(code)
    )


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

@app.post("/api/safer")
def api_safer():

    return _ticket_tool(
        lambda p, code, data: p.make_safer(code)
    )


# ===============================================================
# START
# ===============================================================

if __name__ == "__main__":

    if getattr(bot, "SPORTYBET_PROVIDER", None) is None:

        print("⚠️ WARNING: SportyBet provider is not active.")

    if SMART_TICKET_MODULE is None:

        print("⚠️ WARNING: smart_ticket.py is not loaded.")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        threaded=True,
    )
