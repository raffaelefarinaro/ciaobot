"""The provider-neutral shape an imported conversation is read into.

An importer needs three answers before it can extract anything from a past
conversation: *which* conversation this is, *what was said* in it, and *what did
we not read*. Those three answers are this package's only product.
:func:`read_claude_code_session` fills one in; the extractor (C4), the consent UI
(C5) and the batch store (C6) consume it without knowing which provider wrote
the file underneath.

**Why a contract and not a helper per adapter.** Every decision that *loses*
information is made once, here, and recorded. A branch is a real fork in the
source, and which fork is "the conversation" is a judgement; an omission is
something a user would have to be told about before a model reads their history.
If the Claude Code adapter answered those two questions one way and the OpenCode
adapter (C3b) another, an omission would be loud in one importer and silent in
the other, which is the failure mode the feasibility report rules out ("a batch
that cannot state what it dropped is not allowed to run").

**Omissions are declared, never silent.** Every entry a reader does not turn
into a message is counted by reason in :attr:`NormalizedSession.omissions` —
sidechain and team and meta entries, the non-message entry types, a branch that
branch selection dropped, content blocks that were not text, a line that could
not be parsed, and a read that hit the byte cap. A reader that only returned the
messages it happened to like would leave all of those invisible, and the count
is the only thing that makes them checkable.

**No timestamp is invented.** :attr:`NormalizedMessage.timestamp` is ``str |
None`` and ``None`` is the honest value for a Claude Code session:
``transcripts.get_session_messages_full`` reads no timestamp field, and a file's
mtime is when the engine looked at it, not when the conversation happened (the
feasibility report's "Messages, roles, dates, anchors"). An adapter with a real
date sets it; an adapter without one leaves ``None``, and nothing here derives
one. It is a ``str`` rather than a ``datetime`` because each source spells its
dates its own way and this contract does not pick a format for it yet.

**Provider vocabulary** is the *adapter* names — ``claude_code``, ``opencode``,
``claude_account`` — not Ciaobot's own ``claude``/``opencode``. The two
vocabularies describe the same session from two sides, and
:func:`ciao.import_decouple.canonical_provider` is the one mapping between them;
every lookup that crosses that line goes through it, because a literal
comparison would read a Ciaobot-own Claude session as the user's own history.

Reads nothing. This module has no file access, no network, no model and no
engine; the adapters are the only place a provider's own storage is touched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The largest session file an adapter reads, in bytes. The cap is a bound on
#: what one scan can hold in memory at once, not a size a real session is
#: expected to reach: a long conversation carrying image payloads gets there,
#: and a file over the cap is read up to its last whole line and reported
#: :attr:`NormalizedSession.truncated` rather than refused or silently clipped.
#: Owned here because the contract is what "bounded" is a promise of — an
#: adapter that reads more than this is not honouring it.
MAX_SESSION_BYTES = 8 * 1024 * 1024

#: An adapter's name for a provider, as :class:`SourceRef` carries it. These are
#: not Ciaobot's registry ids (``ciao.provider_registry`` spells Claude as
#: ``claude``); see the module docstring.
PROVIDER_CLAUDE_CODE = "claude_code"
PROVIDER_OPENCODE = "opencode"
PROVIDER_CLAUDE_ACCOUNT = "claude_account"

#: Every provider id the contract knows by name. A provider outside this set is
#: a contract violation rather than a future value, so a typo cannot mint a new
#: vocabulary entry by accident.
KNOWN_PROVIDERS: tuple[str, ...] = (
    PROVIDER_CLAUDE_CODE,
    PROVIDER_OPENCODE,
    PROVIDER_CLAUDE_ACCOUNT,
)

#: The providers an adapter can actually read today. ``claude_account`` is known
#: and deliberately unsupported: the feasibility report could not verify what a
#: Claude account export contains, and an adapter that guessed at an unverified
#: format is the failure this package is built to avoid.
SUPPORTED_PROVIDERS: tuple[str, ...] = (PROVIDER_CLAUDE_CODE, PROVIDER_OPENCODE)

#: Message roles. ``other`` is what a role nobody can map to ``user`` or
#: ``assistant`` normalizes to, so a provider's extra vocabulary is carried
#: rather than dropped — though the adapters here exclude such entries and
#: record them as omissions instead, because a fact extractor reads text.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_OTHER = "other"
ROLES: tuple[str, ...] = (ROLE_USER, ROLE_ASSISTANT, ROLE_OTHER)

# Omission kinds. The first three are named after the source fields that set
# them, so a kind in a stored session is greppable against the provider's own
# schema; the rest name a decision this package made.
#: An entry flagged ``isSidechain``: a subagent's own turn, not this
#: conversation's.
OMISSION_SIDECHAIN = "isSidechain"
#: An entry flagged ``teamName``: another agent's turn in a team session.
OMISSION_TEAM = "teamName"
#: An entry flagged ``isMeta``: the transcript's own bookkeeping posing as a turn.
OMISSION_META = "isMeta"
#: An entry that is not a message (``progress``, ``system``, ``attachment``, and
#: whatever else the format adds) — a chain link or a non-message record.
OMISSION_ENTRY_TYPE = "other_entry_type"
#: A message entry that branch selection did not follow: a real turn of the
#: conversation on a fork this import did not take.
OMISSION_OFF_CHAIN = "not_on_main_chain"
#: Content blocks inside a message that were not text: tool calls and results,
#: reasoning, attachments, inline file data.
OMISSION_NON_TEXT_CONTENT = "non_text_content"
#: A line this reader could not read as an entry at all — it does not parse as
#: JSON, or it parses as JSON that is not an object. A valid object that carries
#: no string ``uuid`` (``summary``, ``file-history-snapshot``, ``custom-title``)
#: is :data:`OMISSION_ENTRY_TYPE` instead: the file is not damaged, this reader
#: simply has nothing to walk there. Counted either way, never dropped without
#: saying so.
OMISSION_UNREADABLE_LINE = "unreadable_line"
#: The read stopped at :data:`MAX_SESSION_BYTES`; what follows is unknown, not
#: empty. Always accompanied by :attr:`NormalizedSession.truncated`.
OMISSION_TRUNCATED = "truncated"

#: Every kind an adapter may report. A kind outside this set is a contract
#: violation, so a consumer (the consent UI) can be written against the list.
OMISSION_KINDS: tuple[str, ...] = (
    OMISSION_SIDECHAIN,
    OMISSION_TEAM,
    OMISSION_META,
    OMISSION_ENTRY_TYPE,
    OMISSION_OFF_CHAIN,
    OMISSION_NON_TEXT_CONTENT,
    OMISSION_UNREADABLE_LINE,
    OMISSION_TRUNCATED,
)


class ContractError(ValueError):
    """A payload is not a readable normalized session.

    Raised by the ``from_json`` readers rather than half-filling an object: a
    session that quietly lost its omissions, or a message with no anchor, is
    exactly the thing this package exists to make impossible.
    """


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One conversation, named well enough to be cited.

    ``provider`` is the adapter's name for it (see :data:`KNOWN_PROVIDERS`).
    ``source_id`` is the provider's own id for the session, not Ciaobot's — the
    dedupe key ``(provider, source_id, anchor)`` and the provenance tag
    ``provider:source_id:anchor`` both rest on it.

    ``project_hint`` is the *weakest* locator the source can offer and is
    explicitly not a path: Claude Code's slug
    (:func:`ciao.agent_paths.claude_project_slug`) folds every non-alphanumeric
    to ``-`` and truncates a long one, so a project cannot be recovered from it.
    Naming it "hint" is what stops a consumer presenting it as a directory.

    ``path`` is where the file was read from, for a log and for the reader that
    wants the bytes itself; it is not part of the session's identity, so a
    snapshot stays valid after the file moves.
    """

    provider: str
    source_id: str
    project_hint: str = ""
    path: str = ""

    def __post_init__(self) -> None:
        if self.provider not in KNOWN_PROVIDERS:
            raise ContractError(
                f"provider {self.provider!r} is not one of {KNOWN_PROVIDERS}"
            )
        if not self.source_id:
            raise ContractError("A source must name the session it is.")

    def to_json(self) -> dict[str, str]:
        """The snapshot form; every field, so nothing is lost in a round trip."""
        return {
            "provider": self.provider,
            "source_id": self.source_id,
            "project_hint": self.project_hint,
            "path": self.path,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> SourceRef:
        """The :class:`SourceRef` a snapshot describes, or :class:`ContractError`."""
        return cls(
            provider=_text(payload, "provider"),
            source_id=_text(payload, "source_id"),
            project_hint=_optional_text(payload, "project_hint"),
            path=_optional_text(payload, "path"),
        )


@dataclass(frozen=True, slots=True)
class Omission:
    """Something the reader left out, and how much of it.

    ``kind`` is one of :data:`OMISSION_KINDS` — a closed vocabulary, so a
    consumer can be written against it and a typo cannot invent a category.
    ``count`` is a count of entries, not a severity: two sidechain entries are
    the same omission twice, and one unreadable line is one.
    """

    kind: str
    count: int = 1

    def __post_init__(self) -> None:
        if self.kind not in OMISSION_KINDS:
            raise ContractError(f"omission kind {self.kind!r} is not one of {OMISSION_KINDS}")
        # bool is an int subclass, and a count of "true" is a typo, not a count.
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 0:
            raise ContractError(f"omission count must be a non-negative integer, got {self.count!r}")

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "count": self.count}

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> Omission:
        return cls(kind=_text(payload, "kind"), count=_count(payload, "count"))


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    """One turn of a conversation, as text somebody can read.

    ``role`` is one of :data:`ROLES`. ``anchor`` is the provider's own id for
    this turn — a Claude Code entry ``uuid``, an OpenCode message id — and it is
    the only stable way back to the source, so it is required and never blank.

    ``text`` is normalized (whitespace collapsed to single spaces): it is what an
    extractor reads and what a normalized-fact dedupe compares, and the source's
    own formatting is not either of those. That collapsing is an **adapter
    convention**, not something this dataclass enforces — it is the provider
    adapter that knows how the source formats its text, and a different provider
    may normalize a different way. An empty ``text`` with a recorded omission
    beside it means the turn carried no prose (a tool call, say), which is a
    different fact from a missing turn.

    ``timestamp`` is ``None`` when the source has no date for the turn. See the
    module docstring: nothing here may supply one.
    """

    role: str
    text: str
    anchor: str
    timestamp: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ContractError(f"role {self.role!r} is not one of {ROLES}")
        if not self.anchor:
            raise ContractError("A normalized message must carry its source anchor.")
        if self.timestamp is not None and not isinstance(self.timestamp, str):
            raise ContractError(f"timestamp must be a string or None, got {self.timestamp!r}")

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "anchor": self.anchor,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> NormalizedMessage:
        return cls(
            role=_text(payload, "role"),
            # An empty text is a real value: a turn whose content blocks held no
            # prose keeps its role and anchor beside a recorded omission.
            text=_optional_text(payload, "text"),
            anchor=_text(payload, "anchor"),
            timestamp=_nullable_text(payload, "timestamp"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedSession:
    """One conversation, normalized: its messages and what was left out.

    ``messages`` are in conversation order, oldest first — the order the source
    walked its parent chain in, which is the only order a branch-free reading
    has. ``omissions`` are the whole record of what this session is not: an
    empty tuple means nothing was dropped, which is a claim a consumer may show.

    ``truncated`` says the reader stopped at :data:`MAX_SESSION_BYTES` and never
    saw the rest. It is separate from ``omissions`` because the difference
    matters to a caller: an omission is something that was read and rejected, and
    a truncation is something that was never read at all.
    """

    source: SourceRef
    messages: tuple[NormalizedMessage, ...] = ()
    omissions: tuple[Omission, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple) or not all(
            isinstance(message, NormalizedMessage) for message in self.messages
        ):
            raise ContractError("messages must be a tuple of NormalizedMessage")
        if not isinstance(self.omissions, tuple) or not all(
            isinstance(omission, Omission) for omission in self.omissions
        ):
            raise ContractError("omissions must be a tuple of Omission")
        if not isinstance(self.truncated, bool):
            raise ContractError(f"truncated must be a boolean, got {self.truncated!r}")

    @property
    def first_user_turn(self) -> str:
        """The first ``user`` message's text, or ``""`` when there is none.

        What :func:`ciao.import_decouple.classify_session` needs to tell a
        Ciaobot-driven session from the user's own history, so it lives here: a
        consumer that went looking for "the first thing the user said" itself
        would have to re-implement the same pick.
        """
        for message in self.messages:
            if message.role == ROLE_USER:
                return message.text
        return ""

    def omission_counts(self) -> dict[str, int]:
        """Every omission kind present, mapped to how many entries it covers.

        The whole record in one mapping, for a consent screen or a log line. A
        kind absent from the mapping was not dropped; a kind mapped to zero is
        dropped from the result rather than carried, so the mapping says only
        what happened. A kind named twice is **summed**, not overwritten: the
        count is how much was left out, so two rows of the same kind are that
        much more of it however they were grouped on the way in.
        """
        counts: dict[str, int] = {}
        for omission in self.omissions:
            if omission.count:
                counts[omission.kind] = counts.get(omission.kind, 0) + omission.count
        return counts

    def to_json(self) -> dict[str, Any]:
        """The snapshot form: every field, so the round trip is lossless."""
        return {
            "source": self.source.to_json(),
            "messages": [message.to_json() for message in self.messages],
            "omissions": [omission.to_json() for omission in self.omissions],
            "truncated": self.truncated,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> NormalizedSession:
        """The :class:`NormalizedSession` a snapshot describes.

        Raises :class:`ContractError` on anything it cannot read whole; see that
        class's docstring.
        """
        source = payload.get("source")
        if not isinstance(source, Mapping):
            raise ContractError("a snapshot needs a 'source' object")
        return cls(
            source=SourceRef.from_json(source),
            messages=tuple(
                NormalizedMessage.from_json(item)
                for item in _objects(payload, "messages")
            ),
            omissions=tuple(
                Omission.from_json(item) for item in _objects(payload, "omissions")
            ),
            truncated=_flag(payload, "truncated"),
        )


def _text(payload: Mapping[str, Any], key: str) -> str:
    """A required string field, or :class:`ContractError`."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{key!r} must be a non-empty string, got {value!r}")
    return value


def _optional_text(payload: Mapping[str, Any], key: str) -> str:
    """A string field that may be absent but is never another type."""
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ContractError(f"{key!r} must be a string or absent, got {value!r}")
    return value


def _nullable_text(payload: Mapping[str, Any], key: str) -> str | None:
    """A field that is a string or genuinely ``None``, and the two stay apart.

    :attr:`NormalizedMessage.timestamp` is the reason: a snapshot written with
    ``None`` has to read back as ``None``, or "this turn has no known date"
    would come back as an empty date that a consumer has to guess about.
    """
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{key!r} must be a string or null, got {value!r}")
    return value


def _flag(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key, False)
    if not isinstance(value, bool):
        raise ContractError(f"{key!r} must be a boolean, got {value!r}")
    return value


def _count(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(f"{key!r} must be a non-negative integer, got {value!r}")
    return value


def _objects(payload: Mapping[str, Any], key: str) -> Sequence[Any]:
    """A list field, refused when it is not a list of objects."""
    value = payload.get(key)
    if not isinstance(value, list):
        raise ContractError(f"{key!r} must be a list, got {value!r}")
    for item in value:
        if not isinstance(item, Mapping):
            raise ContractError(f"{key!r} must hold objects, got {item!r}")
    return value