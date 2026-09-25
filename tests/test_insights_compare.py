"""Tests for ``ciao.insights_compare`` — the dry-run agent-vs-one-shot report.

The whole module is inert by design, so the tests that matter most are the
ones asserting it stays that way: a comparison that wrote a proposals file
or touched a region would be worse than no comparison at all.
"""

from __future__ import annotations

import asyncio
import json
import os
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


# Newest first, and deliberately disagreeing with itself: the two newest chats
# are among the longest and the next two among the shortest, so length terciles
# and recency terciles are different sets. Every tercile holds two chats per
# provider, so a sampler that leaves a tercile's providers grouped shows up as
# two adjacent picks from the same provider.
_SKEWED_POOL = (
    ("chat-01", "claude", 9000),
    ("chat-02", "opencode", 9500),
    ("chat-03", "claude", 2000),
    ("chat-04", "opencode", 2100),
    ("chat-05", "claude", 10500),
    ("chat-06", "opencode", 10000),
    ("chat-07", "claude", 2200),
    ("chat-08", "opencode", 2300),
    ("chat-09", "claude", 10700),
    ("chat-10", "opencode", 10600),
    ("chat-11", "claude", 2400),
    ("chat-12", "opencode", 2450),
)


def _skewed_archives(logs_root: Path) -> None:
    base = 1_700_000_000
    for index, (chat_id, provider, chars) in enumerate(_SKEWED_POOL):
        path = _write_archive(
            logs_root, chat_id, provider,
            name=f"00000000-0000-0000-0000-0000000000{index:02d}",
            body="# chat\n\n" + "x" * chars,
        )
        mtime = base + (len(_SKEWED_POOL) - index) * 3600
        os.utime(path, (mtime, mtime))


def test_varied_sample_sorts_by_length_and_interleaves_providers(
    tmp_path: Path,
) -> None:
    """The three buckets are length terciles, not thirds of the newest-first list.

    Recent chats skew short, so slicing newest-first handed back three recency
    slices while the report claimed a length spread — and a bucket that
    happened to hold one provider's chats was sampled as that provider's
    territory. Both are asserted here, and over several seeds, because a
    seeded shuffle is deterministic per seed rather than wrong every time.
    """
    config = _config(tmp_path)
    _skewed_archives(config.logs_root)

    lengths = sorted(c.chars for c in ic.select_archives(
        config, last=50, sample="recent"
    ))
    assert len(lengths) == len(_SKEWED_POOL)

    for seed in (0, 1, 2):
        picked = ic.select_archives(config, last=12, sample="varied", seed=seed)
        assert len(picked) == 12
        # Round-robin draws one from each tercile in turn, so position i and
        # every position i + 3 came out of tercile i.
        terciles = [picked[i::3] for i in range(3)]
        assert [sorted(c.chars for c in t) for t in terciles] == [
            lengths[:4], lengths[4:8], lengths[8:12],
        ]
        for tercile in terciles:
            providers = [c.provider for c in tercile]
            assert all(
                one != other for one, other in zip(providers, providers[1:])
            ), providers


# Deliberately unequal: one provider leads in the short tercile, the other in
# the long one, and a tie in the middle. Four candidates per tercile is what
# `size = len(pool) // 3` produces here.
_UNEVEN_POOL = (
    ("chat-01", "claude", 900),
    ("chat-02", "claude", 1100),
    ("chat-03", "claude", 1300),
    ("chat-04", "opencode", 1500),
    ("chat-05", "claude", 1700),
    ("chat-06", "opencode", 1900),
    ("chat-07", "claude", 2100),
    ("chat-08", "opencode", 2300),
    ("chat-09", "claude", 2500),
    ("chat-10", "claude", 2700),
    ("chat-11", "opencode", 2900),
    ("chat-12", "opencode", 3100),
)


def _uneven_archives(logs_root: Path) -> None:
    base = 1_700_000_000
    for index, (chat_id, provider, chars) in enumerate(_UNEVEN_POOL):
        path = _write_archive(
            logs_root, chat_id, provider,
            name=f"00000000-0000-0000-0000-0000000000{index:02d}",
            body="# chat\n\n" + "x" * chars,
        )
        mtime = base + (len(_UNEVEN_POOL) - index) * 3600
        os.utime(path, (mtime, mtime))


