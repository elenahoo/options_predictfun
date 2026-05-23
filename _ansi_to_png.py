"""Render an ANSI-coloured text stream to a PNG, terminal-style.

Used to produce a "screenshot" of the Slack alert preview when no real Slack
webhook is configured. Reads from stdin, writes a PNG to argv[1].
"""

import re
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

MONO = "/System/Library/Fonts/Menlo.ttc"
EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"
FONT_SIZE = 18
EMOJI_RENDER_SIZE = 160
EMOJI_DRAW_SIZE = 18
LINE_HEIGHT = 24
PAD_X = 28
PAD_Y = 24
BG = (30, 30, 36)
FG = (220, 220, 220)

ANSI_FG = {
    30: (0, 0, 0), 31: (205, 49, 49), 32: (13, 188, 121),
    33: (229, 229, 16), 34: (36, 114, 200), 35: (188, 63, 188),
    36: (17, 168, 205), 37: (229, 229, 229),
    90: (130, 130, 130), 91: (241, 76, 76), 92: (35, 209, 139),
    93: (245, 245, 67), 94: (89, 173, 255), 95: (214, 112, 214),
    96: (41, 184, 219), 97: (255, 255, 255),
}
ANSI_BG = {
    40: (0, 0, 0), 41: (205, 49, 49), 42: (13, 188, 121),
    43: (229, 229, 16), 44: (36, 114, 200), 45: (188, 63, 188),
    46: (17, 168, 205), 47: (229, 229, 229),
}
EXT_BG_256 = {
    236: (50, 50, 50),
    24: (0, 70, 120),
}

ANSI_RE = re.compile(r"\x1b\[([\d;]*)m")
EMOJI_RE = re.compile(
    "([\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF])"
)


def parse_segments(text: str):
    """Yield (text, fg, bg, bold, italic, underline) segments."""
    fg, bg = FG, None
    bold = italic = underline = False
    pos = 0
    while pos < len(text):
        m = ANSI_RE.search(text, pos)
        if not m:
            yield text[pos:], fg, bg, bold, italic, underline
            return
        if m.start() > pos:
            yield text[pos:m.start()], fg, bg, bold, italic, underline
        codes = m.group(1)
        parts = [int(p) for p in codes.split(";") if p != ""] if codes else [0]
        i = 0
        while i < len(parts):
            c = parts[i]
            if c == 0:
                fg, bg = FG, None
                bold = italic = underline = False
            elif c == 1:
                bold = True
            elif c == 3:
                italic = True
            elif c == 4:
                underline = True
            elif c in ANSI_FG:
                fg = ANSI_FG[c]
            elif c in ANSI_BG:
                bg = ANSI_BG[c]
            elif c == 48 and i + 2 < len(parts) and parts[i + 1] == 5:
                bg = EXT_BG_256.get(parts[i + 2], (60, 60, 60))
                i += 2
            elif c == 38 and i + 2 < len(parts) and parts[i + 1] == 5:
                fg = ANSI_FG.get(parts[i + 2], FG)
                i += 2
            i += 1
        pos = m.end()


def render(text: str, out_path: Path) -> None:
    text = text.replace("\t", "    ")
    raw_lines = text.split("\n")

    font = ImageFont.truetype(MONO, FONT_SIZE)
    font_b = ImageFont.truetype(MONO, FONT_SIZE)
    font_i = ImageFont.truetype(MONO, FONT_SIZE)
    try:
        emoji_font = ImageFont.truetype(EMOJI, EMOJI_RENDER_SIZE)
    except OSError:
        emoji_font = None

    emoji_cache: dict[str, Image.Image] = {}

    def render_emoji(ch: str) -> Image.Image:
        if ch in emoji_cache:
            return emoji_cache[ch]
        big = Image.new("RGBA", (EMOJI_RENDER_SIZE + 20, EMOJI_RENDER_SIZE + 20), (0, 0, 0, 0))
        big_draw = ImageDraw.Draw(big)
        try:
            big_draw.text((0, 0), ch, font=emoji_font, embedded_color=True)
        except Exception:
            pass
        bbox = big.getbbox()
        if bbox is not None:
            big = big.crop(bbox)
        small = big.resize((EMOJI_DRAW_SIZE, EMOJI_DRAW_SIZE), Image.LANCZOS)
        emoji_cache[ch] = small
        return small

    char_w = font.getlength("M")

    visible_lengths = []
    for raw in raw_lines:
        plain = ANSI_RE.sub("", raw)
        visible_lengths.append(len(plain))
    width_chars = max(visible_lengths) if visible_lengths else 80
    width_chars = max(width_chars, 100)

    img_w = int(PAD_X * 2 + char_w * width_chars + 4)
    img_h = PAD_Y * 2 + LINE_HEIGHT * len(raw_lines) + 60

    img = Image.new("RGB", (img_w, img_h), BG)
    draw = ImageDraw.Draw(img)

    title_bar_h = 36
    draw.rectangle((0, 0, img_w, title_bar_h), fill=(45, 45, 55))
    cx = 18
    for color in ((255, 95, 86), (255, 189, 46), (39, 201, 63)):
        draw.ellipse((cx, 12, cx + 12, 24), fill=color)
        cx += 20
    title = "Slack — #arb-alerts (terminal preview)"
    draw.text(
        (img_w // 2 - len(title) * 4, 10), title,
        font=font, fill=(180, 180, 180),
    )

    y = title_bar_h + PAD_Y
    for raw in raw_lines:
        x = PAD_X
        for seg, fg, bg, bold, italic, underline in parse_segments(raw):
            if not seg:
                continue
            parts = EMOJI_RE.split(seg)
            for part in parts:
                if not part:
                    continue
                if EMOJI_RE.match(part) and emoji_font is not None:
                    cell_w = int(char_w * 2)
                    if bg is not None:
                        draw.rectangle(
                            (x, y, x + cell_w, y + LINE_HEIGHT), fill=bg
                        )
                    emoji_img = render_emoji(part)
                    paste_x = x + (cell_w - EMOJI_DRAW_SIZE) // 2
                    paste_y = y + (LINE_HEIGHT - EMOJI_DRAW_SIZE) // 2
                    img.paste(emoji_img, (paste_x, paste_y), emoji_img)
                    x += cell_w
                else:
                    width = int(font.getlength(part))
                    if bg is not None:
                        draw.rectangle(
                            (x, y, x + width, y + LINE_HEIGHT), fill=bg
                        )
                    draw.text((x, y), part, font=font, fill=fg)
                    if underline:
                        draw.line(
                            (x, y + LINE_HEIGHT - 2,
                             x + width, y + LINE_HEIGHT - 2),
                            fill=fg, width=1,
                        )
                    if bold:
                        draw.text((x + 1, y), part, font=font, fill=fg)
                    x += width
        y += LINE_HEIGHT

    img.save(out_path, "PNG")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("preview.png")
    render(sys.stdin.read(), out)
    print(f"wrote {out}")
