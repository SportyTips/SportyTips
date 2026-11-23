"""Real push notifications (they arrive even when the app is closed).

Needs (on Render):
  requirements.txt:   pywebpush
  Environment:        VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT (for example mailto:you@gmail.com)
Without those the app works exactly as before and notifications stay off.

What users receive:
  ticket   "Your ticket is ready" - sent when a ticket finishes while the app is minimised or closed
  daily    Daily 2 odds is ready (every morning)
  weekend  Weekend tickets are ready (Friday afternoon)
  cups     European cups this week (Tuesday)
  ncaa     NCAA games tonight (every evening)
  news     messages the admin sends by hand (POST /api/push/broadcast)
Each user can switch every kind on or off in Settings. Edit SCHEDULE below to change the texts and times.

iPhone note: iOS only allows notifications for apps added to the Home Screen (Share, Add to Home Screen).
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Blueprint, jsonify, request

try:
    from pywebpush import WebPushException, webpush
except Exception:  # not installed yet: everything below stays switched off
    webpush = None
    WebPushException = Exception

VAPID_PUBLIC = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@sportytips.app").strip()

ENABLED = bool(webpush and VAPID_PUBLIC and VAPID_PRIVATE)

KINDS = {
    "ticket": "My ticket is ready",
    "daily": "Daily 2 odds is ready",
    "weekend": "Weekend tickets",
    "cups": "European cups nights",
    "ncaa": "NCAA games tonight",
    "news": "News and offers",
}

# days: 0 = Monday ... 6 = Sunday, None = every day. Times are in the bot's local time (WAT).
SCHEDULE = [
    {"key": "daily", "kind": "daily", "days": None, "at": (9, 0),
     "title": "Daily 2 odds is ready \U0001F3AF", "body": "Today's free ticket is waiting. Tap to open it."},
    {"key": "weekend", "kind": "weekend", "days": [4], "at": (14, 0),
     "title": "Weekend tickets are ready \u26BD", "body": "Top leagues and big games. Tell me your odds."},
    {"key": "cups", "kind": "cups", "days": [1], "at": (12, 0),
     "title": "European nights \U0001F3C6", "body": "Champions, Europa and Conference League tickets are ready."},
    {"key": "ncaa", "kind": "ncaa", "days": None, "at": (18, 0),
     "title": "NCAA games tonight \U0001F3C0", "body": "Ask: are there any ncaa tonight, then pick your odds."},
]

bp = Blueprint("push", __name__)
_cfg = {"db": None, "user": None, "admin": None, "tz": None}
_pool = ThreadPoolExecutor(max_workers=6)


# ------------------------------------------------------------
# storage
# ------------------------------------------------------------
def _init_tables():
    conn = _cfg["db"]()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS push_subs ("
            "endpoint TEXT PRIMARY KEY, user_id INTEGER NOT NULL, p256dh TEXT NOT NULL, "
            "auth TEXT NOT NULL, prefs TEXT NOT NULL DEFAULT '{}', created DOUBLE PRECISION NOT NULL)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS push_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()


def _allowed(prefs_text, kind):
    try:
        prefs = json.loads(prefs_text or "{}")
    except Exception:
        prefs = {}
    return bool(prefs.get(kind, True))  # everything is on until the user switches it off


def _subs(user_id=None, kind=None):
    conn = _cfg["db"]()
    try:
        if user_id is None:
            rows = conn.execute("SELECT * FROM push_subs").fetchall()
        else:
            rows = conn.execute("SELECT * FROM push_subs WHERE user_id = ?", (int(user_id),)).fetchall()
    finally:
        conn.close()

    return [r for r in rows if kind is None or _allowed(r["prefs"], kind)]


def _forget(endpoint):
    conn = _cfg["db"]()
    try:
        conn.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------
# sending
# ------------------------------------------------------------
def _send_one(row, payload):
    try:
        webpush(
            subscription_info={"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}},
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=6 * 3600,
        )
        return True
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in (404, 410):  # the phone removed the subscription
            _forget(row["endpoint"])
        else:
            print(f"Push failed ({code}): {str(exc)[:120]}")
    except Exception as exc:
        print(f"Push failed: {type(exc).__name__}: {str(exc)[:120]}")
    return False


def notify(rows, title, body, url="/chat", tag=None):
    """Send to these subscriptions in the background. Returns how many were queued."""
    if not ENABLED or not rows:
        return 0

    payload = {"title": title, "body": body, "url": url}
    if tag:
        payload["tag"] = tag

    for row in rows:
        _pool.submit(_send_one, row, payload)

    return len(rows)


def notify_user(user_id, kind, title, body, url="/chat", tag=None):
    try:
        return notify(_subs(user_id, kind), title, body, url, tag)
    except Exception as exc:
        print(f"Push to user failed: {type(exc).__name__}: {exc}")
        return 0


def notify_all(kind, title, body, url="/chat"):
    try:
        return notify(_subs(None, kind), title, body, url)
    except Exception as exc:
        print(f"Push to everyone failed: {type(exc).__name__}: {exc}")
        return 0


# ------------------------------------------------------------
# "your ticket is ready"
# ------------------------------------------------------------
def _summary(job):
    import html as _html
    import re

    for item in job.items:
        if item.get("type") != "text":
            continue

        text = _html.unescape(re.sub(r"<[^>]+>", "", str(item.get("html") or "")))
        line = next((l.strip() for l in text.split("\n") if "legs" in l and "odds" in l), "")

        if line:
            return line[:140]

    for item in job.items:
        if item.get("type") == "text":
            text = _html.unescape(re.sub(r"<[^>]+>", "", str(item.get("html") or ""))).strip()
            if text:
                return text.split("\n")[0][:140]

    return "Tap to open it."


def on_job_done(job):
    """Called when a chat turn finishes. Only notifies someone who is not looking at the screen."""
    if not ENABLED or job.watching():
        return

    notify_user(
        job.owner, "ticket",
        "Your ticket is ready \U0001F3AB", _summary(job),
        url="/chat", tag="job-" + job.id,
    )


# ------------------------------------------------------------
# scheduled notifications
# ------------------------------------------------------------
def _meta_get(key):
    conn = _cfg["db"]()
    try:
        row = conn.execute("SELECT v FROM push_meta WHERE k = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row["v"] if row else None


def _meta_set(key, value):
    conn = _cfg["db"]()
    try:
        conn.execute(
            "INSERT INTO push_meta (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (key, value)
        )
        conn.commit()
    finally:
        conn.close()


def run_schedule_once(now=None):
    """Sends every scheduled notification that is due and was not sent today. Returns the keys sent."""
    now = now or datetime.now(_cfg["tz"])
    today = now.strftime("%Y-%m-%d")
    sent = []

    for entry in SCHEDULE:

        if entry["days"] is not None and now.weekday() not in entry["days"]:
            continue

        hour, minute = entry["at"]

        if (now.hour, now.minute) < (hour, minute):
            continue

        # a late start (redeploy) may still send up to 3 hours after the time, never later
        if (now.hour * 60 + now.minute) - (hour * 60 + minute) > 180:
            continue

        mark = "sent:" + entry["key"]

        if _meta_get(mark) == today:
            continue

        _meta_set(mark, today)
        notify_all(entry["kind"], entry["title"], entry["body"])
        sent.append(entry["key"])

    return sent


def _scheduler():
    while True:
        try:
            run_schedule_once()
        except Exception as exc:
            print(f"Push schedule failed: {type(exc).__name__}: {exc}")
        time.sleep(45)


# ------------------------------------------------------------
# web routes
# ------------------------------------------------------------
def _need_user():
    user = _cfg["user"]()
    if user is None:
        return None, (jsonify(error="Please log in first.", login_required=True), 401)
    return user, None


@bp.get("/api/push/key")
def push_key():
    return jsonify(enabled=ENABLED, key=VAPID_PUBLIC if ENABLED else "", kinds=KINDS)


@bp.post("/api/push/subscribe")
def push_subscribe():
    user, err = _need_user()
    if err:
        return err
    if not ENABLED:
        return jsonify(error="Notifications are not switched on for this server yet."), 503

    sub = (request.get_json(silent=True) or {}).get("subscription") or {}
    keys = sub.get("keys") or {}
    endpoint, p256dh, auth = str(sub.get("endpoint", "")), str(keys.get("p256dh", "")), str(keys.get("auth", ""))

    if not (endpoint.startswith("https://") and p256dh and auth) or len(endpoint) > 1000:
        return jsonify(error="That subscription is not valid."), 400

    conn = _cfg["db"]()
    try:
        old = conn.execute("SELECT prefs FROM push_subs WHERE endpoint = ?", (endpoint,)).fetchone()
        conn.execute(
            "INSERT INTO push_subs (endpoint, user_id, p256dh, auth, prefs, created) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth",
            (endpoint, int(user["id"]), p256dh, auth, (old["prefs"] if old else "{}"), time.time()),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify(ok=True)


@bp.post("/api/push/unsubscribe")
def push_unsubscribe():
    user, err = _need_user()
    if err:
        return err

    endpoint = str((request.get_json(silent=True) or {}).get("endpoint", ""))

    conn = _cfg["db"]()
    try:
        conn.execute("DELETE FROM push_subs WHERE endpoint = ? AND user_id = ?", (endpoint, int(user["id"])))
        conn.commit()
    finally:
        conn.close()

    return jsonify(ok=True)


@bp.get("/api/push/prefs")
def push_prefs_get():
    user, err = _need_user()
    if err:
        return err

    endpoint = request.args.get("endpoint", "")
    conn = _cfg["db"]()
    try:
        row = conn.execute(
            "SELECT prefs FROM push_subs WHERE endpoint = ? AND user_id = ?", (endpoint, int(user["id"]))
        ).fetchone()
    finally:
        conn.close()

    try:
        saved = json.loads(row["prefs"]) if row else {}
    except Exception:
        saved = {}

    return jsonify(subscribed=bool(row), prefs={k: bool(saved.get(k, True)) for k in KINDS}, kinds=KINDS)


@bp.post("/api/push/prefs")
def push_prefs_set():
    user, err = _need_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    endpoint = str(data.get("endpoint", ""))
    prefs = {k: bool(v) for k, v in (data.get("prefs") or {}).items() if k in KINDS}

    conn = _cfg["db"]()
    try:
        conn.execute(
            "UPDATE push_subs SET prefs = ? WHERE endpoint = ? AND user_id = ?",
            (json.dumps(prefs), endpoint, int(user["id"])),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify(ok=True)


@bp.post("/api/push/test")
def push_test():
    user, err = _need_user()
    if err:
        return err

    rows = _subs(user["id"])
    sent = notify(rows, "SportyTips notifications are on \u2705", "You will see this when a ticket is ready.", "/chat")
    return jsonify(ok=bool(sent), devices=len(rows))


@bp.post("/api/push/broadcast")
def push_broadcast():
    user, err = _need_user()
    if err:
        return err
    if not _cfg["admin"](user):
        return jsonify(error="Admins only."), 403

    data = request.get_json(silent=True) or {}
    title, body = str(data.get("title", "")).strip()[:80], str(data.get("body", "")).strip()[:200]

    if not title or not body:
        return jsonify(error="Send a title and a body."), 400

    return jsonify(ok=True, queued=notify_all("news", title, body, str(data.get("url") or "/chat")))


def init(app, db, current_user, is_admin, tz):
    """app.py calls this once. A failure here never stops the site."""
    _cfg.update(db=db, user=current_user, admin=is_admin, tz=tz)
    app.register_blueprint(bp)

    if not ENABLED:
        print("NOTE: notifications are off (add pywebpush, VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY).")
        return

    _init_tables()
    threading.Thread(target=_scheduler, daemon=True).start()
    print(f"OK: push notifications on ({len(SCHEDULE)} scheduled messages)")
