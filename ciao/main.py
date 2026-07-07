"""Entrypoint for the Ciaobot server."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from ciao.config import CiaoConfig
from ciao.git_sync import sync_workspace
from ciao.models import ChatContext
from ciao.schedules import ScheduleManager, ScheduleStore
from ciao.sessions import StateStore
from ciao.signals import RestartRequested
from ciao.transcripts import TranscriptStore
from ciao.upgrade import update_skills
from ciao.web.app import create_app
from ciao.error_log import setup_error_logging
from ciao.web.project_chats import ProjectChatManager
from ciao.web.push import PushManager

logger = logging.getLogger(__name__)


def _ensure_homebrew_on_path() -> None:
    """Prepend Homebrew bin dirs to PATH when missing.

    launchd launches the server with a minimal PATH (roughly
    ``/usr/bin:/bin:/usr/sbin:/sbin``) that omits Homebrew, so subprocess
    calls to ``npm``, ``node``, Homebrew's ``git``/``pip``, etc. fail with
    FileNotFoundError. Prepending the standard Homebrew directories lets the
    deploy/upgrade subprocess steps find those tools regardless of how the
    service was started. Adding a non-existent directory is harmless.
    """
    extra = ["/opt/homebrew/bin", "/usr/local/bin"]
    parts = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    prepend = [d for d in extra if d not in parts]
    if prepend:
        os.environ["PATH"] = os.pathsep.join([*prepend, *parts])


_PhaseStatus = Literal["pending", "in_progress", "done", "failed"]


@dataclass
class StartupPhase:
    name: str
    status: _PhaseStatus = "pending"
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class StartupTracker:
    _phases: dict[str, StartupPhase] = field(default_factory=dict)
    overall_ready: bool = False
    # Fired with the phase when it reaches a terminal state (done/failed).
    # Used to record system jobs into job_runs without coupling this class
    # to the recorder. Errors in the callback are swallowed by the caller.
    on_finish: Callable[[StartupPhase], None] | None = None

    def phase(self, name: str) -> StartupPhase:
        if name not in self._phases:
            self._phases[name] = StartupPhase(name=name)
        return self._phases[name]

    def start(self, name: str) -> None:
        p = self.phase(name)
        p.status = "in_progress"
        if p.started_at is None:
            p.started_at = datetime.now(UTC).isoformat()

    def done(self, name: str, message: str = "") -> None:
        p = self.phase(name)
        p.status = "done"
        p.message = message
        p.finished_at = datetime.now(UTC).isoformat()
        self._emit(p)
        self._update_ready()

    def fail(self, name: str, message: str = "") -> None:
        p = self.phase(name)
        p.status = "failed"
        p.message = message
        p.finished_at = datetime.now(UTC).isoformat()
        self._emit(p)
        self._update_ready()

    def _emit(self, p: StartupPhase) -> None:
        if self.on_finish is None:
            return
        try:
            self.on_finish(p)
        except Exception:  # noqa: BLE001 — never let recording break startup
            logger.debug("Startup phase callback failed", exc_info=True)

    def _update_ready(self) -> None:
        self.overall_ready = all(
            p.status in ("done", "failed") for p in self._phases.values()
        )

    def to_dict(self) -> dict:
        return {
            "phases": [asdict(p) for p in self._phases.values()],
            "overall_ready": self.overall_ready,
        }


def _refresh_vault_index(workspace: Path, vault_root: Path | None = None) -> bool:
    """Regenerate memory-vault/INDEX.md from frontmatter. Non-fatal on failure."""
    try:
        from ciao import vault_index

        root = vault_root or (workspace / "memory-vault")
        if not root.is_dir():
            # Bootstrap mode has no vault yet (the setup wizard creates it);
            # never scaffold one preemptively.
            logger.info("Vault root %s does not exist yet; skipping index refresh", root)
            return False
        entries = vault_index.scan_vault(root)
        vault_index.write_index_file(entries, root / "INDEX.md")
        logger.info("Vault index refreshed.")
        return True
    except Exception:
        logger.warning("Vault index refresh failed", exc_info=True)
        return False


def _rebuild_pwa(workspace: Path) -> bool:
    """Rebuild PWA frontend if web/ source exists."""
    web_dir = workspace / "web"
    if not (web_dir / "package.json").exists():
        return False
    try:
        subprocess.run(
            ["npm", "run", "build"],
            cwd=str(web_dir),
            capture_output=True,
            timeout=120,
        )
        logger.info("PWA frontend rebuilt.")
        return True
    except Exception:
        logger.exception("PWA frontend rebuild failed")
        return False


def _push_subject_from_env(env: dict[str, str] | None = None) -> str:
    """Web Push VAPID subject, or "" when CIAO_PUSH_CONTACT is unset.

    An empty subject means Web Push delivery stays disabled until the
    operator sets a contact in Settings; everything else keeps working.
    """
    source = env if env is not None else os.environ
    return source.get("CIAO_PUSH_CONTACT", "").strip()


def _push_subject_for_config(config: CiaoConfig) -> str:
    if getattr(config, "bootstrap_mode", False):
        return "mailto:bootstrap@localhost"
    return _push_subject_from_env()


async def _async_main() -> int:
    _ensure_homebrew_on_path()
    os.environ.setdefault("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND", "file")
    config = CiaoConfig.from_env()
    setup_error_logging(config.workspace_root)

    # Discover models installed on the local Ollama daemon (best-effort,
    # 2s timeout) and surface them in the model pickers alongside the cloud
    # allowlist. Models already in the cloud allowlist keep their cloud
    # routing so discovery never silently changes existing behaviour. Also
    # re-run whenever the Settings → Models tab loads (routes_api), so a
    # freshly pulled model shows up without a restart.
    from ciao.config import (
        refresh_local_ollama_models,
        refresh_cloud_ollama_models,
        refresh_openrouter_models,
    )

    refresh_local_ollama_models(config)
    refresh_cloud_ollama_models(config)
    refresh_openrouter_models(config)

    # Runtime-mutable settings overlay (PWA Settings → Models tab). Applied
    # on top of the env-backed config so PATCHes take effect without a
    # restart and survive one via .runtime/app_settings.json.
    from ciao.app_settings import AppSettingsStore

    app_settings = AppSettingsStore(config.state_path.parent / "app_settings.json")
    app_settings.apply_to_config(config)

    # Pin the job-run recorder to the same .runtime the config uses, then
    # route finished startup phases (sync, vault index, rebuild, ...) into it
    # so the Automation page can show system-task status.
    from ciao import job_runs

    job_runs.configure(config.state_path.parent)
    tracker = StartupTracker(on_finish=job_runs.record_startup_phase)

    # Start provider checks in the background
    tracker.start("connect_claude_code")

    async def check_claude_code():
        try:
            # Use the bundled Claude Code CLI (the same binary the provider
            # spawns) rather than a bare ``claude`` on PATH: under launchd
            # PATH omits ~/.local/bin, and the canonical CLI is the SDK's
            # bundled one anyway.
            from ciao.providers.claude import get_bundled_claude_path
            cli = get_bundled_claude_path() or shutil.which("claude")
            if not cli:
                tracker.fail(
                    "connect_claude_code",
                    "claude CLI not found (no bundled binary and not on PATH)",
                )
                return
            proc = await asyncio.create_subprocess_exec(
                cli, "--version",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            if proc.returncode == 0:
                version = stdout.decode().strip()
                tracker.done("connect_claude_code", f"connected: {version}")
            else:
                tracker.fail("connect_claude_code", f"failed: exit {proc.returncode}")
        except Exception as e:
            tracker.fail("connect_claude_code", f"not found: {e}")

    asyncio.create_task(check_claude_code())

    # Sync workspace before anything else
    if config.auto_sync_on_start:
        tracker.start("sync_workspace")
        try:
            await sync_workspace(config.workspace_root)
            tracker.done("sync_workspace")
        except Exception:
            tracker.fail("sync_workspace", "git sync failed")
            logger.exception("Workspace sync failed")

    # Refresh vault index after git pull so INDEX.md reflects any remote changes
    if config.auto_vault_index:
        tracker.start("refresh_vault_index")
        try:
            await asyncio.to_thread(_refresh_vault_index, config.workspace_root, config.vault_root)
            tracker.done("refresh_vault_index")
        except Exception:
            tracker.fail("refresh_vault_index", "index refresh failed")
            logger.exception("Vault index refresh failed")

    # Rebuild PWA frontend so served assets match latest source
    tracker.start("rebuild_pwa")
    try:
        await asyncio.to_thread(_rebuild_pwa, config.workspace_root)
        tracker.done("rebuild_pwa")
    except Exception:
        tracker.fail("rebuild_pwa", "npm build failed")
        logger.exception("PWA rebuild failed")

    # Update skills in the background, startup should not wait on npm.
    tracker.start("update_skills")

    def _skills_task():
        try:
            update_skills(str(config.workspace_root))
            tracker.done("update_skills")
        except Exception:
            tracker.fail("update_skills", "skill install failed")
            logger.exception("Skill update failed")

    asyncio.create_task(asyncio.to_thread(_skills_task))

    # Initialize stores
    state = StateStore(
        config.state_path,
        config.workspace_root,
        config.media_root,
        default_model=config.claude_default_model,
        default_mode=config.claude_mode,
    )
    transcript_root = config.vault_root / "Logs" / "Chats"
    transcripts = TranscriptStore(config.state_path.parent, transcript_root)

    schedule_store = ScheduleStore(config.state_path.parent, include_system=True)

    # Create ProjectChatManager
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=config.state_path.parent / "web_projects.json",
    )

    # Schedule manager with web-only dispatch
    async def _dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        return await pcm.dispatch_schedule(
            entry, entry.prompt, model, mode, provider,
            target_chat_id=target_chat_id,
        )

    def _prepare_chat(entry, prompt, model, mode, provider):
        return pcm.prepare_schedule_chat(entry, prompt, model, mode, provider)

    def _resolve_schedule_target(entry):
        # Empty entry.model / entry.mode means "use the current default".
        ctx = ChatContext(chat_id=0)
        model = entry.model or state.get_selected_model(ctx)
        mode = entry.mode or state.get_mode(ctx)
        # Schedule provider is optional ("" inherits target chat's provider
        # for fixed-chat schedules or "claude" for new chats via web_project_id).
        provider = entry.provider
        return ("claude", model, mode, provider)

    schedule_manager = ScheduleManager(
        store=schedule_store,
        resolve_target=_resolve_schedule_target,
        dispatch_to_web=_dispatch_to_web,
        prepare_chat=_prepare_chat,
        # A secondary instance (the local dev box) never dispatches, so the
        # cloud primary stays the single scheduler and automations don't
        # double-fire while both machines run.
        is_paused=lambda: not config.dispatch_schedules,
    )

    # Create and wire up web app
    app = create_app(config, app_settings=app_settings)
    app.state.startup_tracker = tracker
    app.state.schedule_manager = schedule_manager
    app.state.state_store = state
    app.state.transcript_store = transcripts
    app.state.project_chat_manager = pcm

    # Workspace git sync: every instance works on whatever branch the
    # workspace checkout is on and syncs it via the Settings button (clean
    # pull -> direct push; conflict -> interactive resolution chat).
    from ciao.local_session import LocalSessionManager

    app.state.local_session_manager = LocalSessionManager(
        workspace=config.workspace_root,
        runtime_root=config.state_path.parent,
        dev_mode=config.dev_mode,
    )
    push_subject = _push_subject_for_config(config)
    if not push_subject:
        logger.info(
            "CIAO_PUSH_CONTACT is not set; Web Push notifications stay "
            "disabled until a contact is configured in Settings."
        )
    app.state.push_manager = PushManager(config.state_path.parent, subject=push_subject)
    app.state.focused_chats = {}
    pcm._push_manager = app.state.push_manager

    # Wire push delivery into the broker drive task so a successful turn
    # notifies subscribed devices even when no WebSocket client is connected.
    def _notify_result(chat_id: str, title: str, snippet: str) -> None:
        focused = app.state.focused_chats.get(chat_id, 0)
        if focused > 0:
            return  # someone has the chat open in foreground; skip OS push
        pm = app.state.push_manager
        if pm is None:
            return
        payload = {
            "title": title or "Ciaobot",
            "body": snippet or "New message",
            "chat_id": chat_id,
        }
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, pm.send, payload)
        except Exception:
            logger.exception("Failed scheduling push send for %s", chat_id)

    pcm.notify_result_cb = _notify_result

    # Permission-approval pushes: same plumbing as _notify_result but fired
    # immediately (no coalesce delay) because the turn is parked on the
    # user's answer. Re-fires every 30 seconds up to 3 times so a missed
    # notification on a locked device doesn't leave the turn hanging forever.
    def _notify_permission(
        chat_id: str, tool_name: str, message: str, request_id: str
    ) -> None:
        focused = app.state.focused_chats.get(chat_id, 0)
        if focused > 0:
            return  # user is watching the chat; in-app bubble is enough
        pm = app.state.push_manager
        if pm is None:
            return
        body = f"{tool_name}: {message}" if tool_name else message
        payload = {
            "title": "Ciaobot needs approval",
            "body": body or "Tool approval required",
            "chat_id": chat_id,
        }
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, pm.send, payload)
        except Exception:
            logger.exception("Failed scheduling permission push for %s", chat_id)
            return

        async def _refire() -> None:
            for _ in range(3):
                await asyncio.sleep(30)
                # Check if the request is still pending in the gate.
                provider_service = pcm._providers.get(chat_id)
                if provider_service is None or provider_service.provider is None:
                    break
                gate = getattr(provider_service.provider, "permission_gate", None)
                if gate is None or not gate.has_pending(request_id):
                    break
                if app.state.focused_chats.get(chat_id, 0) > 0:
                    break
                try:
                    loop.run_in_executor(None, pm.send, payload)
                except Exception:
                    logger.exception("Permission re-fire failed for %s", chat_id)

        try:
            loop.create_task(_refire())
        except Exception:
            logger.exception("Failed to schedule permission re-fire for %s", chat_id)

    pcm.notify_permission_cb = _notify_permission

    # Question pushes: fired when the model uses AskUserQuestion. The headless
    # CLI auto-cancels with empty answers, so we nudge the user to answer in
    # the next turn.
    def _notify_question(chat_id: str, question_text: str) -> None:
        focused = app.state.focused_chats.get(chat_id, 0)
        if focused > 0:
            return
        pm = app.state.push_manager
        if pm is None:
            return
        payload = {
            "title": "Ciaobot has a question",
            "body": question_text or "The model needs your input",
            "chat_id": chat_id,
        }
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, pm.send, payload)
        except Exception:
            logger.exception("Failed scheduling question push for %s", chat_id)

    pcm.notify_question_cb = _notify_question


    schedule_manager.start()

    # Fire any schedules whose target time already passed today but which
    # never triggered (e.g. due to a crash loop or the server being down
    # at the scheduled minute). Runs once, asynchronously, so it doesn't
    # block uvicorn from serving requests.
    async def _run_catch_up() -> None:
        try:
            fired = await schedule_manager.catch_up()
            if fired:
                logger.info("Schedule catch-up fired %d schedule(s): %s",
                            len(fired), ", ".join(fired))
        except Exception:
            logger.exception("Schedule catch-up failed")

    asyncio.create_task(_run_catch_up())

    # ── Workspace branch backup ──────────────────────────────
    # Every instance works on whatever branch the workspace checkout is on;
    # Ciaobot never creates or switches branches. A background loop pushes the
    # branch for backup. Non-git workspaces (fresh `ciao setup`) and repos
    # without an `origin` remote skip this gracefully.
    from ciao.local_session import (
        BACKUP_PUSH_INTERVAL,
        has_origin_remote,
        push_branch,
        workspace_branch,
    )

    async def _branch_backup_loop() -> None:
        branch = await asyncio.to_thread(workspace_branch, config.workspace_root)
        if branch is None:
            logger.info(
                "Workspace %s is not a git repository (or is on a detached HEAD); "
                "skipping branch backup.", config.workspace_root,
            )
            return
        if not await asyncio.to_thread(has_origin_remote, config.workspace_root):
            logger.info(
                "Workspace has no 'origin' remote; skipping branch backup.",
            )
            return
        logger.info("Working on branch '%s'", branch)
        while True:
            try:
                await asyncio.sleep(BACKUP_PUSH_INTERVAL)
                async with job_runs.track(
                    "branch_backup", "Branch backup",
                    category="system", extra={"branch": branch},
                ) as run:
                    ok, detail = await push_branch(config.workspace_root, branch=branch)
                    if not ok:
                        run.status = "error"
                        run.error = detail
                        logger.warning("Branch backup push failed: %s", detail)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Branch backup push failed")

    asyncio.create_task(_branch_backup_loop())


    import uvicorn

    uvi_config = uvicorn.Config(
        app,
        host=config.pwa_host,
        port=config.pwa_port,
        log_level="info",
    )
    server = uvicorn.Server(uvi_config)
    tracker.start("server_starting")
    logger.info("Starting Ciaobot server on %s:%d", config.pwa_host, config.pwa_port)
    tracker.done("server_starting")

    restart_flag: list[int | None] = [None]

    def request_restart(code: int) -> None:
        restart_flag[0] = code
        asyncio.create_task(server.shutdown())
        # asyncio.run's cleanup phase (cancel tasks, shut down the default
        # executor) can wedge after uvicorn drains: leaked Claude SDK
        # subprocess transports and synchronous urllib calls in the
        # heartbeat thread both hold the loop open indefinitely. The bash
        # wrapper then sits in `wait` forever and the service appears alive
        # but is unreachable. If we haven't exited cleanly within the grace
        # window, force it so the wrapper sees the exit code and restarts.
        def _force_exit() -> None:
            time.sleep(15)
            os._exit(code)
        threading.Thread(
            target=_force_exit, daemon=True, name="ciao-restart-watchdog"
        ).start()

    app.state.request_restart = request_restart

    async def _shutdown_providers() -> None:
        # Disconnect every active provider before uvicorn finishes its
        # lifespan shutdown. Otherwise the Claude SDK subprocess transports
        # outlive the loop and asyncio.run wedges in its cleanup phase
        # (cancelled tasks + open subprocess transports = no exit). Bounded
        # in parallel so one stuck provider can't block the rest.
        services = list(pcm._providers.values())
        pcm._providers.clear()
        async def _one(svc):
            try:
                await asyncio.wait_for(svc.disconnect(), timeout=3)
            except Exception:
                logger.exception("Provider disconnect failed during shutdown")
        if services:
            await asyncio.gather(*(_one(s) for s in services), return_exceptions=True)

    app.add_event_handler("shutdown", _shutdown_providers)

    try:
        await server.serve()
    except RestartRequested as exc:
        return int(exc.args[0]) if exc.args else config.restart_exit_code
    if restart_flag[0] is not None:
        return restart_flag[0]
    return 0


def main() -> None:
    """CLI entrypoint."""
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
