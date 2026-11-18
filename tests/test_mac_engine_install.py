import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import packaging
import packaging.markers
import packaging.requirements
import packaging.specifiers
import packaging.utils
import packaging.version
import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
MODULE_PATH = REPO / "mac/scripts/engine_install.py"
SPEC = importlib.util.spec_from_file_location("engine_install", MODULE_PATH)
assert SPEC and SPEC.loader
ei = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ei)

HOST = "https://files.pythonhosted.org/packages"
RUNTIME_NAME = "cpython-3.12.16+20261110-aarch64-apple-darwin-install_only.tar.gz"
RUNTIME_URL = f"{ei.engine_fetch.RUNTIME_PREFIX}20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only.tar.gz"
DERIVED_WHEEL = "http_ece-1.2.1-py2.py3-none-any.whl"
PYPROJECT = '[project]\nname = "x"\nversion = "1"\ndependencies = ["foo==1.0"]\n'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def _target():
    pin = {"python_version": "3.12.16", "target": "aarch64-apple-darwin", "minimum_macos": "13.0"}
    return ei.engine_dependencies.Target(pin)


def _rows(**versions):
    return [{"name": name, "version": version} for name, version in versions.items()]


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "inputs"
    root.mkdir()
    blobs: dict[str, bytes] = {}

    def add(url: str, data: bytes) -> dict:
        blobs[url] = data
        return {"url": url, "hash": f"sha256:{_sha(data)}", "size": len(data)}

    foo = add(f"{HOST}/aa/bb/foo-1.0-py3-none-any.whl", b"foo-bytes")
    bar = add(f"{HOST}/cc/dd/bar-2.0-py3-none-any.whl", b"bar-bytes!")
    pk = add(f"{HOST}/ee/ff/packaging-9.9.9-py3-none-any.whl", b"packaging-bytes")
    packages = [("foo", "1.0", foo), ("bar", "2.0", bar), ("packaging", "9.9.9", pk)]
    lock_lines = []
    for name, version, wheel in packages:
        lock_lines.append(
            f'[[package]]\nname = "{name}"\nversion = "{version}"\nwheels = [\n'
            f'  {{ url = "{wheel["url"]}", hash = "{wheel["hash"]}", size = {wheel["size"]} }},\n]\n')
    lock = root / "uv.lock"
    lock.write_text("\n".join(lock_lines))
    pyproject = root / "pyproject.toml"
    pyproject.write_text(PYPROJECT)
    manifest = root / "engine-runtime.json"
    manifest.write_text("{}")

    runtime_bytes = b"runtime-archive"
    tool_url = f"{HOST}/12/34/setuptools-1.0-py3-none-any.whl"
    sdist_url = f"{HOST}/56/78/http_ece-1.2.1.tar.gz"
    tool = add(tool_url, b"setuptools-bytes")
    sdist = add(sdist_url, b"sdist-bytes")
    derived_bytes = b"http-ece-derived-wheel"
    build_inputs = ei.engine_fetch.engine_derived_wheel.BuildInputs(
        {
            "build_tools": [{"name": "setuptools", "filename": "setuptools-1.0-py3-none-any.whl",
                             "url": tool_url, "size": tool["size"], "sha256": _sha(blobs[tool_url])}],
            "derived_wheels": [{
                "name": "http-ece", "version": "1.2.1",
                "sdist": {"filename": "http_ece-1.2.1.tar.gz", "url": sdist_url, "size": sdist["size"],
                          "sha256": _sha(blobs[sdist_url])},
                "wheel": {"filename": DERIVED_WHEEL, "size": len(derived_bytes), "sha256": _sha(derived_bytes)},
            }],
        },
        "b" * 64,
    )
    pin = {"filename": RUNTIME_NAME, "url": RUNTIME_URL, "size": len(runtime_bytes),
           "sha256": _sha(runtime_bytes), "python_version": "3.12.16",
           "target": "aarch64-apple-darwin", "minimum_macos": "13.0"}
    monkeypatch.setattr(ei, "_platform_check", lambda: None)
    monkeypatch.setattr(ei.engine_fetch, "LOCK_PATH", lock)
    monkeypatch.setattr(ei.engine_fetch, "PYPROJECT_PATH", pyproject)
    monkeypatch.setattr(ei.engine_runtime, "MANIFEST", manifest)
    monkeypatch.setattr(ei.engine_runtime, "load_runtime_pin", lambda: dict(pin))
    monkeypatch.setattr(ei.engine_fetch.engine_derived_wheel, "load_build_inputs", lambda: build_inputs)
    monkeypatch.setattr(ei.engine_fetch, "load_verified_packaging", lambda house: packaging)

    hashes = {
        "pyproject_sha256": _sha(pyproject.read_bytes()),
        "lock_sha256": _sha(lock.read_bytes()),
        "runtime_pin_sha256": _sha(manifest.read_bytes()),
        "build_inputs_sha256": "b" * 64,
    }

    def entry(name, version, wheel):
        return {"name": name, "version": version, "origin": "lock",
                "filename": wheel["url"].rsplit("/", 1)[-1], "url": wheel["url"], "size": wheel["size"],
                "sha256": wheel["hash"][7:], "tags": ["py3-none-any"]}

    report = {
        "schema": 1, "kind": "lock-wheel-audit", "installable": False, "target": "t", "inputs": hashes,
        "selected": [entry("bar", "2.0", bar), entry("foo", "1.0", foo),
                     {"name": "http-ece", "version": "1.2.1", "origin": "derived",
                      "filename": DERIVED_WHEEL, "url": None, "size": len(derived_bytes),
                      "sha256": _sha(derived_bytes), "tags": ["py3-none-any"]}],
        "blockers": [],
    }
    report_path = root / "report.json"
    report_path.write_text(json.dumps(report))

    house = tmp_path.resolve() / "house"
    house.mkdir()
    os.chmod(house, 0o700)
    for name, data in {RUNTIME_NAME: runtime_bytes, "setuptools-1.0-py3-none-any.whl": b"setuptools-bytes",
                       "http_ece-1.2.1.tar.gz": b"sdist-bytes",
                       "packaging-9.9.9-py3-none-any.whl": b"packaging-bytes",
                       "foo-1.0-py3-none-any.whl": b"foo-bytes",
                       "bar-2.0-py3-none-any.whl": b"bar-bytes!"}.items():
        (house / name).write_bytes(data)
    inputs = ei.engine_fetch._Inputs()
    ei.engine_fetch._write_receipt(house, "bootstrap-receipt.json", ei.engine_fetch._bootstrap_plan(inputs), hashes)
    ei.engine_fetch._write_receipt(house, "closure-receipt.json",
                                   ei.engine_fetch._closure_plan(inputs, report_path), hashes)

    out = tmp_path.resolve() / "out"
    out.mkdir()
    os.chmod(out, 0o700)

    state = type("Env", (), {})()
    state.house, state.out, state.report, state.report_path = house, out, report, report_path
    state.hashes, state.pin, state.derived_bytes = hashes, pin, derived_bytes
    state.calls, state.extractions = [], []
    state.d1_status, state.pip_status, state.check_status, state.smoke_status = 0, 0, 0, 0
    state.dists = {
        "foo": {"version": "1.0", "requires": ["bar>=2"], "scripts": ["foo-cli"]},
        "bar": {"version": "2.0",
                "requires": ['http-ece>=1; sys_platform == "darwin"', 'win-only; sys_platform == "win32"'],
                "scripts": []},
        "http-ece": {"version": "1.2.1", "requires": [], "scripts": []},
    }
    state.extra_pip_files = {}

    def fake_extract(archive, destination):
        state.extractions.append((archive, destination))
        site = destination / "lib/python3.12/site-packages"
        (destination / "bin").mkdir(parents=True)
        (destination / "bin/python3.12").write_text("#!/bin/sh\n")
        (destination / "bin/pip3").write_text("#!/bin/sh\n")
        _dist(site, "pip", "26.3")

    def fake_run(argv, env_, cwd, timeout):
        state.calls.append((argv, env_, cwd, timeout))
        if argv[0] == sys.executable:
            if argv[3] == "build":
                if state.d1_status:
                    return state.d1_status, b""
                destination = Path(argv[argv.index("--destination") + 1])
                destination.mkdir(mode=0o700)
                (destination / DERIVED_WHEEL).write_bytes(derived_bytes)
                body = {"name": "http-ece", "version": "1.2.1",
                        "wheel": {"filename": DERIVED_WHEEL, "size": len(derived_bytes),
                                  "sha256": _sha(derived_bytes)}}
                return 0, (json.dumps(body, sort_keys=True, indent=2) + "\n").encode()
            return 0, b""
        assert argv[:3] == [ei.SANDBOX_EXEC, "-p", ei.SANDBOX_PROFILE]
        inner = argv[3:]
        if "install" in inner:
            prefix = Path(inner[0]).parent.parent
            site = prefix / "lib/python3.12/site-packages"
            for name, spec in state.dists.items():
                _dist(site, name, spec["version"], spec["requires"], spec["scripts"], prefix)
            for rel, data in state.extra_pip_files.items():
                target = prefix / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            return state.pip_status, b""
        if "check" in inner:
            return state.check_status, b""
        return state.smoke_status, b"OpenSSL 3.0.99 1 Jan 2099\n"

    monkeypatch.setattr(ei, "_run", fake_run)
    monkeypatch.setattr(ei.engine_runtime, "extract_pinned_runtime", fake_extract)
    state.install = lambda name="dest": ei.install(house, report_path, out / name)
    return state


