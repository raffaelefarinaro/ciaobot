import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import gzip
import socket
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

import pytest

MODULE_PATH = Path(__file__).parents[1] / "mac/scripts/engine_runtime.py"
SPEC = importlib.util.spec_from_file_location("engine_runtime", MODULE_PATH)
assert SPEC and SPEC.loader
engine_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine_runtime)
PIN_TEMPLATE = engine_runtime.load_runtime_pin()


def _fixture_archive() -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data, mode in [
            ("python/bin/python3.12", b"inert runtime bytes", 0o755),
            ("python/lib/library", b"library bytes", 0o644),
        ]:
            info = tarfile.TarInfo(name)
            info.mode, info.size = mode, len(data)
            tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("python/bin/python3")
        link.type, link.linkname = tarfile.SYMTYPE, "python3.12"
        tf.addfile(link)
    return gzip.compress(raw.getvalue())


def _fixture_pin(archive: bytes) -> bytes:
    pin = PIN_TEMPLATE.copy()
    pin["provenance"] = PIN_TEMPLATE["provenance"].copy()
    pin["license"] = PIN_TEMPLATE["license"].copy()
    pin["size"] = len(archive)
    pin["sha256"] = hashlib.sha256(archive).hexdigest()
    return json.dumps(pin).encode()


def _private_parent(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def test_committed_pin_matches_reviewed_evidence() -> None:
    pin = engine_runtime.load_runtime_pin()
    assert pin == {
        "schema_version": 1,
        "upstream": "astral-sh/python-build-standalone",
        "release": "20261110",
        "python_version": "3.12.16",
        "target": "aarch64-apple-darwin",
        "flavor": "install_only",
        "filename": "cpython-3.12.16+20261110-aarch64-apple-darwin-install_only.tar.gz",
        "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only.tar.gz",
        "size": 25371130,
        "sha256": "e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f",
        "checksum_url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/SHA256SUMS",
        "release_metadata_url": "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20261110",
        "archive_root": "python/",
        "minimum_macos": "13.0",
        "provenance": {
            "official_metadata_review": "coordinator comparison recorded in #3128 planning evidence",
            "source_revision": None,
            "source_revision_status": "not recorded in supplied review evidence",
        },
        "license": {
            "member": "python/lib/python3.12/LICENSE.txt",
            "size": 13936,
            "sha256": "3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf",
        },
    }


def test_adapter_passes_exact_committed_hash_and_size_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append((a, k)))
    engine_runtime.extract_pinned_runtime(Path("archive"), Path("destination"))
    assert calls == [
        ((Path("archive"), Path("destination")), {
            "sha256": "e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f",
            "size": 25371130,
        })
    ]


@pytest.mark.parametrize(("needle", "duplicate"), [
    ('"schema_version": 1,', '"schema_version": 0, "schema_version": 1,'),
    ('"source_revision": null,', '"source_revision": "invented", "source_revision": null,'),
])
def test_duplicate_keys_are_rejected_at_every_object_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, needle: str, duplicate: str
) -> None:
    raw = json.dumps(engine_runtime.load_runtime_pin()).replace(needle, duplicate, 1).encode()
    assert duplicate.encode() in raw
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: raw)
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    source = tmp_path / "source"
    source.write_bytes(b"source sentinel")
    destination = tmp_path / "destination"
    destination.write_bytes(b"destination sentinel")
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(source, destination)
    assert calls == []
    assert source.read_bytes() == b"source sentinel"
    assert destination.read_bytes() == b"destination sentinel"


@pytest.mark.parametrize("raw", [
    b"", b"\xff", b"{",
])
def test_pin_reader_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: raw)
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.load_runtime_pin()


def test_nonfinite_constant_is_rejected_by_parser_before_schema_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(engine_runtime.load_runtime_pin()).replace(
        '"schema_version": 1,', '"schema_version": NaN,', 1
    ).encode()
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: raw)
    parser_constants = []
    reject_constant = engine_runtime._reject_constant

    def observe_constant(value: str) -> None:
        parser_constants.append(value)
        reject_constant(value)

    monkeypatch.setattr(engine_runtime, "_reject_constant", observe_constant)
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("source"), Path("destination"))
    assert parser_constants == ["NaN"]
    assert calls == []


