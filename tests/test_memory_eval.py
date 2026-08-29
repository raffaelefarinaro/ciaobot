"""Retrieval eval suite over a synthetic fixture vault.

LongMemEval-style cases for the deterministic half of memory: does the
retrieval layer surface the right note for paraphrase queries, keep both
versions of updated knowledge rankable, respect temporal tags, and return
nothing (abstention) rather than noise when the vault does not know?

Model behavior (whether the assistant *uses* these results well) is not
testable here; the manual probe runbook in docs/runbooks covers spot-checking
a live vault. Everything in this file must stay deterministic — no model
calls, no network.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from ciao import fts_search
from ciao.memory_audit import find_aging_state


@pytest.fixture
def eval_vault(tmp_path: Path) -> Path:
    """A small vault with people, projects, aliases, and an updated fact."""
    vault = tmp_path / "memory-vault"
    people = vault / "People"
    people.mkdir(parents=True)
    (people / "Burak.md").write_text(
        "---\n"
        "tags: [person]\n"
        "aliases: [brother-in-law]\n"
        "updated: 2026-08-01\n"
        "---\n"
        "# Burak\n\nIpek's brother, married to Gizem, father of Defne.\n",
        encoding="utf-8",
    )
    projects = vault / "projects"
    projects.mkdir()
    (projects / "Consulting.md").write_text(
        "---\n"
        "type: project\n"
        "aliases: [hourly rate, how much I charge]\n"
        "updated: 2026-08-01\n"
        "---\n"
        "# Consulting\n\nRate policy: 700 per hour is the top-of-band ask.\n",
        encoding="utf-8",
    )
    (projects / "Wedding.md").write_text(
        "---\ntype: project\nupdated: 2026-08-01\n---\n"
        "# Wedding\n\n"
        "Venue history: originally Hotel Poseidon; the venue is now "
        "Villa Corallo (decided 2026-07-10, supersedes Poseidon).\n",
        encoding="utf-8",
    )
    workspace = vault / "Workspace"
    workspace.mkdir()
    (workspace / "Memory-Proposals.md").write_text(
        "# Memory Proposals\n\n- [review] venue rumor noise about Villa Corallo\n",
        encoding="utf-8",
    )
    return vault


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    fts_search.init_db(connection)
    return connection


def _paths(rows: list[dict[str, str]]) -> list[str]:
    return [row["path"] for row in rows]


def test_paraphrase_queries_find_the_right_note(
    conn: sqlite3.Connection, eval_vault: Path
) -> None:
    """The failures observed on the live vault on 2026-08-29, now covered."""
    fts_search.index_vault(conn, eval_vault)

    hits = _paths(fts_search.search_vault(conn, "brother in law"))
    assert hits and "Burak" in hits[0]

    hits = _paths(fts_search.search_vault(conn, "how much do I charge per hour"))
    assert any("Consulting" in p for p in hits)


def test_updated_knowledge_keeps_current_and_history_findable(
    conn: sqlite3.Connection, eval_vault: Path
) -> None:
    """Supersession by invalidation: both venue names stay searchable, and
    the note carries which one is current."""
    fts_search.index_vault(conn, eval_vault)

    for query in ("wedding venue Villa Corallo", "wedding venue Poseidon"):
        hits = _paths(fts_search.search_vault(conn, query))
        assert any("Wedding" in p for p in hits), query
    snippet = fts_search.search_vault(conn, "venue now")[0]["snippet"]
    assert "Villa Corallo" in snippet


def test_abstention_nothing_is_better_than_noise(
    conn: sqlite3.Connection, eval_vault: Path
) -> None:
    """A query about something the vault does not know returns nothing."""
    fts_search.index_vault(conn, eval_vault)
    assert fts_search.search_vault(conn, "kubernetes ingress miskatonic") == []


def test_bookkeeping_never_outranks_knowledge(
    conn: sqlite3.Connection, eval_vault: Path
) -> None:
    fts_search.index_vault(conn, eval_vault)
    hits = _paths(fts_search.search_vault(conn, "Villa Corallo"))
    assert hits and all("Memory-Proposals" not in p for p in hits)


def test_temporal_tags_age_out_on_schedule() -> None:
    entries = [
        "Adoption data unavailable [as-of: 2026-05-01].",
        "Prefers plain engineering notes. [2026-08-15]",
    ]
    fresh = find_aging_state("memory", entries, today=datetime.date(2026, 6, 1))
    assert fresh == []  # 31 days: nothing aged yet
    later = find_aging_state("memory", entries, today=datetime.date(2026, 9, 1))
    assert [f["kind"] for f in later] == ["as-of"]  # 123d > 90d; learned 17d holds
