"""Move the install workspace to another folder: ``ciao workspace-move``.

The install workspace (``CIAO_WORKSPACE``: ``.env``, ``.runtime/``, the vault,
skills, the agent roots) cannot be moved by editing ``.env``: the engine finds
``.env`` *inside* the workspace, and the service definition (the LaunchAgent on
macOS, the logon task on Windows) is what names the folder. A move therefore
has to stop the engine, rename the folder, repair what recorded the old
absolute path, repoint the service definition and start the engine again —
and the engine cannot do that to itself while it runs.

So the work is split the way an engine update is:

* :func:`plan` is read-only. It answers whether a move to ``target`` is safe
  and what the operator should know first. The CLI prints it; the PWA shows it.
* :func:`start` records an operation outside the workspace and hands it to a
  one-shot job that is a *sibling* of the engine's own service (a launchd job,
  a Task Scheduler task), so stopping the engine cannot take the move with it.
  The CLI's ``--apply`` and the Settings button both call it.
* :func:`run_move` is that job: drain, stop, rename, rewrite, repoint, start,
  verify — and on any failure after the rename, put everything back and start
  the engine where it was.

The record lives beside the update state, outside every workspace, so it
survives the move and the PWA can read the outcome once the engine is back.
There is no separate undo: moving the folder back is the same operation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any, Callable, Protocol

from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import make_private, mkstemp_private

OPERATION_NAME = "operation.json"
LOCK_NAME = "move.lock"
JOB_LABEL = "com.ciao.workspace-move"
JOB_PLIST_NAME = f"{JOB_LABEL}.plist"
JOB_TASK_NAME = r"\Ciaobot\WorkspaceMove"
JOB_TASK_FILE = "Ciaobot-WorkspaceMove.xml"
JOB_LOG_NAME = "workspace-move.log"
DEFINITION_BACKUP_PREFIX = "service-definition"
LINUX_UNIT = "ciaobot.service"
LINUX_UNIT_DIR = Path("/etc/systemd/system")
# How docs/LINUX.md installs the CLI; root's PATH does not include the venv.
LINUX_CLI = "/opt/ciaobot/venv/bin/ciao"

PHASES = (
    "queued",
    "draining",
    "stopping",
    "moving",
    "rewriting",
    "repointing",
    "starting",
    "verifying",
    "done",
    "failed",
    "rolling_back",
    "rolled_back",
    "rollback_failed",
)
TERMINAL_PHASES = frozenset({"done", "failed", "rolled_back", "rollback_failed"})

# The `.runtime` JSON files that record absolute paths under the workspace.
# A fixed list rather than every `*.json`: transcripts and caches are large,
# and a cache keyed by an old path simply regenerates.
_RUNTIME_JSON = (
    "web_projects.json",
    "state.json",
    "workspaces.json",
    "background/state.json",
)
# Receipt files keyed by a hash of their vault's absolute path; after a move
# their names no longer match and the migration they record would re-run.
_VAULT_KEYED_RECEIPTS = ("vault-vocabulary.", "retired-stock-types.")
# Never walked for symlinks: large, and nothing Ciaobot links from lives there.
_SYMLINK_WALK_PRUNE = frozenset({".git", "node_modules", ".venv", ".runtime", "__pycache__"})

_POLL_INTERVAL = 1.0
_HTTP_TIMEOUT = 5.0
_IDLE_POLLS_REQUIRED = 3
_DRAIN_TIMEOUT = 600.0
_STOP_TIMEOUT = 30.0
_READY_TIMEOUT = 120.0
_QUEUED_GRACE = 120.0
# Phases in which nothing on disk has changed yet: a job that died here left
# the workspace where it was.
_PRE_MOVE_PHASES = frozenset({"queued", "draining", "stopping"})

_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

GetJson = Callable[[str], "dict[str, Any] | None"]
PostJson = Callable[[str], "dict[str, Any]"]
Sleep = Callable[[float], None]
Clock = Callable[[], float]


class MoveError(RuntimeError):
    """A move could not start or did not finish. The message is for the operator."""


class EngineHost(Protocol):
    """The part of :class:`ciao.update_host.UpdateHost` a move needs."""

    def engine_port(self) -> int: ...

    def start_engine(self) -> Any: ...

    def stop_engine(self, wait: Callable[[], bool] | None = None) -> bool: ...


class ServiceDefinition(Protocol):
    """The engine's service definition, which names the workspace folder."""

    def workspace(self) -> Path | None:
        """The workspace the definition serves, or None when there is none."""
        ...

    def backup(self, dest: Path) -> None:
        """Copy the definition's bytes to ``dest``."""
        ...

    def repoint(self, old: Path, new: Path) -> None:
        """Rewrite every path under ``old`` to ``new`` and make it current."""
        ...

    def restore(self, backup: Path) -> None:
        """Put the bytes saved by :meth:`backup` back and make them current."""
        ...


# ── state ────────────────────────────────────────────────────────────