def test_pin_reader_bounds_at_65536_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    valid = json.dumps(engine_runtime.load_runtime_pin()).encode()
    exact = valid + b" " * (65536 - len(valid))
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: exact)
    assert engine_runtime.load_runtime_pin()["schema_version"] == 1
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: exact + b" ")
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.load_runtime_pin()


def test_pin_reader_bounds_before_json_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: b"x" * 65537)
    parsed = []
    calls = []
    monkeypatch.setattr(engine_runtime.json, "loads", lambda *args, **kwargs: parsed.append(args))
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("source"), Path("destination"))
    assert parsed == []
    assert calls == []


def test_pin_reader_requests_at_most_limit_plus_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "pin.json"
    manifest.write_bytes(b"x" * 70000)
    original_open = Path.open
    requests = []

    class ReadSpy:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def read(self, size):
            requests.append(size)
            return self.wrapped.read(size)

    def spy_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return ReadSpy(handle) if path == manifest else handle

    monkeypatch.setattr(engine_runtime, "MANIFEST", manifest)
    monkeypatch.setattr(Path, "open", spy_open)
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.load_runtime_pin()
    assert requests == [65537]


@pytest.mark.parametrize(("field", "value"), [
    ("target", "x86_64-unknown-linux-gnu"),
    ("filename", "other.tar.gz"),
    ("url", "http://github.com/fake"),
    ("checksum_url", "https://github.com.evil.invalid/checksums"),
    ("release_metadata_url", "https://api.github.com/other"),
    ("size", True),
    ("sha256", "A" * 64),
])
def test_pin_identity_and_types_fail_closed(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    pin = engine_runtime.load_runtime_pin()
    pin[field] = value
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: json.dumps(pin).encode())
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("source"), Path("destination"))
    assert calls == []


@pytest.mark.parametrize(("path", "replacement"), [
    (("schema_version",), True),
    (("schema_version",), 2),
    (("size",), 0),
    (("size",), -1),
    (("size",), 512 * 1024 * 1024 + 1),
    (("size",), "25371130"),
    (("sha256",), "short"),
    (("python_version",), "3.13.1"),
    (("python_version",), "3.12.01"),
    (("release",), "2026111"),
    (("flavor",), "shared"),
    (("archive_root",), "other/"),
    (("minimum_macos",), "12.0"),
    (("upstream",), "unreviewed/project"),
    (("license", "size"), True),
    (("license", "sha256"), "0" * 64),
    (("license", "member"), None),
    (("license",), []),
    (("provenance",), []),
    (("provenance", "source_revision"), "invented-revision"),
    (("provenance", "source_revision_status"), "available"),
    (("provenance", "extra"), "unexpected"),
])
def test_invalid_pin_never_reaches_extractor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: tuple[str, ...], replacement: object
) -> None:
    pin = engine_runtime.load_runtime_pin()
    target = pin
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement
    raw = json.dumps(pin).encode()
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: raw)
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append((a, k)))
    source = tmp_path / "source"
    source.write_bytes(b"untouched source")
    destination = tmp_path / "existing"
    destination.write_bytes(b"untouched destination")
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(source, destination)
    assert calls == []
    assert source.read_bytes() == b"untouched source"
    assert destination.read_bytes() == b"untouched destination"


@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_pin_rejects_missing_and_unknown_fields(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    pin = engine_runtime.load_runtime_pin()
    if change == "missing":
        del pin["license"]
    else:
        pin["unexpected"] = "value"
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: json.dumps(pin).encode())
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("source"), Path("destination"))
    assert calls == []


