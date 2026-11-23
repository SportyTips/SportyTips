"""Ticket message style: heading with odds and legs, code line, then picks with a flag line and a quoted reason.

    🎫 NCAA 2000 odds · Next 24 hours — 2012.73 odds, 20 legs
    📋 SportyBet code: UDHQRG — 20 games, 2012.73 odds

    📊 Picks
    1. Appalachian State vs St. Bonaventure
    🇺🇸 USA · NCAA, Regular Season · 5:00pm
    🎯 Over 127.5 points @1.45
    | SportyBet market analysis supports this selection at about 64%.

app.py calls restyle(text) on every ticket message (see _ticket_style there). Nothing else is edited.
If a message is not a ticket, or cannot be read, it is returned unchanged.

The country of each league is learnt from SportyBet while the ticket is being built (this file watches
sportybet_provider.sporty_fixture). If SportyBet gives no country, a short list of well-known league
names is used. A league that is in neither gets no flag.

Switch it off on Render with TICKET_STYLE=0.
"""

import html
import os
import re

try:
    import sportybet_provider as sp
except Exception:  # only needed to learn league countries
    sp = None

ENABLED = os.getenv("TICKET_STYLE", "1").strip().lower() in ("1", "true", "yes", "on")

# ------------------------------------------------------------
# flags
# ------------------------------------------------------------
_ISO = {
    "italy": "IT", "spain": "ES", "germany": "DE", "france": "FR", "portugal": "PT", "netherlands": "NL",
    "belgium": "BE", "turkey": "TR", "greece": "GR", "norway": "NO", "saudi arabia": "SA", "denmark": "DK",
    "usa": "US", "united states": "US", "brazil": "BR", "argentina": "AR", "mexico": "MX", "sweden": "SE",
    "poland": "PL", "austria": "AT", "switzerland": "CH", "russia": "RU", "ukraine": "UA", "japan": "JP",
    "south korea": "KR", "china": "CN", "australia": "AU", "egypt": "EG", "south africa": "ZA",
    "nigeria": "NG", "morocco": "MA", "colombia": "CO", "chile": "CL", "peru": "PE", "paraguay": "PY",
    "uruguay": "UY", "bolivia": "BO", "ecuador": "EC", "venezuela": "VE", "guatemala": "GT",
    "honduras": "HN", "costa rica": "CR", "czech republic": "CZ", "czechia": "CZ", "slovakia": "SK",
    "croatia": "HR", "serbia": "RS", "romania": "RO", "bulgaria": "BG", "hungary": "HU", "ireland": "IE",
    "finland": "FI", "iceland": "IS", "israel": "IL", "cyprus": "CY", "qatar": "QA", "uae": "AE",
    "united arab emirates": "AE", "iran": "IR", "india": "IN", "canada": "CA", "slovenia": "SI",
    "scotland": "SCT", "england": "ENG", "wales": "WLS", "northern ireland": "GB",
    "europe": "EU", "international": "WORLD", "world": "WORLD",
}

_TAG = {"ENG": "gbeng", "SCT": "gbsct", "WLS": "gbwls"}


def _flag(code):
    if code in _TAG:
        return "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in _TAG[code]) + "\U000E007F"
    if code == "WORLD":
        return "\U0001F30D"
    if code == "EU":
        return "\U0001F1EA\U0001F1FA"
    if len(code) == 2:
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)
    return ""


# league name -> country, for when SportyBet gives none (first match wins, most specific first)
_BY_LEAGUE = [
    ("brasileir", "Brazil"), ("ncaa", "USA"), ("nba", "USA"), ("mls", "USA"), ("champions league", "Europe"),
    ("europa", "Europe"), ("conference league", "Europe"), ("serie a", "Italy"), ("serie b", "Italy"),
    ("premier league", "England"), ("championship", "England"), ("league one", "England"), ("league two", "England"),
    ("premiership", "Scotland"), ("laliga", "Spain"), ("la liga", "Spain"), ("bundesliga", "Germany"),
    ("ligue 1", "France"), ("ligue 2", "France"), ("eredivisie", "Netherlands"), ("primeira", "Portugal"),
    ("liga portugal", "Portugal"), ("super lig", "Turkey"), ("jupiler", "Belgium"), ("eliteserien", "Norway"),
    ("allsvenskan", "Sweden"), ("ekstraklasa", "Poland"), ("liga mx", "Mexico"), ("liga profesional", "Argentina"),
]

LEAGUE_COUNTRY = {}  # lower-case league name -> country, learnt while tickets are built


def _country_of(league):
    key = html.unescape(league).strip().lower()
    country = LEAGUE_COUNTRY.get(key)

    if country:
        return country

    for part, name in _BY_LEAGUE:
        if part in key:
            return name

    return ""


