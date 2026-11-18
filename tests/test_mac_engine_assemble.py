import base64
import getpass
import hashlib
import importlib.util
import json
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import packaging
import packaging.markers
import packaging.requirements
import packaging.specifiers
import packaging.utils
import packaging.version
import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
MODULE_PATH = REPO / "mac/scripts/engine_assemble.py"
SPEC = importlib.util.spec_from_file_location("engine_assemble", MODULE_PATH)
assert SPEC and SPEC.loader
ea = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ea)
REAL_PLATFORM_CHECK = ea._platform_check
ei = ea.engine_install

SITE = ea.SITE_REL
PIP_TREE = ea.PIP_TREES[0]
ARM64 = 0x0100000C


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _dist(site: Path, name: str, version: str, requires=(), scripts=(), prefix: Path | None = None) -> None:
    directory = site / f"{name.replace('-', '_')}-{version}.dist-info"
    directory.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    (directory / "METADATA").write_text("\n".join(lines) + "\n\nbody\n")
    rows = [f"{directory.name}/METADATA,,"]
    for script in scripts:
        assert prefix is not None
        bin_dir = prefix / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / script).write_text(f"#!{prefix}/bin/python3.12\n")
        rows.append(f"../../../bin/{script},sha256=abc,10")
    rows.append(f"{directory.name}/RECORD,,")
    (directory / "RECORD").write_text("\n".join(rows) + "\n")


