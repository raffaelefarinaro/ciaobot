"""Entrypoint for the Ciaobot server."""

from __future__ import annotations

import asyncio
import logging
import os
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
from ciao.schedules import ScheduleManager, ScheduleStore, migrate_loops
from ciao.sessions import StateStore
from ciao.signals import RestartRequested
from ciao.transcripts import TranscriptStore
from ciao.upgrade import update_skills
from ciao.web.app import create_app
from ciao.error_log import install_asyncio_noise_filter, setup_error_logging
from ciao.web.project_chats import ProjectChatManager
from ciao.web.push import PushManager

logger = logging.getLogger(__name__)


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


def _refresh_vault_index(
    workspace: Path,
    vault_root: Path | None = None,
    targets: list[tuple[Path, str, Path]] | None = None,
) -> bool:
    """Regenerate each vault's INDEX.md from frontmatter. Non-fatal on failure.

    ``targets`` comes from ``CiaoConfig.vault_scan_targets()``: one shared vault
    before the re-rooting and one per agent root after it. Without it this wrote
    a single index at ``config.vault_root``, which on a migrated install is a
    directory that no longer exists — so every root's index went stale and the
    startup log said only "does not exist yet; skipping".
    """
    try:
        from ciao import vault_index

        if targets:
            written = 0
            for root, workspace_name, _prefix in targets:
                if not Path(root).is_dir():
                    logger.info("Vault root %s does not exist yet; skipping", root)
                    continue
                # Each root owns exactly one vault, so its index carries no
                # workspace prefix; the stamp keeps `Entry.workspace` correct for
                # anything reading the index back.
                entries = vault_index.scan_vault(root, workspace=workspace_name)
                vault_index.write_index_file(entries, Path(root) / "INDEX.md")
                written += 1
            if not written:
                return False
            logger.info("Vault index refreshed for %d root(s).", written)
            return True

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


# Web Push (RFC 8292) requires a VAPID "sub" contact URI, but the push
# service never verifies or contacts it. For a localhost/personal app there's
# no reason to make the user supply a real email, so default to a placeholder
# and let CIAO_PUSH_CONTACT override it. This keeps web-push notifications
# working out of the box (previously an unset contact silently disabled them).
DEFAULT_PUSH_SUBJECT = "mailto:ciaobot@localhost"


def _push_subject_from_env(env: dict[str, str] | None = None) -> str:
    """Web Push VAPID subject; falls back to the localhost placeholder.

    A real contact is optional (set CIAO_PUSH_CONTACT to override); the push
    service only needs a syntactically valid mailto/https URI.
    """
    source = env if env is not None else os.environ
    return source.get("CIAO_PUSH_CONTACT", "").strip() or DEFAULT_PUSH_SUBJECT


def _push_subject_for_config(config: CiaoConfig) -> str:
    if getattr(config, "bootstrap_mode", False):
        return "mailto:bootstrap@localhost"
    return _push_subject_from_env()


def _open_browser_when_ready(url: str) -> None:
    """Open the first-run setup wizard in the default browser.

    Waits (in a daemon thread) until the server answers so the tab never
    lands on a connection error. Interactive first runs only: skipped when
    stderr is not a TTY (launchd, CI, redirected logs) or when
    CIAO_NO_BROWSER is set.
    """
    if os.environ.get("CIAO_NO_BROWSER"):
        return
    try:
        if not sys.stderr.isatty():
            return
    except (AttributeError, ValueError):
        return

    def _wait_and_open() -> None:
        import urllib.error
        import urllib.request
        import webbrowser

        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url, timeout=1)
            except urllib.error.HTTPError:
                pass  # any HTTP response means the server is up
            except OSError:
                time.sleep(0.5)
                continue
            webbrowser.open(url)
            return

    threading.Thread(
        target=_wait_and_open, daemon=True, name="ciao-open-wizard"
    ).start()


async def _wait_for_chat_drain(
    pcm: ProjectChatManager,
    *,
    poll_interval: float = 0.5,
    idle_polls_required: int = 3,
) -> None:
    """Wait until chat work stays idle across consecutive observations.

    The stable-idle window closes the handoff race between a parent stream
    ending and its background-subagent watcher or synthesis stream starting.
    ``begin_restart_drain`` prevents unrelated new turns from extending the
    wait after a restart has already been requested.
    """
    idle_polls = 0
    required = max(1, idle_polls_required)
    while idle_polls < required:
        if pcm.active_chat_ids():
            idle_polls = 0
        else:
            idle_polls += 1
        if idle_polls < required:
            await asyncio.sleep(max(0.0, poll_interval))


