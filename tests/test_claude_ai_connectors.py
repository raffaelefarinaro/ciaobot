"""claude.ai connectors are a Claude trim option.

Trimming ``setting:claude_ai_connectors`` sets
``ENABLE_CLAUDEAI_MCP_SERVERS=false`` on a Claude chat. The catalog default
(not trimmed) leaves that variable unset. A memory pass always carries it.
Other providers never see it.

A workspace file that still has ``claude_ai_connectors: false``, and whose
trim map does not already name the option, is read as trimmed. True, a
missing field, and any other value add nothing. The next save drops the old
field. Nothing else in the engine reads it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ciao.claude_trim import CONNECTORS_TRIM_KEY
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from tests.conftest import attach_stub_mcp

ENV_VAR = "ENABLE_CLAUDEAI_MCP_SERVERS"
# A quoted bare field name, or an attribute read. ``setting:claude_ai_connectors``
# is the trim key and is not a read of the retired field.
_FIELD_READ = re.compile(
    r"""(?<!setting:)["']claude_ai_connectors["']|\.claude_ai_connectors\b"""
)


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        }
    )


def _write_registry(tmp_path: Path, entries: list[dict]) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "workspaces.json").write_text(json.dumps(entries), encoding="utf-8")


def _stored(tmp_path: Path, name: str) -> dict:
    rows = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text(encoding="utf-8"))
    return next(row for row in rows if row["name"] == name)


def _manager(tmp_path: Path, *, connectors: bool) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    trim = {} if connectors else {CONNECTORS_TRIM_KEY: True}
    config.workspaces["work"] = WorkspaceConfig(
        name="work", vault_root="memory-vault/work", claude_trim=trim
    )
    manager = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    return attach_stub_mcp(manager)


def _chat(manager: ProjectChatManager, provider: str):
    project = manager.create_project("Notes", workspace="work")
    return manager.create_chat(project.project_id, title="Chat", provider=provider)


def _pass_chat(manager: ProjectChatManager, provider: str):
    """A memory pass in the workspace's Memory project, built as the guardrail tests build it."""
    project = manager._memory_pass.ensure_project("work")
    return manager.create_chat(
        project.project_id,
        title="Memory pass · Pricing rework",
        provider=provider,
        helper={
            "kind": "memory_pass",
            "source_chat_id": "chat-1",
            "archive_policy": "when_clean",
        },
    )


def test_legacy_false_is_read_as_trimmed_and_dropped_on_save(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [
            {
                "name": "work",
                "vault_root": "memory-vault/work",
                "claude_ai_connectors": False,
            }
        ],
    )
    config = _config(tmp_path)
    assert config.workspace("work").claude_trim.get(CONNECTORS_TRIM_KEY) is True

    config.persist_workspace_registry()
    stored = _stored(tmp_path, "work")
    assert "claude_ai_connectors" not in stored
    assert stored["claude_trim"][CONNECTORS_TRIM_KEY] is True


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"claude_ai_connectors": True},
        {"claude_ai_connectors": "false"},
        {"claude_ai_connectors": "true"},
    ],
)
def test_legacy_true_or_absent_converts_to_nothing(tmp_path: Path, extra: dict) -> None:
    _write_registry(
        tmp_path,
        [{"name": "work", "vault_root": "memory-vault/work", **extra}],
    )
    config = _config(tmp_path)
    assert CONNECTORS_TRIM_KEY not in config.workspace("work").claude_trim

    config.persist_workspace_registry()
    stored = _stored(tmp_path, "work")
    assert "claude_ai_connectors" not in stored
    assert CONNECTORS_TRIM_KEY not in stored.get("claude_trim", {})


def test_legacy_false_does_not_override_a_trim_map_that_names_the_option(
    tmp_path: Path,
) -> None:
    _write_registry(
        tmp_path,
        [
            {
                "name": "work",
                "vault_root": "memory-vault/work",
                "claude_ai_connectors": False,
                "claude_trim": {CONNECTORS_TRIM_KEY: False},
            }
        ],
    )
    config = _config(tmp_path)
    # False is the catalog default, so the stored key is kept as "present" and
    # then dropped as sparse. It is not replaced by the legacy false.
    assert config.workspace("work").claude_trim == {}


def test_trimming_connectors_sets_the_env_false(tmp_path: Path) -> None:
    manager = _manager(tmp_path, connectors=False)
    env = manager._build_extra_env(_chat(manager, "claude"))
    assert env[ENV_VAR] == "false"


def test_default_leaves_the_env_unset(tmp_path: Path) -> None:
    manager = _manager(tmp_path, connectors=True)
    env = manager._build_extra_env(_chat(manager, "claude"))
    assert ENV_VAR not in env


def test_memory_pass_always_sets_false_even_when_connectors_are_allowed(tmp_path: Path) -> None:
    manager = _manager(tmp_path, connectors=True)
    env = manager._build_extra_env(_pass_chat(manager, "claude"))
    assert env[ENV_VAR] == "false"


def test_non_claude_chats_never_get_the_variable(tmp_path: Path) -> None:
    manager = _manager(tmp_path, connectors=False)
    assert ENV_VAR not in manager._build_extra_env(_chat(manager, "opencode"))
    assert ENV_VAR not in manager._build_extra_env(_pass_chat(manager, "opencode"))


@pytest.mark.parametrize("provider", ["claude", "opencode"])
def test_chat_env_disables_credential_prompts(tmp_path: Path, provider: str) -> None:
    manager = _manager(tmp_path, connectors=True)
    env = manager._build_extra_env(_chat(manager, provider))
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GH_PROMPT_DISABLED"] == "1"


def test_no_code_path_reads_the_retired_connectors_field() -> None:
    """The stored field is read only by the one-time load conversion."""
    root = Path(__file__).resolve().parents[1]
    conversion = root / "ciao" / "claude_trim.py"
    files = [
        *sorted((root / "ciao").rglob("*.py")),
        *sorted((root / "web" / "src").rglob("*.ts")),
        *sorted((root / "web" / "src").rglob("*.vue")),
    ]
    hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        matches = list(_FIELD_READ.finditer(text))
        if not matches:
            continue
        if path != conversion:
            hits.append(path.relative_to(root).as_posix())
            continue
        start = text.index("def absorb_legacy_claude_ai_connectors")
        rest = text[start + 1 :]
        end_rel = rest.find("\ndef ")
        end = len(text) if end_rel < 0 else start + 1 + end_rel
        assert _FIELD_READ.search(text[start:end]), "the conversion read is not what this scan looks for"
        for match in matches:
            if start <= match.start() < end:
                continue
            # The option's own name in the catalog is not a read of the stored field.
            window = text[max(0, match.start() - 40) : match.start()]
            if "_setting(" in window:
                continue
            hits.append(f"{path.relative_to(root).as_posix()}:{match.start()}")
    assert hits == []
