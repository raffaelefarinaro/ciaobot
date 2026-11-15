"""Disposable proposal server for the real-browser reservation regression."""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

workspace = tempfile.TemporaryDirectory(prefix="ciao-proposal-e2e-") if len(sys.argv) == 1 else None
root = Path(workspace.name if workspace is not None else sys.argv[1]).resolve()
os.environ["HOME"] = str(root)
os.environ["USERPROFILE"] = str(root)
os.environ["CIAO_MEMORY_DIR"] = str(root / "memory")
os.environ["CIAO_LAUNCH_AGENTS_DIR"] = str(root / "launch-agents")

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web import proposal_service
from ciao.web.proposal_reservations import DurableReservationRegistry, reserve_proposal_operation, seal_proposal_operation
from ciao.web.routes_api import list_proposals, proposal_action
from ciao.workspace_guide import guide_path

config = CiaoConfig(
    pwa_auth_token="fixture",
    workspace_root=root,
    state_path=root / "runtime" / "state.json",
    media_root=root / "runtime" / "media",
    vault_root=root / "memory-vault",
    workspaces={"personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")},
)
vault = config.workspace_vault_root("personal")
queue = vault / "Workspace" / "Memory-Proposals.md"
queue.parent.mkdir(parents=True, exist_ok=True)
if not queue.exists():
    queue.write_text("# Memory Proposals\n\n## pass\n\n"
                     "- [memory] Remember the isolated browser test. _(from: Decisions)_\n", encoding="utf-8")
entered, release = threading.Event(), threading.Event()
original_commit = proposal_service._commit_region_row


def held_commit(*args, **kwargs):
    entered.set()
    if not release.wait(60):
        raise TimeoutError("fixture worker was not released")
    return original_commit(*args, **kwargs)


proposal_service._commit_region_row = held_commit
(root / "runtime").mkdir(exist_ok=True)
registry = DurableReservationRegistry(root / "runtime" / "proposal-reservations")


async def status(_request):
    guide = guide_path(config.agent_root("personal"))
    return JSONResponse({"entered": entered.is_set(), "active": bool(registry._tasks),
                         "queue": queue.read_text(encoding="utf-8"),
                         "guide": guide.read_text(encoding="utf-8") if guide.exists() else ""})


async def unblock(_request):
    release.set()
    return JSONResponse({"released": True})


app = Starlette(routes=[
    Route("/api/proposals", list_proposals, methods=["GET"]),
    Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]),
    Route("/api/proposals/reservations/seal", seal_proposal_operation, methods=["POST"]),
    Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
    Route("/__proposal_test__/status", status, methods=["GET"]),
    Route("/__proposal_test__/release", unblock, methods=["POST"]),
])
app.state.config = config
app.state.project_chat_manager = None
app.state.proposal_reservations = registry

if __name__ == "__main__":
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 0))
    sock.listen()
    print(f"READY {sock.getsockname()[1]}", flush=True)
    try:
        uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
    finally:
        release.set()
        if workspace is not None:
            workspace.cleanup()