def _leftovers(out: Path) -> list[str]:
    return sorted(p.name for p in out.iterdir())


# --------------------------------------------------------- requirements


def test_requirements_file_is_exact_sorted_and_derived_hash_comes_from_the_pin(env):
    receipt = env.install()
    text = (env.out / "dest/requirements.txt").read_text()
    assert text == (
        f"bar==2.0 --hash=sha256:{_sha(b'bar-bytes!')}\n"
        f"foo==1.0 --hash=sha256:{_sha(b'foo-bytes')}\n"
        f"http-ece==1.2.1 --hash=sha256:{_sha(env.derived_bytes)}\n")
    assert [row["name"] for row in receipt["selected"]] == ["bar", "foo", "http-ece"]


def test_requirements_text_sorts_by_name():
    rows = [{"name": "b", "version": "1", "sha256": "2" * 64}, {"name": "a", "version": "2", "sha256": "1" * 64}]
    assert ei.requirements_text(rows) == f"a==2 --hash=sha256:{'1' * 64}\nb==1 --hash=sha256:{'2' * 64}\n"


# ----------------------------------------------------- process seam


def test_pip_argv_env_and_sandbox_are_exact(env):
    env.install()
    pip = next(c for c in env.calls if "install" in c[0])
    argv, environment, cwd, timeout = pip
    work = Path(environment["HOME"]).parent
    prefix = work / "prefix"
    assert argv == [
        "/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)",
        str(prefix / "bin/python3.12"), "-I", "-B", "-m", "pip", "install", "--isolated", "--no-index",
        "--find-links", str(work / "links"), "--only-binary", ":all:", "--require-hashes", "--no-compile",
        "--no-warn-script-location", "--disable-pip-version-check", "--no-input",
        "-r", str(work / "requirements.txt")]
    assert environment == {"PATH": "/usr/bin:/bin", "HOME": str(work / "home"),
                           "TMPDIR": str(work / "home"), "LANG": "C"}
    assert cwd == work / "home" and 0 < timeout <= 900
    assert "--target" not in argv and "--prefix" not in argv


