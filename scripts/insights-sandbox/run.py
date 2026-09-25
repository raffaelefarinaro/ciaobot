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
    assert_sandbox_path,
    clone_workspace,
    commit_snapshot,
    diff_summary,
    prepare_clone,
    strip_archives,
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


# ── the agent-clone server ───────────────────────────────────────────────


def start_server(
    agent_clone: Path, run_dir: Path, port: int
) -> tuple[subprocess.Popen, Instance, str]:
    """Start a second Ciaobot server on the agent clone; return it and a client.

    Bound to loopback with a fresh random token: the harness is driving a local
    automation, and a fixed token in a source file is a credential in a
    repository. Started *before* either arm because the project map comes from
    it, and the one-shot arm runs against a different clone, so there is no
    overlap in what either one touches.
    """
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
    log = (run_dir / "agent-server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ciao.cli", "run"],
        cwd=agent_clone,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SandboxError(
                f"the agent-clone server exited with {proc.returncode}; "
                f"see {run_dir / 'agent-server.log'}"
            )
        try:
            if httpx.get(f"{base_url}/api/active-chats", timeout=3.0).status_code == 200:
                break
        except Exception:  # noqa: BLE001 - not up yet
            pass
        time.sleep(1.0)
    else:
        raise SandboxError(
            f"the agent-clone server did not answer within 120s; "
            f"see {run_dir / 'agent-server.log'}"
        )
    return proc, Instance(base_url, token), base_url


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


async def run_oneshot_arm(clone: Path, rows: list[Row], baseline: str) -> list[ArmResult]:
    """Today's production chain, in process, against the ``oneshot`` clone.

    ``config.workspace_root`` is asserted against the clone before the first
    model call. Everything else here follows ``ArchivePipeline._job_inputs``:
    the same model resolution, the same workspace guide, the same proposal
    vault root. If the assertion ever fails the arm aborts rather than
    writing to the live workspace -- that is the whole safety argument.
    """
    from ciao import critique, job_runs, proposal_outcomes
    from ciao.config import CiaoConfig
    from ciao.insights import extract_and_append
    from ciao.workspace_guide import guide_path

    env = {
        "CIAO_WORKSPACE": str(clone),
        "CIAO_RUNTIME_ROOT": str(clone / ".runtime"),
        "PWA_AUTH_TOKEN": "sandbox",
    }
    config = CiaoConfig.from_env(env)
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


async def run_agent_arm(
    inst: Instance,
    agent_clone: Path,
    rows: list[Row],
    baseline: str,
    *,
    turn_timeout: float,
) -> list[ArmResult]:
    """A real attended chat per archive, on the agent clone.

    Attended, not scheduled: an unattended chat is told to defer facts it is
    not sure about (``memory_policy.py`` ~L118-134), which would bias the
    comparison against the arm that is being measured.
    """
    project_ids: dict[str, str] = {}
    project_names: dict[str, str] = {}
    for workspace in sorted({row.workspace for row in rows}):
        created = inst.post(
            "/api/projects",
            {"name": "Insights sandbox", "workspace": workspace},
        )
        project_ids[workspace] = str(created["project_id"])
        # The 201 body is the project dict; there is no GET for one project,
        # only PATCH/DELETE, so the name comes from where it was created.
        project_names[workspace] = str(created.get("name") or "none")
    print(f"agent: project per workspace {project_ids}")

    results: list[ArmResult] = []
    previous = baseline
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
            doc = str(agent_clone / row.doc) if row.doc else "none"
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
        "other | queued | cost |",
        "|---" * 17 + "|",
    ]
    for arm, results in arms.items():
        ok = [r for r in results if r.status == "ok"]
        seconds = [r.seconds for r in ok]
        by_class: dict[str, int] = {}
        for result in results:
            for group, count in (result.diff.get("by_class") or {}).items():
                by_class[group] = by_class.get(group, 0) + int(count)
        cost = sum(r.cost_usd for r in results if r.cost_usd is not None)
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
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "`logs` changes are excluded: transcripts are written by the server by "
        "design, and the archives are inputs rather than output.",
        "",
        "## Per chat",
        "",
    ]
    header = ["chat", "provider", "model"]
    for arm in arms:
        header += [f"{arm} status", f"{arm} s", f"{arm} a/m/d", f"{arm} queued", f"{arm} diff"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|---" * len(header) + "|")
    for row in rows:
        cells = [row.chat_id, row.provider, row.model or "-"]
        for arm, results in arms.items():
            match = next((r for r in results if r.chat_id == row.chat_id), None)
            if match is None:
                cells += ["-", "-", "-", "-", "-"]
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
    full-access agent on the other end.
    """
    results: dict[str, list[ArmResult]] = {}
    proc: subprocess.Popen | None = None
    try:
        proc, inst, base_url = start_server(clones["agent"], run_dir, port)
        print(f"agent server up on {base_url}")
        assert_no_remote(clones["agent"])
        docs = project_docs(clones["agent"], inst, rows)
        for row in rows:
            row.doc = docs.get(row.chat_id, "")
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
        if "oneshot" in arms:
            results["oneshot"] = await run_oneshot_arm(
                clones["oneshot"], rows, baselines["oneshot"]
            )
        if "agent" in arms:
            results["agent"] = await run_agent_arm(
                inst,
                clones["agent"],
                rows,
                baselines["agent"],
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
    parser.add_argument("--port", type=int, default=8543, help="Loopback port for the agent-clone server.")
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

    for arm, arm_results in results.items():
        write_diffs(run_dir, clones[arm], arm, arm_results)
    report = render_report(args.run_id, live, rows, results, started)
    report_path = run_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
