import gzip
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "mac/scripts/engine_artifact.py"
SPEC = importlib.util.spec_from_file_location("engine_artifact", MODULE_PATH)
assert SPEC and SPEC.loader
engine_artifact = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine_artifact)


def archive(entries: list[tuple[str, bytes, str]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data.decode()
            else:
                info.size = len(data)
                info.mode = 0o755 if name.endswith("python3.12") else 0o644
            tf.addfile(info, io.BytesIO(data) if kind == "file" else None)
    return gzip.compress(raw.getvalue())


def typed_archive(entries: list[tuple[str, bytes, bytes, int]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data, kind, mode in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = mode
            info.size = len(data) if kind in (tarfile.REGTYPE, tarfile.AREGTYPE) else 0
            info.linkname = data.decode() if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            tf.addfile(info, io.BytesIO(data) if info.size else None)
    return gzip.compress(raw.getvalue())


def rechecksum(header: bytearray) -> None:
    block = bytearray(header[:512])
    block[148:156] = b"        "
    block[148:156] = f"{sum(block):06o}\0 ".encode()
    header[:512] = block


def rewrite_tar(payload: bytes, mutate) -> bytes:
    raw = bytearray(gzip.decompress(payload))
    mutate(raw)
    return gzip.compress(bytes(raw))


def rechecksum_at(raw: bytearray, offset: int) -> None:
    """Recompute the ustar checksum for the 512-byte header starting at `offset`."""
    header = bytearray(raw[offset:offset + 512])
    rechecksum(header)
    raw[offset:offset + 512] = header


def header_offsets(raw, name: bytes) -> list[int]:
    """Absolute offsets of every 512-byte header whose full name field equals `name`."""
    return [
        offset for offset in range(0, len(raw), 512)
        if raw[offset:offset + 100] == name + b"\0" * (100 - len(name))
    ]


def _set_field(raw: bytearray, offset: int, width: int, effective: bytes, suffix: bytes) -> None:
    """Overwrite one exact-width ustar string field in place and re-checksum its header."""
    padding = b"\0" * (width - len(effective) - len(suffix) - 1)
    assert len(effective) + len(suffix) + 1 <= width
    raw[offset:offset + width] = effective + b"\0" + suffix + padding
    rechecksum_at(raw, offset - offset % 512)


def invoke(tmp_path: Path, payload: bytes, *, sha: str | None = None, size: int | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    destination = parent / "out"
    engine_artifact.verify_and_extract(
        source, destination,
        sha256=sha or hashlib.sha256(payload).hexdigest(),
        size=len(payload) if size is None else size,
    )
    return destination


def assert_rejection_is_isolated(base: Path, payload: bytes) -> None:
    """Integration-rejection hygiene: source, outside sentinel and run-owned
    scratch are untouched, and nothing was published at the destination."""
    assert (base / "artifact.tgz").read_bytes() == payload
    assert (base / "outside-sentinel").read_text() == "keep"
    parent = base / "private"
    assert not list(parent.glob(".engine-artifact-*"))
    assert not (parent / "out").exists()


def test_valid_prefix_extracts_files_and_internal_aliases(tmp_path):
    payload = archive([
        ("python/bin/python3.12", b"runtime", "file"),
        ("python/bin/python3", b"python3.12", "symlink"),
        ("python/lib/site.py", b"module", "file"),
        ("python/lib/current", b".", "symlink"),
    ])
    result = invoke(tmp_path, payload)
    assert (result / "bin/python3.12").read_bytes() == b"runtime"
    assert (result / "bin/python3.12").stat().st_mode & 0o111
    assert os.readlink(result / "bin/python3") == "python3.12"
    assert os.readlink(result / "lib/current") == "."
    assert (result / "lib").is_dir()
    assert (result / "lib/site.py").stat().st_mode & 0o777 == 0o644
    assert (result / "bin/python3.12").stat().st_mode & 0o777 == 0o755
    assert (result / "bin").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    ("field", "offset", "width", "owner", "entries", "expected"),
    [
        ("name", 0, 100, None, [("python/a", b"ok", "file")], "a"),
        ("prefix", 345, 155, None, [("a", b"ok", "file")], "a"),
        ("linkname", 157, 100, b"python/alias", [
            ("python/target", b"ok", "file"),
            ("python/alias", b"target", "symlink"),
        ], "alias"),
    ],
)
@pytest.mark.parametrize(
    "suffix",
    [
        b"stale",
        b"../outside",
        b"\xff\xfe",
        pytest.param(b"back\\slash", id="backslash"),
        pytest.param(b"ctl\x01\x7f\x1f", id="control"),
    ],
)
def test_ustar_first_nul_suffix_compatibility(
    tmp_path, field, offset, width, owner, entries, expected, suffix
):
    payload = archive(entries)
    effective = {"name": b"python/a", "prefix": b"python", "linkname": b"target"}[field]

    def mutate(raw):
        base = 0 if owner is None else header_offsets(raw, owner)[0]
        _set_field(raw, base + offset, width, effective, suffix)

    payload = rewrite_tar(payload, mutate)
    result = invoke(tmp_path, payload)
    if field == "linkname":
        assert os.readlink(result / "alias") == "target"
        assert (result / "target").read_bytes() == b"ok"
    else:
        assert (result / expected).read_bytes() == b"ok"
    # Suffix bytes never create a path or alias of their own.
    names = sorted(str(path.relative_to(result)) for path in result.rglob("*"))
    assert names == (["alias", "target"] if field == "linkname" else ["a"])


@pytest.mark.parametrize("field", ["prefix", "linkname"])
@pytest.mark.parametrize(
    "suffix",
    [b"stale", b"\xff\xfe", b"back\\slash\x03"],
    ids=["ascii", "invalid-utf8", "backslash-control"],
)
def test_multibyte_effective_prefix_and_link_target_with_stale_suffix(tmp_path, field, suffix):
    if field == "prefix":
        effective = "python/\u00e9dir".encode()
        payload = archive([("a", b"ok", "file")])
        payload = _with_field_suffix(payload, 345, 155, effective, suffix)
        result = invoke(tmp_path, payload)
        assert (result / "\u00e9dir" / "a").read_bytes() == b"ok"
        names = sorted(str(path.relative_to(result)) for path in result.rglob("*"))
        assert names == ["\u00e9dir", "\u00e9dir/a"]
    else:
        effective = "lib/\u00e9.py".encode()
        payload = archive([
            ("python/lib/\u00e9.py", b"ok", "file"),
            ("python/alias", effective, "symlink"),
        ])
        payload = rewrite_tar(payload, lambda raw: _set_field(
            raw, header_offsets(raw, b"python/alias")[0] + 157, 100, effective, suffix
        ))
        result = invoke(tmp_path, payload)
        assert (result / "lib" / "\u00e9.py").read_bytes() == b"ok"
        assert os.readlink(result / "alias") == "lib/\u00e9.py"
        names = sorted(str(path.relative_to(result)) for path in result.rglob("*"))
        assert names == ["alias", "lib", "lib/\u00e9.py"]


def test_accepted_stale_suffixes_leave_no_diagnostics_or_extra_paths(tmp_path):
    payload = _with_field_suffix(
        archive([("python/a", b"x", "file")]), 0, 100, b"python/a", b"back\\slash\x01\x1f"
    )
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", str(source), "--destination",
         str(parent / "out"), "--sha256", hashlib.sha256(payload).hexdigest(),
         "--size", str(len(payload))],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert result.stderr == "" and result.stdout == ""
    names = sorted(str(path.relative_to(parent / "out")) for path in (parent / "out").rglob("*"))
    assert names == ["a"]


def _with_field_suffix(payload, offset, width, effective, suffix=b"stale"):
    """Rewrite one ustar string field at absolute `offset` and re-checksum the header that holds it."""
    def mutate(raw):
        _set_field(raw, offset, width, effective, suffix)
    return rewrite_tar(payload, mutate)


def test_effective_ustar_values_agree_at_manifest_write_seam(tmp_path, monkeypatch):
    raw = bytearray(gzip.decompress(typed_archive([
        ("python/lib/é.py", b"ok", tarfile.REGTYPE, 0o644),
        ("python/alias", "lib/é.py".encode(), tarfile.SYMTYPE, 0o777),
        ("python/prefixdir/", b"", tarfile.DIRTYPE, 0o755),
        ("python/prefixdir/prefixed", b"ok", tarfile.REGTYPE, 0o644),
    ])))
    alias_header, dir_header, prefixed_header = (
        header_offsets(raw, name)[0]
        for name in (b"python/alias", b"python/prefixdir/", b"python/prefixdir/prefixed")
    )
    # A valid first-NUL suffix in the name and link fields, plus a prefix-encoded
    # member and an explicit trailing-slash directory carrying the same stale tails.
    _set_field(raw, 0, 100, "python/lib/é.py".encode(), b"stale")
    _set_field(raw, alias_header + 157, 100, "lib/é.py".encode(), b"../outside")
    _set_field(raw, dir_header, 100, b"python/prefixdir/", b"stale")
    _set_field(raw, prefixed_header, 100, b"prefixed", b"stale")
    _set_field(raw, prefixed_header + 345, 155, b"python/prefixdir", b"stale")
    payload = gzip.compress(bytes(raw))
    captured_fields, captured_decodes, captured_records, writes = [], [], [], []
    original_field, original_write = engine_artifact._field, engine_artifact._write_tree

    def observe_field(value, limit):
        result = original_field(value, limit)
        # Production `_field` per header: raw field bytes and the decoded value
        # preflight actually used, paired later with the member's TarInfo.
        captured_fields.append((bytes(value), result))
        captured_decodes.append(result)
        return result

    def observe_write(root, records, destination):
        captured_records.extend(records)
        writes.append(True)
        return original_write(root, records, destination)

    monkeypatch.setattr(engine_artifact, "_field", observe_field)
    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    result = invoke(tmp_path, payload)
    assert "python/lib/é.py" in captured_decodes
    assert "lib/é.py" in captured_decodes
    assert "python/prefixdir/" in captured_decodes
    assert "python/prefixdir" in captured_decodes
    assert "prefixed" in captured_decodes
    assert writes == [True]
    file_record = next(record for record in captured_records if record[2].isfile())
    link_record = next(record for record in captured_records if record[2].issym())
    prefix_record = next(
        record for record in captured_records if record[2].name == "python/prefixdir/prefixed"
    )
    dir_record = next(record for record in captured_records if record[2].isdir())
    assert "/".join(file_record[0]) == file_record[2].name == "python/lib/é.py"
    assert link_record[2].linkname == link_record[1].target == "lib/é.py"
    assert "/".join(prefix_record[0]) == prefix_record[2].name == "python/prefixdir/prefixed"
    # Directory members keep the trailing slash in TarInfo; the comparison applies
    # the helper's own directory normalization instead of raw concatenation.
    assert "/".join(dir_record[0]) == dir_record[2].name.rstrip("/") == "python/prefixdir"

    # Direct per-header pairing: preflight runs one name and one prefix decode
    # per header (plus a link decode for symlinks), in archive order, so each
    # record captured at write maps onto the production `_field` values for its
    # own header. Join and compare exactly what the helper, stdlib TarInfo and
    # the manifest records agree on at that seam.
    cursor = 0
    for parts, node, info in captured_records:
        (name_raw, name_dec), (prefix_raw, prefix_dec) = captured_fields[cursor:cursor + 2]
        cursor += 2
        full = f"{prefix_dec}/{name_dec}" if prefix_dec else name_dec
        if info.isdir():
            # The trailing slash is part of the directory member's name field;
            # the manifest normalizes both sides with rstrip("/") before walking.
            assert full.rstrip("/") == info.name.rstrip("/") == "/".join(parts)
            assert not info.issym()
        else:
            assert full == info.name == "/".join(parts)
        if info.issym():
            (link_raw, link_dec) = captured_fields[cursor]
            cursor += 1
            assert link_dec == info.linkname == node.target
    assert cursor == len(captured_fields)  # every decode mapped to a member

    assert (result / "lib/é.py").read_bytes() == b"ok"
    assert os.readlink(result / "alias") == "lib/é.py"
    assert (result / "prefixdir/prefixed").read_bytes() == b"ok"
    assert captured_records


def test_duplicate_effective_names_with_different_suffixes_refused_before_write(tmp_path, monkeypatch):
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    raw = bytearray(gzip.decompress(archive([
        ("python/a", b"x", "file"), ("python/a", b"y", "file")
    ])))
    for offset, tail in ((0, b"first"), (1024, b"second")):
        header = bytearray(raw[offset:offset + 512])
        header[:100] = b"python/a\0" + tail + bytes(100 - 8 - 1 - len(tail))
        rechecksum(header)
        raw[offset:offset + 512] = header
    payload = gzip.compress(bytes(raw))
    parser, writes = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert len(parser) == 1  # both members pass preflight; the manifest refuses the duplicate
    assert writes == []
    assert_rejection_is_isolated(tmp_path, payload)
    monkeypatch.undo()

    # The same effective path through different encodings: a stale-suffixed
    # prefix ("python" + "a") against an unprefixed stale-suffixed name. Raw
    # tails cannot distinguish the duplicate; preflight passes and the
    # canonical duplicate is refused at the manifest seam before any write.
    raw = bytearray(gzip.decompress(archive([
        ("python/holder-a", b"k", "file"), ("python/holder-b", b"k", "file")
    ])))
    prefix_header = header_offsets(raw, b"python/holder-a")[0]
    name_header = header_offsets(raw, b"python/holder-b")[0]
    _set_field(raw, prefix_header, 100, b"a", b"first")
    _set_field(raw, prefix_header + 345, 155, b"python", b"first")
    _set_field(raw, name_header, 100, b"python/a", b"second")
    payload = gzip.compress(bytes(raw))
    parser, writes = [], []
    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert len(parser) == 1
    assert writes == []
    assert_rejection_is_isolated(tmp_path, payload)


@pytest.mark.parametrize(
    ("first", "last"),
    [(b"python/A", b"python/a"), ("python/café".encode(), "python/cafe\u0301".encode())],
    ids=["casefold-ancestor", "nfc-ancestor"],
)
def test_implicit_ancestor_alias_collisions_with_stale_tails_refused_before_write(
    tmp_path, monkeypatch, first, last
):
    # The members' ancestor components canonically collide while their leaf
    # names and stale post-NUL tails all differ: only normalization can
    # identify the implicit-ancestor alias, before any leaf duplicate arises.
    raw = bytearray(gzip.decompress(archive([
        ("python/zfirst/a", b"x", "file"), ("python/zlast/b", b"y", "file")
    ])))
    first_header = header_offsets(raw, b"python/zfirst/a")[0]
    last_header = header_offsets(raw, b"python/zlast/b")[0]
    _set_field(raw, first_header, 100, first + b"/a", b"t1")
    _set_field(raw, last_header, 100, last + b"/b", b"t2")
    payload = gzip.compress(bytes(raw))
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    parser, writes = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, gzip.compress(bytes(raw)))
    assert len(parser) == 1
    assert writes == []
    assert_rejection_is_isolated(tmp_path, payload)


@pytest.mark.parametrize(
    ("first", "last"),
    [(b"python/A", b"python/a"), ("python/café".encode(), "python/cafe\u0301".encode())],
    ids=["casefold", "nfc"],
)
def test_alias_collisions_cannot_be_distinguished_by_stale_tails(tmp_path, monkeypatch, first, last):
    raw = bytearray(gzip.decompress(archive([
        ("python/zfirst", b"k", "file"), ("python/zlast", b"k", "file")
    ])))
    first_header = header_offsets(raw, b"python/zfirst")[0]
    last_header = header_offsets(raw, b"python/zlast")[0]
    _set_field(raw, first_header, 100, first, b"x")
    _set_field(raw, last_header, 100, last, b"y")
    payload = gzip.compress(bytes(raw))
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    parser, writes = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert len(parser) == 1
    assert writes == []
    assert_rejection_is_isolated(tmp_path, payload)


def test_full_width_ustar_fields_extract_and_no_nul_is_significant(tmp_path):
    name = b"a" * 100
    prefix = b"python/" + b"p" * 148
    # A 100-byte name and 155-byte prefix make a legal 256-byte combined path.
    payload = archive([("python/a", b"ok", "file")])
    raw = bytearray(gzip.decompress(payload))
    raw[:100] = name
    raw[345:500] = prefix
    rechecksum(raw)
    destination = invoke(tmp_path / "full", gzip.compress(bytes(raw)))
    assert (destination / Path(prefix.decode().removeprefix("python/")) / name.decode()).read_bytes() == b"ok"

    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact._field(b"\0" + b"x" * 100, 100)


def test_full_width_existing_relative_link_target_extracts_intact(tmp_path):
    long_name = b"T" * 100
    raw = bytearray(gzip.decompress(typed_archive([
        ("python/" + long_name.decode(), b"x", tarfile.REGTYPE, 0o644),
        ("python/alias", long_name, tarfile.SYMTYPE, 0o777),
    ])))
    target_header = header_offsets(raw, long_name)[0]
    alias_header = header_offsets(raw, b"python/alias")[0]
    # Fixture sanity: the long member is encoded with a "python" prefix and a
    # full-width name, and the alias link field is a full-width value with no NUL.
    assert raw[target_header + 345:target_header + 500] == b"python" + b"\0" * (155 - 6)
    assert raw[alias_header + 157:alias_header + 257] == long_name
    # A full-width no-NUL field decodes without truncation: the final byte is data.
    assert engine_artifact._field(long_name, 100) == "T" * 100
    assert engine_artifact._field(b"python" + b"p" * 149, 155) == "python" + "p" * 149
    raw_path = tmp_path / "ustar.tar"
    raw_path.write_bytes(bytes(raw))
    engine_artifact._preflight(raw_path)
    result = invoke(tmp_path, gzip.compress(bytes(raw)))
    assert (result / long_name.decode()).read_bytes() == b"x"
    assert os.readlink(result / "alias") == long_name.decode()


@pytest.mark.parametrize(
    ("field", "value", "parser_runs"),
    [
        ("name", b"python/" + b"a" * 90 + b"/..", False),
        ("name", b"python/" + b"a" * 92 + b"\xff", False),
        ("prefix", b"python/../" + b"p" * 145, False),
        ("prefix", b"python/" + b"p" * 147 + b"\xff", False),
        ("link", b"t" * 99 + b"\xff", False),
        ("link", b"/" + b"t" * 99, True),
        ("link", b"t" * 97 + b"a\\b", True),
        ("link", b"t" * 99 + b"\x01", True),
    ],
    ids=["name-path", "name-utf8", "prefix-path", "prefix-utf8",
         "link-utf8", "link-absolute", "link-backslash", "link-control"],
)
def test_full_width_no_nul_strings_reject_syntax_and_invalid_utf8(
    tmp_path, monkeypatch, field, value, parser_runs
):
    if field == "link":
        raw = bytearray(gzip.decompress(typed_archive([
            ("python/target", b"x", tarfile.REGTYPE, 0o644),
            ("python/alias", b"target", tarfile.SYMTYPE, 0o777),
        ])))
        base = header_offsets(raw, b"python/alias")[0]
        raw[base + 157:base + 257] = value
    else:
        raw = bytearray(gzip.decompress(archive([("python/a", b"x", "file")])))
        base = 0
        raw[0 if field == "name" else 345:(100 if field == "name" else 500)] = value
    rechecksum_at(raw, base)
    payload = gzip.compress(bytes(raw))
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    parser, writes = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert writes == []
    if parser_runs:
        assert len(parser) == 1
    else:
        assert parser == []
    assert_rejection_is_isolated(tmp_path, payload)


@pytest.mark.parametrize("field,offset,width", [("name", 0, 100), ("prefix", 345, 155), ("link", 157, 100)])
def test_invalid_utf8_before_nul_and_no_nul_reject_before_parser(tmp_path, monkeypatch, field, offset, width):
    source = archive([("python/a", b"x", "file")])
    if field == "link":
        source = typed_archive([
            ("python/target", b"x", tarfile.REGTYPE, 0o644),
            ("python/alias", b"target", tarfile.SYMTYPE, 0o777),
        ])
        offset += 1024
    for invalid, tail in (
        (b"\xff", b"\xc3\xa9"),
        (b"\xc3", b"\xc3\xa9"),
        # A truncated byte before the NUL plus its continuation after it is not
        # a valid é split across the NUL boundary: preflight must reject.
        (b"\xc3", b"\xa9"),
    ):
        payload = _with_field_suffix(source, offset, width, invalid, tail)
        case_dir = tmp_path / f"{field}-{invalid.hex()}-{len(tail)}"
        case_dir.mkdir(parents=True)
        (case_dir / "outside-sentinel").write_text("keep")
        parsed = []
        monkeypatch.setattr(engine_artifact.tarfile, "open", lambda *a, **k: parsed.append(a))
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(case_dir, payload)
        assert parsed == []
        assert_rejection_is_isolated(case_dir, payload)


@pytest.mark.parametrize(
    ("bad", "parser_runs"),
    [
        (b"", False),
        (b"/escape", False),
        (b"python/../escape", False),
        (b"other/file", True),
        (b"python\\escape", False),
        (b"python/bad\nname", False),
    ],
    ids=["empty", "absolute", "traversal", "foreign-root", "backslash", "control"],
)
def test_hostile_effective_paths_with_suffixes_still_refuse(tmp_path, monkeypatch, bad, parser_runs):
    payload = _with_field_suffix(archive([("python/a", b"x", "file")]), 0, 100, bad)
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    parser, writes = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    if parser_runs:
        # "other/file" passes preflight path validation and is refused by the
        # manifest's foreign-root rule instead.
        assert len(parser) == 1
    else:
        # Preflight path validation is the acting seam: zero parser calls.
        assert parser == []
    assert writes == []
    assert_rejection_is_isolated(tmp_path, payload)


@pytest.mark.parametrize("prefix", [b"/outside", b"python/.."])
def test_hostile_effective_prefixes_with_safe_name_and_suffix_still_refuse(tmp_path, monkeypatch, prefix):
    payload = _with_field_suffix(archive([("python/a", b"x", "file")]), 345, 155, prefix)
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    writes, parser = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert writes == []
    assert parser == []  # the combined name/prefix path fails preflight validation
    assert_rejection_is_isolated(tmp_path, payload)


@pytest.mark.parametrize("target", [b"/outside", b"../../outside", b"missing"])
def test_hostile_effective_links_with_suffixes_still_refuse(tmp_path, monkeypatch, target):
    raw = bytearray(gzip.decompress(typed_archive([
        ("python/target", b"x", tarfile.REGTYPE, 0o644),
        ("python/alias", target, tarfile.SYMTYPE, 0o777),
    ])))
    alias_header = header_offsets(raw, b"python/alias")[0]
    _set_field(raw, alias_header + 157, 100, target, b"stale")
    payload = gzip.compress(bytes(raw))
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parser, writes = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, parent / "out", sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert writes == []
    assert len(parser) == 1  # preflight accepted the stale suffix; link policy refuses before writes
    assert_rejection_is_isolated(tmp_path, payload)


@pytest.mark.parametrize("field,offset,width", [("name", 0, 100), ("prefix", 345, 155), ("link", 157, 100)])
def test_utf8_is_strict_before_nul_only(tmp_path, monkeypatch, field, offset, width):
    payload = archive([("python/a", b"x", "file")])
    if field == "link":
        payload = typed_archive([("python/a", b".", tarfile.SYMTYPE, 0o777)])
    effective = b"python/\xc3\xa9" if field == "name" else (b"python" if field == "prefix" else b".")
    accepted = _with_field_suffix(payload, offset, width, effective, b"\xff")
    invoke(tmp_path / "accepted", accepted)
    invalid = _with_field_suffix(payload, offset, width, b"\xff", b"\xc3\xa9")
    rejected_dir = tmp_path / "rejected"
    rejected_dir.mkdir(parents=True)
    (rejected_dir / "outside-sentinel").write_text("keep")
    parsed = []
    monkeypatch.setattr(engine_artifact.tarfile, "open", lambda *a, **k: parsed.append(a))
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(rejected_dir, invalid)
    assert parsed == []
    assert_rejection_is_isolated(rejected_dir, invalid)


def test_suffix_bytes_remain_covered_by_header_checksum(tmp_path, monkeypatch):
    valid = _with_field_suffix(archive([("python/a", b"x", "file")]), 0, 100, b"python/a")
    modified = rewrite_tar(valid, lambda raw: raw.__setitem__(9, ord("X")))
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "outside-sentinel").write_text("keep")
    parsed = []
    monkeypatch.setattr(engine_artifact.tarfile, "open", lambda *a, **k: parsed.append(a))
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(bad_dir, modified)
    assert parsed == []
    assert_rejection_is_isolated(bad_dir, modified)
    monkeypatch.undo()
    assert (invoke(tmp_path / "good", valid) / "a").read_bytes() == b"x"
    # Recalculating the checksum over the exact same modified suffix byte is
    # again accepted: only the checksum mismatch is invalid.
    repaired = rewrite_tar(valid, lambda raw: (raw.__setitem__(9, ord("X")), rechecksum(raw)))
    assert (invoke(tmp_path / "repaired", repaired) / "a").read_bytes() == b"x"


def test_empty_effective_prefix_ignores_suffix_instead_of_adding_path(tmp_path):
    payload = archive([("python/a", b"x", "file")])
    payload = _with_field_suffix(payload, 345, 155, b"", b"python")
    assert (invoke(tmp_path, payload) / "a").read_bytes() == b"x"
    assert not (tmp_path / "private/out/python").exists()


@pytest.mark.parametrize("effective", [b"", b"/outside", b"../../outside", b"missing", b"other"])
def test_hostile_effective_links_with_suffixes_refuse_before_write(tmp_path, monkeypatch, effective):
    entries = [
        ("python/target", b"x", "file"),
        ("python/alias", b"target", "symlink"),
    ]
    if effective == b"other":
        entries = [("python/alias", b"other", "symlink"), ("python/other", b"alias", "symlink")]
    raw = bytearray(gzip.decompress(archive(entries)))
    alias_header = header_offsets(raw, b"python/alias")[0]
    _set_field(raw, alias_header + 157, 100, effective, b"ignored")
    payload = gzip.compress(bytes(raw))
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    parser, writes = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert writes == []
    if effective == b"":
        # The empty effective link itself fails before the tar parser runs.
        assert parser == []
    else:
        # Manifest/link resolution rejects after a full preflight pass.
        assert len(parser) == 1
    assert_rejection_is_isolated(tmp_path, payload)


@pytest.mark.parametrize(
    ("entries", "attacker", "target"),
    [
        (
            [("python/a/up", b"..", "symlink"), ("python/link", b"a/up/../outside", "symlink")],
            b"python/link", b"a/up/../outside",
        ),
        (
            [("python/file", b"x", "file"), ("python/alias", b"file/child", "symlink")],
            b"python/alias", b"file/child",
        ),
        (
            [("python/link", b"real", "symlink"), ("python/link/payload", b"x", "file")],
            b"python/link", b"real",
        ),
    ],
    ids=["intermediate-escape", "file-as-ancestor", "link-write-through"],
)
def test_hostile_link_structures_with_stale_suffixes_refused_before_write(
    tmp_path, monkeypatch, entries, attacker, target
):
    raw = bytearray(gzip.decompress(archive(entries)))
    owner = header_offsets(raw, attacker)[0]
    _set_field(raw, owner + 157, 100, target, b"stale")
    payload = gzip.compress(bytes(raw))
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("keep")
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parser, writes = [], []
    original_write, real_open = engine_artifact._write_tree, engine_artifact.tarfile.open

    def observe_write(*args):
        writes.append(args)
        return original_write(*args)

    def observe_open(*args, **kwargs):
        parser.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_write_tree", observe_write)
    monkeypatch.setattr(engine_artifact.tarfile, "open", observe_open)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, parent / "out", sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert writes == []
    assert len(parser) == 1  # refused at the manifest/link-resolution seam, not by preflight
    assert_rejection_is_isolated(tmp_path, payload)


@pytest.mark.parametrize("width", [100, 155], ids=["name-or-link", "prefix"])
def test_overwidth_field_rejected_even_when_first_byte_is_nul(width):
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact._field(b"\0" + b"x" * width, width)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact._field(b"x" * (width + 1), width)


@pytest.mark.parametrize("case", ["short", "long", "digest"])
def test_digest_or_size_mismatch_never_parses_archive(tmp_path, monkeypatch, case):
    payload = archive([("python/a", b"x", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parsed = []
    for seam in ("_decompress", "_preflight", "_manifest"):
        original = getattr(engine_artifact, seam)

        def observe(*args, _original=original, _seam=seam, **kwargs):
            parsed.append(_seam)
            return _original(*args, **kwargs)

        monkeypatch.setattr(engine_artifact, seam, observe)
    size = len(payload) + (-1 if case == "short" else 1 if case == "long" else 0)
    digest = "0" * 64 if case == "digest" else hashlib.sha256(payload).hexdigest()
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload, sha=digest, size=size)
    assert parsed == []
    assert not (parent / "out").exists()


@pytest.mark.parametrize("name", ["/escape", "other/file", "python/../escape", "python\\escape"])
def test_rejects_hostile_names_before_publication(tmp_path, name):
    payload = archive([(name, b"x", "file")])
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert not (tmp_path / "private/out").exists()


def test_rejects_link_escape_cycle_and_dangling_before_writes(tmp_path, monkeypatch):
    cases = [
        [("python/link", b"../../outside", "symlink")],
        [("python/link", b"/outside", "symlink")],
        [("python/link", b"missing", "symlink")],
        [("python/a", b"b", "symlink"), ("python/b", b"a", "symlink")],
        [("python/link", b"lib", "symlink"), ("python/link/payload", b"x", "file")],
        [("python/a/up", b"..", "symlink"), ("python/link", b"a/up/../outside", "symlink")],
        [("python/lib/file", b"ok", "file"), ("python/link", b"Lib/file", "symlink")],
        [("python/file", b"x", "file"), ("python/alias", b"file/child", "symlink")],
        [("python/a/up", b"../b/up", "symlink"), ("python/b/up", b"../a/up", "symlink"),
         ("python/link", b"a/up", "symlink")],
    ]
    for index, entries in enumerate(cases):
        isolated = tmp_path / str(index)
        isolated.mkdir()
        writes = []
        monkeypatch.setattr(
            engine_artifact, "_write_tree", lambda *a, writes=writes: writes.append(a)
        )
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, archive(entries))
        assert writes == []


def test_valid_intermediate_directory_alias_resolves(tmp_path):
    result = invoke(tmp_path, archive([
        ("python/lib/module.py", b"content", "file"),
        ("python/real", b"lib", "symlink"),
        ("python/a", b"real", "symlink"),
        ("python/alias", b"a/module.py", "symlink"),
    ]))
    assert os.readlink(result / "alias") == "a/module.py"
    assert (result / "alias").read_bytes() == b"content"


def test_repeated_finite_alias_traversal_is_allowed(tmp_path):
    result = invoke(tmp_path, archive([
        ("python/dir/file", b"reachable", "file"),
        ("python/a", b"dir", "symlink"),
        ("python/link", b"a/../a/file", "symlink"),
    ]))
    assert (result / "link").read_bytes() == b"reachable"


def test_file_alias_cannot_be_followed_by_parent_component(tmp_path, monkeypatch):
    writes = []
    monkeypatch.setattr(engine_artifact, "_write_tree", lambda *a: writes.append(a))
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, archive([
            ("python/file", b"x", "file"),
            ("python/a", b"file", "symlink"),
            ("python/b", b"a/../file", "symlink"),
        ]))
    assert writes == []


@pytest.mark.parametrize("hop_count,accepted", [(30, True), (31, False)])
def test_symlink_hops_obey_supported_platform_bound(tmp_path, monkeypatch, hop_count, accepted):
    entries = [("python/target", b"ok", "file")]
    entries.extend(
        (f"python/link-{index}", f"link-{index + 1}".encode(), "symlink")
        for index in range(hop_count - 1)
    )
    entries.append((f"python/link-{hop_count - 1}", b"target", "symlink"))
    payload = archive(entries)
    if accepted:
        result = invoke(tmp_path, payload)
        assert (result / "link-0").read_bytes() == b"ok"
    else:
        writes = []
        monkeypatch.setattr(engine_artifact, "_write_tree", lambda *a: writes.append(a))
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(tmp_path, payload)
        assert writes == []


def test_many_links_keep_public_path_record_work_linear(tmp_path, monkeypatch):
    entries = [("python/bin/file", b"x", "file")]
    entries.extend((f"python/alias-{i}", b"bin/file", "symlink") for i in range(80))
    original = engine_artifact._manifest
    counter = {"visited": 0}

    class CountedRecords(list):
        def __iter__(self):
            for item in super().__iter__():
                counter["visited"] += 1
                yield item

    def observe_manifest(path, *args, **kwargs):
        root, records = original(path, *args, **kwargs)
        return root, CountedRecords(records)

    monkeypatch.setattr(engine_artifact, "_manifest", observe_manifest)
    invoke(tmp_path, archive(entries))
    assert counter["visited"] <= len(entries) * 12


def test_component_resolution_operation_budget_is_aggregate_and_bounded(tmp_path):
    payload = gzip.decompress(archive([
        ("python/lib/file", b"x", "file"),
        ("python/alias", b"lib/file", "symlink"),
        ("python/alias2", b"lib/file", "symlink"),
    ]))
    tar_path = tmp_path / "expanded.tar"
    tar_path.write_bytes(payload)
    engine_artifact._preflight(tar_path)
    root, records = engine_artifact._manifest(tar_path)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact._resolve_links(root, records, operation_limit=3)


def test_existing_destination_is_untouched(tmp_path):
    payload = archive([("python/a", b"x", "file")])
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "out"
    destination.mkdir()
    sentinel = destination / "sentinel"
    sentinel.write_text("keep")
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert sentinel.read_text() == "keep"


def test_rejects_link_write_through_and_ancestor_collisions_before_writes(tmp_path):
    cases = [
        [("python/Lib/a", b"a", "file"), ("python/lib/b", b"b", "file")],
        [("python/lib", b"payload", "file"), ("python/lib/a", b"a", "file")],
        [("python/link", b"real", "symlink"), ("python/link/payload", b"x", "file")],
        [("python/a/up", b"..", "symlink"), ("python/link", b"a/up/../outside", "symlink")],
    ]
    for index, entries in enumerate(cases):
        isolated = tmp_path / str(index)
        isolated.mkdir()
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, archive(entries))
        assert not (isolated / "private/out").exists()