def _ensure_tool_dirs_on_path() -> None:
    """Add the user's real tool directories to PATH when they are missing.

    launchd and LaunchServices both start the engine with a minimal PATH
    (roughly ``/usr/bin:/bin:/usr/sbin:/sbin``), so subprocess calls to ``npm``,
    ``node``, Homebrew's ``git``/``pip``, and the provider CLIs fail with
    FileNotFoundError. Two things depend on this being fixed up before anything
    else runs: the subprocess steps themselves, and ``ciao/cli.py``, which bakes
    this process's PATH into ``{{CIAO_PATH}}`` of ``com.ciao.server.plist`` - so
    when desktop onboarding spawns bootstrap as a child of the Tauri app,
    dropping this wrote the minimal PATH into the LaunchAgent permanently.

    The directory list comes from ``tool_path``, which already curates it for
    this exact problem and includes what a hardcoded Homebrew pair misses -
    notably nvm's ``node/*/bin``, the most common way node is installed.
    """
    from ciao.tool_path import common_tool_dirs

    parts = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    seen = set(parts)
    missing = [d for d in common_tool_dirs() if d not in seen]
    if not missing:
        return
    if os.environ.get("CIAO_BUNDLED_APP") == "1":
        # The bundled launcher puts its own bin first on purpose, so child
        # `ciao` commands resolve to the interpreter that owns the bundled
        # site-packages, and stay on the same engine and version as the app.
        # Appending still finds npm/node/git. This is no longer load-bearing
        # for importability - the bundled interpreter attaches its own
        # dependency tree via its ciao_bundled_site hook rather than through an
        # exported PYTHONPATH, so a child on a different CPython is merely a
        # different install now, not a crash.
        os.environ["PATH"] = os.pathsep.join([*parts, *missing])
    else:
        os.environ["PATH"] = os.pathsep.join([*missing, *parts])


async def _async_main() -> int:
    _ensure_tool_dirs_on_path()
    os.environ.setdefault("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND", "file")
    config = CiaoConfig.from_env()

    from ciao.instance_lock import WorkspaceInstanceLock

    lock = WorkspaceInstanceLock(
        config.state_path.parent,
        workspace_root=config.workspace_root,
        port=config.pwa_port,
    )
    with lock:
        if (
            config._workspace_registry_changed
            and not os.environ.get("CIAO_WORKSPACES", "").strip()
        ):
            config.persist_workspace_registry()
        return await _run_server_locked(config)