def _macho(cputype=ARM64, filetype=2, minos=(13, 0), dylibs=("/usr/lib/libSystem.B.dylib",), rpaths=(),
           fat=None, version_cmd="build", ncmds=None, first_cmdsize=None, magic=b"\xcf\xfa\xed\xfe", platform=1,
           bad_cmd=None) -> bytes:
    if fat is not None:
        slices = [(ct, _macho(cputype=ct)) for ct in fat]
        header = b"\xca\xfe\xba\xbe" + struct.pack(">I", len(slices))
        offset = 8 + 20 * len(slices)
        offset += -offset % 16
        table, body = b"", b""
        for ct, blob in slices:
            table += struct.pack(">iiIII", ct, 0, offset + len(body), len(blob), 4)
            body += blob + b"\0" * (-len(blob) % 16)
        return header + table + b"\0" * (offset - 8 - len(table)) + body
    cmds: list[bytes] = []
    packed = (minos[0] << 16) | (minos[1] << 8)
    if version_cmd == "build":
        cmds.append(struct.pack("<IIIIII", 0x32, 24, platform, packed, packed, 0))
    elif version_cmd == "min":
        cmds.append(struct.pack("<IIII", 0x24, 16, packed, packed))
    for name in dylibs:
        raw = name.encode() + b"\0"
        size = 24 + len(raw)
        size += -size % 8
        cmds.append(struct.pack("<IIIIII", 0xC, size, 24, 0, 0, 0) + raw.ljust(size - 24, b"\0"))
    for path in rpaths:
        raw = path.encode() + b"\0"
        size = 12 + len(raw)
        size += -size % 8
        cmds.append(struct.pack("<III", 0x8000001C, size, 12) + raw.ljust(size - 12, b"\0"))
    if bad_cmd is not None:
        cmds.append(struct.pack("<II", 0x1B, bad_cmd) + b"\0" * 8)
    if first_cmdsize is not None and cmds:
        cmds[0] = struct.pack("<II", struct.unpack("<II", cmds[0][:8])[0], first_cmdsize) + cmds[0][8:]
    blob = b"".join(cmds)
    count = len(cmds) if ncmds is None else ncmds
    return struct.pack("<IiiIIIII", struct.unpack("<I", magic)[0], cputype, 0, filetype, count, len(blob), 0, 0) + blob


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    state = SimpleNamespace()
    # ---- export (C1 export destination)
    export = root / "export"
    export.mkdir()
    os.chmod(export, 0o700)
    source = export / "source"
    pyproject = ('[build-system]\nrequires = ["setuptools>=68", "wheel"]\nbuild-backend = "setuptools.build_meta"\n\n'
                 '[project]\nname = "ciaobot"\nversion = "1.0"\ndependencies = ["foo==1.0"]\n').encode()
    pins = {"pyproject.toml": pyproject, "uv.lock": b"lock", "mac/engine-runtime.json": b"{}",
            "mac/engine-build-inputs.json": b"{\"b\": 1}"}
    for rel, data in pins.items():
        _write(source / rel, data)
    _write(source / "LICENSE", b"ciaobot licence text\n")
    _write(source / "web/package-lock.json", b"{}")
    index = b"<html></html>"
    _write(source / "ciao/web/static/index.html", index)
    static = [{"path": "index.html", "size": len(index), "sha256": _sha(index)}]
    export_receipt = {"schema_version": 1, "commit": "a" * 40, "tree": "b" * 40, "source_date_epoch": 1_700_000_000,
                      "package_lock_sha256": _sha(b"{}"), "static_inventory": static}
    _write(export / "receipt.json", json.dumps(export_receipt).encode())
    # ---- build (C1 build destination) + project receipt
    build = root / "build"
    build.mkdir()
    os.chmod(build, 0o700)
    wheel_bytes = b"ciaobot wheel bytes"
    _write(build / "ciaobot-1.0-py3-none-any.whl", wheel_bytes)
    tool = {"name": "setuptools", "version": "84.2.1", "filename": "setuptools-84.2.1-py3-none-any.whl",
            "size": 1, "sha256": "c" * 64}
    project_receipt = {"wheel": {"filename": "ciaobot-1.0-py3-none-any.whl", "size": len(wheel_bytes),
                                 "sha256": _sha(wheel_bytes)},
                       "commit": "a" * 40, "tree": "b" * 40, "backend_sha256": tool["sha256"],
                       "source_date_epoch": 1_700_000_000}
    receipt_path = root / "project-receipt.json"
    receipt_path.write_text(json.dumps(project_receipt))
    # ---- B3b install destination
    install = root / "install"
    install.mkdir()
    os.chmod(install, 0o700)
    prefix = install / "prefix"
    site = prefix / SITE
    licence = b"cpython licence\n"
    _write(prefix / ea.PYTHON_REL, _macho(), 0o755)
    (prefix / "bin/python3").symlink_to("python3.12")
    _write(prefix / "bin/2to3", b"#!/bin/sh\n", 0o755)
    _write(prefix / "lib/python3.12/LICENSE.txt", licence)
    _write(prefix / "lib/python3.12/encodings/__pycache__/utf_8.cpython-312.pyc", b"\0pyc")
    _write(prefix / "lib/python3.12/ensurepip/__init__.py", b"# ensurepip\n")
    _write(prefix / "lib/python3.12/ensurepip/_bundled/pip-26.3-py3-none-any.whl", b"pipwheel")
    _write(prefix / "lib/python3.12/pipes.py", b"# shell pipes\n")
    _write(site / "pip/__init__.py", b"# pip\n")
    _dist(site, "pip", "26.3", scripts=("pip", "pip3", "pip3.12"), prefix=prefix)
    _dist(site, "foo", "1.0", requires=["http-ece", "certifi"])
    _write(site / "foo/__init__.py", b"# foo\n")
    _write(site / "claude_agent_sdk/_bundled/claude", _macho(), 0o755)
    http_licence = b"http-ece MIT\n"
    _dist(site, "http-ece", "1.2.1")
    _write(site / "http_ece-1.2.1.dist-info/licenses/LICENSE", http_licence)
    cacert = b"-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n"
    _dist(site, "certifi", "2026.1.1")
    _write(site / "certifi/cacert.pem", cacert)
    (site / "certifi-2026.1.1.dist-info/RECORD").write_text(
        f"certifi-2026.1.1.dist-info/METADATA,,\ncertifi/cacert.pem,sha256={_b64(cacert)},{len(cacert)}\n"
        "certifi-2026.1.1.dist-info/RECORD,,\n")
    rows = [{"name": name, "version": version, "origin": origin, "filename": f"{name}-{version}.whl",
             "size": 10, "sha256": "d" * 64}
            for name, version, origin in (("certifi", "2026.1.1", "lock"), ("foo", "1.0", "lock"),
                                          ("http-ece", "1.2.1", "derived"))]
    # ---- pins
    hashes = {"pyproject_sha256": _sha(pyproject), "lock_sha256": _sha(b"lock"),
              "runtime_pin_sha256": _sha(b"{}"), "build_inputs_sha256": _sha(b"{\"b\": 1}")}
    pin = {"python_version": "3.12.16", "target": "aarch64-apple-darwin", "minimum_macos": "13.0",
           "upstream": "astral-sh/python-build-standalone", "release": "1", "flavor": "install_only",
           "filename": "runtime.tar.gz", "size": 5, "sha256": "e" * 64,
           "license": {"size": len(licence), "sha256": _sha(licence)}}
    build_inputs = SimpleNamespace(
        sha256=hashes["build_inputs_sha256"],
        data={"build_tools": [tool],
              "derived_wheels": [{"name": "http-ece", "version": "1.2.1",
                                  "license": {"text_file": {"size": len(http_licence),
                                                            "sha256": _sha(http_licence)}}}]})
    inputs = SimpleNamespace(hashes=hashes, pin=pin, build_inputs=build_inputs)
    monkeypatch.setattr(ea, "_platform_check", lambda: None)
    monkeypatch.setattr(ea, "_inputs", lambda: inputs)
    monkeypatch.setattr(ei, "_load_packaging", lambda house: packaging)

    house = root / "house"
    house.mkdir()
    os.chmod(house, 0o700)
    out = root / "out"
    out.mkdir()
    os.chmod(out, 0o700)

    state.root, state.export, state.build, state.install, state.prefix = root, export, build, install, prefix
    state.source, state.site, state.house, state.out = source, site, house, out
    state.hashes, state.pin, state.inputs, state.tool, state.rows = hashes, pin, inputs, tool, rows
    state.receipt_path, state.wheel_bytes, state.static = receipt_path, wheel_bytes, static
    state.calls, state.profiles, state.checks, state.pip_requirements = [], [], [], []
    state.status = {}
    state.smoke_output = b"OpenSSL 3.0.99 1 Jan 2099\n[\"-I\", \"-B\"]\n"
    state.ciaobot_requires = ["foo", 'pytest; extra == "test"']
    state.scripts = ["ciao", "ciaobot", "ciao-public-preflight", "ciao-package-smoke", "ciao-prepare-release"]
    state.extra_script = False
    state.extra_dist = False
    state.ciaobot_licence = b"ciaobot licence text\n"
    state.pip_hook = state.version_hook = state.probe_hook = None
    state.requires_override = None

    def refresh_install():
        receipt = {"schema": 1, "inputs": hashes, "selected": rows, "derived": [{"name": "http-ece"}],
                   "removed_scripts": ["foo-cli"], "pip_version": "26.3", "openssl_version": "OpenSSL 3.0.99 1 Jan 2099",
                   "inventory": ei.inventory(prefix)}
        (install / "install-receipt.json").write_text(json.dumps(receipt))
        (install / "requirements.txt").write_text(ei.requirements_text(rows))

    state.refresh_install = refresh_install
    refresh_install()

    def fake_run(argv, env_, cwd, timeout):
        state.calls.append((argv, env_, cwd, timeout))
        assert argv[:2] == [ei.SANDBOX_EXEC, "-p"]
        state.profiles.append(argv[2])
        inner = argv[3:]
        engine = Path(inner[0]).parent.parent
        pip_tree = engine / PIP_TREE
        if inner[0].endswith("/bin/ciao"):
            assert not pip_tree.exists()
            if state.version_hook:
                state.version_hook(engine)
            return state.status.get("version", 0), b"ciao 1.0\n"
        if inner[3] == "-c":
            if "default_child_argv" in inner[4]:
                state.smoke_code = inner[4]
                return state.status.get("smoke", 0), state.smoke_output
            assert not pip_tree.exists() and not (engine / ea.STDLIB_REL / "ensurepip").exists()
            if state.probe_hook:
                state.probe_hook(engine)
            return state.status.get("probe", 0), b""
        if inner[4] == "ciao.release_manifest":
            return state.status.get("static", 0), b""
        if inner[5] == "check":
            assert pip_tree.exists()
            return state.status.get("check", 0), b""
        assert inner[5] == "install" and pip_tree.exists()
        state.pip_inner, state.pip_env = inner, env_
        state.pip_requirements.append(Path(inner[-1]).read_text())
        site_ = engine / SITE
        requires = state.requires_override or state.ciaobot_requires
        _dist(site_, "ciaobot", "1.0", requires, state.scripts + (["ciao-extra"] if state.extra_script else []),
              engine)
        info = site_ / "ciaobot-1.0.dist-info"
        (info / "entry_points.txt").write_text(
            "[console_scripts]\n" + "".join(f"{name} = ciao.cli:main\n" for name in state.scripts))
        _write(info / "licenses/LICENSE", state.ciaobot_licence)
        _write(site_ / "ciao/__init__.py", b"# ciao\n")
        if state.extra_dist:
            _dist(site_, "planted", "1.0")
        if state.pip_hook:
            state.pip_hook(engine)
        return state.status.get("pip", 0), b""

    monkeypatch.setattr(ea, "_run", fake_run)

    def check_wheel(*args):
        state.checks.append(args)

    monkeypatch.setattr(ea.engine_project_wheel, "_check_wheel", check_wheel)

    def assemble(parent=None, name="dest"):
        parent = parent or out
        return ea.assemble(house, install, export, build, receipt_path, parent / name)

    def refused(parent=None, name="dest", calls=None):
        parent = parent or out
        with pytest.raises(ea.AssembleError):
            assemble(parent, name)
        assert not (parent / name).exists()
        assert [p.name for p in parent.iterdir()] == []
        if calls is not None:
            assert len(state.calls) == calls

    state.assemble, state.refused = assemble, refused
    return state


