"""Tests for the insights sandbox helpers (``scripts/insights-sandbox/sandbox.py``).

The harness lets a full agent really write, so the only thing standing between
a pilot run and the live workspace is this module. The tests are therefore
mostly about refusals: a path check that passes is a claim about the operator's
real files, and a ``prepare_clone`` step that silently no-ops is a step the
harness's own README promises.

The module is loaded by path rather than imported: ``scripts/insights-sandbox``
is a directory of a hyphen, not a package.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts" / "insights-sandbox" / "sandbox.py"


def _load_sandbox():
    spec = importlib.util.spec_from_file_location("insights_sandbox_helpers", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sandbox = _load_sandbox()


def _git(clone: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── path safety ──────────────────────────────────────────────────────────


def test_assert_sandbox_path_refuses_live_and_outside(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    live = tmp_path / "live"
    (root / "run").mkdir(parents=True)
    live.mkdir()

    # The happy path: a clone directory inside the run directory.
    clone = sandbox.assert_sandbox_path(
        root / "run" / "base", sandbox_root=root, live=live
    )
    assert clone == (root / "run" / "base").resolve()

    # The live workspace itself, and a path inside it.
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(live, sandbox_root=root, live=live)
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(
            live / "memory-vault", sandbox_root=root, live=live
        )

    # Outside the sandbox root entirely.
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(
            tmp_path / "elsewhere" / "base", sandbox_root=root, live=live
        )

    # The sandbox root is not itself a clone: it holds the report and the
    # clones, and a "clone" here would be the run directory the diffs live in.
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(root, sandbox_root=root, live=live)

    # A sandbox root that contains the live path, where the checked path is an
    # ancestor of it: a write into that "clone" would escape upward into the
    # real files, so the containment runs the other way round too.
    inner_live = live / "inner"
    inner_live.mkdir()
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(live, sandbox_root=tmp_path, live=inner_live)
    # ... while a sibling clone in that same root is still fine.
    assert sandbox.assert_sandbox_path(
        root / "run" / "base", sandbox_root=tmp_path, live=inner_live
    ) == (root / "run" / "base").resolve()


# ── clone preparation ────────────────────────────────────────────────────


@pytest.fixture
def fake_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A clone-shaped workspace, plus the packaged stock schedule file."""
    clone = tmp_path / "live"
    runtime = clone / ".runtime"
    runtime.mkdir(parents=True)
    _git(clone.parent, "init", str(clone), "-q")
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "T")
    _git(clone, "remote", "add", "origin", "https://example.invalid/x.git")

    _write_json(
        runtime / "schedules.json",
        {"schedules": [{"schedule_id": "u1", "enabled": True, "title": "nightly"}]},
    )
    _write_json(
        runtime / "system_schedules_state.json",
        {"schedules": {"system-error-triage": {"enabled": True, "last_dispatched_at": "x"}}},
    )
    _write_json(
        runtime / "workspaces.json",
        [
            {
                "name": "work",
                "allowed_mcp_servers": ["notion"],
                "claude_ai_mcps": True,
                "gws_profile": "work-gws",
                "vault_root": "work/memory-vault",
            }
        ],
    )
    _write_json(runtime / "push_subscriptions.json", {"subs": ["endpoint"]})
    (runtime / "background").mkdir()
    (runtime / "background" / "orphan.json").write_text("{}", encoding="utf-8")
    (clone / ".mcp.json").write_text("{}", encoding="utf-8")
    (clone / "sub").mkdir()
    (clone / "sub" / ".mcp.json").write_text("{}", encoding="utf-8")
    (clone / ".env").write_text(
        "# keep me\nCIAO_WORKSPACE=.\nNOTION_TOKEN=x\nPWA_AUTH_TOKEN=y\n\n",
        encoding="utf-8",
    )
    stock = tmp_path / "stock.json"
    _write_json(stock, {"schedules": [{"schedule_id": "system-memory-curation"}]})
    return clone, stock


def test_prepare_clone_neutralises_side_effects(fake_workspace: tuple[Path, Path]) -> None:
    clone, stock = fake_workspace
    notes = sandbox.prepare_clone(
        clone, stock_schedules=stock, disable_insights=True
    )
    assert notes, "prepare_clone must report what it did"

    # The push loop and the phone notifications.
    assert _git(clone, "remote") == ""
    assert not (clone / ".runtime" / "push_subscriptions.json").exists()

    # Both schedule sources, including a system id the state file had never
    # seen: a created entry that stays enabled would fire on the next tick.
    user = json.loads((clone / ".runtime" / "schedules.json").read_text())
    assert all(row["enabled"] is False for row in user["schedules"])
    system = json.loads(
        (clone / ".runtime" / "system_schedules_state.json").read_text()
    )["schedules"]
    assert system["system-error-triage"]["enabled"] is False
    # An existing entry keeps its other fields.
    assert system["system-error-triage"]["last_dispatched_at"] == "x"
    assert "system-memory-curation" in system
    assert system["system-memory-curation"]["enabled"] is False
    assert "system-memory-curation@work" in system
    assert system["system-memory-curation@work"]["enabled"] is False

    # The startup triage chat and any woken background task.
    triage = json.loads((clone / ".runtime" / "startup_triage.json").read_text())
    assert triage["last_dispatched_at"]
    assert list((clone / ".runtime" / "background").iterdir()) == []

    # The archive pipeline must not run under the harness's own chats.
    settings = json.loads((clone / ".runtime" / "app_settings.json").read_text())
    assert settings["insights_enabled"] is False
    assert settings["trajectories_enabled"] is False

    # Integrations: no MCP server, no credential, no workspace profile.
    assert list(clone.rglob(".mcp.json")) == []
    env = (clone / ".env").read_text()
    assert "NOTION_TOKEN" not in env
    assert "CIAO_WORKSPACE=." in env
    assert "PWA_AUTH_TOKEN=y" in env
    assert "# keep me" in env
    workspace = json.loads((clone / ".runtime" / "workspaces.json").read_text())[0]
    assert workspace["allowed_mcp_servers"] == []
    assert workspace["claude_ai_mcps"] is False
    assert workspace["gws_profile"] == ""