async def _run_server_locked(config: CiaoConfig) -> int:
    """Server implementation; caller owns the workspace instance lock."""

    setup_error_logging(config.workspace_root)
    # When CIAO_LOG_LEVEL=debug, also capture DEBUG+ records into a rotating
    # server_debug.log so verbose runtime detail is inspectable after the fact
    # (surfaced through the debug issue report). No-op at the default INFO.
    from ciao.error_log import resolve_log_level, setup_debug_logging

    setup_debug_logging(config.workspace_root)
    log_level = resolve_log_level()
    # Keep the SDK's benign closed-transport control-task errors out of the
    # error log (asyncio would otherwise log them at ERROR). See issue #163.
    install_asyncio_noise_filter()

    # No model discovery at startup: opencode serves its catalog on demand and
    # Claude Code has a fixed tier vocabulary,
    # so there is no allowlist to warm here.

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
    from ciao import proposal_outcomes

    job_runs.configure(config.state_path.parent)
    proposal_outcomes.configure(config.state_path.parent)
    tracker = StartupTracker(on_finish=job_runs.record_startup_phase)
    # Live job events reach the PWA through the chat manager's event bus, so a
    # surface can show background work as it happens rather than only after it
    # lands in the run log. Attached once the manager exists (see below).

    # Start provider checks in the background
    tracker.start("connect_claude_code")

    async def check_claude_code():
        try:
            # Prefer the SDK's bundled Claude CLI when present; otherwise fall
            # back to an external ``claude`` resolved against the login-shell
            # PATH, since launchd omits ~/.local/bin and other tool dirs.
            from ciao.providers.claude import get_bundled_claude_path
            from ciao.tool_path import resolve_tool
            cli = get_bundled_claude_path() or resolve_tool("claude")
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

    # Every other runtime provider reports readiness through its registry
    # status probe, so a new provider gets a startup phase for free. Claude
    # keeps the hand-written check above because its phase id
    # (`connect_claude_code`) predates the registry and is a UI contract.
    from ciao import provider_registry

    def _start_provider_check(descriptor) -> None:
        phase = f"connect_{descriptor.id}"
        tracker.start(phase)

        async def check() -> None:
            try:
                status = await asyncio.to_thread(
                    descriptor.status_probe, os.environ
                )
                detail = str(status.get("detail") or "")
                if status.get("ok"):
                    tracker.done(phase, detail or "connected")
                else:
                    tracker.fail(phase, detail or "not connected")
            except Exception as exc:  # a probe must never block startup
                tracker.fail(phase, f"not found: {exc}")

        asyncio.create_task(check())

    for descriptor in provider_registry.descriptors():
        if descriptor.id == "claude" or not descriptor.status_probe_path:
            continue
        _start_provider_check(descriptor)

    # Sync workspace before anything else
    if config.auto_sync_on_start:
        tracker.start("sync_workspace")
        try:
            await sync_workspace(config.workspace_root)
            tracker.done("sync_workspace")
        except Exception:
            tracker.fail("sync_workspace", "git sync failed")
            logger.exception("Workspace sync failed")

    # Re-root the install, once, before anything reads the vault. After the git
    # sync so the clean-tree gate judges the real tree; before the index refresh
    # so the indexes are rebuilt for the layout that now exists; and before the
    # server binds, so no chat is holding a session in a directory that moves.
    tracker.start("reroot_workspaces")
    try:
        from ciao.workspace_reroot import migrate_if_needed

        reroot = await asyncio.to_thread(migrate_if_needed, config)
        status = str(reroot.get("status", ""))
        if status == "migrated":
            tracker.done("reroot_workspaces", f"{len(reroot.get('applied') or [])} moves")
            # This `config` was loaded before the move, so every per-workspace
            # vault path it holds now points at a directory that has gone —
            # `workspace_vault_root("personal")` still says `memory-vault/personal`.
            # Patching the object in place would leave anything already derived
            # from it stale, so restart into a process that reads the new
            # registry from disk. Receipt-gated, so the next boot is a no-op and
            # this happens exactly once in an install's life.
            logger.info("re-root: restarting to pick up the new layout")
            # Returned, not raised: the `except RestartRequested` handler wraps
            # only `server.serve()`, and this happens long before that. The exit
            # code is the same one that path returns, so the supervisor restarts
            # us identically.
            return config.restart_exit_code
        elif status == "refused":
            # Surfaced by the `workspace-unmigrated` action, which reads the
            # refusal back out of the receipt and offers the retry.
            tracker.fail("reroot_workspaces", "refused; see the housekeeping strip")
        else:
            tracker.done("reroot_workspaces", status or "skipped")
    except Exception:
        tracker.fail("reroot_workspaces", "re-root check failed")
        logger.exception("Workspace re-root check failed")

    # Refresh vault index after git pull so INDEX.md reflects any remote changes
    if config.auto_vault_index:
        tracker.start("refresh_vault_index")
        try:
            await asyncio.to_thread(
                _refresh_vault_index,
                config.workspace_root,
                config.vault_root,
                config.vault_scan_targets(),
            )
            tracker.done("refresh_vault_index")
        except Exception:
            tracker.fail("refresh_vault_index", "index refresh failed")
            logger.exception("Vault index refresh failed")

    # The PWA ships pre-built in the installed package; workspaces never
    # contain app source, so there is no frontend rebuild at startup.

    # Update skills in the background, startup should not wait on npm.
    def _skills_task():
        try:
            # Every AGENT ROOT, not the install root. After the re-rooting the
            # install root is not one: syncing it there seeded a stock CLAUDE.md
            # beside the real per-root guides and pruned the install root's now
            # stale `.agents/skills` links, reporting 17 tracked deletions for
            # mirrors nothing reads any more.
            targets = config.agent_root_targets()
            for root, name in targets:
                update_skills(str(root), workspace_name=name)
            tracker.done("update_skills")
        except Exception:
            tracker.fail("update_skills", "skill install failed")
            logger.exception("Skill update failed")

    tracker.start("update_skills")
    asyncio.create_task(asyncio.to_thread(_skills_task))

    if config.insights_backfill_on_startup:
        tracker.start("backfill_insights")
        async def _backfill_task():
            try:
                from ciao.insights import backfill_insights_task
                from ciao.insights import format_backfill_summary
                result = await backfill_insights_task(config)
                tracker.done("backfill_insights", format_backfill_summary(result))
            except Exception:
                tracker.fail("backfill_insights", "backfill failed")
                logger.exception("Insights backfill failed")
        asyncio.create_task(_backfill_task())

    # Initialize stores
    state = StateStore(
        config.state_path,
        config.workspace_root,
        config.media_root,
        default_model=config.claude_default_model,
        default_mode=config.claude_mode,
    )
    transcript_root = config.logs_root / "Chats"
    transcripts = TranscriptStore(config.state_path.parent, transcript_root)

    # Loops were folded into schedules as the `interval` cadence. Import any
    # legacy `.runtime/loops.json` before the store is read, so a device
    # upgrading (or syncing that file in from one that has not upgraded yet)
    # keeps its automations instead of losing them silently.
    try:
        migrate_loops(config.state_path.parent)
    except Exception:
        logger.warning("Could not import legacy loops.json", exc_info=True)

    # `workspace_names` is read on every list, not captured once, so adding a
    # workspace produces its per-workspace system routines without a restart.
    schedule_store = ScheduleStore(
        config.state_path.parent,
        include_system=True,
        workspace_names=config.workspace_names,
    )

    # Create ProjectChatManager
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=config.state_path.parent / "web_projects.json",
    )
    # Now that a manager exists, let tracked background jobs announce themselves
    # through its event bus (see job_runs.set_publisher).
    pcm.attach_job_runs_publisher()

    # Dispatch failures stamp last_status on the stored schedule row so the
    # Automations sidebar flags them for attention instead of leaving an
    # endless string of invisible `stream error` job records (issue #407).
    # The manager cannot hold a store reference in its constructor without
    # reshuffling init order, so it is attached right after both exist.
    pcm.schedule_store = schedule_store

    # Schedule manager with web-only dispatch
    async def _dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        result = await pcm.dispatch_schedule(
            entry, entry.prompt, model, mode, provider,
            target_chat_id=target_chat_id,
        )
        if result and "chat_id" in result:
            entry.last_run_chat_id = result["chat_id"]
            # Persist only if the entry still exists: a "once" schedule may have
            # been consumed (replace-then-delete) before this background task
            # resolves, and replace() upserts — writing here would resurrect it.
            #
            # Write only the field this function owns, onto a freshly read row.
            # `entry` is the snapshot taken when the run started, and a run can
            # stream for minutes: replacing the whole row with it reverted any
            # edit the user made meanwhile (a Stop, a new prompt, a new
            # interval), and did so *before* `_run_interval`'s own re-read, so
            # that function's documented "the user's edit survives" guarantee
            # was reading an already-clobbered row.
            latest = schedule_store.get(entry.schedule_id)
            if latest is not None:
                latest.last_run_chat_id = result["chat_id"]
                schedule_store.replace(latest)
        return result

    def _prepare_chat(entry, prompt, model, mode, provider):
        return pcm.prepare_schedule_chat(entry, prompt, model, mode, provider)

    def _resolve_schedule_target(entry):
        # Empty entry.model / entry.mode means "use the current default".
        ctx = ChatContext(chat_id=0)
        mode = entry.mode or state.get_mode(ctx)
        provider, model, _workspace = pcm.schedule_effective_routing(entry)
        return ("claude", model, mode, provider)

    from ciao.node_state import NodeStateManager
    node_state_manager = NodeStateManager(config.state_path.parent)

    # An interval schedule bound to an existing chat can only dispatch into a
    # live, non-archived one. Treat an archived (or deleted) target as
    # dispatchable while its project still resolves — the dispatcher forks or
    # opens a replacement chat there — and as undispatchable otherwise, so the
    # entry is disabled instead of erroring every interval with "Cannot send
    # messages to an archived chat" (issue #126).
    def _interval_target_dispatchable(entry) -> bool:
        chat_id = getattr(entry, "web_chat_id", "") or ""
        if not chat_id:
            return True  # project-bound: a fresh chat is created per run
        chat = pcm.get_chat(chat_id)
        if chat is not None and not chat.archived:
            return True
        return pcm.resolve_automation_project(entry) is not None

    schedule_manager = ScheduleManager(
        store=schedule_store,
        resolve_target=_resolve_schedule_target,
        dispatch_to_web=_dispatch_to_web,
        prepare_chat=_prepare_chat,
        is_node_active=node_state_manager.is_active,
        chat_busy=pcm.chat_stream_active,
        chat_dispatchable=_interval_target_dispatchable,
    )

    # Background command runs (issue #282): one command, no model in the loop.
    # Completions wake the chat that started the run after a short coalescing
    # window, so a batch finishing together produces one turn.
    from ciao.background import BackgroundRun, BackgroundRunner, BackgroundRunStore

    def _background_finished(run: BackgroundRun, tail: list[str]) -> None:
        pcm.queue_background_wake(
            run.parent_chat_id,
            run_id=run.run_id,
            label=run.label,
            status=run.status,
            exit_code=run.exit_code,
            last_lines=tail,
            log_path=str(background_runner.log_path(run.run_id)),
            error=run.error,
        )

    background_runner = BackgroundRunner(
        BackgroundRunStore(config.state_path.parent),
        workspace_root=config.workspace_root,
        on_finish=_background_finished,
    )

    # Create and wire up web app. The MCP control plane is mandatory for chat;
    # first-run setup is the one state without one, and a chat attempted there
    # fails loudly rather than running an agent that cannot reach Ciaobot.
    from ciao.mcp_server import CiaoMcpService

    mcp_service = None
    control_plane = None
    if not getattr(config, "bootstrap_mode", False):
        mcp_service = CiaoMcpService(config)
    app = create_app(config, app_settings=app_settings, mcp_service=mcp_service)
    app.state.startup_tracker = tracker
    app.state.node_state_manager = node_state_manager
    # Stamp the target project's name on schedules that only recorded its id,
    # while those ids still resolve. After a fresh init they would not, and the
    # run would fall back to General with the user's choice lost.
    try:
        schedule_manager.backfill_project_names(
            lambda pid: (lambda p: p.name if p else None)(pcm.get_project(pid))
        )
    except Exception:
        logger.warning("Could not backfill schedule project names", exc_info=True)
    # Same window, for the other half of the routing state: a chat-bound entry
    # written before `stamp_fallback_project` existed records no re-home
    # target, and that is only readable off the bound chat while it exists.
    try:
        schedule_manager.backfill_fallback_projects(pcm)
    except Exception:
        logger.warning("Could not backfill schedule fallback projects", exc_info=True)

    app.state.schedule_manager = schedule_manager
    app.state.background_runner = background_runner
    # Lets the wake flusher defer runs to the runner when the restart drain
    # blocks delivery, so the next start replays those wakes.
    pcm._background_runner = background_runner
    app.state.state_store = state
    app.state.transcript_store = transcripts
    app.state.project_chat_manager = pcm

    from ciao.web.connection_tracker import ConnectionTracker

    connection_tracker = ConnectionTracker()
    app.state.connection_tracker = connection_tracker

    # Git sync operates on the repo containing the vault root: the workspace
    # root for the default vault-inside-workspace layout (and as fallback),
    # or the vault's own repo when it lives elsewhere. Every instance works
    # on whatever branch that checkout is on and syncs it via the Settings
    # button (clean pull -> direct push; conflict -> interactive resolution
    # chat).
    from ciao.local_session import LocalSessionManager, sync_root

    git_sync_root = await asyncio.to_thread(sync_root, config)
    app.state.local_session_manager = LocalSessionManager(
        workspace=git_sync_root,
        runtime_root=config.state_path.parent,
        dev_mode=config.dev_mode,
    )
    if mcp_service is not None:
        from ciao.control_plane import CiaoControlPlane

        control_plane = CiaoControlPlane(
            config,
            project_chat_manager=pcm,
            schedule_manager=schedule_manager,
            app_settings=app_settings,
            startup_tracker=tracker,
            connection_tracker=connection_tracker,
            background_runner=background_runner,
        )
        mcp_service.bind(control_plane)
        pcm._mcp_service = mcp_service
        app.state.control_plane = control_plane
    push_subject = _push_subject_for_config(config)
    if not push_subject:
        logger.info(
            "CIAO_PUSH_CONTACT is not set; Web Push notifications stay "
            "disabled until a contact is configured in Settings."
        )
    app.state.push_manager = PushManager(config.state_path.parent, subject=push_subject)
    app.state.focused_chats = {}

    # A read mutation is already broadcast to every connected PWA. Fan the
    # same mutation out to delivered OS notifications in the background: the
    # macOS companion consumes the runtime log, while Web Push subscriptions
    # receive a service-worker control message that closes their chat tag.
    def _clear_notifications(chat_id: str) -> None:
        pm = app.state.push_manager
        if pm is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, pm.clear_chat, chat_id)
        except Exception:
            logger.exception("Failed scheduling notification clear for %s", chat_id)

    pcm.clear_notifications_cb = _clear_notifications

    # Google Workspace token health + server-managed re-login (issue #145).
    from ciao.gws_auth import GwsHealthMonitor, GwsReloginManager, ManualPkceStore

    app.state.gws_health_monitor = GwsHealthMonitor(
        config,
        push_manager=app.state.push_manager,
        events_hub=pcm.events,
        runtime_root=config.state_path.parent,
    )
    app.state.gws_relogin_manager = GwsReloginManager(config)
    # PKCE verifier store for the manual/paste OAuth flow (issue #354).
    app.state.gws_manual_pkce_store = ManualPkceStore()

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


    # Bootstrap mode is the first-run setup wizard: the server runs against a
    # throwaway ~/.ciao/bootstrap workspace until the user picks a real folder.
    # Starting the schedulers here would dispatch system schedules (memory
    # curation, skill evolution, weekly review) against that throwaway
    # workspace mid-wizard — the user sees "chat completed" notifications for
    # chats created in the wrong place. Hold every dispatcher until setup is
    # done; the post-setup restart (no longer in bootstrap mode) starts them.
    if getattr(config, "bootstrap_mode", False):
        logger.info(
            "Bootstrap mode: holding schedule dispatch until setup completes."
        )
    else:
        # One ticker for every cadence. Interval entries resume on their own
        # within an interval of boot; they are excluded from the catch-up pass
        # because replaying intervals missed during downtime is worthless.
        schedule_manager.start()

        # Resolve any background run left non-terminal by a crash (the
        # graceful path terminates them on shutdown, so this normally finds
        # nothing), replay wakes deferred by a draining restart, prune expired
        # ones, and arm the janitor. Orphans are woken here rather than left
        # sitting in `running` forever.
        orphaned = background_runner.start()
        if orphaned:
            logger.warning(
                "Resolved %d orphaned/replayed background run(s) after restart: %s",
                len(orphaned), ", ".join(run.run_id for run in orphaned),
            )

        # Wake chats whose CLI-owned tasks (Monitor / background Bash) were
        # still running when the old server died: no completion watcher
        # survives a restart, so the wake must be armed here.
        swept = pcm.sweep_orphaned_cli_tasks()
        if swept:
            logger.warning("Woke %d chat(s) with CLI tasks orphaned by the restart", swept)

        # Fire each schedule once when its latest expected occurrence was missed
        # (for example while the server was down). This does not replay every
        # skipped interval. Runs asynchronously so it doesn't block uvicorn from
        # serving requests.
        #
        # Right after a first-time setup the onboarding chat should be the only
        # new conversation: within the post-setup grace window, system routines
        # are skipped here and simply fire at their next regular tick, instead
        # of all replaying their missed runs in parallel at first launch.
        from ciao.setup_marker import catch_up_grace_active

        grace_active = catch_up_grace_active(config.state_path.parent)
        if grace_active:
            logger.info(
                "Post-setup grace window active: holding system-routine "
                "catch-up; routines fire at their next regular tick."
            )

        async def _run_catch_up() -> None:
            try:
                fired = await schedule_manager.catch_up(skip_system=grace_active)
                if fired:
                    logger.info("Schedule catch-up fired %d schedule(s): %s",
                                len(fired), ", ".join(fired))
            except Exception:
                logger.exception("Schedule catch-up failed")

        asyncio.create_task(_run_catch_up())

    # ── Branch backup ────────────────────────────────────────
    # Backs up the same repo the sync flow targets (the repo containing the
    # vault root, falling back to the workspace root). Every instance works on
    # whatever branch that checkout is on; Ciaobot never creates or switches
    # branches. A background loop pushes the branch for backup. Non-git roots
    # (fresh `ciao setup` without a remote) and repos without an `origin`
    # remote skip this gracefully.
    from ciao.local_session import (
        BACKUP_PUSH_INTERVAL,
        has_origin_remote,
        is_diverged_backup,
        push_branch,
        workspace_branch,
    )

    async def _branch_backup_loop() -> None:
        branch = await asyncio.to_thread(workspace_branch, git_sync_root)
        if branch is None:
            logger.info(
                "Sync root %s is not a git repository (or is on a detached HEAD); "
                "skipping branch backup.", git_sync_root,
            )
            return
        if not await asyncio.to_thread(has_origin_remote, git_sync_root):
            logger.info(
                "Sync root has no 'origin' remote; skipping branch backup.",
            )
            return
        logger.info("Working on branch '%s'", branch)
        # Credential failures cannot self-heal (there is no TTY to prompt
        # under launchd), so retrying at the normal cadence is pure waste.
        auth_markers = (
            "could not read username",
            "authentication failed",
            "invalid username or token",
            "permission denied (publickey",
        )
        auth_backoff_multiplier = 12
        last_failure_detail: str | None = None
        repeated_failures = 0
        auth_backoff = False
        # Set once push_branch falls back to a per-commit backup ref because
        # the shared branch has a real merge conflict with origin. Backs off
        # the cadence the same way auth_backoff does: retrying a merge that
        # will conflict the same way every 30s is pure waste, and the backup
        # ref push is idempotent (its name is derived from the HEAD sha), so
        # slower retries do not lose any coverage — only a fast-forwardable
        # recovery (the shared push succeeding again) clears it.
        diverged_backoff = False
        while True:
            try:
                await asyncio.sleep(
                    BACKUP_PUSH_INTERVAL
                    * (auth_backoff_multiplier if (auth_backoff or diverged_backoff) else 1)
                )
                async with job_runs.track(
                    "branch_backup", "Branch backup",
                    category="system", extra={"branch": branch},
                ) as run:
                    if not node_state_manager.is_active():
                        run.skip("client mode — host owns backup push")
                        continue
                    ok, detail = await push_branch(git_sync_root, branch=branch)
                    if ok:
                        if is_diverged_backup(detail):
                            if not diverged_backoff:
                                diverged_backoff = True
                                logger.warning(
                                    "Branch backup: %s has a real merge "
                                    "conflict with origin; backing off and "
                                    "backing up to a per-commit ref instead "
                                    "until a human resolves it. %s",
                                    branch, detail,
                                )
                            run.extra["shared_branch_diverged"] = True
                            run.extra["detail"] = detail
                            last_failure_detail = None
                            repeated_failures = 0
                            continue
                        if diverged_backoff:
                            logger.info(
                                "Branch backup: %s push to origin recovered; "
                                "resuming normal cadence.", branch,
                            )
                            diverged_backoff = False
                        if last_failure_detail is not None:
                            logger.info("Branch backup push recovered.")
                        last_failure_detail = None
                        repeated_failures = 0
                        auth_backoff = False
                        continue
                    if detail == last_failure_detail:
                        repeated_failures += 1
                        run.skip("same failure as previous backup attempt")
                        run.extra["repeat_count"] = repeated_failures
                        is_auth = any(
                            marker in detail.lower() for marker in auth_markers
                        )
                        if is_auth and repeated_failures >= 3 and not auth_backoff:
                            auth_backoff = True
                            logger.warning(
                                "Branch backup authentication keeps failing; "
                                "retrying hourly instead. Store credentials to "
                                "resume (e.g. `gh auth setup-git`, or switch "
                                "the remote to SSH).",
                            )
                        logger.debug("Branch backup push still failing: %s", detail)
                        continue
                    last_failure_detail = detail
                    repeated_failures = 1
                    auth_backoff = False
                    run.status = "error"
                    run.error = detail
                    logger.warning("Branch backup push failed: %s", detail)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Branch backup push failed")

    asyncio.create_task(_branch_backup_loop())

    # ── Google Workspace token health ────────────────────────
    # Cheap periodic `auth status` ping per configured GWS profile. When a
    # refresh token is revoked/expired the monitor fires ONE PWA notification
    # plus an in-app status event (debounced until the token recovers), so
    # GWS-dependent schedules don't fail silently (issue #145). The credential
    # files are never read into logs; only the boolean validity is surfaced.
    try:
        _gws_health_interval = int(os.environ.get("CIAO_GWS_HEALTH_INTERVAL", "900"))
    except ValueError:
        _gws_health_interval = 900

    async def _gws_health_loop() -> None:
        if _gws_health_interval <= 0:
            logger.info("GWS token health checks disabled (CIAO_GWS_HEALTH_INTERVAL=0).")
            return
        monitor = app.state.gws_health_monitor
        # Small initial delay so a fresh boot settles before the first probe.
        await asyncio.sleep(30)
        while True:
            try:
                async with job_runs.track(
                    "gws_health", "Google Workspace token health", category="system",
                ) as run:
                    summary = await asyncio.to_thread(monitor.check_once)
                    run.extra["checked"] = summary.get("checked", [])
                    if summary.get("invalid"):
                        run.extra["invalid"] = summary["invalid"]
                    if summary.get("notified"):
                        run.extra["notified"] = summary["notified"]
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("GWS token health check failed")
            await asyncio.sleep(_gws_health_interval)

    if not getattr(config, "bootstrap_mode", False):
        asyncio.create_task(_gws_health_loop())


    import uvicorn

    uvi_config = uvicorn.Config(
        app,
        host=config.pwa_host,
        port=config.pwa_port,
        log_level="debug" if log_level <= logging.DEBUG else "info",
    )
    server = uvicorn.Server(uvi_config)
    tracker.start("server_starting")
    logger.info("Starting Ciaobot server on %s:%d", config.pwa_host, config.pwa_port)
    if getattr(config, "bootstrap_mode", False):
        # The setup wizard's finish step only accepts loopback hosts, so give
        # users a URL that works instead of the 0.0.0.0 bind address above.
        setup_url = f"http://localhost:{config.pwa_port}"
        print(
            "\n"
            "  ──────────────────────────────────────────────────────\n"
            f"   First-run setup — open  {setup_url}\n"
            "   in your browser and follow the wizard.\n"
            "  ──────────────────────────────────────────────────────\n",
            file=sys.stderr,
            flush=True,
        )
        _open_browser_when_ready(setup_url)
    tracker.done("server_starting")

    restart_flag: list[int | None] = [None]
    restart_task: list[asyncio.Task | None] = [None]

    def request_restart(code: int) -> None:
        restart_flag[0] = code
        existing = restart_task[0]
        if existing is not None and not existing.done():
            return

        # Close admission synchronously with the request, before the drain
        # task gets its first event-loop turn. Existing streams keep running
        # and can flush messages that were already queued on them.
        pcm.begin_restart_drain()

        async def _restart_when_idle() -> None:
            active = pcm.active_chat_ids()
            if active:
                logger.info(
                    "Restart requested; waiting for %d active chat(s): %s",
                    len(active),
                    ", ".join(active),
                )
            await _wait_for_chat_drain(pcm)
            logger.info("Chat work drained; proceeding with requested restart")

            # asyncio.run's cleanup phase (cancel tasks, shut down the default
            # executor) can wedge after uvicorn drains: leaked Claude SDK
            # subprocess transports and synchronous urllib calls in the
            # heartbeat thread both hold the loop open indefinitely. Start
            # the watchdog only after chat work drains so it cannot cut the
            # wait short. A plain os._exit would leave a foreground `ciao run`
            # dead; exec a fresh interpreter instead so launchd keeps tracking
            # the same pid and the relaunch picks up the current environment.
            restart_code = restart_flag[0]
            if restart_code is None:
                restart_code = code

            def _force_exit() -> None:
                time.sleep(15)
                if restart_code == 0:
                    # A clean-exit request (setup wizard handing the server
                    # over to launchd): dying is the point, don't relaunch.
                    os._exit(0)
                logger.info(
                    "Cleanup did not finish; re-execing for the requested restart"
                )
                try:
                    os.execv(
                        sys.executable,
                        [sys.executable, "-m", "ciao.cli", *sys.argv[1:]],
                    )
                except OSError:
                    os._exit(restart_code)

            threading.Thread(
                target=_force_exit, daemon=True, name="ciao-restart-watchdog"
            ).start()
            await server.shutdown()

        restart_task[0] = asyncio.create_task(_restart_when_idle())

    app.state.request_restart = request_restart

    # ── Startup error triage ─────────────────────────────────
    # Cap the append-only launchd service logs, then — when the error log
    # or recent job runs contain failures — dispatch a triage chat through
    # the schedule pipeline ({{ISSUE_REPORT}} substitution clears the error
    # log after a clean run). Errors found at boot become a fix-it chat
    # instead of silently accumulating.
    async def _startup_error_triage() -> None:
        try:
            from ciao.startup_triage import cap_service_logs, run_startup_triage

            await asyncio.to_thread(cap_service_logs, config.state_path.parent)
            await run_startup_triage(pcm, config, _resolve_schedule_target)
        except Exception:
            logger.exception("Startup error triage failed")

    asyncio.create_task(_startup_error_triage())

    # ── Stale-install self-heal ──────────────────────────────
    # Production updates replace the complete app bundle atomically and then
    # restart the LaunchAgent. Keep the file-presence guard for development
    # checkouts; the packaged app updater owns production replacement.
    async def _watch_installed_version() -> None:
        from ciao.package_version import InstallWatcher

        watcher = InstallWatcher()
        probe_upgrades = False
        tick = 0
        while True:
            await asyncio.sleep(60)
            tick += 1
            reason = watcher.check_files()
            if reason is None and probe_upgrades and tick % 5 == 0:
                reason = await asyncio.to_thread(watcher.check_version)
            if reason:
                logger.warning(
                    "%s; requesting restart onto the current version.", reason
                )
                request_restart(config.restart_exit_code)
                return

    asyncio.create_task(_watch_installed_version())

    # ── App bundle refresh on upgrade ────────────────────────
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

    async def _shutdown_background_runs() -> None:
        # Terminate every live background command before the loop closes. This
        # is what makes "a restart does not leak orphan processes" true on the
        # graceful path: without it, a detached child (own session, by design,
        # so cancel can reach its whole tree) would survive the engine and the
        # next boot could only report it, never reclaim it.
        try:
            await background_runner.stop()
        except Exception:
            logger.exception("Background runner shutdown failed")

    app.state.shutdown_callbacks = [_shutdown_providers, _shutdown_background_runs]

    try:
        await server.serve()
    except RestartRequested as exc:
        return int(exc.args[0]) if exc.args else config.restart_exit_code
    if restart_flag[0] is not None:
        return restart_flag[0]
    return 0


def main() -> None:
    """CLI entrypoint."""
    from ciao.error_log import resolve_log_level

    logging.basicConfig(level=resolve_log_level())
    from ciao.instance_lock import WorkspaceAlreadyRunningError

    try:
        code = asyncio.run(_async_main())
    except WorkspaceAlreadyRunningError as exc:
        print(f"Ciaobot did not start: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
