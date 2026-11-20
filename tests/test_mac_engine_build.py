import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
MODULE_PATH = REPO / "mac/scripts/engine_build.py"
SPEC = importlib.util.spec_from_file_location("engine_build", MODULE_PATH)
assert SPEC and SPEC.loader
eb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eb)
REAL_PLATFORM_CHECK = eb._platform_check
REAL_CHECK_PYTHON = eb.check_python
REAL_RUN = eb._run

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
        self.envs: list[object] = []
        self.status_bytes = status_bytes
        self.failing = failing
        self.extra_c2 = False

    ls_bytes = b"H mac/a\0H mac/b\0"

    def __call__(self, argv, cwd, timeout, env=None):
        self.calls.append(list(argv))
        self.envs.append(env)
        if argv[0] == eb.GIT:
            if "rev-parse" in argv:
                return 0, (HEAD + "\n").encode()
            if "ls-files" in argv:
                return 0, self.ls_bytes
            if "show" in argv:
                return 0, b'[project]\nname = "ciaobot"\nversion = "1.2.3"\n'
            return 0, self.status_bytes
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
    monkeypatch.setattr(eb, "check_python", lambda: None)
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

    def other(argv, cwd, timeout, env=None):
        if argv[0] == eb.GIT and "rev-parse" in argv:
            return 0, (changed + "\n").encode()
        return original(argv, cwd, timeout, env)

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

    def spy(argv, cwd, timeout, env=None):
        if any(a.endswith("engine_assemble.py") for a in argv):
            seen["receipt"] = Path(argv[argv.index("--project-receipt") + 1]).read_bytes()
        return original(argv, cwd, timeout, env)

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


# ----------------------------------------------------------- review fixes


def test_python_allowlist_gate(monkeypatch, capsys):
    monkeypatch.setattr(eb, "_platform_check", lambda: None)
    for version, ok in (((3, 12, 1), True), ((3, 13, 0), True), ((3, 11, 9), False), ((3, 14, 0), False),
                        ((3, 15, 2), False)):
        monkeypatch.setattr(sys, "version_info", (*version, "final", 0))
        if ok:
            eb.check_python()
            continue
        with pytest.raises(eb.PythonVersion):
            eb.check_python()
    monkeypatch.setattr(sys, "version_info", (3, 15, 2, "final", 0))
    assert eb.main(["--cache", "x", "--destination", "y"]) == 1
    assert capsys.readouterr().err.strip() == "engine build needs Python 3.12 or 3.13 (found 3.15)"


def test_python_gate_runs_before_any_io(env, monkeypatch):
    monkeypatch.setattr(eb, "check_python", REAL_CHECK_PYTHON)
    monkeypatch.setattr(sys, "version_info", (3, 15, 2, "final", 0))
    assert _build(env) == 1
    assert env.rec.calls == []
    assert os.listdir(env.cache) == []


@pytest.mark.parametrize("tag", [b"S", b"s", b"h"])
def test_hidden_tracked_changes_are_refused(env, tag, capsys):
    env.rec.ls_bytes = b"H mac/a\0" + tag + b" mac/b\0"
    assert _build(env) == 1
    assert capsys.readouterr().err.strip() == eb.DIRTY_MESSAGE
    assert [c for c in env.rec.calls if c[0] != eb.GIT] == []
    assert not any("rev-parse" in c for c in env.rec.calls)


def test_git_runs_with_a_scrubbed_environment(env, monkeypatch):
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                 "GIT_CONFIG_VALUE_0", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("PATH_KEEP", "1")
    assert _build(env) == 0
    git_envs = [e for c, e in zip(env.rec.calls, env.rec.envs) if c[0] == eb.GIT]
    assert len(git_envs) == 3
    for git_env in git_envs:
        assert git_env is not None and not [k for k in git_env if k.startswith("GIT_")]
        assert git_env["PATH_KEEP"] == "1"
    child_envs = [e for c, e in zip(env.rec.calls, env.rec.envs) if c[0] != eb.GIT]
    assert all(e is None for e in child_envs)


def _fake_engine(tmp_path, content=b"x"):
    engine = tmp_path / "engine"
    (engine / "bin").mkdir(parents=True)
    (engine / "bin/ciao").write_bytes(content)
    return engine


