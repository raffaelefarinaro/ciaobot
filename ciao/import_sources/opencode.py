"""The OpenCode adapter: one V2 session export as a NormalizedSession.

OpenCode has no transcript file an importer may open. Its history lives in a
SQLite database owned by a running opencode server, and the supported way to
read it is the **V2 CLI**: `opencode session export <id>` writes the whole
session to stdout as one JSON object, and `opencode session list --format json`
writes the project-scoped page of sessions. This adapter shells out to those two
commands and maps what comes back into the C3a contract — the same three answers
the Claude Code adapter gives, with the same rule that an omission is declared
rather than silent.

**The export shape is `{info, messages}`**, declared in ``sst/opencode`` at
``packages/schema/src/session-transfer.ts`` as
``Schema.Struct({ info: Session.Info, messages: Schema.Array(SessionMessage.Info) })``
and built in ``packages/core/src/session/transfer.ts`` as
``{ info: sessions.get(id), messages: sessions.messages({ sessionID, order: "asc" }).filter(isSettled) }``.
Every message is one **flat** object discriminated on its ``type`` tag — there is
no per-message ``role`` field and no ``{info, parts}`` nesting; an assistant
turn's prose lives in ``content[]``, whose parts are tagged ``text``,
``reasoning`` or ``tool``. The mapping is therefore:

* ``user`` → ``user``, text from the message's own ``text``;
* ``assistant`` → ``assistant``, text from its ``content[]`` ``text`` parts;
* every other tag — ``synthetic``, ``system``, ``skill``, ``shell``,
  ``compaction``, ``idle``, ``agent-switched``, ``model-switched``,
  ``location-switched``, and whatever the format adds next — is a record rather
  than a turn, and is counted as an omission;
* ``anchor`` is the message's own ``id`` (``msg_…``), the only stable way back
  to the source.

**Every part that is not prose is counted.** A reasoning part, a tool part with
its input and its output, a user turn's ``files``/``agents``/``skills``, a
``shell`` command and its output, a ``compaction`` summary: none of it is text a
fact extractor may read, and the contract does not carry raw tool payloads, so it
is counted under ``non_text_content`` (parts) or ``other_entry_type`` (whole
records) rather than read. That is the same accounting the Claude Code adapter
does for its ``tool_use``/``tool_result`` blocks.

**``isSettled`` is applied here too, not just relied on.** The CLI filters an
assistant message out unless ``time.completed`` is set, so an interrupted turn
is normally absent from the export entirely — and an adapter that only read what
arrived would report a shorter conversation with nothing to show for the gap. So
an assistant message that arrives without ``time.completed`` produces **no
message** and is counted, on the same rule the CLI uses. It is counted under
``other_entry_type`` because that is the contract's closed kind for "a record
this reader did not turn into a turn"; there is no separate kind for an
unfinished turn, and the count is what makes the loss visible. A ``shell`` or
``compaction`` message that is still ``running`` needs no rule of its own: both
are non-turn records and are counted whether or not they are settled.

**No timestamp is invented.** ``time.created`` is epoch milliseconds
(``DateTimeUtcFromMillis`` encodes to a number, not an ISO string) and is
rendered as ISO-8601 UTC, because that is the value the message itself carries.
When the message has no ``time`` at all the timestamp is ``None``. There is no
file behind this source and therefore no mtime to reach for even if the export
had no date: a session reached through the CLI names no path, which is why
``SourceRef.path`` is empty here and ``project_hint`` holds ``info.location.directory``
— a real directory, unlike Claude Code's lossy slug, so it is stored under the
same field and read as what it is.

**The read is bounded, and every failure is a refusal, never a guess.** The
runner passes ``timeout`` to :func:`subprocess.run` with ``capture_output=True``,
mirroring ``ciao.providers.opencode._server_list``; stdout larger than
:data:`~ciao.import_sources.contract.MAX_SESSION_BYTES` is never parsed at all.
An export is one JSON object, so a cut payload has no whole-message boundary to
stop on and a half-recovered conversation presented as whole is exactly the
failure this package exists to prevent: an over-cap export comes back as a
session with **no** messages, ``truncated`` set and a ``truncated`` omission,
saying "this was never read" instead of guessing. A non-zero exit, a timeout, a
CLI that is not installed, output that is not JSON and an opencode older than the
V2 floor all raise :class:`SourceError` with a stated reason.

**The floor is single-sourced, not restated.** ``opencode session list`` and
``opencode session export`` are read through ``ciao.providers.opencode``'s own
``resolve_opencode_binary`` (which honours ``CIAO_OPENCODE_BIN`` and the
login-shell PATH) and its ``_server_version_error``/``_version_number``, whose
floor is ``(2, 0, 16)``. An older CLI is reported ``Unsupported`` rather than
parsed on the chance that the schema happens to look familiar; the export schema
above was read from the tagged source *at that floor* for exactly this reason.

**Discovery cannot paginate, so it says so.** ``session list`` resolves
``projectID`` from ``process.cwd()`` and passes
``limit: Option.getOrElse(maxCount, () => 100)`` with **no cursor and no offset**,
so one call returns exactly one page of the project's most recent root sessions
and a project with more than that holds sessions this scan cannot see. The cap is
set explicitly to :data:`DISCOVERY_MAX_COUNT` and a full page is logged as a full
page; discovery returns ``SourceRef`` metadata only — an id, a title-bearing row's
directory, never a message — so it cannot read a conversation before the user has
chosen one.

**This adapter decides nothing about Ciaobot's own usage.** It is metadata and
format only: it exposes ``source.provider``, ``source.source_id`` and
``session.first_user_turn`` so a caller can pass them to
:func:`ciao.import_decouple.classify_session` /
:func:`ciao.import_decouple.canonical_provider`, and it reads no registry, no
chat row and no provider database of its own. ``info.parentID`` is the one piece
of Ciaobot-relevant structure it *does* surface — a child session names its
parent — and it is surfaced as the ``project_hint`` row's directory and the
export's own id, not as a decision.

Reads no operator history beyond the export the caller asked for, starts no
server of its own, opens no socket, and starts no model turn.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ciao.import_sources.contract import (
    MAX_SESSION_BYTES,
    OMISSION_ENTRY_TYPE,
    OMISSION_NON_TEXT_CONTENT,
    OMISSION_TRUNCATED,
    OMISSION_UNREADABLE_LINE,
    PROVIDER_OPENCODE,
    ROLE_ASSISTANT,
    ROLE_USER,
    NormalizedMessage,
    NormalizedSession,
    Omission,
    SourceRef,
)

# The floor, the binary resolution and the V2 version check are the provider's
# own, imported rather than restated: a second copy of "2.0.16" in an importer is
# a rule that can drift from the one the engine enforces. They are private to
# `ciao.providers.opencode` and imported anyway, because that is the price of one
# supported opencode rather than two.
from ciao.providers.opencode import (
    _server_version_error,
    _version_number,
    resolve_opencode_binary,
)

logger = logging.getLogger(__name__)

#: How many sessions one ``session list`` call asks for. The CLI's own default is
#: 100 and it has no cursor, so this is a bound on what a scan can *see*, not a
#: page size: a project with more root sessions than this holds sessions this
#: adapter cannot reach through the CLI at all. Named so the cap is something a
#: caller can read and state rather than a literal buried in an argv.
DISCOVERY_MAX_COUNT = 100

#: How long one bounded ``opencode`` call may take, in seconds. Both commands
#: resolve (or start) a server connection, so they are slower than a local file
#: read and slower than ``--version``, which only has to run the binary.
EXPORT_TIMEOUT_SECONDS = 60.0
DISCOVERY_TIMEOUT_SECONDS = 60.0
VERSION_TIMEOUT_SECONDS = 10.0

#: The opencode binary could not be run at all.
REASON_UNAVAILABLE = "unavailable"
#: The installed opencode is below the enforced V2 floor, or not V2.
REASON_UNSUPPORTED = "unsupported_version"
#: The command ran and exited non-zero.
REASON_FAILED = "command_failed"
#: The command did not answer within its timeout.
REASON_TIMEOUT = "timeout"
#: The command answered with something this adapter cannot read as a V2 export.
REASON_UNREADABLE = "unreadable_output"
#: The answer was larger than :data:`MAX_SESSION_BYTES`, so it was never parsed.
REASON_TOO_LARGE = "over_cap"

#: Why a read was refused. A closed vocabulary, so a caller can tell "the CLI is
#: not installed" from "the CLI is too old" from "the CLI answered something else"
#: without parsing an English sentence.
SOURCE_ERROR_REASONS: tuple[str, ...] = (
    REASON_UNAVAILABLE,
    REASON_UNSUPPORTED,
    REASON_FAILED,
    REASON_TIMEOUT,
    REASON_UNREADABLE,
    REASON_TOO_LARGE,
)

#: The two message tags that are turns of the conversation. Everything else in
#: the union — ``synthetic``, ``system``, ``skill``, ``shell``, ``compaction``,
#: ``idle``, ``agent-switched``, ``model-switched``, ``location-switched`` — is a
#: record the session keeps beside the conversation, and is counted instead.
_ROLE_BY_MESSAGE_TYPE = {"user": ROLE_USER, "assistant": ROLE_ASSISTANT}

#: The assistant part tags that are prose. ``reasoning`` and ``tool`` are not.
_TEXT_PART = "text"


class SourceError(RuntimeError):
    """The opencode CLI could not be read as a V2 session source.

    Raised rather than degraded to an empty session: "this source could not be
    read" and "this session has nothing in it" are different statements, and only
    the first one is true when the CLI is missing, too old, slow, failing or
    answering with something this adapter does not recognise. ``reason`` is one of
    :data:`SOURCE_ERROR_REASONS`.
    """

    def __init__(self, reason: str, message: str) -> None:
        if reason not in SOURCE_ERROR_REASONS:
            raise ValueError(f"source error reason {reason!r} is not one of {SOURCE_ERROR_REASONS}")
        super().__init__(message)
        self.reason = reason


def discover_opencode_sessions(
    project_dir: Path | str,
    *,
    binary: str | None = None,
) -> list[SourceRef]:
    """Every root session opencode lists for one project directory, as metadata.

    ``project_dir`` is a workspace opencode has been run in, and it is passed to
    the command as its working directory because ``session list`` resolves its
    project from ``process.cwd()`` — the same directory listed twice is the only
    way to get a per-project listing, and running it from elsewhere would answer
    with a different project's sessions under this one's name.

    The listing is **metadata only**: a session id and the directory the row
    carries. No message is fetched, so discovery cannot read a conversation
    before the user has chosen one and cannot be made to read one at all.
    ``path`` is empty because the CLI names no file — an OpenCode session lives
    in a server's database, not in a document the user could open.

    ``--max-count`` is set explicitly to :data:`DISCOVERY_MAX_COUNT` rather than
    left to the CLI's default of the same size, so the bound is a decision this
    adapter records rather than an accident, and a full page is logged as a full
    page: ``session list`` has no cursor and no offset, so a project holding more
    root sessions than the cap contains sessions this scan cannot see. The result
    is never a claim to completeness.

    A directory that is not there, or is not a directory, is an empty list: a
    workspace nobody ran opencode in is the ordinary case, not an error. Anything
    else — a missing binary, an unsupported version, a failing or unreadable
    command — raises :class:`SourceError`, because a listing that could not be
    fetched has no business reporting that it found nothing.
    """
    directory = Path(project_dir)
    if not directory.is_dir():
        return []
    resolved = _resolve_binary(binary)
    _require_supported_v2(resolved, timeout=VERSION_TIMEOUT_SECONDS)
    payload = _run_opencode_json(
        resolved,
        [
            "session",
            "list",
            "--max-count",
            str(DISCOVERY_MAX_COUNT),
            "--format",
            "json",
        ],
        DISCOVERY_TIMEOUT_SECONDS,
        cwd=str(directory),
    )
    if not isinstance(payload, list):
        raise SourceError(
            REASON_UNREADABLE,
            f"`opencode session list` in {directory} answered with a "
            f"{type(payload).__name__}, not the documented list of session rows.",
        )
    if len(payload) >= DISCOVERY_MAX_COUNT:
        logger.warning(
            "OpenCode discovery in %s returned %d sessions, the --max-count cap: "
            "`opencode session list` returns one page with no cursor, so older "
            "root sessions exist that this scan did not see.",
            directory,
            len(payload),
        )

    refs: list[SourceRef] = []
    for row in payload:
        if not isinstance(row, Mapping):
            logger.warning("OpenCode session row in %s is not an object; skipped.", directory)
            continue
        source_id = row.get("id")
        if not isinstance(source_id, str) or not source_id:
            logger.warning("OpenCode session row in %s names no session id; skipped.", directory)
            continue
        refs.append(
            SourceRef(
                provider=PROVIDER_OPENCODE,
                source_id=source_id,
                project_hint=_directory_hint(row),
                path="",
            )
        )
    return refs


def read_opencode_session(
    source_id: str,
    *,
    binary: str | None = None,
) -> NormalizedSession:
    """One OpenCode session, exported through the V2 CLI, as a NormalizedSession.

    *source_id* is the session id the listing gave (``ses_…``). The returned
    session names itself from the **export**, not from the argument: ``source_id``
    is ``info.id`` as the CLI wrote it, and ``project_hint`` is
    ``info.location.directory``. Both are taken from the payload so a caller
    cannot be told one session's id over another session's transcript, and
    ``path`` is empty because there is no file behind a CLI export.

    Messages come back in the order the CLI wrote them — ``order: "asc"`` — which
    is the conversation order, and anchors on each message's own ``id``.

    An export over :data:`~ciao.import_sources.contract.MAX_SESSION_BYTES` is not
    parsed at all: it comes back with no messages, ``truncated`` set and a
    ``truncated`` omission, because one JSON object has no whole-message boundary
    to stop on and half a conversation is worse than a shorter one that says it
    is shorter. Everything else that can go wrong — no binary, a version below the
    V2 floor, a non-zero exit, a timeout, an unreadable payload — raises
    :class:`SourceError` rather than returning an empty session.

    A payload that is not ``{info, messages}`` in the declared shape is refused
    for the same reason: half a schema read as if it were the whole thing is the
    failure this package exists to prevent. A payload whose shape is right but
    whose *contents* are odd degrades with a stated reason instead — a row with no
    id, a record that is not a turn, a part that is not prose are each counted in
    ``omissions``.

    Reads no registry and decides nothing about Ciaobot's own usage: the caller
    passes ``session.source`` and ``session.first_user_turn`` to
    :func:`ciao.import_decouple.classify_session`.
    """
    requested = source_id.strip()
    if not requested:
        raise SourceError(REASON_UNREADABLE, "an OpenCode session must be named by its session id.")
    resolved = _resolve_binary(binary)
    _require_supported_v2(resolved, timeout=VERSION_TIMEOUT_SECONDS)
    try:
        payload = _run_opencode_json(
            resolved,
            # No `--format json`: `session export` has no such flag at the
            # enforced floor — its whole parameter list is the session id plus
            # `--sanitize`, `--server` and `--standalone` — and it writes
            # `JSON.stringify(data, null, 2)` to stdout unconditionally. Passing
            # a flag the command does not declare would make every export fail.
            # `--sanitize` is deliberately never passed: it replaces the prose
            # with `[redacted:<kind>:<id>]` placeholders, and an import of
            # placeholders is not an import.
            ["session", "export", requested],
            EXPORT_TIMEOUT_SECONDS,
        )
    except SourceError as exc:
        if exc.reason != REASON_TOO_LARGE:
            raise
        # Named from the requested id because the payload was never parsed, and
        # reported as zero messages: what follows the cap is unknown, not empty,
        # which is the difference `truncated` exists to state.
        return NormalizedSession(
            source=SourceRef(provider=PROVIDER_OPENCODE, source_id=requested),
            omissions=(Omission(kind=OMISSION_TRUNCATED),),
            truncated=True,
        )

    if not isinstance(payload, Mapping):
        raise SourceError(
            REASON_UNREADABLE,
            f"The export of {requested} is a {type(payload).__name__}, "
            "not the documented {info, messages} object.",
        )
    info = payload.get("info")
    if not isinstance(info, Mapping):
        raise SourceError(REASON_UNREADABLE, f"The export of {requested} carries no 'info' object.")
    exported_id = info.get("id")
    if not isinstance(exported_id, str) or not exported_id:
        raise SourceError(
            REASON_UNREADABLE,
            f"The export of {requested} names no session id in 'info', so it cannot "
            "be cited against a source.",
        )
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise SourceError(
            REASON_UNREADABLE,
            f"The export of {requested} carries no 'messages' list; it is not the "
            "documented shape and is not partially trusted.",
        )

    normalized: list[NormalizedMessage] = []
    counts: dict[str, int] = {}

    def _drop(kind: str, count: int = 1) -> None:
        if count:
            counts[kind] = counts.get(kind, 0) + count

    for message in messages:
        if not isinstance(message, Mapping):
            # JSON that parsed and is not a message object. Corruption, and the
            # one omission a consent screen has to show.
            _drop(OMISSION_UNREADABLE_LINE)
            continue
        anchor = message.get("id")
        if not isinstance(anchor, str) or not anchor:
            # A well-formed record with nothing to anchor it on, so nothing to
            # cite: the same accounting as any other non-message record.
            _drop(OMISSION_ENTRY_TYPE)
            continue
        role = _ROLE_BY_MESSAGE_TYPE.get(str(message.get("type")))
        if role is None:
            _drop(OMISSION_ENTRY_TYPE)
            continue
        if role == ROLE_ASSISTANT and not _is_settled(message):
            _drop(OMISSION_ENTRY_TYPE)
            continue
        text, non_text = _message_text(message)
        _drop(OMISSION_NON_TEXT_CONTENT, non_text)
        normalized.append(
            NormalizedMessage(
                role=role,
                text=text,
                # str() is narrowing, and parsing proved `id` is a non-empty
                # string: it is the one field a message cannot do without.
                anchor=str(anchor),
                timestamp=_message_time(message),
            )
        )

    location = info.get("location")
    return NormalizedSession(
        source=SourceRef(
            provider=PROVIDER_OPENCODE,
            source_id=str(exported_id),
            project_hint=_directory_hint(
                location if isinstance(location, Mapping) else {}
            ),
            path="",
        ),
        messages=tuple(normalized),
        omissions=tuple(Omission(kind=kind, count=counts[kind]) for kind in sorted(counts)),
        truncated=False,
    )


def _resolve_binary(binary: str | None) -> str:
    """The opencode CLI to run, or :class:`SourceError` when there is none.

    An explicit *binary* is taken as given — that is the seam tests and a caller
    with an unusual install both use. Otherwise the provider's own resolution
    runs, so ``CIAO_OPENCODE_BIN`` and the login-shell PATH mean here exactly
    what they mean everywhere else in Ciaobot, and there is no second place to
    configure it. It can raise ``OSError`` for a PATH entry that is a directory
    or a broken wrapper, which is "installed but not runnable" rather than "not
    installed" and is reported as such.
    """
    if binary is not None:
        explicit = binary.strip()
        if not explicit:
            raise SourceError(REASON_UNAVAILABLE, "An empty path is not the opencode CLI.")
        return explicit
    try:
        found = resolve_opencode_binary()
    except OSError as exc:
        raise SourceError(
            REASON_UNAVAILABLE,
            f"The opencode CLI is on PATH but cannot be run: {exc}",
        ) from exc
    if not found:
        raise SourceError(
            REASON_UNAVAILABLE,
            "The opencode CLI was not found; install opencode or point "
            "CIAO_OPENCODE_BIN at it.",
        )
    return found


def _require_supported_v2(binary: str, *, timeout: float) -> str:
    """The installed version as the CLI reports it, or a refusal.

    The floor is the provider's, not this module's: ``_server_version_error`` is
    the same function ``ciao/providers/opencode.py`` refuses a chat with, so an
    importer and the engine can never disagree about which opencode is supported.
    A version that cannot be parsed at all is refused too — "unknown" is not a
    version, and guessing at a schema from a build nobody has identified is the
    failure the feasibility report rules out.
    """
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceError(
            REASON_TIMEOUT, f"`{binary} --version` did not answer within {timeout:g}s."
        ) from exc
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise SourceError(REASON_UNAVAILABLE, f"Cannot run `{binary} --version`: {exc}") from exc
    if result.returncode != 0:
        raise SourceError(
            REASON_FAILED,
            f"`{binary} --version` exited {result.returncode}: {_detail(result.stderr)}",
        )
    reported = (result.stdout or "").strip().splitlines()
    version = reported[0].strip() if reported else ""
    unsupported = _server_version_error({"version": version})
    if unsupported is not None:
        # The parsed tuple is in the message because the string is not: "v2.0.16"
        # and "2.0.16-beta.3" read alike in a log line, and the log is what an
        # operator has when they ask which opencode is installed.
        raise SourceError(
            REASON_UNSUPPORTED,
            f"{unsupported} `opencode --version` reported "
            f"{version or '(nothing)'}, parsed as {_version_number(version) or 'no version'}.",
        )
    return version


def _run_opencode_json(
    binary: str,
    args: Sequence[str],
    timeout: float,
    *,
    cwd: str | None = None,
    max_bytes: int | None = MAX_SESSION_BYTES,
) -> Any:
    """Run ``opencode <args>`` once, bounded, and return its stdout parsed as JSON.

    The shape mirrors ``ciao.providers.opencode._server_list``: one
    :func:`subprocess.run` with ``capture_output=True``, an explicit encoding and
    a ``timeout``, and no shell. Everything that can go wrong becomes a
    :class:`SourceError` naming which of the things happened — a missing binary,
    a timeout, a non-zero exit, output that is not JSON, or output too large to
    parse.

    ``max_bytes`` is the bound on what one call may answer with. The check is on
    the decoded text's **byte** length, and an over-cap answer is refused before
    :func:`json.loads` is reached: parsing is where an unbounded payload would
    actually be built in memory, so the cap has to be here rather than in a
    consumer. ``None`` disables the check, and is not used: a call with no bound
    is the one thing this helper exists to prevent.
    """
    command = [binary, *args]
    pretty = " ".join(command)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceError(
            REASON_TIMEOUT, f"`{pretty}` did not answer within {timeout:g}s."
        ) from exc
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise SourceError(REASON_UNAVAILABLE, f"Cannot run `{pretty}`: {exc}") from exc
    if result.returncode != 0:
        raise SourceError(
            REASON_FAILED,
            f"`{pretty}` exited {result.returncode}: {_detail(result.stderr)}",
        )
    text = result.stdout or ""
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise SourceError(
            REASON_TOO_LARGE,
            f"`{pretty}` answered with more than {max_bytes} bytes, past the bound one "
            "import read may hold.",
        )
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise SourceError(
            REASON_UNREADABLE, f"`{pretty}` did not answer with JSON: {exc}"
        ) from exc


def _detail(stderr: str | None) -> str:
    """The first line of a command's stderr, for the message of a refusal."""
    for line in (stderr or "").splitlines():
        text = line.strip()
        if text:
            return text
    return "(no message)"


