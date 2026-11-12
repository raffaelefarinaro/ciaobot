"""Where the provider CLIs keep their per-workspace state (D-03, #696).

One module answers "where does Claude Code / OpenCode keep X for this
workspace", from what the tools were observed to do rather than from docs
(D-04). The recorded observations live in
``tests/fixtures/agent_discovery/`` and ``tests/test_agent_paths.py`` checks
this module against them.

Claude Code keeps a workspace's sessions under
``~/.claude/projects/<slug>/``, where the slug is the workspace path with every
character that is not an ASCII letter or digit replaced by ``-``. One rule on
every OS: ``C:\\dev\\ciaobot`` is ``C--dev-ciaobot``, ``/Users/me/ciao`` is
``-Users-me-ciao``, and a ``.``, ``_``, space or non-ASCII letter becomes ``-``
on macOS too (``/Users/me/.archon/x`` is ``-Users-me--archon-x``).

A slug longer than 200 characters is cut to its first 200 and given a suffix,
``-`` and the base-36 absolute value of the path's Java ``String.hashCode``
(``h = 31*h + c`` over UTF-16 code units, 32-bit): Claude Code 2.1.286 kept a
200-character slug whole and shortened a 201-character one this way.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_CLAUDE_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9]")
_CLAUDE_SLUG_MAX = 200
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _java_string_hash(text: str) -> int:
    """Java's ``String.hashCode``: ``h = 31*h + c`` over UTF-16 code units, as an int32."""
    units = text.encode("utf-16-le")
    value = 0
    for index in range(0, len(units), 2):
        value = (value * 31 + int.from_bytes(units[index:index + 2], "little")) & 0xFFFFFFFF
    return value - (1 << 32) if value & 0x80000000 else value


def _base36(value: int) -> str:
    digits = ""
    while True:
        value, digit = divmod(value, 36)
        digits = _BASE36[digit] + digits
        if not value:
            return digits


def claude_project_slug(workspace: str | os.PathLike[str]) -> str:
    """The folder name Claude Code gives ``workspace`` under ``~/.claude/projects``."""
    path = os.fspath(workspace)
    slug = _CLAUDE_SLUG_UNSAFE.sub("-", path)
    if len(slug) <= _CLAUDE_SLUG_MAX:
        return slug
    return f"{slug[:_CLAUDE_SLUG_MAX]}-{_base36(abs(_java_string_hash(path)))}"


def claude_projects_dir(workspace: str | os.PathLike[str]) -> Path:
    """``~/.claude/projects/<slug>`` for ``workspace``, as Claude Code names it."""
    return Path.home() / ".claude" / "projects" / claude_project_slug(workspace)


def claude_config_project_key(cwd: str | os.PathLike[str]) -> str:
    """The ``projects`` key of Claude Code's global config for a CLI started in ``cwd``.

    Claude Code keys the per-project entry (``disabledMcpServers``, trust, local MCP servers) by
    the git root of its cwd (a ``.git`` directory or file), else the resolved cwd, with ``/``
    separators on Windows. Observed 2026-11-09: an entry under ``repo`` applied to a CLI started
    in ``repo/ws``; the ``repo/ws`` entry was ignored.
    """
    resolved = Path(cwd).resolve()
    root = next((c for c in (resolved, *resolved.parents) if (c / ".git").exists()), resolved)
    key = str(root)
    return key.replace("\\", "/") if os.name == "nt" else key


def claude_global_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Claude Code's global config file: ``CLAUDE_CONFIG_PATH``, then ``CLAUDE_CONFIG_DIR``, then home."""
    src = os.environ if env is None else env
    path = str(src.get("CLAUDE_CONFIG_PATH", "")).strip()
    if path:
        return Path(path).expanduser()
    config_dir = str(src.get("CLAUDE_CONFIG_DIR", "")).strip()
    if config_dir:
        return Path(config_dir).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"