def test_prepare_clone_keeps_insights_when_not_disabled(
    fake_workspace: tuple[Path, Path],
) -> None:
    """The one-shot arm needs the pipeline on; only the agent clone is muted."""
    clone, stock = fake_workspace
    sandbox.prepare_clone(clone, stock_schedules=stock, disable_insights=False)
    assert not (clone / ".runtime" / "app_settings.json").exists()


# ── archives and commits ─────────────────────────────────────────────────


def test_strip_archives_removes_insights_sections(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    archive = clone / "Logs" / "Chats" / "chat-1" / "claude" / "a.md"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        "# Chat\n\nthe transcript.\n\n## Session insights\n\n- a fact [memory]\n",
        encoding="utf-8",
    )
    plain = clone / "Logs" / "Chats" / "chat-2" / "claude" / "b.md"
    plain.parent.mkdir(parents=True)
    plain.write_text("# Chat\n\nno insights here.\n", encoding="utf-8")

    changed = sandbox.strip_archives(
        clone, ["Logs/Chats/chat-1/claude/a.md", "Logs/Chats/chat-2/claude/b.md"]
    )

    assert changed == 1
    assert "Session insights" not in archive.read_text()
    assert "the transcript." in archive.read_text()
    # A body with no appended section is returned unchanged, not rewritten.
    assert plain.read_text() == "# Chat\n\nno insights here.\n"


def test_commit_snapshot_and_diff_summary_classify(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(tmp_path, "init", str(clone), "-q")
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "T")
    _git(clone, "commit", "--allow-empty", "-q", "-m", "harness: baseline")
    base_sha = _git(clone, "rev-parse", "HEAD")

    vault = clone / "memory-vault" / "work" / "People"
    vault.mkdir(parents=True)
    (vault / "ada.md").write_text("# Ada\n", encoding="utf-8")
    guide = clone / "work" / "AGENTS.md"
    guide.parent.mkdir(parents=True)
    guide.write_text("# guide\n", encoding="utf-8")
    proposals = clone / "memory-vault" / "work" / "Workspace" / "Memory-Proposals.md"
    proposals.parent.mkdir(parents=True)
    proposals.write_text("# Proposals\n", encoding="utf-8")
    logs = clone / "Logs" / "Chats" / "chat-1" / "claude" / "a.md"
    logs.parent.mkdir(parents=True)
    logs.write_text("# Chat\n", encoding="utf-8")
    sandbox.commit_snapshot(clone, "harness: baseline")
    prepared = _git(clone, "rev-parse", "HEAD")

    # The arm's one chat: a new note, an edited note, a 2-bullet queue append,
    # a transcript rewrite and a guide edit.
    (vault / "ada.md").write_text("# Ada\n\nlikes tea.\n", encoding="utf-8")
    (vault / "grace.md").write_text("# Grace\n", encoding="utf-8")
    proposals.write_text(
        "# Proposals\n\n- maybe a thing [review]\n- maybe another [review]\n",
        encoding="utf-8",
    )
    logs.write_text("# Chat\n\nmore.\n", encoding="utf-8")
    guide.write_text("# guide\n\nexpanded.\n", encoding="utf-8")
    sha = sandbox.commit_snapshot(clone, "agent chat-1")

    summary = sandbox.diff_summary(clone, prepared, sha)
    assert set(summary) == {"added", "modified", "deleted", "by_class", "queued"}
    assert summary["added"] == [
        "memory-vault/work/People/grace.md",
    ]
    assert summary["modified"] == [
        # git sorts its output, and `Logs/` sorts before `memory-vault/`.
        "Logs/Chats/chat-1/claude/a.md",
        "memory-vault/work/People/ada.md",
        "memory-vault/work/Workspace/Memory-Proposals.md",
        "work/AGENTS.md",
    ]
    assert summary["deleted"] == []
    # Transcripts are inputs, not output: the server writes them by design, so
    # the raw lists still name them but they are not counted as a change.
    assert "logs" not in summary["by_class"]
    assert summary["by_class"] == {"vault": 2, "proposals": 1, "guide": 1}
    assert summary["queued"] == 2
    # The baseline commit is its own sha, and a clone with nothing new is
    # empty rather than an error.
    assert base_sha != prepared
    assert sandbox.diff_summary(clone, sha, sha)["by_class"] == {}


def test_classify_paths() -> None:
    assert sandbox.classify("memory-vault/work/Workspace/Memory-Proposals.md") == "proposals"
    assert sandbox.classify("Workspace/Memory-Proposals.md") == "proposals"
    assert sandbox.classify("work/AGENTS.md") == "guide"
    assert sandbox.classify("CLAUDE.md") == "guide"
    assert sandbox.classify("memory-vault/work/MEMORY.md") == "guide"
    assert sandbox.classify("memory-vault/work/People/ada.md") == "vault"
    assert sandbox.classify("work/memory-vault/work/Notes/x.md") == "vault"
    assert sandbox.classify("Logs/Chats/chat-1/claude/a.md") == "logs"
    # A vault path under Logs/ is transcript evidence, not vault output.
    assert sandbox.classify("memory-vault/Logs/x.md") == "logs"
    assert sandbox.classify("pyproject.toml") == "other"
    assert sandbox.classify("web/src/main.tsx") == "other"