def test_rejects_nonzero_data_after_tar_end(tmp_path):
    payload = archive([("python/a", b"x", "file")])
    raw = gzip.decompress(payload) + b"nonzero"
    malformed = gzip.compress(raw)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, malformed)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: (raw.__setitem__(156, ord("x")), rechecksum(raw)),
        lambda raw: (raw.__setitem__(156, ord("g")), rechecksum(raw)),
        lambda raw: (raw.__setitem__(156, ord("L")), rechecksum(raw)),
        lambda raw: (raw.__setitem__(156, ord("K")), rechecksum(raw)),
        lambda raw: (raw.__setitem__(156, ord("S")), rechecksum(raw)),
        lambda raw: (raw.__setitem__(156, ord("D")), rechecksum(raw)),
        lambda raw: (raw.__setitem__(156, ord("M")), rechecksum(raw)),
        lambda raw: (raw.__setitem__(156, ord("V")), rechecksum(raw)),
        lambda raw: (raw.__setitem__(slice(257, 263), b"ustar "), rechecksum(raw)),
        lambda raw: raw.__setitem__(0, ord("/")),
        lambda raw: (raw.__setitem__(124, 0x80), rechecksum(raw)),
        lambda raw: (
            raw.__setitem__(slice(124, 136), f"{1025:011o}\0".encode()),
            rechecksum(raw),
            raw.__delitem__(slice(1536, None)),
        ),
    ],
    ids=["pax", "pax-global", "gnu-longname", "gnu-longlink", "sparse", "gnu-dumpdir",
         "gnu-multivol", "gnu-volhdr", "gnu-variant", "bad-checksum", "base256-number",
         "truncated-padded-extent"],
)
def test_ustar_preflight_rejects_extensions_and_malformed_headers_before_tar_parser(
    tmp_path, monkeypatch, mutation
):
    payload = archive([("python/a", b"x", "file")])
    malformed = rewrite_tar(payload, mutation)
    parser = []
    monkeypatch.setattr(engine_artifact.tarfile, "open", lambda *a, **k: parser.append(a))
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, malformed)
    assert parser == []


