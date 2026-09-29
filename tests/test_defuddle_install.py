"""The managed web reader belongs to the Python environment, not global npm."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ciao import defuddle_install


def test_agent_path_prefers_managed_executable(tmp_path: Path) -> None:
    path = defuddle_install.agent_path("/usr/bin:/bin", tmp_path)
    assert path.split(os.pathsep) == [
        str(tmp_path / "share/ciaobot/defuddle/node_modules/.bin"),
        "/usr/bin",
        "/bin",
    ]


def test_install_uses_pinned_lockfile_and_checks_cli(tmp_path: Path) -> None:
    root = defuddle_install.install_root(tmp_path)
    binary = defuddle_install.binary_dir(tmp_path) / "defuddle"
    commands: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[0] == "/usr/bin/npm":
            binary.parent.mkdir(parents=True)
            binary.touch()
        return subprocess.CompletedProcess(argv, 0)

    with patch.object(defuddle_install, "resolve_tool", return_value="/usr/bin/npm"), \
         patch.object(defuddle_install, "login_shell_path", return_value="/usr/bin:/bin"), \
         patch.object(defuddle_install.subprocess, "run", side_effect=run):
        defuddle_install.install(tmp_path)

    assert '"defuddle": "0.19.3"' in (root / "package.json").read_text()
    assert '"version": "0.19.3"' in (root / "package-lock.json").read_text()
    assert commands == [
        ["/usr/bin/npm", "ci", "--prefix", str(root), "--ignore-scripts", "--no-audit", "--no-fund"],
        [str(binary), "--version"],
    ]


def test_install_requires_npm_before_writing(tmp_path: Path) -> None:
    with patch.object(defuddle_install, "resolve_tool", return_value=None), \
         pytest.raises(RuntimeError, match="Node.js"):
        defuddle_install.install(tmp_path)
    assert not defuddle_install.install_root(tmp_path).exists()