@pytest.mark.parametrize(("field", "value"), [
    ("release", "20261111"),
    ("python_version", "3.12.17"),
    ("filename", "other.tar.gz"),
])
def test_url_identity_validation_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    pin = engine_runtime.load_runtime_pin()
    pin[field] = value
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: json.dumps(pin).encode())
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("archive"), Path("destination"))
    assert calls == []


@pytest.mark.parametrize(("component", "value"), [
    ("scheme", "http"),
    ("netloc", "user:pass@github.com"),
    ("netloc", "github.com:443"),
    ("netloc", "github.com.invalid"),
    ("query", "x=1"),
    ("fragment", "fragment"),
])
def test_malformed_urls_change_one_component_of_canonical_url(
    monkeypatch: pytest.MonkeyPatch, component: str, value: str
) -> None:
    pin = engine_runtime.load_runtime_pin()
    parsed = urllib.parse.urlsplit(pin["url"])
    pin["url"] = urllib.parse.urlunsplit(parsed._replace(**{component: value}))
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: json.dumps(pin).encode())
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("archive"), Path("destination"))
    assert calls == []


@pytest.mark.parametrize(("field", "altered"), [
    ("url", lambda value: value.replace("%2B", "+")),
    ("url", lambda value: value.replace("%2B", "%2b")),
    ("checksum_url", lambda value: value.replace("/20261110/", "/20261109/")),
    ("release_metadata_url", lambda value: value.replace("/20261110", "/20261109")),
])
def test_canonical_url_encoding_and_identity_are_exact(
    monkeypatch: pytest.MonkeyPatch, field: str, altered
) -> None:
    pin = engine_runtime.load_runtime_pin()
    pin[field] = altered(pin[field])
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: json.dumps(pin).encode())
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("archive"), Path("destination"))
    assert calls == []


def _sync_identity_urls(pin: dict) -> None:
    filename = (
        f"cpython-{pin['python_version']}+{pin['release']}-"
        f"{pin['target']}-{pin['flavor']}.tar.gz"
    )
    base = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{pin['release']}/"
    )
    pin["filename"] = filename
    pin["url"] = base + filename.replace("+", "%2B")
    pin["checksum_url"] = base + "SHA256SUMS"
    pin["release_metadata_url"] = (
        "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/"
        f"{pin['release']}"
    )


@pytest.mark.parametrize(("field", "value"), [
    ("release", "٢٠٢٦١١١٠"),
    ("python_version", "3.12.١٦"),
])
def test_pin_numeric_identity_requires_ascii_digits(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    pin = engine_runtime.load_runtime_pin()
    pin[field] = value
    _sync_identity_urls(pin)
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: json.dumps(pin, ensure_ascii=False).encode())
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("archive"), Path("destination"))
    assert calls == []


@pytest.mark.parametrize(("field", "value"), [
    ("release", "2026111"),
    ("python_version", "3.12.016"),
])
def test_malformed_ascii_numeric_identity_rejected_after_dependent_fields_match(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    pin = engine_runtime.load_runtime_pin()
    pin[field] = value
    _sync_identity_urls(pin)
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: json.dumps(pin).encode())
    calls = []
    monkeypatch.setattr(engine_runtime.engine_artifact, "verify_and_extract", lambda *a, **k: calls.append(1))
    with pytest.raises(engine_runtime.RuntimePinError):
        engine_runtime.extract_pinned_runtime(Path("archive"), Path("destination"))
    assert pin["filename"] == (
        f"cpython-{pin['python_version']}+{pin['release']}-{pin['target']}-{pin['flavor']}.tar.gz"
    )
    assert pin["url"].endswith(pin["filename"].replace("+", "%2B"))
    assert pin["checksum_url"].endswith(f"/{pin['release']}/SHA256SUMS")
    assert pin["release_metadata_url"].endswith(f"/{pin['release']}")
    assert calls == []