def _directory_hint(holder: Mapping[str, Any]) -> str:
    """The ``directory`` recorded at *holder*, or ``""`` when it names none.

    Both places OpenCode names a project record it the same way: a listing row
    carries ``directory`` at the top level, and an export carries it under
    ``info.location``. For this source that value **is** a directory — unlike
    Claude Code's one-way slug, nothing is folded or truncated on the way in — so
    it is held under the contract's ``hint`` field and read as what it is. It is
    still a hint: it is the session's own recorded location, not a workspace
    Ciaobot resolved, and nothing downstream may treat it as identity.
    """
    value = holder.get("directory")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _is_settled(message: Mapping[str, Any]) -> bool:
    """Whether the export's own ``isSettled`` rule kept this assistant turn.

    ``isSettled`` in ``packages/core/src/session/transfer.ts`` keeps an
    assistant message only when ``time.completed !== undefined``; a ``shell`` or
    ``compaction`` only when its ``status`` is not ``running``. The CLI has
    already applied it, so this is the second half of the same rule: a turn that
    is still in flight, or was interrupted, never reaches a fact extractor as
    something the model said. A ``shell``/``compaction`` needs no check here
    because both are counted as non-turn records either way.
    """
    time = message.get("time")
    return isinstance(time, Mapping) and "completed" in time