def test_varied_sample_keeps_interleave_order_with_unequal_providers(
    tmp_path: Path,
) -> None:
    """A bucket is read in the order `_provider_interleaved` built it.

    The balanced pool above hides the bug: with two chats per provider per
    tercile, the interleaved order reads the same forwards and backwards. With
    three Claude chats and one opencode one the interleaved order is
    claude, opencode, claude, claude — consuming the bucket from the back
    hands back claude, claude, opencode, claude instead, which groups a
    provider exactly where the sampler claims not to.
    """
    config = _config(tmp_path)
    _uneven_archives(config.logs_root)

    lengths = sorted(
        c.chars
        for c in ic.select_archives(config, last=50, sample="recent")
    )
    assert len(lengths) == len(_UNEVEN_POOL)

    for seed in (0, 1, 2, 3, 4):
        picked = ic.select_archives(config, last=12, sample="varied", seed=seed)
        assert len(picked) == 12
        # Round-robin one per tercile in turn, so position i and every
        # position i + 3 came out of tercile i — lengths still the terciles.
        terciles = [picked[i::3] for i in range(3)]
        assert [sorted(c.chars for c in t) for t in terciles] == [
            lengths[:4], lengths[4:8], lengths[8:12],
        ]
        for tercile in terciles:
            providers = [c.provider for c in tercile]
            # `_provider_interleaved` opens with one candidate per provider
            # before it comes back for seconds, so the first two picks of a
            # bucket cannot be the same provider. Reading the bucket from the
            # back breaks exactly that.
            assert providers[0] != providers[1], (providers, seed)


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


def test_compare_one_records_an_archive_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate that fails before either mode runs is a result, not a raise.

    `run_compare` gathers one `compare_one` per chat and `gather` cancels its
    siblings on the first raise, so a single reclaimed archive — the archives
    are reclaimed under the server, and a fifty-chat run takes minutes — used
    to cost the whole report rather than one row in it.
    """
    config = _config(tmp_path)
    archive = _write_archive(config.logs_root, "chat-1", "claude")
    cache_dir = tmp_path / "cache"
    cand = ic.Candidate(
        archive_path=archive, chat_id="chat-1", provider="claude",
        workspace="", chars=1200,
    )

    calls: list[str] = []

    async def fake_text(body, model, **kwargs):
        calls.append("oneshot")
        return _INSIGHTS

    monkeypatch.setattr(insights, "_call_text_model", fake_text)
    # Selected, then reclaimed before the comparison came to read it.
    archive.unlink()

    record = asyncio.run(ic.compare_one(config, cand, cache_dir=cache_dir))

    assert calls == [], "a chat that cannot be read must not reach a model"
    assert record["oneshot"]["status"] == "error"
    assert "FileNotFoundError" in record["oneshot"]["error"]
    assert record["agent"]["status"] == "error"
    assert record["agent"]["error"] == record["oneshot"]["error"]
    assert record["agent"]["kept"] == []
    # Cached like any other record, so a resume neither retries nor loses it.
    cached = cache_dir / "chat-1__00000000-0000-0000-0000-000000000001.json"
    assert json.loads(cached.read_text(encoding="utf-8")) == record
    assert ic.render_report([record], workspace="", started="2026-09-25")


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


def test_run_compare_isolates_a_failing_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One candidate that raises must not take the other chats' report with it.

    `compare_one` records the failures it knows about; this is the backstop for
    one it does not. `asyncio.gather` cancels its siblings on the first raise,
    so without it a single bad chat ends the run with nothing written.
    """
    lines: list[str] = []

    async def compare_one(config, cand, *, cache_dir):
        if cand.chat_id == "bad":
            raise RuntimeError("the archive vanished mid-run")
        return {
            "chat_id": cand.chat_id, "provider": "claude", "chars": 10,
            "oneshot": {"status": "ok", "seconds": 1.0},
            "agent": {"status": "ok", "seconds": 2.0, "cost_usd": 0.5},
        }

    monkeypatch.setattr(ic, "compare_one", compare_one)
    cands = [
        ic.Candidate(archive_path=Path("good.md"), chat_id="good",
                     provider="claude", workspace="", chars=10),
        ic.Candidate(archive_path=Path("bad.md"), chat_id="bad",
                     provider="claude", workspace="", chars=10),
    ]
    out = asyncio.run(ic.run_compare(
        None, cands, cache_dir=tmp_path, progress=lines.append,
    ))

    assert [r["chat_id"] for r in out] == ["good", "bad"]
    assert out[0]["oneshot"]["status"] == "ok"
    # Reported in the run's own order, with the failure as the row it is.
    assert out[1]["oneshot"]["status"] == "error"
    assert "RuntimeError" in out[1]["oneshot"]["error"]
    assert out[1]["agent"]["status"] == "error"
    # The good chat still reported, and the failed one still counted as done.
    assert len(lines) == 2
    assert lines[1].startswith("[2/2] bad oneshot=error")