def test_pip_check_and_smoke_run_under_the_same_env_and_sandbox(env):
    env.install()
    pip = next(c for c in env.calls if "install" in c[0])
    check = next(c for c in env.calls if c[0][-2:] == ["pip", "check"])
    smoke = next(c for c in env.calls if "-c" in c[0])
    for call in (check, smoke):
        assert call[0][:3] == [ei.SANDBOX_EXEC, "-p", ei.SANDBOX_PROFILE]
        assert call[1] == pip[1]
        assert call[0][4:6] == ["-I", "-B"]
    code = smoke[0][smoke[0].index("-c") + 1]
    for module in ("ssl", "anydoc", "http_ece", "cryptography.hazmat.bindings._rust", "certifi", "claude_agent_sdk"):
        assert repr(module) in code
    assert len(ei.SMOKE_MODULES) == 28


def test_derived_wheel_runs_as_a_child_process_build_then_verify(env):
    receipt = env.install()
    builds = [c for c in env.calls if c[0][0] == sys.executable]
    assert [c[0][3] for c in builds] == ["build", "verify"]
    work = Path(builds[0][1]["HOME"]).parent
    assert builds[0][0] == [
        sys.executable, "-I", str(ei.DERIVED_WHEEL_SCRIPT), "build", "--name", "http-ece",
        "--sdist", str(env.house / "http_ece-1.2.1.tar.gz"),
        "--backend-wheel", str(env.house / "setuptools-1.0-py3-none-any.whl"),
        "--destination", str(work / "derived")]
    assert builds[1][0][-4:-2] == ["--name", "http-ece"]
    assert receipt["derived"][0]["wheel"]["sha256"] == _sha(env.derived_bytes)
    assert builds[0][1] == {"PATH": "/usr/bin:/bin", "HOME": str(work / "home"),
                            "TMPDIR": str(work / "home"), "LANG": "C"}


