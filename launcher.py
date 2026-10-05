"""Launcher for SportyTips. Does NOT edit main.py.

Loads the bot, applies SportyBet-first patches, and exposes send_photo()
so the web frontend can stream ticket images to the browser.

Put this file next to main.py, sportybet_provider.py, ticket_image_lite.py
and smart_ticket.py.
"""

import json
import re
import uuid
import importlib
from urllib.request import Request, urlopen

BOT_MODULE = "main"
LAUNCHER_MODULE = "launcher"

bot = importlib.import_module(BOT_MODULE)
from html import escape

CODE_RE = re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{6,8}\b")


# ---- variety: stop the ticket being all "Over 1.5" ----
MAX_PER_KIND = {"goals": 2, "btts": 3, "dc": 4}


def diverse_select(options, target_odds, picks, overshoot):
    options = sorted(options, key=lambda o: o["prob"], reverse=True)
    counts, pool = {}, []
    for option in options:
        kind = option["kind"]
        limit = MAX_PER_KIND.get(kind, 99)
        if counts.get(kind, 0) >= limit:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        pool.append(option)
    ticket = bot.select_ticket(pool, target_odds, picks, overshoot) \
        if hasattr(bot, "select_ticket") else None
    if ticket is None or (target_odds and not ticket.get("reached")):
        fallback = bot.select_ticket(options, target_odds, picks, overshoot) \
            if hasattr(bot, "select_ticket") else None
        if fallback is not None:
            return fallback
    return ticket


# ---- ticket picture ----
def send_photo(chat_id, png, caption=""):
    """Send a PNG to Telegram. When running under the web frontend, app.py
    replaces this with a base64 streamer."""
    boundary = "----sportytips" + uuid.uuid4().hex
    body = b""
    for name, value in (("chat_id", str(chat_id)), ("caption", caption)):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
             f'filename="ticket.png"\r\nContent-Type: image/png\r\n\r\n').encode("utf-8")
    body += png + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{bot.BOT_TOKEN}/sendPhoto",
        data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urlopen(request, timeout=60) as response:
        json.loads(response.read().decode("utf-8"))


def send_ticket_image(chat_id, req, ticket):
    try:
        from ticket_image import make_ticket_image
    except ImportError:
        from ticket_image_lite import make_ticket_image

    rows = []
    for option in ticket["picks"]:
        fixture = option["fixture"]
        kickoff = bot.parse_fixture_time(fixture).astimezone(bot.LOCAL_TZ)
        teams = fixture.get("teams", {})
        rows.append({
            "time": kickoff.strftime("%a %H:%M"),
            "league": bot.league_name_of(fixture),
            "match": f"{teams['home']['name']} vs {teams['away']['name']}",
            "pick": option["label"],
            "odd": option["odd"],
            "prob": option["prob"],
        })
    mode = {"safe": "  |  safe mode", "risky": "  |  risky mode"}.get(req.get("risk"), "")
    png = make_ticket_image(
        rows, "SPORTYTIPS",
        f"{req['label'].capitalize()}{mode}  |  times in {bot.LOCAL_TZ_NAME}",
        ticket["total_odds"], ticket["win_probability"], ticket.get("booking_code"),
    )
    code = ticket.get("booking_code")
    send_photo(chat_id, png, f"SportyBet code: {code}" if code else "")


