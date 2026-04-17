"""
Export icon16.png, icon48.png, icon128.png using Pillow only (no Cairo needed).

Requires: pip install pillow
"""

import sys, math
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Install Pillow first:  pip install pillow")

OUT = Path(__file__).parent

# Colours (match the app's dark theme)
BG        = (15,  23,  42)   # #0f172a
CARD      = (30,  41,  59)   # #1e293b
BORDER    = (51,  65,  85)   # #334155
LABEL_BG  = (30,  58, 138)   # #1e3a8a
BLUE      = (37,  99, 235)   # #2563eb
PINS      = (71,  85, 105)   # #475569
WHITE     = (255,255,255)


def draw_icon(size: int) -> Image.Image:
    S = size
    sc = S / 128  # scale factor

    img  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def s(v):
        return max(1, round(v * sc))

    # Background with rounded corners
    r_bg = s(22)
    draw.rounded_rectangle([0, 0, S-1, S-1], radius=r_bg, fill=BG)

    # Cartridge body
    cx0, cy0, cx1, cy1 = s(20), s(28), s(108), s(90)
    draw.rounded_rectangle([cx0, cy0, cx1, cy1], radius=s(7), fill=CARD, outline=BORDER, width=s(2))

    # Label panel (blue)
    lx0, ly0, lx1, ly1 = s(30), s(36), s(98), s(72)
    draw.rounded_rectangle([lx0, ly0, lx1, ly1], radius=s(4), fill=LABEL_BG, outline=BLUE, width=s(1))

    # "RF" text centred on the label
    font = None
    font_size = s(22)
    for candidate in [
        "C:/Windows/Fonts/consolab.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
    ]:
        try:
            font = ImageFont.truetype(candidate, font_size)
            break
        except (IOError, OSError):
            pass
    if font is None:
        font = ImageFont.load_default()

    text = "RF"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    lmx = (lx0 + lx1) // 2
    lmy = (ly0 + ly1) // 2
    draw.text((lmx - tw // 2, lmy - th // 2 - bbox[1]), text, font=font, fill=WHITE)

    # Connector pins
    pin_y0, pin_y1 = s(84), s(90)
    pin_xs = [s(34), s(46), s(58), s(70), s(82)]
    for px in pin_xs:
        draw.rounded_rectangle([px, pin_y0, px + s(8), pin_y1], radius=s(2), fill=PINS)

    # Magnifying glass (bottom-right)
    mg_cx, mg_cy, mg_r = s(91), s(95), s(10)
    # Erase circle bg with dark colour (fake "clear")
    draw.ellipse(
        [mg_cx - mg_r - s(4), mg_cy - mg_r - s(4),
         mg_cx + mg_r + s(4), mg_cy + mg_r + s(4)],
        fill=BG,
    )
    draw.ellipse(
        [mg_cx - mg_r, mg_cy - mg_r, mg_cx + mg_r, mg_cy + mg_r],
        outline=BLUE, width=s(3),
    )
    # Handle
    ang = math.radians(45)
    hx0 = mg_cx + int(mg_r * 0.7 * math.cos(ang))
    hy0 = mg_cy + int(mg_r * 0.7 * math.sin(ang))
    hx1 = mg_cx + int((mg_r + s(8)) * math.cos(ang))
    hy1 = mg_cy + int((mg_r + s(8)) * math.sin(ang))
    draw.line([hx0, hy0, hx1, hy1], fill=BLUE, width=s(3))

    return img


def main():
    for size in [16, 48, 128]:
        dest = OUT / f"icon{size}.png"
        img  = draw_icon(size)
        if size < 128:
            # Draw at 128 then resize for crisper small icons
            big = draw_icon(128)
            img = big.resize((size, size), Image.LANCZOS)
        img.save(dest, "PNG")
        print(f"  wrote {dest.name}  ({size}x{size})")

    # Also export a 440x280 store promo tile (required by Web Store)
    promo = make_promo()
    dest  = OUT / "promo_440x280.png"
    promo.save(dest, "PNG")
    print(f"  wrote {dest.name}  (440x280 promo tile)")


def make_promo() -> Image.Image:
    W, H = 440, 280
    img  = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Subtle grid lines
    for x in range(0, W, 40):
        draw.line([(x, 0), (x, H)], fill=(30, 41, 59), width=1)
    for y in range(0, H, 40):
        draw.line([(0, y), (W, y)], fill=(30, 41, 59), width=1)

    # Centre icon
    icon = draw_icon(128)
    img.paste(icon, (W // 2 - 64, H // 2 - 80), icon)

    # App name
    title_font = None
    for candidate in [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "C:/Windows/Fonts/consolab.ttf",
    ]:
        try:
            title_font = ImageFont.truetype(candidate, 32)
            break
        except (IOError, OSError):
            pass
    if title_font is None:
        title_font = ImageFont.load_default()

    sub_font = None
    for candidate in [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/consola.ttf",
    ]:
        try:
            sub_font = ImageFont.truetype(candidate, 15)
            break
        except (IOError, OSError):
            pass
    if sub_font is None:
        sub_font = ImageFont.load_default()

    title = "ROM Finder"
    bbox  = draw.textbbox((0, 0), title, font=title_font)
    tw    = bbox[2] - bbox[0]
    draw.text((W // 2 - tw // 2, H // 2 + 60), title, font=title_font, fill=WHITE)

    subtitle = "RetroAchievements  →  Wanted list  →  ROM sources"
    bbox2 = draw.textbbox((0, 0), subtitle, font=sub_font)
    tw2   = bbox2[2] - bbox2[0]
    draw.text((W // 2 - tw2 // 2, H // 2 + 100), subtitle, font=sub_font, fill=(100, 116, 139))

    return img


if __name__ == "__main__":
    main()
    print("Done.")
