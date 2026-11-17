import base64
import copy
import gzip
import hashlib
import importlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import warnings
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
MODULE_PATH = REPO / "mac/scripts/engine_derived_wheel.py"
SPEC = importlib.util.spec_from_file_location("engine_derived_wheel", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)
COMMITTED = json.loads((REPO / "mac/engine-build-inputs.json").read_text())

ROOT = "http_ece-1.2.1"
EPOCH = 1723075847
LICENSE_BYTES = b"MIT licence text\n"
PKG_INFO = b"Metadata-Version: 2.1\nName: http_ece\nVersion: 1.2.1\nLicense: MIT\n"
LICENCE_REL = "mac/licenses/http-ece-LICENSE"
MODULE_BYTES = b"VALUE = 1\n"

# The stub stands in for setuptools.build_meta. `__OPTS__` selects a misbehaviour
# and `__PRELUDE__` is executed first, with its failures swallowed.
STUB = '''
import base64, hashlib, io, os, sys, time, zipfile
OPTS = __OPTS__
__legacy__ = sys.modules[__name__]

def get_requires_for_build_wheel(config_settings=None):
    return OPTS.get("requires", [])

def _b64(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    try:
__PRELUDE__
    except BaseException:
        pass
    o = OPTS
    t = time.gmtime(int(os.environ["SOURCE_DATE_EPOCH"]))
    stamp = (1999, 1, 1, 0, 0, 0) if o.get("ts_off") else tuple(t[:6])
    name, ver, tag = o.get("name", "http_ece"), o.get("version", "1.2.1"), o.get("tag", "py3")
    di = f"{name}-{ver}.dist-info"
    body = open("http_ece/__init__.py", "rb").read() + (b"#" if o.get("mutate") else b"")
    requires = o.get("requires_dist", ["cryptography>=2.5"])
    meta = f"Metadata-Version: 2.1\\nName: {o.get('meta_name', 'http-ece')}\\nVersion: {o.get('meta_version', ver)}\\n"
    meta += "".join(f"Requires-Dist: {r}\\n" for r in requires)
    tags = "".join(f"Tag: {p}-none-any\\n" for p in o.get("wheel_tag", tag).split("."))
    wheel = f"Wheel-Version: 1.0\\nGenerator: stub\\nRoot-Is-Purelib: true\\n{tags}"
    files = {
        "http_ece/__init__.py": body,
        f"{di}/METADATA": meta.encode(),
        f"{di}/WHEEL": wheel.encode(),
        f"{di}/top_level.txt": b"http_ece\\n",
        f"{di}/licenses/LICENSE": open("LICENSE", "rb").read(),
    }
    if o.get("no_licence"):
        del files[f"{di}/licenses/LICENSE"]
    for extra in o.get("extra", []):
        files[extra[0]] = extra[1].encode()
    rows = [[p, "sha256=" + _b64(d), str(len(d))] for p, d in sorted(files.items())]
    if o.get("bad_record"):
        rows[0][1] = "sha256=" + _b64(b"other")
    rows.append([f"{di}/RECORD", "", ""])
    files[f"{di}/RECORD"] = "".join(",".join(r) + "\\n" for r in rows).encode()

    def emit(filename):
        with zipfile.ZipFile(os.path.join(wheel_directory, filename), "w") as zf:
            for path in sorted(files):
                info = zipfile.ZipInfo(path, date_time=stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, files[path])
            if o.get("symlink"):
                link = zipfile.ZipInfo("http_ece/link.py", date_time=stamp)
                link.external_attr = 0o120777 << 16
                zf.writestr(link, b"target")

    emit(f"{name}-{ver}-{tag}-none-any.whl")
    if o.get("two"):
        emit(f"{name}-{ver}-py2-none-any.whl")
    return "x"
'''


def make_stub(prelude: str = "pass", **opts) -> str:
    body = "\n".join("        " + line for line in prelude.splitlines())
    return STUB.replace("__OPTS__", repr(opts)).replace("__PRELUDE__", body)


def sdist_bytes(files=None, members=None) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        if members is None:
            files = files or {
                "http_ece/__init__.py": MODULE_BYTES,
                "PKG-INFO": PKG_INFO,
                "setup.py": b"# stub\n",
            }
            members = []
            root = tarfile.TarInfo(ROOT)
            root.type = tarfile.DIRTYPE
            members.append((root, b""))
            for path, data in files.items():
                members.append((tarfile.TarInfo(f"{ROOT}/{path}"), data))
        for info, data in members:
            if info.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            else:
                tf.addfile(info)
    return gzip.compress(raw.getvalue(), mtime=0)


