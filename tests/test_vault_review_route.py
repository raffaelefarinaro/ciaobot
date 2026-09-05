"""The /api/vault/review route's scan budget and response shape.

`generate_candidates` reads every note in the vault three times (scan,
validate, then a per-entry `read_bytes`). A row decision therefore has a real
cost, and the client used to add a third full generation by re-GETting after
every POST. The route now hands back the queue it had to rebuild anyway.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.web.app import create_app


@pytest.fixture
def client(tmp_path: Path):
    vault = tmp_path / "memory-vault"
    (vault / "personal").mkdir(parents=True)
    for name in ("Orphan.md", "Lonely.md"):
        (vault / "personal" / name).write_text(
            f"---\ntype: note\n---\n# {name}\n", encoding="utf-8"
        )
    cfg = CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        pwa_auth_required=False,
        vault_root=vault,
    )
    return TestClient(create_app(cfg))


def _count_generations(monkeypatch) -> list[int]:
    from ciao import vault_review

    calls: list[int] = []
    real = vault_review.generate_candidates

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(vault_review, "generate_candidates", counted)
    return calls


def test_a_decision_returns_the_queue_it_rebuilt(client, monkeypatch) -> None:
    listing = client.get("/api/vault/review?workspace=personal&include=trashed")
    assert listing.status_code == 200
    candidates = listing.json()["candidates"]
    assert candidates, "fixture should produce at least one orphan candidate"

    calls = _count_generations(monkeypatch)
    decided = client.post(
        "/api/vault/review?workspace=personal",
        json={
            "action": "decide",
            "candidate_id": candidates[0]["candidate_id"],
            "disposition": "keep",
        },
    )

    assert decided.status_code == 200
    body = decided.json()
    assert body["ok"] is True
    # The whole point: the client can render from this instead of re-GETting.
    assert isinstance(body["candidates"], list)
    assert isinstance(body["trashed"], list)
    # Two generations: one to resolve the candidate id (and prove the note has
    # not changed under us), one to refresh the pending-only projection. The
    # third was the client's follow-up GET.
    assert len(calls) == 2


def test_a_restore_or_delete_also_returns_the_queue(client, monkeypatch) -> None:
    listing = client.get("/api/vault/review?workspace=personal")
    candidate_id = listing.json()["candidates"][0]["candidate_id"]
    client.post(
        "/api/vault/review?workspace=personal",
        json={"action": "trash", "candidate_id": candidate_id},
    )

    calls = _count_generations(monkeypatch)
    restored = client.post(
        "/api/vault/review?workspace=personal",
        json={"action": "restore", "candidate_id": candidate_id},
    )

    assert restored.status_code == 200
    body = restored.json()
    assert isinstance(body["candidates"], list)
    assert isinstance(body["trashed"], list)
    # restore/delete never needed the pre-scan (they address the trash sidecar
    # by id), so they cost exactly one generation.
    assert len(calls) == 1


def test_a_read_only_listing_still_costs_one_generation(client, monkeypatch) -> None:
    calls = _count_generations(monkeypatch)
    assert client.get("/api/vault/review?workspace=personal").status_code == 200
    assert len(calls) == 1
