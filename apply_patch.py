#!/usr/bin/env python3
"""
SportyTips format patch.

Put this file in the folder that holds app.py, smart_ticket.py and index.html,
then run:   python apply_patch.py

It makes a .bak copy of each file first, applies the edits, checks that the
Python files still compile, and rolls back by itself if anything fails.
Running it twice is safe.
"""
import py_compile
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = "SPORTYTIPS-FORMAT-V2"


# ---------------------------------------------------------------- helpers
def read(name):
    path = HERE / name
    if not path.exists():
        return path, None
    return path, path.read_text(encoding="utf-8").replace("\r\n", "\n")


def backup(path):
    bak = path.with_name(path.name + ".bak")
    if not bak.exists():
        shutil.copy2(path, bak)
    return bak


def once(src, old, new, label):
    n = src.count(old)
    if n != 1:
        raise RuntimeError(f"{label}: expected 1 match, found {n}")
    return src.replace(old, new, 1)


# ------------------------------------------------- smart_ticket.py pieces
HELPERS = r'''# SPORTYTIPS-FORMAT-V2
_ISO = {
    "Switzerland": "CH", "Poland": "PL", "Belgium": "BE", "Germany": "DE", "Spain": "ES",
    "Italy": "IT", "France": "FR", "Netherlands": "NL", "Portugal": "PT", "Turkey": "TR",
    "Austria": "AT", "Greece": "GR", "Denmark": "DK", "Sweden": "SE", "Norway": "NO",
    "Finland": "FI", "Ireland": "IE", "Iceland": "IS", "Russia": "RU", "Ukraine": "UA",
    "Czech Republic": "CZ", "Czechia": "CZ", "Slovakia": "SK", "Slovenia": "SI", "Croatia": "HR",
    "Serbia": "RS", "Romania": "RO", "Bulgaria": "BG", "Hungary": "HU", "Cyprus": "CY",
    "Israel": "IL", "Azerbaijan": "AZ", "Kazakhstan": "KZ", "Uzbekistan": "UZ", "Georgia": "GE",
    "Armenia": "AM", "Albania": "AL", "Latvia": "LV", "Lithuania": "LT", "Estonia": "EE",
    "Bosnia & Herzegovina": "BA", "North Macedonia": "MK", "Montenegro": "ME", "Luxembourg": "LU",
    "Brazil": "BR", "Argentina": "AR", "Colombia": "CO", "Chile": "CL", "Uruguay": "UY",
    "Paraguay": "PY", "Peru": "PE", "Ecuador": "EC", "Mexico": "MX", "USA": "US",
    "Canada": "CA", "Japan": "JP", "South Korea": "KR", "China": "CN", "Australia": "AU",
    "Saudi Arabia": "SA", "Qatar": "QA", "UAE": "AE", "Nigeria": "NG", "Ghana": "GH",
    "Egypt": "EG", "Morocco": "MA", "Algeria": "DZ", "Tunisia": "TN", "South Africa": "ZA",
    "Kenya": "KE", "Cameroon": "CM", "Senegal": "SN",
}
_TAG_FLAGS = {"England": "gbeng", "Scotland": "gbsct", "Wales": "gbwls"}


def _flag(country):
    name = str(country or "").strip()
    tag = _TAG_FLAGS.get(name)
    if tag:
        return "\U0001F3F4" + "".join(chr(0xE0000 + ord(ch)) for ch in tag) + "\U000E007F"
    code = _ISO.get(name)
    if not code:
        return "\U0001F30D"
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in code)


def _country_of(event, league_data):
    name = str((league_data or {}).get("country") or "").strip()
    if name:
        return name
    try:
        return str(((event.get("sport") or {}).get("category") or {}).get("name") or "").strip()
    except Exception:
        return ""


'''

NEW_BLOCK = r'''code_txt = html.escape(str(code)) if code else "pending"
lines = [f"{E_TICKET} <b>SPORTYTIPS {DOT} {html.escape(title)}</b>",
         f"\U0001F4CB Ticket <b>{code_txt}</b> \u2014 SportyBet \u2014 {len(chosen)} game{'s' if len(chosen) != 1 else ''}, {total_odds:.2f} odds.",
         f"{E_CAL} {html.escape(req['label'].capitalize())} {DOT} times in {bot.LOCAL_TZ_NAME}",
         "", f"{E_CHART} <b>Picks:</b>"]
for number, candidate in enumerate(chosen, start=1):
    why = candidate["reason"]
    country = str(candidate.get("country") or "").strip()
    place = (html.escape(country) + " \u2014 " if country else "") + html.escape(candidate["league"])
    lines.append("")
    lines.append(f"<b>{number}. {html.escape(candidate['home'])} vs {html.escape(candidate['away'])}</b>")
    lines.append(f"{_flag(country)} {place} \u00b7 <b>{html.escape(_time_text(candidate, today))}</b>")
    lines.append(f"{E_TIPS} <b>{html.escape(candidate['label'])}</b> @{candidate['odd']:.2f}")
    lines.append(f"<blockquote>{html.escape(why)}</blockquote>")'''


