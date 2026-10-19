"""Draws the ticket picture.
Style: navy and red header, white card, odds pills (like the SportyTips site).

Best quality: needs Pillow (add `pillow` to requirements.txt) and the font file
ticket_font.ttf next to this file. If either is missing it still works, but with
the old blocky font.
"""

import io
import math
import os
import struct
import unicodedata
import zlib

WATERMARK = "SPORTYTIPS"
WATERMARK_ENABLED = False
WATERMARK_ALPHA = 38

W = 1000
PAD = 40
ROW_H = 110
HEADER_H = 250

NAVY = (4, 17, 31)
RED = (255, 31, 31)
DARK_RED = (122, 10, 20)
WHITE = (255, 255, 255)
INK = (11, 18, 32)
GRAY = (88, 98, 112)
MUTED = (138, 148, 163)
LINE = (226, 230, 236)
BAR = (243, 245, 248)
GREEN = (22, 163, 74)
LOSS = (220, 38, 38)
SLATE = (100, 112, 130)

RED_X0 = int(W * 0.60)
RED_SLOPE = 0.08
STRIPE_OFFSET = 70
STRIPE_WIDTH = 90

_FONT_SRC = {
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110",
    "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 11001 10101 10011 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 11011 10001",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11110 00001 00001 01110 00001 00001 11110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    ".": "00000 00000 00000 00000 00000 01100 01100",
    ",": "00000 00000 00000 00000 01100 00100 01000",
    ":": "00000 01100 01100 00000 01100 01100 00000",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    "+": "00000 00100 00100 11111 00100 00100 00000",
    "/": "00001 00010 00010 00100 01000 01000 10000",
    "%": "11001 11010 00010 00100 01000 01011 10011",
    "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000",
    "'": "00100 00100 01000 00000 00000 00000 00000",
    "!": "00100 00100 00100 00100 00100 00000 00100",
    "~": "00000 00000 01000 10101 00010 00000 00000",
    "|": "00100 00100 00100 00100 00100 00100 00100",
    "&": "01100 10010 10100 01000 10101 10010 01101",
    "?": "01110 10001 00001 00010 00100 00000 00100",
    " ": "00000 00000 00000 00000 00000 00000 00000",
}
FONT = {ch: src.split() for ch, src in _FONT_SRC.items()}


def ticket_status(rows, status=None):
    """"won" / "cancelled" / None (still pending).

    Each row may carry "result": "won", "lost", "void" or missing (pending).
    One lost pick cancels the ticket straight away. All picks won = won.
    Void picks (postponed games) are ignored when every other pick has won.
    """
    if status in ("won", "cancelled"):
        return status
    results = [str(r.get("result") or "").lower() for r in rows]
    if "lost" in results:
        return "cancelled"
    live = [x for x in results if x != "void"]
    if live and all(x == "won" for x in live):
        return "won"
    return None


