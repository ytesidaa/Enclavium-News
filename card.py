"""
Enclavium news card generator (v2).

1080x1350 (Instagram 4:5). Layout follows the Enclavium master design:
  - "EnclaviumFNF" top-left, logo top-right
  - photo of the person/object as the main focus (optional)
  - big white headline, Poppins ExtraBold, with one red highlight block
  - black-to-red gradient at the bottom
"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageChops, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
if not os.path.exists(os.path.join(ASSETS, "bg_clean.png")) and \
        os.path.exists(os.path.join(HERE, "bg_clean.png")):
    ASSETS = HERE          # files were uploaded without the 'assets' folder

REQUIRED_ASSETS = ["bg_clean.png", "logo_white.png", "Poppins-ExtraBold.ttf",
                   "LiberationSans-Regular.ttf"]


def missing_assets():
    return [n for n in REQUIRED_ASSETS if not os.path.exists(os.path.join(ASSETS, n))]

W, H = 1080, 1350
MARGIN = 96
TEXT_W = W - MARGIN * 2            # 888
PAD_X = 14                         # horizontal padding inside highlight block
BRAND_RED = (222, 8, 20)
WHITE = (255, 255, 255)
PHOTO_H = 960


def _font(name, size):
    return ImageFont.truetype(os.path.join(ASSETS, name), size)


# --------------------------------------------------------------------------
# background + fixed brand elements
# --------------------------------------------------------------------------
def _background():
    return Image.open(os.path.join(ASSETS, "bg_clean.png")).convert("RGB")


def _chrome(img, nfa=False):
    """Handle, logo (and optionally the NFA line) in the master positions."""
    d = ImageDraw.Draw(img)
    d.text((MARGIN, 148), "EnclaviumFNF", font=_font("LiberationSans-Regular.ttf", 27),
           fill=WHITE, anchor="lm")
    logo = Image.open(os.path.join(ASSETS, "logo_white.png"))
    img.paste(logo, (W - 90 - logo.width, 96), logo)
    if nfa:
        d.text((MARGIN, 1246), "NFA : Not Financial Advice",
               font=_font("LiberationSans-Italic.ttf", 27), fill=WHITE, anchor="lm")


# --------------------------------------------------------------------------
# photo layer (automatic placement)
# --------------------------------------------------------------------------
def _detect_face(pil_img):
    """Return (cx, cy) of the largest face, or None. Works without OpenCV too."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        scale = 900 / max(pil_img.size)
        small = pil_img.convert("L")
        if scale < 1:
            small = small.resize((int(pil_img.width * scale), int(pil_img.height * scale)))
        arr = np.array(small)
        for name in ("haarcascade_frontalface_default.xml",
                     "haarcascade_frontalface_alt2.xml",
                     "haarcascade_profileface.xml"):
            casc = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            faces = casc.detectMultiScale(arr, 1.1, 5, minSize=(max(40, arr.shape[1] // 14),) * 2)
            if len(faces):
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                k = 1 / scale if scale < 1 else 1.0
                return (x + w / 2) * k, (y + h / 2) * k
    except Exception:
        return None
    return None


def _apply_photo(img, path, style="natural"):
    """Cover the top ~900px with the photo, face-aware, fading into the background."""
    ph = Image.open(path).convert("RGB")
    if max(ph.size) > 2400:                       # keep things fast
        k = 2400 / max(ph.size)
        ph = ph.resize((int(ph.width * k), int(ph.height * k)), Image.LANCZOS)

    s = max(900 / ph.height, W / ph.width)
    sw, sh = int(ph.width * s) + 1, int(ph.height * s) + 1
    face = _detect_face(ph)
    ph = ph.resize((sw, sh), Image.LANCZOS)
    fx, fy = (face[0] * s, face[1] * s) if face else (sw / 2, sh * 0.38)

    px = int(min(0, max(W - sw, 610 - fx)))       # face a little right of centre
    py = int(min(0, max(900 - sh, 330 - fy)))     # face in the upper third

    if style == "duo":
        g = ImageOps.autocontrast(ph.convert("L"), cutoff=1)
        ph = ImageOps.colorize(g, black=(8, 0, 0), mid=(150, 12, 22), white=(255, 222, 222))

    layer = Image.new("RGB", (W, PHOTO_H), (6, 0, 0))
    layer.paste(ph, (px, py))

    mask = Image.new("L", (W, PHOTO_H), 255)       # bottom fade
    md = ImageDraw.Draw(mask)
    f0, f1 = 520, 900
    for y in range(PHOTO_H):
        t = min(1.0, max(0.0, (y - f0) / (f1 - f0)))
        t = t * t * (3 - 2 * t)
        md.line([(0, y), (W, y)], fill=int(255 * (1 - t)))
    img.paste(layer, (0, 0), mask)

    scrim = Image.new("L", (W, 300), 0)             # keep handle/logo readable
    sd = ImageDraw.Draw(scrim)
    for y in range(300):
        sd.line([(0, y), (W, y)], fill=int(120 * (1 - y / 300) ** 1.5))
    img.paste(Image.new("RGB", (W, 300), (0, 0, 0)), (0, 0), scrim)


# --------------------------------------------------------------------------
# emblem layer (coin / platform logo as a round badge)
# --------------------------------------------------------------------------
def _round_logo(path, size):
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    m = min(w, h)
    im = im.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
    im = im.resize((size, size), Image.LANCZOS).filter(
        ImageFilter.UnsharpMask(radius=2, percent=70, threshold=2))
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    im.putalpha(ImageChops.multiply(im.getchannel("A"), mask))
    return im


def _apply_emblem(img, path, size=400, cx=W // 2, cy=430):
    """Logo as a round badge in the photo area, with a soft red glow."""
    glow = Image.new("L", (W, PHOTO_H), 0)
    gd = ImageDraw.Draw(glow)
    r = size // 2 + 70
    gd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(70)).point(lambda v: int(v * 0.55))
    img.paste(Image.new("RGB", (W, PHOTO_H), (235, 22, 38)), (0, 0), glow)

    with Image.open(path) as probe:                  # don't blow up tiny logos too much
        size = min(size, max(280, int(min(probe.size) * 3.3)))
    badge = _round_logo(path, size)
    ring = Image.new("RGBA", (size + 36, size + 36), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((1, 1, size + 34, size + 34), outline=(255, 255, 255, 90), width=3)
    img.paste(ring, (cx - (size + 36) // 2, cy - (size + 36) // 2), ring)

    shadow = Image.new("L", (W, PHOTO_H), 0)
    ImageDraw.Draw(shadow).ellipse((cx - size // 2, cy - size // 2 + 14, cx + size // 2, cy + size // 2 + 14), fill=255)
    shadow = shadow.filter(ImageFilter.GaussianBlur(24)).point(lambda v: int(v * 0.6))
    img.paste(Image.new("RGB", (W, PHOTO_H), (0, 0, 0)), (0, 0), shadow)
    img.paste(badge, (cx - size // 2, cy - size // 2), badge)


# --------------------------------------------------------------------------
# headline helpers
# --------------------------------------------------------------------------
def _mark_words(headline, highlight):
    words = headline.split()
    flags = [False] * len(words)
    if highlight:
        hw = highlight.split()
        n = len(hw)
        norm = lambda s: s.strip(",.!?:;").lower()
        for i in range(len(words) - n + 1):
            if [norm(x) for x in words[i:i + n]] == [norm(x) for x in hw]:
                for j in range(i, i + n):
                    flags[j] = True
                break
    return list(zip(words, flags))


def _merge(words, font, max_w):
    """Keep a highlighted phrase together on one line when it fits."""
    out, run = [], []

    def flush():
        if run:
            joined = " ".join(run)
            if font.getlength(joined) <= max_w:
                out.append((joined, True))
            else:
                out.extend((w, True) for w in run)
            run.clear()

    for w, hl in words:
        if hl:
            run.append(w)
        else:
            flush()
            out.append((w, False))
    flush()
    return out


def _wrap(tokens, font, max_w):
    lines, cur, cur_w = [], [], 0
    space = font.getlength(" ")
    for w, hl in tokens:
        ww = font.getlength(w) + (2 * PAD_X if hl else 0)
        add = ww if not cur else space + ww
        if cur and cur_w + add > max_w:
            lines.append(cur)
            cur, cur_w = [(w, hl)], ww
        else:
            cur.append((w, hl))
            cur_w += add
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------
def make_card(headline, highlight, out_path, tag=None, source=None, nfa=False,
              photo=None, photo_style="natural", emblem=None,
              glow=False, shadow=True):
    img = _background()
    if photo:
        _apply_photo(img, photo, photo_style)
    elif emblem:
        _apply_emblem(img, emblem)
    _chrome(img, nfa=nfa)

    footer_on = nfa or bool(source)
    bottom = 1130 if footer_on else 1228

    words = _mark_words(headline, highlight)
    max_lines = 4
    sizes = (82, 76, 70, 64, 58, 52)
    for size in sizes:
        font = _font("Poppins-ExtraBold.ttf", size)
        lines = _wrap(_merge(words, font, TEXT_W - 2 * PAD_X), font, TEXT_W)
        if len(lines) <= max_lines:
            break

    # vertical metrics (tight leading, block = 1.10em tall)
    up, down = 0.84 * size, 0.27 * size
    has_hl = [any(h for _, h in ln) for ln in lines]
    pitches = [0] + [int(size * (1.13 if (has_hl[i] or has_hl[i - 1]) else 1.05))
                     for i in range(1, len(lines))]
    total = up + sum(pitches) + down
    tag_h, tag_gap = 56, 30
    if tag:
        total += tag_h + tag_gap
    top = bottom - total

    d = ImageDraw.Draw(img)
    y = top
    if tag:
        tag_font = _font("Poppins-Bold.ttf", 28)
        tag_w = tag_font.getlength(tag) + 44
        d.rectangle([MARGIN, y, MARGIN + tag_w, y + tag_h], fill=BRAND_RED)
        d.text((MARGIN + 22, y + tag_h / 2 + 1), tag, font=tag_font, fill=WHITE, anchor="lm")
        y += tag_h + tag_gap
    head_top = y

    # lay everything out first
    space = font.getlength(" ")
    all_runs, all_text = [], []
    baseline = y + up
    for i, line in enumerate(lines):
        baseline += pitches[i]
        cx, prev_hl, run_start, runs = MARGIN, False, None, []
        for w, hl in line:
            if hl and not prev_hl:
                cx += PAD_X
                run_start = cx
            if not hl and prev_hl:
                runs.append((run_start, cx - space))
                cx += PAD_X
            all_text.append((w, cx, baseline, hl))
            cx += font.getlength(w) + space
            prev_hl = hl
        if prev_hl:
            runs.append((run_start, cx - space))
        for (x0, x1) in runs:
            all_runs.append([int(x0 - PAD_X), int(baseline - up), int(x1 + PAD_X), int(baseline + down)])

    # soft effects underneath
    if glow:
        gl = Image.new("L", (W, H), 0)
        gd = ImageDraw.Draw(gl)
        for r in all_runs:
            gd.rectangle(r, fill=255)
        gl = gl.filter(ImageFilter.GaussianBlur(26)).point(lambda v: int(v * 0.75))
        img.paste(Image.new("RGB", (W, H), (255, 28, 40)), (0, 0), gl)
    if shadow:
        sh = Image.new("L", (W, H), 0)
        sd = ImageDraw.Draw(sh)
        for w, px_, bl, hl in all_text:
            sd.text((px_, bl + 5), w, font=font, fill=255, anchor="ls")
        sh = sh.filter(ImageFilter.GaussianBlur(10)).point(lambda v: int(v * 0.6))
        img.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), sh)

    # highlight blocks
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in all_runs:
        if glow:
            grad = Image.new("RGB", (x1 - x0, y1 - y0))
            gdr = ImageDraw.Draw(grad)
            for yy in range(y1 - y0):
                t = yy / max(1, y1 - y0 - 1)
                col = tuple(int(a + (b - a) * t) for a, b in zip((238, 24, 40), (176, 6, 18)))
                gdr.line([(0, yy), (x1 - x0, yy)], fill=col)
            img.paste(grad, (x0, y0))
        else:
            d.rectangle([x0, y0, x1, y1], fill=BRAND_RED)

    # headline text
    if glow:   # white -> faint pink gradient
        tm = Image.new("L", (W, H), 0)
        td = ImageDraw.Draw(tm)
        for w, px_, bl, hl in all_text:
            td.text((px_, bl), w, font=font, fill=255, anchor="ls")
        tg = Image.new("RGB", (W, H))
        tgd = ImageDraw.Draw(tg)
        for yy in range(H):
            t = min(1.0, max(0.0, (yy - head_top) / max(1, bottom - head_top)))
            tgd.line([(0, yy), (W, yy)], fill=(255, int(255 - 34 * t), int(255 - 34 * t)))
        img.paste(tg, (0, 0), tm)
        d = ImageDraw.Draw(img)
    else:
        for w, px_, bl, hl in all_text:
            d.text((px_, bl), w, font=font, fill=WHITE, anchor="ls")

    if source:
        d.text((W - MARGIN, 1246), f"Sumber: {source}",
               font=_font("LiberationSans-Regular.ttf", 27), fill=WHITE, anchor="rm")

    img.save(out_path, "PNG", optimize=True)
    return out_path