def test_verify_engine_checks_the_inventory(env, tmp_path):
    engine = _fake_engine(tmp_path)
    rows = eb.engine_install.inventory(engine)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"kind": "engine-assembly", "inventory": rows}))
    assert eb.verify_engine(engine, receipt) == "ciao 1.2.3"
    (engine / "bin/ciao").write_bytes(b"tampered")
    with pytest.raises(eb.BuildError):
        eb.verify_engine(engine, receipt)
    (engine / "bin/ciao").write_bytes(b"x")
    (engine / "bin/extra").write_bytes(b"y")
    with pytest.raises(eb.BuildError):
        eb.verify_engine(engine, receipt)


def test_verify_cli_prints_the_committed_version(env, tmp_path, capsys):
    engine = _fake_engine(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"kind": "engine-assembly", "inventory": eb.engine_install.inventory(engine)}))
    assert eb.main(["--verify", str(engine), str(receipt)]) == 0
    assert capsys.readouterr().out.strip() == "ciao 1.2.3"
    receipt.write_text(json.dumps({"kind": "engine-assembly", "inventory": []}))
    assert eb.main(["--verify", str(engine), str(receipt)]) == 1


def test_verify_cannot_be_combined_with_a_build(capsys):
    with pytest.raises(SystemExit) as caught:
        eb.main(["--verify", "a", "b", "--cache", "x", "--destination", "y"])
    assert caught.value.code == 2


def test_failing_child_stderr_tail_is_printed_and_bounded(tmp_path, capsys):
    script = ("import sys; sys.stderr.write('HEAD-MARKER' + 'a' * 20000 + 'TAIL-MARKER'); sys.exit(3)")
    code, out = REAL_RUN([sys.executable, "-I", "-c", script], tmp_path, 30)
    assert code == 3 and out == b""
    err = capsys.readouterr().err
    assert "TAIL-MARKER" in err and "HEAD-MARKER" not in err
    assert len(err) < eb.STDERR_TAIL_BYTES + 400
    code, out = REAL_RUN([sys.executable, "-I", "-c", "print('ok')"], tmp_path, 30)
    assert (code, out) == (0, b"ok\n")
    assert capsys.readouterr().err == ""


def test_timeout_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        REAL_RUN([sys.executable, "-I", "-c", script], tmp_path, 2)
    # killing only the child would leave the grandchild holding the pipe for its full minute
    assert time.monotonic() - started < 20
    pid = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the grandchild outlived the deadline")


def test_timeout_is_a_refusal_and_publishes_nothing(env):
    original = env.rec

    def hang(argv, cwd, timeout, git_env=None):
        if argv[0] != eb.GIT and any(a.endswith("engine_install.py") for a in argv):
            raise subprocess.TimeoutExpired(argv, timeout)
        return original(argv, cwd, timeout, git_env)

    env.monkeypatch.setattr(eb, "_run", hang)
    assert _build(env) == 1
    assert not env.dest.exists()
    assert [n for n in os.listdir(env.parent) if n.startswith(".engine-build-")] == []


def test_deadlines_map_every_step():
    assert {s: eb._deadline(s) for s in eb.STEPS if s != "audit"} == {
        "fetch-bootstrap": 3600, "fetch-closure": 3600, "install": 1800, "export": 3600, "wheel": 1800,
        "assemble": 1800}
    assert eb.DEADLINES["git"] == 120
    env_calls = []

    def seen(argv, cwd, timeout, env=None):
        env_calls.append((Path(argv[0]).name, timeout))
        return 0, b"{}"

    # every child step passes its mapped deadline to the process seam
    with mock.patch.object(eb, "_run", seen):
        for step in ("fetch-bootstrap", "install", "export", "wheel", "assemble"):
            eb._step(step, work=Path("/w"), house=Path("/h"), report=Path("/r"), commit=HEAD, setuptools="s.whl")
    assert [t for _n, t in env_calls] == [3600, 1800, 3600, 1800, 1800]


def test_cleanup_failure_after_publication_is_only_a_warning(env, capsys):
    real = eb.shutil.rmtree

    def flaky(path, *args, **kwargs):
        if Path(path).name.startswith(".engine-build-"):
            raise OSError("busy")
        return real(path, *args, **kwargs)

    env.monkeypatch.setattr(eb.shutil, "rmtree", flaky)
    assert _build(env) == 0
    assert sorted(os.listdir(env.dest)) == ["assembly-receipt.json", "engine"]
    assert "warning" in capsys.readouterr().err
