"""Ticket history for SportyTips.

Every ticket the site builds is saved here as a few hundred bytes of text
(never a picture). The picture is redrawn on demand with a tick or a cross on
each pick and a WON / CANCELLED stamp.

How a ticket gets its status
    pending     some picks are still waiting for a result
    cancelled   one pick lost (the ticket is spoiled straight away)
    won         every pick won
    unverified  no result could be found after a few days (not counted)

Storage uses the same database as the accounts (Neon Postgres when DATABASE_URL
is set, otherwise SQLite). app.py calls init() once and starts the settle loop.
"""

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

# The real picture function, captured BEFORE app.py wraps it for recording.
try:
    import ticket_image_lite as _til

    _RENDER = _til.make_ticket_image
    _STATUS = _til.ticket_status
except Exception:  # pragma: no cover - picture is optional
    _til = None
    _RENDER = None
    _STATUS = None

_cfg = {
    "db": None,
    "pg": False,
    "insert_id": None,
    "fetch_day": None,   # fetch_day("YYYY-MM-DD") -> list of football-data.org matches
    "similar": None,     # similar(a, b) -> 0..1 team name similarity
    "fetch_code": None,  # fetch_code("SNJLYK") -> raw SportyBet booking-code JSON
}

# Settle settings
MATCH_MINUTES = 135            # a game is treated as over this long after kickoff
RECHECK_SECONDS = 25 * 60      # do not look at the same ticket more often than this
UNVERIFIED_AFTER_DAYS = 4      # after this, picks nobody could verify stop waiting
MAX_DAYS_PER_PASS = 4          # football-data.org free plan allows few requests
LOOP_SECONDS = 20 * 60

_lock = threading.Lock()
_settle_lock = threading.Lock()
_stats_cache = {"at": 0.0, "value": None}


# ===============================================================
# SETUP
# ===============================================================

