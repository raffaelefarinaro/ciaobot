"""First-run setup readiness probes.

The setup wizard needs one stable endpoint that answers "what is already
configured?" without scraping files in the browser. Keep checks bounded and
fail-closed: missing or unreachable optional providers report ``ok=false``
with the command the user can run next.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import json
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Any

from ciao import provider_registry

# Claude MCP / skill discovery shells out; cache briefly so Settings refreshes
# stay responsive without freezing status until process restart.
_CLAUDE_DISCOVERY_TTL_SECONDS = 300.0
# An empty discovery is usually a timeout or a transient health-check failure,
# not a real "nothing connected" answer. Serve it only briefly so the
# stale-while-revalidate retry lands within seconds instead of showing "(0)"
# for the full success TTL.
_CLAUDE_DISCOVERY_EMPTY_TTL_SECONDS = 15.0
# ``claude mcp list`` health-checks every configured connector before printing.
# With ~30 claude.ai connectors a healthy run measures ~11-12s, and under load
# it blows past 12s — at which point the old timeout swallowed the error and
# cached the empty list for five minutes. Give the health pass real headroom.
_CLAUDE_MCP_LIST_TIMEOUT_SECONDS = 30.0
logger = logging.getLogger(__name__)

_claude_mcps_cache: tuple[float, str, tuple[str, ...], int] | None = None
_claude_mcps_refreshing = False
_claude_mcps_lock = threading.Lock()
# Set while one runner (startup warm-up, a request thread, or the stale-TTL
# background refresh) is executing the ``claude mcp list`` probe, so
# latecomers wait for it instead of stacking a second full health pass.
_claude_mcps_inflight: threading.Event | None = None
# Bumped on every ``clear_claude_discovery_cache`` so a probe result produced
# from a pre-clear configuration (e.g. the startup warm-up finishing right
# after Verify) is not served or cached as current.
_claude_mcps_generation = 0
_claude_skills_cache: tuple[float, tuple[str, ...]] | None = None


def _resolve_root(raw: Any) -> Path:
    """Resolve a root without assuming the process cwd still exists.

    The desktop deploy relaunch can leave the engine with a cwd inside the
    staging bundle the swap then renames, and there both ``Path.cwd()`` and
    resolving a relative path raise ``FileNotFoundError``. Readiness must still
    answer, so an unresolvable root is kept as written: its checks then report
    not-ready instead of pointing at a directory we made up.
    """
    path = Path(raw).expanduser()
    try:
        return path.resolve()
    except OSError:
        return path


def clear_claude_discovery_cache() -> None:
    """Drop Claude MCP/skill discovery caches (tests and forced refresh).

    An in-flight probe is deliberately left registered: the Verify endpoint
    calls this while the startup warm-up may still be running, and
    unregistering the probe would let the next payload claim single-flight
    ownership and stack a second blocking ``claude mcp list``. Instead the
    generation bump marks any result that pre-clear probe produces as stale,
    so the next discovery waits it out and then re-probes with current
    configuration. Tests that need a clean single-flight state reset
    ``_claude_mcps_inflight`` directly.
    """
    global _claude_mcps_cache, _claude_skills_cache, _claude_mcps_generation
    _claude_mcps_cache = None
    _claude_skills_cache = None
    _claude_mcps_generation += 1


def _acquire_mcps_probe() -> threading.Event | None:
    """Try to become the sole ``claude mcp list`` runner.

    Returns the event this caller owns (``release_mcps_probe`` must be called
    on it), or ``None`` when another probe is already in flight.
    """
    global _claude_mcps_inflight
    with _claude_mcps_lock:
        if _claude_mcps_inflight is not None:
            return None
        event: threading.Event = threading.Event()
        _claude_mcps_inflight = event
        return event


def _release_mcps_probe(event: threading.Event) -> None:
    """Finish this caller's probe run and hand single-flight back."""
    global _claude_mcps_inflight
    event.set()
    with _claude_mcps_lock:
        if _claude_mcps_inflight is event:
            _claude_mcps_inflight = None


