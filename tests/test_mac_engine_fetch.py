import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
MODULE_PATH = REPO / "mac/scripts/engine_fetch.py"
SPEC = importlib.util.spec_from_file_location("engine_fetch", MODULE_PATH)
assert SPEC and SPEC.loader
ef = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ef)

HOST = "https://files.pythonhosted.org/packages"
RUNTIME_URL = f"{ef.RUNTIME_PREFIX}20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only.tar.gz"
RUNTIME_NAME = "cpython-3.12.16+20261110-aarch64-apple-darwin-install_only.tar.gz"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _packaging_wheel(path: Path, version: str = "9.9.9") -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("packaging/__init__.py", f'__version__ = "{version}"\n')
    return path.read_bytes()


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
    wheel_src = root / "pk.whl"
    pk = add(f"{HOST}/ee/ff/packaging-9.9.9-py3-none-any.whl", _packaging_wheel(wheel_src))
    packages = [
        {"name": "foo", "version": "1.0", "wheels": [foo]},
        {"name": "bar", "version": "2.0", "wheels": [bar]},
        {"name": "packaging", "version": "9.9.9", "wheels": [pk]},
    ]
    lines = []
    for package in packages:
        lines.append(f'[[package]]\nname = "{package["name"]}"\nversion = "{package["version"]}"\nwheels = [')
        for w in package["wheels"]:
            lines.append(f'  {{ url = "{w["url"]}", hash = "{w["hash"]}", size = {w["size"]} }},')
        lines.append("]\n")
    lock = root / "uv.lock"
    lock.write_text("\n".join(lines))
    pyproject = root / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'x'\n")
    manifest = root / "engine-runtime.json"
    manifest.write_text("{}")

    runtime = add(RUNTIME_URL, b"runtime-archive")
    tool_url = f"{HOST}/12/34/setuptools-1.0-py3-none-any.whl"
    sdist_url = f"{HOST}/56/78/http_ece-1.2.1.tar.gz"
    tool = add(tool_url, b"setuptools-bytes")
    sdist = add(sdist_url, b"sdist-bytes")
    build_inputs = ef.engine_derived_wheel.BuildInputs(
        {
            "build_tools": [{"name": "setuptools", "filename": "setuptools-1.0-py3-none-any.whl",
                             "url": tool_url, "size": tool["size"], "sha256": _sha(blobs[tool_url])}],
            "derived_wheels": [{"name": "http-ece", "sdist": {
                "filename": "http_ece-1.2.1.tar.gz", "url": sdist_url,
                "size": sdist["size"], "sha256": _sha(blobs[sdist_url])}}],
        },
        "b" * 64,
    )
    pin = {"filename": RUNTIME_NAME, "url": RUNTIME_URL, "size": runtime["size"],
           "sha256": _sha(blobs[RUNTIME_URL])}
    monkeypatch.setattr(ef, "LOCK_PATH", lock)
    monkeypatch.setattr(ef, "PYPROJECT_PATH", pyproject)
    monkeypatch.setattr(ef.engine_runtime, "MANIFEST", manifest)
    monkeypatch.setattr(ef.engine_runtime, "load_runtime_pin", lambda: dict(pin))
    monkeypatch.setattr(ef.engine_derived_wheel, "load_build_inputs", lambda: build_inputs)

    calls: list[tuple[list[str], dict, int]] = []
    served = dict(blobs)

    def fake_run(argv, env_, timeout):
        calls.append((argv, env_, timeout))
        out = Path(argv[argv.index("-o") + 1])
        out.write_bytes(served[argv[-1]])
        return 0

    monkeypatch.setattr(ef, "_run", fake_run)
    house = tmp_path.resolve() / "house"
    house.mkdir(mode=0o700)
    os.chmod(house, 0o700)
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
        "schema": 1, "kind": "lock-wheel-audit", "installable": False, "target": "t",
        "inputs": hashes,
        "selected": [entry("bar", "2.0", bar), entry("foo", "1.0", foo),
                     {"name": "http-ece", "version": "1.2.1", "origin": "derived",
                      "filename": "http_ece-1.2.1-py2.py3-none-any.whl", "url": None,
                      "size": 5, "sha256": "e" * 64, "tags": ["py3-none-any"]}],
        "blockers": [],
    }

    def write_report(data) -> Path:
        path = root / "report.json"
        path.write_text(json.dumps(data))
        return path

    return type("Env", (), {
        "house": house, "calls": calls, "served": served, "report": report, "write_report": staticmethod(write_report),
        "lock": lock, "root": root, "blobs": blobs, "foo": foo, "pk": pk, "hashes": hashes,
    })