def init(db, use_pg, insert_id, fetch_day=None, similar=None, fetch_code=None):
    _cfg.update(db=db, pg=use_pg, insert_id=insert_id, fetch_day=fetch_day,
                similar=similar, fetch_code=fetch_code)
    conn = db()
    try:
        pk = "SERIAL PRIMARY KEY" if use_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        real = "DOUBLE PRECISION" if use_pg else "REAL"
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS tickets (
                id {pk},
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT NOT NULL DEFAULT '',
                total_odds {real} NOT NULL DEFAULT 0,
                win_chance {real} NOT NULL DEFAULT 0,
                code TEXT,
                legs TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_kickoff {real} NOT NULL DEFAULT 0,
                last_check {real} NOT NULL DEFAULT 0,
                settled_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (user_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status, last_kickoff)")
        conn.commit()
    finally:
        conn.close()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===============================================================
# SAVE A TICKET
# ===============================================================

_KEEP = ("match", "pick", "odd", "time", "league", "prob", "kind",
         "home", "away", "kickoff_ts", "result", "source")


def _clean_leg(row):
    leg = {}
    for key in _KEEP:
        value = row.get(key)
        if value is None:
            continue
        if key in ("odd", "prob", "kickoff_ts"):
            try:
                value = round(float(value), 4 if key == "prob" else 3)
            except (TypeError, ValueError):
                continue
        else:
            value = str(value)[:120]
        leg[key] = value
    return leg


def record_ticket(user_id, rows, title, subtitle, total_odds, win_chance, code):
    """Save one finished ticket. Returns its id (or None if it could not be saved)."""
    try:
        legs = [_clean_leg(r) for r in (rows or []) if isinstance(r, dict)]
        if not legs or not user_id:
            return None

        code = str(code).strip().upper() if code else None
        last_kick = max([leg.get("kickoff_ts") or 0 for leg in legs] or [0])

        conn = _cfg["db"]()
        try:
            if code:
                # The same code built twice for the same person is one ticket.
                row = conn.execute(
                    "SELECT id FROM tickets WHERE user_id = ? AND code = ? AND created_at > ?",
                    (int(user_id), code,
                     (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")),
                ).fetchone()
                if row is not None:
                    return int(row["id"])

            ticket_id = _cfg["insert_id"](
                conn,
                "INSERT INTO tickets (user_id, created_at, title, subtitle, total_odds, "
                "win_chance, code, legs, status, last_kickoff, last_check) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0)",
                (int(user_id), _now_iso(), str(title)[:60], str(subtitle or "")[:60],
                 float(total_odds or 0), float(win_chance or 0), code,
                 json.dumps(legs, separators=(",", ":")), float(last_kick)),
            )
            conn.commit()
            _stats_cache["at"] = 0.0
            return ticket_id
        finally:
            conn.close()
    except Exception as exc:
        print(f"History: could not save ticket: {type(exc).__name__}: {exc}")
        return None


# ===============================================================
# DECIDING A PICK
# ===============================================================

def _norm(text):
    return re.sub(r"[^a-z0-9 ]+", "", str(text or "").lower()).strip()


def _text(value):
    """Lower-case text that KEEPS dots and brackets (so 1.5 and (1UP) survive)."""
    return " ".join(str(value or "").lower().split())


def _line(text, default=None):
    match = re.search(r"(\d+(?:\.\d+)?)", text or "")
    return float(match.group(1)) if match else default


def _side(label, home, away):
    """Which team a label talks about: 'home', 'away' or None."""
    text = _norm(label)
    h, a = _norm(home), _norm(away)
    in_h = bool(h) and h in text
    in_a = bool(a) and a in text
    if in_h and not in_a:
        return "home"
    if in_a and not in_h:
        return "away"
    if in_h and in_a:
        return "home" if text.find(h) <= text.find(a) else "away"
    return None


def _clause(text, res, home, away):
    """Decide one simple statement. True / False / None (cannot tell)."""
    t = _text(text)
    h, a = res["h"], res["a"]
    hh, ha = res.get("hh"), res.get("ha")

    # period goals
    period = None
    if re.search(r"\b(1st|first) half\b", t):
        period = "1h"
    elif re.search(r"\b(2nd|second) half\b", t):
        period = "2h"

    if period and hh is None:
        return None

    def goals(which):
        if period == "1h":
            return {"total": hh + ha, "home": hh, "away": ha}[which]
        if period == "2h":
            return {"total": (h - hh) + (a - ha), "home": h - hh, "away": a - ha}[which]
        return {"total": h + a, "home": h, "away": a}[which]

    if "both teams to score" in t:
        both = goals("home") > 0 and goals("away") > 0
        return (not both) if re.search(r"\bno\b", t) else both

    over = re.search(r"\bover (\d+(?:\.\d+)?)", t)
    under = re.search(r"\bunder (\d+(?:\.\d+)?)", t)
    if (over or under) and "corner" not in t and "card" not in t:
        which = "total"
        if "team goal" in t:
            side = _side(text, home, away)
            if side is None:
                return None
            which = side
        value = goals(which)
        if over:
            return value > float(over.group(1))
        return value < float(under.group(1))

    return None


def settle_leg(leg, res):
    """Return 'won', 'lost', 'void' or None when the score cannot prove it."""
    label = str(leg.get("pick") or "")
    kind = str(leg.get("kind") or "")
    home = leg.get("home") or ""
    away = leg.get("away") or ""
    t = _text(label)
    h, a = res["h"], res["a"]
    hh, ha = res.get("hh"), res.get("ha")

    if "corner" in t or "card" in t or "after 10 minutes" in t or "10 min" in t:
        return None  # football-data.org does not give corners or minute-by-minute data

    def yes(value):
        if value is None:
            return None
        return "won" if value else "lost"

    # Home or Away (12)
    if re.search(r"\(12\)", label) or kind == "dc12":
        return yes(h != a)

    # Double chance: "X or Draw" / "Draw or Y"
    if re.search(r"\bor draw\b", t) or re.search(r"\bdraw or\b", t):
        if re.search(r"\bdraw or\b", t):
            return yes(a >= h)
        side = _side(label, home, away)
        if side is None:
            return None
        return yes(h >= a if side == "home" else a >= h)

    # Draw no bet
    if "draw no bet" in t or kind == "dnb":
        side = _side(label, home, away)
        if side is None:
            return None
        if h == a:
            return "void"
        return yes(h > a if side == "home" else a > h)

    # Handicap: "{team} +1 handicap"
    if "handicap" in t or kind == "handicap":
        side = _side(label, home, away)
        line = re.search(r"([+-]\s*\d+(?:\.\d+)?)", label)
        if side is None or not line:
            return None
        value = float(line.group(1).replace(" ", ""))
        mine, other = (h, a) if side == "home" else (a, h)
        diff = mine + value - other
        if diff > 0:
            return "won"
        if diff == 0 and value == int(value):
            return "void"
        return "lost"

    # 1UP / 2UP: a final score can only prove some of them
    up = re.search(r"\((\d)\s*up\)", t)
    if up:
        need = int(up.group(1))
        side = _side(label, home, away)
        if side is None:
            return None
        mine, other = (h, a) if side == "home" else (a, h)
        if mine - other >= need:
            return "won"      # finished ahead by enough, so they were ahead at some point
        if mine < need:
            return "lost"     # never scored enough to be ahead by that much
        return None           # could have led and lost it: only a goal timeline can tell

    # Win either half
    if "either half" in t:
        if hh is None:
            return None
        side = _side(label, home, away)
        if side is None:
            return None
        first = (hh - ha) if side == "home" else (ha - hh)
        second = ((h - hh) - (a - ha)) if side == "home" else ((a - ha) - (h - hh))
        return yes(first > 0 or second > 0)

    # Both halves over/under 1.5
    if "both halves" in t:
        if hh is None:
            return None
        line = _line(t.split("both halves", 1)[1], 1.5)
        over_dir = "over" in t.split("both halves", 1)[1]
        f, s = hh + ha, (h - hh) + (a - ha)
        ok = (f > line and s > line) if over_dir else (f < line and s < line)
        return yes(ok if "yes" in t else not ok)

    # Plain win (also catches "{team} to win")
    if re.search(r"\bto win\b", t) and not re.search(r"\b(half|halves)\b", t):
        side = _side(label, home, away)
        if side is None:
            return None
        return yes(h > a if side == "home" else a > h)

    # Combos joined with "&"
    if "&" in label:
        parts = [_clause(p, res, home, away) for p in label.split("&")]
        if any(p is None for p in parts):
            return None
        return yes(all(parts))

    # Goals / BTTS / team goals (full time or one half)
    return yes(_clause(label, res, home, away))


def _ticket_status(legs, age_days):
    results = [str(leg.get("result") or "") for leg in legs]
    if "lost" in results:
        return "cancelled"
    live = [r for r in results if r != "void"]
    if live and all(r == "won" for r in live):
        return "won"
    if not live and results:
        return "unverified"
    if age_days is not None and age_days >= UNVERIFIED_AFTER_DAYS and any(r == "" for r in results):
        return "unverified"
    return "pending"


# ===============================================================
# FINDING RESULTS
# ===============================================================

def _result_from_match(match):
    status = str(match.get("status") or "").upper()
    if status in ("POSTPONED", "CANCELLED", "CANCELED"):
        return {"void": True}
    if status not in ("FINISHED", "AWARDED"):
        return None
    score = match.get("score") or {}
    full = score.get("regularTime") or score.get("fullTime") or {}
    half = score.get("halfTime") or {}
    h, a = full.get("home"), full.get("away")
    # With extra time, football-data.org's fullTime includes it; markets use 90 minutes.
    if str(score.get("duration") or "").upper() in ("EXTRA_TIME", "PENALTY_SHOOTOUT"):
        reg = score.get("regularTime") or {}
        if reg.get("home") is not None:
            h, a = reg.get("home"), reg.get("away")
    if h is None or a is None:
        return None
    res = {"h": int(h), "a": int(a)}
    if half.get("home") is not None and half.get("away") is not None:
        res["hh"], res["ha"] = int(half["home"]), int(half["away"])
    return res


def _find_match(leg, day_matches):
    similar = _cfg.get("similar")
    if not similar:
        return None, False
    home, away = leg.get("home") or "", leg.get("away") or ""
    best, best_score, flipped = None, 0.0, False
    for match in day_matches:
        mh = ((match.get("homeTeam") or {}).get("name")) or ""
        ma = ((match.get("awayTeam") or {}).get("name")) or ""
        straight = (similar(home, mh) + similar(away, ma)) / 2.0
        reverse = (similar(home, ma) + similar(away, mh)) / 2.0
        score = max(straight, reverse)
        if score > best_score:
            best, best_score, flipped = match, score, reverse > straight
    if best is None or best_score < 0.80:
        return None, False
    return best, flipped


# ---------------------------------------------------------------
# SportyBet's own booking-code page (second source for results)
# ---------------------------------------------------------------

SPORTY_PER_PASS = 15          # booking codes looked at per check
SPORTY_SAFE_MINUTES = 190     # when no "finished" flag is shown, wait this long after kickoff


def _score_pair(value):
    """'2:1' / ['2:1'] / [2, 1] -> (2, 1), else None."""
    if isinstance(value, (list, tuple)):
        if len(value) == 1:
            return _score_pair(value[0])
        if len(value) == 2 and all(isinstance(x, (int, float)) for x in value):
            return int(value[0]), int(value[1])
        return None
    if isinstance(value, str):
        m = re.match(r"^\s*(\d+)\s*[:\-]\s*(\d+)\s*$", value)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _sporty_events(payload):
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    events = (data or {}).get("outcomes") if isinstance(data, dict) else None
    return [e for e in (events or []) if isinstance(e, dict)]


def _sporty_result(event, leg_kickoff):
    """Final score from one event of the booking page, only when it is clearly over."""
    score = None
    for key in ("gameScore", "score", "finalScore"):
        score = _score_pair(event.get(key))
        if score:
            break
    if not score:
        return None
    status = str(event.get("matchStatus") or event.get("status") or "").strip().lower()
    over = bool(status) and (
        status.startswith("end") or "finish" in status or status in ("ft", "closed", "after")
    )
    if status and not over:
        return None  # live, not started, postponed ... never guess
    if not status and (not leg_kickoff or time.time() < float(leg_kickoff) + SPORTY_SAFE_MINUTES * 60):
        return None
    return {"h": score[0], "a": score[1]}


def _sporty_pick_event(leg, events):
    home, away = _norm(leg.get("home")), _norm(leg.get("away"))
    similar = _cfg.get("similar")
    best, best_score = None, 0.0
    for event in events:
        eh, ea = _norm(event.get("homeTeamName")), _norm(event.get("awayTeamName"))
        if eh == home and ea == away:
            return event
        if similar:
            score = (similar(leg.get("home") or "", event.get("homeTeamName") or "")
                     + similar(leg.get("away") or "", event.get("awayTeamName") or "")) / 2.0
            if score > best_score:
                best, best_score = event, score
    return best if best_score >= 0.92 else None


def probe_code(code):
    """Admin helper: show what SportyBet returns for a booking code (no markets, no secrets)."""
    fetch = _cfg.get("fetch_code")
    if not fetch:
        return {"error": "SportyBet is not connected."}
    payload = fetch(code)
    events = _sporty_events(payload)
    out = {"events": len(events), "sample": [], "readable": 0}
    for event in events[:3]:
        flat = {k: v for k, v in event.items()
                if isinstance(v, (str, int, float, bool)) or v is None
                or (isinstance(v, list) and len(v) <= 3 and all(isinstance(x, (str, int, float)) for x in v))}
        out["sample"].append({k: flat[k] for k in list(flat)[:30]})
    for event in events:
        if _sporty_result(event, event.get("estimateStartTime", 0) / 1000.0 if event.get("estimateStartTime") else 0):
            out["readable"] += 1
    return out


def _dates_for(leg):
    ts = leg.get("kickoff_ts")
    if not ts:
        return []
    kick = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    days = [kick.date()]
    if kick.hour >= 22:
        days.append(kick.date() + timedelta(days=1))
    if kick.hour <= 1:
        days.append(kick.date() - timedelta(days=1))
    return [d.strftime("%Y-%m-%d") for d in days]


def settle_pending(force=False):
    """Check waiting tickets and mark finished picks. Safe to call any time."""
    if not (_cfg.get("fetch_day") or _cfg.get("fetch_code")) or not _settle_lock.acquire(blocking=False):
        return {"checked": 0}
    checked = 0
    try:
        now = time.time()
        conn = _cfg["db"]()
        try:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE status = 'pending' AND last_kickoff > 0 "
                "AND last_kickoff < ? ORDER BY id LIMIT 300",
                (now - 60 * 5,),
            ).fetchall()
        finally:
            conn.close()

        work = []
        for row in rows:
            if not force and now - float(row["last_check"] or 0) < RECHECK_SECONDS:
                continue
            legs = json.loads(row["legs"])
            todo = [
                leg for leg in legs
                if not leg.get("result") and leg.get("kickoff_ts")
                and now > float(leg["kickoff_ts"]) + MATCH_MINUTES * 60
            ]
            if todo or now - float(row["last_kickoff"]) > UNVERIFIED_AFTER_DAYS * 86400:
                work.append((row, legs, todo))

        # fetch each needed day once
        days = []
        for _, _, todo in work:
            for leg in todo:
                for d in _dates_for(leg):
                    if d not in days:
                        days.append(d)
        fetched = {}
        for d in days[:MAX_DAYS_PER_PASS]:
            if not _cfg.get("fetch_day"):
                break
            try:
                fetched[d] = _cfg["fetch_day"](d) or []
            except Exception as exc:
                print(f"History: could not load results for {d}: {exc}")

        sporty_left = SPORTY_PER_PASS

        for row, legs, todo in work:
            changed = False
            for leg in todo:
                matches = []
                for d in _dates_for(leg):
                    matches.extend(fetched.get(d, []))
                match, flipped = _find_match(leg, matches)
                if match is None:
                    continue
                res = _result_from_match(match)
                if res is None:
                    continue
                if res.get("void"):
                    leg["result"], leg["source"] = "void", "auto"
                    changed = True
                    continue
                if flipped:  # the data lists the teams the other way round
                    res = {"h": res["a"], "a": res["h"],
                           **({"hh": res["ha"], "ha": res["hh"]} if "hh" in res else {})}
                outcome = settle_leg(leg, res)
                if outcome:
                    leg["result"], leg["source"] = outcome, "auto"
                    changed = True

            # Second source: SportyBet's own page for this booking code.
            remaining = [leg for leg in todo if not leg.get("result")]
            if remaining and row["code"] and _cfg.get("fetch_code") and sporty_left > 0:
                sporty_left -= 1
                try:
                    events = _sporty_events(_cfg["fetch_code"](row["code"]))
                except Exception as exc:
                    events = []
                    print(f"History: SportyBet code page failed: {exc}")
                for leg in remaining:
                    event = _sporty_pick_event(leg, events)
                    res = _sporty_result(event, leg.get("kickoff_ts")) if event else None
                    if res is None:
                        continue
                    outcome = settle_leg(leg, res)
                    if outcome:
                        leg["result"], leg["source"] = outcome, "auto"
                        changed = True

            age = (now - float(row["last_kickoff"])) / 86400
            status = _ticket_status(legs, age)
            _save(row["id"], legs, status, now, changed)
            checked += 1
        _stats_cache["at"] = 0.0
    except Exception as exc:
        print(f"History: settle failed: {type(exc).__name__}: {exc}")
    finally:
        _settle_lock.release()
    return {"checked": checked}


def _save(ticket_id, legs, status, now, changed):
    conn = _cfg["db"]()
    try:
        settled = _now_iso() if status != "pending" else None
        if changed or status != "pending":
            conn.execute(
                "UPDATE tickets SET legs = ?, status = ?, last_check = ?, settled_at = COALESCE(settled_at, ?) WHERE id = ?",
                (json.dumps(legs, separators=(",", ":")), status, float(now), settled, int(ticket_id)),
            )
        else:
            conn.execute("UPDATE tickets SET last_check = ? WHERE id = ?", (float(now), int(ticket_id)))
        conn.commit()
    finally:
        conn.close()


def mark_leg(ticket_id, index, result):
    """Admin only: set one pick by hand ('won', 'lost', 'void' or '' to clear)."""
    if result not in ("won", "lost", "void", ""):
        raise ValueError("bad result")
    conn = _cfg["db"]()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (int(ticket_id),)).fetchone()
        if row is None:
            return None
        legs = json.loads(row["legs"])
        if not 0 <= int(index) < len(legs):
            raise ValueError("bad pick number")
        leg = legs[int(index)]
        if result:
            leg["result"], leg["source"] = result, "admin"
        else:
            leg.pop("result", None)
            leg.pop("source", None)
        age = (time.time() - float(row["last_kickoff"] or 0)) / 86400
        status = _ticket_status(legs, age)
        settled = _now_iso() if status != "pending" else None
        conn.execute(
            "UPDATE tickets SET legs = ?, status = ?, settled_at = ? WHERE id = ?",
            (json.dumps(legs, separators=(",", ":")), status, settled, int(ticket_id)),
        )
        conn.commit()
        _stats_cache["at"] = 0.0
        return status
    finally:
        conn.close()


def start_loop():
    def run():
        time.sleep(90)
        while True:
            try:
                settle_pending()
                cleanup()
            except Exception as exc:
                print(f"History loop error: {exc}")
            time.sleep(LOOP_SECONDS)

    threading.Thread(target=run, daemon=True, name="history-settle").start()


# ===============================================================
# READING
# ===============================================================

def _summary(row):
    legs = json.loads(row["legs"])
    results = [str(leg.get("result") or "") for leg in legs]
    return {
        "id": int(row["id"]),
        "created_at": row["created_at"],
        "title": row["title"],
        "subtitle": row["subtitle"],
        "total_odds": round(float(row["total_odds"] or 0), 2),
        "code": row["code"],
        "status": row["status"],
        "picks": len(legs),
        "won": results.count("won"),
        "lost": results.count("lost"),
        "first_kickoff": min([leg.get("kickoff_ts") or 0 for leg in legs] or [0]),
        "archived": bool(legs) and not any(leg.get("pick") for leg in legs),
    }


def list_tickets(user_id, status="all", limit=30, before=None):
    limit = max(1, min(int(limit or 30), 60))
    sql = "SELECT * FROM tickets WHERE user_id = ?"
    params = [int(user_id)]
    if status in ("won", "cancelled", "pending", "unverified"):
        sql += " AND status = ?"
        params.append(status)
    if before:
        sql += " AND id < ?"
        params.append(int(before))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    conn = _cfg["db"]()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets WHERE user_id = ? GROUP BY status",
            (int(user_id),),
        ).fetchall()
    finally:
        conn.close()
    tally = {r["status"]: int(r["n"]) for r in counts}
    return {"tickets": [_summary(r) for r in rows], "counts": tally}