def backend_bytes(source: str | None = None, entries=None, name="setuptools", version="84.2.1") -> bytes:
    raw = io.BytesIO()
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    entries = entries if entries is not None else [
        ("setuptools/__init__.py", b""),
        ("setuptools/build_meta.py", (source or make_stub()).encode()),
        ("setuptools-84.2.1.dist-info/METADATA", meta),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in entries:
                if isinstance(entry, zipfile.ZipInfo):
                    zf.writestr(entry, b"x")
                else:
                    zf.writestr(entry[0], entry[1])
    return raw.getvalue()


def make_pin(sdist: bytes, backend: bytes, backend_name="setuptools.build_meta:__legacy__", wheel=None, pkg_info=None):
    pin = copy.deepcopy(COMMITTED)
    entry = pin["derived_wheels"][0]
    entry["sdist"].update(size=len(sdist), sha256=hashlib.sha256(sdist).hexdigest())
    pin["build_tools"][0].update(size=len(backend), sha256=hashlib.sha256(backend).hexdigest())
    entry["license"]["declared_in"]["sha256"] = hashlib.sha256(
        PKG_INFO if pkg_info is None else pkg_info).hexdigest()
    entry["license"]["text_file"].update(size=len(LICENSE_BYTES), sha256=hashlib.sha256(LICENSE_BYTES).hexdigest())
    entry["build"]["backend"] = backend_name
    if wheel is not None:
        entry["wheel"] = wheel
    return pin


def inject(monkeypatch, pin) -> bytes:
    raw = json.dumps(pin).encode()
    monkeypatch.setattr(mod, "_read_inputs_bytes", lambda: raw)
    return raw


@pytest.fixture(autouse=True)
def committed_licence_dir(tmp_path_factory, monkeypatch) -> Path:
    root = tmp_path_factory.mktemp("repo-root")
    (root / "mac/licenses").mkdir(parents=True)
    (root / LICENCE_REL).write_bytes(LICENSE_BYTES)
    monkeypatch.setattr(mod, "REPO_ROOT", root)
    return root


def private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


class Env:
    def __init__(self, tmp_path: Path, sdist=None, backend=None, **pin_kwargs):
        self.sdist_data = sdist if sdist is not None else sdist_bytes()
        self.backend_data = backend if backend is not None else backend_bytes()
        self.parent = private(tmp_path / "parent")
        self.sdist = tmp_path / "http_ece-1.2.1.tar.gz"
        self.backend = tmp_path / "setuptools-84.2.1-py3-none-any.whl"
        self.sdist.write_bytes(self.sdist_data)
        self.backend.write_bytes(self.backend_data)
        self.pin = make_pin(self.sdist_data, self.backend_data, **pin_kwargs)
        self.destination = self.parent / "out"

    def build(self, parent: Path | None = None):
        destination = (parent / "out") if parent else self.destination
        return mod.build_derived_wheel("http-ece", self.sdist, self.backend, destination)


def stub_env(tmp_path, monkeypatch, prelude="pass", sdist=None, backend_name=None, **opts) -> Env:
    kwargs = {"backend_name": backend_name} if backend_name else {}
    env = Env(tmp_path, sdist=sdist, backend=backend_bytes(make_stub(prelude, **opts)), **kwargs)
    inject(monkeypatch, env.pin)
    return env


def assert_refused(env: Env) -> None:
    with pytest.raises(mod.DerivedWheelError):
        env.build()
    assert list(env.parent.iterdir()) == []


class Spies:
    def __init__(self, monkeypatch):
        self.calls = {"tar": 0, "zip": 0, "import": 0}
        real_tar, real_zip, real_import = tarfile.open, zipfile.ZipFile, importlib.import_module

        def tar(*a, **k):
            self.calls["tar"] += 1
            return real_tar(*a, **k)

        def zipper(*a, **k):
            self.calls["zip"] += 1
            return real_zip(*a, **k)

        def imp(name, *a, **k):
            if name.startswith("setuptools"):
                self.calls["import"] += 1
            return real_import(name, *a, **k)

        monkeypatch.setattr(tarfile, "open", tar)
        monkeypatch.setattr(zipfile, "ZipFile", zipper)
        monkeypatch.setattr(importlib, "import_module", imp)


# ------------------------------------------------------------------ pins


REVIEWED_MEMBERS = [
    ("http_ece-1.2.1.dist-info/METADATA", 1136),
    ("http_ece-1.2.1.dist-info/RECORD", 478),
    ("http_ece-1.2.1.dist-info/WHEEL", 109),
    ("http_ece-1.2.1.dist-info/licenses/LICENSE", 1081),
    ("http_ece-1.2.1.dist-info/top_level.txt", 9),
    ("http_ece/__init__.py", 13073),
]


def test_committed_inputs_match_reviewed_values() -> None:
    raw = (REPO / "mac/engine-build-inputs.json").read_bytes()
    data = json.loads(raw)
    inputs = mod.load_build_inputs()
    assert inputs.data == data and inputs.sha256 == hashlib.sha256(raw).hexdigest()
    assert data["schema_version"] == 1
    assert data["build_tools"] == [{
        "name": "setuptools",
        "version": "84.2.1",
        "filename": "setuptools-84.2.1-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/ef/55/6dc6d6bb953bdfc52519a0059592c52b6e3b8ad51cf8256dffceb55b0610/setuptools-84.2.1-py3-none-any.whl",
        "size": 876263,
        "sha256": "ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2",
    }]
    (entry,) = data["derived_wheels"]
    assert entry["name"] == "http-ece" and entry["version"] == "1.2.1"
    assert entry["sdist"] == {
        "filename": "http_ece-1.2.1.tar.gz",
        "url": "https://files.pythonhosted.org/packages/7c/af/249d1576653b69c20b9ac30e284b63bd94af6a175d72d87813235caf2482/http_ece-1.2.1.tar.gz",
        "size": 8830,
        "sha256": "8c6ab23116bbf6affda894acfd5f2ca0fb8facbcbb72121c11c75c33e7ce8cff",
    }
    assert entry["build"] == {
        "tool": "setuptools",
        "backend": "setuptools.build_meta:__legacy__",
        "source_date_epoch": 1723075847,
        "umask": "022",
    }
    assert entry["build"]["source_date_epoch"] == EPOCH
    wheel = entry["wheel"]
    assert wheel["filename"] == "http_ece-1.2.1-py2.py3-none-any.whl"
    assert wheel["size"] == 5717
    assert wheel["sha256"] == "696199016b82de2b91f936d01ddf636769174fcc2766f9b1a741c12676beea80"
    assert wheel["requires_dist"] == ["cryptography>=2.5"]
    assert [(m["path"], m["size"]) for m in wheel["members"]] == REVIEWED_MEMBERS
    assert [m["sha256"] for m in wheel["members"]] == [
        "66a0ad64af9c95f34fa525de5debd85570a76686b1576d78406a0446fcbf04e4",
        "a48e4d0541a46780fbcc309c9610ef73741801fdd5c4873af8e69fe21dc599a4",
        "52f960d9a36510a76296cc85489b75417e667b47846c28d0cd1de765e164bf2e",
        "7d71f48c474da1de82fbdaac7fea5a1eaa41e4ac998dd44fd2897812812138d9",
        "fbbe03a364f0c71fa6a5da8082763a4a301f86771d8f92ce2bd2269976c3b63e",
        "2ddd0f18e1b0c287fe6bea53be7fbab1d950fe15b7184bb80ffabd98c40433f9",
    ]
    committed_text = (REPO / "mac/licenses/http-ece-LICENSE").read_bytes()
    licence = entry["license"]
    assert {k: v for k, v in licence.items() if k != "note"} == {
        "spdx": "MIT",
        "declared_in": {
            "sdist_member": "http_ece-1.2.1/PKG-INFO",
            "sha256": "005011863b66a1f73e228f95ac3ba2d58959b2d293f134c264947974bf3862de",
        },
        "text_file": {
            "path": "mac/licenses/http-ece-LICENSE",
            "size": 1081,
            "sha256": "7d71f48c474da1de82fbdaac7fea5a1eaa41e4ac998dd44fd2897812812138d9",
        },
        "source_url": "https://raw.githubusercontent.com/martinthomson/encrypted-content-encoding/master/python/LICENSE",
    }
    assert "no licence file" in licence["note"]
    assert "provenance only, not machine-checked" in licence["note"]
    assert len(committed_text) == 1081 and b"Copyright (c) 2015 Martin Thomson" in committed_text
    assert hashlib.sha256(committed_text).hexdigest() == licence["text_file"]["sha256"]


def test_committed_wheel_pin_is_self_consistent_and_verifiable_in_shape() -> None:
    """The real wheel is not in the repo (no network here), so check the pin's shape.

    The pin must be accepted by the loader, name a wheel `verify_derived_wheel` would look up,
    carry the licence text it hashes, and list members `_check_wheel` could produce.
    """
    entry = mod.load_build_inputs().data["derived_wheels"][0]
    wheel = entry["wheel"]
    assert wheel["filename"] in mod._wheel_filenames(entry["name"], entry["version"])
    paths = [m["path"] for m in wheel["members"]]
    assert paths == sorted(paths) and len(set(paths)) == len(paths)
    by_path = {m["path"]: m for m in wheel["members"]}
    assert by_path["http_ece-1.2.1.dist-info/licenses/LICENSE"]["sha256"] == entry["license"]["text_file"]["sha256"]
    assert by_path["http_ece-1.2.1.dist-info/licenses/LICENSE"]["size"] == entry["license"]["text_file"]["size"]
    assert sum(m["size"] for m in wheel["members"]) <= mod.MAX_WHEEL_BYTES
    # Every non-module member is one _check_wheel's fixed set allows.
    fixed = {f"http_ece-1.2.1.dist-info/{n}" for n in ("METADATA", "WHEEL", "RECORD", "top_level.txt", "licenses/LICENSE")}
    assert set(paths) - fixed == {"http_ece/__init__.py"}


def test_pin_sdist_matches_uv_lock() -> None:
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    (package,) = [p for p in lock["package"] if p["name"] == "http-ece"]
    entry = mod.load_build_inputs().data["derived_wheels"][0]
    assert package["version"] == entry["version"]
    assert "wheels" not in package
    assert package["sdist"]["url"] == entry["sdist"]["url"]
    assert package["sdist"]["hash"] == "sha256:" + entry["sdist"]["sha256"]
    assert package["sdist"]["size"] == entry["sdist"]["size"]


def _dup(text_key: str, insert: str):
    def apply(raw: str) -> str:
        if text_key == "":
            return "{" + insert + raw[1:]
        index = raw.index(text_key) + len(text_key)
        return raw[:index] + insert + raw[index:]
    return apply


DUPLICATES = [
    _dup("", '"schema_version": 1, '),
    _dup('"build_tools": [{', '"size": 1, '),
    _dup('"derived_wheels": [{', '"name": "x", '),
    _dup('"sdist": {', '"size": 1, '),
    _dup('"license": {', '"spdx": "MIT", '),
    _dup('"build": {', '"tool": "setuptools", '),
    _dup('"wheel": {', '"size": 1, '),
    _dup('"members": [{', '"size": 1, '),
]


def _mutations():
    def tool(**kw):
        return lambda d: d["build_tools"][0].update(kw)

    def entry(section, **kw):
        return lambda d: d["derived_wheels"][0][section].update(kw)

    base = "https://files.pythonhosted.org/packages/7c/af/x/http_ece-1.2.1.tar.gz"
    cases = {
        "bool-size": tool(size=True),
        "zero-size": tool(size=0),
        "uppercase-digest": tool(sha256="CA140B45D5325A9478F880E351484F62FEF15F336462E449CA0C03C873D589C2"),
        "short-digest": entry("sdist", sha256="ab"),
        "http-url": entry("sdist", url=base.replace("https", "http")),
        "credentials": entry("sdist", url=base.replace("files.", "u:p@files.")),
        "port": entry("sdist", url=base.replace(".org", ".org:443")),
        "query": entry("sdist", url=base + "?x=1"),
        "fragment": entry("sdist", url=base + "#x"),
        "other-host": entry("sdist", url=base.replace("files.pythonhosted.org", "example.org")),
        "basename": entry("sdist", url=base.replace("http_ece-1.2.1", "other-1.2.1")),
        "dotdot": entry("sdist", url=base.replace("/x/", "/../")),
        "wrong-sdist-name": entry("sdist", filename="other-1.2.1.tar.gz"),
        "wrong-backend": entry("build", backend="hatchling.build"),
        "wrong-umask": entry("build", umask="077"),
        "epoch-1979": entry("build", source_date_epoch=315532799),
        "epoch-bool": entry("build", source_date_epoch=True),
        "wrong-tag": entry("wheel", filename="http_ece-1.2.1-cp312-none-any.whl"),
        "unsorted-members": lambda d: d["derived_wheels"][0]["wheel"].update(members=[
            {"path": "http_ece/a.py", "size": 1, "sha256": "0" * 64},
            {"path": "http_ece/__init__.py", "size": 1, "sha256": "0" * 64}]),
        "duplicate-members": lambda d: d["derived_wheels"][0]["wheel"].update(members=[
            {"path": "http_ece/a.py", "size": 1, "sha256": "0" * 64}] * 2),
        "absolute-member": lambda d: d["derived_wheels"][0]["wheel"].update(members=[
            {"path": "/etc/passwd", "size": 1, "sha256": "0" * 64}]),
        "license-outside-root": lambda d: d["derived_wheels"][0]["license"]["declared_in"].update(
            sdist_member="other/PKG-INFO"),
        "license-text-outside-dir": lambda d: d["derived_wheels"][0]["license"]["text_file"].update(
            path="mac/other/http-ece-LICENSE"),
        "license-unknown-key": entry("license", extra=1),
        "unknown-top": lambda d: d.update(extra=1),
        "unknown-nested": entry("sdist", extra=1),
        "missing-key": lambda d: d["derived_wheels"][0].pop("reason"),
        "missing-nested": lambda d: d["derived_wheels"][0]["wheel"].pop("sha256"),
        "schema-2": lambda d: d.update(schema_version=2),
        "schema-bool": lambda d: d.update(schema_version=True),
    }
    return list(cases.items())


@pytest.mark.parametrize("label,mutate", _mutations(), ids=[m[0] for m in _mutations()])
def test_loader_is_strict_and_bounded(monkeypatch, label, mutate) -> None:
    data = copy.deepcopy(COMMITTED)
    mutate(data)
    inject(monkeypatch, data)
    with pytest.raises(mod.DerivedWheelError):
        mod.load_build_inputs()


@pytest.mark.parametrize("apply", DUPLICATES, ids=[f"dup{i}" for i in range(len(DUPLICATES))])
def test_loader_rejects_duplicate_keys(monkeypatch, apply) -> None:
    raw = apply(json.dumps(COMMITTED)).encode()
    monkeypatch.setattr(mod, "_read_inputs_bytes", lambda: raw)
    with pytest.raises(mod.DerivedWheelError):
        mod.load_build_inputs()


def test_loader_refuses_oversize_before_parsing(monkeypatch) -> None:
    calls = []
    real = json.loads
    monkeypatch.setattr(mod.json, "loads", lambda *a, **k: calls.append(1) or real(*a, **k))
    raw = json.dumps(COMMITTED).encode().ljust(mod.MAX_INPUT_BYTES + 1, b" ")
    monkeypatch.setattr(mod, "_read_inputs_bytes", lambda: raw)
    with pytest.raises(mod.DerivedWheelError):
        mod.load_build_inputs()
    assert calls == []
    monkeypatch.setattr(mod, "_read_inputs_bytes", lambda: b"\xff")
    with pytest.raises(mod.DerivedWheelError):
        mod.load_build_inputs()
    monkeypatch.setattr(mod, "_read_inputs_bytes", lambda: b'{"schema_version": NaN}')
    with pytest.raises(mod.DerivedWheelError):
        mod.load_build_inputs()


def test_loader_hash_is_of_the_bytes_read(monkeypatch, tmp_path) -> None:
    raw = json.dumps(COMMITTED, indent=4).encode()
    monkeypatch.setattr(mod, "_read_inputs_bytes", lambda: raw)
    inputs = mod.load_build_inputs()
    monkeypatch.setattr(mod, "INPUTS", tmp_path / "missing.json")
    assert inputs.sha256 == hashlib.sha256(raw).hexdigest()
    with pytest.raises(AttributeError):
        inputs.sha256 = "x"  # type: ignore[misc]


# --------------------------------------------------------- input checks


@pytest.mark.parametrize("target", ["sdist", "backend"])
@pytest.mark.parametrize("fault", ["digest", "size-plus", "size-minus"])
def test_input_hash_or_size_mismatch_never_parses(tmp_path, monkeypatch, target, fault) -> None:
    env = stub_env(tmp_path, monkeypatch)
    entry = env.pin["build_tools"][0] if target == "backend" else env.pin["derived_wheels"][0]["sdist"]
    if fault == "digest":
        entry["sha256"] = "1" * 64
    else:
        entry["size"] += 1 if fault == "size-plus" else -1
    inject(monkeypatch, env.pin)
    spies = Spies(monkeypatch)
    assert_refused(env)
    assert spies.calls == {"tar": 0, "zip": 0, "import": 0}


def _tar_member(name, kind=tarfile.REGTYPE, linkname="", data=b"x"):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info, data


def _valid_members():
    """A complete, valid sdist: PKG-INFO declaring the pinned licence, so the build would succeed."""
    return [
        _tar_member(ROOT, tarfile.DIRTYPE),
        _tar_member(f"{ROOT}/PKG-INFO", data=PKG_INFO),
        _tar_member(f"{ROOT}/setup.py", data=b"# stub\n"),
        _tar_member(f"{ROOT}/http_ece/__init__.py", data=MODULE_BYTES),
    ]


def _hostile_sdists():
    # Each case is the valid sdist plus hostile members at the end, so valid files are
    # extracted before the hostile one is reached unless its own guard refuses the archive.
    cases = {
        "absolute": [_tar_member("/etc/passwd")],
        "dotdot": [_tar_member(f"{ROOT}/../evil")],
        "dot-component": [_tar_member(f"{ROOT}/./evil")],
        "empty-component": [_tar_member(f"{ROOT}//evil")],
        "outside-root": [_tar_member("other-1.0/x")],
        "outside-root-sliceable": [_tar_member("http_ece-1.2.2/payload.py")],
        "symlink": [_tar_member(f"{ROOT}/l", tarfile.SYMTYPE, "/etc/passwd")],
        "hardlink": [_tar_member(f"{ROOT}/h", tarfile.LNKTYPE, f"{ROOT}/setup.py")],
        "fifo": [_tar_member(f"{ROOT}/f", tarfile.FIFOTYPE)],
        "device": [_tar_member(f"{ROOT}/d", tarfile.CHRTYPE)],
        # Distinct on every filesystem; equal only under str.casefold (not str.lower).
        "casefold": [_tar_member(f"{ROOT}/stra\u00dfe"), _tar_member(f"{ROOT}/STRASSE")],
        "too-many": [_tar_member(f"{ROOT}/f{i}") for i in range(501)],
        "too-big": [_tar_member(f"{ROOT}/big", data=b"\0" * (4 * 1024 * 1024 + 1))],
    }
    return [(label, _valid_members() + extra) for label, extra in cases.items()]


def test_valid_sdist_fixture_base_builds(tmp_path, monkeypatch) -> None:
    env = stub_env(tmp_path, monkeypatch, sdist=sdist_bytes(members=_valid_members()))
    assert env.build()["wheel"]["filename"] == "http_ece-1.2.1-py3-none-any.whl"


@pytest.mark.parametrize("label,members", _hostile_sdists(), ids=[c[0] for c in _hostile_sdists()])
def test_hostile_sdist_refused_before_backend_import(tmp_path, monkeypatch, label, members) -> None:
    env = stub_env(tmp_path, monkeypatch, sdist=sdist_bytes(members=members))
    spies = Spies(monkeypatch)
    writes, extractions = [], []
    real_write, real_extract = mod._write_file, mod._extract_sdist
    monkeypatch.setattr(mod, "_write_file", lambda *a, **k: writes.append(a[0]) or real_write(*a, **k))

    def extract(archive, root_name, epoch, target):
        try:
            return real_extract(archive, root_name, epoch, target)
        except mod.DerivedWheelError:
            extractions.append(os.path.lexists(target))
            raise

    monkeypatch.setattr(mod, "_extract_sdist", extract)
    assert_refused(env)
    assert extractions == [False], "the sdist guard must refuse before anything is extracted"
    assert writes == [], "nothing may be written by the extraction seam"
    assert spies.calls["import"] == 0


def _hostile_backends():
    def link():
        info = zipfile.ZipInfo("setuptools/link.py")
        info.external_attr = 0o120777 << 16
        return info

    good = [("setuptools/__init__.py", b""), ("setuptools/build_meta.py", make_stub().encode())]
    meta = ("setuptools-84.2.1.dist-info/METADATA", b"Name: setuptools\nVersion: 84.2.1\n")
    return {
        "dotdot": backend_bytes(entries=[*good, meta, ("../evil.py", b"x")]),
        "absolute": backend_bytes(entries=[*good, meta, ("/evil.py", b"x")]),
        "duplicate": backend_bytes(entries=[*good, meta, good[0]]),
        "symlink": backend_bytes(entries=[*good, meta, link()]),
        "ratio-bomb": backend_bytes(entries=[*good, meta, ("setuptools/zeros", b"\0" * (8 * 1024 * 1024))]),
        "wrong-name": backend_bytes(name="other"),
        "wrong-version": backend_bytes(version="1.0"),
        "no-metadata": backend_bytes(entries=good),
    }


@pytest.mark.parametrize("label", list(_hostile_backends()))
def test_hostile_backend_wheel_refused(tmp_path, monkeypatch, label) -> None:
    env = Env(tmp_path, backend=_hostile_backends()[label])
    inject(monkeypatch, env.pin)
    spies = Spies(monkeypatch)
    assert_refused(env)
    assert spies.calls["import"] == 0


def _pyproject(requires: str, backend: str | None = "setuptools.build_meta") -> bytes:
    text = f"[build-system]\nrequires = {requires}\n"
    if backend:
        text += f'build-backend = "{backend}"\n'
    return text.encode()


@pytest.mark.parametrize(
    "pyproject,pin_backend,accepted",
    [
        (None, "setuptools.build_meta:__legacy__", True),
        (_pyproject('["setuptools>=61", "wheel"]'), "setuptools.build_meta", True),
        (_pyproject('["setuptools>=61", "wheel"]', None), "setuptools.build_meta:__legacy__", True),
        (b"[tool.x]\na = 1\n", "setuptools.build_meta:__legacy__", True),
        (_pyproject('["setuptools>=90"]'), "setuptools.build_meta", False),
        (_pyproject('["setuptools", "setuptools_scm"]'), "setuptools.build_meta", False),
        (_pyproject('["hatchling"]', "hatchling.build"), "setuptools.build_meta", False),
        (_pyproject('["flit_core"]', "flit_core.buildapi"), "setuptools.build_meta", False),
        (_pyproject('["setuptools"]', "hatchling.build"), "setuptools.build_meta", False),
        (None, "setuptools.build_meta", False),
        (_pyproject('["setuptools"]'), "setuptools.build_meta:__legacy__", False),
    ],
    ids=["legacy", "setuptools61-wheel", "no-backend-key", "no-build-system", "setuptools90", "scm",
         "hatchling", "flit", "backend-mismatch", "pin-mismatch", "pin-legacy-mismatch"],
)
def test_build_declaration_matrix(tmp_path, monkeypatch, pyproject, pin_backend, accepted) -> None:
    files = {"http_ece/__init__.py": MODULE_BYTES, "PKG-INFO": PKG_INFO, "setup.py": b"# stub\n"}
    if pyproject is not None:
        files["pyproject.toml"] = pyproject
    env = stub_env(tmp_path, monkeypatch, sdist=sdist_bytes(files), backend_name=pin_backend)
    if accepted:
        assert env.build()["wheel"]["filename"] == "http_ece-1.2.1-py3-none-any.whl"
    else:
        assert_refused(env)


# --------------------------------------------------------------- guards


RUNNER = '''
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("m", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
raw = Path(sys.argv[2]).read_bytes()
m._read_inputs_bytes = lambda: raw
m.REPO_ROOT = Path(sys.argv[6])
try:
    m.build_derived_wheel("http-ece", Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]))
except m.DerivedWheelError:
    sys.exit(3)
'''


def _isolated(tmp_path: Path, prelude: str, plant: Path | None = None) -> tuple[int, Path]:
    env = Env(tmp_path, backend=backend_bytes(make_stub(prelude)))
    pin_file = tmp_path / "pin.json"
    pin_file.write_text(json.dumps(env.pin))
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER)
    result = subprocess.run(
        [sys.executable, "-I", str(runner), str(MODULE_PATH), str(pin_file), str(env.sdist),
         str(env.backend), str(env.destination), str(mod.REPO_ROOT)],
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode, env.parent


GUARD_PRELUDES = {
    "socket": "import socket\nsocket.create_connection(('127.0.0.1', 9), timeout=1)",
    "resolve": "import socket\nsocket.getaddrinfo('example.org', 80)",
    "subprocess": "import subprocess\nsubprocess.run(['/usr/bin/true'])",
    "os-system": "import os\nos.system('true')",
    "sendto": "import socket\nsocket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 9))",
    "sendmsg": "import socket\nsocket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b'x'], [], 0, ('127.0.0.1', 9))",
    "ctypes": "import ctypes\nctypes.create_string_buffer(4)",
    "foreign-import": "PLANT",
}


def test_guard_positive_control(tmp_path) -> None:
    code, parent = _isolated(tmp_path, "pass")
    assert code == 0
    assert (parent / "out" / "http_ece-1.2.1-py3-none-any.whl").is_file()


@pytest.mark.parametrize("label", list(GUARD_PRELUDES))
def test_guard_blocks_network_subprocess_and_foreign_imports(tmp_path, label) -> None:
    prelude = GUARD_PRELUDES[label]
    if label == "foreign-import":
        plant = private(tmp_path / "plant")
        (plant / "planted_mod.py").write_text("X = 1\n")
        prelude = f"import sys\nsys.path.append({str(plant)!r})\nimport planted_mod"
    code, parent = _isolated(tmp_path, prelude)
    assert code == 3
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("event", [
    "socket.connect", "socket.getaddrinfo", "socket.bind", "socket.gethostbyname", "socket.gethostbyaddr",
    "socket.getnameinfo", "socket.sendto", "socket.sendmsg", "urllib.Request", "subprocess.Popen",
    "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.dlsym/handle", "ctypes.cdata",
    "ctypes.call_function", "ctypes.create_string_buffer", "ctypes.string_at", "ctypes.PyObj_FromPtr",
])
def test_audit_hook_blocks_event_only_while_armed(event) -> None:
    # Disarm in a finally, not a fixture: the armed hook would block pytest's own putenv.
    saved = dict(mod._GUARD)
    try:
        mod._GUARD.update(on=False, violation=False)
        mod._audit(event, ())
        assert mod._GUARD["violation"] is False
        mod._GUARD["on"] = True
        with pytest.raises(RuntimeError):
            mod._audit(event, ())
        assert mod._GUARD["violation"] is True
        mod._GUARD["violation"] = False
        mod._audit("open", ())
        mod._audit("socket.__new__", ())
        assert mod._GUARD["violation"] is False
    finally:
        mod._GUARD.update(saved)


def test_build_ignores_real_users_pydistutils_cfg(tmp_path, monkeypatch) -> None:
    fake_home = private(tmp_path / "real-home")
    (fake_home / ".pydistutils.cfg").write_text("[build]\nexecutable = /bin/evil\n")
    import pwd
    real = pwd.getpwuid(os.getuid())
    fake = pwd.struct_passwd((real.pw_name, real.pw_passwd, real.pw_uid, real.pw_gid, real.pw_gecos,
                             str(fake_home), real.pw_shell))
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: fake)
    # Control: with HOME absent (what the scrubbed environment used to leave) the passwd entry
    # wins, so a probe in the backend would find the planted file.
    monkeypatch.delenv("HOME", raising=False)
    assert os.path.exists(os.path.join(os.path.expanduser("~"), ".pydistutils.cfg"))
    probe = (
        "import os\n"
        "if os.path.exists(os.path.join(os.path.expanduser('~'), '.pydistutils.cfg')):\n"
        "    open('LICENSE', 'wb').write(b'tainted by the real home')\n"
    )
    env = stub_env(tmp_path, monkeypatch, prelude=probe)
    assert env.build()["wheel"]["filename"] == "http_ece-1.2.1-py3-none-any.whl"
    assert env.destination.is_dir()


