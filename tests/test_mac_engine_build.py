import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
MODULE_PATH = REPO / "mac/scripts/engine_build.py"
SPEC = importlib.util.spec_from_file_location("engine_build", MODULE_PATH)
assert SPEC and SPEC.loader
eb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eb)
REAL_PLATFORM_CHECK = eb._platform_check

HEAD = "a" * 40
HASHES = {"pyproject_sha256": "1" * 64, "lock_sha256": "2" * 64, "runtime_pin_sha256": "3" * 64,
          "build_inputs_sha256": "4" * 64}
SETUPTOOLS = "setuptools-84.2.1-py3-none-any.whl"
WHEEL_STDOUT = b'{ "receipt" :   1 }\n'
CHILD_STEPS = ("fetch-bootstrap", "fetch-closure", "install", "export", "wheel", "assemble")
REPORT = {"schema": 1, "kind": "lock-wheel-audit", "selected": [{"name": "x"}], "blockers": []}


def _inputs(hashes=None):
    return SimpleNamespace(
        hashes=dict(hashes or HASHES),
        build_inputs=SimpleNamespace(data={"build_tools": [{"name": "setuptools", "filename": SETUPTOOLS}]}),
    )


class Recorder:
    """Fake process seam: creates each child's destination like the real helper."""

    def __init__(self, status_bytes=b"", failing=None):
        self.calls: list[list[str]] = []
        self.status_bytes = status_bytes
        self.failing = failing
        self.extra_c2 = False

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        if argv[0] == eb.GIT:
            return (0, (HEAD + "\n").encode()) if "rev-parse" in argv else (0, self.status_bytes)
        flags = argv[argv.index("--destination") + 1] if "--destination" in argv else None
        step = self._step(argv)
        if step == self.failing:
            return 1, b""
        if step == "install":
            Path(flags).mkdir()
        elif step == "export":
            Path(flags).mkdir()
        elif step == "wheel":
            Path(flags).mkdir()
            return 0, WHEEL_STDOUT
        elif step == "assemble":
            (Path(flags) / "engine/bin").mkdir(parents=True)
            (Path(flags) / "engine/bin/ciao").write_text("x")
            (Path(flags) / "assembly-receipt.json").write_text("{}")
            if self.extra_c2:
                (Path(flags) / "extra").write_text("x")
        return 0, b"{}"

    @staticmethod
    def _step(argv):
        at = argv.index("-S") + 1 if "-S" in argv else 2
        script, rest = Path(argv[at]).name, argv[at + 1:]
        if script == "engine_fetch.py":
            return "fetch-" + rest[0]
        if script == "engine_install.py":
            return "install"
        if script == "engine_assemble.py":
            return "assemble"
        return "export" if rest[0] == "export" else "wheel"

    def steps(self):
        return [self._step(c) for c in self.calls if c[0] != eb.GIT]


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    os.chmod(cache, 0o700)
    os.chmod(parent, 0o700)
    recorder = Recorder()
    audit_calls: list[object] = []
    monkeypatch.setattr(eb, "_platform_check", lambda: None)
    monkeypatch.setattr(eb.engine_fetch, "_Inputs", lambda: _inputs())
    monkeypatch.setattr(eb, "_run", recorder)

    def load(house):
        audit_calls.append(("load", Path(house)))

    def audit(project, lock):
        audit_calls.append(("audit", Path(project), Path(lock)))
        return json.loads(json.dumps(REPORT))

    monkeypatch.setattr(eb.engine_fetch, "load_verified_packaging", load)
    monkeypatch.setattr(eb.engine_dependencies, "audit", audit)
    monkeypatch.setattr(eb.importlib, "import_module", lambda name: None)
    return SimpleNamespace(cache=cache, parent=parent, dest=parent / "engine-out", rec=recorder,
                           audit=audit_calls, monkeypatch=monkeypatch, tmp=tmp_path)


def _build(env, capsys=None):
    return eb.main(["--cache", str(env.cache), "--destination", str(env.dest)])


def _house(env):
    return env.cache / eb.cache_key(HASHES)