def test_rejects_invalid_raw_utf8_name_link_and_extension_preamble_before_parser(tmp_path, monkeypatch):
    base = archive([("python/a", b"x", "file")])
    malformed = [
        rewrite_tar(base, lambda raw: (raw.__setitem__(0, 0xff), rechecksum(raw))),
        rewrite_tar(base, lambda raw: (raw.__setitem__(156, ord("2")), raw.__setitem__(157, 0xff),
                                      rechecksum(raw))),
    ]
    field = engine_artifact._field
    for index, payload in enumerate(malformed):
        isolated = tmp_path / str(index)
        isolated.mkdir()
        parser = []
        monkeypatch.setattr(engine_artifact.tarfile, "open", lambda *a, **k: parser.append(a))
        decoded = []

        def observe_field(raw, limit):
            decoded.append(raw)
            return field(raw, limit)

        monkeypatch.setattr(engine_artifact, "_field", observe_field)
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, payload)
        assert parser == []
        assert any(0xff in value for value in decoded)


def test_decompressed_limit_applies_to_full_gzip_stream_before_tar_parser(tmp_path, monkeypatch):
    payload = archive([("python/a", b"x", "file")])
    raw = gzip.decompress(payload)
    monkeypatch.setattr(engine_artifact, "MAX_EXPANDED", len(raw) - 1)
    called = False

    def parser(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("parser must not see over-limit stream")

    monkeypatch.setattr(engine_artifact, "_preflight", parser)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert not called


def test_gzip_crc_trailer_and_later_member_fail_before_preflight(tmp_path, monkeypatch):
    valid = archive([("python/a", b"x", "file")])
    bad_crc = bytearray(valid)
    bad_crc[-8] ^= 0xff
    truncated_trailer = valid[:-4]
    malformed_second_member = valid + gzip.compress(b"later stream")[:-5]
    preflight_calls = []
    monkeypatch.setattr(
        engine_artifact, "_preflight", lambda *a, **k: preflight_calls.append(a)
    )
    for index, payload in enumerate((bytes(bad_crc), truncated_trailer, malformed_second_member)):
        isolated = tmp_path / str(index)
        isolated.mkdir()
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, payload)
    assert preflight_calls == []