def test_backend_sees_private_home_tmp_and_xdg(tmp_path, monkeypatch) -> None:
    probe = (
        "import os, tempfile\n"
        "work = os.path.realpath(os.path.join(os.getcwd(), os.pardir))\n"
        "names = ('HOME', 'TMPDIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME')\n"
        "dirs = [os.environ.get(n, '') for n in names] + [tempfile.gettempdir(), os.path.expanduser('~')]\n"
        "inside = all(d and os.path.realpath(d).startswith(work + os.sep) and os.path.isdir(d) for d in dirs)\n"
        "if not inside or set(os.environ) != {'SOURCE_DATE_EPOCH', 'LC_ALL', 'TZ', *names}:\n"
        "    open('LICENSE', 'wb').write(b'tainted')\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    saved_tempdir = mod.tempfile.tempdir
    env = stub_env(tmp_path, monkeypatch, prelude=probe)
    assert env.build()["wheel"]["filename"] == "http_ece-1.2.1-py3-none-any.whl"
    assert os.environ["HOME"] == str(tmp_path) and mod.tempfile.tempdir == saved_tempdir


def test_wheel_tampered_before_publish_is_refused(tmp_path, monkeypatch) -> None:
    env = stub_env(tmp_path, monkeypatch)
    real_chmod, real_rename, renames = os.chmod, os.rename, []
    tampered = []

    def chmod(path, mode, *a, **k):
        if str(path).endswith(".whl") and f"{os.sep}out{os.sep}" in str(path) and mode == 0o644:
            with open(path, "ab") as handle:
                handle.write(b"x")
            tampered.append(path)
        return real_chmod(path, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "rename", lambda *a, **k: renames.append(a) or real_rename(*a, **k))
    assert_refused(env)
    assert len(tampered) == 1 and renames == []


def test_caller_environment_cannot_change_output(tmp_path, monkeypatch) -> None:
    outputs, premodes = [], []
    real_chmod = os.chmod

    def chmod(path, mode, *a, **k):
        if str(path).endswith(".whl") and f"{os.sep}out{os.sep}" in str(path):
            premodes.append(stat.S_IMODE(os.stat(path).st_mode))
        return real_chmod(path, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", chmod)
    for index, (epoch, tz, mask) in enumerate([("0", "Asia/Tokyo", 0o077), ("99", "America/Chicago", 0o002)]):
        base = private(tmp_path / f"case{index}")
        env = stub_env(base, monkeypatch)
        parent = private(base / f"deep{index}")
        monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)
        monkeypatch.setenv("TZ", tz)
        old = os.umask(mask)
        try:
            env.build(parent=parent)
        finally:
            os.umask(old)
        assert os.environ["SOURCE_DATE_EPOCH"] == epoch and os.environ["TZ"] == tz
        outputs.append((parent / "out" / "http_ece-1.2.1-py3-none-any.whl").read_bytes())
    assert outputs[0] == outputs[1]
    # The backend wrote its wheel under umask 022 whatever the caller's umask was (077, 002).
    assert premodes == [0o644, 0o644]
    with zipfile.ZipFile(io.BytesIO(outputs[0])) as zf:
        assert {i.date_time for i in zf.infolist()} == {mod._zip_time(EPOCH)}


# ------------------------------------------------------------ validation


OUTPUT_FAULTS = {
    "extra-so": {"extra": [["http_ece/x.so", "bin"]]},
    "pth": {"extra": [["http_ece.pth", "import x"]]},
    "entry-points": {"extra": [["http_ece-1.2.1.dist-info/entry_points.txt", "[console_scripts]"]]},
    "data-dir": {"extra": [["http_ece-1.2.1.data/scripts/x", "x"]]},
    "extra-py-not-in-sdist": {"extra": [["http_ece/other.py", "x = 1\n"]]},
    "symlink": {"symlink": True},
    "wrong-filename-tag": {"tag": "cp312"},
    "wrong-filename-name": {"name": "other_pkg"},
    "wrong-filename-version": {"version": "9.9.9"},
    "wrong-wheel-file-tag": {"wheel_tag": "cp312"},
    "extra-wheel-file-tag": {"tag": "py2.py3", "wheel_tag": "py3"},
    "wrong-metadata-version": {"meta_version": "9.9.9"},
    "wrong-metadata-name": {"meta_name": "evil"},
    "extra-requires": {"requires_dist": ["cryptography>=2.5", "requests"]},
    "bad-record": {"bad_record": True},
    "mutated-module": {"mutate": True},
    "timestamp": {"ts_off": True},
    "two-wheels": {"two": True},
}


@pytest.mark.parametrize("label", list(OUTPUT_FAULTS))
def test_output_validation_matrix(tmp_path, monkeypatch, label) -> None:
    env = stub_env(tmp_path, monkeypatch, **OUTPUT_FAULTS[label])
    checked = []
    real_check = mod._check_wheel
    monkeypatch.setattr(mod, "_check_wheel", lambda *a, **k: checked.append(1) or real_check(*a, **k))
    assert_refused(env)
    # The filename faults are refused before the wheel is opened; every other fault must get
    # as far as the wheel content check, so it is refused by its own guard.
    assert checked == ([] if label.startswith("wrong-filename-") or label == "two-wheels" else [1])


def test_two_wheels_reach_the_single_wheel_guard(tmp_path, monkeypatch) -> None:
    env = stub_env(tmp_path, monkeypatch, two=True)
    produced = []
    real_run = mod._run_backend

    def run(backend, backend_dir, sdist_root, out_dir, *a, **k):
        real_run(backend, backend_dir, sdist_root, out_dir, *a, **k)
        produced.append(sorted(p.name for p in out_dir.iterdir()))

    monkeypatch.setattr(mod, "_run_backend", run)
    assert_refused(env)
    assert produced == [["http_ece-1.2.1-py2-none-any.whl", "http_ece-1.2.1-py3-none-any.whl"]]
    # With one wheel (control) the same inputs build.
    one = stub_env(private(tmp_path / "one"), monkeypatch)
    assert one.build()["wheel"]["filename"] == "http_ece-1.2.1-py3-none-any.whl"


@pytest.mark.parametrize("tag", ["py3", "py2.py3"])
def test_output_validation_accepts_pure_python_tags(tmp_path, monkeypatch, tag) -> None:
    env = stub_env(tmp_path, monkeypatch, tag=tag)
    receipt = env.build()
    assert receipt["wheel"]["filename"] == f"http_ece-1.2.1-{tag}-none-any.whl"
    assert [m["path"] for m in receipt["wheel"]["members"]] == sorted(m["path"] for m in receipt["wheel"]["members"])
    assert receipt["source_date_epoch"] == EPOCH
    assert receipt["sdist_sha256"] == env.pin["derived_wheels"][0]["sdist"]["sha256"]
    assert stat.S_IMODE(os.stat(env.destination / receipt["wheel"]["filename"]).st_mode) == 0o644
    assert sorted(p.name for p in env.parent.iterdir()) == ["out"]


def test_built_wheel_carries_pinned_licence_text(tmp_path, monkeypatch) -> None:
    env = stub_env(tmp_path, monkeypatch)
    receipt = env.build()
    paths = {m["path"] for m in receipt["wheel"]["members"]}
    assert "http_ece-1.2.1.dist-info/licenses/LICENSE" in paths
    with zipfile.ZipFile(env.destination / receipt["wheel"]["filename"]) as zf:
        assert zf.read("http_ece-1.2.1.dist-info/licenses/LICENSE") == LICENSE_BYTES
        assert zf.read("http_ece/__init__.py") == MODULE_BYTES


def test_wrong_declared_licence_refused(tmp_path, monkeypatch) -> None:
    other = PKG_INFO.replace(b"License: MIT", b"License: GPL-3.0-only")
    sdist = sdist_bytes({"http_ece/__init__.py": MODULE_BYTES, "PKG-INFO": other, "setup.py": b"# stub\n"})
    env = Env(tmp_path, sdist=sdist, backend=backend_bytes(make_stub()), pkg_info=other)
    inject(monkeypatch, env.pin)
    assert_refused(env)


@pytest.mark.parametrize("pkg_info", [
    PKG_INFO.replace(b"License: MIT\n", b""),
    PKG_INFO + b"License: MIT\n",
], ids=["undeclared", "declared-twice"])
def test_missing_or_ambiguous_declared_licence_refused(tmp_path, monkeypatch, pkg_info) -> None:
    sdist = sdist_bytes({"http_ece/__init__.py": MODULE_BYTES, "PKG-INFO": pkg_info, "setup.py": b"# stub\n"})
    env = Env(tmp_path, sdist=sdist, backend=backend_bytes(make_stub()), pkg_info=pkg_info)
    inject(monkeypatch, env.pin)
    assert_refused(env)


def test_pkg_info_hash_mismatch_refused(tmp_path, monkeypatch) -> None:
    env = stub_env(tmp_path, monkeypatch)
    env.pin["derived_wheels"][0]["license"]["declared_in"]["sha256"] = "1" * 64
    inject(monkeypatch, env.pin)
    assert_refused(env)


@pytest.mark.parametrize("tamper", ["bytes", "size", "missing"])
def test_tampered_licence_text_refused(tmp_path, monkeypatch, committed_licence_dir, tamper) -> None:
    env = stub_env(tmp_path, monkeypatch)
    committed = committed_licence_dir / LICENCE_REL
    if tamper == "bytes":
        committed.write_bytes(b"MIT licence text!\n")
    elif tamper == "size":
        committed.write_bytes(LICENSE_BYTES + b"\n")
    else:
        committed.unlink()
    assert_refused(env)


def test_sdist_with_its_own_licence_file_refused(tmp_path, monkeypatch) -> None:
    sdist = sdist_bytes({"http_ece/__init__.py": MODULE_BYTES, "PKG-INFO": PKG_INFO,
                         "LICENSE": b"other\n", "setup.py": b"# stub\n"})
    assert_refused(stub_env(tmp_path, monkeypatch, sdist=sdist))


def test_wheel_without_licence_or_with_wrong_text_refused(tmp_path, monkeypatch) -> None:
    assert_refused(stub_env(private(tmp_path / "a"), monkeypatch, no_licence=True))
    assert_refused(stub_env(private(tmp_path / "b"), monkeypatch, prelude="open('LICENSE', 'wb').write(b'swapped')"))


def test_destination_must_be_absent_in_private_parent(tmp_path, monkeypatch) -> None:
    env = stub_env(tmp_path, monkeypatch)
    env.destination.mkdir()
    with pytest.raises(mod.DerivedWheelError):
        env.build()
    env.destination.rmdir()
    os.chmod(env.parent, 0o755)
    with pytest.raises(mod.DerivedWheelError):
        env.build()


def _verify_env(tmp_path, monkeypatch):
    env = stub_env(tmp_path, monkeypatch)
    receipt = env.build()
    pin_wheel = {
        "filename": receipt["wheel"]["filename"],
        "size": receipt["wheel"]["size"],
        "sha256": receipt["wheel"]["sha256"],
        "requires_dist": ["cryptography>=2.5"],
        "members": receipt["wheel"]["members"],
    }
    env.pin["derived_wheels"][0]["wheel"] = pin_wheel
    inject(monkeypatch, env.pin)
    return env, env.destination / pin_wheel["filename"], pin_wheel


def test_verify_derived_wheel(tmp_path, monkeypatch) -> None:
    env, wheel, pin_wheel = _verify_env(tmp_path, monkeypatch)
    mod.verify_derived_wheel("http-ece", wheel)

    data = wheel.read_bytes()
    flipped = tmp_path / wheel.name
    flipped.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(mod.DerivedWheelError):
        mod.verify_derived_wheel("http-ece", flipped)

    for delta in (1, -1):
        bad = copy.deepcopy(env.pin)
        bad["derived_wheels"][0]["wheel"]["size"] += delta
        inject(monkeypatch, bad)
        with pytest.raises(mod.DerivedWheelError):
            mod.verify_derived_wheel("http-ece", wheel)

    inject(monkeypatch, env.pin)
    renamed = tmp_path / "renamed.whl"
    renamed.write_bytes(data)
    with pytest.raises(mod.DerivedWheelError):
        mod.verify_derived_wheel("http-ece", renamed)

    bad = copy.deepcopy(env.pin)
    bad["derived_wheels"][0]["wheel"]["members"][0]["size"] += 1
    inject(monkeypatch, bad)
    with pytest.raises(mod.DerivedWheelError):
        mod.verify_derived_wheel("http-ece", wheel)

    inject(monkeypatch, env.pin)
    with pytest.raises(mod.DerivedWheelError):
        mod.verify_derived_wheel("unknown", wheel)


# ------------------------------------------------------------------ CLI


def _run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(MODULE_PATH), *args], capture_output=True,
                          text=True, cwd=cwd, timeout=60)


