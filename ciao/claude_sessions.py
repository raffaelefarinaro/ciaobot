"""Read Claude Code session JSONL without the Claude Agent SDK.

Ported from claude-agent-sdk 0.2.178 `_internal/sessions.py` (MIT, copyright notice and
license text in THIRD_PARTY_NOTICES.md at the repository root); Claude Code's JSONL layout
is not a public contract. Chat history for chats created before the ACP
switch still lives in that store, so the reader stays after the SDK is gone.

Callers: transcript history (``ciao.transcripts``), the session-file delete on the
Claude ACP provider, and the golden test that pins this port to the SDK output.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionMessage:
    """A user or assistant message from a session transcript."""

    type: str
    uuid: str
    session_id: str
    message: dict[str, Any] | None
    parent_tool_use_id: str | None = None
    parent_agent_id: str | None = None


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


MAX_SANITIZED_LENGTH = 200


def validate_uuid(maybe_uuid: str) -> str | None:
    """Returns the string if it is a valid UUID, else None.

    The whole string must match: a trailing newline is not part of a UUID.
    """
    if _UUID_RE.fullmatch(maybe_uuid):
        return maybe_uuid
    return None


def _simple_hash(s: str) -> str:
    """32-bit integer hash to base36, matching the CLI's directory naming."""
    h = 0
    for ch in s:
        char = ord(ch)
        h = (h << 5) - h + char
        # Emulate JS `hash |= 0` (coerce to 32-bit signed int)
        h = h & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    h = abs(h)
    # JS toString(36)
    if h == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    n = h
    while n > 0:
        out.append(digits[n % 36])
        n //= 36
    return "".join(reversed(out))


def _sanitize_path(name: str) -> str:
    """Makes a string safe for use as a directory name.

    Replaces all non-alphanumeric characters with hyphens. For paths
    exceeding MAX_SANITIZED_LENGTH, truncates and appends a hash suffix.
    """
    sanitized = _SANITIZE_RE.sub("-", name)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    h = _simple_hash(name)
    return f"{sanitized[:MAX_SANITIZED_LENGTH]}-{h}"


def _get_claude_config_home_dir() -> Path:
    """Returns the Claude config directory (respects CLAUDE_CONFIG_DIR)."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(unicodedata.normalize("NFC", config_dir))
    return Path(unicodedata.normalize("NFC", str(Path.home() / ".claude")))


def _get_projects_dir(env_override: dict[str, str] | None = None) -> Path:
    """Returns the projects directory.

    ``env_override`` is consulted before ``os.environ`` so callers that pass
    ``CLAUDE_CONFIG_DIR`` to the subprocess via ``options.env`` resolve the
    same directory the subprocess will write to.
    """
    if env_override:
        override = env_override.get("CLAUDE_CONFIG_DIR")
        if override:
            return Path(unicodedata.normalize("NFC", override)) / "projects"
    return _get_claude_config_home_dir() / "projects"


def _get_project_dir(project_path: str) -> Path:
    return _get_projects_dir() / _sanitize_path(project_path)


def _canonicalize_path(d: str) -> str:
    """Resolves a directory path to its canonical form using realpath + NFC."""
    try:
        resolved = os.path.realpath(d)
        return unicodedata.normalize("NFC", resolved)
    except OSError:
        return unicodedata.normalize("NFC", d)


def _find_project_dir(project_path: str) -> Path | None:
    """Finds the project directory for a given path.

    Tolerates hash mismatches for long paths (>200 chars). The CLI uses
    Bun.hash while the SDK under Node.js uses simpleHash — for paths that
    exceed MAX_SANITIZED_LENGTH, these produce different directory suffixes.
    This function falls back to prefix-based scanning when the exact match
    doesn't exist.
    """
    exact = _get_project_dir(project_path)
    if exact.is_dir():
        return exact

    # Exact match failed — for short paths this means no sessions exist.
    # For long paths, try prefix matching to handle hash mismatches.
    sanitized = _sanitize_path(project_path)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return None

    prefix = sanitized[:MAX_SANITIZED_LENGTH]
    projects_dir = _get_projects_dir()
    try:
        for entry in projects_dir.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix + "-"):
                return entry
    except OSError:
        pass
    return None


def _get_worktree_paths(cwd: str) -> list[str]:
    """Returns absolute worktree paths for the git repo containing cwd.

    Returns empty list if git is unavailable or cwd is not in a repo.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if result.returncode != 0 or not result.stdout:
        return []

    paths = []
    for line in result.stdout.split("\n"):
        if line.startswith("worktree "):
            path = unicodedata.normalize("NFC", line[len("worktree ") :])
            paths.append(path)
    return paths