def _message_text(message: Mapping[str, Any]) -> tuple[str, int]:
    """One message's prose and how much beside it is not prose.

    A ``user`` message's prose is its ``text``; an assistant's is its ``content[]``
    ``text`` parts, joined, because the parts are the message and a two-sentence
    answer split across them is one turn. Everything else beside the prose is
    counted one part at a time: an assistant's ``reasoning`` and ``tool`` parts
    (whose ``state`` holds the tool's input and its output), a user turn's
    ``files``/``agents``/``skills``, and any part tag this reader does not know.
    A count of zero means the message carried nothing but prose, which is the
    ordinary case and therefore no omission at all.

    Whitespace is collapsed to single spaces, as in the Claude Code adapter and
    for the same reason: the text is what an extractor reads and what a
    normalized-fact dedupe compares, and the source's own formatting is neither.
    """
    if message.get("type") == "user":
        raw = message.get("text")
        prose = raw if isinstance(raw, str) else ""
        return _join([prose]), len(_attachments(message))
    content = message.get("content")
    if not isinstance(content, list):
        # An assistant message with no parts at all carries no prose and no
        # countable part either; it is a turn with empty text, which the contract
        # reads as different from a missing turn.
        return "", 0
    pieces: list[str] = []
    dropped = 0
    for part in content:
        if isinstance(part, Mapping) and part.get("type") == _TEXT_PART:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                pieces.append(text)
                continue
        dropped += 1
    return _join(pieces), dropped


def _attachments(message: Mapping[str, Any]) -> list[Any]:
    """A user turn's structured attachments, which are counted and never read."""
    found: list[Any] = []
    for key in ("files", "agents", "skills"):
        value = message.get(key)
        if isinstance(value, list):
            found.extend(value)
    return found


def _join(pieces: Sequence[str]) -> str:
    return " ".join(" ".join(piece.split()) for piece in pieces if piece and piece.strip())


def _message_time(message: Mapping[str, Any]) -> str | None:
    """The turn's own creation time as ISO-8601 UTC, or ``None`` when it has none.

    ``time.created`` is epoch milliseconds — ``DateTimeUtcFromMillis`` decodes a
    number into a ``DateTimeUtc``, so the JSON the CLI writes holds a number, not
    an ISO string — and it is rendered here as the ISO-8601 UTC the contract's
    string field is for. Nothing else is ever consulted: there is no file behind
    this source, so there is no mtime to reach for, and a message with no ``time``
    reports ``None`` rather than a date this reader did not read.
    """
    time = message.get("time")
    if not isinstance(time, Mapping):
        return None
    created = time.get("created")
    # bool is an int subclass, and `true` as a timestamp is a shape error, not a
    # date. Anything non-numeric is the same kind of absence.
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None