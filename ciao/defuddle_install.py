"""Install the pinned upstream Defuddle CLI inside the current Python environment."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

from ciao.tool_path import login_shell_path, resolve_tool


def install_root(prefix: Path | None = None) -> Path:
    return (prefix or Path(sys.prefix)) / "share" / "ciaobot" / "defuddle"


def binary_dir(prefix: Path | None = None) -> Path:
    return install_root(prefix) / "node_modules" / ".bin"


def agent_path(path: str | None = None, prefix: Path | None = None) -> str:
    """Give agent subprocesses the private CLI, ahead of any global copy."""
    return os.pathsep.join((str(binary_dir(prefix)), path if path is not None else os.environ.get("PATH", "")))


def install(prefix: Path | None = None) -> None:
    npm = resolve_tool("npm")
    if npm is None:
        raise RuntimeError("npm is required to install Defuddle; install Node.js and retry Ciaobot")
    root = install_root(prefix)
    root.mkdir(parents=True, exist_ok=True)
    source = resources.files("ciao.stock").joinpath("defuddle")
    for filename in ("package.json", "package-lock.json"):
        (root / filename).write_bytes(source.joinpath(filename).read_bytes())
    subprocess.run(
        [npm, "ci", "--prefix", str(root), "--ignore-scripts", "--no-audit", "--no-fund"],
        check=True,
        timeout=180,
        env={**os.environ, "PATH": login_shell_path()},
    )
    binary = binary_dir(prefix) / "defuddle"
    if not binary.is_file():
        raise RuntimeError(f"Defuddle executable missing after installation: {binary}")
    subprocess.run(
        [str(binary), "--version"],
        check=True,
        timeout=15,
        env={**os.environ, "PATH": agent_path(login_shell_path(), prefix=prefix)},
        stdout=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    install()