def test_module_never_imports_the_in_process_derived_wheel_builder():
    source = MODULE_PATH.read_text()
    assert "build_derived_wheel" not in source
    assert "import engine_derived_wheel" not in source
    assert '_load_sibling("engine_derived_wheel")' not in source


# ------------------------------------------------------- publication


def test_install_publishes_prefix_requirements_and_receipt_only(env):
    receipt = env.install()
    dest = env.out / "dest"
    assert _leftovers(env.out) == ["dest"]
    assert sorted(p.name for p in dest.iterdir()) == ["install-receipt.json", "prefix", "requirements.txt"]
    assert json.loads((dest / "install-receipt.json").read_text()) == receipt
    assert receipt["inputs"] == env.hashes
    assert receipt["pip_version"] == "26.3"
    assert receipt["openssl_version"] == "OpenSSL 3.0.99 1 Jan 2099"
    assert receipt["removed_scripts"] == ["foo-cli"]
    paths = {row["path"] for row in receipt["inventory"]}
    assert "bin/pip3" in paths and "bin/foo-cli" not in paths
    record = (dest / "prefix/lib/python3.12/site-packages/foo-1.0.dist-info/RECORD").read_text()
    assert "bin/foo-cli" not in record and "METADATA" in record and "RECORD,," in record
    assert not (dest / "prefix/bin/foo-cli").exists()
    assert sorted(receipt) == ["derived", "inputs", "inventory", "openssl_version", "pip_version",
                               "removed_scripts", "schema", "selected"]
    assert str(env.out) not in json.dumps(receipt)


def test_two_installs_have_identical_inventories(env):
    first = env.install("one")
    second = env.install("two")
    assert first == second
    assert first["inventory"] == json.loads((env.out / "two/install-receipt.json").read_text())["inventory"]


def test_existing_destination_and_traversal_are_refused_without_extraction(env):
    (env.out / "dest").mkdir()
    with pytest.raises(ei.InstallError):
        env.install()
    with pytest.raises(ei.InstallError):
        ei.install(env.house, env.report_path, env.out / ".." / "x")
    assert env.extractions == [] and env.calls == []


