#!/usr/bin/env python3
"""Full-agent memory-pass sandbox harness (#594): run both arms, diff both clones.

The #586 comparison put a *read-only* agent next to today's one-shot
extraction and the agent barely used its tools: the median was one call, and
it was reading the transcript. It also had no ``ciao`` CLI, no skills and no
way to check what memory already held, so it could not do the thing a chat
actually does at the end of a conversation -- write.

This harness measures that. Both arms process the same archived chats:

- ``oneshot`` runs today's production chain in process
  (``insights.extract_and_append(..., text_mode=True)``) -- insights, then
  the project-doc fold, then memory proposals with auto-promote.
- ``agent`` opens a normal attended chat on a second Ciaobot server and lets
  it do the pass the way a person would.

Each arm gets its own APFS clone of the live workspace, prepared by
:mod:`sandbox` so nothing in it can reach outside, and commits after every
chat. The report is a diff: what each arm really wrote, per chat, side by
side. No verdict is written. The operator reads it and decides.

The dry run is therefore enforced by *where* the arms run, not by which
tools they have -- a restricted agent is not the behaviour under test.

Usage::

    PYTHONPATH=$PWD python scripts/insights-sandbox/run.py \\
        --run-id pilot-1 --arms oneshot --limit 2

Nothing here is ever pointed at the live workspace: every clone path goes
through :func:`sandbox.assert_sandbox_path` first, and the one-shot arm
re-checks its own resolved config before the first model call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import (  # noqa: E402 - after the sys.path insert above
    SandboxError,
    assert_contained,
    assert_sandbox_path,
    clone_workspace,
    commit_snapshot,
    diff_summary,
    pick_port,
    prepare_clone,
    strip_archives,
    sum_claude_usage,
)

# The develop checkout the harness itself runs from, so the agent-clone server
# imports the source under review rather than the copy of `ciao/` that sits
# inside the clone it is serving.
REPO_ROOT = Path(__file__).resolve().parents[2]

# The default #586 cache dirs: the two workspaces the comparison was run for.
DEFAULT_CACHE_RUN_IDS = ("2026-09-25-work", "2026-09-25-personal")

AGENT_PROMPT = (
    "A conversation in this workspace just ended. Its archived transcript is "
    "at {archive}. It belonged to project {project} (canonical doc: {doc}). "
    "Do the end-of-conversation memory pass the way you normally would: read "
    "the transcript, check what the vault and memory already hold, update "
    "existing notes (people, projects, the project doc), create a note only "
    "for a genuinely new entity, promote durable facts to memory with the "
    "ciao CLI, and queue anything uncertain for review. Do not do anything "
    "outside memory and the vault: no messages, emails, commits, pushes or "
    "external calls. Finish with a short list of what you changed."
)


class Instance:
    """Minimal authenticated HTTP client for a Ciaobot server.

    Copied from ``scripts/surface-compare/run_sessions.py`` (~L104), which is
    the reference for driving real chat sessions over REST. Only the calls this
    harness needs are kept, and ``approve_permission`` is deliberately absent:
    the agent arm must never click an approval card, because a card is a tool
    call the report would not otherwise account for.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url, timeout=30.0, follow_redirects=True
        )
        r = self.client.post("/api/auth", json={"token": token})
        r.raise_for_status()

    def get(self, path: str, **kw: Any) -> Any:
        r = self.client.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: dict | None = None) -> Any:
        r = self.client.post(path, json=payload or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    def active_chat_ids(self) -> set[str]:
        return set(self.get("/api/active-chats").get("active_chat_ids") or [])

    def chat(self, chat_id: str) -> dict[str, Any] | None:
        for row in self.get("/api/chats"):
            if row.get("chat_id") == chat_id:
                return row
        return None


# ── selection ────────────────────────────────────────────────────────────


@dataclass
class Row:
    """One archived chat both arms will process, as read from a #586 cache."""

    chat_id: str
    workspace: str
    provider: str
    effective_provider: str
    model: str
    archive_rel: Path
    doc: str = ""


def select_rows(cache_dirs: list[Path], live: Path, limit: int) -> list[Row]:
    """The #586 cache rows whose archive still exists under ``live``.

    The cache is the shared input for both arms, which is what makes them
    comparable: same chats, same model per chat, same workspace. A row whose
    archive has since been reclaimed is dropped rather than substituted --
    a different chat would break the pairing, not just the count.
    """
    rows: list[Row] = []
    seen: set[tuple[str, str]] = set()
    for cache_dir in cache_dirs:
        if not cache_dir.is_dir():
            continue
        for path in sorted(cache_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            chat_id = str(data.get("chat_id") or "")
            provider = str(data.get("provider") or "")
            if not chat_id or not provider or "__" not in path.stem:
                continue
            # The cache file is "<chat id>__<archive stem>.json".
            stem = path.stem.split("__", 1)[1]
            archive_rel = Path("Logs") / "Chats" / chat_id / provider / f"{stem}.md"
            if not (live / archive_rel).exists():
                continue
            key = (chat_id, stem)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                Row(
                    chat_id=chat_id,
                    workspace=str(data.get("workspace") or ""),
                    provider=provider,
                    effective_provider=str(
                        data.get("effective_provider") or provider
                    ),
                    model=str(data.get("model") or ""),
                    archive_rel=archive_rel,
                )
            )
    if limit > 0:
        rows = rows[:limit]
    return rows


def check_live_idle(live_url: str) -> None:
    """Refuse to start while the live instance has chats in flight.

    Cloning a workspace mid-turn would capture a half-written archive and a
    half-finished memory pass into all three baselines. ``/api/active-chats``
    needs no auth; a request that fails is a warning, not an abort, because a
    stopped live instance is the normal case for an offline pilot.
    """
    try:
        response = httpx.get(f"{live_url.rstrip('/')}/api/active-chats", timeout=5.0)
        response.raise_for_status()
        active = list(response.json().get("active_chat_ids") or [])
    except Exception as exc:  # noqa: BLE001 - a stopped live server is fine
        print(f"warning: could not read live active-chats ({exc}); continuing")
        return
    if active:
        raise SandboxError(
            f"the live instance has {len(active)} active chat(s) ({', '.join(active[:5])}); "
            "wait for them to finish before cloning the workspace"
        )
    print("live instance is idle")


# ── project map ──────────────────────────────────────────────────────────


def project_docs(agent_clone: Path, inst: Instance, rows: list[Row]) -> dict[str, str]:
    """chat id -> the canonical project doc path both arms are told about.

    Mirrors ``ArchivePipeline._job_inputs``: a project's ``vault_doc_path`` is
    used only when the chat really belonged to a named, non-auto,
    non-system project. An auto or system project has no doc a person curates,
    so pointing an agent at one would be inventing a destination.

    Read from the agent clone's own ``web_projects.json`` -- the server has
    just booted on it and may have written to it.
    """
    map_rows: dict[str, dict[str, Any]] = {}
    for workspace in sorted({row.workspace for row in rows}):
        for project in inst.get("/api/projects", params={"workspace": workspace}):
            map_rows[str(project.get("project_id") or "")] = project
    try:
        data = json.loads(
            (agent_clone / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {row.chat_id: "" for row in rows}
    chats = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(chats, dict):
        return {row.chat_id: "" for row in rows}
    docs: dict[str, str] = {}
    for row in rows:
        chat = chats.get(row.chat_id)
        project_id = str(chat.get("project_id") or "") if isinstance(chat, dict) else ""
        project = map_rows.get(project_id) or {}
        usable = bool(project) and not project.get("is_auto") and not project.get(
            "is_system"
        )
        docs[row.chat_id] = str(project.get("vault_doc_path") or "") if usable else ""
    return docs


# ── path containment ─────────────────────────────────────────────────────


def clone_config(clone: Path) -> Any:
    """A ``CiaoConfig`` bound to ``clone`` by an explicit env dict.

    Never built from ``os.environ``: :meth:`CiaoConfig.from_env` falls back to
    the LaunchAgent's (live) workspace when ``CIAO_WORKSPACE`` is missing, and
    the containment check below is only meaningful if the config it inspects
    is the one the arm will actually use.
    """
    from ciao.config import CiaoConfig

    return CiaoConfig.from_env(
        {
            "CIAO_WORKSPACE": str(clone),
            "CIAO_RUNTIME_ROOT": str(clone / ".runtime"),
            "PWA_AUTH_TOKEN": "sandbox",
        }
    )


def clone_doc(clone: Path, doc: str) -> Path:
    """A project doc as a path inside ``clone``, without joining an absolute one.

    ``vault_doc_path`` may be absolute -- a vault outside the workspace is a
    supported configuration -- and ``Path / absolute_path`` returns the
    absolute path, so ``clone / doc`` would silently hand an external doc to
    the agent. The check is what decides, so the join only happens for a
    relative doc, and an absolute one is passed through to be refused.
    """
    path = Path(doc).expanduser()
    if not path.is_absolute():
        return clone / path
    return path


def assert_clone_containment(
    config: Any, clone: Path, rows: list[Row], *, docs: list[str] | None = None
) -> None:
    """Refuse unless every path both arms will write lives inside ``clone``.

    Covers the settings ``prepare_clone`` cannot rebase: ``vault_root`` and
    every selected ``workspace_vault_root``/``agent_root`` are registry values
    that may be absolute, and a project's ``vault_doc_path`` may point at a
    vault outside the workspace entirely. A clone of such an install resolves
    the originals, and both arms -- the one-shot chain and a full agent with
    Bash -- would write them.

    Fails closed rather than rebasing: a refused run is recoverable, a memory
    pass that wrote the live vault is not. Called before the first model call
    in the one-shot arm and before ``Popen`` in the agent arm, so the refusal
    lands before anything has been started or written.

    ``docs`` defaults to the docs on ``rows``, which is what both arms want once
    the project map is known. The pre-boot call passes an empty list instead,
    because there is no project map yet -- and it has to be an empty *list*
    rather than a default, or the fallback would check rows whose docs are not
    filled in yet.
    """
    workspaces = sorted({row.workspace for row in rows if row.workspace})
    paths: list[Path] = [config.workspace_root, config.vault_root]
    for workspace in workspaces:
        try:
            paths.append(config.workspace_vault_root(workspace))
            paths.append(config.agent_root(workspace))
        except ValueError as exc:
            # An unusable workspace name is still a refusal, not a crash: the
            # arm would resolve this workspace's roots before it wrote
            # anything, and the operator needs the name and the reason rather
            # than a traceback from inside `CiaoConfig`.
            raise SandboxError(
                f"workspace {workspace!r} does not resolve to a usable root: {exc}"
            ) from exc
    paths.extend(clone / row.archive_rel for row in rows)
    selected_docs = [row.doc for row in rows] if docs is None else docs
    for doc in selected_docs:
        if doc:
            paths.append(clone_doc(clone, doc))
    assert_contained(paths, clone)


# ── the agent-clone server ───────────────────────────────────────────────


def start_server(
    agent_clone: Path, run_dir: Path, port: int, rows: list[Row]
) -> tuple[subprocess.Popen, Instance, str]:
    """Start a second Ciaobot server on the agent clone; return it and a client.

    Bound to loopback with a fresh random token: the harness is driving a local
    automation, and a fixed token in a source file is a credential in a
    repository. Started *before* either arm because the project map comes from
    it, and the one-shot arm runs against a different clone, so there is no
    overlap in what either one touches.

    ``port`` is resolved by :func:`sandbox.pick_port` before the process is
    started, so a default run cannot land on a port somebody else is already
    listening on.

    The agent arm's containment check runs here, before ``Popen``: this is the
    last point at which the run can be refused without a server -- and, once
    the agent clone has a server on it, a full agent with Bash -- already
    running against it. The doc half of the check has to wait, because the
    project map only exists after boot; the caller does that part.

    Everything after ``Popen`` sits in a ``try``. A server that exits during
    boot, and a login that turns out to have been answered by somebody else's
    Ciaobot instance, are both failures -- and neither may leave a process
    listening on the operator's loopback holding a full-access agent token,
    because the caller's ``finally`` cannot stop a process this function never
    returned.
    """
    port = pick_port(port)
    # The config the server will build for itself, with no project docs yet:
    # the roots, the archives, and nothing that only boot can know.
    assert_clone_containment(clone_config(agent_clone), agent_clone, rows, docs=[])
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CIAO_") and not key.startswith("PWA_")
    }
    token = secrets.token_urlsafe(24)
    env.update(
        {
            "CIAO_WORKSPACE": str(agent_clone),
            "CIAO_RUNTIME_ROOT": str(agent_clone / ".runtime"),
            "PWA_PORT": str(port),
            "PWA_HOST": "127.0.0.1",
            "PWA_AUTH_TOKEN": token,
            "CIAO_NO_BROWSER": "1",
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )
    log_path = run_dir / "agent-server.log"
    log = log_path.open("w", encoding="utf-8")
    proc: subprocess.Popen | None = None
    base_url = f"http://127.0.0.1:{port}"
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ciao.cli", "run"],
            cwd=agent_clone,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise SandboxError(
                    f"the agent-clone server exited with {proc.returncode}:"
                    f"{_log_tail(log_path)}"
                )
            try:
                if httpx.get(f"{base_url}/api/active-chats", timeout=3.0).status_code == 200:
                    break
            except Exception:  # noqa: BLE001 - not up yet
                pass
            time.sleep(1.0)
        else:
            raise SandboxError(
                f"the agent-clone server did not answer on {base_url} within 120s:"
                f"{_log_tail(log_path)}"
            )
        try:
            inst = Instance(base_url, token)
        except httpx.HTTPStatusError as exc:
            # A Ciaobot instance that does not know this token says 401, and
            # the harness token is one nobody else can have: so the thing that
            # answered is a Ciaobot server this run did not start.
            if exc.response.status_code == 401:
                raise SandboxError(
                    f"server on {base_url} rejected the harness token; "
                    "another server may own this port"
                ) from exc
            raise
        # Ownership, second gate. The token is only proof if the instance let
        # it in; an endpoint the harness actually uses proves there is a
        # Ciaobot API on the other end at all, rather than something that
        # happened to answer /api/auth.
        inst.get("/api/projects")
        assert_no_remote(agent_clone)
        return proc, inst, base_url
    except BaseException:
        # The server is stopped here rather than by the caller, because the
        # caller only knows about a process this function handed back.
        # `stop_server` is a no-op on a process that already exited, and there
        # is nothing to stop if Popen itself failed.
        if proc is not None:
            stop_server(proc)
        log.close()
        raise


def _log_tail(path: Path, lines: int = 40) -> str:
    """The last ``lines`` of a server log, indented onto the error message.

    A pilot that dies on a port should not need a second command before the
    operator can see what the server it *did* start said on its way out.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return " (no log)"
    tail = [line for line in text.splitlines() if line.strip()][-lines:]
    if not tail:
        return " (the log is empty)"
    return ":\n  " + "\n  ".join(tail)


def stop_server(proc: subprocess.Popen) -> None:
    """SIGTERM, wait, then SIGKILL. Always called, via try/finally."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(20)
    except subprocess.TimeoutExpired:
        proc.kill()


def assert_no_remote(clone: Path) -> None:
    """The clone's git remote must still be gone after the server booted.

    ``prepare_clone`` removed it before anything ran; this is the check that
    nothing the server did put it back, because a restored remote means the
    30-second backup loop is live again.
    """
    remotes = subprocess.run(
        ["git", "-C", str(clone), "remote"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if remotes:
        raise SandboxError(f"{clone} regained a git remote after boot: {remotes}")


# How long `/api/active-chats` must have been continuously empty before the
# harness treats boot as finished. Not a single empty sample: a server that has
# just bound its port has not yet run its first tick, and a tick that fires a
# schedule, a startup-triage check or the startup backfill would write into the
# agent clone with nobody attributing it. Ten seconds of nothing is a cheap way
# to tell "booted" from "about to".
_BOOT_IDLE_SECONDS = 10.0
_BOOT_IDLE_TIMEOUT = 300.0


def wait_for_boot_idle(
    inst: Instance,
    *,
    seconds: float = _BOOT_IDLE_SECONDS,
    timeout: float = _BOOT_IDLE_TIMEOUT,
    poll: float = 1.0,
) -> None:
    """Block until ``/api/active-chats`` has been empty for ``seconds`` in a row.

    A chat that keeps appearing is a schedule or triage getting through the
    clone prep; the run waits rather than aborting, because it is a timing
    question and a pilot should not die on one slow boot. A chat that is
    *still* running when the timeout expires is the opposite -- something is
    active that the measurement cannot attribute -- and that is a refusal.

    ``poll``, ``seconds`` and ``timeout`` take arguments so a test can drive a
    minute of wall clock in a millisecond; the driver uses the defaults.
    """
    deadline = time.monotonic() + timeout
    empty_since: float | None = None
    while time.monotonic() < deadline:
        if inst.active_chat_ids():
            empty_since = None
        elif empty_since is None:
            empty_since = time.monotonic()
        elif time.monotonic() - empty_since >= seconds:
            return
        time.sleep(poll)
    raise SandboxError(
        f"the agent-clone server still had an active chat after "
        f"{timeout:.0f}s; its writes cannot be attributed to a chat"
    )


# ── arms ─────────────────────────────────────────────────────────────────


@dataclass
class ArmResult:
    """One arm's outcome for one chat, plus what it changed in its clone."""

    chat_id: str
    status: str = "ok"
    seconds: float = 0.0
    error: str = ""
    cost_usd: float | None = None
    permission_cards: int = 0
    diff: dict = field(default_factory=dict)
    previous_sha: str = ""
    sha: str = ""
    usage: dict[str, int] = field(default_factory=dict)


def chat_usage(
    inst: Instance, agent_clone: Path, chat_id: str, provider: str
) -> dict[str, int]:
    """Token usage for one agent chat, or ``{}`` when it cannot be read.

    ``GET /api/chats`` carries no cost field, so the only honest source is
    Claude Code's own session transcript: the chat's ``session_id`` names a
    ``<session_id>.jsonl`` under the agent clone's project directory, and every
    assistant record in it reports ``message.usage``. That is the agent's real
    spend, read rather than estimated, which is what makes the report's cost
    column worth having.

    Claude only, deliberately. OpenCode keeps its own session store with a
    different shape, and guessing at it would put a number in the report that
    is not a measurement. An arm that cannot be read reports ``{}`` and the
    report prints ``-`` for it, which is the honest answer.
    """
    if provider != "claude":
        return {}
    session_id = str((inst.chat(chat_id) or {}).get("session_id") or "")
    if not session_id:
        return {}
    from ciao.transcripts import _claude_projects_dir

    return sum_claude_usage(_claude_projects_dir(agent_clone) / f"{session_id}.jsonl")


async def run_oneshot_arm(clone: Path, rows: list[Row], baseline: str) -> list[ArmResult]:
    """Today's production chain, in process, against the ``oneshot`` clone.

    ``config.workspace_root`` is asserted against the clone, and then every
    other path the arm was handed is asserted to be *inside* it, both before
    the first model call. Everything else here follows
    ``ArchivePipeline._job_inputs``: the same model resolution, the same
    workspace guide, the same proposal vault root. If either assertion ever
    fails the arm aborts rather than writing to the live workspace -- that is
    the whole safety argument.
    """
    from ciao import critique, job_runs, proposal_outcomes
    from ciao.insights import extract_and_append
    from ciao.workspace_guide import guide_path

    config = clone_config(clone)
    # `apply_app_settings_overlay` prefers `os.environ["CIAO_RUNTIME_ROOT"]` over
    # the config it is handed, so pin it to the clone for the call: a stray
    # value in the operator's shell would otherwise load the *live* Settings
    # and quietly decide this arm's model and flags.
    previous_runtime = os.environ.get("CIAO_RUNTIME_ROOT")
    os.environ["CIAO_RUNTIME_ROOT"] = str(clone / ".runtime")
    try:
        critique.apply_app_settings_overlay(config)
    finally:
        if previous_runtime is None:
            os.environ.pop("CIAO_RUNTIME_ROOT", None)
        else:
            os.environ["CIAO_RUNTIME_ROOT"] = previous_runtime
    config.insights_enabled = True
    job_runs.configure(clone / ".runtime")
    proposal_outcomes.configure(clone / ".runtime")
    if config.workspace_root.resolve() != clone.resolve():
        raise SandboxError(
            f"refusing to run the one-shot arm: config resolves to "
            f"{config.workspace_root}, not the clone {clone}"
        )
    # The stronger half of the same argument: not only is the workspace root
    # the clone, every path the arm was handed must be inside it too. An
    # absolute `vault_root`, a workspace `vault_root` or a project doc that
    # escapes the clone is refused here, before the first model call.
    assert_clone_containment(config, clone, rows)
    print(f"oneshot: config pinned to {config.workspace_root}")

    results: list[ArmResult] = []
    # Each chat's diff is against the previous commit *in this clone*, so the
    # numbers are per chat rather than cumulative. Sequential processing is
    # what buys that; see "Out of scope" in the plan.
    previous = baseline
    for row in rows:
        result = ArmResult(chat_id=row.chat_id, previous_sha=previous)
        started = time.monotonic()
        try:
            await extract_and_append(
                archive_path=clone / row.archive_rel,
                filtered_jsonl="",
                config=config,
                model=row.model,
                workspace_root=config.workspace_root,
                vault_root=config.vault_root,
                proposal_vault_root=config.workspace_vault_root(row.workspace),
                guide_path=guide_path(config.agent_root(row.workspace)),
                trajectories_enabled=False,
                memory_proposals_enabled=True,
                provider=row.effective_provider,
                project_doc_path=row.doc,
                text_mode=True,
            )
        except Exception as exc:  # noqa: BLE001 - one chat must not end the arm
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"[:300]
        result.seconds = round(time.monotonic() - started, 1)
        result.sha = commit_snapshot(clone, f"oneshot {row.chat_id}")
        previous = result.sha
        result.diff = diff_summary(clone, result.previous_sha, result.sha)
        results.append(result)
        print(
            f"[oneshot] {row.chat_id} {result.status} {result.seconds}s "
            f"files={len(result.diff.get('added', []))}a/"
            f"{len(result.diff.get('modified', []))}m queued={result.diff.get('queued', 0)}",
            flush=True,
        )
    return results


async def _wait_for_turn(inst: Instance, chat_id: str, timeout_s: float) -> tuple[bool, int, float]:
    """Wait for one chat turn to settle.

    Same shape as ``scripts/surface-compare/run_sessions.py`` (~L318) with one
    change: a ``pending_permission`` is **not** approved. A card is recorded
    and the chat is stopped, because approving it would let the agent do
    something the diff could not explain and the run could not undo.
    """
    deadline = time.monotonic() + timeout_s
    seen_active = False
    cards = 0
    started = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        active = chat_id in inst.active_chat_ids()
        chat = inst.chat(chat_id) or {}
        if str(chat.get("pending_permission") or "").strip():
            cards += 1
            try:
                inst.post(f"/api/chats/{chat_id}/stop")
            except Exception:  # noqa: BLE001 - the card is the finding
                pass
            return True, cards, round(time.monotonic() - started, 1)
        if active:
            seen_active = True
            continue
        if seen_active or time.monotonic() - started > 20:
            await asyncio.sleep(2.0)
            if chat_id not in inst.active_chat_ids():
                return True, cards, round(time.monotonic() - started, 1)
    return False, cards, round(time.monotonic() - started, 1)


def create_harness_projects(
    inst: Instance, rows: list[Row]
) -> dict[str, tuple[str, str]]:
    """One "Insights sandbox" project per selected workspace.

    Returns ``{workspace: (project_id, name)}``. The projects have to exist
    before the first chat, and creating them writes rows into
    ``.runtime/web_projects.json`` in the clone -- so this is deliberately *not*
    called from inside the arm's chat loop, where the write would be charged to
    chat 1. ``run_arms`` creates them, commits, and only then starts the arm.
    """
    projects: dict[str, tuple[str, str]] = {}
    for workspace in sorted({row.workspace for row in rows}):
        created = inst.post(
            "/api/projects",
            {"name": "Insights sandbox", "workspace": workspace},
        )
        # The 201 body is the project dict; there is no GET for one project,
        # only PATCH/DELETE, so the name comes from where it was created.
        projects[workspace] = (
            str(created["project_id"]),
            str(created.get("name") or "none"),
        )
    print(
        "agent: project per workspace "
        + str({ws: pid for ws, (pid, _) in projects.items()})
    )
    return projects


async def run_agent_arm(
    inst: Instance,
    agent_clone: Path,
    rows: list[Row],
    projects: dict[str, tuple[str, str]],
    setup_sha: str,
    *,
    turn_timeout: float,
) -> list[ArmResult]:
    """A real attended chat per archive, on the agent clone.

    Attended, not scheduled: an unattended chat is told to defer facts it is
    not sure about (``memory_policy.py`` ~L118-134), which would bias the
    comparison against the arm that is being measured.

    ``setup_sha`` is the *setup* snapshot, not the pre-boot one. Booting the
    server writes to the clone before any chat exists -- a regenerated
    ``INDEX.md``, the harness's own project rows in ``web_projects.json`` --
    and against the pre-boot baseline all of that lands in chat 1's diff,
    which would make the first chat look busier than it was. ``run_arms``
    commits boot and then ``harness: project setup``, so chat 1 is measured
    from a clone that already contains its own scaffolding.
    """
    project_ids = {workspace: pid for workspace, (pid, _) in projects.items()}
    project_names = {workspace: name for workspace, (_, name) in projects.items()}

    results: list[ArmResult] = []
    previous = setup_sha
    for row in rows:
        result = ArmResult(chat_id=row.chat_id, previous_sha=previous)
        started = time.monotonic()
        try:
            chat = inst.post(
                f"/api/projects/{project_ids[row.workspace]}/chats",
                {
                    "title": f"sandbox {row.chat_id}",
                    "mode": "bypass",
                    "provider": row.effective_provider,
                    "model": row.model,
                },
            )
            chat_id = str(chat["chat_id"])
            # `clone_doc` rather than `agent_clone / row.doc`: an absolute
            # `vault_doc_path` survives the join, so `Path / "/elsewhere/x.md"`
            # would hand the agent a doc outside the clone. Containment has
            # already refused such a run, so what reaches here is inside the
            # clone -- and this keeps the prompt honest about that.
            doc = str(clone_doc(agent_clone, row.doc)) if row.doc else "none"
            inst.post(
                f"/api/chats/{chat_id}/prompt",
                {
                    "prompt": AGENT_PROMPT.format(
                        archive=agent_clone / row.archive_rel,
                        project=project_names[row.workspace],
                        doc=doc,
                    )
                },
            )
            settled, cards, seconds = await _wait_for_turn(inst, chat_id, turn_timeout)
            result.permission_cards = cards
            result.seconds = seconds
            if not settled:
                result.status = "timeout"
                result.error = f"turn did not settle within {turn_timeout:.0f}s"
                try:
                    inst.post(f"/api/chats/{chat_id}/stop")
                except Exception:  # noqa: BLE001
                    pass
            elif cards:
                result.status = "permission"
                result.error = (
                    f"{cards} permission card(s) were not approved; the chat was stopped"
                )
            # `GET /api/chats` carries no cost today; record None rather than
            # inventing a number the report would then total.
            cost = (inst.chat(chat_id) or {}).get("cost_usd")
            result.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
            result.usage = chat_usage(
                inst, agent_clone, chat_id, row.effective_provider
            )
        except Exception as exc:  # noqa: BLE001 - one chat must not end the arm
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"[:300]
            # A chat that blew up after four minutes is not a 0-second chat.
            result.seconds = round(time.monotonic() - started, 1)
        result.sha = commit_snapshot(agent_clone, f"agent {row.chat_id}")
        previous = result.sha
        result.diff = diff_summary(agent_clone, result.previous_sha, result.sha)
        results.append(result)
        print(
            f"[agent] {row.chat_id} {result.status} {result.seconds}s "
            f"cards={result.permission_cards} "
            f"files={len(result.diff.get('added', []))}a/"
            f"{len(result.diff.get('modified', []))}m queued={result.diff.get('queued', 0)}",
            flush=True,
        )
    return results


# ── report ───────────────────────────────────────────────────────────────


def _median(values: list[float]) -> str:
    return f"{statistics.median(values):.1f}" if values else "-"


# Every token counter, so an arm that reports usage does so consistently and a
# reader can compare cache reads against fresh input rather than one opaque
# "tokens" number. A provider the harness cannot read shows `-` on the total
# and on each field, rather than a zero that would read as "this chat was free".
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def usage_total(usage: dict[str, int]) -> int:
    """Every counted token in one usage row, or 0 when there is no row."""
    return sum(int(value) for value in (usage or {}).values())


def usage_cell(usage: dict[str, int] | None, counter: str) -> str:
    """One token counter for the report, or ``-`` when it was not read."""
    if not usage:
        return "-"
    return f"{int(usage.get(counter, 0)):,}"


def _sum_usage(results: list[ArmResult]) -> dict[str, int]:
    """Every chat's usage added up, or ``{}`` when no chat reported any.

    An arm where *some* chats reported usage totals only those, because a
    total that silently skipped unread ones would understate the run; the
    per-chat column is where that gap is visible.
    """
    total: dict[str, int] = {}
    for result in results:
        for counter, value in (result.usage or {}).items():
            total[counter] = total.get(counter, 0) + int(value)
    return total


def render_report(
    run_id: str,
    live: Path,
    rows: list[Row],
    arms: dict[str, list[ArmResult]],
    started: str,
) -> str:
    """The run as Markdown: totals per arm, then a per-chat table.

    Counts only, and no verdict. Which arm's writes are better is a judgement
    about real memory, and a summary that made it would be the thing under
    test. Per-chat diffs are written beside this file; the report is the index
    into them.
    """
    lines = [
        "# Insights sandbox report",
        "",
        f"Run: `{run_id}`. Started {started}. Live workspace: `{live}`.",
        "",
        f"Chats: {len(rows)}. Arms: {', '.join(arms) or 'none'}.",
        "",
        "## Totals",
        "",
        "| arm | chats | ok | errors | timeouts | permission cards | "
        "median s | added | modified | deleted | vault | guide | proposals | "
        "other | queued | cost | tokens | in | out | cache read | cache write |",
        "|---" * 21 + "|",
    ]
    for arm, results in arms.items():
        ok = [r for r in results if r.status == "ok"]
        seconds = [r.seconds for r in ok]
        by_class: dict[str, int] = {}
        for result in results:
            for group, count in (result.diff.get("by_class") or {}).items():
                by_class[group] = by_class.get(group, 0) + int(count)
        cost = sum(r.cost_usd for r in results if r.cost_usd is not None)
        # An arm whose provider cannot be read has no usage at all, so all five
        # token cells are `-`: a zero would read as "this chat was free".
        usage = _sum_usage(results)
        cells = [
            arm,
            str(len(results)),
            str(len(ok)),
            str(sum(1 for r in results if r.status == "error")),
            str(sum(1 for r in results if r.status == "timeout")),
            str(sum(r.permission_cards for r in results)),
            _median(seconds),
            str(sum(len(r.diff.get("added", [])) for r in results)),
            str(sum(len(r.diff.get("modified", [])) for r in results)),
            str(sum(len(r.diff.get("deleted", [])) for r in results)),
            str(by_class.get("vault", 0)),
            str(by_class.get("guide", 0)),
            str(by_class.get("proposals", 0)),
            str(by_class.get("other", 0)),
            str(sum(int(r.diff.get("queued") or 0) for r in results)),
            f"${cost:.4f}" if any(r.cost_usd is not None for r in results) else "-",
            f"{usage_total(usage):,}" if usage else "-",
            *(usage_cell(usage, counter) for counter in _TOKEN_FIELDS),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "`logs` changes are excluded from every count above: transcripts are "
        "written by the server by design, and the archives are inputs rather "
        "than output.",
        "",
        "Token columns are read from each chat's own Claude session transcript "
        "(`message.usage`, deduped by `message.id`), not estimated. A chat "
        "whose provider is not claude, or whose session transcript is "
        "unreadable, shows `-`.",
        "",
        "## Per chat",
        "",
    ]
    header = ["chat", "provider", "model"]
    for arm in arms:
        header += [
            f"{arm} status",
            f"{arm} s",
            f"{arm} a/m/d",
            f"{arm} queued",
            f"{arm} tokens",
            f"{arm} diff",
        ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|---" * len(header) + "|")
    for row in rows:
        cells = [row.chat_id, row.provider, row.model or "-"]
        for arm, results in arms.items():
            match = next((r for r in results if r.chat_id == row.chat_id), None)
            if match is None:
                cells += ["-", "-", "-", "-", "-", "-"]
                continue
            counts = (
                f"{len(match.diff.get('added', []))}/"
                f"{len(match.diff.get('modified', []))}/"
                f"{len(match.diff.get('deleted', []))}"
            )
            cells += [
                match.status,
                f"{match.seconds:.1f}",
                counts,
                str(match.diff.get("queued", 0)),
                f"{usage_total(match.usage):,}" if match.usage else "-",
                f"diffs/{arm}/{match.chat_id}.diff",
            ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "## Notes", ""]
    for arm, results in arms.items():
        for result in results:
            if result.error:
                lines.append(f"- `{arm}` {result.chat_id}: {result.error}")
    lines.append("")
    return "\n".join(lines)


def write_diffs(run_dir: Path, clone: Path, arm: str, results: list[ArmResult]) -> None:
    """One full diff per chat per arm, so a count can always be read in context."""
    out = run_dir / "diffs" / arm
    out.mkdir(parents=True, exist_ok=True)
    for result in results:
        diff = subprocess.run(
            ["git", "-C", str(clone), "diff", result.previous_sha, result.sha],
            capture_output=True,
            text=True,
        ).stdout
        (out / f"{result.chat_id}.diff").write_text(diff, encoding="utf-8")
        stat = subprocess.run(
            ["git", "-C", str(clone), "diff", "--stat", result.previous_sha, result.sha],
            capture_output=True,
            text=True,
        ).stdout.strip()
        (out / f"{result.chat_id}.stat").write_text(stat + "\n", encoding="utf-8")


# ── driver ───────────────────────────────────────────────────────────────


def set_up_clones(
    live: Path, run_dir: Path, sandbox_root: Path, rows: list[Row], *, stock: Path
) -> tuple[dict[str, Path], dict[str, str], list[str]]:
    """Clone, neutralise, strip and baseline all three copies of the workspace.

    ``base`` exists so the report has something to diff against that neither
    arm produced; ``oneshot`` and ``agent`` are the two measurements.
    """
    clones = {
        name: assert_sandbox_path(
            run_dir / name, sandbox_root=sandbox_root, live=live
        )
        for name in ("base", "oneshot", "agent")
    }
    rel_archives = [str(row.archive_rel) for row in rows]
    baselines: dict[str, str] = {}
    all_notes: list[str] = []
    for name, clone in clones.items():
        clone_workspace(live, clone)
        notes = prepare_clone(clone, stock_schedules=stock, disable_insights=name == "agent")
        stripped = strip_archives(clone, rel_archives)
        baselines[name] = commit_snapshot(clone, "harness: baseline")
        notes.append(f"stripped {stripped} archive(s) of their insights sections")
        notes.append(f"baseline commit {baselines[name]}")
        all_notes.append(f"[{name}] {clone}")
        all_notes.extend(f"[{name}] {note}" for note in notes)
        print(f"{name}: prepared {clone} (baseline {baselines[name][:8]})")
    return clones, baselines, all_notes


async def run_arms(
    clones: dict[str, Path],
    baselines: dict[str, str],
    run_dir: Path,
    rows: list[Row],
    arms: list[str],
    *,
    port: int,
    turn_timeout: float,
) -> dict[str, list[ArmResult]]:
    """Start the agent-clone server, resolve the project docs, run the arms.

    The server is started before either arm, because the project map comes
    from it, and it is stopped in a ``finally`` so an exception in one chat
    cannot leave a process listening on the operator's loopback with a
    full-access agent on the other end. ``start_server`` additionally stops it
    itself if it never got far enough to hand it back.

    Once the server answers, boot is *waited out* and then *committed*. A boot
    that finds work to do writes to the clone -- a regenerated ``INDEX.md``, a
    schedule that fired before its disable took effect -- and with the
    pre-boot baseline as chat 1's ``previous``, that setup lands in the first
    chat's numbers. Waiting for a quiet window and then committing
    ``harness: server boot`` puts that work in its own commit; a second
    ``harness: project setup`` commit does the same for the harness's own
    projects, which exist before chat 1 for the same reason. Chat 1 is
    therefore only ever measured against a clone that was already running.
    """
    results: dict[str, list[ArmResult]] = {}
    proc: subprocess.Popen | None = None
    try:
        proc, inst, base_url = start_server(clones["agent"], run_dir, port, rows)
        print(f"agent server up on {base_url}")
        wait_for_boot_idle(inst)
        agent_baseline = commit_snapshot(clones["agent"], "harness: server boot")
        docs = project_docs(clones["agent"], inst, rows)
        for row in rows:
            row.doc = docs.get(row.chat_id, "")
        # The doc half of the containment check, which start_server could not
        # do: a project's `vault_doc_path` is only knowable once the project map
        # has been read from the booted server, and it may name a vault outside
        # the workspace. Refused here it stops the run before either arm's
        # first model call.
        assert_clone_containment(
            clone_config(clones["agent"]), clones["agent"], rows
        )
        (run_dir / "selection.json").write_text(
            json.dumps(
                [
                    {
                        "chat_id": row.chat_id,
                        "provider": row.provider,
                        "effective_provider": row.effective_provider,
                        "model": row.model,
                        "workspace": row.workspace,
                        "archive": str(row.archive_rel),
                        "doc": row.doc,
                    }
                    for row in rows
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if "agent" in arms:
            # The harness's own projects, created and committed as setup, so
            # the rows they add to `.runtime/web_projects.json` are not part
            # of chat 1's diff.
            projects = create_harness_projects(inst, rows)
            agent_baseline = commit_snapshot(
                clones["agent"], "harness: project setup"
            )
        else:
            projects = {}
        if "oneshot" in arms:
            results["oneshot"] = await run_oneshot_arm(
                clones["oneshot"], rows, baselines["oneshot"]
            )
        if "agent" in arms:
            results["agent"] = await run_agent_arm(
                inst,
                clones["agent"],
                rows,
                projects,
                agent_baseline,
                turn_timeout=turn_timeout,
            )
    finally:
        if proc is not None:
            stop_server(proc)
            print("agent server stopped")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="insights-sandbox",
        description=(
            "Compare a full Ciaobot chat session against today's one-shot "
            "memory pass, each on its own disposable clone of the live workspace."
        ),
    )
    parser.add_argument("--live", default="~/repos/ciao", help="Live workspace to clone (read-only).")
    parser.add_argument("--sandbox-root", default="~/ciao-sandbox", help="Where clones are made. Nothing outside this tree is ever written.")
    parser.add_argument("--run-id", required=True, help="Run name; also the clone directory name, so it must not exist.")
    parser.add_argument(
        "--from-cache",
        action="append",
        default=None,
        help="#586 insights_compare cache dir holding the chat selection. Repeatable.",
    )
    parser.add_argument("--arms", default="oneshot,agent", help="Comma-separated arms to run.")
    parser.add_argument("--limit", type=int, default=0, help="Max chats; 0 means all.")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help=(
            "Loopback port for the agent-clone server; 0 (the default) picks a free "
            "one. An explicit port is refused if anything already listens on it."
        ),
    )
    parser.add_argument("--turn-timeout", type=float, default=900.0, help="Seconds to wait for one agent turn.")
    parser.add_argument("--live-url", default="http://127.0.0.1:8443", help="Live instance, for the idle check.")
    args = parser.parse_args()

    live = Path(args.live).expanduser().resolve()
    sandbox_root = Path(args.sandbox_root).expanduser().resolve()
    run_dir = assert_sandbox_path(
        Path(args.sandbox_root).expanduser() / args.run_id,
        sandbox_root=sandbox_root,
        live=live,
    )
    arms = [name for name in (a.strip() for a in args.arms.split(",")) if name]
    unknown = [name for name in arms if name not in ("oneshot", "agent")]
    if unknown:
        print(f"error: unknown arm(s) {', '.join(unknown)}", file=sys.stderr)
        return 2

    cache_dirs = (
        [Path(d).expanduser() for d in args.from_cache]
        if args.from_cache
        else [live / ".runtime" / "insights_compare" / rid for rid in DEFAULT_CACHE_RUN_IDS]
    )
    for cache_dir in cache_dirs:
        if not cache_dir.is_dir():
            print(f"error: cache dir not found: {cache_dir}", file=sys.stderr)
            return 2

    rows = select_rows(cache_dirs, live, args.limit)
    if not rows:
        print("error: no cached chat had an archive under the live workspace")
        return 1
    print(f"selected {len(rows)} chat(s) from {len(cache_dirs)} cache dir(s)")

    try:
        check_live_idle(args.live_url)
    except SandboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    run_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    stock = REPO_ROOT / "ciao" / "stock" / "schedules.json"
    clones, baselines, notes = set_up_clones(live, run_dir, sandbox_root, rows, stock=stock)
    (run_dir / "prep.log").write_text("\n".join(notes) + "\n", encoding="utf-8")

    for row in rows:
        row.doc = ""
    try:
        results = asyncio.run(
            run_arms(
                clones,
                baselines,
                run_dir,
                rows,
                arms,
                port=args.port,
                turn_timeout=args.turn_timeout,
            )
        )
    except SandboxError as exc:
        # A refusal -- a path outside a clone, a busy boot, a stranger's port
        # -- is an answer, not a crash. The server is stopped by `run_arms`'s
        # own `finally` on the way out, so printing the reason and exiting is
        # safe; a traceback here would bury the one line that says what to fix.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for arm, arm_results in results.items():
        write_diffs(run_dir, clones[arm], arm, arm_results)
    report = render_report(args.run_id, live, rows, results, started)
    report_path = run_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
