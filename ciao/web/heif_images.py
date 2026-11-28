"""HEIC/HEIF photo uploads: a JPEG the agent can see, beside the original.

Agent image inputs take PNG, JPEG, GIF and WebP, so an iPhone photo (HEIC)
cannot be sent as an image block. On macOS the system `sips` decodes it; the
script below draws the photo upright (EXIF orientation applied), no longer
than ``MAX_EDGE_PX`` on its long edge, into a JPEG that carries no camera
metadata (location included). Other hosts have no decoder Ciaobot ships, and
the original then stays attached as a plain file the agent reads by path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HEIF_EXTENSIONS = frozenset({".heic", ".heif", ".heics", ".heifs", ".hif"})

# ISO-BMFF major brands of HEIF stills and sequences. A browser can send a HEIC
# under a .jpg name or none at all, so the bytes decide, not only the suffix.
_HEIF_BRANDS = frozenset({
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1",
})

SIPS = Path("/usr/bin/sips")
CONVERT_TIMEOUT_S = 60
MAX_EDGE_PX = 2048
_OUTPUT_NAME = "converted.jpg"

# sips' JavaScript mode (`sips -j`, see `sips --man`) draws into an HTML-style
# canvas. A plain `sips -s format jpeg` keeps the sensor-order pixels and an
# EXIF orientation tag that not every model honours, so the photo is drawn
# upright here instead: one canvas transform per EXIF orientation 2-8.
_SIPS_SCRIPT = f"""
const MAX_EDGE = {MAX_EDGE_PX}
const image = sips.images[0]
const o = Number(image.getProperty('Orientation')) || 1
const scale = Math.min(1, MAX_EDGE / Math.max(image.size.width, image.size.height))
const w = Math.max(1, Math.round(image.size.width * scale))
const h = Math.max(1, Math.round(image.size.height * scale))
const swap = o >= 5 && o <= 8
const canvas = new Canvas(swap ? h : w, swap ? w : h)
const t = {{2: [-1, 0, 0, 1, w, 0], 3: [-1, 0, 0, -1, w, h], 4: [1, 0, 0, -1, 0, h],
  5: [0, 1, 1, 0, 0, 0], 6: [0, 1, -1, 0, h, 0], 7: [0, -1, -1, 0, h, w], 8: [0, -1, 1, 0, 0, w]}}[o]
if (t) canvas.transform(...t)
canvas.drawImage(image, 0, 0, w, h)
new Output(canvas, '{_OUTPUT_NAME}', 'public.jpeg').addToQueue()
"""


class HeifConversionError(RuntimeError):
    """The photo could not be turned into a JPEG on this host."""


class HeifNotConverted(ValueError):
    """A HEIF upload kept only as its original file.

    ``original`` is the saved upload; ``reason`` is plain text for the user.
    """

    def __init__(self, original: Path, reason: str) -> None:
        super().__init__(reason)
        self.original = original
        self.reason = reason


def is_heif(data: bytes, filename: str) -> bool:
    if Path(filename).suffix.lower() in HEIF_EXTENSIONS:
        return True
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS


def heif_extension(filename: str) -> str:
    """The suffix to keep the original under: its own, else ``.heic``."""
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in HEIF_EXTENSIONS else ".heic"


def convert_heif_to_jpeg(source: Path, target: Path) -> None:
    """Write ``source`` (HEIF) to ``target`` as an upright, bounded JPEG.

    Raises ``HeifConversionError`` with user-facing text on any failure; a
    partial ``target`` is never left behind.
    """
    if sys.platform != "darwin" or not SIPS.is_file():
        raise HeifConversionError("this computer has no HEIC converter (only macOS has one built in)")
    if not source.is_absolute():
        # sips reads a leading "-" as an option; an absolute path never has one.
        raise HeifConversionError("internal error: the photo path is not absolute")
    with tempfile.TemporaryDirectory(prefix="ciao-heif-") as tmp:
        script = Path(tmp) / "orient.js"
        script.write_text(_SIPS_SCRIPT, encoding="utf-8", newline="")
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        argv = [str(SIPS), "-j", str(script), "-o", str(out_dir), str(source)]
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=CONVERT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HeifConversionError(f"converting the photo took longer than {CONVERT_TIMEOUT_S} s") from exc
        except OSError as exc:
            raise HeifConversionError("the system image converter (sips) could not be started") from exc
        produced = out_dir / _OUTPUT_NAME
        if result.returncode != 0 or not produced.is_file():
            raise HeifConversionError("the system image converter (sips) could not read this photo")
        with produced.open("rb") as handle:
            if handle.read(3) != b"\xff\xd8\xff":
                raise HeifConversionError("the system image converter (sips) did not produce a JPEG")
        try:
            shutil.move(str(produced), str(target))
        except OSError as exc:
            Path(target).unlink(missing_ok=True)
            raise HeifConversionError("the converted photo could not be saved") from exc
    os.chmod(target, 0o644)
