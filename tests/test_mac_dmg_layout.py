"""The disk image's Finder layout writer (mac/scripts/dmg_layout.py).

The file reads the .DS_Store and Alias bytes with its own small parser, so the
module is never used to check itself.
"""
import importlib.util
import json
import plistlib
import struct
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "mac/scripts/dmg_layout.py"
SPEC = importlib.util.spec_from_file_location("dmg_layout", MODULE_PATH)
assert SPEC and SPEC.loader
dmg_layout = importlib.util.module_from_spec(SPEC)
sys.modules["dmg_layout"] = dmg_layout
SPEC.loader.exec_module(dmg_layout)


def read_dsstore(data: bytes):
    """Return (allocated blocks, free lists, DSDB fields, records)."""
    assert data[:8] == b"\x00\x00\x00\x01Bud1"
    root_offset, root_size, root_again = struct.unpack(">III", data[8:20])
    assert root_offset == root_again

    def block(address: int) -> bytes:
        offset, size = address & ~0x1F, 1 << (address & 0x1F)
        return data[4 + offset : 4 + offset + size]

    root = data[4 + root_offset : 4 + root_offset + root_size]
    count, _ = struct.unpack(">II", root[:8])
    addresses = struct.unpack(">256I", root[8 : 8 + 1024])
    pos = 8 + 1024
    (toc_count,) = struct.unpack(">I", root[pos : pos + 4])
    pos += 4
    toc = {}
    for _ in range(toc_count):
        name_len = root[pos]
        name = root[pos + 1 : pos + 1 + name_len]
        (value,) = struct.unpack(">I", root[pos + 1 + name_len : pos + 5 + name_len])
        toc[name] = value
        pos += 5 + name_len
    free = []
    for log in range(32):
        (n,) = struct.unpack(">I", root[pos : pos + 4])
        pos += 4
        free.extend((o, log) for o in struct.unpack(">%dI" % n, root[pos : pos + 4 * n]))
        pos += 4 * n
    allocated = [(8 * 0, 5)]  # the header: Bud1 plus the 28 bytes after it
    allocated += [(a & ~0x1F, a & 0x1F) for a in addresses[:count]]

    dsdb = struct.unpack(">IIIII", block(addresses[toc[b"DSDB"]])[:20])
    leaf = block(addresses[dsdb[0]])
    pointer, n_records = struct.unpack(">II", leaf[:8])
    assert pointer == 0
    pos = 8
    records = []
    for _ in range(n_records):
        (units,) = struct.unpack(">I", leaf[pos : pos + 4])
        name = leaf[pos + 4 : pos + 4 + 2 * units].decode("utf-16-be")
        pos += 4 + 2 * units
        code, kind = leaf[pos : pos + 4], leaf[pos + 4 : pos + 8]
        pos += 8
        if kind == b"blob":
            (length,) = struct.unpack(">I", leaf[pos : pos + 4])
            value = leaf[pos + 4 : pos + 4 + length]
            pos += 4 + length
        elif kind == b"long":
            (value,) = struct.unpack(">I", leaf[pos : pos + 4])
            pos += 4
        elif kind == b"type":
            value = leaf[pos : pos + 4]
            pos += 4
        else:
            raise AssertionError("unexpected record type %r" % kind)
        records.append((name, code.decode(), kind.decode(), value))
    return allocated, free, dsdb, records


def parse_alias(data: bytes):
    (_, length, version, kind) = struct.unpack(">4sHHH", data[:10])
    vol_len = data[10]
    volume = data[11 : 11 + vol_len].decode()
    name_len = data[10 + 28 + 4 + 2 + 2 + 4]
    name_start = 10 + 28 + 4 + 2 + 2 + 4 + 1
    filename = data[name_start : name_start + name_len].decode()
    extras = {}
    pos = 150
    while True:
        tag, size = struct.unpack(">hH", data[pos : pos + 4])
        pos += 4
        if tag == -1:
            break
        extras[tag] = data[pos : pos + size]
        pos += size + (size % 2)
    return length, version, kind, volume, filename, extras, pos


