"""CLI upgrade helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
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


async def upgrade_project_deps(project_root: str) -> dict[str, tuple[str, str]]:
    """Upgrade project deps via ``pip install -e '.[test]'`` and notebooklm-py.

    Returns a dict of ``{package: (before, after)}`` for packages whose version changed.
    """
    # Packages we care about tracking individually
    tracked = ["openai", "claude-agent-sdk", "notebooklm-py", "apfel"]

    async def _get_versions() -> dict[str, str]:
        versions: dict[str, str] = {}
        for pkg in tracked:
            ver = await read_version([sys.executable, "-m", "pip", "show", pkg])
            if ver:
                versions[pkg] = ver
        return versions

    before = await _get_versions()

    # Install project dependencies in accordance with pyproject.toml pins.
    # We do not use --upgrade or install untracked explicit packages,
    # because they are strictly pinned in pyproject.toml now.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-e", f"{project_root}[test,voice-local]",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    after = await _get_versions()

    changed: dict[str, tuple[str, str]] = {}
    for pkg in tracked:
        b = before.get(pkg, "?")
        a = after.get(pkg, "?")
        if b != a:
            changed[pkg] = (b, a)
    return changed


async def upgrade_gws() -> UpgradeResult:
    """Upgrade the gws CLI via npm."""
    if shutil.which("npm") is None:
        return UpgradeResult(
            command=["npm", "install", "-g", "@googleworkspace/cli@latest"],
            changed=False, success=False,
            stdout="", stderr="npm not found",
            before_version="", after_version="",
        )
    return await run_upgrade(
        install_command=["npm", "install", "-g", "@googleworkspace/cli@latest"],
        version_command=["gws", "--version"],
    )


async def upgrade_defuddle() -> UpgradeResult:
    """Upgrade the defuddle CLI via npm."""
    if shutil.which("npm") is None:
        return UpgradeResult(
            command=["npm", "install", "-g", "defuddle"],
            changed=False, success=False,
            stdout="", stderr="npm not found",
            before_version="", after_version="",
        )
    return await run_upgrade(
        install_command=["npm", "install", "-g", "defuddle"],
        version_command=["defuddle", "--version"],
    )


async def upgrade_claude_code() -> UpgradeResult:
    """Upgrade the Claude Code CLI.

    Since we use the bundled CLI in the SDK, its upgrade is managed via pip
    (upgrading ``claude-agent-sdk``).
    """
    from ciao.providers.claude import get_bundled_claude_path

    bundled_bin = get_bundled_claude_path()
    if bundled_bin is None:
        return UpgradeResult(
            command=[],
            changed=False, success=False,
            stdout="", stderr="bundled claude not found",
            before_version="", after_version="",
        )

    # Managed via pip (claude-agent-sdk). Just report current version.
    ver = await read_version([bundled_bin, "--version"])
    return UpgradeResult(
        command=[],
        changed=False,
        success=True,
        stdout=f"Using bundled Claude Code: {ver}",
        stderr="",
        before_version=ver,
        after_version=ver,
    )


async def upgrade_codex() -> UpgradeResult:
    """Upgrade the installed Codex CLI through its native updater."""
    from ciao.providers.codex import resolve_codex_binary

    binary = resolve_codex_binary()
    if not binary:
        return UpgradeResult(
            command=["codex", "update"],
            changed=False,
            success=False,
            stdout="",
            stderr="codex not found",
            before_version="",
            after_version="",
        )
    if ".app/Contents/Resources/" in binary:
        version = await read_version([binary, "--version"])
        return UpgradeResult(
            command=[],
            changed=False,
            success=True,
            stdout=(
                f"Using Codex bundled with the desktop app: {version}. "
                "Update the desktop app to update Codex."
            ),
            stderr="",
            before_version=version,
            after_version=version,
        )
    return await run_upgrade(
        install_command=[binary, "update"],
        version_command=[binary, "--version"],
    )




async def upgrade_root_npm(project_root: str) -> UpgradeResult:
    """Update root npm packages via npm update."""
    if shutil.which("npm") is None:
        return UpgradeResult(
            command=["npm", "update", "--prefix", project_root, "--no-audit", "--no-fund"],
            changed=False, success=False,
            stdout="", stderr="npm not found",
            before_version="", after_version="",
        )
    return await run_upgrade(
        install_command=["npm", "update", "--prefix", project_root, "--no-audit", "--no-fund"],
        version_command=["npm", "--version"],
    )


async def upgrade_web_npm(project_root: str) -> UpgradeResult:
    """Update web frontend npm packages via npm update."""
    web_dir = os.path.join(project_root, "web")
    if shutil.which("npm") is None:
        return UpgradeResult(
            command=["npm", "update", "--prefix", web_dir, "--no-audit", "--no-fund"],
            changed=False, success=False,
            stdout="", stderr="npm not found",
            before_version="", after_version="",
        )
    return await run_upgrade(
        install_command=["npm", "update", "--prefix", web_dir, "--no-audit", "--no-fund"],
        version_command=["npm", "--version"],
    )


async def upgrade_apfel() -> UpgradeResult:
    """Install or upgrade apfel via Homebrew on macOS."""
    brew_bin = shutil.which("brew")
    if brew_bin is None:
        return UpgradeResult(
            command=["brew", "install", "apfel"],
            changed=False, success=False,
            stdout="", stderr="brew not found (Homebrew is required for apfel on macOS)",
            before_version="", after_version="",
        )
    apfel_installed = shutil.which("apfel") is not None
    install_command = [brew_bin, "upgrade", "apfel"] if apfel_installed else [brew_bin, "install", "apfel"]
    return await run_upgrade(
        install_command=install_command,
        version_command=["apfel", "--version"],
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


async def upgrade_all(project_root: str) -> str | None:
    """Run all upgrades (pip deps + root npm + web npm + gws + defuddle
    + claude + codex). Returns a summary or *None*."""
    parts: list[str] = []

    # All upgrades in parallel.
    pip_task = asyncio.create_task(upgrade_project_deps(project_root))
    gws_task = asyncio.create_task(upgrade_gws())
    defuddle_task = asyncio.create_task(upgrade_defuddle())
    claude_task = asyncio.create_task(upgrade_claude_code())
    codex_task = asyncio.create_task(upgrade_codex())
    root_npm_task = asyncio.create_task(upgrade_root_npm(project_root))
    web_npm_task = asyncio.create_task(upgrade_web_npm(project_root))
    apfel_task = asyncio.create_task(upgrade_apfel())
    libreoffice_task = asyncio.create_task(upgrade_libreoffice())
    (
        pip_changed, gws_result, defuddle_result,
        claude_result, codex_result, root_npm_result, web_npm_result,
        apfel_result, libreoffice_result,
    ) = await asyncio.gather(
        pip_task, gws_task, defuddle_task, claude_task, codex_task,
        root_npm_task, web_npm_task, apfel_task, libreoffice_task,
    )

    # Failures get logged + surfaced in the summary even when nothing changed,
    # so silent EACCES (e.g. npm-global writes without a prior sudo seed) don't
    # disappear into the void on every redeploy.
    named_results = [
        ("gws", gws_result),
        ("defuddle", defuddle_result),
        ("claude", claude_result),
        ("codex", codex_result),
        ("root-npm", root_npm_result),
        ("web-npm", web_npm_result),
        ("apfel", apfel_result),
        ("libreoffice", libreoffice_result),
    ]

    for pkg, (before, after) in pip_changed.items():
        parts.append(f"{pkg}: {before} -> {after}")

    for name, result in named_results:
        if result.changed:
            parts.append(f"{name}: {result.before_version} -> {result.after_version}")
        elif not result.success:
            tail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "error"
            logger.warning("%s upgrade failed: %s", name, tail)
            parts.append(f"{name}: install failed ({tail})")

    if not parts:
        logger.info("Upgrades: everything already up to date.")
        return None

    summary = "Upgrades: " + ", ".join(parts)
    logger.info(summary)
    return summary


def install_custom_skills(cwd: str) -> int:
    """Install and mirror Ciaobot skills through the packaged sync command.

    Returns the number of skills installed.
    """
    try:
        from ciao.sync_skills import sync_workspace_skills

        result = sync_workspace_skills(cwd)
        return result.custom_installed
    except Exception:
        logger.exception("Custom skills install failed")
        return 0


def update_skills(cwd: str) -> str | None:
    """Install the curated skill set from ``skills-lock.json`` + ``skills/``.

    The package command performs upstream refresh when possible, then mirrors
    skills, commands, and agents into the Claude catalog.
    """
    n_custom = install_custom_skills(cwd)
    if n_custom:
        logger.info("Installed %d custom skill(s).", n_custom)
    return None