def _files(house: Path) -> list[str]:
    return sorted(p.name for p in house.iterdir())


# ----------------------------------------------------------- transport


def test_curl_argv_and_env_are_exact(env):
    ef.closure(env.house, env.write_report(env.report))
    assert len(env.calls) == 2
    argv, environment, timeout = next(c for c in env.calls if c[0][-1].endswith("foo-1.0-py3-none-any.whl"))
    out = argv[argv.index("-o") + 1]
    assert argv == [
        "/usr/bin/curl", "-q", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2", "-fsS",
        "--max-redirs", "5", "--max-filesize", str(env.foo["size"]), "--connect-timeout", "30",
        "--max-time", "900", "-o", out, f"{HOST}/aa/bb/foo-1.0-py3-none-any.whl",
    ]
    assert set(environment) == {"PATH", "HOME", "LANG"}
    assert environment["PATH"] == "/usr/bin:/bin" and environment["LANG"] == "C"
    assert Path(environment["HOME"]).parent == env.house
    assert timeout > 900


def test_closure_publishes_only_lock_entries_and_receipt(env):
    ef.closure(env.house, env.write_report(env.report))
    assert _files(env.house) == ["bar-2.0-py3-none-any.whl", "closure-receipt.json", "foo-1.0-py3-none-any.whl"]
    receipt = json.loads((env.house / "closure-receipt.json").read_text())
    assert [a["filename"] for a in receipt["artifacts"]] == ["bar-2.0-py3-none-any.whl", "foo-1.0-py3-none-any.whl"]
    assert {a["source"] for a in receipt["artifacts"]} == {"lock"}
    assert receipt["inputs"] == env.hashes
    assert set(receipt["artifacts"][0]) == {"filename", "url", "size", "sha256", "origin", "source"}


def test_bootstrap_fetches_runtime_build_inputs_and_packaging(env):
    ef.bootstrap(env.house)
    assert _files(env.house) == sorted([
        RUNTIME_NAME, "setuptools-1.0-py3-none-any.whl", "http_ece-1.2.1.tar.gz",
        "packaging-9.9.9-py3-none-any.whl", "bootstrap-receipt.json"])
    receipt = json.loads((env.house / "bootstrap-receipt.json").read_text())
    assert {a["source"] for a in receipt["artifacts"]} == {"runtime-pin", "build-inputs", "lock"}
    assert (env.house / RUNTIME_NAME).read_bytes() == b"runtime-archive"


def test_closure_does_not_overwrite_the_bootstrap_receipt(env):
    ef.bootstrap(env.house)
    boot = (env.house / "bootstrap-receipt.json").read_bytes()
    ef.closure(env.house, env.write_report(env.report))
    assert (env.house / "bootstrap-receipt.json").read_bytes() == boot
    closure = json.loads((env.house / "closure-receipt.json").read_text())
    assert {a["source"] for a in closure["artifacts"]} == {"lock"}
    sources = {a["source"] for a in json.loads(boot)["artifacts"]}
    assert {"runtime-pin", "build-inputs"} <= sources


def test_bootstrap_requires_exactly_one_setuptools_build_tool(env, monkeypatch):
    inputs = ef.engine_derived_wheel.load_build_inputs()
    for tools in ([], inputs.data["build_tools"] * 2):
        data = copy.deepcopy(inputs.data)
        data["build_tools"] = copy.deepcopy(tools)
        monkeypatch.setattr(ef.engine_derived_wheel, "load_build_inputs",
                            lambda data=data: ef.engine_derived_wheel.BuildInputs(data, "b" * 64))
        with pytest.raises(ef.FetchError):
            ef.bootstrap(env.house)
    assert env.calls == []