def contrast(hex_color: str, other: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    a, b = sorted((luminance(hex_color), luminance(other)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def decode(name_records, name, code):
    for record_name, record_code, _, value in name_records:
        if (record_name, record_code) == (name, code):
            return value
    raise AssertionError((name, code))


DSSTORE = dmg_layout.build_dsstore("Ciaobot", "Ciaobot.app")


def test_dsstore_is_deterministic():
    assert dmg_layout.build_dsstore("Ciaobot", "Ciaobot.app") == DSSTORE
    assert len(DSSTORE) == 4 + 8192
    assert DSSTORE.startswith(b"\x00\x00\x00\x01Bud1")


def test_dsstore_reads_back_the_layout():
    _, _, dsdb, records = read_dsstore(DSSTORE)
    assert [(r[0], r[1]) for r in records] == [
        (".", "bwsp"),
        (".", "icvl"),
        (".", "icvp"),
        (".", "vSrn"),
        ("Applications", "Iloc"),
        ("Ciaobot.app", "Iloc"),
    ]
    assert dsdb == (2, 0, 6, 1, 0x1000)
    tail = (0xFFFFFFFF, 0xFFFF0000)
    assert struct.unpack(">IIII", decode(records, "Ciaobot.app", "Iloc")) == (160, 200) + tail
    assert struct.unpack(">IIII", decode(records, "Applications", "Iloc")) == (480, 200) + tail
    assert decode(records, ".", "icvl") == b"icnv"
    assert decode(records, ".", "vSrn") == 1


def test_buddy_blocks_tile_the_address_space():
    allocated, free, _, _ = read_dsstore(DSSTORE)
    blocks = sorted(allocated + free)
    assert len(allocated) == 4
    end = 0
    for offset, log in blocks:
        size = 1 << log
        assert offset % size == 0, (offset, log)
        assert offset == end, "blocks overlap or leave a gap at %d" % end
        end = offset + size
    assert end == 2**31


def test_window_hides_all_chrome():
    _, _, _, records = read_dsstore(DSSTORE)
    bwsp = plistlib.loads(decode(records, ".", "bwsp"))
    for key in (
        "ShowToolbar",
        "ShowSidebar",
        "ShowStatusBar",
        "ShowPathbar",
        "ShowTabView",
        "ContainerShowSidebar",
        "PreviewPaneVisibility",
    ):
        assert bwsp[key] is False, key
    assert bwsp["SidebarWidth"] == 0
    # 400 pt of art plus the 28 pt title bar Finder counts in the frame.
    assert bwsp["WindowBounds"] == "{{200, 120}, {640, 428}}"


def test_icon_view_uses_the_background_alias():
    _, _, _, records = read_dsstore(DSSTORE)
    icvp = plistlib.loads(decode(records, ".", "icvp"))
    assert icvp["backgroundType"] == 2
    assert icvp["iconSize"] == 128.0
    assert icvp["textSize"] == 13.0
    assert icvp["arrangeBy"] == "none"
    assert icvp["backgroundImageAlias"] == dmg_layout.background_alias("Ciaobot")


def test_background_alias_names_the_hidden_file():
    data = dmg_layout.background_alias("Ciaobot")
    length, version, kind, volume, filename, extras, end = parse_alias(data)
    assert (version, kind) == (2, 0)
    assert length == len(data) == end
    assert volume == "Ciaobot"
    assert filename == ".background.tiff"
    assert extras[2] == b"Ciaobot:.background.tiff"
    assert extras[18] == b"/.background.tiff"
    assert extras[19] == b"/Volumes/Ciaobot"
    assert data[-4:] == struct.pack(">hH", -1, 0)


def test_icons_and_plates_fit_the_window():
    geo = dmg_layout.geometry("Ciaobot.app")
    width, height = geo["contentSize"]
    assert (width, height) == (640, 400)
    half = geo["iconSize"] // 2
    squares = []
    for key in ("appCenter", "applicationsCenter"):
        cx, cy = geo[key]
        squares.append((cx - half, cy - half, cx + half, cy + half))
    plates = [
        (p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"]) for p in geo["plates"].values()
    ]
    for left, top, right, bottom in squares + plates:
        assert 0 <= left < right <= width and 0 <= top < bottom <= height
    (ax, _), (bx, _) = geo["arrow"]["from"], geo["arrow"]["to"]
    assert squares[0][2] < ax < bx < squares[1][0]
    first, second = plates
    assert first[2] <= second[0], "plates must not overlap"


def test_plate_colour_is_legible_in_light_and_dark():
    assert contrast(dmg_layout.PLATE_COLOR, "#000000") >= 4.5
    assert contrast(dmg_layout.PLATE_COLOR, "#ffffff") >= 4.5


@pytest.mark.parametrize("app_name", ["Ciaobot", "a/b.app", ""])
def test_rejects_bad_app_names(app_name):
    with pytest.raises(ValueError):
        dmg_layout.build_dsstore("Ciaobot", app_name)
    with pytest.raises(ValueError):
        dmg_layout.geometry(app_name)


@pytest.mark.parametrize("volume_name", ["x" * 28, "a:b", "", "a/b"])
def test_rejects_bad_volume_names(volume_name):
    with pytest.raises(ValueError):
        dmg_layout.build_dsstore(volume_name, "Ciaobot.app")
    with pytest.raises(ValueError):
        dmg_layout.background_alias(volume_name)


def run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-I", str(MODULE_PATH), *args], capture_output=True, env=env
    )


def test_cli_exit_codes_and_output():
    result = run_cli("dsstore", "--volume-name", "Ciaobot", "--app-name", "Ciaobot.app")
    assert result.returncode == 0 and result.stdout == DSSTORE
    geometry = run_cli("geometry", "--app-name", "Ciaobot.app")
    assert json.loads(geometry.stdout)["appCenter"] == [160, 200]
    assert run_cli("dsstore", "--volume-name", "Ciaobot", "--app-name", "Ciaobot").returncode == 2
    assert run_cli("dsstore").returncode == 2
    refused = run_cli(
        "stage",
        "--volume-name",
        "Ciaobot",
        "--app-name",
        "Ciaobot.app",
        "--background",
        "a.png",
        "--background-2x",
        "b.png",
        "/nonexistent-stage-dir",
    )
    assert refused.returncode == 1
    assert refused.stderr.startswith(b"dmg-layout: ")


@pytest.fixture
def fake_tiffutil(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("the fake tiffutil is a shell script")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "tiffutil.log"
    tool = bin_dir / "tiffutil"
    tool.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n'
        'for last; do :; done\nprintf tiff > "$last"\n'.format(log=log)
    )
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", "%s:%s" % (bin_dir, "/usr/bin:/bin"))
    return log


def make_stage(tmp_path, with_app=True, with_link=True):
    stage = tmp_path / "stage"
    stage.mkdir()
    if with_app:
        (stage / "Ciaobot.app").mkdir()
    if with_link:
        (stage / "Applications").symlink_to("/Applications")
    return stage


def test_stage_writes_layout_with_a_fake_tiffutil(tmp_path, fake_tiffutil):
    stage = make_stage(tmp_path)
    one, two = tmp_path / "background.png", tmp_path / "background@2x.png"
    dmg_layout.stage(stage, "Ciaobot", "Ciaobot.app", one, two)

    words = fake_tiffutil.read_text().split()
    assert words[:3] == ["-cathidpicheck", str(one), str(two)]
    assert words[3] == "-out"
    assert Path(words[4]).parent == stage
    assert (stage / ".background.tiff").read_bytes() == b"tiff"
    assert (stage / ".DS_Store").read_bytes() == DSSTORE
    assert sorted(p.name for p in stage.iterdir()) == [
        ".DS_Store",
        ".background.tiff",
        "Applications",
        "Ciaobot.app",
    ], "the temporary picture must not be left behind"


def test_stage_refuses_existing_targets_and_symlinked_stage(tmp_path, fake_tiffutil):
    args = ("Ciaobot", "Ciaobot.app", tmp_path / "a.png", tmp_path / "b.png")

    existing = make_stage(tmp_path)
    (existing / ".DS_Store").write_bytes(b"mine")
    with pytest.raises(ValueError):
        dmg_layout.stage(existing, *args)
    assert (existing / ".DS_Store").read_bytes() == b"mine"

    real = tmp_path / "real"
    real.mkdir()
    (real / "Ciaobot.app").mkdir()
    (real / "Applications").symlink_to("/Applications")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        dmg_layout.stage(link, *args)

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError):
        dmg_layout.stage(other, *args)
    (other / "Ciaobot.app").mkdir()
    with pytest.raises(ValueError):
        dmg_layout.stage(other, *args)

    assert not fake_tiffutil.exists(), "tiffutil must never run for a refused stage"
    assert not (real / ".DS_Store").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="needs the system tiffutil")
def test_real_tiffutil_joins_the_art_into_one_retina_picture(tmp_path):
    root = Path(__file__).parents[1] / "mac/release/dmg"
    stage = make_stage(tmp_path)
    dmg_layout.stage(
        stage, "Ciaobot", "Ciaobot.app", root / "background.png", root / "background@2x.png"
    )
    info = subprocess.run(
        ["tiffutil", "-info", str(stage / ".background.tiff")], capture_output=True, text=True
    ).stdout
    assert "Image Width: 640 Image Length: 400" in info
    assert "Image Width: 1280 Image Length: 800" in info