def default_state_dir() -> Path:
    """Beside the update state: outside every workspace, so it survives the move."""
    from ciao.engine_update import default_state_dir as update_state_dir

    return update_state_dir().parent / "workspace-move"


@dataclass
class MoveOperation:
    id: str
    phase: str
    source: str
    target: str
    started_at: str
    updated_at: str
    error: str = ""
    port: int = 0
    python: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_operation(state_dir: Path | None = None) -> MoveOperation | None:
    """The last recorded move, or None. A malformed file is no record."""
    path = (state_dir or default_state_dir()) / OPERATION_NAME
    try:
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("phase") not in PHASES:
        return None
    try:
        return MoveOperation(
            id=str(parsed["id"]),
            phase=str(parsed["phase"]),
            source=str(parsed["source"]),
            target=str(parsed["target"]),
            started_at=str(parsed.get("started_at", "")),
            updated_at=str(parsed.get("updated_at", "")),
            error=str(parsed.get("error", "")),
            port=int(parsed.get("port", 0) or 0),
            python=str(parsed.get("python", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def write_operation(op: MoveOperation, state_dir: Path | None = None) -> None:
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(root / OPERATION_NAME, json.dumps(asdict(op), indent=2) + "\n", private=True)


def in_flight(op: MoveOperation | None) -> bool:
    return op is not None and op.phase not in TERMINAL_PHASES


def job_running(state_dir: Path | None = None) -> bool:
    """Whether a move job holds the move lock right now."""
    path = (state_dir or default_state_dir()) / LOCK_NAME
    try:
        handle = path.open("rb")
    except OSError:
        return False
    with handle:
        try:
            lock_exclusive(handle.fileno(), blocking=False)
        except OSError:
            return True
        unlock(handle.fileno())
        return False


def _age_seconds(stamp: str) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def abandoned(op: MoveOperation | None, state_dir: Path | None = None) -> bool:
    """An in-flight record no job is working on: the job crashed, or the machine
    rebooted under it. A queued record gets a grace period, because the job takes
    the lock only once launchd or Task Scheduler has started it."""
    if not in_flight(op):
        return False
    assert op is not None
    if op.phase == "queued" and _age_seconds(op.updated_at) < _QUEUED_GRACE:
        return False
    return not job_running(state_dir)


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _keep_owner(path: Path, like: Path, *, follow: bool = True) -> None:
    """Give ``path`` the owner of ``like`` when running as root.

    On Linux the administrator runs the move as root while the files belong to
    the service account; a file root rewrote would otherwise become one the
    engine can no longer write.
    """
    if sys.platform == "win32" or not _running_as_root():
        return
    try:
        st = like.stat()
        if follow:
            os.chown(path, st.st_uid, st.st_gid)
        else:
            os.lchown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def _keep_owner_tree(root: Path, like: Path) -> None:
    if not _running_as_root():
        return
    _keep_owner(root, like)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            _keep_owner(Path(dirpath) / name, like, follow=False)


def _write_text_atomic(target: Path, text: str, *, private: bool = False) -> None:
    fd, tmp_name = mkstemp_private(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if private:
            make_private(tmp)
        else:
            try:
                os.chmod(tmp, target.stat().st_mode & 0o777)
            except OSError:
                pass
        _keep_owner(tmp, target if target.exists() else target.parent)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


# ── plan ─────────────────────────────────────────────────────────────


@dataclass
class MovePlan:
    source: str
    target: str
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refusals

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _python_inside(python: str, source: Path) -> bool:
    try:
        return _is_within(Path(python).absolute(), source)
    except OSError:
        return False


def plan(
    source: Path,
    target_raw: str,
    *,
    registered: Path | None,
    python: str = sys.executable,
    platform: str | None = None,
    update_in_flight: bool = False,
    engine_running: bool = True,
    admin: bool | None = None,
    service_can_reach: Callable[[Path], bool] | None = None,
    state_dir: Path | None = None,
) -> MovePlan:
    """Whether ``source`` can move to ``target_raw``, and what to know first.

    ``registered`` is the workspace the service definition serves: the move
    repoints that definition, so a definition for another folder, or none, is a
    refusal rather than something to guess around.
    """
    platform = platform or sys.platform
    source = source.expanduser().resolve()
    raw = target_raw.strip()
    result = MovePlan(source=str(source), target=raw)
    if platform not in ("darwin", "win32") and not platform.startswith("linux"):
        result.refusals.append(f"Moving the workspace is not supported on {platform}.")
        return result
    if platform.startswith("linux") and not (_running_as_root() if admin is None else admin):
        # The service runs as an unprivileged account and its unit is root's:
        # stopping it and rewriting the unit is the administrator's job, as
        # updates are on Linux.
        result.refusals.append(
            "On Linux the administrator moves the workspace: run "
            f"`sudo {LINUX_CLI} workspace-move <new folder> --apply` on the server. "
            "It stops the systemd service and rewrites its unit."
        )
        return result
    if not raw:
        result.refusals.append("Choose a destination folder.")
        return result
    target = Path(raw).expanduser()
    if not target.is_absolute():
        result.refusals.append(f"The destination must be an absolute path: {raw}")
        return result
    parent = target.parent
    try:
        parent = parent.resolve(strict=True)
    except (OSError, RuntimeError):
        result.refusals.append(f"The folder {target.parent} does not exist.")
        return result
    target = parent / target.name
    result.target = str(target)

    if target == source:
        result.refusals.append("That is the folder the workspace is already in.")
    elif _is_within(target, source):
        result.refusals.append("The destination is inside the current workspace.")
    elif _is_within(source, target):
        result.refusals.append("The destination contains the current workspace.")
    if target.exists() or target.is_symlink():
        if not target.is_dir() or target.is_symlink():
            result.refusals.append(f"{target} already exists and is not a folder.")
        elif any(target.iterdir()):
            result.refusals.append(f"{target} already exists and is not empty.")
    try:
        if parent.stat().st_dev != source.stat().st_dev:
            result.refusals.append(
                "The destination is on a different disk. Ciaobot only moves the workspace "
                "within the same disk, where a move is a rename and cannot be left half-copied."
            )
    except OSError as exc:
        result.refusals.append(f"Could not read {parent}: {exc}")
    if service_can_reach is not None and parent.is_dir() and not service_can_reach(parent):
        # Root can move the folder anywhere, but the engine runs as the service
        # account: it would fail to start there, and the move would roll back
        # only after the full readiness wait.
        result.refusals.append(
            f"The Ciaobot service account cannot open {parent}. Choose a folder it can reach."
        )
    if platform == "darwin":
        from ciao.setup_status import tcc_protected_location

        protected = tcc_protected_location(target)
        if protected:
            result.refusals.append(
                f"The destination is inside ~/{protected}, which macOS privacy protection "
                "blocks the background engine from reading. Choose another folder, e.g. ~/ciaobot."
            )
    if registered is None:
        result.refusals.append(
            "No Ciaobot service is registered, so there is nothing to repoint. "
            "Run `ciao setup --workspace <folder> --load-launchd` instead."
        )
    elif registered.expanduser().resolve() != source:
        result.refusals.append(
            f"The Ciaobot service runs the workspace at {registered}, not {source}."
        )
    if _python_inside(python, source):
        result.refusals.append(
            f"The engine runs from a Python inside the workspace ({python}); "
            "it would be moved out from under itself."
        )
    if update_in_flight:
        result.refusals.append("An engine update is in progress. Try again when it has finished.")
    if not engine_running:
        # The job drains running chats through the engine before it stops it,
        # so a stopped engine is a refusal rather than a mid-move failure.
        result.refusals.append(
            "Ciaobot is not running. Start it with `ciao service start`, then move it."
        )
    previous = read_operation(state_dir)
    if in_flight(previous):
        assert previous is not None
        if not abandoned(previous, state_dir):
            result.refusals.append("A workspace move is already in progress.")
        elif previous.phase not in _PRE_MOVE_PHASES:
            result.refusals.append(
                f"A previous move from {previous.source} to {previous.target} stopped during "
                f"'{previous.phase}' without finishing. Check both folders and the service, then "
                f"delete {(state_dir or default_state_dir()) / OPERATION_NAME} to move again."
            )

    if (source / ".venv").exists():
        result.warnings.append(
            "The Python environment in .venv names its old location and will stop "
            "working; recreate it after the move."
        )
    worktrees = source / ".git" / "worktrees"
    if worktrees.is_dir() and any(worktrees.iterdir()):
        result.warnings.append(
            "This folder has git worktrees. Run `git worktree repair` in the new folder afterwards."
        )
    try:
        schedules: Any = json.loads((source / ".runtime" / "schedules.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        schedules = None
    # Searched in the parsed values, not the raw text: JSON escapes the
    # backslashes of a Windows path.
    if _mentions(schedules, str(source)):
        result.warnings.append(
            "Some automation prompts mention the old folder by path. Edit them after the move."
        )
    return result


def _mentions(data: Any, needle: str) -> bool:
    if isinstance(data, str):
        return needle in data
    if isinstance(data, list):
        return any(_mentions(item, needle) for item in data)
    if isinstance(data, dict):
        return any(_mentions(value, needle) for value in data.values())
    return False


# ── path rewriting ───────────────────────────────────────────────────


def _separators() -> tuple[str, ...]:
    return ("/", "\\") if os.sep == "\\" else ("/",)


def rewrite_prefix(value: str, old: str, new: str) -> str:
    """``value`` with a leading ``old`` path replaced by ``new``; else unchanged.

    Only a whole path component matches: ``/a/ciao`` rewrites ``/a/ciao/x`` but
    not ``/a/ciaobot``.
    """
    if value == old:
        return new
    for sep in _separators():
        if value.startswith(old + sep):
            return new + value[len(old):]
    return value


def _rewrite_tree(data: Any, old: str, new: str) -> Any:
    if isinstance(data, str):
        return rewrite_prefix(data, old, new)
    if isinstance(data, list):
        return [_rewrite_tree(item, old, new) for item in data]
    if isinstance(data, dict):
        return {key: _rewrite_tree(value, old, new) for key, value in data.items()}
    return data


def rewrite_json_file(path: Path, old: str, new: str) -> bool:
    """Rewrite path values under ``old`` in one JSON file. True when it changed."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return False
    rewritten = _rewrite_tree(data, old, new)
    if rewritten == data:
        return False
    indent = 2 if raw.lstrip().startswith(("{\n", "[\n")) else None
    _write_text_atomic(path, json.dumps(rewritten, indent=indent, ensure_ascii=False) + "\n")
    return True


def rewrite_env_file(path: Path, old: str, new: str) -> bool:
    """Rewrite absolute paths under ``old`` in ``.env`` values. True when it changed."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    boundary = "".join(re.escape(sep) for sep in _separators())
    pattern = re.compile(re.escape(old) + rf"(?=[{boundary}\"']|\s|$)", re.MULTILINE)
    lines = raw.splitlines(keepends=True)
    out = []
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            value = pattern.sub(lambda _match: new, value)
        out.append(key + sep + value)
    rewritten = "".join(out)
    if rewritten == raw:
        return False
    _write_text_atomic(path, rewritten, private=True)
    return True


def rekey_vault_receipts(migration_dir: Path) -> list[str]:
    """Rename vault-keyed receipts to the key of the vault path they now record."""
    from ciao import vault_migration

    renamed: list[str] = []
    if not migration_dir.is_dir():
        return renamed
    for path in sorted(migration_dir.glob("*.json")):
        prefix = next((p for p in _VAULT_KEYED_RECEIPTS if path.name.startswith(p)), None)
        if prefix is None or path.name == vault_migration.RECEIPT_NAME:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        vault = data.get("vault_root") if isinstance(data, dict) else None
        if not vault:
            continue
        runtime = migration_dir.parent
        if prefix == "vault-vocabulary.":
            expected = vault_migration.receipt_path(runtime, Path(vault))
        else:
            expected = vault_migration.retired_receipt_path(runtime, Path(vault))
        if expected.name != path.name and not expected.exists():
            path.rename(expected)
            renamed.append(expected.name)
    return renamed


def relink_symlinks(root: Path, old: str, new: str) -> int:
    """Repoint links under ``root`` (symlinks; junctions on Windows) that name ``old``."""
    from ciao.os_support import links

    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in [*dirnames, *filenames]:
            link = base / name
            if not links.is_link(link):
                continue
            try:
                current = links.link_target(link)
            except OSError:
                continue
            if not Path(current).is_absolute():
                continue
            updated = rewrite_prefix(str(Path(current)), old, new)
            if updated == str(Path(current)):
                continue
            links.remove_link(link)
            if Path(updated).is_dir():
                links.link_dir(updated, link)
            else:
                links.link_file(updated, link)
            _keep_owner(link, base, follow=False)
            count += 1
        # Never descend into a link, and never into the pruned trees.
        dirnames[:] = [
            d for d in dirnames
            if d not in _SYMLINK_WALK_PRUNE and not links.is_link(base / d)
        ]
    return count


def rewrite_workspace_state(root: Path, old: str, new: str) -> None:
    """Rewrite everything inside the moved workspace that names ``old``."""
    rewrite_env_file(root / ".env", old, new)
    runtime = root / ".runtime"
    for name in _RUNTIME_JSON:
        rewrite_json_file(runtime / name, old, new)
    migration = runtime / "migration"
    if migration.is_dir():
        for path in sorted(migration.glob("*.json")):
            rewrite_json_file(path, old, new)
        rekey_vault_receipts(migration)
    relink_symlinks(root, old, new)


def _agent_roots(root: Path) -> list[Path]:
    """The workspace and its immediate subfolders: every folder a chat runs in."""
    roots = [root]
    try:
        roots.extend(sorted(child for child in root.iterdir() if child.is_dir()))
    except OSError:
        pass
    return roots


def copy_claude_sessions(old: Path, new: Path, *, home: Path | None = None) -> list[str]:
    """Copy Claude Code's per-folder session stores to the new folders' names.

    Claude Code keeps a folder's sessions under ``~/.claude/projects/<slug>``;
    without a copy every chat in the moved workspace would silently start
    fresh. Copied, never moved: the originals are what a rollback, or a
    Claude Code session still open on the old path, would read.
    """
    from ciao.agent_paths import claude_project_slug

    projects = (home or Path.home()) / ".claude" / "projects"
    copied: list[str] = []
    for new_root in _agent_roots(new):
        old_root = old / new_root.relative_to(new)
        source = projects / claude_project_slug(old_root)
        dest = projects / claude_project_slug(new_root)
        if source == dest or not source.is_dir():
            continue
        shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
        _keep_owner_tree(dest, source)
        copied.append(dest.name)
    return copied


def rekey_claude_json(old: str, new: str, *, home: Path | None = None) -> int:
    """Copy ``~/.claude.json`` per-folder entries (trust, MCP approvals) to the new paths."""
    path = (home or Path.home()) / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return 0
    added = 0
    for key, value in list(projects.items()):
        updated = rewrite_prefix(key, old, new)
        if updated != key and updated not in projects:
            projects[updated] = value
            added += 1
    if added:
        _write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return added


# ── service definitions ──────────────────────────────────────────────


class MacServiceDefinition:
    """``~/Library/LaunchAgents/com.ciao.server.plist``."""

    def __init__(self, path: Path | None = None) -> None:
        from ciao import macos_service

        self.path = path or macos_service.default_launch_agents_dir() / f"{macos_service.SERVER_LABEL}.plist"

    def _load(self) -> dict[str, Any]:
        import plistlib

        with self.path.open("rb") as handle:
            loaded: Any = plistlib.load(handle)
        if not isinstance(loaded, dict):
            raise MoveError(f"{self.path} is not a launchd job definition")
        return loaded

    def workspace(self) -> Path | None:
        try:
            plist = self._load()
        except (OSError, ValueError, MoveError):
            return None
        env = plist.get("EnvironmentVariables")
        value = env.get("CIAO_WORKSPACE") if isinstance(env, dict) else None
        value = value or plist.get("WorkingDirectory")
        return Path(str(value)) if value else None

    def backup(self, dest: Path) -> None:
        shutil.copy2(self.path, dest)

    def repoint(self, old: Path, new: Path) -> None:
        from ciao.update_host import _write_plist

        rewritten: Any = _rewrite_tree(self._load(), str(old), str(new))
        _write_plist(rewritten, self.path)

    def restore(self, backup: Path) -> None:
        shutil.copy2(backup, self.path)


class WindowsServiceDefinition:
    """The engine logon task's XML, re-registered after every change."""

    def __init__(self, path: Path | None = None) -> None:
        from ciao import windows_service

        self.path = path or windows_service.live_task_dir() / windows_service.TASK_FILE_NAME

    def workspace(self) -> Path | None:
        from ciao import windows_service

        try:
            if not windows_service.task_exists():
                return None
        except windows_service.WindowsServiceError:
            return None
        return windows_service.task_workspace(self.path)

    def backup(self, dest: Path) -> None:
        shutil.copy2(self.path, dest)

    def repoint(self, old: Path, new: Path) -> None:
        from xml.sax.saxutils import escape

        from ciao import windows_service

        xml = self.path.read_bytes().decode("utf-16")
        current = windows_service.task_workspace(self.path)
        if current is None:
            raise MoveError(f"{self.path} names no working directory")
        updated = rewrite_prefix(str(current), str(old), str(new))
        rewritten = xml.replace(
            f"<WorkingDirectory>{escape(str(current))}</WorkingDirectory>",
            f"<WorkingDirectory>{escape(updated)}</WorkingDirectory>",
        )
        if updated == str(current) or rewritten == xml:
            raise MoveError(f"could not repoint the working directory in {self.path}")
        self.path.write_bytes(rewritten.encode("utf-16"))
        windows_service.register_task(self.path)

    def restore(self, backup: Path) -> None:
        from ciao import windows_service

        shutil.copy2(backup, self.path)
        windows_service.register_task(self.path)


class LinuxServiceDefinition:
    """The root-installed systemd unit (docs/LINUX.md), reloaded after every change."""

    def __init__(self, path: Path | None = None, *, systemctl: Callable[..., Any] | None = None) -> None:
        self.path = path or LINUX_UNIT_DIR / LINUX_UNIT
        self._systemctl = systemctl or _systemctl

    def _setting(self, key: str) -> str | None:
        """The last ``key=`` value in the unit (systemd: a later line wins)."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        found: str | None = None
        for line in lines:
            name, sep, value = line.strip().partition("=")
            if sep and name == key:
                found = value
        return found

    def _environment(self, name: str) -> str | None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        found: str | None = None
        for line in lines:
            key, sep, value = line.strip().partition("=")
            if not sep or key != "Environment":
                continue
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            var, eq, rest = value.partition("=")
            if eq and var == name:
                found = rest.replace("%%", "%")
        return found

    def workspace(self) -> Path | None:
        value = self._environment("CIAO_WORKSPACE") or self._setting("WorkingDirectory")
        return Path(value.replace("%%", "%")) if value else None

    def home(self) -> Path | None:
        value = self._environment("HOME")
        return Path(value) if value else None

    def user(self) -> str | None:
        return self._setting("User")

    def backup(self, dest: Path) -> None:
        shutil.copy2(self.path, dest)

    def repoint(self, old: Path, new: Path) -> None:
        text = self.path.read_text(encoding="utf-8")
        rewritten = text
        # The unit holds the path as written and with `%` doubled (specifiers).
        for before, after in ((str(old), str(new)), (str(old).replace("%", "%%"), str(new).replace("%", "%%"))):
            pattern = re.compile(re.escape(before) + r'(?=[/"\s]|$)', re.MULTILINE)
            rewritten = pattern.sub(after.replace("\\", r"\\"), rewritten)
        if rewritten == text:
            raise MoveError(f"could not repoint the workspace in {self.path}")
        _write_text_atomic(self.path, rewritten)
        self._reload()

    def restore(self, backup: Path) -> None:
        shutil.copy2(backup, self.path)
        self._reload()

    def _reload(self) -> None:
        completed = self._systemctl("daemon-reload")
        if completed.returncode != 0:
            raise MoveError(_completed_detail(completed) or "systemctl daemon-reload failed")


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=180,
    )


def _completed_detail(completed: Any) -> str:
    return str(getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or "").strip()


class LinuxEngineHost:
    """``systemctl`` for the engine's unit; the port from the workspace ``.env``."""

    def __init__(self, workspace: Path, *, systemctl: Callable[..., Any] | None = None) -> None:
        self._workspace = workspace
        self._systemctl = systemctl or _systemctl

    def engine_port(self) -> int:
        from ciao.macos_service import read_dotenv

        raw = read_dotenv(self._workspace / ".env").get("PWA_PORT", "").strip()
        try:
            return int(raw)
        except ValueError:
            return 8443

    def start_engine(self) -> Any:
        completed = self._systemctl("start", LINUX_UNIT)
        ok = completed.returncode == 0
        return SimpleNamespace(ok=ok, message="" if ok else _completed_detail(completed))

    def stop_engine(self, wait: Callable[[], bool] | None = None) -> bool:
        # Synchronous: systemd returns once the unit's control group is gone.
        self._systemctl("stop", LINUX_UNIT)
        return wait() if wait is not None else True


def current_service_definition() -> ServiceDefinition:
    if sys.platform == "win32":
        return WindowsServiceDefinition()
    if sys.platform.startswith("linux"):
        return LinuxServiceDefinition()
    return MacServiceDefinition()


def current_engine_host(workspace: Path) -> EngineHost:
    if sys.platform.startswith("linux"):
        return LinuxEngineHost(workspace)
    from ciao.update_host import current_update_host

    return current_update_host()


# ── start: record the operation and hand it to the detached job ──────


def start(
    move_plan: MovePlan,
    *,
    port: int,
    python: str = sys.executable,
    state_dir: Path | None = None,
    spawn: Callable[[MoveOperation, Path], None] | None = None,
) -> MoveOperation:
    """Record the move and start the job that performs it. Raises ``MoveError``."""
    if not move_plan.ok:
        raise MoveError(" ".join(move_plan.refusals))
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = _now()
    op = MoveOperation(
        id=f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
        phase="queued",
        source=move_plan.source,
        target=move_plan.target,
        started_at=stamp,
        updated_at=stamp,
        port=port,
        python=python,
    )
    write_operation(op, root)
    try:
        (spawn or spawn_job)(op, root)
    except Exception as exc:  # noqa: BLE001 — recorded, then re-raised as ours
        op.phase = "failed"
        op.error = f"could not start the move job: {exc}"
        op.updated_at = _now()
        write_operation(op, root)
        raise MoveError(op.error) from exc
    return op


def _job_argv(op: MoveOperation, state_dir: Path) -> list[str]:
    return [
        op.python,
        "-I",
        "-m",
        "ciao.workspace_move",
        "run",
        "--operation",
        op.id,
        "--state-dir",
        str(state_dir),
    ]


def spawn_job(op: MoveOperation, state_dir: Path) -> None:
    """Start the move as a sibling of the engine's service, never as its child.

    On Linux the administrator's own root shell is already outside the
    service's control group, so the move runs right here, in the foreground.
    """
    if sys.platform == "win32":
        _spawn_windows_job(op, state_dir)
    elif sys.platform.startswith("linux"):
        execute(op.id, state_dir)
    else:
        _spawn_mac_job(op, state_dir)


def _spawn_mac_job(op: MoveOperation, state_dir: Path) -> None:
    from ciao import macos_service
    from ciao.update_host import _write_plist

    log = str(state_dir / JOB_LOG_NAME)
    plist_path = _write_plist(
        {
            "Label": JOB_LABEL,
            "ProgramArguments": _job_argv(op, state_dir),
            "WorkingDirectory": str(state_dir),
            "RunAtLoad": True,
            "KeepAlive": False,
            "AbandonProcessGroup": True,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        },
        state_dir / JOB_PLIST_NAME,
    )
    domain = f"gui/{macos_service._getuid()}"
    macos_service._launchctl(["bootout", f"{domain}/{JOB_LABEL}"])
    bootstrap = macos_service._launchctl(["bootstrap", domain, str(plist_path)])
    if bootstrap.returncode != 0:
        detail = (bootstrap.stderr or bootstrap.stdout or "").strip()
        raise MoveError(detail or "launchctl bootstrap failed")


def _spawn_windows_job(op: MoveOperation, state_dir: Path) -> None:
    from ciao import windows_service

    argv = _job_argv(op, state_dir)
    xml = windows_service._update_job_xml(
        task_name=JOB_TASK_NAME,
        description="Moves the Ciaobot workspace folder, then exits.",
        triggers="",
        python=windows_service.windowless_python(argv[0]),
        arguments=argv[1:],
        # Never the workspace: Windows refuses to rename a folder that is
        # some process's working directory.
        workdir=str(state_dir),
        user=windows_service.current_user(),
    )
    definition = state_dir / JOB_TASK_FILE
    definition.write_bytes(xml.encode("utf-16"))
    windows_service.register_task(definition, JOB_TASK_NAME)
    completed = windows_service._schtasks("/Run", "/TN", JOB_TASK_NAME)
    if completed.returncode != 0:
        raise MoveError((completed.stderr or completed.stdout or "").strip() or "schtasks /Run failed")


# ── the detached job ─────────────────────────────────────────────────


def _get_json(url: str) -> dict[str, Any] | None:
    try:
        with _LOCAL_OPENER.open(url, timeout=_HTTP_TIMEOUT) as response:
            parsed: Any = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — unreachable is an answer here
        return None
    return parsed if isinstance(parsed, dict) else None


def _post_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="POST", data=b"")
    with _LOCAL_OPENER.open(request, timeout=_HTTP_TIMEOUT) as response:
        parsed: Any = json.loads(response.read().decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _server_lock_held(workspace: Path) -> bool:
    """Whether a process holds ``workspace``'s engine ``server.lock``.

    The runtime root is resolved the way the engine resolves it:
    ``CIAO_RUNTIME_ROOT`` from the workspace's ``.env``, else ``.runtime``.
    """
    from ciao.macos_service import read_dotenv

    runtime_raw = read_dotenv(workspace / ".env").get("CIAO_RUNTIME_ROOT", "").strip()
    runtime = Path(runtime_raw).expanduser() if runtime_raw else workspace / ".runtime"
    if not runtime.is_absolute():
        runtime = workspace / runtime
    try:
        handle = (runtime / "server.lock").open("rb")
    except OSError:
        return False
    with handle:
        try:
            lock_exclusive(handle.fileno(), blocking=False)
        except OSError:
            return True
        unlock(handle.fileno())
        return False


def _lock(state_dir: Path) -> IO[str]:
    handle = open(state_dir / LOCK_NAME, "a+", encoding="utf-8", newline="")
    try:
        lock_exclusive(handle.fileno(), blocking=False)
    except OSError as exc:
        handle.close()
        raise MoveError("another workspace move is running") from exc
    return handle


def run_move(
    op: MoveOperation,
    *,
    state_dir: Path,
    host: EngineHost,
    service: ServiceDefinition,
    get: GetJson = _get_json,
    post: PostJson = _post_json,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
    claude_home: Path | None = None,
) -> MoveOperation:
    """Perform ``op``: the detached half. Returns the final record."""
    source, target = Path(op.source), Path(op.target)
    base = f"http://localhost:{op.port or host.engine_port()}"
    backup = state_dir / f"{DEFINITION_BACKUP_PREFIX}-{op.id}"

    def advance(phase: str, error: str = "") -> None:
        op.phase = phase
        op.error = error
        op.updated_at = _now()
        write_operation(op, state_dir)

    # The folder the engine is running from: where to look for its lock.
    engine_root = source

    def wait_until_unreachable() -> bool:
        # The port closes before the process has finished shutting down; its
        # last writes go to absolute paths under the folder, so the stop is
        # confirmed only once `server.lock` is released as well.
        deadline = clock() + _STOP_TIMEOUT
        while get(f"{base}/api/startup-status") is not None or _server_lock_held(engine_root):
            if clock() >= deadline:
                return False
            sleep(_POLL_INTERVAL)
        return True

    def wait_until_ready() -> bool:
        deadline = clock() + _READY_TIMEOUT
        while True:
            body = get(f"{base}/api/startup-status") or {}
            if body.get("overall_ready") is True:
                return True
            if clock() >= deadline:
                return False
            sleep(_POLL_INTERVAL)

    def reopen() -> None:
        try:
            post(f"{base}/api/admin/drain/cancel")
        except Exception:  # noqa: BLE001 — best effort
            pass

    # Drain, then stop. Nothing has changed on disk yet, so a failure here
    # leaves the engine where it was, admitting turns again.
    try:
        advance("draining")
        deadline = clock() + _DRAIN_TIMEOUT
        idle = 0
        while idle < _IDLE_POLLS_REQUIRED:
            body = post(f"{base}/api/admin/drain")
            idle = 0 if body.get("active_chat_ids") else idle + 1
            if idle >= _IDLE_POLLS_REQUIRED:
                break
            if clock() >= deadline:
                raise MoveError(f"chats were still running after {int(_DRAIN_TIMEOUT)}s")
            sleep(_POLL_INTERVAL)
        advance("stopping")
        if not host.stop_engine(wait=wait_until_unreachable):
            host.start_engine()
            raise MoveError("the engine did not stop")
    except Exception as exc:  # noqa: BLE001 — recorded for the operator
        reopen()
        advance("failed", str(exc) or exc.__class__.__name__)
        return op

    moved = False
    backed_up = False
    try:
        advance("moving")
        if target.is_dir() and not any(target.iterdir()):
            target.rmdir()
        os.rename(source, target)
        moved = True
        engine_root = target
        advance("rewriting")
        rewrite_workspace_state(target, str(source), str(target))
        copy_claude_sessions(source, target, home=claude_home)
        rekey_claude_json(str(source), str(target), home=claude_home)
        advance("repointing")
        service.backup(backup)
        # Restored on any failure from here on: a repoint can fail after it has
        # already rewritten the definition (Windows writes the XML, then
        # registers it).
        backed_up = True
        service.repoint(source, target)
        advance("starting")
        started = host.start_engine()
        if not bool(getattr(started, "ok", True)):
            raise MoveError(str(getattr(started, "message", "") or "the engine did not start"))
        advance("verifying")
        if not wait_until_ready():
            raise MoveError(f"the engine did not become ready within {int(_READY_TIMEOUT)}s")
    except Exception as exc:  # noqa: BLE001 — every failure rolls back
        reason = str(exc) or exc.__class__.__name__
        advance("rolling_back", reason)
        try:
            if not host.stop_engine(wait=wait_until_unreachable):
                raise MoveError("the engine at the new folder did not stop")
            if backed_up:
                service.restore(backup)
            if moved:
                rewrite_workspace_state(target, str(target), str(source))
                os.rename(target, source)
            host.start_engine()
            if not wait_until_ready():
                raise MoveError("the engine did not come back at the old folder")
        except Exception as rollback_exc:  # noqa: BLE001
            advance("rollback_failed", f"{reason}; rollback: {rollback_exc}")
            return op
        advance("rolled_back", reason)
        return op
    backup.unlink(missing_ok=True)
    advance("done")
    return op


# ── helpers for the CLI and the PWA ──────────────────────────────────


def registered_workspace() -> Path | None:
    return current_service_definition().workspace()


def update_in_flight() -> bool:
    from ciao.engine_update import _IN_FLIGHT_PHASES, read_operation as read_update

    update = read_update()
    return update is not None and update.phase in _IN_FLIGHT_PHASES


def engine_port() -> int:
    workspace = registered_workspace() or Path(".")
    return current_engine_host(workspace).engine_port()


def engine_running() -> bool:
    try:
        port = engine_port()
    except Exception:  # noqa: BLE001 — no update host: the plan refuses the platform
        return False
    return _get_json(f"http://localhost:{port}/api/startup-status") is not None


def _linux_service_can_reach() -> Callable[[Path], bool] | None:
    """On Linux as root: whether the unit's account can enter a folder."""
    if not sys.platform.startswith("linux") or not _running_as_root():
        return None
    user = LinuxServiceDefinition().user()
    if not user:
        return None

    def can_reach(folder: Path) -> bool:
        completed = subprocess.run(
            ["runuser", "-u", user, "--", "test", "-x", str(folder)],
            capture_output=True, check=False, timeout=30,
        )
        return completed.returncode == 0

    return can_reach


def plan_for(source: Path, target_raw: str) -> MovePlan:
    """:func:`plan` with this machine's service, engine, update and move state."""
    return plan(
        source,
        target_raw,
        registered=registered_workspace(),
        update_in_flight=update_in_flight(),
        engine_running=engine_running(),
        service_can_reach=_linux_service_can_reach(),
    )


def execute(operation_id: str, state_dir: Path) -> MoveOperation:
    """Run the queued move ``operation_id`` with this machine's host. Raises ``MoveError``."""
    op = read_operation(state_dir)
    if op is None or op.id != operation_id or op.phase != "queued":
        raise MoveError(f"no queued workspace move {operation_id}")
    handle = _lock(state_dir)
    try:
        service = current_service_definition()
        home = service.home() if isinstance(service, LinuxServiceDefinition) else None
        return run_move(
            op,
            state_dir=state_dir,
            host=current_engine_host(Path(op.source)),
            service=service,
            claude_home=home,
        )
    finally:
        unlock(handle.fileno())
        handle.close()


def main(argv: list[str] | None = None) -> int:
    """``python -m ciao.workspace_move run --operation <id>``: the detached job."""
    parser = argparse.ArgumentParser(prog="python -m ciao.workspace_move")
    sub = parser.add_subparsers(dest="verb", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--operation", required=True)
    run_parser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    state_dir = args.state_dir or default_state_dir()
    try:
        result = execute(args.operation, state_dir)
    except MoveError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{result.phase}: {result.error}" if result.error else result.phase)
    return 0 if result.phase == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