def _compare_dirs(vault: Path) -> list[Path]:
    """The per-call directories an agent extraction puts in the vault.

    Spelled out rather than read off the module, so the tests below assert
    what reaches the vault and not what the module happens to call it.
    """
    return sorted(vault.glob(".ciao-compare-*"))


def test_agent_extraction_leaves_no_tmp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcript copy is the only thing an agent run may put in the vault.

    It exists while the agent reads it and is gone afterwards, along with the
    private directory holding it: a dry run that leaves a `.ciao-compare-*`
    directory behind in a real vault is a change to the vault it promised not
    to make.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    archive = _write_archive(tmp_path / "logs", "chat-1", "claude")
    during: list[list[Path]] = []

    async def fake_run_readonly_agent(prompt, **kwargs):
        during.append(sorted(_compare_dirs(vault)[0].rglob("*")))
        return AgentRunResult(text=_INSIGHTS, turns=1, tool_calls=1)

    monkeypatch.setattr(insights_agent, "run_readonly_agent", fake_run_readonly_agent)

    result = asyncio.run(insights_agent.run_agent_extraction(
        archive, vault_root=vault, guide_path=None, model="sonnet",
    ))

    assert result.text == _INSIGHTS
    # The agent had one transcript to read, and it was inside the vault.
    assert len(during[0]) == 1
    assert during[0][0].suffix == ".md"
    # And afterwards the vault is exactly as it was found.
    assert _compare_dirs(vault) == []
    assert sorted(vault.rglob("*")) == []


def _transcript_path_from_prompt(prompt: str) -> Path:
    for line in prompt.splitlines():
        if "is at: " in line:
            return Path(line.split("is at: ", 1)[1].strip())
    raise AssertionError(f"prompt names no transcript: {prompt!r}")


def test_agent_extraction_preserves_existing_ciao_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temp directory the comparison did not create is not its to delete.

    The shared `.ciao-tmp` was created on demand and then `rmdir`'d
    unconditionally, so a run against a vault that already had one removed
    state it did not own. The copy now lives in a per-call `.ciao-compare-*`
    directory, and only that directory is removed.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    pre_existing = vault / ".ciao-tmp"
    pre_existing.mkdir()
    archive = _write_archive(tmp_path / "logs", "chat-1", "claude")
    seen: list[Path] = []

    async def fake_run_readonly_agent(prompt, **kwargs):
        transcript = _transcript_path_from_prompt(prompt)
        seen.append(transcript)
        # Inside the vault, so the read-only gate's single root covers it.
        assert transcript.is_file()
        return AgentRunResult(text=_INSIGHTS, turns=1, tool_calls=1)

    monkeypatch.setattr(insights_agent, "run_readonly_agent", fake_run_readonly_agent)

    result = asyncio.run(insights_agent.run_agent_extraction(
        archive, vault_root=vault, guide_path=None, model="sonnet",
    ))

    assert result.text == _INSIGHTS
    # The copy and the private directory holding it are gone.
    assert seen and not seen[0].exists()
    assert not seen[0].parent.exists()
    # The pre-existing directory is untouched: still there, still empty, and
    # the vault holds nothing else.
    assert pre_existing.is_dir()
    assert list(pre_existing.iterdir()) == []
    assert sorted(vault.rglob("*")) == [pre_existing]


def test_default_report_path_is_in_the_workspace(tmp_path: Path) -> None:
    assert ic.default_report_path(tmp_path, "2026-09-25") == (
        tmp_path / "Workspace" / "Insights-Compare-2026-09-25.md"
    )