def test_runtime_destination_name_must_match_the_pin_url_basename(env, monkeypatch):
    # The destination file name comes from the pin; the validator ties it to the URL
    # (%2B decoded to +), and the plan re-checks it so a drifted pin cannot rename a file.
    boot = ef._bootstrap_plan(ef._Inputs())
    assert boot[0]["filename"] == RUNTIME_NAME and "%2B" in boot[0]["url"]
    pin = {"filename": "other.tar.gz", "url": RUNTIME_URL, "size": 1, "sha256": "a" * 64}
    monkeypatch.setattr(ef.engine_runtime, "load_runtime_pin", lambda: dict(pin))
    with pytest.raises(ef.FetchError):
        ef.bootstrap(env.house)
    assert env.calls == []
    assert _files(env.house) == []


# ------------------------------------------------- authority (no calls)


def _mutations():
    def field(entry_index, key, value):
        def apply(report):
            report["selected"][entry_index][key] = value
        return apply

    def top(key, value):
        def apply(report):
            report[key] = value
        return apply

    def inp(key):
        def apply(report):
            report["inputs"][key] = "0" * 64
        return apply

    cases = {
        "name": field(0, "name", "foo"),
        "version": field(0, "version", "2.1"),
        "filename": field(0, "filename", "other-2.0-py3-none-any.whl"),
        **{f"{key}_{label}": field(0, key, value)
           for key in ("name", "version", "filename")
           for label, value in (("list", ["x"]), ("dict", {"x": 1}), ("int", 1), ("none", None))},
        "url": field(0, "url", f"{HOST}/cc/dd/other-2.0-py3-none-any.whl"),
        "size_plus": lambda r: r["selected"][0].__setitem__("size", r["selected"][0]["size"] + 1),
        "size_bool": field(0, "size", True),
        "sha": field(0, "sha256", "0" * 64),
        "unknown_origin": field(0, "origin", "pin"),
        "extra_key": field(0, "extra", 1),
        "kind": top("kind", "other"),
        "schema": top("schema", 2),
        "blocker": top("blockers", [{"code": "missing-compatible-wheel", "chain": "a"}]),
        "extra_top": top("extra", 1),
        "not_in_lock": lambda r: r["selected"].append(
            {**r["selected"][0], "name": "evil", "filename": "evil-1-py3-none-any.whl",
             "url": f"{HOST}/aa/bb/evil-1-py3-none-any.whl"}),
    }
    for key in ("pyproject_sha256", "lock_sha256", "runtime_pin_sha256", "build_inputs_sha256"):
        cases[f"hash_{key}"] = inp(key)
    return cases


@pytest.mark.parametrize("case", sorted(_mutations()))
def test_tampered_report_makes_no_transport_call(env, case):
    report = copy.deepcopy(env.report)
    _mutations()[case](report)
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(report))
    assert env.calls == []
    assert _files(env.house) == []


@pytest.mark.parametrize("bad", [
    "http://files.pythonhosted.org/packages/aa/bb/foo-1.0-py3-none-any.whl",
    "https://evil.example/packages/aa/bb/foo-1.0-py3-none-any.whl",
    "https://user:pw@files.pythonhosted.org/packages/aa/bb/foo-1.0-py3-none-any.whl",
    "https://files.pythonhosted.org:443/packages/aa/bb/foo-1.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/aa/bb/foo-1.0-py3-none-any.whl?x=1",
    "https://files.pythonhosted.org/packages/aa/../bb/foo-1.0-py3-none-any.whl",
    "https://files.pythonhosted.org/other/aa/foo-1.0-py3-none-any.whl",
])
def test_non_canonical_url_in_the_lock_itself_is_refused(env, bad):
    text = env.lock.read_text().replace(f"{HOST}/aa/bb/foo-1.0-py3-none-any.whl", bad)
    env.lock.write_text(text)
    report = copy.deepcopy(env.report)
    report["inputs"]["lock_sha256"] = _sha(env.lock.read_bytes())
    report["selected"][1]["url"] = bad
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(report))
    assert env.calls == []


