"""The Claude Code adapter: one session's JSONL transcript as a NormalizedSession.

Claude Code writes a conversation as an append-only JSONL file, one JSON object
per line, at ``~/.claude/projects/<slug>/<sid>.jsonl`` — the location
:func:`ciao.agent_paths.claude_projects_dir` names. Those files hold far more
than the conversation: subagent turns, team agents' turns, the transcript's own
bookkeeping, progress and attachment records, and the raw payloads of every tool
call. They also hold **branches**: one session file can have several leaves,
because editing a message forks the conversation, and a compaction replaces a
stretch of it with a summary linked through ``logicalParentUuid``.

:func:`read_claude_code_session` therefore reconstructs **one main chain** the
way ``ciao.transcripts.get_session_messages_full`` does (find the entries no
other entry names as a parent, walk up through ``parentUuid`` **or**
``logicalParentUuid`` until a ``user``/``assistant`` entry, prefer a leaf that is
not ``isSidechain``/``teamName``/``isMeta``, and among those take the
latest-indexed), and maps that chain to the contract:

* roles map from the entry's own ``type`` (``user`` / ``assistant``), which is
  what Ciaobot's own reader branches on;
* ``anchor`` is the entry's ``uuid``;
* **no timestamp is read.** The source has none this reader may use and the file
  system's mtime is when the engine looked, so ``timestamp`` stays ``None``;
* every entry that produced no message is counted, by reason, into
  :attr:`~ciao.import_sources.contract.NormalizedSession.omissions` — including
  the branch that branch selection did not take, which is a real turn of the
  conversation and is exactly the kind of thing a user must be told about.

This is an **import** reader, and the differences from the transcript renderer
are deliberate. It does not import the SDK's private session helpers, so it
cannot follow them into a shape change and cannot fail the way
``get_session_messages_full`` falls back when they are missing; it does not lift
the display concerns (compact-summary flags, tool-call ids, session ids);
``get_session_messages``' rendering flags are not import facts. It never writes
to the file and never writes to its directory. It refuses a symlink and any
non-regular file rather than following one, and it reads at most
:data:`~ciao.import_sources.contract.MAX_SESSION_BYTES`, stopping at the last
whole line so the result is whole entries or nothing.

Reads no operator history beyond the one file it is handed, starts no model and
no engine, and opens no socket.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ciao.import_sources.contract import (
    MAX_SESSION_BYTES,
    OMISSION_ENTRY_TYPE,
    OMISSION_META,
    OMISSION_NON_TEXT_CONTENT,
    OMISSION_OFF_CHAIN,
    OMISSION_SIDECHAIN,
    OMISSION_TEAM,
    OMISSION_TRUNCATED,
    OMISSION_UNREADABLE_LINE,
    PROVIDER_CLAUDE_CODE,
    ROLE_ASSISTANT,
    ROLE_USER,
    NormalizedMessage,
    NormalizedSession,
    Omission,
    SourceRef,
)
from ciao.os_support.files import open_fd

logger = logging.getLogger(__name__)

#: What Claude Code names its session files.
SESSION_SUFFIX = ".jsonl"

#: How much of the cap to pull in per read. One syscall per chunk keeps the
#: bounded read a bounded read instead of a single huge allocation.
_READ_CHUNK = 256 * 1024

#: ``O_NONBLOCK`` where the platform has it, so a FIFO cannot hang the open.
#: Windows has no such open flag — ``os_support.files.create_fd`` ignores a flag
#: it does not map — and no FIFO either, so the absence costs nothing there.
_NO_BLOCK = getattr(os, "O_NONBLOCK", 0)

#: The two types that are a turn of the conversation. A leaf is the nearest
#: ancestor-or-self of this kind: Claude Code appends non-message records
#: (progress, a compact boundary) to the same chain, and the leaf that names the
#: conversation is the last message on it.
_MESSAGE_TYPES = frozenset({"user", "assistant"})

_ROLE_BY_ENTRY_TYPE = {"user": ROLE_USER, "assistant": ROLE_ASSISTANT}


class SourceRefusal(OSError):
    """The path is not a plain file, so it is not read.

    A symlink at the session path, a directory, a device, a socket: anything
    where "open it and read what is inside" would mean reading something the
    provider's own naming did not put there. Raised as an ``OSError`` subclass
    because that is what it is, and with one type on every OS — Windows refuses
    a link at the descriptor with ``ELOOP`` and a directory with ``EISDIR``,
    while POSIX opens a directory happily and is caught by the ``fstat`` — so a
    caller has one thing to catch.
    """


def discover_claude_code_sessions(project_dir: Path) -> list[SourceRef]:
    """Every session file in one Claude Code project directory, as metadata.

    ``project_dir`` is ``~/.claude/projects/<slug>``, i.e. what
    :func:`ciao.agent_paths.claude_projects_dir` returns for a workspace. The
    listing is metadata only — a name and a path — so discovery cannot read a
    conversation before the user has chosen one, and cannot be made to read one
    at all.

    ``project_hint`` is the slug, which is the weakest locator Claude Code keeps
    and is **not** reversible into a workspace path
    (:func:`ciao.agent_paths.claude_project_slug` folds every non-alphanumeric to
    ``-``). The result is sorted by session id so a scan is reproducible.

    A directory with no sessions in it, or no directory at all, is an empty
    list: a workspace nobody ran Claude Code in is the ordinary case, not an
    error. A directory that cannot be listed is logged and also returns nothing,
    because a scan that cannot see a directory has no business claiming to have
    scanned it.
    """
    directory = Path(project_dir)
    try:
        candidates = sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        logger.warning("cannot list Claude Code sessions in %s: %s", directory, exc)
        return []

    refs: list[SourceRef] = []
    for path in candidates:
        if path.suffix != SESSION_SUFFIX:
            continue
        # A link is not a session. Refused rather than followed, for the same
        # reason the reader refuses one: whatever it points at is not a file
        # Claude Code wrote under this slug.
        if path.is_symlink() or not path.is_file():
            continue
        refs.append(
            SourceRef(
                provider=PROVIDER_CLAUDE_CODE,
                source_id=path.stem,
                project_hint=directory.name,
                path=str(path),
            )
        )
    return refs


def read_claude_code_session(
    path: Path,
    *,
    provider: str = PROVIDER_CLAUDE_CODE,
) -> NormalizedSession:
    """One Claude Code session file as a :class:`NormalizedSession`.

    *path* is ``<sid>.jsonl`` under a Claude Code project directory. The
    returned session names itself from the file: ``source_id`` is the file's
    stem (Claude Code's session id is the file name) and ``project_hint`` is the
    slug directory it sits in — lossy by construction, and stored as a hint for
    that reason. ``provider`` defaults to the adapter's own name and exists so
    a caller reading a file through a layer that has its own vocabulary can
    label it; it must still be a provider id the contract knows.

    The file is read, never written: no truncation, no rewrite, no rename, and
    nothing beside it is touched.

    A file that holds no readable conversation comes back as a session with no
    messages and the omissions that say why — a corrupt file is a result the
    caller can show, not an exception it has to catch to find out. What *is*
    refused is the file itself: :class:`SourceRefusal` for a symlink or any
    non-regular file, and ``FileNotFoundError`` for one that is not there.

    Over :data:`~ciao.import_sources.contract.MAX_SESSION_BYTES` the read stops
    at the file's last whole line, :attr:`NormalizedSession.truncated` is set,
    and a ``truncated`` omission is recorded. The lines dropped are not counted
    as omissions of their own kinds, because they were never read and so cannot
    be described: that is what the flag is for.
    """
    target = Path(path)
    raw, truncated = _read_bounded(target)
    entries, unreadable = _parse_entries(raw)
    chain = _main_chain(entries)
    on_chain = {entry["uuid"] for entry in chain}

    messages: list[NormalizedMessage] = []
    non_text_blocks = 0
    for entry in chain:
        # Chain order, not file order: the chain is what makes the conversation
        # chronological, and a compaction rewrites the file order around itself.
        role = _ROLE_BY_ENTRY_TYPE.get(str(entry.get("type")))
        if role is None or _omission_kind(entry, on_chain=True) is not None:
            continue
        text, dropped = _message_text(entry.get("message"))
        non_text_blocks += dropped
        messages.append(
            NormalizedMessage(
                role=role,
                text=text,
                # str() is narrowing `uuid`, which parsing already proved is a
                # string: it is the one field an entry cannot do without.
                anchor=str(entry["uuid"]),
                # Never a fabricated date. See the module docstring.
                timestamp=None,
            )
        )

    counts: dict[str, int] = {}

    def _count_omission(kind: str, count: int = 1) -> None:
        if count:
            counts[kind] = counts.get(kind, 0) + count

    if unreadable:
        _count_omission(OMISSION_UNREADABLE_LINE, unreadable)
    if truncated:
        _count_omission(OMISSION_TRUNCATED)
    if non_text_blocks:
        _count_omission(OMISSION_NON_TEXT_CONTENT, non_text_blocks)
    # Every entry, including the branches not followed: a reader that only
    # counted what happened on the chain it took would make the fork it dropped
    # look like there had been no fork.
    for entry in entries:
        kind = _omission_kind(entry, on_chain=entry["uuid"] in on_chain)
        if kind is not None:
            _count_omission(kind)

    return NormalizedSession(
        source=SourceRef(
            provider=provider,
            source_id=target.stem,
            project_hint=target.parent.name,
            path=str(target),
        ),
        messages=tuple(messages),
        omissions=tuple(
            Omission(kind=kind, count=counts[kind]) for kind in sorted(counts)
        ),
        truncated=truncated,
    )


def _read_bounded(path: Path) -> tuple[str, bool]:
    """The file's text up to :data:`MAX_SESSION_BYTES`, and whether it was cut.

    Opened by descriptor with ``follow_symlinks=False``, so a link at the final
    component is refused in the same step as the open — the ``os_support`` idiom
    for POSIX and Windows alike — and the size is taken from the descriptor
    rather than from a path a link could still redirect between the check and the
    read.

    ``O_NONBLOCK`` is there for the one file type that would otherwise hang this
    open: a FIFO opens read-only on POSIX without a writer, so the refusal below
    would never be reached. The flag is ignored for a regular file.
    """
    try:
        fd = open_fd(path, os.O_RDONLY | _NO_BLOCK, follow_symlinks=False)
    except OSError as exc:
        # A missing file is not a refusal and keeps its own type: a caller
        # scanning a directory it just listed should see the race, not a
        # "this is not a file" claim about a file that was there.
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            raise
        raise SourceRefusal(f"{path} is not a plain file, so it is not read: {exc}") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SourceRefusal(f"{path} is not a regular file, so it is not read.")
        truncated = info.st_size > MAX_SESSION_BYTES
        chunks: list[bytes] = []
        remaining = MAX_SESSION_BYTES
        while remaining > 0:
            chunk = os.read(fd, min(_READ_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)

    data = b"".join(chunks)
    if truncated:
        # Back to the last newline: an entry is one line, so a partial trailing
        # line is a half-message, and half a message is worse than a shorter
        # conversation the omissions already declare.
        cut = data.rfind(b"\n")
        data = data[: cut + 1] if cut >= 0 else b""
    # Never raises on a foreign byte: a replacement character costs one
    # character, while refusing the file would cost the whole conversation.
    return data.decode("utf-8", errors="replace"), truncated


def _parse_entries(raw: str) -> tuple[list[dict[str, Any]], int]:
    """The entries a chain can be walked over, and how many lines were not.

    An entry is any JSON object carrying a string ``uuid`` — which is every
    record Claude Code writes into the file, including the non-message types the
    walk treats as chain links. Entries of a type this reader does not know are
    kept rather than dropped at parse, so the omission count can account for
    them instead of losing them.

    A line that is not one of those — unparseable, not an object, no uuid — is
    counted and skipped. It is counted because the contract says omissions are
    never silent, and a corrupt line that vanished would be the one omission
    nobody could notice.
    """
    entries: list[dict[str, Any]] = []
    unreadable = 0
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            unreadable += 1
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("uuid"), str):
            unreadable += 1
            continue
        entries.append(entry)
    return entries, unreadable


def _main_chain(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The main chain of a session file, oldest entry first.

    The walk :func:`ciao.transcripts.get_session_messages_full` performs, for
    the same reason: a session file's entries form a tree, and one branch of it
    is the conversation. Entries no other entry names as a parent are the tips;
    each tip is walked up through ``parentUuid`` or ``logicalParentUuid`` until it
    reaches a ``user``/``assistant`` entry, which is that branch's leaf; a leaf
    that is ``isSidechain``, ``teamName`` or ``isMeta`` is not the user's
    conversation and loses to one that is; among the rest the latest-indexed wins
    (Claude Code appends, so the last branch is the one the user last took). The
    winner is then walked back down to the root and reversed into conversation
    order.

    Returns an empty list for a file with no walkable message chain, which the
    caller reports as a session with no messages rather than as a failure.
    """
    if not entries:
        return []
    by_uuid = {str(entry["uuid"]): entry for entry in entries}
    index = {str(entry["uuid"]): position for position, entry in enumerate(entries)}

    parented: set[str] = set()
    for entry in entries:
        parent = entry.get("parentUuid")
        if isinstance(parent, str) and parent:
            parented.add(parent)

    leaves: list[dict[str, Any]] = []
    for entry in entries:
        if str(entry["uuid"]) in parented:
            continue
        leaf = _first_message_up(entry, by_uuid)
        if leaf is not None:
            leaves.append(leaf)

    if not leaves:
        return []
    main = [leaf for leaf in leaves if _is_conversational(leaf)]
    # The latest-indexed candidate. `max` on the index rather than a loop, and
    # `max` keeps the first of equals, which is the earlier entry on a tie.
    leaf = max(main or leaves, key=lambda item: index[str(item["uuid"])])

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = leaf
    while current is not None:
        uid = current.get("uuid")
        if not isinstance(uid, str) or uid in seen:
            break
        seen.add(uid)
        chain.append(current)
        current = _parent_of(current, by_uuid)
    chain.reverse()
    return chain


def _parent_of(
    entry: dict[str, Any], by_uuid: Mapping[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """The entry this one continues, by ``parentUuid`` then ``logicalParentUuid``.

    ``logicalParentUuid`` is the post-compaction link: after an autocompact the
    summary entry has no ``parentUuid`` of its own and names the entry it
    replaced instead, so a walk that read only ``parentUuid`` would stop at the
    compaction and lose everything before it.
    """
    parent = entry.get("parentUuid") or entry.get("logicalParentUuid")
    if isinstance(parent, str) and parent:
        return by_uuid.get(parent)
    return None


def _first_message_up(
    entry: dict[str, Any],
    by_uuid: Mapping[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """The nearest message entry at or above *entry*, or ``None``.

    A tip that is not a message itself (a trailing ``progress`` record, a
    compact boundary) has no leaf of its own; the message it hangs off is the
    leaf of that branch. ``None`` means the branch holds no message at all,
    which is not an error — a session can end on a record and still be a real
    conversation.
    """
    seen: set[str] = set()
    current: dict[str, Any] | None = entry
    while current is not None:
        uid = current.get("uuid")
        if not isinstance(uid, str) or uid in seen:
            return None
        seen.add(uid)
        if current.get("type") in _MESSAGE_TYPES:
            return current
        current = _parent_of(current, by_uuid)
    return None


def _is_conversational(entry: Mapping[str, Any]) -> bool:
    """Whether this leaf is the user's own conversation rather than a side one.

    The same three flags the chain walk drops: ``isSidechain`` is a subagent's
    turn, ``teamName`` is a team agent's, and ``isMeta`` is the transcript's own
    bookkeeping. A session whose only leaves are sidechain ones is still read —
    falling back to them beats reporting no conversation at all — but they lose
    to a real leaf whenever the file has one.
    """
    return not entry.get("isSidechain") and not entry.get("teamName") and not entry.get("isMeta")


def _omission_kind(entry: Mapping[str, Any], *, on_chain: bool) -> str | None:
    """Why this entry produced no message, or ``None`` when it produced one.

    The order is most-specific-first, so an entry a reader rejects for a concrete
    reason is never filed under the generic one: a sidechain turn on the branch
    that was dropped is a ``isSidechain`` omission, not a "we did not follow
    this branch" one, and the count a consent screen shows is only useful if the
    two are not conflated.

    ``isSidechain``/``teamName``/``isMeta`` are Claude Code's own flags, and the
    kinds are named after them so a stored omission is greppable against the
    source schema.
    """
    if entry.get("isSidechain"):
        return OMISSION_SIDECHAIN
    if entry.get("teamName"):
        return OMISSION_TEAM
    if entry.get("isMeta"):
        return OMISSION_META
    if entry.get("type") not in _MESSAGE_TYPES:
        return OMISSION_ENTRY_TYPE
    if not on_chain:
        return OMISSION_OFF_CHAIN
    return None


def _message_text(payload: object) -> tuple[str, int]:
    """One entry's text and how many of its content blocks held none.

    A message's content is either a string or a list of blocks, and the list is
    where tool calls, tool results, reasoning and attachments live. Those are not
    text a fact extractor may read — the tool-less extractor (C4) is a later
    child, and this contract does not carry raw tool payloads — so they are
    counted and left behind rather than read.

    Whitespace is collapsed to single spaces: the text is what an extractor
    reads and what a normalized-fact dedupe compares, and the source's own line
    breaks and indentation are neither.
    """
    if not isinstance(payload, Mapping):
        return "", 0
    content = payload.get("content")
    if isinstance(content, str):
        return _collapse(content), 0
    if not isinstance(content, list):
        return "", 0

    parts: list[str] = []
    dropped = 0
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(_collapse(text))
                continue
        dropped += 1
    return " ".join(part for part in parts if part), dropped


def _collapse(text: str) -> str:
    """Whitespace runs collapsed to one space, ends trimmed."""
    return " ".join(text.split())