_TRANSCRIPT_ENTRY_TYPES = frozenset(
    {"user", "assistant", "progress", "system", "attachment"}
)


_TranscriptEntry = dict[str, Any]


# Largest transcript read into memory. A session above it has no history here: callers
# get the same None as for a missing file, so their existing "not in this root" path runs.
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024


def _try_read_session_file(project_dir: Path, file_name: str) -> str | None:
    """Tries to read a session JSONL file from a project directory.

    Returns None when the file is missing, unreadable or larger than MAX_TRANSCRIPT_BYTES.
    """
    path = project_dir / file_name
    try:
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            # The name and the path are left out: the session id is in the file name.
            logger.warning(
                "A Claude session transcript is larger than %d bytes; its history is unavailable.",
                MAX_TRANSCRIPT_BYTES,
            )
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_session_file(session_id: str, directory: str | None) -> str | None:
    """Finds and reads the session JSONL file.

    If directory is provided, looks in that project directory and its git
    worktrees (with prefix-fallback for Bun/Node hash mismatches on long
    paths). Otherwise, searches all project directories.

    Returns the file content, or None if not found or larger than MAX_TRANSCRIPT_BYTES.
    """
    file_name = f"{session_id}.jsonl"

    if directory:
        canonical_dir = _canonicalize_path(directory)

        # Try the exact/prefix-matched project directory first
        project_dir = _find_project_dir(canonical_dir)
        if project_dir is not None:
            content = _try_read_session_file(project_dir, file_name)
            if content:
                return content

        # Try worktree paths — sessions may live under a different worktree root
        try:
            worktree_paths = _get_worktree_paths(canonical_dir)
        except Exception:
            worktree_paths = []

        for wt in worktree_paths:
            if wt == canonical_dir:
                continue  # already tried above
            wt_project_dir = _find_project_dir(wt)
            if wt_project_dir is not None:
                content = _try_read_session_file(wt_project_dir, file_name)
                if content:
                    return content

        return None

    # No directory provided — search all project directories
    projects_dir = _get_projects_dir()
    try:
        dirents = list(projects_dir.iterdir())
    except OSError:
        return None

    for entry in dirents:
        content = _try_read_session_file(entry, file_name)
        if content:
            return content

    return None


def parse_transcript_entries(content: str) -> list[_TranscriptEntry]:
    """Parses JSONL content into transcript entries.

    Only keeps entries that have a uuid and are transcript message types
    (user/assistant/progress/system/attachment). Skips corrupt lines.
    """
    entries: list[_TranscriptEntry] = []
    start = 0
    length = len(content)

    while start < length:
        end = content.find("\n", start)
        if end == -1:
            end = length

        line = content[start:end].strip()
        start = end + 1
        if not line:
            continue

        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError, RecursionError):
            # RecursionError: a line of deeply nested brackets. It is corrupt, like any other.
            continue

        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type in _TRANSCRIPT_ENTRY_TYPES and isinstance(entry.get("uuid"), str):
            entries.append(entry)

    return entries


