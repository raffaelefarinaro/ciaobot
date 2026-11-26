"""The curation CLI's budget flags and lease TTL (kept from the retired care eval harness tests)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ciao import curation_run as cr


def _context_args(vault: Path, **flags: Any) -> Any:
    import argparse

    base: dict[str, Any] = {
        "vault_root": vault, "workspace": None, "guide": None, "max_items": None, "max_seconds": None,
        "holder": "h1", "json": True, "key": ["k1"],
    }
    return argparse.Namespace(**{**base, **flags})


def test_curation_flags_win_over_the_default_budget_at_the_cli(tmp_path: Path) -> None:
    from ciao import cli

    assert cli._curation_context(_context_args(tmp_path, max_items=1))[3] == cr.RunBudget(max_items=1)


def test_curation_begin_takes_its_lease_for_the_effective_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import cli

    seen: list[float] = []

    def busy(vault: Path, **kwargs: Any) -> Any:
        seen.append(kwargs["ttl_s"])
        raise cr.CurationBusy("held")

    monkeypatch.setattr(cr, "begin_run", busy)
    for name in ("CIAO_AGENT_TOKEN", "CIAO_AGENT_URL"):
        monkeypatch.delenv(name, raising=False)
    cli._curation_begin_command(_context_args(tmp_path, max_seconds=90.0))
    cli._curation_begin_command(_context_args(tmp_path))
    assert seen == [90.0, cr.DEFAULT_MAX_SECONDS]


def test_managed_care_begin_sends_the_effective_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import agent_cli, cli

    sent: list[tuple[str, dict[str, Any]]] = []

    def post(action: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        sent.append((action, payload))
        return {"ok": False, "error": {"code": "curation_busy", "message": "held"}}

    monkeypatch.setattr(agent_cli, "post", post)
    monkeypatch.setenv("CIAO_AGENT_TOKEN", "t")
    monkeypatch.setenv("CIAO_AGENT_URL", "http://127.0.0.1:1")
    assert cli._curation_begin_command(_context_args(tmp_path, max_seconds=45.0)) == cli.CURATION_BUSY_EXIT
    assert sent == [("care_begin", {"ttl_s": 45.0, "vault": str(tmp_path)})]


def test_curation_progress_renews_the_lease_for_the_effective_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import cli

    seen: list[float] = []

    def record(vault: Path, keys: list[str], **kwargs: Any) -> list[str]:
        seen.append(kwargs["ttl_s"])
        raise cr.CurationBusy("lost")

    monkeypatch.setattr(cr, "record_done", record)
    cli._curation_progress_command(_context_args(tmp_path, max_seconds=120.0))
    cli._curation_progress_command(_context_args(tmp_path))
    assert seen == [120.0, cr.DEFAULT_MAX_SECONDS]
