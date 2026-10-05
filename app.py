"""Web frontend for SportyTips.

Web frontend:
  /       homepage
  /chat   chat page
  /api/chat
  /api/build
  /api/straight_win
  /api/check
  /api/edit
  /api/safer

Production:
  gunicorn app:app -w 1 --threads 8 --timeout 600

Required:
  USE_SPORTYBET=1
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

from flask import Flask, Response, jsonify, redirect, request

LAUNCHER_MODULE = os.getenv("LAUNCHER_MODULE", "launcher")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RATE_LIMIT = 8
RATE_WINDOW = 60
TOOL_LIMIT = 30

# ===============================================================
# LOAD BOT
# ===============================================================

bot = importlib.import_module("main")

# IMPORTANT:
# Import smart_ticket AFTER main.
# smart_ticket replaces bot.prediction_ticket_flow with its
# real SportyBet ticket builder.
try:
    smart_ticket = importlib.import_module("smart_ticket")
    print("✅ smart_ticket loaded.")
except Exception as exc:
    smart_ticket = None
    print(f"❌ smart_ticket failed to load: {type(exc).__name__}: {exc}")

try:
    launcher = importlib.import_module(LAUNCHER_MODULE)
except Exception as exc:
    launcher = None
    print(f"⚠️ Launcher failed to load: {exc}")

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
    text = re.sub(
        r"SamuelBet\s*(<span[^>]*>)\s*AI\s*(</span>)",
        r"Sporty\1Tips\2",
        text,
    )

    return (
        text.replace("SamuelBet AI", "SportyTips")
        .replace("SamuelBet", "SportyTips")
    )


def web_send_message(chat_id, text):
    kept = [
        line
        for line in str(text).split("\n")
        if not HIDE_LINES.search(line)
    ]

    cleaned = re.sub(
        r"\n{3,}",
        "\n\n",
        "\n".join(kept),
    ).strip()

    if not cleaned:
        cleaned = "I couldn't get that right now. Please try again in a minute."

    cleaned = (
        cleaned
        .replace("&#x27;", "'")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )

    _emit({
        "type": "text",
        "html": _rebrand(cleaned),
    })


def web_send_photo(chat_id, png, caption=""):
    try:
        encoded = base64.b64encode(png).decode("ascii")

        _emit({
            "type": "image",
            "data": encoded,
            "caption": caption or "",
        })

    except Exception as exc:
        _emit({
            "type": "text",
            "html": "🖼️ Could not display ticket image: "
                    + html.escape(str(exc)),
        })


# Replace Telegram output with browser output.
bot.send_message = web_send_message

# Prevent accidental Telegram calls from the web version.
bot.telegram_request = lambda method, params=None: {
    "ok": True,
    "result": [],
}

if launcher is not None:
    try:
        launcher.send_photo = web_send_photo
    except Exception:
        pass


# ===============================================================
# FLASK
# ===============================================================

app = Flask(__name__)

RUN_LOCK = threading.Lock()
_hits = {}
_active = set()


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
        request.headers.get(
            "X-Forwarded-For",
            ""
        ).split(",")[0].strip()
        or request.remote_addr
        or "?"
    )


# ===============================================================
# BACKGROUND TURN
# ===============================================================

def run_turn(session, text, q):
    _ctx.q = q

    try:
        # Keep one ticket build at a time.
        with RUN_LOCK:
            bot.handle_text(session, text)

    except Exception as exc:
        import traceback

        traceback.print_exc()

        q.put({
            "type": "text",
            "html": "❌ " + html.escape(str(exc)),
        })

    finally:
        _active.discard(session)
        q.put(None)


def start_turn(session, text):
    if session in _active:
        return None, (
            jsonify(
                error=(
                    "I'm still building your ticket. "
                    "Wait for it to finish before starting another one."
                )
            ),
            429,
        )

    if RUN_LOCK.locked():
        return None, (
            jsonify(
                error=(
                    "Another ticket is being built right now. "
                    "Please wait a little."
                )
            ),
            429,
        )

    _active.add(session)

    q = queue.Queue()

    thread = threading.Thread(
        target=run_turn,
        args=(session, text, q),
        daemon=True,
    )

    thread.start()

    return q, None


# ===============================================================
# STREAM
# ===============================================================

def _stream_response(q):
    def stream():
        while True:
            try:
                item = q.get(timeout=15)

            except queue.Empty:
                # Keep Render/browser connection alive.
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
# PAGES
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

        box.querySelectorAll(".content strong")
            .forEach(function (s) {

                if (s.dataset.cc) return;

                var text = s.textContent.trim();

                if (!CODE.test(text)) return;

                var prev = s.previousSibling;

                var before =
                    prev &&
                    prev.nodeType === 3
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

    new MutationObserver(scan)
        .observe(box, {
            childList: true,
            subtree: true
        });

    scan();
})();
</script>
"""


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
# HEALTH
# ===============================================================

@app.get("/health")
def health():
    provider = getattr(
        bot,
        "SPORTYBET_PROVIDER",
        None,
    )

    return jsonify({
        "status": "ok",
        "sportytips": True,
        "sportybet": provider is not None,
        "smart_ticket": smart_ticket is not None,
    })


# ===============================================================
# CHAT
# ===============================================================

@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}

    text = str(
        data.get("message", "")
    ).strip()[:500]

    session = str(
        data.get("session", "")
    )[:64]

    if not text or not session:
        return jsonify(
            error="Empty message."
        ), 400

    if too_fast(client_ip()):
        return jsonify(
            error="Slow down. Try again in a minute."
        ), 429

    q, busy = start_turn(
        session,
        text,
    )

    if busy:
        return busy

    return _stream_response(q)


