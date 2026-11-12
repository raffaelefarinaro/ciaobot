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
    (people / "Dario.md").write_text(
        "---\n"
        "tags: [person]\n"
        "aliases: [brother-in-law]\n"
        "updated: 2026-08-01\n"
        "---\n"
        "# Dario\n\nElena's brother, married to Sofia, father of Nina.\n",
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
        "# Consulting\n\nRate policy: 250 per hour is the top-of-band ask.\n",
        encoding="utf-8",
    )
    (projects / "Wedding.md").write_text(
        "---\ntype: project\nupdated: 2026-08-01\n---\n"
        "# Wedding\n\n"
        "Venue history: originally Hotel Boreale; the venue is now "
        "Villa Australis (decided 2026-07-10, supersedes Boreale).\n",
        encoding="utf-8",
    )
    workspace = vault / "Workspace"
    workspace.mkdir()
    (workspace / "Memory-Proposals.md").write_text(
        "# Memory Proposals\n\n- [review] venue rumor noise about Villa Australis\n",
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
    assert hits and "Dario" in hits[0]

    hits = _paths(fts_search.search_vault(conn, "how much do I charge per hour"))
    assert any("Consulting" in p for p in hits)


def test_updated_knowledge_keeps_current_and_history_findable(
    conn: sqlite3.Connection, eval_vault: Path
) -> None:
    """Supersession by invalidation: both venue names stay searchable, and
    the note carries which one is current."""
    fts_search.index_vault(conn, eval_vault)

    for query in ("wedding venue Villa Australis", "wedding venue Boreale"):
        hits = _paths(fts_search.search_vault(conn, query))
        assert any("Wedding" in p for p in hits), query
    snippet = fts_search.search_vault(conn, "venue now")[0]["snippet"]
    assert "Villa Australis" in snippet


def test_knowledge_update_returns_old_and_new_note_when_and_matches_one(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The note holding the old value and the one holding the new value rarely
    share every query word. AND matched only the newer note here, and because
    the OR fallback fired only on zero hits, the older note was dropped; the
    question "what changed" then had only half its evidence.
    """
    vault = tmp_path / "memory-vault"
    logs = vault / "projects"
    logs.mkdir(parents=True)
    (logs / "Commute-2026-03.md").write_text(
        "# Commute\n\nI drive to the office in Oerlikon, about 25 minutes.\n",
        encoding="utf-8",
    )
    (logs / "Commute-2026-09.md").write_text(
        "# Commute update\n\nSold the car: I now take tram 11 to the office in "
        "Oerlikon, about 35 minutes.\n",
        encoding="utf-8",
    )
    fts_search.index_vault(conn, vault)

    query = "commute office Oerlikon tram"
    # The fixture's premise: AND-of-all-words matches only the newer note.
    and_only = conn.execute(
        "SELECT path FROM vault_fts WHERE vault_fts MATCH ?",
        (" AND ".join(query.split()),),
    ).fetchall()
    assert [row[0] for row in and_only] == ["memory-vault/projects/Commute-2026-09.md"]

    hits = _paths(fts_search.search_vault(conn, query, limit=5))
    assert hits == ["memory-vault/projects/Commute-2026-09.md", "memory-vault/projects/Commute-2026-03.md"]


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
    hits = _paths(fts_search.search_vault(conn, "Villa Australis"))
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
