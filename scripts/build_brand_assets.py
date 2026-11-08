"""Build every icon, banner and avatar from the one logo file.

Run after replacing branding/cyberdefency-logo.webp:

    python scripts/build_brand_assets.py

Small sizes use the emblem (shield on the globe) without the wordmark: letters
are unreadable at 32-192 px. Large surfaces use the full logo.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

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


def banner(width: int, height: int) -> Image.Image:
    """The full logo centred on a wide background with a soft blue glow."""
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    glow = Image.new("L", (width, height), 0)
    ImageDraw.Draw(glow).ellipse(
        (width * 0.2, -height * 0.3, width * 0.8, height * 1.3), fill=90
    )
    glow = glow.filter(ImageFilter.GaussianBlur(height / 4))
    canvas.paste(Image.new("RGB", (width, height), (14, 60, 120)), (0, 0), glow)
    logo = full_logo(height)
    mask = _feather_mask(height, height / 2, 0.06)
    canvas.paste(logo, ((width - height) // 2, 0), mask)
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