def clean(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return "".join(c if c in FONT else "?" for c in text.upper())


def text_width(text, scale):
    return max(0, len(text) * 6 * scale - scale)


def fit(text, scale, max_width, min_scale=3):
    text = clean(text)
    s = scale
    while s > min_scale and text_width(text, s) > max_width:
        s -= 1
    while len(text) > 3 and text_width(text, s) > max_width:
        text = text[:-4] + "..."
    return text, s


class Canvas:
    def __init__(self, width, height, color):
        self.w, self.h = width, height
        self.rows = [bytearray(bytes(color) * width) for _ in range(height)]

    def rect(self, x0, y0, x1, y1, color):
        x0, x1 = max(0, x0), min(self.w, x1)
        if x1 <= x0:
            return
        chunk = bytes(color) * (x1 - x0)
        for y in range(max(0, y0), min(self.h, y1)):
            self.rows[y][x0 * 3:x1 * 3] = chunk

    def rrect(self, x0, y0, x1, y1, r, color):
        r = max(0, min(r, (x1 - x0) // 2, (y1 - y0) // 2))
        chunk_color = bytes(color)
        for y in range(max(0, y0), min(self.h, y1)):
            if y < y0 + r:
                d = (y0 + r) - (y + 0.5)
            elif y >= y1 - r:
                d = (y + 0.5) - (y1 - r)
            else:
                d = 0
            inset = 0 if d <= 0 else int(round(r - math.sqrt(max(0.0, r * r - d * d))))
            xs, xe = max(0, x0 + inset), min(self.w, x1 - inset)
            if xe > xs:
                self.rows[y][xs * 3:xe * 3] = chunk_color * (xe - xs)

    def circle(self, cx, cy, r, color):
        self.rrect(cx - r, cy - r, cx + r, cy + r, r, color)

    def thick_line(self, x0, y0, x1, y1, w, color):
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        half = w // 2
        for i in range(steps + 1):
            x = x0 + (x1 - x0) * i // steps
            y = y0 + (y1 - y0) * i // steps
            self.rect(x - half, y - half, x - half + w, y - half + w, color)

    def text(self, x, y, text, scale, color, anchor="l", bold=False):
        text = clean(text)
        if anchor == "r":
            x -= text_width(text, scale)
        elif anchor == "m":
            x -= text_width(text, scale) // 2
        for shift in ((0, 1) if bold else (0,)):
            for index, ch in enumerate(text):
                glyph = FONT[ch]
                gx = x + shift + index * 6 * scale
                for gy, bits in enumerate(glyph):
                    for bx, bit in enumerate(bits):
                        if bit == "1":
                            px, py = gx + bx * scale, y + gy * scale
                            self.rect(px, py, px + scale, py + scale, color)

    def blend_rect(self, x0, y0, x1, y1, color, alpha):
        x0, x1 = max(0, x0), min(self.w, x1)
        for y in range(max(0, y0), min(self.h, y1)):
            row = self.rows[y]
            for x in range(x0, x1):
                i = x * 3
                row[i] += (color[0] - row[i]) * alpha // 255
                row[i + 1] += (color[1] - row[i + 1]) * alpha // 255
                row[i + 2] += (color[2] - row[i + 2]) * alpha // 255

    def slanted_text(self, x, y, text, scale, color, alpha, slope=0.5):
        for index, ch in enumerate(clean(text)):
            glyph = FONT[ch]
            gx = x + index * 6 * scale
            for gy, bits in enumerate(glyph):
                for bx, bit in enumerate(bits):
                    if bit == "1":
                        px = gx + bx * scale
                        py = y + gy * scale - int((px - x) * slope)
                        self.blend_rect(px, py, px + scale, py + scale, color, alpha)

    def png(self):
        raw = b"".join(b"\x00" + bytes(row) for row in self.rows)

        def chunk(kind, data):
            body = kind + data
            return (struct.pack(">I", len(data)) + body
                    + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6))
                + chunk(b"IEND", b""))


def _make_bitmap_image(rows, title, subtitle, total_odds, win_chance, code=None, status=None):
    status = ticket_status(rows, status)
    card_x0, card_x1 = PAD, W - PAD
    card_y0 = HEADER_H
    bar_h = 90
    card_h = 14 + ROW_H * len(rows) + 16 + bar_h + (76 if code else 0) + 26
    height = card_y0 + card_h + 130
    c = Canvas(W, height, NAVY)

    for y in range(height):
        xr = int(RED_X0 + y * RED_SLOPE)
        c.rect(xr, y, W, y + 1, RED)
        c.rect(xr + STRIPE_OFFSET, y, xr + STRIPE_OFFSET + STRIPE_WIDTH, y + 1, DARK_RED)

    c.rrect(PAD, 40, PAD + 64, 104, 16, RED)
    c.text(PAD + 32, 56, "S", 6, WHITE, anchor="m", bold=True)
    c.text(PAD + 84, 62, "SPORTYTIPS", 4, WHITE, bold=True)

    sub, sub_scale = fit(subtitle, 3, 400, min_scale=2)
    pill_w = text_width(sub, sub_scale) + 56
    c.rrect(W - PAD - pill_w, 46, W - PAD, 100, 27, WHITE)
    c.text(W - PAD - pill_w // 2, 73 - (7 * sub_scale) // 2, sub, sub_scale, INK,
           anchor="m", bold=True)

    head, head_scale = fit(title, 7, W - 2 * PAD, min_scale=4)
    c.text(PAD, 140, head, head_scale, WHITE, bold=True)
    c.rect(PAD, 140 + 7 * head_scale + 18, PAD + int(text_width(head, head_scale) * 0.6),
           140 + 7 * head_scale + 24, RED)

    c.rrect(card_x0, card_y0, card_x1, card_y0 + card_h, 40, WHITE)

    y = card_y0 + 14
    text_x = card_x0 + 82
    pill_w = 176
    pill_x1 = card_x1 - 28
    for index, row in enumerate(rows):
        result = str(row.get("result") or "").lower()
        cx, cy = card_x0 + 44, y + 38
        if result == "won":
            c.circle(cx, cy, 20, GREEN)
            c.thick_line(cx - 9, cy, cx - 3, cy + 7, 5, WHITE)
            c.thick_line(cx - 3, cy + 7, cx + 10, cy - 8, 5, WHITE)
        elif result == "lost":
            c.circle(cx, cy, 20, LOSS)
            c.thick_line(cx - 8, cy - 8, cx + 8, cy + 8, 5, WHITE)
            c.thick_line(cx - 8, cy + 8, cx + 8, cy - 8, 5, WHITE)
        elif result == "void":
            c.circle(cx, cy, 20, SLATE)
            c.rect(cx - 9, cy - 2, cx + 9, cy + 3, WHITE)
        else:
            c.circle(cx, cy, 13, GRAY)
            c.circle(cx, cy, 10, WHITE)
            c.circle(cx, cy, 6, INK)

        max_w = pill_x1 - pill_w - 24 - text_x
        line, s = fit(row["match"], 4, max_w, min_scale=2)
        c.text(text_x, y + 14, line, s, INK, bold=True)
        line, s = fit(row["pick"], 3, max_w, min_scale=2)
        c.text(text_x, y + 14 + 7 * 4 + 14, line, s, GRAY, bold=True)

        pill_color = GREEN if result == "won" else SLATE if result in ("lost", "void") else NAVY
        c.rrect(pill_x1 - pill_w, y + 14, pill_x1, y + 62, 24, pill_color)
        odd, odd_scale = fit(f"{row['odd']:.2f}", 5, pill_w - 40, min_scale=3)
        c.text(pill_x1 - pill_w // 2, y + 38 - (7 * odd_scale) // 2, odd, odd_scale,
               WHITE, anchor="m", bold=True)
        c.text(pill_x1 - pill_w // 2, y + 74, row["time"], 2, GRAY, anchor="m", bold=True)

        y += ROW_H
        c.rect(card_x0 + 28, y - 2, card_x1 - 28, y, LINE)

    y += 16
    c.rrect(card_x0 + 24, y, card_x1 - 24, y + bar_h, 28, BAR)
    c.text(card_x0 + 52, y + 22, "COMBINED ODDS", 3, INK, bold=True)
    c.text(card_x0 + 52, y + 56, f"CHANCE ALL WIN: ~{win_chance * 100:.0f}%", 2, GRAY, bold=True)
    total, total_scale = fit(f"{total_odds:.2f}", 5, 190, min_scale=3)
    big_w = max(200, text_width(total, total_scale) + 60)
    total_color = GREEN if status == "won" else SLATE if status == "cancelled" else RED
    c.rrect(card_x1 - 24 - 20 - big_w, y + 16, card_x1 - 24 - 20, y + bar_h - 16, 29, total_color)
    c.text(card_x1 - 24 - 20 - big_w // 2, y + bar_h // 2 - (7 * total_scale) // 2, total,
           total_scale, WHITE, anchor="m", bold=True)
    y += bar_h

    if code:
        y += 14
        c.rrect(card_x0 + 24, y, card_x1 - 24, y + 62, 24, BAR)
        c.text(card_x0 + 52, y + 21, "SPORTYBET CODE", 3, INK, bold=True)
        code_text, code_scale = fit(code, 4, 300, min_scale=3)
        code_w = text_width(code_text, code_scale) + 60
        c.rrect(card_x1 - 24 - 12 - code_w, y + 8, card_x1 - 24 - 12, y + 54, 23, NAVY)
        c.text(card_x1 - 24 - 12 - code_w // 2, y + 31 - (7 * code_scale) // 2, code_text,
               code_scale, WHITE, anchor="m", bold=True)

    if status:
        word = "WON" if status == "won" else "CANCELLED"
        color = GREEN if status == "won" else LOSS
        scale = 6 if len(word) <= 3 else 5
        sw = text_width(word, scale) + 50
        sx1 = W - PAD
        sx0 = sx1 - sw
        c.rrect(sx0 - 6, 128, sx1 + 6, 128 + 7 * scale + 56, 20, color)
        c.rrect(sx0, 134, sx1, 128 + 7 * scale + 50, 14, WHITE)
        c.text((sx0 + sx1) // 2, 128 + 28, word, scale, color, anchor="m", bold=True)

    foot_y = card_y0 + card_h + 34
    c.text(PAD, foot_y, "AI PICKS. SMARTER SLIPS.", 3, WHITE, bold=True)
    c.text(W - PAD, foot_y, "SPORTYTIPS", 3, WHITE, anchor="r", bold=True)
    c.text(PAD, foot_y + 44, "ESTIMATES ONLY, NOT GUARANTEES. BET RESPONSIBLY (18+).",
           2, MUTED)

    if WATERMARK_ENABLED:
        mark = clean(WATERMARK)
        mark_w = text_width(mark, 6)
        step_x, step_y = mark_w + 120, 170
        row_number = 0
        for wy in range(60, height + int(mark_w * 0.5) + step_y, step_y):
            offset = (row_number % 2) * (step_x // 2)
            for wx in range(-step_x + offset, W, step_x):
                c.slanted_text(wx, wy, mark, 6, WHITE, WATERMARK_ALPHA)
            row_number += 1

    return c.png()


try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_CANDIDATES = [
    os.path.join(HERE, "ticket_font.ttf"),
    "ticket_font.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
SCALE = 2


def _find_font():
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _make_smooth_image(font_path, rows, title, subtitle, total_odds, win_chance, code, status=None):
    status = ticket_status(rows, status)
    S = SCALE
    card_x0, card_x1 = PAD, W - PAD
    card_y0 = HEADER_H
    bar_h = 90
    REASON_SIZE, REASON_LINE = 20, 25
    reason_max_w = (card_x1 - 28 - 176 - 24) - (card_x0 + 82)

    fonts = {}
    measure = ImageDraw.Draw(Image.new("RGB", (4, 4)))

    def font(size):
        size = int(size)
        if size not in fonts:
            fonts[size] = ImageFont.truetype(font_path, size * S)
        return fonts[size]

    def wrap(text_value, size, max_w, max_lines=2):
        words, lines_out, current = str(text_value).split(), [], ""
        for word in words:
            trial = (current + " " + word).strip()
            if measure.textlength(trial, font=font(size)) / S <= max_w:
                current = trial
            else:
                if current:
                    lines_out.append(current)
                current = word
        if current:
            lines_out.append(current)
        if len(lines_out) > max_lines:
            lines_out = lines_out[:max_lines]
            lines_out[-1] = lines_out[-1].rstrip(".,") + "..."
        return lines_out

    reason_lines = [wrap(r.get("reason"), REASON_SIZE, reason_max_w) if r.get("reason") else [] for r in rows]
    row_heights = [ROW_H + (len(rl) * REASON_LINE + 4 if rl else 0) for rl in reason_lines]
    card_h = 14 + sum(row_heights) + 16 + bar_h + (76 if code else 0) + 26
    height = card_y0 + card_h + 130
    if height > 3200:
        S = 1

    img = Image.new("RGB", (W * S, height * S), NAVY)
    d = ImageDraw.Draw(img)
    fonts.clear()

    def X(v):
        return int(round(v * S))

    def width_of(text, size):
        return d.textlength(text, font=font(size)) / S

    def fit(text, size, max_w, min_size):
        text = str(text)
        while size > min_size and width_of(text, size) > max_w:
            size -= 1
        while len(text) > 3 and width_of(text, size) > max_w:
            text = text[:-4].rstrip() + "..."
        return text, size

    def rr(x0, y0, x1, y1, r, fill):
        d.rounded_rectangle([X(x0), X(y0), X(x1) - 1, X(y1) - 1], radius=X(r), fill=fill)

    def circle(cx, cy, r, fill):
        d.ellipse([X(cx - r), X(cy - r), X(cx + r) - 1, X(cy + r) - 1], fill=fill)

    def text(x, y, value, size, fill, anchor="lm"):
        d.text((X(x), X(y)), str(value), font=font(size), fill=fill, anchor=anchor)

    def xr(y):
        return RED_X0 + y * RED_SLOPE
    d.polygon([(X(xr(0)), 0), (X(W), 0), (X(W), X(height)), (X(xr(height)), X(height))], fill=RED)
    d.polygon([(X(xr(0) + STRIPE_OFFSET), 0), (X(xr(0) + STRIPE_OFFSET + STRIPE_WIDTH), 0),
               (X(xr(height) + STRIPE_OFFSET + STRIPE_WIDTH), X(height)),
               (X(xr(height) + STRIPE_OFFSET), X(height))], fill=DARK_RED)

    rr(PAD, 40, PAD + 64, 104, 16, RED)
    text(PAD + 32, 72, "S", 46, WHITE, "mm")
    text(PAD + 84, 72, "SPORTYTIPS", 34, WHITE)

    sub, sub_size = fit(subtitle, 27, 400, 18)
    pill_w = width_of(sub, sub_size) + 56
    rr(W - PAD - pill_w, 46, W - PAD, 100, 27, WHITE)
    text(W - PAD - pill_w / 2, 73, sub, sub_size, INK, "mm")

    head, head_size = fit(str(title).upper(), 74, W - 2 * PAD, 40)
    text(PAD, 168, head, head_size, WHITE)
    line_y = 168 + int(head_size * 0.55) + 12
    d.rectangle([X(PAD), X(line_y), X(PAD + width_of(head, head_size) * 0.6), X(line_y + 6) - 1], fill=RED)

    rr(card_x0, card_y0, card_x1, card_y0 + card_h, 40, WHITE)

    y = card_y0 + 14
    text_x = card_x0 + 82
    pill_w = 176
    pill_x1 = card_x1 - 28
    for row_index, row in enumerate(rows):
        result = str(row.get("result") or "").lower()
        cx, cy = card_x0 + 44, y + 38
        if result == "won":
            circle(cx, cy, 21, GREEN)
            d.line([(X(cx - 10), X(cy + 1)), (X(cx - 3), X(cy + 8)), (X(cx + 11), X(cy - 9))],
                   fill=WHITE, width=X(5), joint="curve")
        elif result == "lost":
            circle(cx, cy, 21, LOSS)
            d.line([(X(cx - 9), X(cy - 9)), (X(cx + 9), X(cy + 9))], fill=WHITE, width=X(5))
            d.line([(X(cx - 9), X(cy + 9)), (X(cx + 9), X(cy - 9))], fill=WHITE, width=X(5))
        elif result == "void":
            circle(cx, cy, 21, SLATE)
            d.line([(X(cx - 10), X(cy)), (X(cx + 10), X(cy))], fill=WHITE, width=X(5))
        else:
            circle(cx, cy, 13, GRAY)
            circle(cx, cy, 10, WHITE)
            circle(cx, cy, 6, INK)

        max_w = pill_x1 - pill_w - 24 - text_x
        line, size = fit(row["match"], 31, max_w, 18)
        text(text_x, y + 32, line, size, INK)
        line, size = fit(row["pick"], 25, max_w, 17)
        text(text_x, y + 70, line, size, GRAY)

        pill_color = GREEN if result == "won" else SLATE if result in ("lost", "void") else NAVY
        rr(pill_x1 - pill_w, y + 14, pill_x1, y + 62, 24, pill_color)
        odd, odd_size = fit(f"{row['odd']:.2f}", 33, pill_w - 40, 20)
        text(pill_x1 - pill_w / 2, y + 38, odd, odd_size, WHITE, "mm")
        text(pill_x1 - pill_w / 2, y + 85, row["time"], 17, GRAY, "mm")

        for line_no, reason_line in enumerate(reason_lines[row_index]):
            text(text_x, y + 98 + line_no * REASON_LINE, reason_line, REASON_SIZE, (110, 120, 135))

        y += row_heights[row_index]
        d.rectangle([X(card_x0 + 28), X(y - 2), X(card_x1 - 28), X(y) - 1], fill=LINE)

    y += 16
    rr(card_x0 + 24, y, card_x1 - 24, y + bar_h, 28, BAR)
    text(card_x0 + 52, y + 32, "COMBINED ODDS", 25, INK)
    text(card_x0 + 52, y + 64, f"Chance all win: ~{win_chance * 100:.0f}%", 19, GRAY)
    total, total_size = fit(f"{total_odds:.2f}", 37, 190, 22)
    big_w = max(200, width_of(total, total_size) + 60)
    total_color = GREEN if status == "won" else SLATE if status == "cancelled" else RED
    rr(card_x1 - 44 - big_w, y + 16, card_x1 - 44, y + bar_h - 16, 29, total_color)
    text(card_x1 - 44 - big_w / 2, y + bar_h / 2, total, total_size, WHITE, "mm")
    y += bar_h

    if code:
        y += 14
        rr(card_x0 + 24, y, card_x1 - 24, y + 62, 24, BAR)
        text(card_x0 + 52, y + 31, "SPORTYBET CODE", 25, INK)
        code_text, code_size = fit(code, 30, 300, 20)
        code_w = width_of(code_text, code_size) + 60
        rr(card_x1 - 36 - code_w, y + 8, card_x1 - 36, y + 54, 23, NAVY)
        text(card_x1 - 36 - code_w / 2, y + 31, code_text, code_size, WHITE, "mm")

    foot_y = card_y0 + card_h + 46
    text(PAD, foot_y, "AI picks. Smarter slips.", 27, WHITE)
    text(W - PAD, foot_y, "SportyTips", 27, WHITE, "rm")
    text(PAD, foot_y + 46, "Estimates only, not guarantees. Bet responsibly (18+).", 18, MUTED)

    if status:
        # Tilted stamp in the header corner, so it never covers a pick.
        word = "WON" if status == "won" else "CANCELLED"
        color = GREEN if status == "won" else LOSS
        stamp_font = ImageFont.truetype(font_path, 54 * S)
        tw = int(d.textlength(word, font=stamp_font))
        pad = 20 * S
        sw, sh = tw + 2 * pad, 54 * S + 2 * pad
        stamp = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        sd = ImageDraw.Draw(stamp)
        sd.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=16 * S,
                             outline=color + (255,), width=7 * S, fill=(255, 255, 255, 245))
        sd.text((sw // 2, sh // 2), word, font=stamp_font, fill=color + (255,), anchor="mm")
        stamp = stamp.rotate(9, expand=True, resample=Image.BICUBIC)
        base = img.convert("RGBA")
        cx_stamp, cy_stamp = X(W - PAD - 175), X(182)
        base.alpha_composite(stamp, (max(0, cx_stamp - stamp.width // 2),
                                     max(0, cy_stamp - stamp.height // 2)))
        img = base.convert("RGB")

    out = img.resize((W, height), getattr(Image, "Resampling", Image).LANCZOS)
    buffer = io.BytesIO()
    out.save(buffer, format="PNG")
    return buffer.getvalue()


def make_ticket_image(rows, title, subtitle, total_odds, win_chance, code=None, status=None):
    """rows: [{"time","league","match","pick","odd","prob","result"}] -> PNG bytes.

    "result" is optional per row: "won", "lost", "void" or missing (pending).
    The ticket gets a WON / CANCELLED stamp automatically from the row results,
    or you can force it with status="won" / "cancelled".
    """
    font_path = _find_font()
    if Image is not None and font_path and not WATERMARK_ENABLED:
        try:
            return _make_smooth_image(font_path, rows, title, subtitle,
                                      total_odds, win_chance, code, status)
        except Exception as exc:
            print(f"Smooth ticket failed, using simple one: {exc}")
    return _make_bitmap_image(rows, title, subtitle, total_odds, win_chance, code, status)