"""Tests for ``ciao.insights_compare`` — the dry-run agent-vs-one-shot report.

The whole module is inert by design, so the tests that matter most are the
ones asserting it stays that way: a comparison that wrote a proposals file
or touched a region would be worse than no comparison at all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ciao import insights
from ciao import insights_agent
from ciao import insights_compare as ic
from ciao.providers.oneshot import AgentRunResult


_ARCHIVE_BODY = "# Archived chat\n\nturn one.\n"
_INSIGHTS = (
    "## Decisions\n"
    "- Chose the thing over the other because it is cheaper. [memory]\n"
)


def _config(tmp_path: Path):
    """A config with only the roots the comparison reads, pointed at tmp."""
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    config.vault_root = tmp_path / "vault"
    config.workspace_root = tmp_path / "ws"
    config.state_path = tmp_path / "runtime" / "state.json"
    config.vault_root.mkdir(parents=True)
    config.workspace_root.mkdir(parents=True)
    return config


def _write_archive(
    logs_root: Path,
    chat_id: str,
    provider: str,
    name: str = "00000000-0000-0000-0000-000000000001",
    body: str | None = None,
) -> Path:
    folder = logs_root / "Chats" / chat_id / provider
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(
        body if body is not None else _ARCHIVE_BODY + ("x" * 1200 + "\n"),
        encoding="utf-8",
    )
    return path


def test_strip_insights_removes_appended_section() -> None:
    stamped = (
        "# chat\n\nturns.\n\n<!-- ciao:session-insights -->\n"
        "## Session insights\n\n## Decisions\n- old answer [memory]\n"
    )
    stripped = ic.strip_insights(stamped)
    assert "old answer" not in stripped
    assert stripped.startswith("# chat")
    # Nothing appended: the body is the whole archive, not a cut of it.
    assert ic.strip_insights(_ARCHIVE_BODY) == _ARCHIVE_BODY


def test_select_archives_filters_workspace_and_varied_sample(tmp_path: Path) -> None:
    config = _config(tmp_path)
    logs = config.logs_root
    _write_archive(logs, "chat-work-1", "claude")
    _write_archive(logs, "chat-work-2", "opencode")
    _write_archive(logs, "chat-work-3", "claude")
    # Short enough that no mode has anything to say about it.
    _write_archive(logs, "chat-work-4", "claude", body="# stub\n\ntiny.\n")
    # Right shape, wrong workspace.
    _write_archive(logs, "chat-home-1", "claude")
    # A directory that is not a provider: not an archive to compare.
    _write_archive(logs, "chat-work-5", "nonsense")

    (config.state_path.parent).mkdir(parents=True, exist_ok=True)
    (config.state_path.parent / "web_projects.json").write_text(
        json.dumps(
            {
                "chats": {
                    "chat-work-1": {"project_id": "p-work"},
                    "chat-work-2": {"project_id": "p-work"},
                    "chat-work-3": {"project_id": "p-work"},
                    "chat-work-4": {"project_id": "p-work"},
                    "chat-work-5": {"project_id": "p-work"},
                    "chat-home-1": {"project_id": "p-home"},
                },
                "projects": {
                    "p-work": {"workspace": "work"},
                    "p-home": {"workspace": "personal"},
                },
            }
        ),
        encoding="utf-8",
    )

    picked = ic.select_archives(config, workspace="work", last=10, sample="varied")
    ids = {c.chat_id for c in picked}
    assert ids == {"chat-work-1", "chat-work-2", "chat-work-3"}
    assert all(c.workspace == "work" for c in picked)
    # The `varied` sampler must not collapse the sample onto one provider.
    assert {c.provider for c in picked} == {"claude", "opencode"}

    # Deterministic for a fixed seed: a rerun has to be the same run.
    again = ic.select_archives(config, workspace="work", last=10, sample="varied")
    assert [c.chat_id for c in again] == [c.chat_id for c in picked]

    assert ic.select_archives(config, workspace="work", last=1, sample="varied")
    assert len(ic.select_archives(config, workspace="work", last=1, sample="recent")) == 1


def test_compare_one_dry_run_writes_only_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    logs = config.logs_root
    archive = _write_archive(logs, "chat-1", "claude")
    cache_dir = tmp_path / "cache"
    calls: list[str] = []

    async def fake_text(body, model, **kwargs):
        calls.append("oneshot")
        return _INSIGHTS

    async def fake_agent(archive_path, *, vault_root, guide_path, model, provider="claude"):
        calls.append("agent")
        return AgentRunResult(
            text=_INSIGHTS, turns=2, tool_calls=3, cost_usd=0.05, denied=0
        )

    monkeypatch.setattr(insights, "_call_text_model", fake_text)
    monkeypatch.setattr(insights_agent, "run_agent_extraction", fake_agent)

    before = sorted(config.vault_root.rglob("*"))
    record = asyncio.run(ic.compare_one(
        config,
        ic.Candidate(
            archive_path=archive, chat_id="chat-1", provider="claude",
            workspace="", chars=1200,
        ),
        cache_dir=cache_dir,
    ))

    assert record["oneshot"]["status"] == "ok"
    assert record["agent"]["status"] == "ok"
    assert record["agent"]["tool_calls"] == 3
    assert record["agent"]["cost_usd"] == 0.05
    kept = record["oneshot"]["kept"]
    assert [k["target"] for k in kept] == ["memory"]
    assert kept[0]["text"].startswith("Chose the thing")
    assert record["agent"]["kept"] == kept

    # A dry run touches the vault not at all.
    assert sorted(config.vault_root.rglob("*")) == before
    assert not (config.vault_root / "Workspace" / "Memory-Proposals.md").exists()

    cached = cache_dir / "chat-1__00000000-0000-0000-0000-000000000001.json"
    assert json.loads(cached.read_text(encoding="utf-8")) == record

    # Resumable: a second call is served from the cache, not from a model.
    again = asyncio.run(ic.compare_one(
        config,
        ic.Candidate(
            archive_path=archive, chat_id="chat-1", provider="claude",
            workspace="", chars=1200,
        ),
        cache_dir=cache_dir,
    ))
    assert again == record
    assert calls == ["oneshot", "agent"]


def test_compare_one_passes_provider_to_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    logs = config.logs_root
    seen: list[str] = []

    async def fake_text(body, model, **kwargs):
        return _INSIGHTS

    async def fake_agent(archive_path, *, vault_root, guide_path, model, provider="claude"):
        seen.append(provider)
        return AgentRunResult(text=_INSIGHTS)

    monkeypatch.setattr(insights, "_call_text_model", fake_text)
    monkeypatch.setattr(insights_agent, "run_agent_extraction", fake_agent)

    opencode_archive = _write_archive(logs, "chat-oc", "opencode")
    record = asyncio.run(ic.compare_one(
        config,
        ic.Candidate(
            archive_path=opencode_archive, chat_id="chat-oc", provider="opencode",
            workspace="", chars=1200,
        ),
        cache_dir=tmp_path / "cache",
    ))
    assert seen == ["opencode"]
    assert record["agent"]["status"] == "ok"

    # A provider with no read-only ruleset is reported as skipped, not
    # silently compared against an empty agent run.
    other = _write_archive(logs, "chat-other", "acme")
    record = asyncio.run(ic.compare_one(
        config,
        ic.Candidate(
            archive_path=other, chat_id="chat-other", provider="acme",
            workspace="", chars=1200,
        ),
        cache_dir=tmp_path / "cache",
    ))
    assert record["agent"]["status"] == "skipped: acme"
    assert seen == ["opencode"]
    assert record["oneshot"]["status"] == "ok"


def test_render_report_has_totals_and_per_chat_sections() -> None:
    results = [
        {
            "chat_id": "chat-1",
            "provider": "claude",
            "chars": 4200,
            "oneshot": {
                "status": "ok", "seconds": 4.0, "kept": [
                    {"target": "memory", "payload": "", "text": "One fact."}
                ],
                "suppressed": 0, "dropped": 1, "error": "",
            },
            "agent": {
                "status": "ok", "seconds": 31.5, "kept": [
                    {"target": "people", "payload": "Ada", "text": "Another fact."}
                ],
                "suppressed": 2, "dropped": 0, "tool_calls": 7, "denied": 1,
                "cost_usd": 0.12, "error": "",
            },
        },
        {
            "chat_id": "chat-2",
            "provider": "opencode",
            "chars": 900,
            "oneshot": {
                "status": "error", "seconds": 1.0, "kept": [],
                "suppressed": 0, "dropped": 0, "error": "timeout",
            },
            "agent": {
                "status": "skipped: opencode", "seconds": 0.0, "kept": [],
                "suppressed": 0, "dropped": 0, "tool_calls": 0, "denied": 0,
                "cost_usd": None, "error": "",
            },
        },
    ]

    report = ic.render_report(results, workspace="work", started="2026-09-25")

    assert "type: reference" in report
    assert "tags: [insights-compare]" in report
    assert "2026-09-25" in report
    # Totals per provider and per mode.
    assert "### claude" in report
    assert "### opencode" in report
    assert "| oneshot |" in report
    assert "| agent |" in report
    assert "$0.1200" in report
    # One section per chat, with both modes' kept proposals.
    assert "### chat-1 (claude, 4200 chars)" in report
    assert "- [memory] One fact." in report
    assert "- [people: Ada] Another fact." in report
    assert "7 tool calls, 1 denied" in report
    assert "### chat-2 (opencode, 900 chars)" in report
    assert "error: timeout" in report


def test_run_compare_reports_each_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[str] = []

    # Only the progress line is under test here; compare_one is covered above.
    async def compare_one(config, cand, *, cache_dir):
        return {
            "chat_id": cand.chat_id, "provider": "claude", "chars": 10,
            "oneshot": {"status": "ok", "seconds": 1.0},
            "agent": {"status": "ok", "seconds": 2.0, "cost_usd": 0.5},
        }

    monkeypatch.setattr(ic, "compare_one", compare_one)
    cands = [
        ic.Candidate(archive_path=Path("a.md"), chat_id="a", provider="claude",
                     workspace="", chars=10),
        ic.Candidate(archive_path=Path("b.md"), chat_id="b", provider="claude",
                     workspace="", chars=10),
    ]
    out = asyncio.run(ic.run_compare(
        None, cands, cache_dir=tmp_path, progress=lines.append,
    ))

    assert len(out) == 2
    assert len(lines) == 2
    assert lines[0].startswith("[1/2] a oneshot=ok 1.0s agent=ok 2.0s cost_so_far=$0.5000")
    assert lines[1].startswith("[2/2] b oneshot=ok 1.0s agent=ok 2.0s cost_so_far=$1.0000")


def test_default_report_path_is_in_the_workspace(tmp_path: Path) -> None:
    assert ic.default_report_path(tmp_path, "2026-09-25") == (
        tmp_path / "Workspace" / "Insights-Compare-2026-09-25.md"
    )