def test_decompressed_limit_includes_padding_and_concatenated_gzip_after_tar_end(tmp_path, monkeypatch):
    payload = archive([("python/a", b"x", "file")])
    raw = gzip.decompress(payload)
    parser_calls = []
    original_preflight = engine_artifact._preflight
    monkeypatch.setattr(engine_artifact, "MAX_EXPANDED", len(raw))
    monkeypatch.setattr(engine_artifact, "_preflight", original_preflight)
    result = invoke(tmp_path / "exact", payload)
    assert (result / "a").read_bytes() == b"x"

    for index, expanded in enumerate((raw + bytes(1024 * 1024), raw + gzip.decompress(gzip.compress(b"second")))):
        isolated = tmp_path / f"excess-{index}"
        isolated.mkdir()
        excess = gzip.compress(expanded) if index == 0 else payload + gzip.compress(b"second")
        monkeypatch.setattr(engine_artifact, "MAX_EXPANDED", len(raw))
        monkeypatch.setattr(engine_artifact, "_preflight", lambda *_: parser_calls.append(True))
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, excess)
    assert parser_calls == []


def test_limits_are_enforced_at_boundary(tmp_path, monkeypatch):
    payload = archive([("python/a", b"a", "file"), ("python/b", b"b", "file"), ("python/c", b"c", "file")])
    isolated = tmp_path / "compressed"
    isolated.mkdir()
    monkeypatch.setattr(engine_artifact, "MAX_COMPRESSED", len(payload) - 1)
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(isolated, payload)
    assert not (isolated / "private/out").exists()

    isolated = tmp_path / "members"
    isolated.mkdir()
    monkeypatch.setattr(engine_artifact, "MAX_COMPRESSED", 512 * 1024 * 1024)
    monkeypatch.setattr(engine_artifact, "MAX_MEMBERS", 2)
    headers = []
    read_header = engine_artifact._read_tar_header

    def observe_header(stream):
        block = read_header(stream)
        headers.append(block)
        return block

    monkeypatch.setattr(engine_artifact, "_read_tar_header", observe_header)
    parsed = []
    monkeypatch.setattr(engine_artifact, "_manifest", lambda *a, **k: parsed.append(a))
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(isolated, payload)
    assert len(headers) == 3
    assert parsed == []


