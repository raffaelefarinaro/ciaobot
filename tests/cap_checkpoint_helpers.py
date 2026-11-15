"""Test fixtures for exercising checkpoint construction through the stream."""

from pathlib import Path
from typing import Any

from ciao import memory_receipts as mr, proposal_predictions as pp


def checkpoint_config(queue: Path):
    from ciao.config import CiaoConfig, WorkspaceConfig
    vault = queue.parent.parent
    return CiaoConfig(
        pwa_auth_token="test", workspace_root=vault, vault_root=vault,
        state_path=vault / ".runtime" / "state.json", media_root=vault / ".runtime" / "media",
        workspaces={"test": WorkspaceConfig(name="test", vault_root=str(vault))},
    )


def write_sidecars(queue: Path, sidecars: dict[str, bytes | None]) -> None:
    for suffix, raw in sidecars.items():
        path = queue.with_suffix(suffix)
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(raw)


def build_checkpoint(
    queue: Path, sidecars: dict[str, bytes | None], receipts: list[dict[str, Any]],
    *, previous_checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    write_sidecars(queue, sidecars)
    if previous_checkpoint is not None and previous_checkpoint not in receipts:
        raise ValueError("installed checkpoint missing")
    predecessor = previous_checkpoint or {
        "v": mr.RECEIPT_VERSION, "id": mr.CAP_CHECKPOINT_ID, "kind": "cap_event_checkpoint",
        "status": mr.APPLIED, "schema": 1, "queue": str(queue),
        "sidecars": {".dismissed.jsonl": None, ".dismissed.log": None}, "events": [],
    }
    captured: dict[str, Any] = {}
    events, _ = pp.stream_checkpoint_events(queue, predecessor, receipts, config=checkpoint_config(queue), capture=captured)
    return {**predecessor, "sidecars": captured["bindings"], "events": events}


def validate_checkpoint(checkpoint: dict[str, Any], sidecars: dict[str, bytes | None]) -> bool:
    queue = Path(checkpoint["queue"])
    write_sidecars(queue, sidecars)
    try:
        pp.stream_checkpoint_events(queue, checkpoint, [checkpoint], config=checkpoint_config(queue))
    except (ValueError, OSError, UnicodeError, TypeError):
        return False
    return True