def test_platform_is_refused_before_any_io(env, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(ei.platform, "machine", lambda: "x86_64")
    before = _leftovers(env.out)
    with pytest.raises(ei.InstallError):
        ei.install(env.house, env.report_path, env.out / "dest")
    assert _leftovers(env.out) == before == []


# ---------------------------------------------------------- step 1


def test_wheelhouse_byte_flip_means_zero_runtime_extraction(env):
    victim = env.house / "foo-1.0-py3-none-any.whl"
    data = bytearray(victim.read_bytes())
    data[0] ^= 1
    victim.write_bytes(bytes(data))
    with pytest.raises(ei.InstallError):
        env.install()
    assert env.extractions == [] and env.calls == []
    assert _leftovers(env.out) == []


def test_runtime_archive_flip_is_caught_before_extraction(env):
    victim = env.house / RUNTIME_NAME
    victim.write_bytes(b"runtime-archivX")
    with pytest.raises(ei.InstallError):
        env.install()
    assert env.extractions == []


@pytest.mark.parametrize("mutate", ["extra-file", "receipt-input", "missing-receipt", "receipt-artifact"])
def test_wheelhouse_receipts_and_membership_are_enforced(env, mutate):
    if mutate == "extra-file":
        (env.house / "stray-1.0-py3-none-any.whl").write_bytes(b"x")
    elif mutate == "missing-receipt":
        (env.house / "closure-receipt.json").unlink()
    else:
        path = env.house / ("bootstrap-receipt.json" if mutate == "receipt-input" else "closure-receipt.json")
        body = json.loads(path.read_text())
        if mutate == "receipt-input":
            body["inputs"]["lock_sha256"] = "0" * 64
        else:
            body["artifacts"][0]["size"] += 1
        path.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
    with pytest.raises(ei.InstallError):
        env.install()
    assert env.extractions == [] and env.calls == []


@pytest.mark.parametrize("mutate", ["version", "sha", "url", "name-case"])
def test_report_rows_must_agree_with_the_pins(env, mutate):
    rows = {r["name"]: r for r in env.report["selected"]}
    if mutate == "version":
        rows["http-ece"]["version"] = "1.2.2"
    elif mutate == "sha":
        rows["http-ece"]["sha256"] = "f" * 64
    elif mutate == "url":
        rows["http-ece"]["url"] = f"{HOST}/aa/http_ece.whl"
    else:
        rows["foo"]["name"] = "Foo"
    env.report_path.write_text(json.dumps(env.report))
    with pytest.raises(ei.InstallError):
        env.install()
    assert env.extractions == []


def test_report_with_tampered_lock_row_is_refused(env):
    rows = {r["name"]: r for r in env.report["selected"]}
    rows["foo"]["sha256"] = "f" * 64
    env.report_path.write_text(json.dumps(env.report))
    with pytest.raises((ei.InstallError, ei.engine_fetch.FetchError)):
        env.install()
    assert env.extractions == []


# ------------------------------------------------- failure => no output


@pytest.mark.parametrize("flag", ["d1_status", "pip_status", "check_status", "smoke_status"])
def test_a_failing_child_process_leaves_no_destination(env, flag):
    setattr(env, flag, 1)
    with pytest.raises(ei.InstallError):
        env.install()
    assert _leftovers(env.out) == []


def test_d1_exit_one_stops_before_pip(env):
    env.d1_status = 1
    with pytest.raises(ei.InstallError):
        env.install()
    assert not any("install" in c[0] for c in env.calls)


def test_d1_receipt_that_differs_from_the_pin_fails_the_install(env, monkeypatch):
    real = ei._run

    def lying(argv, env_, cwd, timeout):
        status, out = real(argv, env_, cwd, timeout)
        if argv[0] == sys.executable and argv[3] == "build":
            body = json.loads(out)
            body["wheel"]["sha256"] = "0" * 64
            out = json.dumps(body, sort_keys=True, indent=2).encode()
        return status, out

    monkeypatch.setattr(ei, "_run", lying)
    with pytest.raises(ei.InstallError):
        env.install()
    assert _leftovers(env.out) == []


def test_a_derived_wheel_that_is_not_the_pinned_bytes_fails(env, monkeypatch):
    real = ei._run

    def swapping(argv, env_, cwd, timeout):
        status, out = real(argv, env_, cwd, timeout)
        if argv[0] == sys.executable and argv[3] == "build":
            destination = Path(argv[argv.index("--destination") + 1])
            (destination / DERIVED_WHEEL).write_bytes(b"x" * len(env.derived_bytes))
        return status, out

    monkeypatch.setattr(ei, "_run", swapping)
    with pytest.raises(ei.InstallError):
        env.install()
    assert not any("install" in c[0] for c in env.calls)


@pytest.mark.parametrize("added", ["lib/python3.12/site-packages/foo-1.0.dist-info/direct_url.json",
                                   "lib/python3.12/site-packages/foo/__pycache__/x.cpython-312.pyc",
                                   "pyvenv.cfg", "lib/python3.12/EXTERNALLY-MANAGED"])
def test_forbidden_added_files_are_refused(env, added):
    env.extra_pip_files = {added: b"x"}
    with pytest.raises(ei.InstallError):
        env.install()
    assert _leftovers(env.out) == []


def test_files_the_runtime_already_had_are_not_blamed_on_the_install(tmp_path):
    prefix = tmp_path / "p"
    (prefix / "lib").mkdir(parents=True)
    (prefix / "lib/EXTERNALLY-MANAGED").write_text("x")
    (prefix / "lib/old.pyc").write_bytes(b"x")
    ei.check_added(prefix, ei._paths(prefix))
    (prefix / "lib/new.pyc").write_bytes(b"x")
    with pytest.raises(ei.InstallError):
        ei.check_added(prefix, {"lib", "lib/EXTERNALLY-MANAGED", "lib/old.pyc"})


def test_script_with_the_build_path_left_in_place_is_refused(env):
    # An unrecorded script (so the RECORD-driven scrub cannot delete it) keeps the build path.
    env.dists["foo"]["scripts"] = []
    env.extra_pip_files = {"bin/stray": b"#!/private/build/prefix/bin/python3.12\n"}
    with pytest.raises(ei.InstallError):
        env.install()
    assert _leftovers(env.out) == []


def test_runtime_pip_version_is_pinned(env, monkeypatch):
    monkeypatch.setattr(ei, "RUNTIME_PIP", ("pip", "26.2"))
    with pytest.raises(ei.InstallError):
        env.install()


# ------------------------------------------------------------ step 6


def _site(tmp_path: Path) -> Path:
    site = tmp_path / "prefix/lib/python3.12/site-packages"
    site.mkdir(parents=True)
    _dist(site, "pip", "26.3")
    return site


def _validate(site, rows, pyproject=PYPROJECT):
    return ei.validate_installed(site, rows, packaging, _target(), pyproject.encode())


def test_a_consistent_tree_validates_and_follows_extras_and_markers(tmp_path):
    site = _site(tmp_path)
    _dist(site, "foo", "1.0", ["bar[fast]>=2"])
    _dist(site, "bar", "2.0", ['speedy; extra == "fast"', 'win-only; sys_platform == "win32"',
                               'mac-only; sys_platform == "darwin"'])
    _dist(site, "speedy", "1.0")
    _dist(site, "mac-only", "1.0")
    installed = _validate(site, _rows(foo="1.0", bar="2.0", speedy="1.0", **{"mac-only": "1.0"}))
    assert set(installed) == {"pip", "foo", "bar", "speedy", "mac-only"}


def test_extra_not_requested_leaves_the_extra_dist_unreachable(tmp_path):
    site = _site(tmp_path)
    _dist(site, "foo", "1.0", ["bar>=2"])
    _dist(site, "bar", "2.0", ['speedy; extra == "fast"'])
    _dist(site, "speedy", "1.0")
    with pytest.raises(ei.InstallError):
        _validate(site, _rows(foo="1.0", bar="2.0", speedy="1.0"))


@pytest.mark.parametrize("case", [
    "missing-dep", "extra-dist", "wrong-version", "unsatisfied-marker-gated", "unreachable",
    "wrong-name-in-metadata", "duplicate", "url-requirement", "pip-version"])
def test_inconsistent_trees_are_refused(tmp_path, case):
    site = _site(tmp_path)
    rows = _rows(foo="1.0", bar="2.0")
    foo_requires = ["bar>=2"]
    bar_requires: list[str] = []
    bar_version = "2.0"
    if case == "missing-dep":
        foo_requires = ["bar>=2", "ghost"]
    elif case == "unsatisfied-marker-gated":
        bar_requires = ['foo>=5; sys_platform == "darwin"']
    elif case == "wrong-version":
        bar_version = "2.1"
    elif case == "url-requirement":
        foo_requires = ["bar @ https://example.invalid/bar.whl"]
    _dist(site, "foo", "1.0", foo_requires)
    _dist(site, "bar", bar_version, bar_requires)
    if case == "extra-dist":
        _dist(site, "surplus", "1.0")
    elif case == "unreachable":
        _dist(site, "orphan", "1.0")
        rows += _rows(orphan="1.0")
    elif case == "wrong-name-in-metadata":
        meta = site / "bar-2.0.dist-info/METADATA"
        meta.write_text(meta.read_text().replace("Name: bar", "Name: baz"))
    elif case == "duplicate":
        other = site / "Bar-2.0.0.dist-info"
        other.mkdir()
        (other / "METADATA").write_text("Name: Bar\nVersion: 2.0.0\n")
    elif case == "pip-version":
        (site / "pip-26.3.dist-info/METADATA").write_text("Name: pip\nVersion: 26.2\n")
    with pytest.raises(ei.InstallError):
        _validate(site, rows)


def test_missing_distribution_from_the_report_is_refused(tmp_path):
    site = _site(tmp_path)
    _dist(site, "foo", "1.0", [])
    with pytest.raises(ei.InstallError):
        _validate(site, _rows(foo="1.0", bar="2.0"))


def test_root_requirement_must_be_satisfied(tmp_path):
    site = _site(tmp_path)
    _dist(site, "foo", "1.1", [])
    with pytest.raises(ei.InstallError):
        _validate(site, _rows(foo="1.1"))


# ------------------------------------------------------------ step 7


def test_scrub_removes_recorded_scripts_only_and_keeps_runtime_scripts(tmp_path):
    prefix = tmp_path / "prefix"
    site = prefix / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "bin/pip3").write_text("#!/bin/sh\n")
    baseline = ei._paths(prefix)
    _dist(site, "foo", "1.0", [], ["foo-cli", "foo-2"], prefix)
    installed = ei._read_dists(site, packaging)
    removed = ei.scrub_scripts(prefix, site, installed, _rows(foo="1.0"), baseline)
    assert removed == ["foo-2", "foo-cli"]
    assert sorted(p.name for p in (prefix / "bin").iterdir()) == ["pip3"]
    record = (site / "foo-1.0.dist-info/RECORD").read_text()
    assert record == "foo-1.0.dist-info/METADATA,,\nfoo-1.0.dist-info/RECORD,,\n"
    ei.refuse_build_path(prefix, site, baseline, tmp_path)