def _build_conversation_chain(
    entries: list[_TranscriptEntry],
) -> list[_TranscriptEntry]:
    """Builds the conversation chain by finding the leaf and walking parentUuid.

    Returns messages in chronological order (root -> leaf). Tool calls and
    tool results that the walk skipped because the turn ran its tools in
    parallel are put back by ``_recover_orphaned_parallel_tool_results``.

    Note: logicalParentUuid (set on compact_boundary entries) is intentionally
    NOT followed. This matches VS Code IDE behavior — post-compaction, the
    isCompactSummary message replaces earlier messages, so following logical
    parents would duplicate content.
    """
    if not entries:
        return []

    # Index by uuid for O(1) parent lookup
    by_uuid: dict[str, _TranscriptEntry] = {}
    for entry in entries:
        by_uuid[entry["uuid"]] = entry

    # Build index of entry positions (file order) for tie-breaking
    entry_index: dict[str, int] = {}
    for i, entry in enumerate(entries):
        entry_index[entry["uuid"]] = i

    # Find terminal messages (no children point to them via parentUuid)
    parent_uuids: set[str] = set()
    for entry in entries:
        parent = entry.get("parentUuid")
        if parent:
            parent_uuids.add(parent)

    terminals = [e for e in entries if e["uuid"] not in parent_uuids]

    # From each terminal, walk back to find the nearest user/assistant leaf
    leaves: list[_TranscriptEntry] = []
    for terminal in terminals:
        walk_cur: _TranscriptEntry | None = terminal
        walk_seen: set[str] = set()
        while walk_cur is not None:
            uid = walk_cur["uuid"]
            if uid in walk_seen:
                break
            walk_seen.add(uid)
            if walk_cur.get("type") in ("user", "assistant"):
                leaves.append(walk_cur)
                break
            parent = walk_cur.get("parentUuid")
            walk_cur = by_uuid.get(parent) if parent else None

    if not leaves:
        return []

    # Pick the leaf from the main chain (not sidechain/team/meta), preferring
    # the highest position in the entries array (most recent in file)
    main_leaves = [
        leaf
        for leaf in leaves
        if not leaf.get("isSidechain")
        and not leaf.get("teamName")
        and not leaf.get("isMeta")
    ]

    def _pick_best(candidates: list[_TranscriptEntry]) -> _TranscriptEntry:
        best = candidates[0]
        best_idx = entry_index.get(best["uuid"], -1)
        for cur in candidates[1:]:
            cur_idx = entry_index.get(cur["uuid"], -1)
            if cur_idx > best_idx:
                best = cur
                best_idx = cur_idx
        return best

    leaf = _pick_best(main_leaves) if main_leaves else _pick_best(leaves)

    # Walk from leaf to root via parentUuid
    chain: list[_TranscriptEntry] = []
    chain_seen: set[str] = set()
    chain_cur: _TranscriptEntry | None = leaf
    while chain_cur is not None:
        uid = chain_cur["uuid"]
        if uid in chain_seen:
            break
        chain_seen.add(uid)
        chain.append(chain_cur)
        parent = chain_cur.get("parentUuid")
        chain_cur = by_uuid.get(parent) if parent else None

    chain.reverse()
    return _recover_orphaned_parallel_tool_results(by_uuid, chain, chain_seen)


def _assistant_message_id(entry: _TranscriptEntry) -> str | None:
    """Returns the API ``message.id`` of an assistant entry, if it has one."""
    if entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    return message_id if isinstance(message_id, str) and message_id else None