def sporty_first_flow(chat_id, req):
    """Legacy SportyBet-first flow. Retained as a fallback so imports do not
    break, but the real ticket building is handled by smart_ticket.flow()."""
    from sportybet_provider import sporty_fixture, sporty_odds_shape

    profile = bot.RISK_PROFILES.get(req["risk"], bot.RISK_PROFILES["normal"])
    markets = profile["markets"] or bot.ENABLED_MARKETS

    bot.send_message(
        chat_id,
        f"🔎 <b>{bot.BRAND}</b>\n\n<b>Request understood:</b>\n"
        f"{bot.describe_request(req)}\n\n⏳ Reading SportyBet's matches and odds...",
    )

    analysis = {"options": [], "checked": 0, "with_odds": 0, "no_odds": 0,
                "errors": [], "quota_stop": False, "dropped": 0}
    try:
        events = bot.SPORTYBET_PROVIDER.get_upcoming(req["start"], req["end"])
    except Exception as exc:
        bot.send_message(chat_id, f"❌ Could not read SportyBet:\n{escape(str(exc))}")
        return

    for event in events:
        fixture = sporty_fixture(event)
        odds = sporty_odds_shape(event)
        if not odds:
            analysis["no_odds"] += 1
            continue
        analysis["with_odds"] += 1
        for option in bot.build_options(fixture, odds, None,
                                        min_prob=profile["min_prob"],
                                        markets=markets,
                                        min_odds=profile["min_odds"]):
            option["sporty_event"] = event
            analysis["options"].append(option)

    total = analysis["with_odds"] + analysis["no_odds"]
    ticket = diverse_select(analysis["options"], req["target_odds"],
                            req["picks"], profile["overshoot"])

    if ticket:
        try:
            selections = [(o["sporty_event"], o["spec"]) for o in ticket["picks"]]
            ticket["booking_code"] = bot.SPORTYBET_PROVIDER.create_booking_code(selections)
        except Exception as exc:
            analysis["errors"].append(f"Booking code failed: {exc}")

    bot.send_message(chat_id, bot.format_ticket(req, ticket, analysis, total)
                     if hasattr(bot, "format_ticket") else "Ticket built.")

    if ticket:
        try:
            send_ticket_image(chat_id, req, ticket)
        except ImportError:
            bot.send_message(chat_id, "ℹ️ Install Pillow for ticket pictures.")
        except Exception as exc:
            print(f"Ticket image failed: {exc}")


def safer_code_flow(chat_id, code):
    r = bot.SPORTYBET_PROVIDER.make_safer(code.upper())
    lines = [f"🛡️ <b>Safer version of {escape(code.upper())}</b>", ""]
    for row in r["rows"]:
        lines.append(f"⚽ {escape(row['match'])}")
        if row["changed"]:
            lines.append(f"🔁 {escape(row['old'])} ({row['old_odd']:.2f}) → "
                         f"<b>{escape(row['new'])}</b> ({row['new_odd']:.2f})")
        else:
            lines.append(f"✅ Kept {escape(row['old'])} ({row['old_odd']:.2f})")
        lines.append("")
    lines.append(f"💰 Odds: {r['old_total']:.2f} → <b>{r['new_total']:.2f}</b>")
    if r["new_code"]:
        lines.append(f"📲 New code: <b>{escape(r['new_code'])}</b>")
    else:
        lines.append("Every pick was already on its safest option.")
    bot.send_message(chat_id, "\n".join(lines))


# ---- plug into the bot (only as a fallback) ----
_original_flow = bot.prediction_ticket_flow
_original_handle = bot.handle_text


def new_ticket_flow(chat_id, text):
    if bot.SPORTYBET_PROVIDER is not None:
        return sporty_first_flow(chat_id, bot.parse_request(text))
    return _original_flow(chat_id, text)


def new_handle_text(chat_id, text):
    message = text.strip()
    if bot.SPORTYBET_PROVIDER is not None and not message.startswith("/") \
            and len(message.split()) <= 4:
        found = CODE_RE.search(message)
        if found:
            try:
                bot.telegram_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                safer_code_flow(chat_id, found.group(0))
            except Exception as exc:
                bot.send_message(chat_id, f"❌ Could not read that code:\n{escape(str(exc))}")
            return
    return _original_handle(chat_id, text)


# NOTE: these patches are NOT applied automatically.
# smart_ticket.flow() is the active ticket builder, imported by upgrades.py.
# Keeping the originals here lets you re-enable the old launcher flow manually
# by uncommenting the two lines below if you ever need it.
# bot.prediction_ticket_flow = new_ticket_flow
# bot.handle_text = new_handle_text


if __name__ == "__main__":
    bot.main()