def _engine_of(env, name="dest", parent=None):
    return (parent or env.out) / name / "engine"


def _provenance(env, name="dest", parent=None):
    return json.loads((_engine_of(env, name, parent) / "provenance.json").read_text())


# --------------------------------------------------------------- pipeline


def test_assemble_publishes_engine_and_receipt_only(env):
    env.assemble()
    assert sorted(p.name for p in (env.out / "dest").iterdir()) == ["assembly-receipt.json", "engine"]
    assert [p.name for p in env.out.iterdir()] == ["dest"]


def test_pip_argv_env_and_sandbox_are_exact(env):
    env.assemble()
    engine_like = Path(env.pip_inner[0]).parent.parent
    # the engine was a temp name inside out/; recompute the argv from the recorded paths.
    links = Path(env.pip_inner[env.pip_inner.index("--find-links") + 1])
    requirements = Path(env.pip_inner[-1])
    assert env.pip_inner == ea.project_pip_argv(Path(env.pip_inner[0]), links, requirements)
    for flag in ("--no-deps", "--require-hashes", "--no-index", "--isolated", "--no-compile"):
        assert flag in env.pip_inner
    assert env.pip_inner[env.pip_inner.index("--only-binary") + 1] == ":all:"
    home = requirements.parent / "home"
    pip_call = next(call for call in env.calls if "install" in call[0])
    assert pip_call[0][2] == ei.sandbox_profile([engine_like, home])
    assert env.pip_env == ei._child_env(home)
    assert env.pip_requirements == [f"ciaobot==1.0 --hash=sha256:{_sha(env.wheel_bytes)}\n"]


