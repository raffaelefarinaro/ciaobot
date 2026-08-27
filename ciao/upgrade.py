"""CLI upgrade helpers."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UpgradeResult:
    """One upgrade command result."""

    command: list[str]
    changed: bool
    success: bool
    stdout: str
    stderr: str
    before_version: str
    after_version: str


def _extract_version(text: str) -> str:
    """Extract a concise version from CLI output.

    Handles multi-line ``pip show`` output by pulling the ``Version:`` field,
    and falls back to the full (stripped) text for single-line outputs like
    ``claude --version``.
    """
    for line in text.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return text.strip()


async def read_version(command: list[str]) -> str:
    """Read a CLI version string."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        return _extract_version(stdout.decode(errors="replace"))
    except FileNotFoundError:
        return ""


async def run_upgrade(
    install_command: list[str],
    version_command: list[str],
) -> UpgradeResult:
    """Run one CLI upgrade and infer whether it changed."""
    before = await read_version(version_command)
    try:
        process = await asyncio.create_subprocess_exec(
            *install_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except FileNotFoundError:
        return UpgradeResult(
            command=install_command, changed=False, success=False,
            stdout="", stderr="command not found", before_version=before, after_version=before,
        )
    after = await read_version(version_command)
    success = process.returncode == 0
    stdout_text = stdout.decode(errors="replace")
    changed = success and before != after
    return UpgradeResult(
        command=install_command,
        changed=changed,
        success=success,
        stdout=stdout_text,
        stderr=stderr.decode(errors="replace"),
        before_version=before,
        after_version=after,
    )


async def upgrade_libreoffice() -> UpgradeResult:
    """Install LibreOffice via Homebrew Cask on macOS."""
    brew_bin = shutil.which("brew")
    if brew_bin is None:
        return UpgradeResult(
            command=["brew", "install", "--cask", "libreoffice"],
            changed=False, success=False,
            stdout="", stderr="brew not found (Homebrew is required for LibreOffice on macOS)",
            before_version="", after_version="",
        )
    
    from pathlib import Path
    libreoffice_installed = False
    for cmd in ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if shutil.which(cmd) or Path(cmd).exists():
            libreoffice_installed = True
            break

    if libreoffice_installed:
        return UpgradeResult(
            command=["brew", "install", "--cask", "libreoffice"],
            changed=False, success=True,
            stdout="LibreOffice already installed", stderr="",
            before_version="installed", after_version="installed",
        )

    soffice_path = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return await run_upgrade(
        install_command=[brew_bin, "install", "--cask", "libreoffice"],
        version_command=[soffice_path, "--version"],
    )


def install_custom_skills(cwd: str, workspace_name: str | None = None) -> int:
    """Install and mirror Ciaobot skills through the packaged sync command.

    Returns the number of skills installed.
    """
    try:
        from ciao.sync_skills import sync_workspace_skills

        result = sync_workspace_skills(cwd, workspace_name=workspace_name)
        return result.custom_installed
    except Exception:
        logger.exception("Custom skills install failed")
        return 0


def update_skills(cwd: str, workspace_name: str | None = None) -> str | None:
    """Install the curated skill set from ``skills/``.

    The package command mirrors skills, commands, and agents into the Claude
    catalog. Skills are local folders under ``skills/<name>/SKILL.md``.
    ``workspace_name`` lets the caller tie a root to its registered workspace so
    the ``gws-*`` stock skills are gated on that workspace's Google account.
    """
    n_custom = install_custom_skills(cwd, workspace_name=workspace_name)
    if n_custom:
        logger.info("Installed %d custom skill(s).", n_custom)
    return None