if sp is not None and hasattr(sp, "sporty_fixture"):

    _prev_fixture = sp.sporty_fixture

    def sporty_fixture(event, *args, **kwargs):
        fixture = _prev_fixture(event, *args, **kwargs)

        try:
            league = (fixture or {}).get("league") or {}
            name = str(league.get("name") or "").strip().lower()
            country = str(league.get("country") or "").strip()

            if name and country and len(LEAGUE_COUNTRY) < 3000:
                LEAGUE_COUNTRY[name] = country
        except Exception:
            pass

        return fixture

    sp.sporty_fixture = sporty_fixture

# ------------------------------------------------------------
# the new layout
# ------------------------------------------------------------
_PICK = re.compile(
    r"<b>(\d+)\.</b> \U0001F552 (?P<time>.+?) \u2022 \U0001F3C6 (?P<league>.+?)\n"
    r"\u26BD (?P<match>.+?)\n"
    r"\U0001F3AF <b>(?P<label>.+?)</b> \u2022 \U0001F4B0 (?P<odd>[0-9.]+) \u2022 \S+ (?P<pct>\d+)%\n"
    r"\U0001F4AC (?P<reason>.+?)(?=\n\n|\n\u2501|\Z)",
    re.S,
)
_TIME = re.compile(r"(\d{1,2}:\d{2}) (AM|PM)")


def _short_time(text):
    return _TIME.sub(lambda m: m.group(1) + m.group(2).lower(), text)


def _pretty_title(raw):
    title = re.sub(r"<[^>]+>", "", raw).replace("\u2022", " ")
    title = re.sub(r"\bODDS\b", "odds", title)
    title = re.sub(r"\bPICKS\b", "picks", title)
    title = re.sub(r"\s+", " ", title).strip()

    # "12 picks" on its own says nothing about the sport: the legs count already sits in the heading
    if re.fullmatch(r"\d+ picks", title):
        title = "Football picks"

    return title


def restyle(text):

    try:

        if not ENABLED or not text or "SPORTYTIPS" not in text.split("\n", 1)[0] or "Total odds:" not in text:
            return text

        lines = text.split("\n")

        title = _pretty_title(re.sub(r"^.*?SPORTYTIPS\s*\u2022?\s*", "", lines[0], count=1))

        window = ""
        if len(lines) > 1 and lines[1].startswith("\U0001F4C5"):
            window = lines[1].replace("\U0001F4C5", "").split("\u2022")[0].strip()

        picks = list(_PICK.finditer(text))

        if not picks:
            return text

        total = re.search(r"Total odds:\s*([0-9.]+)", text)
        total = total.group(1) if total else ""

        chance = re.search(r"Estimated combined chance:\s*<b>(.+?)</b>", text)
        code = re.search(r"SportyBet code:\s*<b>([A-Za-z0-9]+)</b>", text)

        count = len(picks)
        head = f"\U0001F3AB <b>{title}" + (f" \u00b7 {html.escape(window, quote=False)}" if window else "") + "</b>"
        head += f" \u2014 {total} odds, {count} leg" + ("" if count == 1 else "s")

        out = [head]

        if code:
            out.append(f"\U0001F4CB SportyBet code: <b>{code.group(1)}</b> \u2014 {count} game" + ("" if count == 1 else "s") + f", {total} odds")

        out += ["", "\U0001F4CA <b>Picks</b>"]

        for m in picks:

            league = m.group("league")
            country = _country_of(league)
            flag = _flag(_ISO.get(country.lower(), "")) if country else ""

            place = " \u00b7 ".join(x for x in (country, league) if x)
            place = (flag + " " if flag else "") + place

            odd = f"{float(m.group('odd')):g}"

            out += [
                f"<b>{m.group(1)}. {m.group('match')}</b>",
                f"{place} \u00b7 <b>{_short_time(m.group('time'))}</b>",
                f"\U0001F3AF <b>{m.group('label')}</b> @{odd}",
                "> " + " ".join(m.group("reason").split()),
                "",
            ]

        if chance:
            out.append(f"\U0001F4C8 Chance that all picks win: {chance.group(1)}")

        # warnings and notes the builder added (could not reach the odds, football stats, swaps ...)
        skip = ("\U0001F4B0 <b>Total odds", "\U0001F4CA Estimated", "\U0001F9E9 Mix", "\U0001F4F2 SportyBet code")
        tail = text[text.index("\u2501"):].split("\n")[1:]

        for line in tail:
            if line.strip() and not line.startswith(skip):
                out.append(line)

        return "\n".join(out).strip()

    except Exception as exc:
        print(f"Ticket style: kept the old layout ({type(exc).__name__}: {exc})")
        return text