@pytest.mark.parametrize("flags", [
    ["build", "--name", "http-ece", "--sdist", "a", "--backend-wheel", "b", "--destination", "c", "--sha256", "0"],
    ["build", "--name", "http-ece", "--sdist", "a", "--backend-wheel", "b", "--destination", "c", "--epoch", "1"],
    ["build", "--name", "http-ece", "--sdist", "a", "--backend-wheel", "b", "--destination", "c", "--url", "x"],
    ["build", "--name", "http-ece", "--sd", "a", "--backend-wheel", "b", "--destination", "c"],
    ["verify", "--name", "http-ece", "--whe", "a"],
    ["verify"],
    [],
])
def test_cli_usage_errors_exit_2(tmp_path, flags) -> None:
    result = _run_cli(*flags, cwd=tmp_path)
    assert result.returncode == 2
    assert result.stderr == "derived wheel failed\n" and result.stdout == ""


def test_cli_failure_is_sanitized(tmp_path) -> None:
    parent = private(tmp_path / "secret-parent")
    marker = "marker-sdist.tar.gz"
    result = _run_cli("build", "--name", "http-ece", "--sdist", marker, "--backend-wheel", "missing-backend",
                      "--destination", str(parent / "out"), cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr == "derived wheel failed\n" and result.stdout == ""
    assert list(parent.iterdir()) == []
    verify = _run_cli("verify", "--name", "http-ece", "--wheel", str(tmp_path / "nope.whl"), cwd=tmp_path)
    assert verify.returncode == 1 and verify.stderr == "derived wheel failed\n"


@pytest.mark.parametrize("variant", ["dot", "dotdot", "control"])
def test_cli_refuses_dot_components(tmp_path, monkeypatch, variant) -> None:
    # Otherwise valid inputs: only the destination spelling differs, and the control builds.
    env = stub_env(tmp_path, monkeypatch)
    destination = {
        "dot": f"{env.parent}/./out",
        "dotdot": f"{env.parent}/../parent/out",
        "control": f"{env.parent}/out",
    }[variant]
    built = []
    real_build = mod.build_derived_wheel
    monkeypatch.setattr(mod, "build_derived_wheel", lambda *a, **k: built.append(1) or real_build(*a, **k))
    code = mod.main(["build", "--name", "http-ece", "--sdist", str(env.sdist),
                     "--backend-wheel", str(env.backend), "--destination", destination])
    if variant == "control":
        assert code == 0 and built == [1] and env.destination.is_dir()
    else:
        assert code == 1 and built == [] and list(env.parent.iterdir()) == []


def test_cli_win32_refusal_precedes_io(tmp_path, monkeypatch, capsys) -> None:
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(a) or real_open(*a, **k))
    reads = []
    monkeypatch.setattr(mod, "_read_inputs_bytes", lambda: reads.append(1) or b"")
    with monkeypatch.context() as ctx:
        ctx.setattr(sys, "platform", "win32")
        assert mod.main(["verify", "--name", "http-ece", "--wheel", "x"]) == 1
        assert mod.main(["build", "--name", "http-ece", "--sdist", "a", "--backend-wheel", "b",
                         "--destination", str(tmp_path / "out")]) == 1
    assert opened == [] and reads == []
    assert capsys.readouterr().err == "derived wheel failed\n" * 2


def test_cli_build_prints_receipt_in_process(tmp_path, monkeypatch, capsys) -> None:
    env = stub_env(tmp_path, monkeypatch)
    code = mod.main(["build", "--name", "http-ece", "--sdist", str(env.sdist),
                     "--backend-wheel", str(env.backend), "--destination", str(env.destination)])
    assert code == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["wheel"]["filename"] == "http_ece-1.2.1-py3-none-any.whl"
    assert str(tmp_path) not in json.dumps(receipt)