def test_receipt_hash_that_differs_from_the_wheel_is_refused_before_pip(env):
    data = json.loads(env.receipt_path.read_text())
    data["wheel"]["sha256"] = "0" * 64
    env.receipt_path.write_text(json.dumps(data))
    env.refused(calls=0)


@pytest.mark.parametrize("rel", ["pyproject.toml", "uv.lock", "mac/engine-runtime.json",
                                 "mac/engine-build-inputs.json"])
def test_export_pin_files_must_equal_the_inputs(env, rel):
    with (env.source / rel).open("ab") as handle:
        handle.write(b" ")
    env.refused(calls=0)


def test_install_receipt_must_match_current_pins(env):
    path = env.install / "install-receipt.json"
    data = json.loads(path.read_text())
    data["inputs"]["lock_sha256"] = "0" * 64
    path.write_text(json.dumps(data))
    env.refused(calls=0)


def test_install_receipt_keys_are_exact(env):
    path = env.install / "install-receipt.json"
    data = json.loads(path.read_text())
    data["extra"] = 1
    path.write_text(json.dumps(data))
    env.refused(calls=0)


def test_prefix_changed_after_b3b_is_refused(env):
    with (env.prefix / "lib/python3.12/pipes.py").open("ab") as handle:
        handle.write(b"# tampered\n")
    env.refused(calls=0)


@pytest.mark.parametrize("field", ["commit", "tree", "source_date_epoch", "backend_sha256"])
def test_project_receipt_must_match_the_export(env, field):
    data = json.loads(env.receipt_path.read_text())
    data[field] = data[field] + 1 if field == "source_date_epoch" else "9" * len(data[field])
    env.receipt_path.write_text(json.dumps(data))
    env.refused(calls=0)


def test_wheel_is_revalidated_against_the_export(env):
    env.assemble()
    assert env.checks == [(env.wheel_bytes, "1.0", ["foo==1.0"], env.static, 1_700_000_000)]


def test_wheel_validation_failure_is_refused(env, monkeypatch):
    def boom(*_args):
        raise ea.engine_project_wheel.ProjectWheelError()

    monkeypatch.setattr(ea.engine_project_wheel, "_check_wheel", boom)
    env.refused(calls=0)


def test_project_build_must_hold_exactly_the_wheel(env):
    (env.build / "extra.txt").write_text("x")
    env.refused(calls=0)


@pytest.mark.parametrize("case", ["missing-dep", "unreachable-dist", "requires-pip"])
def test_closure_is_rooted_at_ciaobot(env, case):
    if case == "missing-dep":
        env.requires_override = ["foo", "ghost"]
    elif case == "unreachable-dist":
        env.extra_dist = True
    else:
        meta = env.site / "foo-1.0.dist-info/METADATA"
        meta.write_text(meta.read_text().replace("Version: 1.0\n", "Version: 1.0\nRequires-Dist: pip\n"))
        env.refresh_install()
    env.refused()


@pytest.mark.parametrize("step", ["pip", "check", "smoke", "static", "version", "probe"])
def test_a_failing_child_publishes_nothing(env, step):
    env.status[step] = 1
    env.refused()


@pytest.mark.parametrize("output", [b"OpenSSL 9.9\n[\"-I\", \"-B\"]\n", b"OpenSSL 3.0.99 1 Jan 2099\n[]\n",
                                    b"OpenSSL 3.0.99 1 Jan 2099\n[\"-I\", \"-B\"]\nextra\n"])
def test_smoke_output_shape_is_enforced(env, output):
    env.smoke_output = output
    env.refused()


def test_ciaobot_scripts_are_scrubbed_and_launcher_installed(env):
    env.assemble()
    engine = _engine_of(env)
    template = ea.LAUNCHER_TEMPLATE.read_bytes()
    assert (engine / "bin/ciao").read_bytes() == template
    assert stat.S_IMODE((engine / "bin/ciao").stat().st_mode) == 0o755
    for name in env.scripts[1:]:
        assert not (engine / "bin" / name).exists()
    assert "../../../bin/" not in (engine / SITE / "ciaobot-1.0.dist-info/RECORD").read_text()
    assert _provenance(env)["generated_scripts_removed"]["ciaobot"] == sorted(env.scripts)


def test_scripts_removed_must_equal_entry_points(env):
    env.extra_script = True
    env.refused()