def test_non_canonical_build_input_url_is_refused(env, monkeypatch):
    inputs = ef.engine_derived_wheel.load_build_inputs()
    data = copy.deepcopy(inputs.data)
    data["build_tools"][0]["url"] = "https://evil.example/packages/setuptools-1.0-py3-none-any.whl"
    monkeypatch.setattr(ef.engine_derived_wheel, "load_build_inputs",
                        lambda: ef.engine_derived_wheel.BuildInputs(data, "b" * 64))
    with pytest.raises(ef.FetchError):
        ef.bootstrap(env.house)
    assert env.calls == []


def test_runtime_url_must_be_the_github_release(env, monkeypatch):
    pin = {"filename": RUNTIME_NAME, "url": "https://evil.example/x.tar.gz", "size": 1, "sha256": "a" * 64}
    monkeypatch.setattr(ef.engine_runtime, "load_runtime_pin", lambda: dict(pin))
    with pytest.raises(ef.FetchError):
        ef.bootstrap(env.house)
    assert env.calls == []


def test_report_with_duplicate_keys_or_constants_is_refused(env):
    path = env.root / "report.json"
    path.write_text('{"schema": 1, "schema": 1}')
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, path)
    path.write_text('{"schema": NaN}')
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, path)
    assert env.calls == []


# ---------------------------------------------- bytes and publication


@pytest.mark.parametrize("mutate", [
    lambda b: b[:-1], lambda b: b + b"x", lambda b: b"X" + b[1:], lambda b: b"",
])
def test_wrong_bytes_are_not_published_and_temp_is_removed(env, mutate):
    for url in list(env.served):
        env.served[url] = mutate(env.served[url])
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(env.report))
    assert _files(env.house) == []


def test_transport_failure_is_a_refusal_without_retry(env, monkeypatch):
    monkeypatch.setattr(ef, "_run", lambda argv, e, t: env.calls.append(argv) or 22)
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(env.report))
    assert len(env.calls) == 1
    assert _files(env.house) == []


def test_transport_timeout_is_a_refusal(env, monkeypatch):
    def boom(argv, e, t):
        raise subprocess.TimeoutExpired(argv, t)

    monkeypatch.setattr(ef, "_run", boom)
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(env.report))
    assert _files(env.house) == []


def test_cache_hit_makes_zero_calls(env):
    report = env.write_report(env.report)
    ef.closure(env.house, report)
    first = len(env.calls)
    ef.closure(env.house, report)
    assert len(env.calls) == first
    ef.bootstrap(env.house)
    after_bootstrap = len(env.calls)
    ef.bootstrap(env.house)
    assert len(env.calls) == after_bootstrap


def test_cache_mismatch_refuses_and_leaves_the_file(env):
    victim = env.house / "foo-1.0-py3-none-any.whl"
    victim.write_bytes(b"tampered")
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(env.report))
    assert victim.read_bytes() == b"tampered"
    assert env.calls == [] or all(not c[0][-1].endswith("foo-1.0-py3-none-any.whl") for c in env.calls)


def test_cache_entry_that_is_a_symlink_is_refused_untouched(env, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_bytes(env.blobs[env.foo["url"]])
    link = env.house / "foo-1.0-py3-none-any.whl"
    link.symlink_to(target)
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(env.report))
    assert link.is_symlink() and target.read_bytes() == env.blobs[env.foo["url"]]


def test_existing_destination_is_never_replaced_by_a_concurrent_writer(env, monkeypatch):
    victim = env.house / "foo-1.0-py3-none-any.whl"
    real_run = ef._run

    def racing(argv, e, t):
        status = real_run(argv, e, t)
        if argv[-1].endswith("foo-1.0-py3-none-any.whl"):
            victim.write_bytes(b"raced")
        return status

    monkeypatch.setattr(ef, "_run", racing)
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(env.report))
    assert victim.read_bytes() == b"raced"
    assert not [n for n in _files(env.house) if n.startswith(".engine-fetch-")]


