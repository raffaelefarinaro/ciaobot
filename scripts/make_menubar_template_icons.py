"""Generate monochrome macOS template icons for the menu bar app.

macOS status bar icons are "template images": pure black with an alpha
channel, tinted by the system to match the menu bar appearance. This
script derives them from the colored Ciaobot faces by keeping dark
pixels (hat, glasses, beard) opaque black and dropping light pixels
(skin, outline, teeth) to transparent, then downscaling to 2x menu bar
resolution.

Usage: python3 scripts/make_menubar_template_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

STATIC = Path(__file__).resolve().parent.parent / "ciao" / "web" / "static"

# Pixels darker than this luminance (0-255) become black; the rest
# transparent. Chosen so the pink outline and skin drop out while the
# hat, glasses, and beard stay.
LUMINANCE_THRESHOLD = 110

# 2x the 20pt size rumps gives status bar icons, for retina crispness.
OUTPUT_SIZE = 40


def to_template(source: Path, destination: Path) -> None:
    image = Image.open(source).convert("RGBA")
    pixels = image.load()
    width, height = image.size
    mask = Image.new("L", image.size, 0)
    mask_pixels = mask.load()
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            if a > 128 and luminance < LUMINANCE_THRESHOLD:
                mask_pixels[x, y] = 255
    # Crop to the face and pad back to a square so the icon uses the
    # full status bar height regardless of margins in the source art.
    left, top, right, bottom = mask.getbbox()
    side = max(right - left, bottom - top)
    margin = side // 16
    side += 2 * margin
    square = Image.new("L", (side, side), 0)
    square.paste(
        mask.crop((left, top, right, bottom)),
        (margin + (side - 2 * margin - (right - left)) // 2, margin),
    )
    mask = square.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS)
    template = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    template.putalpha(mask)
    template.save(destination)
    print(f"{source.name} -> {destination.name}")


def main() -> None:
    to_template(STATIC / "face.png", STATIC / "face_template.png")
    to_template(STATIC / "face_scared.png", STATIC / "face_scared_template.png")


if __name__ == "__main__":
    main()
