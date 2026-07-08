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

# Number of frames in the "spinning head" animation the menu bar plays while
# a chat is working. Kept in sync with menubar.SPIN_FRAME_COUNT.
SPIN_FRAME_COUNT = 12


def _face_mask(source: Path) -> Image.Image:
    """Monochrome alpha mask of the face, cropped and padded to a square.

    The mask is the pre-resize square (native resolution) so callers can
    resize it directly or rotate it for the spin frames without compounding
    resampling loss.
    """

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
    return square


def _save_template(mask: Image.Image, destination: Path) -> None:
    resized = mask.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS)
    template = Image.new("RGBA", resized.size, (0, 0, 0, 0))
    template.putalpha(resized)
    template.save(destination)


def to_template(source: Path, destination: Path) -> None:
    _save_template(_face_mask(source), destination)
    print(f"{source.name} -> {destination.name}")


def to_spin_frames(source: Path, deploy: Path, *, frames: int = SPIN_FRAME_COUNT) -> None:
    """Render a rotating-head animation as numbered template frames.

    Each frame rotates the face mask by a full turn's fraction. The mask is
    padded to its diagonal first so no part of the head clips at intermediate
    angles; the trade-off is that the spinning head sits marginally smaller
    than the static icon, which reads fine at menu bar size.
    """

    mask = _face_mask(source)
    diagonal = int(mask.width * 1.4143) + 1
    padded = Image.new("L", (diagonal, diagonal), 0)
    offset = (diagonal - mask.width) // 2
    padded.paste(mask, (offset, offset))
    for index in range(frames):
        angle = 360.0 * index / frames
        rotated = padded.rotate(angle, resample=Image.BICUBIC, expand=False)
        name = f"face_spin_{index:02d}.png"
        _save_template(rotated, deploy / name)
        print(f"{source.name} -> {name}")


def main() -> None:
    deploy = STATIC.parents[1] / "stock" / "deploy"
    to_template(STATIC / "face.png", deploy / "face_template.png")
    to_template(STATIC / "face_scared.png", deploy / "face_scared_template.png")
    to_spin_frames(STATIC / "face.png", deploy)


if __name__ == "__main__":
    main()