def _content_block_ids(
    entry: _TranscriptEntry, block_type: str, id_field: str
) -> list[str]:
    """Returns ``id_field`` of each ``block_type`` content block in an entry."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    ids: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == block_type:
            block_id = block.get(id_field)
            if isinstance(block_id, str):
                ids.append(block_id)
    return ids


def _is_tool_result_entry(entry: _TranscriptEntry) -> bool:
    """Returns True for a user entry with a parent that carries a tool_result."""
    if entry.get("type") != "user":
        return False
    parent = entry.get("parentUuid")
    if not isinstance(parent, str) or not parent:
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _same_writer(a: _TranscriptEntry, b: _TranscriptEntry) -> bool:
    """Returns True if two entries were written by the same (sub)agent."""
    return bool(a.get("isSidechain")) == bool(b.get("isSidechain")) and a.get(
        "agentId"
    ) == b.get("agentId")


def _timestamp_sort_key(entry: _TranscriptEntry) -> str:
    timestamp = entry.get("timestamp")
    return timestamp if isinstance(timestamp, str) else ""


def _recover_orphaned_parallel_tool_results(
    by_uuid: dict[str, _TranscriptEntry],
    chain: list[_TranscriptEntry],
    seen: set[str],
) -> list[_TranscriptEntry]:
    """Puts back the tool calls and results a parallel turn left off the chain.

    The CLI writes one assistant entry per content block, so a response with
    N parallel tool calls is N assistant entries that share ``message.id``
    and are chained one after another. Each tool_result entry's
    ``parentUuid`` is its own call's assistant entry, so the results fan out
    from different parents and the transcript forks. The ``parentUuid`` walk
    follows only the branch the next turn continued from: it keeps one
    result, and drops every call written after the one that result answers.

    For each response on the chain, this collects its assistant entries and
    their tool results that the walk did not reach, and inserts them right
    after the response's last entry on the chain, so the response stays in
    one piece and every result follows its call. Mirrors the TypeScript
    SDK's ``getSessionMessages``.

    ``seen`` is the set of uuids on ``chain``; recovered uuids are added to it.
    """
    chain_assistants = [e for e in chain if e.get("type") == "assistant"]
    if not chain_assistants:
        return chain

    # Anchor = the last on-chain entry of each response. chain_assistants is
    # in chain order, so later entries overwrite earlier ones.
    anchor_by_message_id: dict[str, _TranscriptEntry] = {}
    for entry in chain_assistants:
        message_id = _assistant_message_id(entry)
        if message_id:
            anchor_by_message_id[message_id] = entry
    if not anchor_by_message_id:
        return chain

    # Group every assistant entry by response, and record which entry made
    # each tool call. A tool_use id claimed by two different responses is
    # ambiguous (None) and is never used to place a result.
    siblings_by_message_id: dict[str, list[_TranscriptEntry]] = {}
    call_owner: dict[str, _TranscriptEntry | None] = {}
    tool_result_entries: list[_TranscriptEntry] = []
    for entry in by_uuid.values():
        message_id = _assistant_message_id(entry)
        if message_id:
            siblings_by_message_id.setdefault(message_id, []).append(entry)
            for call_id in _content_block_ids(entry, "tool_use", "id"):
                if call_id not in call_owner:
                    call_owner[call_id] = entry
                    continue
                owner = call_owner[call_id]
                same_response = (
                    owner is not None and _assistant_message_id(owner) == message_id
                )
                call_owner[call_id] = entry if same_response else None
        elif _is_tool_result_entry(entry):
            tool_result_entries.append(entry)

    # Index each tool result under the assistant entries it may belong to:
    # its parent, its sourceToolAssistantUUID, and the owner of each call it
    # answers. The last two only count when written by the same agent.
    results_by_assistant: dict[str, list[_TranscriptEntry]] = {}
    for result in tool_result_entries:
        results_by_assistant.setdefault(result["parentUuid"], []).append(result)
        source = result.get("sourceToolAssistantUUID")
        if isinstance(source, str):
            source_entry = by_uuid.get(source)
            if source_entry is not None and _same_writer(result, source_entry):
                results_by_assistant.setdefault(source, []).append(result)
        for call_id in _content_block_ids(result, "tool_result", "tool_use_id"):
            owner = call_owner.get(call_id)
            if owner is not None and _same_writer(result, owner):
                results_by_assistant.setdefault(owner["uuid"], []).append(result)

    # Only needed to place strays, which are rare: built on first use.
    answered_on_chain: set[str] | None = None
    file_position: dict[str, int] | None = None

    processed: set[str] = set()
    inserts: dict[str, list[_TranscriptEntry]] = {}
    for assistant in chain_assistants:
        message_id = _assistant_message_id(assistant)
        if not message_id or message_id in processed:
            continue
        processed.add(message_id)

        group = siblings_by_message_id.get(message_id, [assistant])
        member_uuids = {member["uuid"] for member in group}
        orphaned_siblings = [m for m in group if m["uuid"] not in seen]
        orphaned_results: list[_TranscriptEntry] = []
        # Results indexed under this response whose parent is not one of its
        # entries: recovered only if they answer a still-unanswered call.
        strays: list[_TranscriptEntry] = []
        collected: set[str] = set()
        for member in group:
            for result in results_by_assistant.get(member["uuid"], ()):
                if result["uuid"] in seen or result["uuid"] in collected:
                    continue
                collected.add(result["uuid"])
                if result["parentUuid"] in member_uuids:
                    orphaned_results.append(result)
                else:
                    strays.append(result)

        if strays:
            if answered_on_chain is None or file_position is None:
                answered_on_chain = {
                    call_id
                    for entry in chain
                    for call_id in _content_block_ids(
                        entry, "tool_result", "tool_use_id"
                    )
                }
                file_position = {uid: i for i, uid in enumerate(by_uuid)}
            answered = set(answered_on_chain)
            for result in orphaned_results:
                answered.update(
                    _content_block_ids(result, "tool_result", "tool_use_id")
                )
            positions = file_position
            strays.sort(key=lambda e: positions[e["uuid"]])
            for result in strays:
                call_ids = _content_block_ids(result, "tool_result", "tool_use_id")
                answers_open_call = False
                for call_id in call_ids:
                    owner = call_owner.get(call_id)
                    if (
                        call_id not in answered
                        and owner is not None
                        and owner["uuid"] in member_uuids
                    ):
                        answers_open_call = True
                        break
                if not answers_open_call:
                    continue
                answered.update(call_ids)
                orphaned_results.append(result)

        if not orphaned_siblings and not orphaned_results:
            continue

        # Timestamp order keeps content-block and completion order. The sort
        # is stable, so on a tie siblings keep file order and results keep
        # the order of the calls they answer.
        orphaned_siblings.sort(key=_timestamp_sort_key)
        orphaned_results.sort(key=_timestamp_sort_key)
        recovered = orphaned_siblings + orphaned_results
        for entry in recovered:
            seen.add(entry["uuid"])
        inserts[anchor_by_message_id[message_id]["uuid"]] = recovered

    if not inserts:
        return chain

    result_chain: list[_TranscriptEntry] = []
    for entry in chain:
        result_chain.append(entry)
        result_chain.extend(inserts.get(entry["uuid"], ()))
    return result_chain


def is_visible_message(entry: _TranscriptEntry) -> bool:
    """Returns True if the entry should be included in the returned messages."""
    entry_type = entry.get("type")
    if entry_type != "user" and entry_type != "assistant":
        return False
    if entry.get("isMeta"):
        return False
    if entry.get("isSidechain"):
        return False
    # Note: isCompactSummary messages are intentionally included. They contain
    # the summarized content from compacted conversations and are the only
    # representation of that content post-compaction. This matches VS Code IDE
    # behavior (transcriptToSessionMessage does not filter them).
    return not entry.get("teamName")


def to_session_message(
    entry: _TranscriptEntry,
    parent_tool_use_id: str | None = None,
    parent_agent_id: str | None = None,
) -> SessionMessage:
    """Converts a transcript entry dict into a SessionMessage."""
    entry_type = entry.get("type")
    # Narrow to the Literal type — is_visible_message already guarantees
    # this is "user" or "assistant".
    msg_type: str = "user" if entry_type == "user" else "assistant"
    return SessionMessage(
        type=msg_type,  # type: ignore[arg-type]
        uuid=entry.get("uuid", ""),
        session_id=entry.get("sessionId", ""),
        message=entry.get("message"),
        parent_tool_use_id=parent_tool_use_id,
        parent_agent_id=parent_agent_id,
    )


def _resolve_session_file_path(session_id: str, directory: str | None) -> Path | None:
    """Resolves the on-disk path of a session JSONL file.

    Directory resolution mirrors ``_read_session_file``: when ``directory``
    is provided, looks in that project directory and its git worktrees;
    otherwise searches all project directories. Returns the path of the
    first non-empty match, or ``None`` if not found.
    """
    file_name = f"{session_id}.jsonl"

    def _stat_candidate(project_dir: Path) -> Path | None:
        candidate = project_dir / file_name
        try:
            if candidate.stat().st_size > 0:
                return candidate
        except OSError:
            pass
        return None

    if directory:
        canonical_dir = _canonicalize_path(directory)

        project_dir = _find_project_dir(canonical_dir)
        if project_dir is not None:
            found = _stat_candidate(project_dir)
            if found is not None:
                return found

        try:
            worktree_paths = _get_worktree_paths(canonical_dir)
        except Exception:
            worktree_paths = []

        for wt in worktree_paths:
            if wt == canonical_dir:
                continue
            wt_project_dir = _find_project_dir(wt)
            if wt_project_dir is not None:
                found = _stat_candidate(wt_project_dir)
                if found is not None:
                    return found

        return None

    projects_dir = _get_projects_dir()
    try:
        dirents = list(projects_dir.iterdir())
    except OSError:
        return None

    for entry in dirents:
        if not entry.is_dir():
            continue
        found = _stat_candidate(entry)
        if found is not None:
            return found

    return None


def project_key_for_directory(directory: str | Path | None = None) -> str:
    """The project directory name Claude Code uses for ``directory`` (realpath, NFC, sanitized)."""
    return _sanitize_path(_canonicalize_path(str(directory) if directory is not None else "."))


def get_session_messages(session_id: str, *, directory: str | None = None) -> list[SessionMessage]:
    """The session's visible user and assistant messages, in chronological order.

    Builds the conversation chain from ``parentUuid`` links, so a turn that ran tools in
    parallel comes back whole. Returns ``[]`` for an invalid id or a missing session.
    Callers page by slicing the result.
    """
    if not validate_uuid(session_id):
        return []
    content = read_session_file(session_id, directory)
    if not content:
        return []
    chain = _build_conversation_chain(parse_transcript_entries(content))
    return [to_session_message(entry) for entry in chain if is_visible_message(entry)]


def delete_session_file(session_id: str, directory: str | None) -> bool:
    """Remove the session's JSONL file and its subagent directory. True when a file was removed.

    Raises ``OSError`` for an I/O failure other than the file being absent. The subagent
    directory is removed best-effort, as the SDK does.
    """
    if not validate_uuid(session_id):
        return False
    path = _resolve_session_file_path(session_id, directory)
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    subagents = path.parent / session_id
    if subagents.is_symlink():
        # Only the link goes. rmtree would refuse a link, so it is removed explicitly.
        subagents.unlink(missing_ok=True)
    else:
        shutil.rmtree(subagents, ignore_errors=True)
    return True