def patch_smart(src):
    if MARK in src:
        if "_rich_reason(candidate) or " in src:
            src = src.replace('_rich_reason(candidate) or candidate["reason"]', 'candidate["reason"]')
            return src, "smart_ticket.py: updated to use the new reasons"
        return src, "smart_ticket.py: already patched"

    # 1. country on every candidate
    old = '"league": league_data.get("name", "Football"),'
    src = once(src, old, old + ' "country": _country_of(event, league_data),', "country key")

    # 2. helpers, placed before _reason_for
    m = list(re.finditer(r"^def _reason_for\(candidate\):", src, re.M))
    if len(m) != 1:
        raise RuntimeError("could not find def _reason_for(candidate)")
    src = src[: m[0].start()] + HELPERS + src[m[0].start():]

    # 3. honest fallback when no football data exists for a match
    old = """return f"Stats based on recent games for {candidate['home']} and {candidate['away']}.\""""
    new = "return 'Based on the SportyBet odds for this match.'"
    src = once(src, old, new, "fallback reason")

    # 4. new ticket layout
    rows = src.split("\n")
    start = next((i for i, l in enumerate(rows) if 'lines = [f"{E_TICKET} <b>SPORTYTIPS' in l), None)
    if start is None:
        raise RuntimeError("could not find the ticket header line in _flow_main")
    end = next((i for i in range(start + 1, len(rows)) if rows[i].lstrip().startswith('lines += ["", LINE,')), None)
    if end is None:
        raise RuntimeError("could not find the 'lines += [\"\", LINE' line")
    indent = re.match(r"\s*", rows[start]).group(0)
    rows[start:end] = [(indent + l) if l.strip() else "" for l in NEW_BLOCK.split("\n")]
    src = "\n".join(rows)

    # 5. refresh the cached Daily 2 odds so it uses the new layout
    src, n = re.subn(r"^DAILY_VERSION = 2\b", "DAILY_VERSION = 3", src, flags=re.M)
    note = "smart_ticket.py: patched" + ("" if n else " (DAILY_VERSION line not found, bump it to 3 by hand)")
    return src, note


# ------------------------------------------------------------ index.html
NEW_MD = r'''function md(text) {
  const lines = esc(text).split("\n");
  let out = "";
  let inList = false;
  for (const line of lines) {
    const quote = line.match(/^\s*&gt;\s?(.*)$/);
    if (quote) {
      if (inList) { out += "</ul>"; inList = false; }
      out += "<blockquote>" + quote[1] + "</blockquote>";
      continue;
    }
    const bullet = line.match(/^\s*[-\u2022]\s+(.*)$/);
    if (bullet) {
      if (!inList) { out += "<ul>"; inList = true; }
      out += "<li>" + bullet[1] + "</li>";
      continue;
    }
    if (inList) { out += "</ul>"; inList = false; }
    out += line + "<br>";
  }
  if (inList) out += "</ul>";
  return out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/(<br>)+$/, "");
}

'''

CSS_QUOTE = (
    ".content strong { font-weight: 700; }\n"
    ".content blockquote { margin: 6px 0 12px; padding: 9px 14px; border-left: 4px solid var(--line); "
    "background: var(--bar); border-radius: 0 12px 12px 0; color: var(--gray); font-size: 16px; line-height: 1.45; }"
)


def patch_html(src):
    if "<blockquote>" in src and "&gt;\\s?" in src:
        return src, "index.html: already patched"

    # blockquote survives the tag stripping in addItem
    old = '.replace(/<\\/?b>/g, "**")'
    new = '.replace(/<blockquote>([\\s\\S]*?)<\\/blockquote>/g, "> $1")\n' + old
    src = once(src, old, new, "addItem blockquote")

    # md() renders "> text" lines as a quote box
    a = src.find("function md(text) {")
    b = src.find("function clock() {")
    if a < 0 or b < 0 or b < a:
        raise RuntimeError("could not find function md / function clock")
    src = src[:a] + NEW_MD + src[b:]

    # styling
    src = once(src, ".content strong { font-weight: 700; }", CSS_QUOTE, "blockquote css")
    return src, "index.html: patched"


# ---------------------------------------------------------------- app.py
NEW_DEBUG = r'''@app.route('/debug-sportapi/<int:team_id>')
@login_required
def debug_sportapi(team_id):
    # Admin only. Never prints any part of the API key.
    if g.user["identifier"] not in ADMIN_EMAILS:
        abort(404)
    import sportapi_data as sd
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://sportapi.ai/api/teams/{team_id}",
            headers={"X-Api-Key": sd.API_KEY, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return Response(resp.read().decode("utf-8"), mimetype="application/json")
    except Exception as exc:
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 502
'''


def patch_app(src):
    if "# SPORTYTIPS-DEBUG-LOCKED" in src:
        return src, "app.py: already patched"
    m = list(re.finditer(r"^@app\.route\(['\"]/debug-sportapi/<int:team_id>['\"]\)", src, re.M))
    if len(m) != 1:
        return src, "app.py: debug route not found, left unchanged"
    src = src[: m[0].start()] + "# SPORTYTIPS-DEBUG-LOCKED\n" + NEW_DEBUG
    return src, "app.py: debug route is now admin only"


# ------------------------------------------------------------------ main
def main():
    jobs = [("smart_ticket.py", patch_smart, True), ("index.html", patch_html, False), ("app.py", patch_app, True)]
    done = []
    try:
        for name, fn, is_py in jobs:
            path, src = read(name)
            if src is None:
                print(f"SKIPPED {name}: not found next to this script")
                continue
            new_src, note = fn(src)
            if new_src != src:
                bak = backup(path)
                path.write_text(new_src, encoding="utf-8", newline="\n")
                done.append((path, bak))
                if is_py:
                    py_compile.compile(str(path), doraise=True)
            print("OK  " + note)
    except Exception as exc:
        print(f"\nFAILED: {exc}\nRolling back...")
        for path, bak in done:
            shutil.copy2(bak, path)
        sys.exit(1)
    print("\nDone. Backups are saved as *.bak. Restart your server and test.")


if __name__ == "__main__":
    main()