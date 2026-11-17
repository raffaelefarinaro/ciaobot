#!/usr/bin/env python3
"""Finder layout for the release disk image, written without Finder.

A mounted DMG opens as the familiar install window because the volume root
carries a `.DS_Store` (window bounds, icon positions, view options) and a
background picture. This module writes both into the *staging folder* that
`make-dmg.sh` hands to `hdiutil create`, so the layout is produced headlessly
and byte for byte the same on every run: no mount, no Finder, no AppleScript.

The background is the hidden multi-resolution `.background.tiff` built with the
system `tiffutil` from `release/dmg/background.png` and `background@2x.png`.
It stays invisible in the window because of its dot prefix. The `.DS_Store`
names it through an Alias v2 record that carries the volume name and the path
`/.background.tiff`; Finder resolves it by that name and path, so the record
can be written before the image exists.

Only the Python standard library is used, and the code stays compatible with
the Python 3.9 of the Command Line Tools.

Usage:
    dmg_layout.py stage --volume-name V --app-name A.app --background P --background-2x P STAGE_DIR
    dmg_layout.py dsstore --volume-name V --app-name A.app     (raw bytes on stdout)
    dmg_layout.py geometry --app-name A.app                    (JSON on stdout)
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

VOLUME_NAME_DEFAULT = "Ciaobot"
BACKGROUND_NAME = ".background.tiff"
CONTENT_SIZE = (640, 400)
WINDOW_ORIGIN = (200, 120)
TITLEBAR = 28
ICON_SIZE = 128
TEXT_SIZE = 13
APP_CENTER = (160, 200)
APPLICATIONS_CENTER = (480, 200)
ARROW = ((240, 200), (400, 200))
PLATE_SIZE = (150, 30)
PLATE_Y = 282
PLATE_COLOR = "#6d739c"

_APP_NAME = re.compile(r"[^/\x00]+\.app")

# Buddy allocator layout (offsets are relative to file offset 4).
_HEADER_OFFSET, _HEADER_LOG = 0, 5
_DSDB_OFFSET, _DSDB_LOG = 32, 5
_ROOT_OFFSET, _ROOT_LOG = 2048, 11
_LEAF_OFFSET, _LEAF_LOG = 4096, 12
_SPACE_LOG = 31
_FILE_SIZE = 4 + 8192


def _check_app_name(app_name: str) -> None:
    if not _APP_NAME.fullmatch(app_name):
        raise ValueError("app name must be a bundle name ending in .app without a slash")


def _check_volume_name(volume_name: str) -> None:
    try:
        encoded = volume_name.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("volume name must be ASCII") from None
    if not 1 <= len(encoded) <= 27:
        raise ValueError("volume name must be 1 to 27 bytes")
    if any(b < 0x20 or b == 0x7F for b in encoded) or ":" in volume_name or "/" in volume_name:
        raise ValueError("volume name must not contain control characters, ':' or '/'")


def geometry(app_name: str) -> Dict[str, Any]:
    """Everything the art and the .DS_Store share, as JSON-safe values."""
    _check_app_name(app_name)
    plate_w, plate_h = PLATE_SIZE

    def plate(center_x: int) -> Dict[str, int]:
        return {"x": center_x - plate_w // 2, "y": PLATE_Y - plate_h // 2, "width": plate_w, "height": plate_h}

    return {
        "appName": app_name,
        "volumeName": VOLUME_NAME_DEFAULT,
        "backgroundName": BACKGROUND_NAME,
        "contentSize": list(CONTENT_SIZE),
        "windowBounds": _window_bounds(),
        "iconSize": ICON_SIZE,
        "textSize": TEXT_SIZE,
        "appCenter": list(APP_CENTER),
        "applicationsCenter": list(APPLICATIONS_CENTER),
        "arrow": {"from": list(ARROW[0]), "to": list(ARROW[1])},
        "plateColor": PLATE_COLOR,
        "plates": {"app": plate(APP_CENTER[0]), "applications": plate(APPLICATIONS_CENTER[0])},
    }


def _window_bounds() -> str:
    return "{{%d, %d}, {%d, %d}}" % (
        WINDOW_ORIGIN[0],
        WINDOW_ORIGIN[1],
        CONTENT_SIZE[0],
        CONTENT_SIZE[1] + TITLEBAR,
    )


def _pascal(text: str, width: int) -> bytes:
    raw = text.encode("ascii")
    return bytes([len(raw)]) + raw.ljust(width, b"\x00")


def _tag(tag: int, data: bytes) -> bytes:
    pad = b"\x00" if len(data) % 2 else b""
    return struct.pack(">hH", tag, len(data)) + data + pad


def background_alias(volume_name: str) -> bytes:
    """Alias v2 record naming `/.background.tiff` on `volume_name` by name and path."""
    _check_volume_name(volume_name)
    unicode_name = BACKGROUND_NAME.encode("utf-16-be")
    unicode_volume = volume_name.encode("utf-16-be")
    extras = b"".join(
        [
            _tag(0, volume_name.encode("ascii")),
            _tag(2, ("%s:%s" % (volume_name, BACKGROUND_NAME)).encode("ascii")),
            _tag(14, struct.pack(">H", len(BACKGROUND_NAME)) + unicode_name),
            _tag(15, struct.pack(">H", len(volume_name)) + unicode_volume),
            _tag(18, ("/" + BACKGROUND_NAME).encode("ascii")),
            _tag(19, ("/Volumes/" + volume_name).encode("ascii")),
            struct.pack(">hH", -1, 0),
        ]
    )
    header_length = 150
    header = struct.pack(
        ">4sHHH28sI2sHI64sII4s4sHHIH10s",
        b"\x00\x00\x00\x00",
        header_length + len(extras),
        2,
        0,
        _pascal(volume_name, 27),
        0,
        b"H+",
        5,
        2,
        _pascal(BACKGROUND_NAME, 63),
        0,
        0,
        b"\x00\x00\x00\x00",
        b"\x00\x00\x00\x00",
        0xFFFF,
        0xFFFF,
        0,
        0,
        b"\x00" * 10,
    )
    assert len(header) == header_length
    return header + extras


def _record(name: str, code: bytes, kind: bytes, value: bytes) -> Tuple[Tuple[str, bytes], bytes]:
    encoded = name.encode("utf-16-be")
    raw = struct.pack(">I", len(encoded) // 2) + encoded + code + kind + value
    return (name.casefold(), code), raw


def _blob(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _plist(value: Dict[str, Any]) -> bytes:
    return plistlib.dumps(value, fmt=plistlib.FMT_BINARY, sort_keys=True)


def _iloc(center: Tuple[int, int]) -> bytes:
    return _blob(struct.pack(">IIII", center[0], center[1], 0xFFFFFFFF, 0xFFFF0000))


def _free_lists(allocated: List[Tuple[int, int]]) -> List[List[int]]:
    """Buddy decomposition of [0, 2**31) minus the allocated (offset, log2 size) blocks."""
    lists: List[List[int]] = [[] for _ in range(32)]

    def walk(offset: int, log: int) -> None:
        end = offset + (1 << log)
        overlaps = [(o, g) for o, g in allocated if o < end and o + (1 << g) > offset]
        if not overlaps:
            lists[log].append(offset)
            return
        if any(o == offset and g == log for o, g in overlaps):
            return
        walk(offset, log - 1)
        walk(offset + (1 << (log - 1)), log - 1)

    walk(0, _SPACE_LOG)
    return lists


def build_dsstore(volume_name: str, app_name: str) -> bytes:
    """The volume root's .DS_Store: 6 records in one B-tree leaf, deterministic."""
    _check_volume_name(volume_name)
    _check_app_name(app_name)

    bwsp = {
        "ContainerShowSidebar": False,
        "PreviewPaneVisibility": False,
        "ShowPathbar": False,
        "ShowSidebar": False,
        "ShowStatusBar": False,
        "ShowTabView": False,
        "ShowToolbar": False,
        "SidebarWidth": 0,
        "WindowBounds": _window_bounds(),
    }
    icvp = {
        "arrangeBy": "none",
        "backgroundColorBlue": 1.0,
        "backgroundColorGreen": 1.0,
        "backgroundColorRed": 1.0,
        "backgroundImageAlias": background_alias(volume_name),
        "backgroundType": 2,
        "gridOffsetX": 0.0,
        "gridOffsetY": 0.0,
        "gridSpacing": 100.0,
        "iconSize": float(ICON_SIZE),
        "labelOnBottom": True,
        "scrollPositionX": 0.0,
        "scrollPositionY": 0.0,
        "showIconPreview": True,
        "showItemInfo": False,
        "textSize": float(TEXT_SIZE),
        "viewOptionsVersion": 1,
    }
    records = [
        _record(".", b"bwsp", b"blob", _blob(_plist(bwsp))),
        _record(".", b"icvl", b"type", b"icnv"),
        _record(".", b"icvp", b"blob", _blob(_plist(icvp))),
        _record(".", b"vSrn", b"long", struct.pack(">I", 1)),
        _record("Applications", b"Iloc", b"blob", _iloc(APPLICATIONS_CENTER)),
        _record(app_name, b"Iloc", b"blob", _iloc(APP_CENTER)),
    ]
    records.sort(key=lambda item: item[0])

    leaf = struct.pack(">II", 0, len(records)) + b"".join(raw for _, raw in records)
    if len(leaf) > 1 << _LEAF_LOG:
        raise ValueError("layout does not fit in one .DS_Store page")

    dsdb = struct.pack(">IIIII", 2, 0, len(records), 1, 0x1000)

    allocated = [
        (_HEADER_OFFSET, _HEADER_LOG),
        (_DSDB_OFFSET, _DSDB_LOG),
        (_ROOT_OFFSET, _ROOT_LOG),
        (_LEAF_OFFSET, _LEAF_LOG),
    ]
    addresses = [
        _ROOT_OFFSET | _ROOT_LOG,
        _DSDB_OFFSET | _DSDB_LOG,
        _LEAF_OFFSET | _LEAF_LOG,
    ]
    root = struct.pack(">II", len(addresses), 0)
    root += b"".join(struct.pack(">I", a) for a in addresses + [0] * (256 - len(addresses)))
    root += struct.pack(">I", 1) + bytes([4]) + b"DSDB" + struct.pack(">I", 1)
    for offsets in _free_lists(allocated):
        root += struct.pack(">I", len(offsets)) + b"".join(struct.pack(">I", o) for o in offsets)
    if len(root) > 1 << _ROOT_LOG:
        raise ValueError("allocator root block overflow")

    body = bytearray(_FILE_SIZE)
    body[0:8] = b"\x00\x00\x00\x01Bud1"
    struct.pack_into(">III", body, 8, _ROOT_OFFSET, 1 << _ROOT_LOG, _ROOT_OFFSET)
    for offset, data in ((_DSDB_OFFSET, dsdb), (_ROOT_OFFSET, root), (_LEAF_OFFSET, leaf)):
        body[4 + offset : 4 + offset + len(data)] = data
    return bytes(body)


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def stage(stage_dir: Path, volume_name: str, app_name: str, background: Path, background_2x: Path) -> None:
    """Write `.DS_Store` and `.background.tiff` into the staging folder."""
    _check_volume_name(volume_name)
    _check_app_name(app_name)
    stage_dir = Path(stage_dir)
    if os.path.islink(stage_dir) or not stage_dir.is_dir():
        raise ValueError("stage is not a real directory: %s" % stage_dir)
    app = stage_dir / app_name
    if os.path.islink(app) or not app.is_dir():
        raise ValueError("stage has no %s" % app_name)
    applications = stage_dir / "Applications"
    if not os.path.islink(applications) or os.readlink(applications) != "/Applications":
        raise ValueError("stage has no Applications link to /Applications")
    target = stage_dir / BACKGROUND_NAME
    dsstore = stage_dir / ".DS_Store"
    for existing in (target, dsstore):
        if os.path.lexists(existing):
            raise ValueError("stage already has %s" % existing.name)
    data = build_dsstore(volume_name, app_name)

    fd, tmp_name = tempfile.mkstemp(prefix=".background-", suffix=".tmp", dir=str(stage_dir))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        subprocess.run(
            ["tiffutil", "-cathidpicheck", str(background), str(background_2x), "-out", str(tmp)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.chmod(tmp, 0o644)
        # A hard link fails if the target appeared meanwhile, so it never overwrites.
        os.link(tmp, target)
    finally:
        tmp.unlink()
    _write_exclusive(dsstore, data)


def _name_arg(check: Any) -> Any:
    def parse(value: str) -> str:
        try:
            check(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(str(error)) from None
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dmg_layout.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    volume = _name_arg(_check_volume_name)
    app = _name_arg(_check_app_name)

    staging = sub.add_parser("stage", help="write the layout into a staging folder")
    staging.add_argument("--volume-name", required=True, type=volume)
    staging.add_argument("--app-name", required=True, type=app)
    staging.add_argument("--background", required=True, type=Path)
    staging.add_argument("--background-2x", required=True, type=Path)
    staging.add_argument("stage_dir", type=Path)

    dsstore = sub.add_parser("dsstore", help="write the .DS_Store bytes to stdout")
    dsstore.add_argument("--volume-name", required=True, type=volume)
    dsstore.add_argument("--app-name", required=True, type=app)

    geo = sub.add_parser("geometry", help="write the shared geometry as JSON to stdout")
    geo.add_argument("--app-name", required=True, type=app)
    return parser


def main(argv: List[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage":
            stage(args.stage_dir, args.volume_name, args.app_name, args.background, args.background_2x)
        elif args.command == "dsstore":
            sys.stdout.buffer.write(build_dsstore(args.volume_name, args.app_name))
            sys.stdout.buffer.flush()
        else:
            print(json.dumps(geometry(args.app_name), indent=2, sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("dmg-layout: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