def test_pip_is_removed_last_and_recorded(env):
    env.assemble()
    engine = _engine_of(env)
    for tree in ea.PIP_TREES:
        assert not (engine / tree).exists()
    for script in ea.PIP_SCRIPTS:
        assert not (engine / script).exists()
    assert (engine / "lib/python3.12/pipes.py").exists()
    removed = [row for row in _provenance(env)["runtime_modifications"]["removed"] if row["reason"] == "pip"]
    paths = [row["path"] for row in removed]
    assert paths == sorted(paths)
    expected = sorted([
        "bin/pip", "bin/pip3", "bin/pip3.12",
        f"{SITE}/pip/__init__.py",
        f"{SITE}/pip-26.3.dist-info/METADATA", f"{SITE}/pip-26.3.dist-info/RECORD",
        "lib/python3.12/ensurepip/__init__.py",
        "lib/python3.12/ensurepip/_bundled/pip-26.3-py3-none-any.whl",
    ])
    assert paths == expected
    dirs = _provenance(env)["runtime_modifications"]["removed_directories"]
    assert {row["path"] for row in dirs if row["reason"] == "pip"} >= set(ea.PIP_TREES)
    wheel = next(row for row in removed if row["path"].endswith(".whl"))
    assert (wheel["size"], wheel["sha256"]) == (len(b"pipwheel"), _sha(b"pipwheel"))


def test_pip_scripts_must_be_named_by_pips_record(env):
    record = env.site / "pip-26.3.dist-info/RECORD"
    record.write_text("".join(line for line in record.read_text().splitlines(keepends=True)
                              if "bin/pip3," not in line))
    env.refresh_install()
    env.refused()


@pytest.mark.parametrize("case", ["whl", "bin-pip3.13", "pip-dist-info", "ensurepip"])
def test_leftover_installer_is_refused(env, case):
    def plant(engine):
        if case == "whl":
            _write(engine / SITE / "x/y.whl", b"w")
        elif case == "bin-pip3.13":
            _write(engine / "bin/pip3.13", b"#!/bin/sh\n", 0o755)
        elif case == "pip-dist-info":
            _dist(engine / SITE, "pip", "9")

    if case == "ensurepip":
        env.probe_hook = lambda engine: _write(engine / "lib/python3.12/ensurepip/__init__.py", b"")
    else:
        env.pip_hook = plant
    env.refused()


@pytest.mark.parametrize("case", ["whl", "bin-pip", "bin-pip3.13", "site-pip", "pip-dist-info", "ensurepip"])
def test_assert_no_installer_unit(tmp_path, case):
    engine = tmp_path / "engine"
    (engine / "bin").mkdir(parents=True)
    (engine / SITE).mkdir(parents=True)
    ea.assert_no_installer(engine)
    plant = {
        "whl": f"{SITE}/x/y.whl", "bin-pip": "bin/pip", "bin-pip3.13": "bin/pip3.13",
        "site-pip": f"{SITE}/pip/__init__.py", "pip-dist-info": f"{SITE}/pip-9.dist-info/METADATA",
        "ensurepip": "lib/python3.12/ensurepip/__init__.py",
    }[case]
    _write(engine / plant, b"x")
    with pytest.raises(ea.AssembleError):
        ea.assert_no_installer(engine)


def test_assert_no_installer_ignores_lookalikes(tmp_path):
    engine = tmp_path / "engine"
    _write(engine / "lib/python3.12/pipes.py", b"x")
    _write(engine / "bin/pipx", b"x")
    _write(engine / SITE / "pip_extra/__init__.py", b"x")
    ea.assert_no_installer(engine)


def test_runtime_bytecode_is_removed_and_recorded(env):
    env.assemble()
    engine = _engine_of(env)
    assert not [p for p in engine.rglob("*") if p.suffix == ".pyc" or p.name == "__pycache__"]
    modifications = _provenance(env)["runtime_modifications"]
    pyc = [row for row in modifications["removed"] if row["reason"] == "bytecode"]
    assert pyc == [{"path": "lib/python3.12/encodings/__pycache__/utf_8.cpython-312.pyc", "reason": "bytecode",
                    "size": 4, "sha256": _sha(b"\0pyc")}]
    assert {"path": "lib/python3.12/encodings/__pycache__", "reason": "bytecode"} in modifications[
        "removed_directories"]


def test_bytecode_in_site_packages_baseline_is_refused(env):
    _write(env.site / "foo/__pycache__/x.cpython-312.pyc", b"x")
    env.refresh_install()
    env.refused()


@pytest.mark.parametrize("variant", ["file", "empty-dir"])
def test_bytecode_written_while_running_is_refused(env, variant):
    def hook(engine):
        cache = engine / "lib/python3.12/__pycache__"
        cache.mkdir()
        if variant == "file":
            (cache / "a.pyc").write_bytes(b"x")

    env.version_hook = hook
    env.refused()


def test_inventory_must_not_change_while_running(env):
    def hook(engine):
        with (engine / SITE / "ciao/__init__.py").open("ab") as handle:
            handle.write(b"x")

    env.probe_hook = hook
    env.refused()


@pytest.mark.parametrize("needle", ["work", "install"])
def test_build_path_anywhere_is_refused(env, needle):
    def hook(engine):
        value = engine.parent if needle == "work" else env.install
        with (engine / SITE / "ciao/__init__.py").open("ab") as handle:
            handle.write(os.fsencode(os.fspath(value)))

    env.pip_hook = hook
    env.refused()


