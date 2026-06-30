"""Smoke tests for `scripts/memory-cli.py`.

The CLI is a thin passthrough to `ciao.memory_tool`, which already has its
own deep tests. We only cover the wiring (env-var resolution, JSON output,
exit codes, action plumbing) so a Pi subagent invoking the script can rely
on its contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scripts" / "memory-cli.py"


def _run(args: list[str], memory_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CIAO_MEMORY_DIR": str(memory_dir)}
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_read_empty_memory_returns_ok_json(tmp_path: Path) -> None:
    result = _run(["read", "--target", "memory"], tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["entries"] == []
    assert payload["used_chars"] == 0
    assert payload["char_limit"] == 2200


def test_add_then_read_round_trip(tmp_path: Path) -> None:
    add = _run(["add", "--target", "memory", "--text", "Test entry one."], tmp_path)
    assert add.returncode == 0, add.stderr
    add_payload = json.loads(add.stdout)
    assert add_payload["ok"] is True
    assert add_payload["added"] == "Test entry one."

    read = _run(["read", "--target", "memory"], tmp_path)
    payload = json.loads(read.stdout)
    assert payload["entries"] == ["Test entry one."]


def test_replace_substring_match(tmp_path: Path) -> None:
    _run(["add", "--target", "memory", "--text", "Old entry to replace."], tmp_path)
    res = _run(
        ["replace", "--target", "memory", "--old", "Old entry", "--new", "New entry."],
        tmp_path,
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["ok"] is True
    assert payload["with"] == "New entry."


def test_remove_substring_match(tmp_path: Path) -> None:
    _run(["add", "--target", "memory", "--text", "Will be removed."], tmp_path)
    res = _run(["remove", "--target", "memory", "--text", "be removed"], tmp_path)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["ok"] is True
    read = _run(["read", "--target", "memory"], tmp_path)
    assert json.loads(read.stdout)["entries"] == []


def test_user_target_uses_different_file_and_limit(tmp_path: Path) -> None:
    res = _run(["read", "--target", "user"], tmp_path)
    payload = json.loads(res.stdout)
    assert payload["char_limit"] == 1375
    assert (tmp_path / "user.md").exists() or payload["entries"] == []


def test_failure_returns_exit_code_1(tmp_path: Path) -> None:
    # Removing from empty memory should fail with ok=false and exit code 1.
    res = _run(["remove", "--target", "memory", "--text", "nothing"], tmp_path)
    assert res.returncode == 1
    payload = json.loads(res.stdout)
    assert payload["ok"] is False
    assert "no entry matches" in payload["error"]


def test_unknown_target_rejected(tmp_path: Path) -> None:
    res = _run(["read", "--target", "bogus"], tmp_path)
    # argparse rejects bogus choices before we hit the resolver, so exit
    # code is 2 (argparse's "command line usage error") and the message
    # goes to stderr.
    assert res.returncode == 2
    assert "invalid choice" in result_stderr(res)


def result_stderr(res: subprocess.CompletedProcess[str]) -> str:
    return res.stderr or ""


def test_plain_output_format(tmp_path: Path) -> None:
    _run(["add", "--target", "memory", "--text", "Plain mode entry."], tmp_path)
    res = _run(["--plain", "read", "--target", "memory"], tmp_path)
    assert res.returncode == 0, res.stderr
    assert "Plain mode entry." in res.stdout
    assert "§" in res.stdout
