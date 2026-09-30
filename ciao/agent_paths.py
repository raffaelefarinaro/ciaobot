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
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_CLAUDE_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9]")


def claude_project_slug(workspace: str | os.PathLike[str]) -> str:
    """The folder name Claude Code gives ``workspace`` under ``~/.claude/projects``."""
    return _CLAUDE_SLUG_UNSAFE.sub("-", os.fspath(workspace))


def claude_projects_dir(workspace: str | os.PathLike[str]) -> Path:
    """``~/.claude/projects/<slug>`` for ``workspace``, as Claude Code names it."""
    return Path.home() / ".claude" / "projects" / claude_project_slug(workspace)