def test_scrub_refuses_a_record_row_that_names_a_runtime_script(tmp_path):
    prefix = tmp_path / "prefix"
    site = prefix / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "bin/pip3").write_text("#!/bin/sh\n")
    baseline = ei._paths(prefix)
    _dist(site, "foo", "1.0", [])
    record = site / "foo-1.0.dist-info/RECORD"
    record.write_text(record.read_text() + "../../../bin/pip3,,\n")
    installed = ei._read_dists(site, packaging)
    with pytest.raises(ei.InstallError):
        ei.scrub_scripts(prefix, site, installed, _rows(foo="1.0"), baseline)
    assert (prefix / "bin/pip3").exists()


@pytest.mark.parametrize("where", ["bin-file", "dist-info", "bin-symlink", "new-bin-file"])
def test_refuse_build_path(tmp_path, where):
    build = tmp_path / "work"
    prefix = build / "prefix"
    site = prefix / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "bin/tool").write_text("#!/bin/sh\n")
    _dist(site, "foo", "1.0", [])
    baseline = ei._paths(prefix)
    ei.refuse_build_path(prefix, site, baseline, build)
    if where == "bin-file":
        (prefix / "bin/tool").write_text(f"#!{prefix}/bin/python3.12\n")
    elif where == "dist-info":
        (site / "foo-1.0.dist-info/INSTALLER").write_text(f"{build}/x\n")
    elif where == "bin-symlink":
        (prefix / "bin/tool").unlink()
        os.symlink(f"{build}/prefix/bin/python3.12", prefix / "bin/tool")
    else:
        (prefix / "bin/extra").write_text("#!/bin/sh\n")
    with pytest.raises(ei.InstallError):
        ei.refuse_build_path(prefix, site, baseline, build)


