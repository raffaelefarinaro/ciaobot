"""Skills that ship with Claude Code, recorded from a chat's init payload.

Shapes copied from Claude Code 2.1.289: the init ``skills`` list mixes user,
plugin, synced and bundled skills, and the initialize response's ``commands``
flags bundled entries with ``builtin`` but also lists slash commands.
"""

from __future__ import annotations

import pytest

from ciao import setup_status
from ciao.providers.claude import ClaudeProvider


class _Client:
    def __init__(self, info: object = None, error: Exception | None = None) -> None:
        self._info = info
        self._error = error

    async def get_server_info(self) -> object:
        if self._error is not None:
            raise self._error
        return self._info


INFO = {
    "commands": [
        {"name": "impeccable", "description": "Design. (user)", "argumentHint": ""},
        {"name": "code-review", "description": "Review", "builtin": True, "aliases": []},
        {"name": "dataviz", "description": "Charts", "builtin": True},
        {"name": "clear", "description": "Clear", "builtin": True},
        {"name": "docx", "description": "Word (claude.ai sync)", "aliases": []},
        {"name": "skill-creator:skill-creator", "description": "Skills", "aliases": []},
    ]
}
INIT = {
    "skills": [
        "impeccable",
        "skill-creator:skill-creator",
        "dataviz",
        "code-review",
        "anthropic-skills:docx",
        "ciao-memory",
    ]
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(setup_status, "_claude_bundled_skills", None)


@pytest.mark.asyncio
async def test_records_only_skills_the_cli_flags_as_builtin():
    await ClaudeProvider._record_bundled_skills(_Client(INFO), INIT)

    # /clear is built in but a command, not a skill; impeccable, the synced
    # docx, the plugin and the workspace skill are not built in.
    assert setup_status.claude_bundled_skills() == ["code-review", "dataviz"]


@pytest.mark.asyncio
async def test_leaves_bundled_skills_unknown_when_the_cli_cannot_say():
    await ClaudeProvider._record_bundled_skills(_Client(error=RuntimeError("gone")), INIT)
    await ClaudeProvider._record_bundled_skills(_Client(None), INIT)
    await ClaudeProvider._record_bundled_skills(_Client(INFO), {})
    # A CLI that predates the `builtin` flag on commands.
    unflagged = {
        "commands": [
            {k: v for k, v in c.items() if k != "builtin"} for c in INFO["commands"]
        ]
    }
    await ClaudeProvider._record_bundled_skills(_Client(unflagged), INIT)

    assert setup_status.claude_bundled_skills() is None
