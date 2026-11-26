"""Every ciao re-exec repeats the interpreter isolation flags the engine runs with."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import cli, dev, supervise
from ciao.main import _restart_watchdog
from ciao.os_support.processes import python_isolation_flags

REPO = Path(__file__).resolve().parents[1]


def _flags(monkeypatch: pytest.MonkeyPatch, isolated: int, no_bytecode: int) -> None:
    monkeypatch.setattr(sys, "flags", SimpleNamespace(isolated=isolated, dont_write_bytecode=no_bytecode))


@pytest.mark.parametrize(
    ("isolated", "no_bytecode", "expected"),
    [(0, 0, []), (0, 1, []), (1, 0, ["-I"]), (1, 1, ["-I", "-B"])],
)
def test_flags_follow_isolated_mode(
    monkeypatch: pytest.MonkeyPatch, isolated: int, no_bytecode: int, expected: list[str]
) -> None:
    _flags(monkeypatch, isolated, no_bytecode)
    assert python_isolation_flags() == expected


def test_every_reexec_site_repeats_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, 1, 1)
    head = [sys.executable, "-I", "-B", "-m", "ciao.cli"]
    assert cli._relaunch_argv()[:5] == head
    assert supervise.default_child_argv()[:5] == head
    assert dev._backend_command()[:5] == head

    execs: list[list[str]] = []

    def fail(_code: int) -> None:  # pragma: no cover
        raise AssertionError("must re-exec")

    monkeypatch.setattr(sys, "argv", ["ciao", "run"])
    _restart_watchdog(
        75,
        supervised=False,
        sleep=lambda s: None,
        execv=lambda exe, argv: execs.append(argv),
        exit_now=fail,
    )
    assert execs[0][1:3] == ["-I", "-B"]


def test_non_isolated_reexec_argv_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, 0, 0)
    assert supervise.default_child_argv() == [sys.executable, "-m", "ciao.cli", "run", "--supervised"]


def test_no_reexec_argv_bypasses_the_helper() -> None:
    pattern = re.compile(r'sys\.executable,\s*"-m",\s*"ciao')
    hits = [
        path.relative_to(REPO).as_posix()
        for path in (REPO / "ciao").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []


# Interpreters that are an installed tool env or a service-manager plist, never the
# bundled engine's own sys.executable, so they cannot inherit its -B.
_FOREIGN_INTERPRETER_FILES = {
    "ciao/engine_update.py",
    "ciao/update_host.py",
}


def _bare_isolated_argvs(source: str) -> list[int]:
    """Line numbers of argv lists/tuples with a literal "-I" and neither "-B" nor a flags helper."""
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        consts = [e.value for e in node.elts if isinstance(e, ast.Constant)]
        if "-I" not in consts or "-B" in consts:
            continue
        helper = any(
            isinstance(e, ast.Starred) and isinstance(e.value, ast.Call) and "flags" in ast.unparse(e.value.func)
            for e in node.elts
        )
        if not helper:
            bad.append(node.lineno)
    return bad


def test_no_interpreter_argv_passes_dash_i_without_dash_b() -> None:
    # -I ignores PYTHONDONTWRITEBYTECODE, so a bare "-I" argv would write __pycache__ into the bundle.
    hits = []
    for path in (REPO / "ciao").rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if rel in _FOREIGN_INTERPRETER_FILES or rel == "ciao/os_support/processes.py":
            continue
        hits += [f"{rel}:{n}" for n in _bare_isolated_argvs(path.read_text(encoding="utf-8"))]
    assert hits == []


def test_bare_dash_i_detector_flags_a_bad_site() -> None:
    assert _bare_isolated_argvs('x = [sys.executable, "-I", "-c", "pass"]\n') == [1]
    assert _bare_isolated_argvs('x = [sys.executable, *isolated_probe_flags(), "-c", "p"]\n') == []
    assert _bare_isolated_argvs('x = [sys.executable, "-I", "-B", "-c", "p"]\n') == []


def test_probe_flags_repeat_dash_b_only_when_bytecode_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support.processes import isolated_probe_flags

    _flags(monkeypatch, 0, 1)
    assert isolated_probe_flags() == ["-I", "-B"]
    _flags(monkeypatch, 0, 0)
    assert isolated_probe_flags() == ["-I"]


def test_isolated_reexec_ignores_a_hostile_environment(tmp_path: Path) -> None:
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "evilmod.py").write_text("X = 1\n")
    user_base = tmp_path / "ub"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "json.py").write_text("raise RuntimeError('cwd module imported')\n")
    cafile = tmp_path / "ca.pem"
    cafile.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    probe = (
        "import json, os, ssl, sys\n"
        "print(json.dumps({'isolated': sys.flags.isolated, 'dont_write_bytecode': sys.flags.dont_write_bytecode,"
        " 'evil_on_path': any(p.startswith(%r) for p in sys.path),"
        " 'user_site': any(p.startswith(%r) for p in sys.path),"
        " 'cafile': ssl.get_default_verify_paths().cafile}))\n" % (str(evil), str(user_base))
    )
    code = (
        "import json, os, subprocess, sys\n"
        "sys.path.insert(0, %r)\n"
        "import ciao.supervise\n"
        "argv = ciao.supervise.default_child_argv()\n"
        "env = {'PYTHONPATH': %r, 'PYTHONUSERBASE': %r, 'SSL_CERT_FILE': %r, 'PATH': os.environ['PATH']}\n"
        "done = subprocess.run([argv[0], *argv[1:3], '-c', %r], env=env, cwd=%r, capture_output=True, text=True)\n"
        "sys.stdout.write(done.stdout)\n"
        "sys.stderr.write(done.stderr)\n"
        "sys.exit(done.returncode)\n" % (str(REPO), str(evil), str(user_base), str(cafile), probe, str(cwd))
    )
    # Plant a user site under the hostile base so a non-isolated child would add it.
    monkey_env = {"PYTHONUSERBASE": str(user_base)}
    user_site = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        env={**monkey_env, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=True,
    ).stdout.strip()
    Path(user_site).mkdir(parents=True)
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, cwd=cwd, timeout=60
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["isolated"] == 1
    assert result["dont_write_bytecode"] == 1
    assert result["evil_on_path"] is False
    assert result["user_site"] is False
    assert result["cafile"] == str(cafile)