def test_provenance_is_deterministic_and_path_free(env, tmp_path):
    other = env.root / "out2"
    other.mkdir()
    os.chmod(other, 0o700)
    first = env.assemble()
    second = env.assemble(parent=other)
    one, two = (_engine_of(env) / "provenance.json"), (_engine_of(env, parent=other) / "provenance.json")
    assert one.read_bytes() == two.read_bytes()
    assert first["inventory"] == second["inventory"]
    text = one.read_text()
    assert str(env.root) not in text and getpass.getuser() not in text
    data = json.loads(text)
    assert set(data) == {"schema", "kind", "engine", "source", "inputs", "runtime", "project_wheel", "distributions",
                         "derived", "pwa", "licenses", "tls", "bytecode", "launcher", "runtime_modifications",
                         "generated_scripts_removed", "build_tools"}
    assert [d["name"] for d in data["distributions"]] == ["certifi", "ciaobot", "foo", "http-ece"]
    licences = {entry["component"]: entry for entry in data["licenses"]}
    assert [entry["component"] for entry in data["licenses"]] == sorted(licences)
    for entry in licences.values():
        raw = (_engine_of(env) / entry["path"]).read_bytes()
        assert (entry["size"], entry["sha256"]) == (len(raw), _sha(raw))
    cafile = (_engine_of(env) / data["tls"]["cafile"]).read_bytes()
    assert (data["tls"]["size"], data["tls"]["sha256"], data["tls"]["policy"]) == (len(cafile), _sha(cafile),
                                                                                    "certifi-only")
    assert data["tls"]["certifi_version"] == "2026.1.1"
    assert data["bytecode"] == {"policy": "none"}
    assert data["build_tools"] == {"pip": "26.3", "setuptools": "84.2.1"}
    receipt = json.loads((env.out / "dest/assembly-receipt.json").read_text())
    assert receipt["provenance_sha256"] == _sha(one.read_bytes())
    assert {"schema", "kind", "provenance_sha256", "launcher", "macho", "totals", "largest", "inventory",
            "host_python"} == set(receipt)
    assert receipt["totals"]["macho_files"] == len(receipt["macho"]) == 2


@pytest.mark.parametrize("which", ["runtime", "http-ece", "ciaobot", "cafile"])
def test_licence_and_cafile_must_match_their_pins(env, which):
    if which == "ciaobot":
        env.ciaobot_licence = b"other licence\n"
    else:
        target = {"runtime": env.prefix / "lib/python3.12/LICENSE.txt",
                  "http-ece": env.site / "http_ece-1.2.1.dist-info/licenses/LICENSE",
                  "cafile": env.site / "certifi/cacert.pem"}[which]
        target.write_bytes(target.read_bytes() + b"x")
        env.refresh_install()
    env.refused()


def test_runtime_minor_must_match_the_launcher(env):
    env.pin["python_version"] = "3.13.1"
    env.refused(calls=0)
    assert not (env.out / "dest").exists()


# -------------------------------------------------------------------- Mach-O


def _engine(tmp_path, extra=None):
    engine = tmp_path / "engine"
    _write(engine / ea.PYTHON_REL, _macho(), 0o755)
    _write(engine / ea.REQUIRED_MACHO[1], _macho(), 0o755)
    for rel, data in (extra or {}).items():
        _write(engine / rel, data)
    return engine


def test_macho_accepts_system_and_loader_paths(tmp_path):
    cases = {
        "lib/a.dylib": _macho(filetype=6),
        "lib/b.dylib": _macho(filetype=6, minos=(12, 0), version_cmd="min"),
        "lib/c.so": _macho(filetype=8, dylibs=["@loader_path/../lib/x.dylib"]),
        "lib/d.dylib": _macho(filetype=6, dylibs=["@rpath/x.dylib"], rpaths=["@loader_path/../lib"]),
        "lib/x.dylib": _macho(filetype=6),
        "lib/e.dylib": _macho(filetype=6, dylibs=["/System/Library/Frameworks/CoreFoundation.framework/"
                                                  "CoreFoundation"]),
        "lib/f.dylib": _macho(fat=[0x01000007, ARM64]),
        "lib/Foo.class": b"\xca\xfe\xba\xbe" + struct.pack(">I", 0x34) + b"rest",
        "lib/plain.txt": b"hello",
        "lib/tiny": b"x",
    }
    audited = ea.macho_audit(_engine(tmp_path, cases))
    paths = [row["path"] for row in audited]
    assert paths == sorted(paths)
    assert "lib/Foo.class" not in paths and "lib/plain.txt" not in paths and "lib/f.dylib" in paths
    by_path = {row["path"]: row for row in audited}
    assert by_path["lib/b.dylib"]["minos"] == "12.0"
    assert by_path["lib/a.dylib"]["minos"] == "13.0" and by_path["lib/a.dylib"]["arch"] == "arm64"
    assert by_path["lib/d.dylib"]["rpaths"] == ["@loader_path/../lib"]
    assert by_path["lib/c.so"]["filetype"] == "bundle"