def test_steps_run_in_order_with_exact_argv(env):
    assert _build(env) == 0
    py, s = sys.executable, eb.SCRIPT_DIR
    house = str(_house(env))
    # the work directory is a random mkdtemp under the destination parent
    work = Path(env.rec.calls[-1][env.rec.calls[-1].index("--install") + 1]).parent
    report = str(work / "audit-report.json")
    expected = [
        [py, "-I", "-S", str(s / "engine_fetch.py"), "bootstrap", "--wheelhouse", house],
        [py, "-I", "-S", str(s / "engine_fetch.py"), "closure", "--wheelhouse", house, "--audit-report", report],
        [py, "-I", "-S", str(s / "engine_install.py"), "--wheelhouse", house, "--audit-report", report,
         "--destination", str(work / "b3b")],
        [py, "-I", str(s / "engine_project_wheel.py"), "export", "--commit", HEAD,
         "--destination", str(work / "export")],
        [py, "-I", str(s / "engine_project_wheel.py"), "build", "--source", str(work / "export"),
         "--backend-wheel", str(_house(env) / SETUPTOOLS), "--destination", str(work / "wheel")],
        [py, "-I", "-S", str(s / "engine_assemble.py"), "--wheelhouse", house, "--install", str(work / "b3b"),
         "--source", str(work / "export"), "--project-build", str(work / "wheel"),
         "--project-receipt", str(work / "wheel-receipt.json"), "--destination", str(work / "c2")],
    ]
    assert [c for c in env.rec.calls if c[0] != eb.GIT] == expected


def test_progress_lines_are_exact(env, capsys):
    assert _build(env) == 0
    assert capsys.readouterr().out.splitlines() == [f"engine build: {s}" for s in eb.STEPS]


def test_dirty_tree_is_refused_before_any_child(env, capsys):
    env.rec.status_bytes = b" M mac/x\0"
    assert _build(env) == 1
    assert capsys.readouterr().err.strip() == eb.DIRTY_MESSAGE
    assert all(c[0] == eb.GIT for c in env.rec.calls)
    assert len(env.rec.calls) == 1
    assert os.listdir(env.cache) == []
    assert not env.dest.exists()


def test_export_uses_head(env):
    _build(env)
    export = next(c for c in env.rec.calls if "export" in c and "--commit" in c)
    assert export[export.index("--commit") + 1] == HEAD
    env.rec.calls.clear()
    env.dest = env.parent / "second"
    changed = "b" * 40
    original = env.rec.__call__

    def other(argv, cwd, timeout):
        if argv[0] == eb.GIT and "rev-parse" in argv:
            return 0, (changed + "\n").encode()
        return original(argv, cwd, timeout)

    env.monkeypatch.setattr(eb, "_run", other)
    _build(env)
    export = next(c for c in env.rec.calls if "export" in c and "--commit" in c)
    assert export[export.index("--commit") + 1] == changed


def test_wheelhouse_is_keyed_by_the_four_input_hashes(env):
    house = eb.prepare_wheelhouse(env.cache, eb.cache_key(HASHES))
    assert house == _house(env)
    assert oct(os.stat(house).st_mode & 0o777) == "0o700"
    keys = {eb.cache_key(HASHES)}
    for name in HASHES:
        changed = dict(HASHES, **{name: "9" * 64})
        keys.add(eb.cache_key(changed))
    assert len(keys) == 5


def test_stale_keys_are_pruned_and_current_is_kept(env):
    current = eb.cache_key(HASHES)
    (env.cache / current).mkdir(mode=0o700)
    (env.cache / current / "sentinel").write_text("keep")
    stale = env.cache / ("5" * 64)
    stale.mkdir(mode=0o700)
    (stale / "old.whl").write_text("old")
    eb.prepare_wheelhouse(env.cache, current)
    assert sorted(os.listdir(env.cache)) == [current]
    assert (env.cache / current / "sentinel").read_text() == "keep"


@pytest.mark.parametrize("kind", ["file", "symlink", "non-hex", "mode-0755"])
def test_unexpected_cache_entries_are_refused(env, kind):
    stale = env.cache / ("5" * 64)
    stale.mkdir(mode=0o700)
    if kind == "file":
        (env.cache / ("6" * 64)).write_text("x")
    elif kind == "symlink":
        (env.cache / ("6" * 64)).symlink_to(env.tmp)
    elif kind == "non-hex":
        (env.cache / "notes").mkdir(mode=0o700)
    else:
        (env.cache / ("6" * 64)).mkdir()
        os.chmod(env.cache / ("6" * 64), 0o755)
    assert _build(env) == 1
    assert stale.exists()
    assert not env.dest.exists()
    assert env.rec.steps() == []