def test_symlinked_wheelhouse_is_refused(env, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(env.house)
    with pytest.raises(ef.FetchError):
        ef.closure(link, env.write_report(env.report))
    assert env.calls == []


def test_non_0700_wheelhouse_is_refused(env):
    os.chmod(env.house, 0o755)
    with pytest.raises(ef.FetchError):
        ef.closure(env.house, env.write_report(env.report))
    assert env.calls == []


# ---------------------------------------------- load_verified_packaging


@pytest.fixture
def clean_packaging(monkeypatch):
    saved = {n: m for n, m in sys.modules.items() if n == "packaging" or n.startswith("packaging.")}
    for name in saved:
        monkeypatch.delitem(sys.modules, name)
    before = list(sys.path)
    yield
    for name in [n for n in sys.modules if n == "packaging" or n.startswith("packaging.")]:
        del sys.modules[name]
    sys.modules.update(saved)
    assert sys.path == before


def test_load_verified_packaging_imports_from_the_wheel(env, clean_packaging):
    ef.bootstrap(env.house)
    module = ef.load_verified_packaging(env.house)
    assert module.__version__ == "9.9.9"
    assert module.__file__.startswith(str(env.house / "packaging-9.9.9-py3-none-any.whl") + os.sep)


def test_load_verified_packaging_refuses_tampered_wheel(env, clean_packaging):
    ef.bootstrap(env.house)
    wheel = env.house / "packaging-9.9.9-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"\0")
    with pytest.raises(ef.FetchError):
        ef.load_verified_packaging(env.house)
    assert "packaging" not in sys.modules


def test_load_verified_packaging_refuses_missing_wheel(env, clean_packaging):
    with pytest.raises(ef.FetchError):
        ef.load_verified_packaging(env.house)


def test_load_verified_packaging_refuses_preimported_foreign_packaging(env, monkeypatch):
    ef.bootstrap(env.house)
    foreign = type(sys)("packaging")
    foreign.__file__ = "/elsewhere/packaging/__init__.py"
    foreign.__version__ = "9.9.9"
    monkeypatch.setitem(sys.modules, "packaging", foreign)
    with pytest.raises(ef.FetchError):
        ef.load_verified_packaging(env.house)


def test_load_verified_packaging_accepts_preimported_packaging_from_the_same_wheel(env, clean_packaging):
    ef.bootstrap(env.house)
    first = ef.load_verified_packaging(env.house)
    assert ef.load_verified_packaging(env.house) is first
    assert env.calls and len([c for c in env.calls if c[0][-1].endswith("packaging-9.9.9-py3-none-any.whl")]) == 1


def test_load_verified_packaging_refuses_wheel_whose_own_version_differs_from_the_lock(env, clean_packaging):
    pk_url = env.pk["url"]
    bad = _packaging_wheel(env.root / "bad.whl", "9.9.8")
    env.served[pk_url] = bad
    text = env.lock.read_text().replace(env.pk["hash"], f"sha256:{_sha(bad)}").replace(
        f"size = {env.pk['size']}", f"size = {len(bad)}")
    env.lock.write_text(text)
    ef.bootstrap(env.house)
    with pytest.raises(ef.FetchError):
        ef.load_verified_packaging(env.house)


def test_load_verified_packaging_refuses_version_mismatch(env, clean_packaging):
    ef.bootstrap(env.house)
    text = env.lock.read_text().replace('version = "9.9.9"', 'version = "9.9.8"')
    env.lock.write_text(text)
    with pytest.raises(ef.FetchError):
        ef.load_verified_packaging(env.house)


# ------------------------------------------------------------------ CLI


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-I", "-S", str(MODULE_PATH), *args],
        capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.parametrize("args", [
    [],
    ["bootstrap"],
    ["closure", "--wheelhouse", "x"],
    ["bootstrap", "--wheelhouse", "x", "--url", "https://example.com/x"],
    ["bootstrap", "--wheelhouse", "x", "--sha256", "a" * 64],
    ["bootstrap", "--wh", "x"],
    ["bootstrap", "--wheelhouse", "x", "--cache", "y"],
    ["closure", "--wheelhouse", "x", "--audit-report", "y", "--host", "z"],
    ["fetch", "--wheelhouse", "x"],
])
def test_cli_usage_and_override_flags_exit_2_sanitized(args):
    result = _cli(*args)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "artifact fetch failed: usage\n"


def test_cli_failure_exits_1_with_fixed_line(tmp_path):
    result = _cli("bootstrap", "--wheelhouse", str(tmp_path / "does-not-exist"))
    assert result.returncode == 1
    assert result.stderr == "artifact fetch failed\n"