def test_macho_executable_path_and_rpath_variants(tmp_path):
    cases = {
        "bin/helper.dylib": _macho(filetype=6),
        "lib/g.dylib": _macho(filetype=6, dylibs=["@executable_path/helper.dylib"]),
        "lib/h.dylib": _macho(filetype=6, dylibs=["@rpath/anything.dylib"], rpaths=["/usr/lib"]),
    }
    ea.macho_audit(_engine(tmp_path, cases))


@pytest.mark.parametrize("name,blob", [
    ("x86_64", _macho(cputype=0x01000007)),
    ("minos-14", _macho(minos=(14, 0))),
    ("no-version", _macho(version_cmd="none")),
    ("homebrew", _macho(dylibs=["/opt/homebrew/lib/libssl.dylib"])),
    ("rpath-unresolved", _macho(dylibs=["@rpath/missing.dylib"], rpaths=["@loader_path/../lib"])),
    ("rpath-absent", _macho(dylibs=["@rpath/missing.dylib"])),
    ("escape", _macho(dylibs=["@loader_path/../../outside.dylib"])),
    ("usr-local-rpath", _macho(dylibs=["@rpath/x.dylib"], rpaths=["/usr/local/lib"])),
    ("32-bit", _macho(magic=b"\xfe\xed\xfa\xce")),
    ("ncmds-too-big", _macho(ncmds=100)),
    ("cmdsize-4", _macho(bad_cmd=4)),
    ("cmdsize-unaligned", _macho(bad_cmd=12)),
    ("cmdsize-past-end", _macho(bad_cmd=4096)),
    ("platform-ios", _macho(platform=2)),
    ("unused-bad-rpath", _macho(rpaths=["/usr/local/lib"])),
    ("absolute-with-system-rpath", _macho(dylibs=["/opt/homebrew/lib/libssl.dylib"], rpaths=["/usr/lib/"])),
    ("fat-no-arm64", _macho(fat=[0x01000007, 7])),
    ("object", _macho(filetype=1)),
    ("system-escape", _macho(dylibs=["/usr/lib/../../opt/x.dylib"])),
    ("absolute-missing", _macho(dylibs=["/Library/x.dylib"])),
])
def test_macho_refuses(tmp_path, name, blob):
    _write(tmp_path / "outside.dylib", _macho(filetype=6))
    engine = _engine(tmp_path, {"lib/probe.dylib": blob, "lib/x.dylib": _macho(filetype=6)})
    with pytest.raises(ea.AssembleError):
        ea.macho_audit(engine)


def test_required_macho_files_must_be_audited(tmp_path):
    engine = _engine(tmp_path)
    (engine / ea.PYTHON_REL).write_bytes(b"#!/bin/sh\n")
    with pytest.raises(ea.AssembleError):
        ea.macho_audit(engine)


# ------------------------------------------------------------------ launcher


def _unset_names():
    lines = ea.LAUNCHER_TEMPLATE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("unset "))
    names, i = [], start
    while True:
        names += lines[i].replace("unset", "").replace("\\", "").split()
        if not lines[i].rstrip().endswith("\\"):
            return names
        i += 1


@pytest.fixture
def launcher(tmp_path):
    root = tmp_path.resolve() / "Ciaobot É x" / "engine"
    _write(root / "bin/ciao", ea.LAUNCHER_TEMPLATE.read_bytes(), 0o755)
    _write(root / "bin/python3.12",
           b'#!/bin/sh\nout="${0%/bin/python3.12}/../out.txt"\n'
           b'{ printf "%s\\n" "$0"; for a in "$@"; do printf "A:%s\\n" "$a"; done; echo ---; /usr/bin/env; } > "$out"\n',
           0o755)
    _write(root / ea.CAFILE_REL, b"pem")
    return root


def _captured(root):
    text = (root.parent / "out.txt").read_text()
    head, _, tail = text.partition("---\n")
    lines = head.splitlines()
    env = dict(line.split("=", 1) for line in tail.splitlines() if "=" in line)
    return lines[0], [line[2:] for line in lines[1:]], env


def test_launcher_execs_isolated_python_with_exact_argv(launcher):
    done = subprocess.run(["/usr/bin/env", "-i", str(launcher / "bin/ciao"), "run", "a b"])
    assert done.returncode == 0
    python, args, env = _captured(launcher)
    assert env["SSL_CERT_FILE"] == f"{os.path.realpath(launcher)}/{ea.CAFILE_REL}"
    assert python == str(Path(os.path.realpath(launcher)) / "bin/python3.12")
    assert args == ["-I", "-B", "-m", "ciao", "run", "a b"]


UNSET = ("PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE PYTHONEXECUTABLE PYTHONPLATLIBDIR "
         "PYTHONBREAKPOINT PYTHONWARNINGS PYTHONPYCACHEPREFIX DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH "
         "DYLD_FRAMEWORK_PATH DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH DYLD_VERSIONED_LIBRARY_PATH "
         "DYLD_VERSIONED_FRAMEWORK_PATH DYLD_IMAGE_SUFFIX DYLD_ROOT_PATH DYLD_SHARED_REGION DYLD_SHARED_CACHE_DIR "
         "DYLD_FORCE_FLAT_NAMESPACE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE").split()