def test_source_replacement_cannot_change_verified_snapshot(tmp_path, monkeypatch):
    original_payload = archive([("python/original", b"verified", "file")])
    replacement_payload = archive([("python/replaced", b"attacker", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(original_payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    snapshot = engine_artifact._snapshot

    def replace_after_verification(*args):
        snapshot(*args)
        source.write_bytes(replacement_payload)

    monkeypatch.setattr(engine_artifact, "_snapshot", replace_after_verification)
    destination = parent / "out"
    engine_artifact.verify_and_extract(
        source, destination, sha256=hashlib.sha256(original_payload).hexdigest(),
        size=len(original_payload),
    )
    assert (destination / "original").read_bytes() == b"verified"
    assert not (destination / "replaced").exists()


def test_refuses_symlink_source_or_nonregular_input(tmp_path, monkeypatch):
    payload = archive([("python/a", b"x", "file")])
    real = tmp_path / "real.tgz"
    real.write_bytes(payload)
    link = tmp_path / "link.tgz"
    link.symlink_to(real)
    directory = tmp_path / "directory"
    directory.mkdir()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    calls = []
    reads = []
    real_open = engine_artifact.os.open
    real_fstat = engine_artifact.os.fstat
    opened_fds = set()

    def observed_open(path, flags, *args, **kwargs):
        if Path(path) in (link, directory):
            calls.append(("open-attempt", flags))
            fd = real_open(path, flags, *args, **kwargs)
            opened_fds.add(fd)
            return fd
        return real_open(path, flags, *args, **kwargs)

    def observed_fstat(fd):
        if fd in opened_fds:
            calls.append(("fstat", fd))
            opened_fds.remove(fd)
        return real_fstat(fd)

    monkeypatch.setattr(engine_artifact.os, "open", observed_open)
    monkeypatch.setattr(engine_artifact.os, "fstat", observed_fstat)
    monkeypatch.setattr(engine_artifact.os, "fdopen", lambda *a, **k: reads.append(a))
    for source in (link, directory):
        with pytest.raises(engine_artifact.ArtifactError):
            engine_artifact.verify_and_extract(
                source, parent / source.name, sha256="0" * 64, size=0
            )
    assert len(calls) == 3
    assert calls[0][0] == "open-attempt"
    assert calls[0][1] & os.O_NOFOLLOW and calls[0][1] & os.O_NONBLOCK
    assert calls[1][0] == "open-attempt"
    assert calls[2][0] == "fstat"
    assert reads == []


def test_fifo_refusal_has_a_bounded_subprocess_deadline(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    guards = tmp_path / "guards"
    guards.mkdir()
    (guards / "sitecustomize.py").write_text(
        "import atexit, json, os, stat, sys\n"
        f"fifo = {str(fifo)!r}\n"
        "events = []\n"
        "tracked = set()\n"
        "real_open, real_fstat = os.open, os.fstat\n"
        "real_fdopen, real_read = os.fdopen, os.read\n"
        "def observed_open(path, flags, *args, **kwargs):\n"
        "    if os.fspath(path) == fifo:\n"
        "        events.append(['open', flags])\n"
        "        fd = real_open(path, flags, *args, **kwargs)\n"
        "        tracked.add(fd)\n"
        "        events.append(['opened', fd])\n"
        "        return fd\n"
        "    return real_open(path, flags, *args, **kwargs)\n"
        "def observed_fstat(fd):\n"
        "    result = real_fstat(fd)\n"
        "    if fd in tracked:\n"
        "        events.append(['fstat-fifo', stat.S_ISFIFO(result.st_mode)])\n"
        "    return result\n"
        "def observed_fdopen(fd, *args, **kwargs):\n"
        "    if fd in tracked: events.append(['fdopen'])\n"
        "    return real_fdopen(fd, *args, **kwargs)\n"
        "def observed_read(fd, *args, **kwargs):\n"
        "    if fd in tracked: events.append(['read'])\n"
        "    return real_read(fd, *args, **kwargs)\n"
        "os.open, os.fstat, os.fdopen, os.read = observed_open, observed_fstat, observed_fdopen, observed_read\n"
        "atexit.register(lambda: print('__FIFO_EVENTS__' + json.dumps(events)))\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(guards)
    result = subprocess.run(
        [
            sys.executable, str(MODULE_PATH), "--archive", str(fifo),
            "--destination", str(parent / "out"), "--sha256", "0" * 64, "--size", "0",
        ],
        capture_output=True, text=True, timeout=3, env=env,
    )
    assert result.returncode != 0
    assert result.stderr == "artifact verification failed\n"
    event_line = next(line for line in result.stdout.splitlines() if line.startswith("__FIFO_EVENTS__"))
    events = json.loads(event_line.removeprefix("__FIFO_EVENTS__"))
    assert events[0][0] == "open" and events[0][1] & os.O_NOFOLLOW and events[0][1] & os.O_NONBLOCK
    assert events[1][0] == "opened" and events[2] == ["fstat-fifo", True]
    assert all(event[0] not in {"fdopen", "read"} for event in events)


def test_cli_rejects_raw_dot_destination_component_before_path_conversion(tmp_path):
    payload = archive([("python/a", b"x", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    raw_destination = f"{parent}/./out"
    result = subprocess.run(
        [
            sys.executable, str(MODULE_PATH), "--archive", str(source),
            "--destination", raw_destination, "--sha256", hashlib.sha256(payload).hexdigest(),
            "--size", str(len(payload)),
        ],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert result.stderr == "artifact verification failed\n"
    assert not (parent / "out").exists()


@pytest.mark.parametrize(
    "platform_name,remove_flag",
    [("Windows", None), ("Darwin", "O_NOFOLLOW"), ("Linux", "O_NONBLOCK")],
)
def test_unsupported_platform_or_missing_open_flags_refuses_before_io(
    tmp_path, monkeypatch, platform_name, remove_flag
):
    calls = []
    monkeypatch.setattr(engine_artifact.platform, "system", lambda: platform_name)
    if remove_flag:
        monkeypatch.delattr(engine_artifact.os, remove_flag)
    for owner, name in (
        (engine_artifact, "_safe_parent"),
        (engine_artifact, "_check_no_overlap"),
        (engine_artifact.os, "open"),
        (engine_artifact.os, "stat"),
        (engine_artifact.os, "lstat"),
        (engine_artifact.os.path, "abspath"),
        (engine_artifact.os.path, "lexists"),
        (engine_artifact.tempfile, "mkdtemp"),
        (Path, "stat"),
        (Path, "lstat"),
        (Path, "is_symlink"),
    ):
        monkeypatch.setattr(owner, name, lambda *a, _name=name, **k: calls.append(_name))
    with pytest.raises(engine_artifact.ArtifactError, match="^invalid artifact$"):
        engine_artifact.verify_and_extract(
            tmp_path / "not-read", tmp_path / "not-created/out", sha256="0" * 64, size=1
        )
    assert calls == []


def test_rejects_hostile_member_manifest_before_writes(tmp_path, monkeypatch):
    invalid = [
        [("/escape", b"x", "file")],
        [("python/../escape", b"x", "file")],
        [("other/file", b"x", "file")],
        [("python/a/../../escape", b"x", "file")],
        [("python\\escape", b"x", "file")],
        [("python/control\nname", b"x", "file")],
        [("python/a", b"x", "file"), ("python/a", b"y", "file")],
        [("python/A", b"x", "file"), ("python/a", b"y", "file")],
        [("python/caf\u00e9/a", b"x", "file"), ("python/cafe\u0301/b", b"y", "file")],
    ]
    special_types = [
        (tarfile.LNKTYPE, "hardlink"),
        (tarfile.FIFOTYPE, "fifo"),
        (tarfile.CHRTYPE, "device"),
        (tarfile.BLKTYPE, "block-device"),
        (b"s", "socket"),
    ]
    for index, entries in enumerate(invalid):
        isolated = tmp_path / f"manifest-{index}"
        isolated.mkdir()
        outside = isolated / "escape"
        outside.write_text("keep")
        writes = []
        monkeypatch.setattr(engine_artifact, "_write_tree", lambda *a: writes.append(a))
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, archive(entries))
        assert writes == []
        assert outside.read_text() == "keep"
    for index, (kind, _) in enumerate(special_types):
        isolated = tmp_path / f"type-{index}"
        isolated.mkdir()
        payload = typed_archive([("python/bad", b"", kind, 0o644)])
        writes = []
        monkeypatch.setattr(engine_artifact, "_write_tree", lambda *a: writes.append(a))
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, payload)
        assert writes == []
    isolated = tmp_path / "setuid"
    isolated.mkdir()
    writes = []
    monkeypatch.setattr(engine_artifact, "_write_tree", lambda *a: writes.append(a))
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(isolated, typed_archive([("python/bad", b"x", tarfile.REGTYPE, 0o4644)]))
    assert writes == []


def test_implicit_directories_confirm_once_but_explicit_duplicates_fail(tmp_path):
    valid = typed_archive([
        ("python/lib/module.py", b"ok", tarfile.REGTYPE, 0o644),
        ("python/lib", b"", tarfile.DIRTYPE, 0o755),
    ])
    result = invoke(tmp_path / "valid", valid)
    assert (result / "lib/module.py").read_bytes() == b"ok"
    duplicate_parent = tmp_path / "duplicate"
    duplicate_parent.mkdir()
    duplicate = typed_archive([
        ("python/lib", b"", tarfile.DIRTYPE, 0o755),
        ("python/lib", b"", tarfile.DIRTYPE, 0o755),
    ])
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(duplicate_parent, duplicate)


def test_symlink_size_must_be_octal_zero_before_tar_parser(tmp_path, monkeypatch):
    valid = typed_archive([("python/link", b"target", tarfile.SYMTYPE, 0o777)])
    malformed = [
        rewrite_tar(valid, lambda raw: (
            raw.__setitem__(slice(124, 136), f"{1:011o}\0".encode()), rechecksum(raw)
        )),
        rewrite_tar(valid, lambda raw: (
            raw.__setitem__(124, 0x80), rechecksum(raw)
        )),
    ]
    for index, payload in enumerate(malformed):
        isolated = tmp_path / str(index)
        isolated.mkdir()
        parsed = []
        monkeypatch.setattr(engine_artifact.tarfile, "open", lambda *a, **k: parsed.append(a))
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, payload)
        assert parsed == []


def test_non_directory_explicit_python_root_fails_before_member_write(tmp_path, monkeypatch):
    writes = []
    monkeypatch.setattr(engine_artifact, "_write_tree", lambda *args: writes.append(args))
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, archive([("python", b"not a directory", "file")]))
    assert writes == []


def test_root_directory_record_is_optional_and_allowed(tmp_path):
    payload = typed_archive([
        ("python/", b"", tarfile.DIRTYPE, 0o755),
        ("python/bin/app", b"ok", tarfile.REGTYPE, 0o755),
    ])
    result = invoke(tmp_path, payload)
    assert (result / "bin/app").read_bytes() == b"ok"


def test_component_trie_rejects_nfc_ancestor_and_reverse_file_collision(tmp_path, monkeypatch):
    cases = [
        [("python/caf\u00e9/a", b"x", "file"), ("python/cafe\u0301/b", b"y", "file")],
        [("python/lib/a", b"x", "file"), ("python/lib", b"y", "file")],
        [("python/link/child", b"x", "file"), ("python/link", b"target", "symlink")],
    ]
    for index, entries in enumerate(cases):
        isolated = tmp_path / str(index)
        isolated.mkdir()
        writes = []
        monkeypatch.setattr(engine_artifact, "_write_tree", lambda *a: writes.append(a))
        with pytest.raises(engine_artifact.ArtifactError):
            invoke(isolated, archive(entries))
        assert writes == []


def test_name_link_and_metadata_bounds(tmp_path):
    raw_path = tmp_path / "ustar"
    base = gzip.decompress(archive([("python/a", b"x", "file")]))

    def write_preflight(mutate, source=base):
        raw = bytearray(source)
        mutate(raw)
        raw_path.write_bytes(raw)
        engine_artifact._preflight(raw_path)

    write_preflight(lambda raw: (
        raw.__setitem__(slice(0, 100), b"a" * 100),
        raw.__setitem__(slice(345, 500), b"p" * 155),
        rechecksum(raw),
    ))
    link_raw = bytearray(gzip.decompress(typed_archive([
        ("python/link", b"l" * 100, tarfile.SYMTYPE, 0o777),
    ])))
    raw_path.write_bytes(link_raw)
    engine_artifact._preflight(raw_path)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact._components("a" * 256)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact._components("a" * 255 + "/" + "b")
    for source, mutate in (
        (base, lambda raw: (raw.__setitem__(0, 0xff), rechecksum(raw))),
        (link_raw, lambda raw: (raw.__setitem__(157, 0xff), rechecksum(raw))),
        *[
            (base, lambda raw, offset=offset: (raw.__setitem__(offset, 0x80), rechecksum(raw)))
            for offset in (100, 108, 116, 124, 136, 329, 337)
        ],
    ):
        with pytest.raises(engine_artifact.ArtifactError):
            write_preflight(mutate, source)


def test_large_extension_preamble_is_rejected_on_first_header(tmp_path, monkeypatch):
    raw = bytearray(gzip.decompress(archive([("python/a", b"x", "file")])))
    first = bytearray(raw[:512])
    first[156] = ord("x")
    rechecksum(first)
    expanded = bytes(first) * 1000 + bytes(1024)
    payload = gzip.compress(expanded)
    headers = []
    read_header = engine_artifact._read_tar_header

    def observe_header(stream):
        block = read_header(stream)
        headers.append(block)
        return block

    monkeypatch.setattr(engine_artifact, "_read_tar_header", observe_header)
    parser = []
    monkeypatch.setattr(engine_artifact, "_manifest", lambda *a, **k: parser.append(a))
    with pytest.raises(engine_artifact.ArtifactError):
        invoke(tmp_path, payload)
    assert len(headers) == 1
    assert parser == []


def test_truncated_archive_and_mid_copy_failure_leave_no_destination(tmp_path, monkeypatch):
    payload = archive([("python/a", b"x" * 900, "file")])
    input_path = tmp_path / "artifact.tgz"
    input_path.write_bytes(payload)
    outside = tmp_path / "outside"
    outside.write_text("keep")
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "out"
    truncated = gzip.compress(gzip.decompress(payload)[:700])
    input_path.write_bytes(truncated)
    framing = []
    preflight = engine_artifact._preflight

    def observe_preflight(path, *args, **kwargs):
        framing.append(path)
        return preflight(path, *args, **kwargs)

    monkeypatch.setattr(engine_artifact, "_preflight", observe_preflight)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            input_path, destination, sha256=hashlib.sha256(truncated).hexdigest(), size=len(truncated)
        )
    assert framing
    assert not destination.exists()
    assert not list(parent.glob(".engine-artifact-*"))
    input_path.write_bytes(payload)

    write_tree = engine_artifact._write_tree

    def fail_during_copy(source, output, size):
        output.write(source.read(min(size, 16)))
        raise OSError("injected copy failure")

    monkeypatch.setattr(engine_artifact, "_copy_member", fail_during_copy)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            input_path, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert input_path.read_bytes() == payload
    assert outside.read_text() == "keep"
    assert not destination.exists()
    assert not list(parent.glob(".engine-artifact-*"))


@pytest.mark.parametrize("kind", ["directory", "file", "dangling", "empty-directory"])
def test_existing_or_symlink_destination_is_never_replaced(tmp_path, kind):
    payload = archive([("python/a", b"new", "file")])
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "out"
    if kind in ("directory", "empty-directory"):
        destination.mkdir()
        if kind == "directory":
            (destination / "sentinel").write_text("keep")
    elif kind == "file":
        destination.write_text("keep")
    else:
        destination.symlink_to(parent / "absent")
    before = (
        destination.read_text() if kind == "file"
        else (destination / "sentinel").read_text() if kind == "directory"
        else os.readlink(destination) if kind == "dangling"
        else list(destination.iterdir())
    )
    mode_before = destination.lstat().st_mode
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    after = (
        destination.read_text() if kind == "file"
        else (destination / "sentinel").read_text() if kind == "directory"
        else os.readlink(destination) if kind == "dangling"
        else list(destination.iterdir())
    )
    assert before == after
    assert destination.lstat().st_mode == mode_before


def test_publication_requires_exclusive_private_parent(tmp_path, monkeypatch):
    payload = archive([("python/a", b"complete", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, loose / "out", sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    alias_parent = tmp_path / "parent-link"
    alias_parent.symlink_to(real_parent)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, alias_parent / "out", sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    overlap = real_parent / "overlap"
    overlap.mkdir()
    nested_source = overlap / "artifact.tgz"
    nested_source.write_bytes(payload)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            nested_source, overlap, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert nested_source.read_bytes() == payload

    destination = real_parent / "out"
    writer = engine_artifact._write_tree

    def inject_destination(snapshot, records, stage):
        writer(snapshot, records, stage)
        destination.mkdir()
        injected_identity.append((destination.stat().st_dev, destination.stat().st_ino))

    injected_identity = []
    monkeypatch.setattr(engine_artifact, "_write_tree", inject_destination)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert list(destination.iterdir()) == []
    assert (destination.stat().st_dev, destination.stat().st_ino) == injected_identity[0]
    assert not list(real_parent.glob(".engine-artifact-*"))


def test_destination_path_rejects_dotdot_before_abspath_can_hide_symlink(tmp_path):
    redirect = tmp_path / "redirect"
    redirect.mkdir(mode=0o700)
    nested = redirect / "nested"
    nested.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(nested)
    payload = archive([("python/a", b"x", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    destination = f"{alias}/../out"
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert not (tmp_path / "out").exists()
    assert not (redirect / "out").exists()


def test_archive_destination_overlap_guard_rejects_nested_paths():
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact._check_no_overlap(
            Path("/private/build/output/archive.tar.gz"),
            Path("/private/build/output"),
        )


def test_chmod_failure_cleans_private_scratch_and_never_publishes(tmp_path, monkeypatch):
    payload = archive([("python/a", b"x", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "out"
    mkdtemp = engine_artifact.tempfile.mkdtemp
    allocated = []

    def record_mkdtemp(*args, **kwargs):
        result = Path(mkdtemp(*args, **kwargs))
        allocated.append(result)
        return result

    monkeypatch.setattr(engine_artifact.tempfile, "mkdtemp", record_mkdtemp)
    monkeypatch.setattr(engine_artifact.os, "chmod", lambda *_: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert allocated and not allocated[0].exists()
    assert not destination.exists()


def test_scratch_cleanup_failure_cannot_leave_published_destination(tmp_path, monkeypatch):
    payload = archive([("python/a", b"x", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "out"
    remove = engine_artifact.shutil.rmtree
    attempted = []

    def fail_once(path, *args, **kwargs):
        if Path(path).name.startswith(".engine-artifact-") and not attempted:
            attempted.append(Path(path))
            raise OSError("injected cleanup failure")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(engine_artifact.shutil, "rmtree", fail_once)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert attempted
    assert not destination.exists()
    assert not attempted[0].exists()


def test_reported_rename_failure_rolls_back_only_our_published_tree(tmp_path, monkeypatch):
    payload = archive([("python/a", b"x", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "out"
    rename = engine_artifact.os.rename

    def rename_then_fail(staging, target):
        rename(staging, target)
        raise OSError("injected after rename")

    monkeypatch.setattr(engine_artifact.os, "rename", rename_then_fail)
    with pytest.raises(engine_artifact.ArtifactError):
        engine_artifact.verify_and_extract(
            source, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
        )
    assert not destination.exists()
    assert not list(parent.glob(".engine-artifact-*"))


def test_publication_renames_complete_same_filesystem_tree(tmp_path, monkeypatch):
    payload = archive([("python/bin/app", b"complete", "file")])
    source = tmp_path / "artifact.tgz"
    source.write_bytes(payload)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "out"
    rename = engine_artifact.os.rename
    observed = []

    def verify_before_rename(staging, target):
        staging, target = Path(staging), Path(target)
        assert staging.is_dir() and (staging / "bin/app").read_bytes() == b"complete"
        assert os.stat(staging).st_dev == os.stat(target.parent).st_dev
        result = rename(staging, target)
        observed.append(target)
        assert (target / "bin/app").read_bytes() == b"complete"
        return result

    monkeypatch.setattr(engine_artifact.os, "rename", verify_before_rename)
    engine_artifact.verify_and_extract(
        source, destination, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)
    )
    assert observed == [destination]


def test_cli_is_offline_and_errors_are_sanitized(tmp_path):
    secret_path = tmp_path / "operator-token-secret"
    bad_member = "python/../member-name-secret"
    invalid = archive([(bad_member, b"x", "file")])
    source = tmp_path / "invalid-artifact-name-secret.tgz"
    source.write_bytes(invalid)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", str(source), "--destination",
         str(parent / "out"), "--sha256", hashlib.sha256(invalid).hexdigest(),
         "--size", str(len(invalid))],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 1
    assert result.stderr == "artifact verification failed\n"
    assert str(source) not in result.stderr and bad_member not in result.stderr
    assert "secret" not in result.stderr
    malformed_args = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", "private-path-secret",
         "--destination", "private-destination-secret", "--sha256", "hash-secret",
         "--size", "size-secret"],
        capture_output=True, text=True, timeout=5,
    )
    assert malformed_args.returncode != 0
    assert malformed_args.stderr == "artifact verification failed\n"
    assert "secret" not in malformed_args.stderr

    marker = tmp_path / "artifact-executed"
    code = f"from pathlib import Path; Path({str(marker)!r}).touch()".encode()
    valid = archive([("python/data.py", code, "file")])
    source.write_bytes(valid)
    success = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", str(source), "--destination",
         str(parent / "valid"), "--sha256", hashlib.sha256(valid).hexdigest(),
         "--size", str(len(valid))],
        capture_output=True, text=True, timeout=5,
    )
    assert success.returncode == 0 and success.stderr == ""
    assert (parent / "valid/data.py").read_bytes() == code
    assert not marker.exists()
    assert not secret_path.exists()
    guards = tmp_path / "guards"
    guards.mkdir()
    (guards / "sitecustomize.py").write_text(
        "import os, socket, subprocess\n"
        "def blocked(*args, **kwargs): raise RuntimeError('forbidden external action')\n"
        "socket.socket = blocked\n"
        "socket.create_connection = blocked\n"
        "subprocess.Popen = blocked\n"
        "os.system = blocked\n"
        "os.posix_spawn = blocked\n"
    )
    guarded_env = os.environ.copy()
    guarded_env["PYTHONPATH"] = str(guards)
    guarded = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", str(source), "--destination",
         str(parent / "guarded"), "--sha256", hashlib.sha256(valid).hexdigest(),
         "--size", str(len(valid))],
        capture_output=True, text=True, timeout=5, env=guarded_env,
    )
    assert guarded.returncode == 0 and guarded.stderr == ""
    assert (parent / "guarded/data.py").read_bytes() == code
    assert not marker.exists()