def test_build_path_spanning_a_read_chunk_is_found(tmp_path):
    needle = b"/private/build/x"
    path = tmp_path / "big"
    path.write_bytes(b"a" * ((1 << 20) - 5) + needle + b"b" * 10)
    assert ei._contains(path, needle)
    path.write_bytes(b"a" * ((1 << 20) - 5) + needle[:-1] + b"b" * 10)
    assert not ei._contains(path, needle)


def test_inventory_is_sorted_with_sizes_hashes_and_link_targets(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b/z").write_bytes(b"zz")
    (tmp_path / "a").write_bytes(b"a")
    os.symlink("a", tmp_path / "link")
    assert ei.inventory(tmp_path) == [
        {"path": "a", "size": 1, "sha256": _sha(b"a")},
        {"path": "b/z", "size": 2, "sha256": _sha(b"zz")},
        {"path": "link", "symlink": "a"},
    ]


# --------------------------------------------------------------- CLI


def test_cli_usage_errors_exit_2_with_a_fixed_line(capsys):
    for argv in ([], ["--wheelhouse", "x"], ["--url", "u"],
                 ["--wheelhouse", "a", "--audit-report", "b", "--destination", "c", "--no-index"]):
        with pytest.raises(SystemExit) as raised:
            ei.main(argv)
        assert raised.value.code == 2
    assert capsys.readouterr().err == "engine install failed: usage\n" * 4


def test_cli_refusal_exits_1_with_a_fixed_line(env, capsys):
    (env.house / "closure-receipt.json").unlink()
    code = ei.main(["--wheelhouse", str(env.house), "--audit-report", str(env.report_path),
                    "--destination", str(env.out / "dest")])
    assert code == 1
    assert capsys.readouterr().err == "engine install failed\n"


def test_cli_success_exits_0_silently(env, capsys):
    code = ei.main(["--wheelhouse", str(env.house), "--audit-report", str(env.report_path),
                    "--destination", str(env.out / "dest")])
    assert code == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_cli_sanitizes_unexpected_exceptions(monkeypatch, capsys):
    def boom(*_args):
        raise AttributeError("secret detail")

    monkeypatch.setattr(ei, "install", boom)
    assert ei.main(["--wheelhouse", "a", "--audit-report", "b", "--destination", "c"]) == 1
    assert capsys.readouterr().err == "engine install failed\n"


def test_derived_receipt_is_found_after_backend_stdout_noise(env, monkeypatch):
    real = ei._run

    def noisy(argv, env_, cwd, timeout):
        status, out = real(argv, env_, cwd, timeout)
        if argv[0] == sys.executable and argv[3] == "build":
            out = b"warning line\n{ inline brace\n" + json.dumps(json.loads(out), sort_keys=True, indent=2).encode()
        return status, out

    monkeypatch.setattr(ei, "_run", noisy)
    assert env.install()["derived"][0]["name"] == "http-ece"