def detect_nested_workspaces(vault_path: Path) -> list[str]:
    """Return workspace names implied by the existing vault layout.

    Legacy and migrated vaults keep each workspace in its own subdirectory
    under the vault root (``memory-vault/personal/``, ``memory-vault/work/``,
    etc.), with a ``MEMORY.md`` file inside. Detecting those subdirectories
    lets setup adopt the existing logical workspaces instead of creating a
    single synthetic workspace that points at the whole vault.
    """
    try:
        entries = [p for p in vault_path.iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted(
        entry.name
        for entry in entries
        if (entry / "MEMORY.md").is_file()
    )


# macOS TCC (privacy) protects these home subfolders. A launchd-spawned
# background agent has no access grant for them, so a workspace placed inside
# one fails at runtime with EPERM ("Operation not permitted") reading its own
# .runtime files — the server and menu bar die (exit 78). Steer setup away.
_TCC_PROTECTED_DIRS = ("Desktop", "Documents", "Downloads")


def tcc_protected_location(path: Path | str) -> str | None:
    """Return the protected home folder (Desktop/Documents/Downloads) that
    contains ``path`` on macOS, or None. Non-macOS platforms return None."""
    if sys.platform != "darwin":
        return None
    resolved = Path(path).expanduser().resolve()
    home = Path.home()
    for name in _TCC_PROTECTED_DIRS:
        base = (home / name).resolve()
        if resolved == base or base in resolved.parents:
            return name
    return None


def _check(
    *,
    check_id: str,
    label: str,
    ok: bool,
    required: bool,
    detail: str = "",
    command: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "required": bool(required),
    }
    if detail:
        row["detail"] = detail
    if command:
        row["command"] = command
    return row


def _provider(
    *,
    name: str,
    ok: bool,
    auth: str,
    command: str,
    detail: str = "",
    version: str = "",
    account: str = "",
    protocol: str = "",
    skills: list[str] | None = None,
    mcps: list[str] | None = None,
    install_url: str = "",
    app_path: str = "",
    cli_path: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "ok": bool(ok),
        "auth": auth,
        "command": command,
    }
    if detail:
        row["detail"] = detail
    if version:
        row["version"] = version
    if account:
        row["account"] = account
    if protocol:
        row["protocol"] = protocol
    if skills is not None:
        row["skills"] = skills
    if mcps is not None:
        row["mcps"] = mcps
    if install_url:
        row["install_url"] = install_url
    if app_path:
        row["app_path"] = app_path
    if cli_path:
        row["cli_path"] = cli_path
    return row


@lru_cache(maxsize=4)
def _cli_version(binary: str) -> str:
    try:
        run = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "installed"
    lines = (run.stdout or run.stderr).strip().splitlines()
    return lines[-1] if lines else "installed"


def discover_claude_system_skills() -> list[str]:
    """Discover enabled Claude Code plugins plus standalone ~/.claude/skills."""
    global _claude_skills_cache
    now = time.monotonic()
    if (
        _claude_skills_cache is not None
        and now - _claude_skills_cache[0] < _CLAUDE_DISCOVERY_TTL_SECONDS
    ):
        return list(_claude_skills_cache[1])

    skills = _discover_claude_system_skills_uncached()
    _claude_skills_cache = (now, tuple(skills))
    return skills


def _claude_standalone_skills_dir() -> Path:
    """Directory of standalone (non-plugin) Claude skills, injectable for tests."""
    return Path.home() / ".claude" / "skills"


def _discover_claude_system_skills_uncached() -> list[str]:
    skills: set[str] = set()
    # A clean `claude plugin list` exit is authoritative even when zero plugins
    # are enabled; only a failure (missing binary, timeout, nonzero exit) needs
    # the installed_plugins.json fallback below.
    cli_ok = False
    binary = claude_cli_path()
    if binary:
        try:
            res = subprocess.run(
                [binary, "plugin", "list"],
                capture_output=True, text=True, timeout=8.0, check=False,
            )
            cli_ok = res.returncode == 0
            output = (res.stdout or "") + "\n" + (res.stderr or "")
            # Claude Code uses a multi-line block format:
            #   ❯ plugin-name@source
            #     Status: ✔ enabled  (or ✘ disabled)
            current_name: str | None = None
            for line in output.splitlines():
                stripped = line.strip()
                if stripped.startswith("❯ "):
                    current_name = stripped[2:].split("@")[0].strip()
                elif stripped.startswith("Status:") and current_name:
                    if "enabled" in stripped and "disabled" not in stripped:
                        skills.add(current_name)
                    current_name = None
        except Exception:
            cli_ok = False
    # Standalone skills under ~/.claude/skills never appear in `claude plugin
    # list`, so merge them in whether or not the CLI reported plugins.
    skills_dir = _claude_standalone_skills_dir()
    if skills_dir.is_dir():
        for entry in skills_dir.glob("*"):
            if entry.is_dir() or entry.name.endswith(".md"):
                skills.add(entry.stem)
    if not cli_ok:
        # The CLI could not report (missing, failed, timed out): list every
        # installed plugin so the page still shows something actionable.
        installed_json = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
        if installed_json.is_file():
            try:
                data = json.loads(installed_json.read_text(encoding="utf-8"))
                for key in (data.get("plugins") or {}).keys():
                    skills.add(str(key).split("@")[0])
            except Exception:
                pass
    return sorted(skills)


def discover_claude_mcps(
    workspace_root: Path | str | None = None,
    *,
    config_path: Path | None = None,
) -> list[str]:
    """Discover connected+enabled Claude MCP connectors via CLI + ~/.claude.json.

    ``claude mcp list`` reports health (Connected) even for connectors disabled
    in the per-project ``/mcp`` panel. Those disables live in
    ``~/.claude.json`` → ``projects.<path>.disabledMcpServers``.

    Served stale-while-revalidate: ``claude mcp list`` health-checks every
    connector and measures ~12s on a real install, and discovery is on the
    Settings -> Providers load path, so a plain TTL made every visit after the
    window pay the full cost. An expired entry is returned immediately and
    refreshed on a background thread, so only the first call after startup ever
    waits. Empty results expire fast (see ``_CLAUDE_DISCOVERY_EMPTY_TTL_SECONDS``)
    because they usually mean the probe timed out, not that nothing is connected.
    """
    global _claude_mcps_cache
    now = time.monotonic()
    ws_key = str(_resolve_root(workspace_root)) if workspace_root else ""
    cached = _claude_mcps_cache
    ttl = (
        _CLAUDE_DISCOVERY_TTL_SECONDS
        if cached is not None and cached[2]
        else _CLAUDE_DISCOVERY_EMPTY_TTL_SECONDS
    )
    fresh = (
        cached is not None
        and now - cached[0] < ttl
        and cached[1] == ws_key
        and cached[3] == _claude_mcps_generation
    )
    if cached is not None and cached[1] == ws_key:
        if not fresh:
            _refresh_claude_mcps_async(ws_key, config_path)
        return list(cached[2])

    # Single-flight: the startup warm-up, the stale-TTL background refresh, or
    # another request thread may already be paying for the probe. Wait for it
    # to finish instead of stacking a second ``claude mcp list``; only become
    # a runner once no other probe is in flight. The wait is unbounded on
    # purpose: the owner's ``finally`` always fires and the probe itself is
    # bounded by the subprocess timeout, so the event cannot stay unset
    # forever. A waiter whose owner finished without a usable entry for this
    # root (failed probe, different workspace root, or a result invalidated by
    # a cache clear mid-probe) loops and reacquires ownership — it never times
    # out into running a duplicate probe alongside the in-flight one.
    while True:
        inflight = _acquire_mcps_probe()
        if inflight is not None:
            break
        with _claude_mcps_lock:
            running = _claude_mcps_inflight
        if running is None:
            # The owner finished between our failed acquire and this read;
            # loop to serve its freshly stamped cache or take over.
            continue
        running.wait()
        cached = _claude_mcps_cache
        if (
            cached is not None
            and cached[1] == ws_key
            and cached[3] == _claude_mcps_generation
        ):
            return list(cached[2])

    probe_generation = _claude_mcps_generation
    try:
        connected = _discover_claude_mcps_uncached(
            workspace_root=Path(ws_key) if ws_key else None,
            config_path=config_path,
        )
        _claude_mcps_cache = (
            time.monotonic(),
            ws_key,
            tuple(connected),
            probe_generation,
        )
        return connected
    finally:
        _release_mcps_probe(inflight)


def _claude_config_path_from_env(source: Mapping[str, str] | None = None) -> Path:
    """The Claude config file the provider payload would read.

    Honors ``CLAUDE_CONFIG_PATH`` so the startup warm-up probes the same
    connector set the Settings -> Providers route will report.
    """
    src = source if source is not None else os.environ
    raw = str(src.get("CLAUDE_CONFIG_PATH", "")).strip()
    return Path(raw).expanduser() if raw else Path.home() / ".claude.json"


def warm_claude_discovery_cache(
    workspace_root: Path | str | None = None,
    *,
    config_path: Path | None = None,
) -> None:
    """Pre-populate the Claude MCP/skill caches off the request path.

    ``claude mcp list`` health-checks every connector (~12s on a real install),
    so run it at app startup instead of letting the first Settings -> Providers
    visit block on it. ``config_path`` defaults to the same ``CLAUDE_CONFIG_PATH``
    derivation the payload uses, so the warmed cache cannot disagree with what
    the route would report. Fire-and-forget: a failure just leaves the cache
    cold and the first request pays the cost as before.
    """

    def run() -> None:
        try:
            discover_claude_mcps(
                workspace_root,
                config_path=config_path or _claude_config_path_from_env(),
            )
            discover_claude_system_skills()
        except Exception:  # noqa: BLE001 - warm-up must never break startup
            logger.debug("Claude discovery warm-up failed", exc_info=True)

    threading.Thread(target=run, name="claude-discovery-warmup", daemon=True).start()


def _refresh_claude_mcps_async(ws_key: str, config_path: Path | None) -> None:
    """Re-discover in the background, at most one refresh in flight.

    The refresh joins the same single-flight as the warm-up and the request
    path: if another probe is running it skips entirely (that probe's result
    is at least as fresh), and while it runs, Verify's cache bust cannot stack
    a second probe on top of it. Failures are swallowed on purpose: the stale
    list already went out to the caller, and a discovery error must not
    surface as a broken Settings page.
    """
    global _claude_mcps_refreshing
    with _claude_mcps_lock:
        if _claude_mcps_refreshing:
            return
        _claude_mcps_refreshing = True

    def run() -> None:
        global _claude_mcps_cache, _claude_mcps_refreshing
        inflight = _acquire_mcps_probe()
        try:
            if inflight is None:
                return  # a newer probe is already running; its result wins
            # Record the generation before probing: a cache clear mid-probe
            # must invalidate this result just like the request path's.
            probe_generation = _claude_mcps_generation
            connected = _discover_claude_mcps_uncached(
                workspace_root=Path(ws_key) if ws_key else None,
                config_path=config_path,
            )
            _claude_mcps_cache = (
                time.monotonic(),
                ws_key,
                tuple(connected),
                probe_generation,
            )
        except Exception:  # noqa: BLE001 - a stale list is already serving
            logger.debug("background Claude MCP discovery failed", exc_info=True)
        finally:
            if inflight is not None:
                _release_mcps_probe(inflight)
            with _claude_mcps_lock:
                _claude_mcps_refreshing = False

    threading.Thread(target=run, name="claude-mcp-refresh", daemon=True).start()


def _short_claude_mcp_name(name: str) -> str:
    cleaned = str(name).strip()
    if cleaned.startswith("claude.ai "):
        return cleaned[len("claude.ai ") :].strip()
    return cleaned


def _disabled_claude_mcp_names(
    *,
    workspace_root: Path | None = None,
    config_path: Path | None = None,
) -> set[str]:
    """Return short connector names disabled via the per-project ``/mcp`` panel."""
    path = config_path or Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()

    disabled: set[str] = set()
    for name in data.get("disabledMcpServers") or []:
        short = _short_claude_mcp_name(str(name))
        if short:
            disabled.add(short)

    projects = data.get("projects")
    if not isinstance(projects, dict):
        return disabled

    roots: list[Path] = []
    if workspace_root is not None:
        try:
            roots.append(Path(workspace_root).expanduser().resolve())
        except OSError:
            pass
    try:
        roots.append(Path.cwd().resolve())
    except OSError:
        pass

    best_key: str | None = None
    best_len = -1
    for root in roots:
        for key, meta in projects.items():
            if not isinstance(meta, dict):
                continue
            try:
                key_path = Path(str(key)).expanduser().resolve()
            except OSError:
                continue
            try:
                root.relative_to(key_path)
            except ValueError:
                continue
            key_len = len(str(key_path))
            if key_len > best_len:
                best_key = str(key)
                best_len = key_len

    if best_key is not None:
        meta = projects.get(best_key) or {}
        if isinstance(meta, dict):
            for name in meta.get("disabledMcpServers") or []:
                short = _short_claude_mcp_name(str(name))
                if short:
                    disabled.add(short)
    return disabled


def _discover_claude_mcps_uncached(
    *,
    workspace_root: Path | None = None,
    config_path: Path | None = None,
) -> list[str]:
    connected: list[str] = []
    # Workspace project MCPs live in .mcp.json and are shown under MCP status,
    # not as Claude Code platform connectors on the Providers tab.
    excluded = {"n8n_mcp", "notion", "ciaobot", "ciaobot-fastmcp"}
    disabled = _disabled_claude_mcp_names(
        workspace_root=workspace_root,
        config_path=config_path,
    )
    binary = claude_cli_path()
    if not binary:
        return connected
    try:
        res = subprocess.run(
            [binary, "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_MCP_LIST_TIMEOUT_SECONDS,
            check=False,
        )
        output = (res.stdout or "") + "\n" + (res.stderr or "")
        for line in output.splitlines():
            if "Connected" not in line:
                continue
            line_clean = (
                line.split(":", 1)[0].replace("claude.ai", "").replace("✔", "").strip()
            )
            if (
                line_clean
                and line_clean not in connected
                and line_clean not in excluded
                and line_clean not in disabled
            ):
                connected.append(line_clean)
    except Exception:
        pass
    return connected


# Provider status probes share one keyword-only contract so ``setup_status``
# can enumerate them from ``ciao.provider_registry`` instead of naming each
# provider. Each probe takes whatever context it needs and ignores the rest.
def claude_status_probe(
    env: Mapping[str, str],
    *,
    credentials_path: Path,
    config_path: Path,
    workspace_root: Path | None = None,
    **_unused: Any,
) -> dict[str, Any]:
    return _claude_status(
        env, credentials_path, config_path, workspace_root=workspace_root
    )


def opencode_status_probe(
    env: Mapping[str, str],
    **_unused: Any,
) -> dict[str, Any]:
    from ciao.providers.opencode import opencode_login_status

    return opencode_login_status(env)


# Where the wizard sends someone who has no Claude Code at all. Kept as a
# constant so the PWA and the CLI point at the same page.
CLAUDE_INSTALL_DOCS_URL = (
    "https://code.claude.com/docs/en/quickstart#step-1-install-claude-code"
)


def claude_install_command() -> str:
    """The documented one-line installer for this platform."""
    if sys.platform == "win32":
        return "irm https://claude.ai/install.ps1 | iex"
    return "curl -fsSL https://claude.ai/install.sh | bash"


def claude_app_path() -> str:
    """Path to an installed Claude desktop app, or "".

    The desktop app ships Claude Code too, but Ciaobot drives the ``claude``
    CLI through the Agent SDK, so an app-only install still needs the CLI.
    Detecting it lets setup say "you have the app, add the CLI" instead of the
    blunter "not installed".
    """
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/Claude.app"),
            home / "Applications" / "Claude.app",
            Path("/Applications/Claude Code.app"),
            home / "Applications" / "Claude Code.app",
        ]
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates = [
                Path(local) / "AnthropicClaude",
                Path(local) / "Programs" / "Claude",
            ]
    else:
        candidates = [Path("/opt/Claude"), Path("/usr/share/claude")]
    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return ""


def claude_cli_path() -> str:
    """Absolute path to the ``claude`` CLI Ciaobot would run, or "".

    Prefers the binary bundled with the desktop build, then the user's own
    install. ``resolve_tool`` (not ``shutil.which``) because the engine often
    runs under launchd with a stripped PATH that omits ``~/.local/bin`` and
    Homebrew, where the documented installers put ``claude``.
    """
    from ciao.providers.claude import get_bundled_claude_path
    from ciao.tool_path import resolve_tool

    bundled = get_bundled_claude_path()
    if bundled:
        return str(bundled)
    try:
        resolved = resolve_tool("claude")
    except Exception:
        resolved = None
    return resolved or shutil.which("claude") or ""


def _claude_status(
    env: Mapping[str, str],
    credentials_path: Path,
    config_path: Path,
    *,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    binary = claude_cli_path()
    version = _cli_version(binary) if binary else "not installed"
    claude_skills = discover_claude_system_skills()
    claude_mcps = discover_claude_mcps(workspace_root, config_path=config_path)
    if not binary:
        # No CLI means no chats, whatever credentials exist: report the install
        # step rather than an auth command the user cannot run yet.
        app_path = claude_app_path()
        detail = (
            f"The Claude desktop app is installed ({app_path}), but Ciaobot runs "
            "chats through the claude CLI, which is not on PATH."
            if app_path
            else "Claude Code is not installed on this machine."
        )
        return _provider(
            name="claude",
            ok=False,
            auth="not_installed",
            command=claude_install_command(),
            detail=detail,
            version=version,
            skills=claude_skills,
            mcps=claude_mcps,
            install_url=CLAUDE_INSTALL_DOCS_URL,
            app_path=app_path,
        )
    if env.get("ANTHROPIC_API_KEY", "").strip():
        return _provider(
            name="claude",
            ok=True,
            auth="api_key",
            command="ciao auth claude",
            detail="ANTHROPIC_API_KEY is set.",
            version=version,
            account="Anthropic API",
            protocol="Agent SDK ready",
            skills=claude_skills,
            mcps=claude_mcps,
            cli_path=binary,
        )
    if credentials_path.is_file():
        return _provider(
            name="claude",
            ok=True,
            auth="oauth",
            command="ciao auth claude",
            detail=str(credentials_path),
            version=version,
            account="OAuth credentials",
            protocol="Agent SDK ready",
            skills=claude_skills,
            mcps=claude_mcps,
            cli_path=binary,
        )
    account = _claude_oauth_account(config_path)
    if account:
        return _provider(
            name="claude",
            ok=True,
            auth="oauth",
            command="ciao auth claude",
            detail=account,
            version=version,
            account=account.removeprefix("oauthAccount: "),
            protocol="Agent SDK ready",
            skills=claude_skills,
            mcps=claude_mcps,
            cli_path=binary,
        )
    return _provider(
        name="claude",
        ok=False,
        auth="missing",
        command="ciao auth claude",
        detail="Run Claude OAuth or set ANTHROPIC_API_KEY.",
        version=version,
        skills=claude_skills,
        mcps=claude_mcps,
        cli_path=binary,
    )


def _claude_oauth_account(config_path: Path) -> str:
    """Return a short identifier from ``~/.claude.json``'s ``oauthAccount``.

    Returns an empty string when the file is missing, unparseable, or has no
    usable account metadata. We deliberately avoid touching the Keychain: the
    server process often cannot unlock it, and the account block is enough to
    confirm a completed OAuth login.
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    account = data.get("oauthAccount")
    if not isinstance(account, dict) or not account:
        return ""
    email = str(account.get("emailAddress", "")).strip()
    if email:
        return f"oauthAccount: {email}"
    uuid_ = str(account.get("accountUuid", "")).strip()
    if uuid_:
        return f"oauthAccount: {uuid_}"
    return "oauthAccount present"


def _workspace_guides_linked(workspace_root: Path) -> bool:
    """True when AGENTS.md resolves to CLAUDE.md for shared guide loading."""
    claude_guide = workspace_root / "CLAUDE.md"
    shared_guide = workspace_root / "AGENTS.md"
    try:
        return claude_guide.is_file() and shared_guide.resolve() == claude_guide.resolve()
    except OSError:
        return False


def _memory_regions_well_formed(workspace_root: Path) -> bool:
    """True when both bounded memory regions are present and well-formed in CLAUDE.md."""
    from ciao.memory_tool import diagnose_guide

    guide = workspace_root / "CLAUDE.md"
    return guide.is_file() and not diagnose_guide(guide)


def setup_status(
    config: Any,
    *,
    env: Mapping[str, str] | None = None,
    claude_credentials_path: Path | None = None,
    claude_config_path: Path | None = None,
) -> dict[str, Any]:
    """Return setup readiness for the wizard and expert CLI.

    ``env`` is injectable for tests and lets the API route pass the live
    process environment without exposing secret values in the response.
    """
    source = env if env is not None else os.environ
    # ``Path.cwd()`` cannot be the getattr default: it is evaluated even when the
    # config carries an explicit workspace_root, and it raises once the cwd is gone.
    configured_root = getattr(config, "workspace_root", None)
    workspace_root = _resolve_root(configured_root if configured_root is not None else Path("."))
    configured_vault = getattr(config, "vault_root", None)
    vault_root = _resolve_root(
        configured_vault if configured_vault is not None else workspace_root / "memory-vault"
    )
    raw_credentials_path = source.get("CLAUDE_CREDENTIALS_PATH", "").strip()
    credentials_path = (
        claude_credentials_path
        or (Path(raw_credentials_path).expanduser() if raw_credentials_path else None)
        or Path.home() / ".claude" / ".credentials.json"
    )
    config_path = claude_config_path or _claude_config_path_from_env(source)

    checks = [
        _check(
            check_id="workspace",
            label="Workspace folder",
            ok=workspace_root.is_dir(),
            required=True,
            detail=str(workspace_root),
        ),
        _check(
            check_id="vault",
            label="Vault folder",
            ok=vault_root.is_dir(),
            required=True,
            detail=str(vault_root),
        ),
        _check(
            check_id="workspace_guides",
            label="Linked workspace guides",
            ok=_workspace_guides_linked(workspace_root),
            # Optional: a custom AGENTS.md is preserved on purpose, but then
            # the provider guides are no longer shared.
            required=False,
            detail=str(workspace_root / "AGENTS.md"),
        ),
        _check(
            check_id="memory_regions",
            label="Bounded memory regions",
            ok=_memory_regions_well_formed(workspace_root),
            # Optional: sync-skills self-heals missing regions on the next
            # startup, so this is informational rather than blocking.
            required=False,
            detail=str(workspace_root / "CLAUDE.md"),
        ),
        _check(
            check_id="pwa_auth_token",
            label="PWA auth token",
            ok=bool(getattr(config, "pwa_auth_token", "")),
            required=True,
        ),
        _check(
            check_id="push_contact",
            label="Push contact",
            ok=bool(source.get("CIAO_PUSH_CONTACT", "").strip()),
            # Optional: without it Web Push stays disabled, nothing else breaks.
            required=False,
            detail="CIAO_PUSH_CONTACT",
        ),
    ]
    providers = {
        descriptor.id: descriptor.status_probe(
            source,
            config=config,
            credentials_path=credentials_path,
            config_path=config_path,
            workspace_root=workspace_root,
        )
        for descriptor in provider_registry.descriptors()
        if descriptor.status_probe_path
    }
    # Routing backends, not runtime providers: they have no provider module and
    # run through Claude Code, but Settings still shows their credential state.
    configured = all(row["ok"] for row in checks if row["required"])
    provider_ready = any(row["ok"] for row in providers.values())
    bootstrap = bool(getattr(config, "bootstrap_mode", False))
    return {
        "configured": configured and provider_ready,
        "bootstrap": bootstrap,
        "mode": "bootstrap" if bootstrap else "configured",
        "workspace_root": str(workspace_root),
        "vault_root": str(vault_root),
        "checks": checks,
        "providers": providers,
        "provider_ready": provider_ready,
    }