def get_ticket(ticket_id):
    conn = _cfg["db"]()
    try:
        return conn.execute("SELECT * FROM tickets WHERE id = ?", (int(ticket_id),)).fetchone()
    finally:
        conn.close()


def stats():
    """Public record over every ticket (counts only, no personal data)."""
    if time.time() - _stats_cache["at"] < 60 and _stats_cache["value"] is not None:
        return _stats_cache["value"]
    conn = _cfg["db"]()
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status").fetchall()
    finally:
        conn.close()
    tally = {r["status"]: int(r["n"]) for r in rows}
    won = tally.get("won", 0)
    cancelled = tally.get("cancelled", 0)
    done = won + cancelled
    value = {
        "won": won,
        "cancelled": cancelled,
        "pending": tally.get("pending", 0),
        "unverified": tally.get("unverified", 0),
        "win_rate": round(100.0 * won / done, 1) if done else None,
    }
    _stats_cache.update(at=time.time(), value=value)
    return value


def render_png(row):
    """Draw the ticket picture with ticks, crosses and the stamp."""
    if _RENDER is None:
        raise RuntimeError("ticket picture is not available")
    legs = json.loads(row["legs"])
    rows = [
        {
            "time": leg.get("time", ""),
            "league": leg.get("league", ""),
            "match": leg.get("match", ""),
            "pick": leg.get("pick", ""),
            "odd": float(leg.get("odd") or 1.0),
            "prob": leg.get("prob") or 0,
            "result": leg.get("result"),
        }
        for leg in legs
    ]
    status = row["status"] if row["status"] in ("won", "cancelled") else None
    return _RENDER(
        rows,
        row["title"],
        row["subtitle"] or "",
        float(row["total_odds"] or 0),
        float(row["win_chance"] or 0),
        row["code"],
        status,
    )


# ===============================================================
# KEEP THE DATABASE SMALL
# ===============================================================

DETAIL_DAYS = 180


def cleanup():
    """After 180 days a finished ticket keeps only its odds and results (no picture)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DETAIL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _cfg["db"]()
    try:
        rows = conn.execute(
            "SELECT id, legs FROM tickets WHERE created_at < ? AND status != 'pending' "
            "AND legs != '[]' LIMIT 500",
            (cutoff,),
        ).fetchall()
        for row in rows:
            legs = json.loads(row["legs"])
            slim = [{"pick": "", "match": "", "odd": l.get("odd", 1), "result": l.get("result")} for l in legs]
            conn.execute("UPDATE tickets SET legs = ? WHERE id = ?",
                         (json.dumps(slim, separators=(",", ":")), int(row["id"])))
        if rows:
            conn.commit()
    finally:
        conn.close()
