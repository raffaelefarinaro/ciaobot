"""Shared brains: Git checkouts a workspace links for read access.

The user clones a team's notes folder; Ciaobot only links it (by absolute path),
names it, reads its state and keeps it current with a fast-forward-only pull.
The engine never clones, never connects to a URL itself and never rewrites a
folder: no reset, rebase, stash, checkout, clean or push. Every update first
vets the folder's own git config, then that it is still its own checkout, still
outside the install and vaults, and still fetches from the identity it was linked
with.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from contextlib import ExitStack, asynccontextmanager, contextmanager
from typing import Any, Literal

from ciao import job_runs
from ciao.config import (
    DEFAULT_SHARING_POLICY,
    SHARED_BRAIN_NAME_PATTERN,
    SHARING_POLICIES,
    SharedBrainLink,
    SharingConsent,
    shared_brains_from_rows,
)
from ciao.git_proc import EXACT_BYTES, GIT_TIMEOUT_DETAIL, run_git
from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.locks import await_exclusive, lock_exclusive, unlock
from ciao.os_support.private import (
    is_private_descriptor_strict,
    make_private_dir,
    mkstemp_private,
)
from ciao.os_support.child_env import child_env
from ciao.redact import redact_text
from ciao.repo_link import validate_repo_path

logger = logging.getLogger(__name__)

UPDATE_INTERVAL_S = 1800
FIRST_UPDATE_DELAY_S = 60
FETCH_TIMEOUT_S = 60.0
GIT_TIMEOUT_S = 5.0
MAX_REASON_CHARS = 300
MAX_BRAINS_PER_WORKSPACE = 5
MAX_BRAINS_PER_PASS = 20
NAME_PATTERN = SHARED_BRAIN_NAME_PATTERN
OPERATOR_ONLY_KEYS = ("shared_brains",)

Outcome = Literal["updated", "up_to_date", "skipped", "failed"]

_NAME_RE = re.compile(NAME_PATTERN)
#: Prepended to every git call here. ``core.hooksPath`` points at the null device
#: so a checkout's own hooks never run; the credential helper is emptied so no
#: helper from the checkout's config runs; maintenance and gc never start; only
#: https and ssh transports are allowed, so ext::, fd:: and file:// never run.
_HARDEN: tuple[str, ...] = (
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "protocol.ssh.allow=always",
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "maintenance.auto=false",
    "-c", "gc.auto=0",
    "-c", "credential.helper=",
)
#: The only settings a folder's own config (local or worktree scope) may hold.
#: Anything else can run a program, redirect a URL or read more config on git's
#: behalf, and the folder is refused. Exact-case keys: git keeps subsection case,
#: so ``remote.ORIGIN.url`` is a different remote and is refused.
_SAFE_PLAIN = frozenset({
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
    "core.ignorecase", "core.precomposeunicode", "core.symlinks",
    "fetch.prune",
    "extensions.worktreeconfig", "extensions.objectformat", "extensions.refstorage",
})
_ORIGIN_FETCH = "+refs/heads/*:refs/remotes/origin/*"
_BRANCH_KEY_RE = re.compile(r"branch\..+\.(remote|merge)")

_locks: dict[str, asyncio.Lock] = {}
_last_update: dict[str, dict[str, Any]] = {}
_pending: dict[str, asyncio.Task[Any]] = {}
_cursor = 0
_updates_closed = False
_update_loop_task: asyncio.Task[None] | None = None
_shutdown_task: asyncio.Task[None] | None = None


def _git_env(folder: Path) -> dict[str, str]:
    """Child environment for git on a shared brain.

    Every inherited ``GIT_*`` variable is dropped first (GIT_DIR, GIT_WORK_TREE,
    GIT_CONFIG_*, ...): they would point git at another repository or config.
    ``GIT_CEILING_DIRECTORIES`` stops discovery at the folder's parent, so a
    folder whose own ``.git`` is gone is not read as part of a parent repository.
    ``GIT_SSH_COMMAND`` outranks a checkout's ``core.sshCommand``; prompts are off.
    """
    env = child_env()
    for name in [key for key in env if key.startswith("GIT_")]:
        del env[name]
    env["GIT_CEILING_DIRECTORIES"] = str(folder.parent)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    env["GIT_ASKPASS"] = ""
    env.pop("SSH_ASKPASS", None)
    return env


def _git(path: Path, *args: str) -> tuple[int, str]:
    """(returncode, stdout) of a quick sync git call; (1, "") on spawn failure or timeout."""
    try:
        proc = subprocess.run(
            ["git", *EXACT_BYTES, *_HARDEN, "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=_git_env(path),
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout


async def _git_async(folder: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str, str]:
    return await run_git(
        folder, *_HARDEN, *args, timeout=timeout, env=_git_env(folder)
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sharing_grant_stored(link: SharedBrainLink) -> bool:
    """True when the registry row still carries an automatic consent."""
    return link.sharing_policy == "automatic" and link.sharing_consent is not None


def sharing_consent_active(link: SharedBrainLink) -> bool:
    """True only when the link carries a consent matching its generation and target.

    The three bindings are independent guards: a registry edit that changes the
    identity, a relink that changes the lifetime, and a revocation that bumps
    the generation each invalidate the consent on their own. A future publisher
    must hold the shared checkout/registry fencing across this check, the
    mutation and the receipt so a revocation cannot interleave.
    """
    consent = link.sharing_consent
    return (
        link.sharing_policy == "automatic"
        and consent is not None
        and consent.generation == link.sharing_generation
        and consent.identity == link.identity
        and consent.lifetime_id == link.lifetime_id
    )


def set_sharing_policy(link: SharedBrainLink, policy: str) -> SharedBrainLink:
    """The link with ``policy`` applied, rotating consent durably.

    A grant stores a fresh consent at a new generation; a revocation clears the
    consent and bumps the generation again, so neither an old consent nor a
    publisher that captured one can survive a change of mind. Repeating the
    current choice returns the same link, so a double-submitted Settings save
    does not invent a new generation.
    """
    if not isinstance(policy, str) or policy not in SHARING_POLICIES:
        raise ValueError("sharing policy must be review or automatic")
    if policy == "review":
        if link.sharing_policy == DEFAULT_SHARING_POLICY and link.sharing_consent is None:
            return link
        return replace(
            link,
            sharing_policy=DEFAULT_SHARING_POLICY,
            sharing_generation=link.sharing_generation + 1,
            sharing_consent=None,
        )
    if sharing_consent_active(link):
        return link
    generation = link.sharing_generation + 1
    return replace(
        link,
        sharing_policy="automatic",
        sharing_generation=generation,
        sharing_consent=SharingConsent(
            generation=generation,
            identity=link.identity,
            lifetime_id=link.lifetime_id,
            granted_at=_now(),
        ),
    )


def reset_link_for_restore(link: SharedBrainLink) -> SharedBrainLink:
    """A restored link starts a new lifetime and a fresh review state.

    An archive is a pause, not a durable standing permission. The old lifetime
    is retired so a stale tab or publisher can never match the restored link,
    the consent is dropped, and the generation advances for audit continuity.
    """
    return replace(
        link,
        lifetime_id=secrets.token_urlsafe(24),
        sharing_policy=DEFAULT_SHARING_POLICY,
        sharing_generation=link.sharing_generation + 1,
        sharing_consent=None,
    )


MAX_SHARING_REVOCATIONS = 256
_SHARING_REVOCATIONS_SCHEMA = 2
@dataclass(frozen=True, slots=True)
class SharingRevocations:
    """A loaded revocation journal; unhealthy or full means fail closed."""

    keys: frozenset[tuple[str, int]] = frozenset()
    healthy: bool = True
    fail_closed: bool = False


def _sharing_revocations_path(config: Any) -> Path:
    return Path(config.state_path).parent / "sharing-revocations.json"


def load_sharing_revocations(config: Any) -> SharingRevocations:
    """Load the durable revocation journal.

    A missing file is UNHEALTHY: the journal is required to prove that a
    persisted automatic grant was not revoked. An existing file that cannot be
    read, is not JSON, or has the wrong shape is also UNHEALTHY: callers must
    fail closed (demote every stored automatic grant), never treat it as empty. A
    journal marked ``fail_closed`` means the bounded journal overflowed and all
    automatic grants must stay review until a later successful write replaces it.
    """
    path = _sharing_revocations_path(config)
    if not path.exists():
        # The journal is required to prove that a persisted automatic grant has
        # not been revoked. Missing is not proof of an empty healthy journal.
        logger.error("The shared-brain revocation journal is missing; failing closed")
        return SharingRevocations(healthy=False, fail_closed=True)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        logger.error("The shared-brain revocation journal is unreadable; failing closed")
        return SharingRevocations(healthy=False, fail_closed=True)
    if not isinstance(raw, dict) or raw.get("schema") not in (1, _SHARING_REVOCATIONS_SCHEMA):
        logger.error("The shared-brain revocation journal is malformed; failing closed")
        return SharingRevocations(healthy=False, fail_closed=True)
    fail_closed = raw.get("fail_closed") is True
    grants = raw.get("grants")
    if not isinstance(grants, list):
        logger.error("The shared-brain revocation journal has no grant list; failing closed")
        return SharingRevocations(healthy=False, fail_closed=True)
    found: set[tuple[str, int]] = set()
    for row in grants:
        if not isinstance(row, dict):
            logger.error("The shared-brain revocation journal has an invalid grant row; failing closed")
            return SharingRevocations(healthy=False, fail_closed=True)
        lifetime = row.get("lifetime_id")
        generation = row.get("generation")
        if (
            not isinstance(lifetime, str)
            or not lifetime
            or type(generation) is not int
            or generation < 0
        ):
            logger.error("The shared-brain revocation journal has an invalid grant; failing closed")
            return SharingRevocations(healthy=False, fail_closed=True)
        found.add((lifetime, generation))
    return SharingRevocations(
        keys=frozenset(found), healthy=True, fail_closed=fail_closed
    )


def _persisted_automatic_grants(registry_bytes: bytes | None) -> set[tuple[str, int]] | None:
    """Keys of automatic grants still present in the persisted registry, or None."""
    if registry_bytes is None:
        return None
    try:
        data = json.loads(registry_bytes.decode("utf-8", "strict"))
    except (UnicodeError, ValueError):
        return None
    if isinstance(data, dict):
        rows = list(data.values())
    elif isinstance(data, list):
        rows = data
    else:
        return None
    found: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        links = row.get("shared_brains", [])
        if not isinstance(links, list):
            return None
        for link in links:
            if not isinstance(link, dict):
                return None
            generation = link.get("sharing_generation")
            consent = link.get("sharing_consent")
            lifetime = link.get("lifetime_id")
            if (
                link.get("kind", "git") == "git"
                and link.get("sharing_policy") == "automatic"
                and isinstance(lifetime, str)
                and lifetime
                and type(generation) is int
                and isinstance(consent, dict)
                and consent.get("generation") == generation
            ):
                found.add((lifetime, generation))
    return found


def _bounded_revocation_keys(
    keys: set[tuple[str, int]], registry_bytes: bytes | None
) -> tuple[set[tuple[str, int]], bool]:
    """Drop entries already repaired in the registry and bound the journal.

    Only a revocation whose exact old grant is still in the persisted registry
    needs to survive. Once a successful registry write has moved that link to
    review, its journal entry is safe to drop. If the result exceeds the bound,
    keep the journal bounded and mark it fail-closed instead of dropping keys
    that might still be needed.
    """
    pending = _persisted_automatic_grants(registry_bytes)
    if pending is not None:
        keys = {key for key in keys if key in pending}
    if len(keys) > MAX_SHARING_REVOCATIONS:
        return set(), True
    return keys, False


def save_sharing_revocations(
    config: Any,
    keys: set[tuple[str, int]],
    *,
    registry_bytes: bytes | None = None,
    force_fail_closed: bool = False,
) -> None:
    """Atomically persist a compacted, bounded revocation journal; raises on failure."""
    keys, fail_closed = _bounded_revocation_keys(set(keys), registry_bytes)
    fail_closed = fail_closed or force_fail_closed
    if fail_closed and not force_fail_closed:
        logger.error("The shared-brain revocation journal exceeded its bound; failing closed")
    parent = Path(config.state_path).parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(parent)
    rows = [
        {"lifetime_id": lifetime, "generation": generation}
        for lifetime, generation in sorted(keys)
    ]
    payload = (
        json.dumps(
            {
                "schema": _SHARING_REVOCATIONS_SCHEMA,
                "fail_closed": fail_closed,
                "grants": rows,
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    fd, temp_name = mkstemp_private(dir=parent, prefix=".sharing-revocations-")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temp, _sharing_revocations_path(config))
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


@contextmanager
def _sharing_journal_claim(config: Any):
    """One interprocess read-modify-write lock for the revocation journal."""
    root = Path(config.state_path).parent / "locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(root)
    fd = open_fd(
        root / "sharing-revocations.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
        follow_symlinks=False,
    )
    try:
        if not is_private_descriptor_strict(fd):
            raise OSError("shared-brain journal lock is not private")
        lock_exclusive(fd)
        yield
    finally:
        try:
            unlock(fd)
        finally:
            os.close(fd)


def merge_sharing_revocations(
    config: Any,
    new_keys: set[tuple[str, int]],
    *,
    registry_bytes: bytes | None = None,
    force_fail_closed: bool | None = None,
) -> None:
    """Merge revocations under one interprocess journal lock.

    Distinct checkout paths can record drift concurrently without losing each
    other's keys. The locked read-modify-write is what makes the journal
    authoritative; each caller's ``registry_bytes`` only guides compaction.
    """
    with _sharing_journal_claim(config):
        state = load_sharing_revocations(config)
        keys = set(state.keys) | set(new_keys)
        if force_fail_closed is None:
            force_fail_closed = state.fail_closed or not state.healthy
        save_sharing_revocations(
            config,
            keys,
            registry_bytes=registry_bytes,
            force_fail_closed=force_fail_closed,
        )


def _sharing_fence_dir(config: Any) -> Path:
    return Path(config.state_path).parent / "sharing-pending"


def _private_atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(path.parent)
    fd, temp_name = mkstemp_private(dir=path.parent, prefix=".sharing-pending-")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temp, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _fenced_grants_for_path(config: Any, link: SharedBrainLink) -> list[dict[str, Any]]:
    """Exact current automatic grants at a path, read without origin observation."""
    canonical = os.path.realpath(link.path)
    grants: list[dict[str, Any]] = []
    for workspace_name, workspace in getattr(config, "workspaces", {}).items():
        for item in getattr(workspace, "shared_brains", None) or []:
            if (
                item.kind == "git"
                and os.path.realpath(item.path) == canonical
                and sharing_grant_stored(item)
                and (
                    item.lifetime_id != link.lifetime_id
                    or item.sharing_generation == link.sharing_generation
                )
            ):
                grants.append(
                    {
                        "workspace": workspace_name,
                        "name": item.name,
                        "path": item.path,
                        "identity": item.identity,
                        "lifetime_id": item.lifetime_id,
                        "generation": item.sharing_generation,
                    }
                )
    return grants


def write_pending_sharing_fence(
    config: Any, link: SharedBrainLink, grants: list[dict[str, Any]] | None = None
) -> tuple[Path, list[dict[str, Any]]]:
    """Persist the exact grants at risk BEFORE any authorization-relevant origin read."""
    if grants is None:
        grants = _fenced_grants_for_path(config, link)
    if not grants:
        return _sharing_fence_dir(config) / "none", []
    canonical = os.path.realpath(link.path)
    key = json.dumps(
        {"path": canonical, "grants": sorted(grants, key=lambda row: (row["workspace"], row["name"], row["lifetime_id"], row["generation"]))},
        sort_keys=True,
        separators=(",", ":"),
    )
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()
    path = _sharing_fence_dir(config) / (name + ".json")
    payload = (
        json.dumps(
            {
                "schema": 1,
                "path": canonical,
                "observed_at": _now(),
                "grants": grants,
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    _private_atomic_write(path, payload)
    return path, grants


def _live_origin_identity(link: SharedBrainLink) -> str:
    """Read the live origin identity after a fence exists, under caller claims."""
    if link.kind != "git":
        return ""
    try:
        code, listing = _git(Path(link.path), "config", "--list", "--show-scope")
    except (OSError, ValueError):
        return ""
    if code != 0 or _first_unsafe_key(listing) is not None:
        return ""
    return _safe_identity(_origin_from_listing(listing))


def fence_before_origin_read(
    config: Any, link: SharedBrainLink, owner: Any
) -> tuple[Path | None, list[dict[str, Any]]]:
    """Write a fence for persisted automatic grants before any origin read."""
    if link.kind != "git" or path_problem(config, link.path):
        return None, []
    grants = _fenced_grants_for_path(config, link)
    matching = _fence_matching_grants({"grants": grants}, owner.current)
    if not matching:
        return None, []
    try:
        return write_pending_sharing_fence(config, link, matching)
    except OSError:
        # No fence could be written. Fail closed in this process and try a
        # durable global marker before the caller treats the read as unavailable.
        changed = _demote_fenced_grants(config, matching)
        config.workspaces.update(changed)
        config._sharing_recovery_pending = True
        try:
            merge_sharing_revocations(
                config, set(), registry_bytes=owner.current, force_fail_closed=True
            )
        except OSError:
            logger.exception("No durable shared-brain fence or fail-closed marker could be written")
        raise


def settle_fence_after_healthy_probe(
    fence_path: Path | None,
    grants: list[dict[str, Any]],
    live_identity: str,
) -> None:
    """Clear a fence only when every fenced grant still matches the live origin."""
    if fence_path is None:
        return
    if live_identity and all(row.get("identity") == live_identity for row in grants):
        try:
            fence_path.unlink(missing_ok=True)
        except OSError:
            pass


def probe_under_fence(config: Any, link: SharedBrainLink, owner: Any) -> dict[str, Any]:
    """Fence, observe origin, then settle only on a healthy result."""
    fence_path, grants = fence_before_origin_read(config, link, owner)
    live_identity = _live_origin_identity(link) if fence_path is not None else ""
    state = _brain_state_owned(link, config)
    if not state.get("problem"):
        settle_fence_after_healthy_probe(fence_path, grants, live_identity)
    return state


def _load_fence(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        return None
    grants = raw.get("grants")
    if not isinstance(grants, list):
        return None
    for row in grants:
        if not isinstance(row, dict):
            return None
        if not all(
            isinstance(row.get(key), str) and row.get(key)
            for key in ("workspace", "name", "path", "identity", "lifetime_id")
        ) or type(row.get("generation")) is not int:
            return None
    return raw


def _registry_grants(registry_bytes: bytes | None) -> list[dict[str, Any]] | None:
    if registry_bytes is None:
        return None
    try:
        data = json.loads(registry_bytes.decode("utf-8", "strict"))
    except (UnicodeError, ValueError):
        return None
    rows = list(data.values()) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    found: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        workspace = row.get("name")
        links = row.get("shared_brains", [])
        if not isinstance(workspace, str) or not isinstance(links, list):
            return None
        for link in links:
            if not isinstance(link, dict):
                return None
            if (
                link.get("kind", "git") == "git"
                and link.get("sharing_policy") == "automatic"
                and isinstance(link.get("name"), str)
                and isinstance(link.get("path"), str)
                and isinstance(link.get("lifetime_id"), str)
                and type(link.get("sharing_generation")) is int
                and isinstance(link.get("sharing_consent"), dict)
                and link["sharing_consent"].get("generation") == link["sharing_generation"]
            ):
                found.append(
                    {
                        "workspace": workspace,
                        "name": link["name"],
                        "path": link["path"],
                        "identity": link.get("identity", ""),
                        "lifetime_id": link["lifetime_id"],
                        "generation": link["sharing_generation"],
                    }
                )
    return found


def _fence_matching_grants(
    fence: dict[str, Any], registry_bytes: bytes | None
) -> list[dict[str, Any]]:
    persisted = _registry_grants(registry_bytes)
    if persisted is None:
        return []
    wanted = {
        (row.get("workspace"), row.get("name"), row.get("identity"), row.get("lifetime_id"), row.get("generation"))
        for row in fence.get("grants", [])
        if isinstance(row, dict)
    }
    return [
        row
        for row in persisted
        if (row["workspace"], row["name"], row["identity"], row["lifetime_id"], row["generation"]) in wanted
    ]


def _demote_fenced_grants(config: Any, grants: list[dict[str, Any]]) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    wanted = {
        (row.get("workspace"), row.get("name"), row.get("identity"), row.get("lifetime_id"), row.get("generation"))
        for row in grants
    }
    for workspace_name, workspace in getattr(config, "workspaces", {}).items():
        links = list(getattr(workspace, "shared_brains", None) or [])
        kept: list[SharedBrainLink] = []
        touched = False
        for item in links:
            if (
                item.kind == "git"
                and sharing_grant_stored(item)
                and (workspace_name, item.name, item.identity, item.lifetime_id, item.sharing_generation) in wanted
            ):
                kept.append(set_sharing_policy(item, DEFAULT_SHARING_POLICY))
                touched = True
            else:
                kept.append(item)
        if touched:
            changed[workspace_name] = replace(workspace, shared_brains=kept)
    return changed


def _demote_all_automatic(config: Any) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for workspace_name, workspace in getattr(config, "workspaces", {}).items():
        links = list(getattr(workspace, "shared_brains", None) or [])
        kept: list[SharedBrainLink] = []
        touched = False
        for item in links:
            if item.kind == "git" and sharing_grant_stored(item):
                kept.append(set_sharing_policy(item, DEFAULT_SHARING_POLICY))
                touched = True
            else:
                kept.append(item)
        if touched:
            changed[workspace_name] = replace(workspace, shared_brains=kept)
    return changed


def recover_pending_sharing_fences(config: Any, owner: Any | None = None) -> bool:
    """Turn abandoned drift fences into persisted review before admission."""
    from ciao.workspaces import (
        WorkspaceRegistryBusy,
        acquire_workspace_registry,
        persist_workspaces,
        read_workspace_registry_bytes,
        workspace_registry_lock,
    )

    directory = _sharing_fence_dir(config)
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return False
    if not paths:
        return True
    own_owner = owner is None
    if owner is None:
        try:
            owner = acquire_workspace_registry(config)
        except WorkspaceRegistryBusy:
            return False
    try:
        for path in paths:
            fence = _load_fence(path)
            with workspace_registry_lock(config):
                if fence is None:
                    changed = _demote_all_automatic(config)
                    matching = None
                else:
                    matching = _fence_matching_grants(fence, owner.current)
                    changed = _demote_fenced_grants(config, matching)
                if not changed:
                    # No surviving authority to retire; the fence is settled.
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        return False
                    continue
                config.workspaces.update(changed)
                try:
                    persist_workspaces(config, dict(config.workspaces), owner=owner)
                except Exception:
                    return False
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    return False
        return True
    finally:
        if own_owner and owner is not None:
            owner.close()


def recover_sharing_state(config: Any, owner: Any | None = None) -> bool:
    """Durably settle fences and the journal before the server admits work."""
    from ciao.workspaces import (
        WorkspaceRegistryBusy,
        acquire_workspace_registry,
        persist_workspaces,
        read_workspace_registry_bytes,
        workspace_registry_lock,
    )

    own_owner = owner is None
    if owner is None:
        try:
            owner = acquire_workspace_registry(config)
        except WorkspaceRegistryBusy:
            return False
    try:
        if not recover_pending_sharing_fences(config, owner):
            return False
        state = load_sharing_revocations(config)
        if state.healthy and not state.fail_closed and not state.keys:
            return True
        with workspace_registry_lock(config):
            if not state.healthy or state.fail_closed:
                changed = _demote_all_automatic(config)
            else:
                fenced = [
                    {"workspace": workspace_name, "name": item.name,
                     "identity": item.identity,
                     "lifetime_id": item.lifetime_id,
                     "generation": item.sharing_generation}
                    for workspace_name, workspace in getattr(config, "workspaces", {}).items()
                    for item in getattr(workspace, "shared_brains", None) or []
                    if item.kind == "git"
                    and sharing_grant_stored(item)
                    and (item.lifetime_id, item.sharing_generation) in state.keys
                ]
                changed = _demote_fenced_grants(config, fenced)
            if changed:
                config.workspaces.update(changed)
                try:
                    persist_workspaces(config, dict(config.workspaces), owner=owner)
                except Exception:
                    return False
            # Only after every surviving automatic grant was durably demoted
            # may a missing/malformed/fail-closed journal be replaced.
            try:
                with _sharing_journal_claim(config):
                    save_sharing_revocations(
                        config,
                        set(),
                        registry_bytes=owner.current,
                        force_fail_closed=False,
                    )
            except OSError:
                return False
        return True
    finally:
        if own_owner and owner is not None:
            owner.close()


def has_automatic_grants(config: Any) -> bool:
    return any(
        item.kind == "git" and sharing_grant_stored(item)
        for workspace in getattr(config, "workspaces", {}).values()
        for item in getattr(workspace, "shared_brains", None) or []
    )


def apply_persisted_sharing_revocations(config: Any) -> None:
    """Load consent state and mark whether durable recovery is needed.

    A healthy journal with no pending keys or fences preserves grants across a
    restart. Anything else leaves the persisted registry untouched and marks
    ``_sharing_recovery_pending``; `recover_sharing_state` settles it under the
    server's authoritative registry claim before work is admitted.
    """
    directory = _sharing_fence_dir(config)
    try:
        fences = list(directory.glob("*.json"))
    except OSError:
        # An inaccessible fence directory cannot be proven empty; fail closed.
        config._sharing_recovery_pending = True
        return
    if fences:
        config._sharing_recovery_pending = True
        return
    state = load_sharing_revocations(config)
    if state.healthy and not state.fail_closed and not state.keys:
        config._sharing_recovery_pending = False
        return
    if not has_automatic_grants(config):
        # No surviving authority needs demotion; a fresh empty journal is safe.
        try:
            with _sharing_journal_claim(config):
                save_sharing_revocations(
                    config, set(), force_fail_closed=False
                )
        except OSError:
            config._sharing_recovery_pending = True
        else:
            config._sharing_recovery_pending = False
        return
    config._sharing_recovery_pending = True


def record_sharing_revocation_without_registry(
    config: Any, link: SharedBrainLink
) -> bool:
    """Fence and journal a drift revocation when the registry claim is unavailable.

    The fence is written before any origin observation and names the exact
    grants at risk. It is left in place for startup recovery because the registry
    could not be rewritten here. Raises OSError if neither the fence nor the
    journal can be written.
    """
    fence_path, grants = write_pending_sharing_fence(config, link)
    if not grants:
        return False
    from ciao.workspaces import read_workspace_registry_bytes

    registry_bytes = read_workspace_registry_bytes(config)
    matching = _fence_matching_grants(
        {"grants": grants}, registry_bytes
    )
    if not matching:
        try:
            fence_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    keys = {(row["lifetime_id"], row["generation"]) for row in matching}
    changed = _demote_fenced_grants(config, matching)
    merge_sharing_revocations(
        config,
        keys,
        registry_bytes=registry_bytes,
    )
    config.workspaces.update(changed)
    return True


def invalidate_sharing_on_drift(
    config: Any, link: SharedBrainLink, owner: Any | None = None
) -> bool:
    """Fence the exact grants at risk, then retire them under fresh claims.

    The fence is written before any authorization-relevant origin read. A later
    re-probe cannot erase the observation: only the fresh persisted registry, not
    a repaired origin, decides which exact grants are still there to retire.

    The journal is merged before the registry. If the registry write fails, the
    fence remains and startup recovery demotes the exact grants. If the fence
    itself cannot be written, the caller gets an OSError and must treat the
    result as unavailable; this function never claims durable success when no
    authoritative local record was written.
    """
    from ciao.workspaces import (
        WorkspaceRegistryBusy,
        acquire_workspace_registry,
        persist_workspaces,
        workspace_registry_lock,
    )

    own_owner = owner is None
    if owner is None:
        try:
            owner = acquire_workspace_registry(config)
        except WorkspaceRegistryBusy:
            return record_sharing_revocation_without_registry(config, link)
    try:
        fence_path, grants = write_pending_sharing_fence(config, link)
    except OSError:
        # The fence could not be written. Fail closed in this process and try
        # one durable global marker instead: a fail-closed journal revokes all
        # automatic authority on the next restart. If that also fails, no
        # durable record exists and the caller receives the OSError.
        changed = _demote_fenced_grants(config, _fenced_grants_for_path(config, link))
        config.workspaces.update(changed)
        config._sharing_recovery_pending = True
        try:
            merge_sharing_revocations(
                config, set(), registry_bytes=owner.current, force_fail_closed=True
            )
        except OSError:
            logger.exception("No durable shared-brain revocation record could be written")
        raise
    if not grants:
        if own_owner and owner is not None:
            owner.close()
        return False
    try:
        with workspace_registry_lock(config):
            matching = _fence_matching_grants({"grants": grants}, owner.current)
            if not matching:
                # Stale queued grants or a relink: nothing in the fresh registry
                # still has the authority the fence named.
                fence_path.unlink(missing_ok=True)
                return False
            keys = {(row["lifetime_id"], row["generation"]) for row in matching}
            changed = _demote_fenced_grants(config, matching)
            journal_state = load_sharing_revocations(config)
            try:
                merge_sharing_revocations(
                    config,
                    keys,
                    registry_bytes=owner.current,
                )
            except OSError:
                # The registry write below still proves the demotion and the
                # fence remains until that write succeeds.
                logger.exception("Could not write the shared-brain revocation journal")
            config.workspaces.update(changed)
            try:
                persist_workspaces(config, dict(config.workspaces), owner=owner)
            except Exception:
                # Leave the fence in place: a restart retires these exact grants.
                raise
            # Committed review. The fence is no longer needed.
            try:
                fence_path.unlink(missing_ok=True)
            except OSError:
                pass
            # Compact only after the new registry bytes are available. If the
            # journal was unhealthy or fail-closed, keep that global marker while
            # any unrelated automatic grant still survives.
            remaining = _persisted_automatic_grants(
                getattr(config, "_workspace_registry_bytes", None)
            )
            keep_fail_closed = bool(
                (journal_state.fail_closed or not journal_state.healthy)
                and remaining
            )
            try:
                merge_sharing_revocations(
                    config,
                    keys,
                    registry_bytes=getattr(config, "_workspace_registry_bytes", None),
                    force_fail_closed=keep_fail_closed,
                )
            except OSError:
                logger.exception("Could not compact the shared-brain revocation journal")
            return True
    finally:
        if own_owner and owner is not None:
            owner.close()


_SCP_RE = re.compile(r"(?:[^@/\\:\s]+@)?[^@/\\:\s]+:[^\s]+")


def canonical_identity(remote_url: str) -> str:
    """``host/owner/repo`` for a remote address; the same repository gives the same string.

    Only ``https://``, ``ssh://`` and scp-like ``[user@]host:path`` are accepted.
    Anything with ``::`` (remote helpers such as ext:: and fd::), any other
    scheme (file://, http://, git://) and a bare local path are refused.
    """
    text = remote_url.strip()
    if not text:
        raise ValueError("shared brain has no remote called origin")
    shown = redact_text(text)
    if "::" in text:
        raise ValueError(f"unsupported remote address: {shown}")
    lowered = text.lower()
    if "://" in lowered:
        scheme, _, rest = lowered.partition("://")
        if scheme not in ("https", "ssh"):
            raise ValueError(f"unsupported remote address: {shown}")
        authority, _, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1]
    else:
        if not _SCP_RE.fullmatch(lowered):
            raise ValueError(f"unsupported remote address: {shown}")
        rest = lowered.rsplit("@", 1)[-1]
        host, _, path = rest.partition(":")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not host or not path:
        raise ValueError(f"unrecognised remote address: {shown}")
    return f"{host}/{path}"


def overlaps_protected(config: Any, path: str) -> bool:
    """True when ``path`` (after following symlinks) is, holds, or sits inside the
    install, a vault or the runtime directory. Pure path arithmetic, no git call."""
    target = Path(os.path.realpath(path))
    protected = [Path(config.workspace_root), Path(config.state_path).parent]
    for name in config.workspace_names():
        try:
            protected.append(Path(config.workspace_vault_root(name)))
        except ValueError:
            continue
    for root in protected:
        root = root.resolve()
        if target == root or target.is_relative_to(root) or root.is_relative_to(target):
            return True
    return False


def validate_brain_path(config: Any, raw: str) -> str:
    """The resolved checkout path, or ValueError when it cannot be a shared brain."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("Shared brain path contains a control character")
    resolved = validate_repo_path(raw)
    if not resolved:
        return ""
    if overlaps_protected(config, resolved):
        raise ValueError(
            f"Shared brain folder must be outside the Ciaobot install and its vaults: {resolved}"
        )
    return resolved


def validate_folder_path(config: Any, raw: str) -> str:
    """Resolve an accessible ordinary folder outside every protected root."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("Shared brain path contains a control character")
    expanded = os.path.expanduser(raw.strip())
    if not expanded or not os.path.isabs(expanded):
        raise ValueError("Shared brain folder path must be absolute")
    resolved = os.path.realpath(expanded)
    if not os.path.isdir(resolved):
        raise ValueError(f"Shared brain folder does not exist: {resolved}")
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise ValueError(f"Shared brain folder is not accessible: {resolved}")
    try:
        with os.scandir(resolved):
            pass
    except OSError as exc:
        raise ValueError(f"Shared brain folder is not accessible: {resolved}") from exc
    if overlaps_protected(config, resolved):
        raise ValueError(
            f"Shared brain folder must be outside the Ciaobot install and its vaults: {resolved}"
        )
    return resolved


def _own_entries(listing: str) -> list[tuple[str, str]]:
    """(key, value) of every local and worktree setting; global, system and -c are not the folder's own."""
    entries: list[tuple[str, str]] = []
    for line in listing.splitlines():
        scope, _, entry = line.partition("\t")
        if scope in ("local", "worktree"):
            key, eq, value = entry.partition("=")
            entries.append((key, value if eq else ""))
    return entries


def _entry_allowed(key: str, value: str) -> bool:
    if key == "remote.origin.url":
        return True  # its value is checked against the identity, not here
    if key == "remote.origin.fetch":
        return value == _ORIGIN_FETCH
    return key in _SAFE_PLAIN or _BRANCH_KEY_RE.fullmatch(key) is not None


def _first_unsafe_key(listing: str) -> str | None:
    """The first of the folder's own settings outside the allowlist, or None."""
    for key, value in _own_entries(listing):
        if not _entry_allowed(key, value):
            return key
    return None


def _origin_from_listing(listing: str) -> str:
    """The folder's own ``remote.origin.url``, or "" when missing or ambiguous."""
    urls = [value for key, value in _own_entries(listing) if key == "remote.origin.url"]
    return urls[0] if len(urls) == 1 else ""


def _safe_identity(url: str) -> str:
    try:
        return canonical_identity(url)
    except ValueError:
        return ""


def _branch_remote_ok(listing: str, branch: str) -> bool:
    """A branch whose remote is set must fetch from ``origin`` and nothing else."""
    for key, value in _own_entries(listing):
        if key == f"branch.{branch}.remote":
            return value == "origin"
    return True


def _refusal_message(key: str) -> str:
    return (
        "this folder's git settings can run programs; use a fresh clone "
        f"(refused setting: {redact_text(key)})"
    )


def _require_own_git_dir(path: str) -> None:
    """A linked worktree or submodule keeps ``.git`` as a pointer file; refuse it."""
    if not (Path(path) / ".git").is_dir():
        raise ValueError("this folder is a linked worktree or submodule; use a fresh clone")


def _link_origin(path: str) -> str:
    """Vet a new link's own config and return its origin URL (``ValueError`` on refusal)."""
    _require_own_git_dir(path)
    code, listing = _git(Path(path), "config", "--list", "--show-scope")
    if code != 0:
        raise ValueError(f"could not read the git settings of {path}")
    key = _first_unsafe_key(listing)
    if key is not None:
        raise ValueError(_refusal_message(key))
    return _origin_from_listing(listing)


def _derived_name(identity: str, taken: set[str]) -> str:
    repo = identity.rsplit("/", 1)[-1]
    base = re.sub(r"[^a-z0-9-]+", "-", repo).strip("-")[:32].strip("-") or "brain"
    candidate, n = base, 2
    while candidate in taken:
        suffix = f"-{n}"
        candidate = f"{base[: 32 - len(suffix)].rstrip('-')}{suffix}"
        n += 1
    return candidate


def conflicts_with_link_kind(config: Any, workspace: str, path: str, kind: str) -> bool:
    key = os.path.realpath(path)
    return any(
        link.kind != kind and os.path.realpath(link.path) == key
        for name, record in getattr(config, "workspaces", {}).items() if name != workspace
        for link in getattr(record, "shared_brains", ())
    )


def resolve_links(
    config: Any,
    workspace: str,
    raw: list[dict[str, str]],
    existing: list[SharedBrainLink],
) -> list[SharedBrainLink]:
    """The whole new list for one workspace. Git runs only for rows that are new."""
    if len(raw) > MAX_BRAINS_PER_WORKSPACE:
        raise ValueError(f"A workspace can link at most {MAX_BRAINS_PER_WORKSPACE} shared brains")
    by_path = {os.path.realpath(link.path): link for link in existing}
    names: set[str] = set()
    identities: dict[str, str] = {}
    paths: set[str] = set()
    out: list[SharedBrainLink] = []
    for row in raw:
        requested = str(row.get("name") or "").strip()
        raw_path = str(row.get("path", ""))
        key = os.path.realpath(os.path.expanduser(raw_path.strip()))
        kind = row.get("kind")
        if kind is None:
            kind = by_path[key].kind if key in by_path else "git"
        if kind not in ("git", "folder"):
            raise ValueError("shared brain kind must be 'git' or 'folder'")
        if conflicts_with_link_kind(config, workspace, key, kind):
            raise ValueError("shared brain path is linked with another kind in a different workspace")
        if key in paths:
            raise ValueError(f"Shared brain folder is listed twice: {key}")
        paths.add(key)
        kept = by_path.get(key)
        if kept is not None:
            if kind != kept.kind:
                raise ValueError(
                    f"Shared brain '{kept.name}' is already linked as {kept.kind}; unlink and add it again to change kind"
                )
            if requested and requested != kept.name:
                raise ValueError(
                    f"Shared brain '{kept.name}' is already linked; remove and add it again to rename it"
                )
            if kept.name in names or kept.identity in identities:
                raise ValueError("retained shared brain name or identity is duplicated")
            names.add(kept.name)
            identities[kept.identity] = kept.name
            out.append(kept)
            continue
        if kind == "folder":
            path = validate_folder_path(config, raw_path)
            identity = f"folder:{path}"
        else:
            path = validate_brain_path(config, raw_path)
            origin = _link_origin(path)
            try:
                identity = canonical_identity(origin)
            except ValueError as exc:
                raise ValueError(f"{path}: {exc}") from exc
        if identity in identities:
            raise ValueError(
                f"{path} is the same repository as shared brain '{identities[identity]}'"
            )
        if requested:
            if not _NAME_RE.match(requested):
                raise ValueError("shared brain name must be 1 to 32 lowercase letters, digits or dashes")
            if requested in names:
                raise ValueError(f"shared brain name '{requested}' is already used in this workspace")
            name = requested
        else:
            name = _derived_name(identity, names)
        names.add(name)
        identities[identity] = name
        out.append(SharedBrainLink(name=name, path=path, identity=identity, kind=kind))
    return out


def fetched_at(path: str) -> str | None:
    """ISO UTC mtime of ``<path>/.git/FETCH_HEAD``, or None. One stat, no subprocess."""
    git_dir = Path(path) / ".git"
    if not git_dir.is_dir():
        return None
    try:
        mtime = (git_dir / "FETCH_HEAD").stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_problem(config: Any, path: str) -> str:
    """Why the folder cannot be used right now, or "" (no git call).

    ``path`` is the stored absolute path, not resolved, so a symlink swapped in
    after linking shows up as ``moved``.
    """
    if os.path.islink(path) or os.path.realpath(path) != path:
        return "moved"
    if overlaps_protected(config, path):
        return "outside_install"
    if not os.path.isdir(path):
        return "missing"
    if not os.path.isdir(os.path.join(path, ".git")):
        return "not_a_checkout"  # a pointer file, or no repository of its own
    return ""


def link_problem(config: Any, link: SharedBrainLink) -> str:
    """Current filesystem problem for either kind, without starting Git."""
    if link.kind == "git":
        return path_problem(config, link.path)
    path = link.path
    if os.path.realpath(path) != path or not os.path.isdir(path):
        return "missing" if not os.path.exists(path) else "moved"
    if overlaps_protected(config, path):
        return "outside_install"
    if not os.access(path, os.R_OK | os.X_OK):
        return "inaccessible"
    try:
        with os.scandir(path):
            pass
    except OSError:
        return "inaccessible"
    return ""


def _is_own_checkout(folder: Path, top: str) -> bool:
    return bool(top.strip()) and os.path.realpath(top.strip()) == os.path.realpath(folder)


def _initial_state(link: SharedBrainLink) -> dict[str, Any]:
    automatic = sharing_consent_active(link) if link.kind == "git" else False
    stored = sharing_grant_stored(link) if link.kind == "git" else False
    state: dict[str, Any] = {
        "name": link.name,
        "kind": link.kind,
        "identity": link.identity,
        "path": link.path,
        "lifetime": link.lifetime_id,
        "sharing_policy": "automatic" if automatic else DEFAULT_SHARING_POLICY,
        "sharing_generation": link.sharing_generation if link.kind == "git" else 0,
        "sharing_granted_at": (
            link.sharing_consent.granted_at
            if automatic and link.sharing_consent is not None
            else ""
        ),
        # A stored grant that is not effective (origin drift, a revoked-in-
        # memory generation, or an invalid row) still needs a revoke action.
        "sharing_grant_pending": stored and not automatic,
        "sharing_invalidation_failed": False,
        "origin_changed": False,
        "missing": False,
        "problem": "",
        "branch": "",
        "head": "",
        "head_date": "",
        "dirty": False,
        "upstream": False,
        "fetched_at": None,
        "last_update": _last_update.get(_lock_key(link.path)) if link.kind == "git" else None,
    }
    return state


def brain_state(link: SharedBrainLink, config: Any, workspace: str) -> dict[str, Any]:
    """Inspect a registered checkout only while its path and registry are owned."""
    from ciao.workspaces import WorkspaceRegistryConflict, persisted_checkout_links, workspace_registry_claim

    if link.kind == "git" and getattr(config, "_sharing_recovery_pending", False):
        # Recovery is not settled: do not probe Git without claims.
        state = _initial_state(link)
        state["sharing_policy"] = DEFAULT_SHARING_POLICY
        state["sharing_granted_at"] = ""
        state["sharing_grant_pending"] = sharing_grant_stored(link)
        state["sharing_invalidation_failed"] = True
        return state
    if link.kind == "folder":
        return _brain_state_owned(link, config)
    try:
        with checkout_claims_nowait([link.path], config), workspace_registry_claim(config) as owner:
            if owner.current is None or owner.current != config._workspace_registry_bytes:
                raise WorkspaceRegistryConflict("workspace registry changed")
            matches = persisted_checkout_links(owner.current, link.path)
            if not any(
                name == workspace and row.get("name") == link.name and row.get("identity") == link.identity
                and row.get("lifetime_id", link.lifetime_id) == link.lifetime_id
                for name, row in matches
            ):
                raise WorkspaceRegistryConflict("shared brain link changed")
            return probe_under_fence(config, link, owner)
    except (OSError, ValueError, WorkspaceRegistryConflict):
        state = _initial_state(link)
        state["problem"] = "registry_unavailable"
        if getattr(config, "_sharing_recovery_pending", False):
            state["sharing_policy"] = DEFAULT_SHARING_POLICY
            state["sharing_granted_at"] = ""
            state["sharing_grant_pending"] = sharing_grant_stored(link)
            state["sharing_invalidation_failed"] = True
        return state


def _brain_state_owned(link: SharedBrainLink, config: Any) -> dict[str, Any]:
    """Read live status inside the caller's checkout or registry transaction."""
    state = _initial_state(link)
    if link.kind == "folder":
        state["problem"] = link_problem(config, link)
        state["missing"] = state["problem"] == "missing"
        return state
    state["fetched_at"] = fetched_at(link.path)
    problem = path_problem(config, link.path)
    if problem:
        state["problem"] = problem
        state["missing"] = problem == "missing"
        return state
    folder = Path(link.path)
    code, listing = _git(folder, "config", "--list", "--show-scope")
    if code != 0 or _first_unsafe_key(listing) is not None:
        state["problem"] = "unsafe_config"
        return state
    code, top = _git(folder, "rev-parse", "--show-toplevel")
    if code != 0 or not _is_own_checkout(folder, top):
        state["problem"] = "not_a_checkout"
        return state
    _code, branch = _git(folder, "branch", "--show-current")
    state["branch"] = branch.strip()
    if not _branch_remote_ok(listing, state["branch"]):
        state["problem"] = "unsafe_config"
        return state
    state["origin_changed"] = _safe_identity(_origin_from_listing(listing)) != link.identity
    if state["origin_changed"]:
        state["problem"] = "origin_changed"
        # A retargeted folder is not the audience the consent named. Report
        # review even before the durable invalidation below has run, so no
        # reader can mistake this observation for a live automatic grant.
        state["sharing_policy"] = DEFAULT_SHARING_POLICY
        state["sharing_granted_at"] = ""
        state["sharing_grant_pending"] = sharing_grant_stored(link)
    code, head = _git(folder, "log", "-1", "--format=%h%x00%cs")
    if code == 0 and head.strip():
        sha, _, day = head.strip().partition("\x00")
        state["head"], state["head_date"] = sha, day
    code, status = _git(folder, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
    state["dirty"] = code == 0 and bool(status.strip())
    code, _ = _git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    state["upstream"] = code == 0
    return state


def _result(outcome: Outcome, reason: str = "", detail: str = "") -> dict[str, Any]:
    return {
        "outcome": outcome,
        "reason": reason,
        "detail": redact_text(detail)[:MAX_REASON_CHARS],
        "at": _now(),
    }


def _lock_key(path: str) -> str:
    """Absolute path with ``~`` expanded but symlinks NOT followed: a swapped link must show."""
    return os.path.abspath(os.path.expanduser(path))


def _checkout_lock(path: str) -> asyncio.Lock:
    """One in-process lock for every operation that changes a shared checkout."""
    key = _lock_key(path)
    return _locks.setdefault(key, asyncio.Lock())


@contextmanager
def _checkout_descriptor(path: str, config: Any):
    root = Path(config.state_path).parent / "locks" / "shared-brains"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(root)
    key = _lock_key(path)
    lock_path = root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    fd = open_fd(lock_path, os.O_CREAT | os.O_RDWR, 0o600, follow_symlinks=False)
    try:
        if not is_private_descriptor_strict(fd):
            raise OSError("shared brain lock is not private")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def checkout_claims_nowait(paths, config: Any):
    from ciao.workspaces import WorkspaceRegistryBusy
    with ExitStack() as stack:
        for path in sorted({_lock_key(path) for path in paths}):
            fd = stack.enter_context(_checkout_descriptor(path, config))
            try:
                lock_exclusive(fd, blocking=False)
            except BlockingIOError as exc:
                raise WorkspaceRegistryBusy("shared checkout is busy") from exc
        yield


@asynccontextmanager
async def _checkout_claim(path: str, config: Any):
    """Serialize checkout network and ref changes across engine processes."""
    with _checkout_descriptor(path, config) as fd:
        await await_exclusive(fd)
        yield


async def update_brain(link: SharedBrainLink, config: Any) -> dict[str, Any]:
    """Fetch and fast-forward one linked folder, one case from the matrix (see the issue)."""
    if link.kind != "git":
        return _result("skipped", "folder")
    key = _lock_key(link.path)
    lock = _checkout_lock(key)
    async with lock:
        try:
            async with _checkout_claim(key, config):
                from ciao.workspaces import (
                    WorkspaceRegistryConflict,
                    persisted_checkout_links,
                    run_while_claimed,
                )

                fence_path: Path | None = None
                fenced_grants: list[dict[str, Any]] = []
                owner = None
                try:
                    owner = await _acquire_registry_claimed(config)
                    raw = owner.current
                    if raw is not None:
                        if raw != getattr(config, "_workspace_registry_bytes", None):
                            return _result("skipped", "registry_changed")
                        links = persisted_checkout_links(raw, key)
                        if not any(
                            row.get("identity") == link.identity
                            and row.get("name") == link.name
                            and row.get("lifetime_id", link.lifetime_id) == link.lifetime_id
                            for _, row in links
                        ):
                            return _result("skipped", "registry_changed")
                    if conflicts_with_link_kind(config, "", key, "git"):
                        return _result("skipped", "folder")
                    fence_path, fenced_grants = await run_while_claimed(
                        fence_before_origin_read, config, link, owner
                    )
                except WorkspaceRegistryConflict:
                    return _result("skipped", "registry_busy")
                except (OSError, ValueError):
                    return _result("failed", "fence_unavailable")
                finally:
                    if owner is not None:
                        owner.close()
                result = await _update_unlocked(config, Path(key), link.identity)
                if result.get("reason") == "origin_changed":
                    try:
                        await run_while_claimed(
                            invalidate_sharing_on_drift, config, link
                        )
                    except (OSError, WorkspaceRegistryConflict):
                        result["sharing_invalidation_failed"] = True
                        config._sharing_recovery_pending = True
                else:
                    # Settle the fence only after proving the live origin still
                    # matches every fenced grant.
                    owner = None
                    try:
                        owner = await _acquire_registry_claimed(config)
                        live_identity = (
                            await run_while_claimed(_live_origin_identity, link)
                            if fence_path is not None
                            else ""
                        )
                        await run_while_claimed(
                            settle_fence_after_healthy_probe,
                            fence_path,
                            fenced_grants,
                            live_identity,
                        )
                    except (OSError, WorkspaceRegistryConflict):
                        pass
                    finally:
                        if owner is not None:
                            owner.close()
        except OSError:
            result = _result("failed", "lock_unavailable")
    _last_update[key] = result
    return result


async def _acquire_registry_claimed(config: Any, *, blocking: bool = True):
    """Cancellation-safe ownership acquisition; never leaves an orphan owner."""
    from ciao.workspaces import acquire_workspace_registry

    acquire_task = asyncio.create_task(
        asyncio.to_thread(acquire_workspace_registry, config, blocking=blocking)
    )
    try:
        return await asyncio.shield(acquire_task)
    except asyncio.CancelledError:
        while not acquire_task.done():
            try:
                await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not acquire_task.cancelled() and acquire_task.exception() is None:
            acquire_task.result().close()
        raise


async def _update_unlocked(config: Any, folder: Path, identity: str) -> dict[str, Any]:
    if not folder.is_dir():
        return _result("failed", "missing")
    problem = path_problem(config, str(folder))
    if problem:
        return _result("failed", problem)
    code, listing, _ = await _git_async(folder, "config", "--list", "--show-scope")
    if code != 0 or _first_unsafe_key(listing) is not None:
        return _result("failed", "unsafe_config")
    if _safe_identity(_origin_from_listing(listing)) != identity:
        return _result("failed", "origin_changed")
    code, top, _ = await _git_async(folder, "rev-parse", "--show-toplevel")
    if code != 0 or not _is_own_checkout(folder, top):
        return _result("failed", "not_a_checkout")
    code, branch, _ = await _git_async(folder, "branch", "--show-current")
    if code != 0 or not branch.strip():
        return _result("skipped", "detached")
    if not _branch_remote_ok(listing, branch.strip()):
        return _result("failed", "unsafe_config")
    code, _, _ = await _git_async(
        folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
    )
    if code != 0:
        return _result("skipped", "no_upstream")
    code, _, err = await _git_async(
        folder, "fetch", "--no-tags", "--no-recurse-submodules", timeout=FETCH_TIMEOUT_S
    )
    if code == -1 and err == GIT_TIMEOUT_DETAIL:
        return _result("failed", "timeout", err)
    if code != 0:
        return _result("failed", "fetch_failed", err)
    code, counts, err = await _git_async(
        folder, "rev-list", "--left-right", "--count", "HEAD...@{u}"
    )
    parts = counts.split()
    if code != 0 or len(parts) != 2:
        return _result("failed", "fetch_failed", err)
    ahead, behind = int(parts[0]), int(parts[1])
    if behind == 0:
        return _result("up_to_date")
    if ahead > 0:
        return _result("failed", "diverged")
    code, status, _ = await _git_async(
        folder, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"
    )
    if code != 0 or status.strip():
        return _result("skipped", "local_changes")
    code, _, err = await _git_async(folder, "merge", "--ff-only", "@{u}")
    if code != 0:
        return _result("failed", "merge_failed", err)
    return _result("updated")


def schedule_update(link: SharedBrainLink, config: Any) -> None:
    """Start one background update for ``link`` unless one is already pending."""
    if link.kind != "git" or _updates_closed:
        return
    key = _lock_key(link.path)
    if key in _pending:
        return
    task = asyncio.create_task(update_brain(link, config))
    _pending[key] = task
    task.add_done_callback(
        lambda done: _pending.pop(key, None) if _pending.get(key) is done else None
    )


async def _run_pass(config: Any, links: list[SharedBrainLink]) -> dict[str, int]:
    """Update each link once, one after another; a failure counts and moves on."""
    counts: dict[str, int] = {}
    for link in links:
        try:
            outcome = (await update_brain(link, config))["outcome"]
        except Exception:
            logger.exception("Shared brain update failed")
            outcome = "failed"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _linked_links(config: Any) -> list[SharedBrainLink]:
    """One link per distinct path; links into the install or a vault are kept but not updated."""
    seen: dict[str, SharedBrainLink] = {}
    for workspace in getattr(config, "workspaces", {}).values():
        for link in getattr(workspace, "shared_brains", []):
            if link.kind == "git" and not overlaps_protected(config, link.path):
                seen.setdefault(link.path, link)
    return list(seen.values())


def _forget_unlinked(keep: set[str]) -> None:
    """Drop lock and result entries for folders no workspace links any more.

    A lock that is held belongs to a running update and stays.
    """
    for key in [k for k in _locks if k not in keep and not _locks[k].locked()]:
        _locks.pop(key, None)
    for key in [k for k in _last_update if k not in keep]:
        _last_update.pop(key, None)


def _next_batch(links: list[SharedBrainLink]) -> list[SharedBrainLink]:
    """Up to MAX_BRAINS_PER_PASS links, continuing where the last pass stopped."""
    global _cursor
    if not links:
        return []
    _cursor %= len(links)
    count = min(MAX_BRAINS_PER_PASS, len(links))
    batch = [links[(_cursor + i) % len(links)] for i in range(count)]
    _cursor = (_cursor + count) % len(links)
    return batch


async def update_loop(config: Any) -> None:
    """Background loop: every UPDATE_INTERVAL_S, update the linked folders once each."""
    await asyncio.sleep(FIRST_UPDATE_DELAY_S)
    while True:
        try:
            async with job_runs.track(
                "shared_brain_update", "Shared brain update", category="system",
            ) as run:
                links = _linked_links(config)
                _forget_unlinked({_lock_key(link.path) for link in links})
                counts = await _run_pass(config, _next_batch(links))
                run.extra["outcomes"] = counts
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Shared brain update loop failed")
        await asyncio.sleep(UPDATE_INTERVAL_S)


def start_update_loop(config: Any) -> asyncio.Task[None]:
    """Start and retain the engine's periodic shared brain updater."""
    global _update_loop_task
    if _updates_closed:
        raise RuntimeError("shared brain updates are shut down")
    if _update_loop_task is None or _update_loop_task.done():
        _update_loop_task = asyncio.create_task(update_loop(config))
    return _update_loop_task


def begin_update_lifecycle() -> None:
    """Open update admission for a new server lifetime after its predecessor drained."""
    global _updates_closed, _update_loop_task, _shutdown_task
    if not _updates_closed:
        return
    if _shutdown_task is None or not _shutdown_task.done():
        raise RuntimeError("previous shared brain updates are still shutting down")
    _shutdown_task.result()
    if any(not task.done() for task in _pending.values()):
        raise RuntimeError("previous shared brain updates are still running")
    if _update_loop_task is not None and not _update_loop_task.done():
        raise RuntimeError("previous shared brain update loop is still running")
    _pending.clear()
    _update_loop_task = None
    _shutdown_task = None
    _updates_closed = False


async def shutdown_updates() -> None:
    """Close update admission, then cancel and await every owned update task."""
    global _updates_closed, _shutdown_task
    _updates_closed = True
    if _shutdown_task is None:
        tasks = set(_pending.values())
        if _update_loop_task is not None:
            tasks.add(_update_loop_task)

        async def cancel_and_wait() -> None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        _shutdown_task = asyncio.create_task(cancel_and_wait())
    cancelled = False
    while True:
        try:
            await asyncio.shield(_shutdown_task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if _shutdown_task.done():
                break
    _shutdown_task.result()
    if cancelled:
        raise asyncio.CancelledError
