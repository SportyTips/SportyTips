"""Web frontend for SportyTips.

Does NOT edit your bot files. It imports them, swaps the Telegram send
functions for ones that stream to the browser, and calls the bot's own
handle_text() for every message.

Pages (it finds the chat page by itself: the file with the message box):
  /       the homepage (home.html)
  /chat   the chat (index.html)
  /status  "free" or "busy" (shows if a ticket is still being built)

Put this file next to main.py, launcher.py, sportybet_provider.py,
ticket_image_lite.py, smart_ticket.py, upgrades.py, home.html and index.html.

Run locally:   python app.py
Production:    gunicorn app:app -w 1 --threads 8 --timeout 600
(keep -w 1: the bot keeps its caches and chat memory in this process)

Env vars:
  USE_SPORTYBET=1 (required for tickets)
  TELEGRAM_BOT_TOKEN is NOT needed for the web frontend.
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

from flask import Flask, Response, jsonify, redirect, request, send_from_directory

LAUNCHER_MODULE = os.getenv("LAUNCHER_MODULE", "launcher")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RATE_LIMIT = 8
RATE_WINDOW = 60
TOOL_LIMIT = 30
LOCK_WAIT_SECONDS = 240      # how long a message waits behind another ticket before giving up

bot = importlib.import_module("main")
launcher = importlib.import_module(LAUNCHER_MODULE)
try:
    upgrades = importlib.import_module("upgrades")
except ImportError:
    upgrades = None

# ---------------------------------------------------------------
# Replace Telegram output with a per-request queue
# ---------------------------------------------------------------
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


def web_send_message(chat_id, text):
    kept = [line for line in text.split("\n") if not HIDE_LINES.search(line)]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if not cleaned:
        cleaned = "I couldn't get that right now. Please try again in a minute."
    _emit({"type": "text", "html": cleaned})


def web_send_photo(chat_id, png, caption=""):
    _emit({"type": "image", "data": base64.b64encode(png).decode("ascii"),
           "caption": caption})


bot.send_message = web_send_message
bot.telegram_request = lambda method, params=None: {"ok": True, "result": []}
launcher.send_photo = web_send_photo

# ---------------------------------------------------------------
# Server
# ---------------------------------------------------------------
app = Flask(__name__)
RUN_LOCK = threading.Lock()
_hits = {}


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
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


def run_turn(session, text, q):
    """Runs one message. Only one ticket is built at a time, so if another one is
    still running we SAY so instead of loading in silence."""
    _ctx.q = q
    got_lock = RUN_LOCK.acquire(blocking=False)
    try:
        if not got_lock:
            q.put({"type": "text",
                   "html": "⏳ I'm still finishing another ticket. Yours is next, one moment..."})
            got_lock = RUN_LOCK.acquire(timeout=LOCK_WAIT_SECONDS)
            if not got_lock:
                q.put({"type": "text",
                       "html": "I'm still busy with another ticket. Please try again in a minute."})
                return
        bot.handle_text(session, text)
    except Exception as exc:
        q.put({"type": "text", "html": "❌ " + html.escape(str(exc))})
    finally:
        if got_lock:
            RUN_LOCK.release()
        q.put(None)


CHAT_EXTRAS = r"""
<style>
  .code-copy { margin-left: 10px; border: 0; background: #ff1f1f; color: #fff; font: inherit; font-size: 14px;
    font-weight: 700; padding: 6px 15px; border-radius: 999px; cursor: pointer; vertical-align: middle; }
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
      btn.textContent = "Copied"; btn.classList.add("done");
      setTimeout(function () { btn.textContent = "Copy"; btn.classList.remove("done"); }, 1500);
    }
    function fallback() {
      var t = document.createElement("textarea");
      t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
      document.body.appendChild(t); t.select();
      try { document.execCommand("copy"); } catch (e) {}
      t.remove(); done();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else { fallback(); }
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
      b.type = "button"; b.className = "code-copy"; b.textContent = "Copy";
      b.onclick = function () { copyText(text, b); };
      s.after(b);
    });
  }
  new MutationObserver(scan).observe(box, { childList: true, subtree: true });
  scan();
})();
</script>
"""


def _read(name):
    path = os.path.join(BASE_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _pages():
    """(landing_page, chat_page). The chat page is the file that contains the message box,
    so it works even if home.html and index.html have been swapped."""
    pages = [p for p in (_read("home.html"), _read("index.html")) if p]
    chat = next((p for p in pages if 'id="messages"' in p), None)
    landing = next((p for p in pages if 'id="messages"' not in p), None)
    return landing, chat


NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def home_page():
    landing, chat = _pages()
    if landing is None:
        return redirect("/chat")          # no homepage file: go straight to chat
    return Response(landing, mimetype="text/html", headers=NO_CACHE)


@app.get("/index.html")
def chat_page_alias():
    return redirect("/chat")


@app.get("/chat")
def chat_page():
    landing, page = _pages()
    if page is None:
        return "The chat page is missing. It is the file that has the message box.", 404
    if "</body>" in page:
        page = page.replace("</body>", CHAT_EXTRAS + "</body>", 1)
    else:
        page += CHAT_EXTRAS
    return Response(page, mimetype="text/html", headers=NO_CACHE)


@app.get("/health")
def health():
    return "ok"


@app.get("/status")
def status():
    """Open this while the chat is loading: 'busy' means a ticket is still being built."""
    return "busy" if RUN_LOCK.locked() else "free"


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

    return Response(stream(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    threading.Thread(target=run_turn, args=(session, text, q), daemon=True).start()
    return _stream_response(q)


# ---------------------------------------------------------------
# Straight-win shortcuts (used by the two buttons in the web UI)
# ---------------------------------------------------------------
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

    # Inject the trigger message as if the user typed it
    text = "straight win long ticket" if window == "long" else "straight win today"

    q = queue.Queue()
    threading.Thread(target=run_turn, args=(session, text, q), daemon=True).start()
    return _stream_response(q)


# ---------------------------------------------------------------
# Ticket tools (check / edit / make safer a SportyBet code)
# ---------------------------------------------------------------
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
        return jsonify(error="SportyBet mode is off. Set USE_SPORTYBET to 1 on the server."), 503
    try:
        return jsonify(work(provider, code, data))
    except Exception as exc:
        print(f"Ticket tool error: {exc}")
        return jsonify(error=f"I could not read SportyBet right now: {exc}"), 502


def _ints(value):
    return [int(x) for x in (value or []) if str(x).isdigit()][:30]


@app.post("/api/check")
def api_check():
    return _ticket_tool(lambda p, code, data: p.check_code(code))


@app.post("/api/edit")
def api_edit():
    return _ticket_tool(lambda p, code, data: p.edit_code(
        code, remove=_ints(data.get("remove")), swap=_ints(data.get("swap")), picker=None))


@app.post("/api/safer")
def api_safer():
    return _ticket_tool(lambda p, code, data: p.make_safer(code))


if __name__ == "__main__":
    if bot.SPORTYBET_PROVIDER is None:
        print("WARNING: USE_SPORTYBET is not set to 1, so tickets cannot be built.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)