def test_audit_report_uses_only_the_verified_packaging_wheel(env):
    _build(env)
    assert [e[0] for e in env.audit] == ["load", "audit"]
    assert env.audit[0][1] == _house(env)
    assert env.audit[1][1:] == (eb.REPO_ROOT / "pyproject.toml", eb.REPO_ROOT / "uv.lock")


def test_audit_report_bytes(env):
    house = _house(env)
    house.mkdir(mode=0o700)
    work = env.parent
    path = eb.write_audit_report(house, work)
    assert path.read_text() == json.dumps(REPORT, sort_keys=True, indent=2) + "\n"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_audit_blockers_stop_before_closure(env):
    blocked = dict(REPORT, blockers=[{"code": "missing-compatible-wheel", "chain": "x"}])
    env.monkeypatch.setattr(eb.engine_dependencies, "audit", lambda p, l: blocked)
    assert _build(env) == 1
    assert env.rec.steps() == ["fetch-bootstrap"]
    assert not env.dest.exists()


def test_wheel_receipt_is_the_build_stdout(env):
    seen = {}
    original = env.rec.__call__

    def spy(argv, cwd, timeout):
        if any(a.endswith("engine_assemble.py") for a in argv):
            seen["receipt"] = Path(argv[argv.index("--project-receipt") + 1]).read_bytes()
        return original(argv, cwd, timeout)

    env.monkeypatch.setattr(eb, "_run", spy)
    assert _build(env) == 0
    assert seen["receipt"] == WHEEL_STDOUT


@pytest.mark.parametrize("failing", CHILD_STEPS)
def test_any_failing_step_publishes_nothing(env, failing):
    env.rec.failing = failing
    assert _build(env) == 1
    assert not env.dest.exists()
    assert [n for n in os.listdir(env.parent) if n.startswith(".engine-build-")] == []
    assert _house(env).is_dir()


def test_publication_is_exactly_the_c2_layout(env):
    assert _build(env) == 0
    assert sorted(os.listdir(env.dest)) == ["assembly-receipt.json", "engine"]
    assert os.listdir(env.parent) == ["engine-out"]


def test_extra_c2_entry_is_refused(env):
    env.rec.extra_c2 = True
    assert _build(env) == 1
    assert not env.dest.exists()
    assert os.listdir(env.parent) == []


@pytest.mark.parametrize("case", ["exists", "dot", "parent-0755", "parent-symlink"])
def test_destination_rules(env, case):
    dest = env.dest
    if case == "exists":
        dest.mkdir()
    elif case == "dot":
        dest = env.parent / ".." / "out" / "x"
    elif case == "parent-0755":
        os.chmod(env.parent, 0o755)
    else:
        link = env.tmp / "link"
        link.symlink_to(env.parent)
        dest = link / "x"
    assert eb.main(["--cache", str(env.cache), "--destination", str(dest)]) == 1
    assert env.rec.calls == []


def test_platform_refused_before_io(env, monkeypatch):
    monkeypatch.setattr(eb, "_platform_check", REAL_PLATFORM_CHECK)
    monkeypatch.setattr(sys, "platform", "linux")
    assert _build(env) == 1
    assert env.rec.calls == []
    assert os.listdir(env.cache) == []


@pytest.mark.parametrize("argv", [["--destination", "x"], ["--cache", "x", "--dest", "y"],
                                  ["--cache", "x", "--destination", "y", "--skip-engine"]])
def test_cli_usage_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        eb.main(argv)
    assert caught.value.code == 2
    assert capsys.readouterr().err.strip() == "engine build failed: usage"


def test_cli_refusal_exit_1_fixed_line(env, capsys):
    env.rec.failing = "install"
    assert _build(env) == 1
    assert capsys.readouterr().err.strip() == "engine build failed"


def test_module_never_imports_in_process_builders():
    text = MODULE_PATH.read_text()
    for banned in ("build_derived_wheel", "build_project_wheel", "_run_backend"):
        assert banned not in text