def test_reader_uses_repository_location_not_cwd_or_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "engine-runtime.json").write_text('{"bogus": true}')
    (tmp_path / "engine_artifact.py").write_text('raise AssertionError("wrong sibling imported")\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_ENGINE_RUNTIME", str(tmp_path / "engine-runtime.json"))
    monkeypatch.setenv("ENGINE_RUNTIME_PIN", str(tmp_path / "engine-runtime.json"))
    assert engine_runtime.load_runtime_pin()["release"] == "20261110"
    assert Path(engine_runtime.engine_artifact.__file__).resolve() == (
        MODULE_PATH.with_name("engine_artifact.py").resolve()
    )
    marker = tmp_path / "imported"
    (tmp_path / "engine_artifact.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()\n")
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", "archive", "--destination", "dest"],
        cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "runtime verification failed"
    assert not marker.exists()


def test_file_path_import_ignores_hostile_cwd_pythonpath_and_preloaded_module(tmp_path: Path) -> None:
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    marker = hostile / "poison-imported"
    (hostile / "engine_artifact.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch(); raise AssertionError('poison')\n"
    )
    (hostile / "engine-runtime.json").write_text('{"wrong": true}')
    check = """
import importlib.util, sys, types
from pathlib import Path
runtime_path = Path(sys.argv[1])
expected_helper = Path(sys.argv[2]).resolve()
before = sys.path.copy()
poison = types.ModuleType("engine_artifact")
poison.__file__ = "preloaded-poison"
sys.modules["engine_artifact"] = poison
spec = importlib.util.spec_from_file_location("isolated_runtime", runtime_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert Path(module.engine_artifact.__file__).resolve() == expected_helper
assert sys.path == before
assert sys.modules["engine_artifact"] is poison
assert module.load_runtime_pin()["release"] == "20261110"
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(hostile)
    result = subprocess.run(
        [sys.executable, "-c", check, str(MODULE_PATH), str(MODULE_PATH.with_name("engine_artifact.py"))],
        cwd=hostile, env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_cli_rejects_raw_dot_traversal_and_overrides(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    source = tmp_path / "archive-marker"
    source.write_bytes(b"inert source")
    script = str(MODULE_PATH)
    for destination, extra, code in [
        (f"{private}/./out", [], 1),
        (str(private / "out"), ["--sha256", "secret-marker"], 2),
    ]:
        result = subprocess.run(
            [sys.executable, script, "--archive", str(source), "--destination", destination, *extra],
            cwd=tmp_path, text=True, capture_output=True, check=False,
        )
        assert result.returncode == code
        assert result.stderr.strip() == "runtime verification failed"
        assert "marker" not in result.stderr and "Traceback" not in result.stderr
        assert source.read_bytes() == b"inert source"
        assert list(private.iterdir()) == []
    result = subprocess.run(
        [sys.executable, script, "--archive", str(source), "--destination", f"{private}/../out"],
        cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "runtime verification failed"
    assert source.read_bytes() == b"inert source"
    assert list(private.iterdir()) == []


@pytest.mark.parametrize("suffix", ["/./out", "/../out"])
def test_cli_raw_dot_paths_stop_before_adapter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, suffix: str
) -> None:
    calls = []
    monkeypatch.setattr(engine_runtime, "extract_pinned_runtime", lambda *a: calls.append(a))
    monkeypatch.setattr(
        sys, "argv",
        ["engine_runtime.py", "--archive", "inert-source", "--destination", f"{tmp_path}{suffix}"],
    )
    assert engine_runtime.main() == 1
    assert capsys.readouterr().err.strip() == "runtime verification failed"
    assert calls == []


@pytest.mark.parametrize("extra", [
    ["--sha256", "secret-marker"],
    ["--size", "123"],
    ["--pin", "secret-marker"],
    ["--url", "https://example.invalid"],
    ["--arch", "anything"],
])
def test_cli_rejects_every_override(tmp_path: Path, extra: list[str]) -> None:
    source = tmp_path / "source-marker"
    source.write_bytes(b"inert source")
    destination = tmp_path / "destination-marker"
    destination.write_bytes(b"existing destination")
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", str(source), "--destination", str(destination), *extra],
        cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert result.stderr.strip() == "runtime verification failed"
    assert "marker" not in result.stderr and "Traceback" not in result.stderr
    assert source.read_bytes() == b"inert source"
    assert destination.read_bytes() == b"existing destination"


def test_cli_rejects_empty_destination(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", "archive-marker", "--destination", ""],
        cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "runtime verification failed"


def test_cli_mismatched_local_archive_is_sanitized(tmp_path: Path) -> None:
    archive = tmp_path / "archive-marker"
    archive.write_bytes(b"wrong bytes")
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    destination = parent / "output-marker"
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--archive", str(archive), "--destination", str(destination)],
        cwd=tmp_path, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "runtime verification failed"
    assert "marker" not in result.stderr and "Traceback" not in result.stderr
    assert archive.read_bytes() == b"wrong bytes"
    assert not destination.exists()
    assert list(parent.iterdir()) == []


def test_fixture_prefix_extracts_inert_files_with_real_extractor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_bytes = _fixture_archive()
    archive = tmp_path / "input.tar.gz"
    archive.write_bytes(archive_bytes)
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: _fixture_pin(archive_bytes))
    parent = _private_parent(tmp_path / "private")
    destination = parent / "prefix"
    engine_runtime.extract_pinned_runtime(archive, destination)
    assert (destination / "bin/python3.12").read_bytes() == b"inert runtime bytes"
    assert (destination / "lib/library").read_bytes() == b"library bytes"
    assert (destination / "bin/python3").resolve() == (destination / "bin/python3.12").resolve()
    assert (destination / "bin/python3").is_symlink()
    assert os.readlink(destination / "bin/python3") == "python3.12"
    assert (destination / "bin/python3.12").stat().st_mode & 0o777 == 0o755
    assert (destination / "lib/library").stat().st_mode & 0o777 == 0o644
    assert {p.relative_to(destination).as_posix() for p in destination.rglob("*")} == {
        "bin", "bin/python3.12", "bin/python3", "lib", "lib/library",
    }
    assert archive.read_bytes() == archive_bytes


@pytest.mark.parametrize("size_delta", [0, -1, 1])
def test_mismatched_bytes_and_size_preserve_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size_delta: int
) -> None:
    archive_bytes = _fixture_archive()
    source = tmp_path / "source.tar.gz"
    source.write_bytes(archive_bytes)
    pin = engine_runtime.load_runtime_pin()
    pin["size"] = len(archive_bytes) + size_delta
    pin["sha256"] = "0" * 64 if size_delta == 0 else hashlib.sha256(archive_bytes).hexdigest()
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: json.dumps(pin).encode())
    parent = _private_parent(tmp_path / "private")
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"preserve")
    with pytest.raises(engine_runtime.engine_artifact.ArtifactError):
        engine_runtime.extract_pinned_runtime(source, parent / "prefix")
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(archive_bytes).digest()
    assert list(parent.iterdir()) == []
    assert outside.read_bytes() == b"preserve"


def test_mismatched_bytes_and_failed_extract_preserve_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_bytes = _fixture_archive()
    source = tmp_path / "source.tar.gz"
    source.write_bytes(archive_bytes)
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: _fixture_pin(archive_bytes))
    monkeypatch.setattr(
        engine_runtime.engine_artifact, "_copy_member",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected copy failure")),
    )
    parent = _private_parent(tmp_path / "private")
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"preserve")
    with pytest.raises(engine_runtime.engine_artifact.ArtifactError):
        engine_runtime.extract_pinned_runtime(source, parent / "prefix")
    assert source.read_bytes() == archive_bytes
    assert not (parent / "prefix").exists()
    assert list(parent.iterdir()) == []
    assert outside.read_bytes() == b"preserve"


def test_destination_refusals_preserve_existing_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_bytes = _fixture_archive()
    source = tmp_path / "source.tar.gz"
    source.write_bytes(archive_bytes)
    monkeypatch.setattr(engine_runtime, "_read_pin_bytes", lambda: _fixture_pin(archive_bytes))
    parent = _private_parent(tmp_path / "private")
    existing_file = parent / "existing-file"
    existing_file.write_bytes(b"file sentinel")
    existing_dir = parent / "existing-dir"
    existing_dir.mkdir()
    (existing_dir / "sentinel").write_bytes(b"dir sentinel")
    dangling = parent / "dangling"
    dangling.symlink_to(parent / "missing")
    for destination in (existing_file, existing_dir, dangling):
        with pytest.raises(engine_runtime.engine_artifact.ArtifactError):
            engine_runtime.extract_pinned_runtime(source, destination)
    assert existing_file.read_bytes() == b"file sentinel"
    assert (existing_dir / "sentinel").read_bytes() == b"dir sentinel"
    assert dangling.is_symlink() and not dangling.exists()
    assert source.read_bytes() == archive_bytes
    assert (parent.stat().st_mode & 0o777) == 0o700

    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o755)
    os.chmod(public_parent, 0o755)
    with pytest.raises(engine_runtime.engine_artifact.ArtifactError):
        engine_runtime.extract_pinned_runtime(source, public_parent / "prefix")
    assert (public_parent.stat().st_mode & 0o777) == 0o755

    symlink = tmp_path / "ancestor-link"
    symlink.symlink_to(parent, target_is_directory=True)
    with pytest.raises(engine_runtime.engine_artifact.ArtifactError):
        engine_runtime.extract_pinned_runtime(source, symlink / "through-link")
    assert symlink.is_symlink() and source.read_bytes() == archive_bytes

    overlap_parent = _private_parent(tmp_path / "overlap-parent")
    overlap_destination = overlap_parent / "prefix"
    overlap_destination.mkdir()
    overlap_source = overlap_destination / "source.tar.gz"
    overlap_source.write_bytes(archive_bytes)
    overlap_errors = []
    safe_parent_calls = []
    original_check_overlap = engine_runtime.engine_artifact._check_no_overlap
    original_safe_parent = engine_runtime.engine_artifact._safe_parent

    def observe_overlap(source_path: Path, destination_path: Path) -> None:
        try:
            original_check_overlap(source_path, destination_path)
        except engine_runtime.engine_artifact.ArtifactError:
            overlap_errors.append((source_path, destination_path))
            raise

    def observe_safe_parent(path: Path) -> Path:
        safe_parent_calls.append(path)
        return original_safe_parent(path)

    monkeypatch.setattr(engine_runtime.engine_artifact, "_check_no_overlap", observe_overlap)
    monkeypatch.setattr(engine_runtime.engine_artifact, "_safe_parent", observe_safe_parent)
    with pytest.raises(engine_runtime.engine_artifact.ArtifactError):
        engine_runtime.extract_pinned_runtime(overlap_source, overlap_destination)
    assert overlap_errors == [(overlap_source, overlap_destination)]
    assert safe_parent_calls == []
    assert (overlap_parent.stat().st_mode & 0o777) == 0o700
    assert overlap_source.read_bytes() == archive_bytes


def test_adapter_has_no_network_or_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_bytes = _fixture_archive()
    source = tmp_path / "source.tar.gz"
    source.write_bytes(archive_bytes)
    manifest = tmp_path / "fixture-pin.json"
    manifest.write_bytes(_fixture_pin(archive_bytes))
    monkeypatch.setattr(engine_runtime, "MANIFEST", manifest)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network or execution attempted")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "execv", forbidden)
    monkeypatch.setattr(os, "execve", forbidden)
    monkeypatch.setattr(os, "spawnv", forbidden)
    monkeypatch.setattr(os, "posix_spawn", forbidden)
    parent = _private_parent(tmp_path / "private")
    assert engine_runtime.load_runtime_pin()["sha256"] == hashlib.sha256(archive_bytes).hexdigest()
    engine_runtime.extract_pinned_runtime(source, parent / "prefix")
    assert (parent / "prefix/bin/python3.12").read_bytes() == b"inert runtime bytes"
    assert source.read_bytes() == archive_bytes
