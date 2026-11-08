"""Build every icon, banner and avatar from the one logo file.

Run after replacing branding/cyberdefency-logo.webp:

    python scripts/build_brand_assets.py

Small sizes use the emblem (shield on the globe) without the wordmark: letters
are unreadable at 32-192 px. Large surfaces use the full logo, banners put it on
a drawn backdrop (light, a network, orbits, sparks) that is the same every run.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "branding" / "cyberdefency-logo.webp"
# The shield and the top of the globe, above the wordmark (it starts at y=1252).
EMBLEM_BOX = (545, 350, 1445, 1250)
BACKGROUND = (5, 10, 20)  # #050a14, the page background of the apps


def _feather_mask(size: int, radius: float, softness: float) -> Image.Image:
    """A round mask that fades out, so the crop has no hard square edge."""
    mask = Image.new("L", (size, size), 0)
    inner = radius * (1 - softness)
    centre = size / 2
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        (centre - inner, centre - inner, centre + inner, centre + inner), fill=255
    )
    return mask.filter(ImageFilter.GaussianBlur(radius * softness / 2))


def emblem(size: int, scale: float, *, transparent: bool = False) -> Image.Image:
    """The emblem on the background, filling `scale` of the square."""
    crop = Image.open(MASTER).convert("RGB").crop(EMBLEM_BOX)
    inner = max(1, round(size * scale))
    art = crop.resize((inner, inner), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (*BACKGROUND, 0 if transparent else 255))
    offset = (size - inner) // 2
    mask = _feather_mask(inner, inner / 2, 0.18)
    canvas.paste(art, (offset, offset), mask)
    return canvas


def full_logo(size: int) -> Image.Image:
    return (
        Image.open(MASTER).convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
    )


def _spot(width: int, height: int, wide: float, tall: float) -> Image.Image:
    """255 in the middle fading to 0: an ellipse `wide` x `tall` times the canvas."""
    radial = Image.radial_gradient("L").resize(
        (int(width * wide), int(height * tall)), Image.Resampling.BICUBIC
    )
    left, top = (radial.width - width) // 2, (radial.height - height) // 2
    return ImageChops.invert(radial.crop((left, top, left + width, top + height)))


def _backdrop(width: int, height: int) -> Image.Image:
    """The banner's background: light behind the centre, a network of nodes,
    two tilted orbits like the ones around the globe, and sparks. The middle
    stays clear for the logo. The same picture on every run."""
    scale = height / 360
    rng = random.Random(11)  # noqa: S311  # nosec B311  # decoration, not security
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    light = _spot(width, height, 1.5, 2.3).point(lambda v: int(v * 0.62))
    canvas = Image.composite(
        Image.new("RGB", (width, height), (16, 78, 158)), canvas, light
    )

    lines = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(lines)
    nodes = [
        (rng.uniform(-0.02, 1.02) * width, rng.uniform(-0.02, 1.02) * height)
        for _ in range(46)
    ]
    reach = 150 * scale
    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            gap = math.dist(a, b)
            if gap < reach:
                shade = (96, 196, 255, int(120 * (1 - gap / reach)))
                draw.line([a, b], fill=shade, width=max(1, round(scale)))
    for x, y in nodes:
        r = 1.6 * scale
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(170, 226, 255, 200))

    orbit = Image.new("RGBA", (width * 2, height * 2), (0, 0, 0, 0))
    ring = ImageDraw.Draw(orbit)
    for rx, ry, alpha in ((0.95, 0.30, 120), (0.78, 0.22, 80)):
        box = (
            width - rx * width,
            height - ry * height,
            width + rx * width,
            height + ry * height,
        )
        ring.ellipse(
            box, outline=(110, 205, 255, alpha), width=max(2, round(2 * scale))
        )
    for angle in (-13, 9):
        turned = orbit.rotate(angle, resample=Image.Resampling.BICUBIC)
        turned = turned.crop(
            (width // 2, height // 2, width // 2 + width, height // 2 + height)
        )
        lines = Image.alpha_composite(lines, turned)

    sparks = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    dots = ImageDraw.Draw(sparks)
    for _ in range(150):
        x, y = rng.uniform(0, width), rng.uniform(0, height)
        r = rng.choice((0.7, 0.9, 1.2, 1.8)) * scale
        dots.ellipse(
            (x - r, y - r, x + r, y + r), fill=(200, 238, 255, rng.randint(70, 220))
        )

    # Everything fades out towards the middle, where the logo sits.
    clear = _spot(width, height, 1.1, 1.3)
    keep = ImageChops.invert(clear).point(lambda v: min(255, int(v * 1.5)))
    for layer in (lines, sparks):
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), keep))
    halo = lines.filter(ImageFilter.GaussianBlur(3 * scale))
    soft = sparks.filter(ImageFilter.GaussianBlur(1.2 * scale))
    for layer in (halo, lines, soft, sparks):
        canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")

    edge = _spot(width, height, 1.25, 1.55).point(lambda v: 150 + int(v * 0.4))
    return Image.composite(canvas, Image.new("RGB", (width, height), BACKGROUND), edge)


def banner(width: int, height: int) -> Image.Image:
    """The full logo, a little smaller than the height, on the backdrop."""
    canvas = _backdrop(width, height)
    size = round(height * 0.98)
    mask = _feather_mask(size, size / 2, 0.07)
    canvas.paste(full_logo(size), ((width - size) // 2, (height - size) // 2), mask)
    return canvas


def save_png(image: Image.Image, path: Path) -> None:
    """Opaque icons as a dithered 256-colour palette: about 5x smaller,
    with no visible banding at icon sizes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    palette = image.convert("RGB").quantize(
        colors=256,
        method=Image.Quantize.FASTOCTREE,
        dither=Image.Dither.FLOYDSTEINBERG,
    )
    palette.save(path, "PNG", optimize=True)


def save_webp(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "WEBP", quality=86, method=6)


def save_jpeg(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path, "JPEG", quality=90, optimize=True, progressive=True)


def main() -> None:
    for app in ("pwa", "tma"):
        public = ROOT / app / "public"
        save_png(emblem(192, 0.94), public / "icons" / "icon-192.png")
        save_png(emblem(512, 0.94), public / "icons" / "icon-512.png")
        # Launchers cut maskable icons to a circle or squircle: keep a safe zone.
        save_png(emblem(512, 0.74), public / "icons" / "maskable-512.png")
        save_png(emblem(32, 1.0), public / "icons" / "favicon-32.png")
        save_png(emblem(48, 1.0), public / "icons" / "favicon-48.png")
        save_webp(
            emblem(128, 1.0, transparent=True), public / "brand" / "emblem-128.webp"
        )
        save_webp(full_logo(512), public / "brand" / "logo-512.webp")
    pwa = ROOT / "pwa" / "public"
    save_png(emblem(180, 0.86), pwa / "icons" / "apple-touch-icon.png")
    save_jpeg(banner(1200, 630), pwa / "brand" / "og-image.jpg")
    telegram = ROOT / "branding" / "telegram"
    save_jpeg(full_logo(640), telegram / "avatar-640.jpg")
    save_jpeg(banner(640, 360), telegram / "description-640x360.jpg")
    save_jpeg(banner(1280, 720), telegram / "welcome-1280x720.jpg")


if __name__ == "__main__":
    main()