# ===============================================================
# BUILD
#
# This exists because your frontend calls /api/build.
#
# It accepts:
# {
#   "message": "best 5 odds today"
# }
#
# or:
# {
#   "text": "best 5 odds today"
# }
#
# or:
# {
#   "prompt": "best 5 odds today"
# }
# ===============================================================

@app.post("/api/build")
def api_build():
    data = request.get_json(silent=True) or {}

    text = (
        data.get("message")
        or data.get("text")
        or data.get("prompt")
        or ""
    )

    text = str(text).strip()[:500]

    session = str(
        data.get("session", "")
    ).strip()[:64]

    if not text:
        return jsonify(
            error="Empty build request."
        ), 400

    if not session:
        # Generate a temporary session when the builder does not
        # send one.
        session = (
            "build-"
            + str(int(time.time() * 1000))
            + "-"
            + str(threading.get_ident())
        )

    if too_fast(client_ip()):
        return jsonify(
            error="Slow down. Try again in a minute."
        ), 429

    q, busy = start_turn(
        session,
        text,
    )

    if busy:
        return busy

    return _stream_response(q)


# ===============================================================
# STRAIGHT WIN
# ===============================================================

@app.post("/api/straight_win")
def api_straight_win():
    data = request.get_json(silent=True) or {}

    window = str(
        data.get("window", "today")
    ).lower()

    session = str(
        data.get("session", "")
    )[:64]

    if window not in ("today", "long"):
        return jsonify(
            error="Unknown window."
        ), 400

    if not session:
        return jsonify(
            error="Missing session."
        ), 400

    if too_fast(client_ip()):
        return jsonify(
            error="Slow down. Try again in a minute."
        ), 429

    if window == "long":
        text = "straight win long ticket"
    else:
        text = "straight win today"

    q, busy = start_turn(
        session,
        text,
    )

    if busy:
        return busy

    return _stream_response(q)


# ===============================================================
# TICKET TOOLS
# ===============================================================

CODE_OK = re.compile(
    r"^[A-Z0-9]{5,12}$"
)


def _ticket_tool(work):
    data = request.get_json(silent=True) or {}

    code = str(
        data.get("code", "")
    ).strip().upper()

    if not CODE_OK.match(code):
        return jsonify(
            error="That does not look like a SportyBet code."
        ), 400

    if too_fast(
        "tool:" + client_ip(),
        TOOL_LIMIT,
    ):
        return jsonify(
            error="Slow down. Try again in a minute."
        ), 429

    provider = getattr(
        bot,
        "SPORTYBET_PROVIDER",
        None,
    )

    if provider is None:
        return jsonify(
            error=(
                "SportyBet mode is off. "
                "Set USE_SPORTYBET=1 on Render."
            )
        ), 503

    try:
        return jsonify(
            work(
                provider,
                code,
                data,
            )
        )

    except Exception as exc:
        print(
            f"Ticket tool error: {exc}"
        )

        return jsonify(
            error=(
                "I could not read SportyBet right now: "
                + str(exc)
            )
        ), 502


def _ints(value):
    return [
        int(x)
        for x in (value or [])
        if str(x).isdigit()
    ][:30]


@app.post("/api/check")
def api_check():
    return _ticket_tool(
        lambda p, code, data:
            p.check_code(code)
    )


@app.post("/api/edit")
def api_edit():
    return _ticket_tool(
        lambda p, code, data:
            p.edit_code(
                code,
                remove=_ints(
                    data.get("remove")
                ),
                swap=_ints(
                    data.get("swap")
                ),
                picker=None,
            )
    )


@app.post("/api/safer")
def api_safer():
    return _ticket_tool(
        lambda p, code, data:
            p.make_safer(code)
    )


# ===============================================================
# OPTIONAL /api/why
#
# Your frontend has mentioned /api/why, so provide a simple
# compatible endpoint if the provider supports it.
# ===============================================================

@app.post("/api/why")
def api_why():
    data = request.get_json(silent=True) or {}

    code = str(
        data.get("code", "")
    ).strip().upper()

    if not CODE_OK.match(code):
        return jsonify(
            error="That does not look like a SportyBet code."
        ), 400

    provider = getattr(
        bot,
        "SPORTYBET_PROVIDER",
        None,
    )

    if provider is None:
        return jsonify(
            error="SportyBet mode is off."
        ), 503

    try:
        if hasattr(provider, "check_code"):
            result = provider.check_code(code)

            return jsonify({
                "ok": True,
                "code": code,
                "data": result,
            })

        return jsonify({
            "ok": True,
            "code": code,
            "message": "SportyBet ticket loaded.",
        })

    except Exception as exc:
        return jsonify(
            error=str(exc)
        ), 502


# ===============================================================
# START LOCAL SERVER
# ===============================================================

if __name__ == "__main__":

    if getattr(
        bot,
        "SPORTYBET_PROVIDER",
        None
    ) is None:
        print(
            "⚠️ WARNING: USE_SPORTYBET is not set to 1."
        )

    else:
        print(
            "✅ SportyBet provider is ON."
        )

    if smart_ticket is None:
        print(
            "❌ WARNING: smart_ticket.py is not loaded."
        )
    else:
        print(
            "✅ Smart ticket builder is loaded."
        )

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8000"
            )
        ),
        threaded=True,
    )