def test_launcher_sanitises_the_environment(launcher):
    names = UNSET
    assert set(_unset_names()) == set(names)
    given = [f"{name}=/x" for name in names] + ["SSL_CERT_FILE=/evil", "KEEP=1"]
    # DYLD_* is also removed by SIP for /bin/sh on macOS; the unset list is what protects non-SIP hosts
    # (the Linux CI run), so that part of this test is mutation-effective there.
    subprocess.run(["/usr/bin/env", "-i", *given, str(launcher / "bin/ciao")], check=True)
    _python, _args, env = _captured(launcher)
    assert not [name for name in names if name in env]
    root = os.path.realpath(launcher)
    assert env["SSL_CERT_FILE"] == f"{root}/{ea.CAFILE_REL}"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["KEEP"] == "1"


def test_launcher_works_by_relative_path_from_another_cwd(launcher):
    subprocess.run(["/usr/bin/env", "-i", "./engine/bin/ciao", "x"], cwd=launcher.parent, check=True)
    assert _captured(launcher)[1][-1] == "x"


def test_launcher_needs_no_path(launcher):
    subprocess.run([str(launcher / "bin/ciao")], env={}, check=True)
    text = ea.LAUNCHER_TEMPLATE.read_text()
    assert not any(word in text for word in ("dirname", "realpath", "readlink"))


def test_launcher_refuses_a_symlink(launcher, tmp_path):
    link = launcher / "bin/ciao-link"  # beside the real launcher, so only the symlink check can stop it
    link.symlink_to("ciao")
    done = subprocess.run(["/usr/bin/env", "-i", str(link), "--version"], capture_output=True)
    assert done.returncode == 70
    assert not (launcher.parent / "out.txt").exists()


@pytest.mark.parametrize("missing", ["python", "cafile"])
def test_launcher_refuses_an_incomplete_engine(launcher, missing):
    (launcher / ("bin/python3.12" if missing == "python" else ea.CAFILE_REL)).unlink()
    done = subprocess.run(["/usr/bin/env", "-i", str(launcher / "bin/ciao")], capture_output=True)
    assert done.returncode == 70


def test_launcher_hardcodes_the_assembler_paths():
    text = ea.LAUNCHER_TEMPLATE.read_text()
    assert ea.PY_MM == "3.12" and "bin/python3.12" in text and ea.CAFILE_REL in text
    assert ea.LAUNCHER_TEMPLATE.stat().st_mode & 0o111


# ----------------------------------------------------------------------- CLI

FLAGS = ["--wheelhouse", "a", "--install", "b", "--source", "c", "--project-build", "d", "--project-receipt", "e",
         "--destination", "f"]


def test_cli_usage_errors_exit_2_with_a_fixed_line(capsys):
    for argv in (FLAGS[:-2], [*FLAGS[:-2], "--dest", "f"], [*FLAGS, "--url", "x"]):
        with pytest.raises(SystemExit) as caught:
            ea.main(argv)
        assert caught.value.code == 2
        assert capsys.readouterr().err == "engine assemble failed: usage\n"


def test_cli_refusal_exits_1_with_a_fixed_line(monkeypatch, capsys):
    def refuse(*_args):
        raise ea.AssembleError()

    monkeypatch.setattr(ea, "assemble", refuse)
    assert ea.main(FLAGS) == 1
    assert capsys.readouterr().err == "engine assemble failed\n"


def test_cli_sanitizes_unexpected_exceptions(monkeypatch, capsys):
    def boom(*_args):
        raise RuntimeError("secret /private/path")

    monkeypatch.setattr(ea, "assemble", boom)
    assert ea.main(FLAGS) == 1
    captured = capsys.readouterr()
    assert captured.err == "engine assemble failed\n" and captured.out == ""


def test_cli_success_exits_0_silently(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(ea, "assemble", lambda *args: seen.append(args) or {})
    assert ea.main(FLAGS) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "") and len(seen) == 1


def test_platform_is_refused_before_any_io(env, monkeypatch):
    monkeypatch.setattr(ea, "_platform_check", REAL_PLATFORM_CHECK)
    monkeypatch.setattr(sys, "platform", "linux")
    env.refused(calls=0)


def test_existing_destination_and_traversal_are_refused(env):
    (env.out / "dest").mkdir()
    with pytest.raises(ea.AssembleError):
        env.assemble()
    with pytest.raises(ea.AssembleError):
        ea.assemble(env.house, env.install, env.export, env.build, env.receipt_path,
                    Path(f"{env.out}/../out/other"))
    assert env.calls == [] and [p.name for p in env.out.iterdir()] == ["dest"]


def test_module_never_references_in_process_builders():
    text = MODULE_PATH.read_text()
    assert not any(name in text for name in ("build_derived_wheel", "build_project_wheel", "_run_backend"))
