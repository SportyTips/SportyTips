#!/usr/bin/env python3
"""Applies the new SportyTips welcome screen to index.html.

Run it from the folder that holds index.html:

    python patch_ui.py

What it changes:
  - the four buttons leave the message area and become cards on an empty welcome screen
  - after the first message, the same four options become a chip row above the input
  - "Daily2odds" is shown as "Daily 2 odds" (the text sent to the bot is unchanged)

A backup is saved as index.before-ui.html. Nothing in app.py needs to change.
"""

import os
import re
import shutil
import sys

FILE = sys.argv[1] if len(sys.argv) > 1 else "index.html"

NEW_CSS = r'''
/* ============ WELCOME SCREEN + CHIP ROW ============ */

.em {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 4px 0 8px;
}

.em-hero { text-align: center; margin: 8px 0 22px; }

.em-hero .logo {
  width: 52px;
  height: 52px;
  border-radius: 15px;
  font-size: 28px;
  margin: 0 auto 14px;
}

.em-hero h2 {
  margin: 0 0 6px;
  font-size: 24px;
  line-height: 1.15;
  letter-spacing: -.01em;
  font-weight: 800;
}

.em-hero p { margin: 0; color: var(--gray); font-size: 16px; }

.em-cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.em-card {
  text-align: left;
  font-family: inherit;
  color: var(--ink);
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 14px;
  cursor: pointer;
  transition: border-color .15s, transform .1s;
}

.em-card:active { transform: scale(.98); border-color: var(--red); }
.em-card:focus-visible { outline: 2px solid var(--red); outline-offset: 2px; }

.em-card svg {
  width: 24px;
  height: 24px;
  display: block;
  margin-bottom: 10px;
  stroke: var(--red);
  fill: none;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.em-card b { display: block; font-size: 16px; line-height: 1.2; }
.em-card span { display: block; color: var(--gray); font-size: 13px; margin-top: 3px; }

.qrow {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 0 2px 10px;
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
}

.qrow::-webkit-scrollbar { display: none; }

.qchip {
  flex: none;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--ink);
  font-family: inherit;
  font-weight: 600;
  font-size: 14px;
  padding: 8px 14px;
  border-radius: 999px;
  cursor: pointer;
  white-space: nowrap;
}

.qchip:active { background: var(--red); border-color: var(--red); color: #fff; }
'''

NEW_JS = r'''const OPTIONS = [
  {
    label: "Safe 5 picks tomorrow",
    send: "Safe 5 picks tomorrow",
    note: "Five low-risk picks",
    icon: '<path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/><polyline points="8.5 12 11 14.5 15.5 9.5"/>'
  },
  {
    label: "Straight win tickets",
    send: "Straight win today",
    note: "Favourites to win",
    icon: '<path d="M8 4h8v5a4 4 0 0 1-8 0z"/><path d="M8 6H5v1a3 3 0 0 0 3 3"/><path d="M16 6h3v1a3 3 0 0 1-3 3"/><path d="M12 13v7"/><path d="M8.5 20h7"/>'
  },
  {
    label: "Today's bankers",
    send: "Safe 5 picks today",
    note: "The surest games today",
    icon: '<polygon points="12 3 14.8 9 21 9.7 16.4 14 17.7 20.5 12 17.3 6.3 20.5 7.6 14 3 9.7 9.2 9"/>'
  },
  {
    label: "Daily 2 odds",
    send: "Daily2odds",
    note: "One very sure ticket",
    icon: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/>'
  }
];

/* Welcome screen: cards, shown until the first message. */

const empty = document.createElement("div");
empty.className = "em";

empty.innerHTML =
  '<div class="em-hero"><div class="logo">S</div>' +
  "<h2>What should we build today?</h2>" +
  "<p>Pick one, or type your own request.</p></div>" +
  '<div class="em-cards"></div>';

box.appendChild(empty);

OPTIONS.forEach((o) => {
  const b = document.createElement("button");

  b.type = "button";
  b.className = "em-card";

  b.innerHTML =
    '<svg viewBox="0 0 24 24" aria-hidden="true">' + o.icon + "</svg>" +
    "<b>" + o.label + "</b><span>" + o.note + "</span>";

  b.onclick = () => send(o.send);

  empty.querySelector(".em-cards").appendChild(b);
});

/* Chip row: same options, above the input, after the first message. */

const qrow = $("qrow");

OPTIONS.forEach((o) => {
  const c = document.createElement("button");

  c.type = "button";
  c.className = "qchip";
  c.textContent = o.label;
  c.onclick = () => send(o.send);

  qrow.appendChild(c);
});

function dismissEmpty() {
  if (empty.parentNode) {
    empty.remove();
  }

  qrow.hidden = false;
}'''


def main():
    if not os.path.exists(FILE):
        print("Could not find " + FILE + ". Run this from the folder that holds index.html.")
        sys.exit(1)

    with open(FILE, encoding="utf-8") as fh:
        s = fh.read()

    if "em-cards" in s:
        print("This file already has the new welcome screen. Nothing to do.")
        return

    problems = []

    # 1. Old chip styles (optional clean-up).
    s, n = re.subn(
        r"\.chips \{[^}]*\}\s*\.chip \{[^}]*\}\s*\.chip:active \{[^}]*\}",
        "",
        s,
        count=1,
    )
    print(("OK    " if n else "SKIP  ") + "1. old chip styles removed" + ("" if n else " (not found, harmless)"))

    # 2. New styles.
    i = s.find("</style>")
    if i < 0:
        problems.append("2. could not find </style>")
    else:
        s = s[:i] + NEW_CSS + "\n" + s[i:]
        print("OK    2. new styles added")

    # 3. Chip row slot inside the composer.
    marker = '<div class="composer">'
    if s.count(marker) != 1:
        problems.append('3. could not find <div class="composer"> exactly once')
    else:
        s = s.replace(marker, marker + '\n    <div class="qrow" id="qrow" hidden></div>', 1)
        print("OK    3. chip row slot added above the input")

    # 4. Replace the old welcome message and its chips.
    start_marker = 'const welcome = document.createElement("div");'
    end_marker = "box.appendChild(welcome);"
    a = s.find(start_marker)
    b = s.find(end_marker)

    if a < 0 or b < 0 or b < a:
        problems.append("4. could not find the old welcome block")
    else:
        s = s[:a] + NEW_JS + s[b + len(end_marker):]
        print("OK    4. welcome block replaced with cards and chip row")

    # 5. The ticket fixer now lives on the welcome screen.
    if s.count("welcome.appendChild(fixer);") != 1:
        problems.append("5. could not find welcome.appendChild(fixer);")
    else:
        s = s.replace("welcome.appendChild(fixer);", "empty.appendChild(fixer);", 1)
        print("OK    5. ticket fixer moved onto the welcome screen")

    # 6. Hide the welcome screen on the first message.
    if s.count("busy = true;") != 1:
        problems.append("6. could not find busy = true; exactly once")
    else:
        s = s.replace("busy = true;", "busy = true;\n  dismissEmpty();", 1)
        print("OK    6. welcome screen hides on first message")

    if problems:
        print("\nNothing was changed, because these steps failed:")
        for p in problems:
            print("  - " + p)
        print("Send me the error and I will fix the script.")
        sys.exit(1)

    shutil.copyfile(FILE, "index.before-ui.html")

    with open(FILE, "w", encoding="utf-8") as fh:
        fh.write(s)

    print("\nDone. Backup saved as index.before-ui.html")


main()
