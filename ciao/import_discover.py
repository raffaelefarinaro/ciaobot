"""What past conversations this machine holds, and which of them may be imported.

The import adapters can read a conversation; nothing until this module could
*find* one, show it to a person, or say whether it is Ciaobot's own. This is the
engine-host half of that, and it is deliberately the smallest half that answers
the question:

* :func:`discover_sources` lists what is there. It resolves **one** root per
  source from the configuration — Claude Code's slug directory through
  :func:`ciao.agent_paths.claude_projects_dir`, the OpenCode project directory
  through the workspace's own agent root — and reads nothing else. It scans no
  directory outside those roots, follows no symlink, refuses a file over
  :data:`~ciao.import_sources.contract.MAX_SESSION_BYTES`, and **reads no
  conversation content at all**: what comes back is names, sizes and reasons.
  :func:`~ciao.import_sources.claude_code.discover_claude_code_sessions` and
  :func:`~ciao.import_sources.opencode.discover_opencode_sessions` do the
  listing; this module decides what may be offered and reports what it refused.
* :func:`classify_for_import` is the one place the decoupling rule is applied to a
  discovered session (behind the shared ``_classify`` core), so discovery and
  extraction cannot disagree about it.
* :func:`preview_selected` is the other half of the consent boundary, and the
  first function here that opens a conversation. It reads **only the refs a
  person selected**, resolves each one from the configured root rather than from
  anything a caller supplied, and answers with counts, dates and omission kinds
  — never the text. An OpenCode id is checked against this workspace's own
  listing before anything is exported, because ``session export`` resolves ids
  across projects and an id a caller posts is not evidence of ownership.

**Nothing here is claimed to be `external`.** A session is external only once its
own opening turn has been read and carries no Ciaobot marker — the rule
``docs/CONVERSATION_IMPORT_FEASIBILITY.md`` states and
:func:`ciao.import_decouple.classify_session` implements. Discovery reads no
content, so it cannot apply that rule, and the direction it fails in must be the
safe one: every row it offers is **undecided**, not cleared. What metadata *can*
decide is what Ciaobot itself created or drove, and that is excluded here, before
the file is opened, so Ciaobot's own sessions are never even read.
:func:`preview_selected` then reads the selected rows, applies the same rule again
with the turn it read, and reports anything that is not ``external`` as excluded
with its reason — which is where an ``ambiguous`` session is refused, exactly as
it would be refused again before extraction.

**A listing is a page, not a claim.** :data:`MAX_SESSIONS_PER_SOURCE` bounds what
one source contributes to a listing and OpenCode's CLI answers one unpaged
``--max-count`` page (:data:`~ciao.import_sources.opencode.DISCOVERY_MAX_COUNT`),
so a listing that reaches a cap says so per source in
:attr:`DiscoveryResult.truncated` rather than reading as the whole history.

**A source that cannot be listed is a row, not an error.** An OpenCode older than
the enforced V2 floor, an absent CLI, a failing command — each is reported in
:attr:`DiscoveryResult.unsupported` with the adapter's own reason, so a consent
screen can say *Unsupported version, export a file instead* instead of showing an
empty list that looks like "you have no conversations".

Reads Ciaobot's own two state files (through
:func:`ciao.import_decouple.ciaobot_own_session_ids`), the selected source
files, and — once per preview, and only when an OpenCode ref is selected — that
workspace's own OpenCode session listing. It writes nothing, starts no model
turn, no engine and no server of its own, and never reads ``~/.claude``,
``~/.opencode``, a Downloads folder or an account export anywhere but through
the two configured roots above.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ciao.agent_paths import claude_projects_dir
from ciao.config import CiaoConfig
from ciao.import_decouple import (
    AMBIGUOUS,
    CIAOBOT_OWN,
    EXTERNAL,
    SessionClassification,
    ciaobot_own_session_ids,
    classify_session,
)
from ciao.import_sources import (
    DISCOVERY_MAX_COUNT,
    KNOWN_PROVIDERS,
    MAX_SESSION_BYTES,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_OPENCODE,
    SUPPORTED_PROVIDERS,
    NormalizedSession,
    SourceError,
    SourceRef,
    discover_opencode_sessions,
)
from ciao.import_sources.claude_code import (
    discover_claude_code_sessions,
    read_claude_code_session,
)
from ciao.import_sources.opencode import read_opencode_session
from ciao.workspaces import agent_root_for

logger = logging.getLogger(__name__)


#: The most sessions one source contributes to a listing. A bound on what a
#: single listing may hold, not a claim about how many exist: reaching it sets
#: :attr:`DiscoveryResult.truncated` for that provider, so the consent screen can
#: say the list is a page rather than a whole history. OpenCode's own bound is
#: :data:`~ciao.import_sources.opencode.DISCOVERY_MAX_COUNT`, which the CLI
#: applies without a cursor; this cap is the one that covers a Claude Code slug
#: directory holding years of sessions.
MAX_SESSIONS_PER_SOURCE = 500

#: The most refs one preview may summarize. A selection is a person's decision
#: about a handful of conversations, and the preview reads each one it is given,
#: so the count is bounded rather than trusted.
MAX_SELECTION = 50

#: The most conversations one import batch may process (#975's per-batch cap).
#: Stated here because the consent screen has to show the cap *before* the batch
#: runs, and the batch itself is C6's.
BATCH_CAP = 10

# ── Why a discovered conversation is not offered ───────────────────────────
#
# A closed vocabulary, so the consent screen is written against the list rather
# than against strings a caller invents.

#: Ciaobot created or drove this session. Decided from Ciaobot's own records, so
#: it is known before the file is opened — the one refusal discovery makes.
REASON_CIAOBOT_OWN = "ciaobot_own"
#: Not readable as a plain file: a symlink, a directory, a vanished session, or a
#: file this process cannot stat.
REASON_UNREADABLE = "unreadable"
#: Larger than :data:`~ciao.import_sources.contract.MAX_SESSION_BYTES`, so one
#: bounded read cannot hold it.
REASON_OVER_CAP = "over_cap"
#: Named a source no adapter can read (a Claude account export, whose format the
#: feasibility report could not verify).
REASON_NO_ADAPTER = "no_adapter"

#: Every reason a discovered conversation can be excluded, in reporting order.
EXCLUSION_REASONS: tuple[str, ...] = (
    REASON_CIAOBOT_OWN,
    REASON_UNREADABLE,
    REASON_OVER_CAP,
)


class UnknownWorkspace(ValueError):
    """The named workspace is not registered, so nothing can be scanned for it."""


@dataclass(frozen=True, slots=True)
class ExcludedSource:
    """One discovered conversation that is never offered, and why.

    The ``(SourceRef, reason)`` pair a consent screen shows as a non-selectable
    row. ``message`` is the sentence a person reads; ``reason`` is the closed
    value a caller branches on, so the two cannot drift apart by rewording.
    """

    ref: SourceRef
    reason: str
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"ref": self.ref.to_json(), "reason": self.reason, "message": self.message}


@dataclass(frozen=True, slots=True)
class UnsupportedSource:
    """One source that could not be listed at all.

    A row rather than an error, because "OpenCode is too old to read" and "you
    have no OpenCode conversations" are different facts and only the first one is
    true here. ``reason`` is the adapter's own
    :data:`~ciao.import_sources.opencode.SOURCE_ERROR_REASONS` value where the
    source has one, so an importer can tell *why* without parsing the sentence.
    """

    provider: str
    reason: str
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"provider": self.provider, "reason": self.reason, "message": self.message}


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """What one workspace's sources offer, what they refuse, and where a cap bit.

    ``available`` holds :class:`~ciao.import_sources.contract.SourceRef` metadata
    only — an id, a hint, a path, nothing else — and **every row in it is
    undecided**, because discovery reads no conversation and therefore cannot
    apply the third rule of ``classify_session``. See the module docstring.

    ``excluded`` is the other half of the same listing: Ciaobot's own sessions
    and the files this importer refuses to open, each with a reason, so a screen
    can show why a conversation the user can see in their own tool is not
    importable here.

    ``unsupported`` names the sources that could not be listed (an OpenCode below
    the V2 floor, an absent CLI), and ``truncated`` says per provider whether a
    listing cap was reached — a listing is a page, and never a claim to have seen
    everything. Every provider that was considered has an entry, including one
    that could not be listed (its own ``truncated`` is then ``False``: no cap was
    reached, because nothing came back).
    """

    available: tuple[SourceRef, ...] = ()
    excluded: tuple[ExcludedSource, ...] = ()
    unsupported: tuple[UnsupportedSource, ...] = ()
    truncated: Mapping[str, bool] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "available": [ref.to_json() for ref in self.available],
            "excluded": [row.to_json() for row in self.excluded],
            "unsupported": [row.to_json() for row in self.unsupported],
            "truncated": dict(self.truncated),
        }


# ── Discovery ──────────────────────────────────────────────────────────────


def discover_sources(
    config: CiaoConfig,
    workspace: str,
    *,
    sources: Sequence[str] | None = None,
) -> DiscoveryResult:
    """Every conversation this workspace's known sources hold, as metadata.

    ``workspace`` is a **registered** workspace name; an unknown or empty one
    raises :class:`UnknownWorkspace` rather than falling back to the install
    root, because a scan that silently answered for a different workspace is a
    consent screen offering the wrong history.

    ``sources`` narrows which providers to list and defaults to every provider
    the contract supports today (``claude_code`` and ``opencode``). A name the
    contract knows but no adapter reads — ``claude_account`` — is reported in
    :attr:`DiscoveryResult.unsupported`; a name it does not know at all is a
    :class:`ValueError`, so a typo cannot mint a source.

    Nothing is written, no model is called, and no conversation is opened: each
    candidate is ``lstat``-ed for its size and refused if it is a link, a
    directory or over
    :data:`~ciao.import_sources.contract.MAX_SESSION_BYTES`. Rows Ciaobot itself
    owns are excluded **here**, from ``ciaobot_own_session_ids(config, workspace)``,
    before anything reads them.

    Raises :class:`~ciao.import_decouple.RegistrySnapshotError` when Ciaobot's own
    registry cannot be read. An empty exclusion set obtained from a corrupt
    registry would offer Ciaobot's own sessions as the user's history, which is
    the one failure this function exists to prevent, so the scan is refused
    instead of degraded.
    """
    name = str(workspace or "").strip()
    if not name or config.workspace(name) is None:
        raise UnknownWorkspace(
            f"unknown workspace {workspace!r}: expected a registered workspace name"
        )
    wanted = _resolve_sources(sources)
    known_own = ciaobot_own_session_ids(config, name)
    root = agent_root_for(config, name)

    available: list[SourceRef] = []
    excluded: list[ExcludedSource] = []
    unsupported: list[UnsupportedSource] = []
    truncated: dict[str, bool] = {}

    for provider in wanted:
        if provider == PROVIDER_CLAUDE_CODE:
            found, capped = _list_claude_code(root)
        elif provider == PROVIDER_OPENCODE:
            found, capped, refusal = _list_opencode(root)
            if refusal is not None:
                unsupported.append(refusal)
                # No cap was reached because nothing was listed; the row in
                # `unsupported` is what a screen shows for this provider.
                truncated[provider] = False
                continue
        else:
            unsupported.append(
                UnsupportedSource(
                    provider=provider,
                    reason=REASON_NO_ADAPTER,
                    message=(
                        f"{provider} has no reader yet; a Claude account export "
                        "cannot be imported until its format is verified."
                    ),
                )
            )
            truncated[provider] = False
            continue
        truncated[provider] = capped
        for ref in found:
            # Ciaobot's own first: this is the one refusal that can be made
            # without opening the file, so it is made before the file is stat-ed
            # for a size.
            if _classify(ref, known_own) == CIAOBOT_OWN:
                excluded.append(
                    ExcludedSource(
                        ref=ref,
                        reason=REASON_CIAOBOT_OWN,
                        message="Created or driven by Ciaobot; it is not your own history.",
                    )
                )
                continue
            unreadable = _refusal(ref)
            if unreadable is not None:
                excluded.append(unreadable)
                continue
            available.append(ref)

    return DiscoveryResult(
        available=tuple(available),
        excluded=tuple(excluded),
        unsupported=tuple(unsupported),
        truncated=truncated,
    )


def classify_for_import(
    config: CiaoConfig,
    workspace: str,
    ref: SourceRef,
    *,
    first_user_turn: str = "",
) -> SessionClassification:
    """Whether ``ref`` is Ciaobot's own, external, or not decided.

    A thin wrapper over :func:`ciao.import_decouple.classify_session` whose
    ``known_ids`` is always Ciaobot's own records for this workspace, so a caller
    cannot pass a weaker answer than the engine's.

    ``first_user_turn`` is the session's own opening turn, and it defaults to
    nothing — which is what a metadata-only caller has. The empty default is not
    a shortcut past the rule: ``classify_session`` answers ``ambiguous`` without
    a readable turn rather than ``external``, so the direction a caller that
    passed nothing fails in is the one that refuses to import. Pass the real turn
    (as :func:`preview_selected` does, having read it) to get a decision.

    The two Ciaobot-owned records are read per call, which is right for a caller
    with one session to decide and wasteful for a listing: :func:`discover_sources`
    reads them once and classifies every row against that one answer.
    """
    return _classify(ref, ciaobot_own_session_ids(config, workspace), first_user_turn)


def _classify(
    ref: SourceRef,
    known_own: Collection[tuple[str, str]],
    first_user_turn: str = "",
) -> SessionClassification:
    """:func:`ciao.import_decouple.classify_session` with an already-read set."""
    return classify_session(ref.provider, ref.source_id, first_user_turn, known_own)


def _resolve_sources(sources: Sequence[str] | None) -> tuple[str, ...]:
    """The providers to list, in the contract's own order, de-duplicated.

    A name the contract knows but no adapter reads (``claude_account``) is kept:
    it becomes an ``UnsupportedSource`` row, which is a fact a screen can show.
    Dropping it would report it as "nothing found" instead.
    """
    wanted = tuple(sources) if sources is not None else SUPPORTED_PROVIDERS
    unknown = [provider for provider in wanted if provider not in KNOWN_PROVIDERS]
    if unknown:
        raise ValueError(
            f"unknown import source(s) {unknown!r}; expected some of {KNOWN_PROVIDERS}"
        )
    chosen = set(wanted)
    return tuple(provider for provider in KNOWN_PROVIDERS if provider in chosen)


def _list_claude_code(root: Path) -> tuple[list[SourceRef], bool]:
    """The Claude Code sessions under this workspace's slug directory.

    One directory, named by :func:`ciao.agent_paths.claude_projects_dir` for the
    workspace's own agent root, and nothing above it: no glob over
    ``~/.claude/projects``, so another workspace's sessions cannot appear here.
    A missing directory is the ordinary case (nobody ran Claude Code here yet)
    and is an empty listing, not a failure.

    The returned flag says the cap cut the listing short.
    """
    directory = claude_projects_dir(root)
    found = discover_claude_code_sessions(directory)
    if len(found) <= MAX_SESSIONS_PER_SOURCE:
        return found, False
    logger.warning(
        "import discovery: %s holds %d Claude Code sessions, past the %d cap; "
        "some are not listed (the page is by session id, not by recency).",
        directory,
        len(found),
        MAX_SESSIONS_PER_SOURCE,
    )
    # Deterministic and reproducible: the adapter sorts by session id, so the
    # same listing always yields the same page.
    return found[:MAX_SESSIONS_PER_SOURCE], True


def _list_opencode(root: Path) -> tuple[list[SourceRef], bool, UnsupportedSource | None]:
    """The OpenCode root sessions for this workspace's project directory.

    ``session list`` resolves its project from ``process.cwd()`` and returns one
    page with no cursor, so a project holding more root sessions than the cap
    holds sessions this listing cannot see; a full page is reported as one
    rather than presented as the whole history.

    Every failure the adapter can report — no CLI, an unsupported version, a
    command that failed or timed out — comes back as an
    :class:`UnsupportedSource` rather than an error, because a listing that could
    not be fetched has no business claiming it found nothing.
    """
    try:
        found = discover_opencode_sessions(root)
    except SourceError as exc:
        logger.info("import discovery: opencode listing refused (%s)", exc.reason)
        return [], False, UnsupportedSource(
            provider=PROVIDER_OPENCODE, reason=exc.reason, message=str(exc)
        )
    return found, len(found) >= DISCOVERY_MAX_COUNT, None


def _refusal(ref: SourceRef) -> ExcludedSource | None:
    """Why this file is not offered, or ``None`` when it may be.

    The check is on the path's own inode, taken with ``lstat`` so a link is seen
    as a link: a symlink under a Claude Code slug is not a file Claude Code wrote
    there, and following it would let anything on the machine be presented as a
    conversation. A file over
    :data:`~ciao.import_sources.contract.MAX_SESSION_BYTES` is refused here for
    the same reason the reader stops at the cap — one bounded read cannot hold
    it — rather than offered and truncated later.

    A source with no file behind it (an OpenCode session lives in a server's
    database) is not stat-ed at all.
    """
    if not ref.path:
        return None
    path = Path(ref.path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ExcludedSource(
            ref=ref,
            reason=REASON_UNREADABLE,
            message="The file is gone; the listing is older than the disk.",
        )
    except OSError as exc:
        return ExcludedSource(
            ref=ref, reason=REASON_UNREADABLE, message=f"Cannot read it: {exc}"
        )
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return ExcludedSource(
            ref=ref,
            reason=REASON_UNREADABLE,
            message="Not a plain file, so it is not read.",
        )
    if info.st_size > MAX_SESSION_BYTES:
        return ExcludedSource(
            ref=ref,
            reason=REASON_OVER_CAP,
            message=(
                f"{info.st_size} bytes, past the {MAX_SESSION_BYTES}-byte bound "
                "one import read may hold."
            ),
        )
    return None


# ── The selected half: what will be processed, stated before anything runs ──

#: A preview row's state. ``ready`` is the only one a selection may contain, and
#: it means "classified external and read"; the other two are refusals a screen
#: shows rather than hides.
STATE_READY = "ready"
STATE_EXCLUDED = "excluded"
STATE_UNREADABLE = "unreadable"
PREVIEW_STATES: tuple[str, ...] = (STATE_READY, STATE_EXCLUDED, STATE_UNREADABLE)


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """One selected conversation, described without any of its text.

    ``message_count`` and ``estimated_chars`` are what an input-volume estimate
    is made of; ``first_date`` is the source's own date for the first turn it
    carries, and is empty when the source has none — the contract forbids
    inventing one, so a Claude Code transcript has none and says so rather than
    reaching for a file mtime.

    ``omitted`` is the reader's own per-kind record, so the screen can say what
    a later extraction will not see.

    ``already_imported`` is C6's answer (the batch store's per-source digest);
    nothing in C5 knows it, so it is ``False`` here rather than guessed at.
    """

    ref: SourceRef
    state: str
    classification: SessionClassification = AMBIGUOUS
    message_count: int = 0
    estimated_chars: int = 0
    first_date: str = ""
    omitted: Mapping[str, int] = field(default_factory=dict)
    truncated: bool = False
    already_imported: bool = False
    reason: str = ""
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.ref.to_json(),
            "state": self.state,
            "classification": self.classification,
            "message_count": self.message_count,
            "estimated_chars": self.estimated_chars,
            "first_date": self.first_date,
            "omitted": dict(self.omitted),
            "truncated": self.truncated,
            "already_imported": self.already_imported,
            "reason": self.reason,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """What a selection would process, and on whose account.

    This is the confirmation payload a person reads **before** any model call:
    the exact rows (:attr:`conversations`), the provider and model that would
    receive the text (:attr:`provider`, :attr:`model`), the input-volume
    estimate (:attr:`estimated_chars`, :attr:`estimated_messages`), the per-batch
    cap (:attr:`batch_cap`) and the destination (:attr:`destination`).

    ``provider`` and ``model`` are resolved from the configuration through
    ``CiaoConfig.default_provider_for_workspace`` and
    ``default_model_for_workspace`` — the same answers a new chat gets — so the
    screen cannot promise one provider and run another.
    """

    workspace: str
    conversations: tuple[ConversationSummary, ...] = ()
    provider: str = ""
    model: str = ""
    estimated_chars: int = 0
    estimated_messages: int = 0
    batch_cap: int = BATCH_CAP
    destination: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "conversations": [row.to_json() for row in self.conversations],
            "provider": self.provider,
            "model": self.model,
            "estimated_chars": self.estimated_chars,
            "estimated_messages": self.estimated_messages,
            "batch_cap": self.batch_cap,
            "destination": self.destination,
        }


def preview_selected(
    config: CiaoConfig,
    workspace: str,
    refs: Collection[SourceRef],
) -> ImportPreview:
    """Describe the selected conversations, reading only those.

    This is the first function in this module that opens a conversation, and it
    is the whole reason the boundary is drawn here: discovery listed metadata,
    a person chose from that list, and only the chosen refs are read. Each is
    resolved from **this workspace's configured root**, never from a path a
    caller supplied, and is re-checked with the same link, type and size rules
    :func:`_refusal` applies — the selection carries an id, not a location.

    An OpenCode id is additionally required to be one this workspace's own
    listing named, because ``opencode session export <id>`` resolves ids across
    projects: without that check a hand-built POST could name another project's
    conversation and have it summarized as though it belonged here. The listing
    runs **once** per preview, and not at all when no OpenCode ref is selected.

    A row is summarized only when it is ``external``. Ciaobot's own sessions are
    refused before the file is opened (their recorded ids decide it), and one
    whose own opening turn makes it Ciaobot's own or unreadable is refused after,
    with the reason the screen shows. Nothing here runs a model, writes a
    proposal, or returns a single character of a conversation.
    """
    name = str(workspace or "").strip()
    if not name or config.workspace(name) is None:
        raise UnknownWorkspace(
            f"unknown workspace {workspace!r}: expected a registered workspace name"
        )
    selected = tuple(refs)
    if len(selected) > MAX_SELECTION:
        raise ValueError(
            f"a preview covers at most {MAX_SELECTION} conversations, got {len(selected)}"
        )
    root = agent_root_for(config, name)
    # One read of Ciaobot's own records for the whole listing: the exclusion set
    # is the same answer for every row, and a slug directory can hold hundreds.
    known_own = ciaobot_own_session_ids(config, name)
    # ...and one OpenCode listing for the whole preview, for the same reason.
    opencode_ids = _opencode_membership(selected, root)

    summaries: list[ConversationSummary] = []
    for ref in selected:
        summaries.append(_summarize(ref, root, known_own, opencode_ids))

    ready = [row for row in summaries if row.state == STATE_READY]
    provider = config.default_provider_for_workspace(name)
    return ImportPreview(
        workspace=name,
        conversations=tuple(summaries),
        provider=provider,
        model=config.default_model_for_workspace(name, provider),
        estimated_chars=sum(row.estimated_chars for row in ready),
        estimated_messages=sum(row.message_count for row in ready),
        destination=str(config.workspace_vault_root(name)),
    )


def _summarize(
    ref: SourceRef,
    root: Path,
    known_own: Collection[tuple[str, str]],
    opencode_ids: frozenset[str] | SourceError,
) -> ConversationSummary:
    """One selected conversation, described; every refusal becomes a row."""
    # Ciaobot's own, before the file is opened: a recorded id is the one answer
    # that needs no content, and reading a session Ciaobot drove to discover it
    # is not what this module is for.
    if _classify(ref, known_own) == CIAOBOT_OWN:
        return ConversationSummary(
            ref=ref,
            state=STATE_EXCLUDED,
            classification=CIAOBOT_OWN,
            reason=REASON_CIAOBOT_OWN,
            message="Created or driven by Ciaobot; it is not your own history.",
        )

    if ref.provider == PROVIDER_CLAUDE_CODE:
        return _summarize_claude_code(ref, root, known_own)
    if ref.provider == PROVIDER_OPENCODE:
        return _summarize_opencode(ref, known_own, opencode_ids)
    return ConversationSummary(
        ref=ref,
        state=STATE_EXCLUDED,
        reason=REASON_NO_ADAPTER,
        message=f"{ref.provider} has no reader yet.",
    )


def _opencode_membership(
    selected: Sequence[SourceRef], root: Path
) -> frozenset[str] | SourceError:
    """Which OpenCode sessions this workspace's own listing names for a preview.

    One bounded ``session list`` for the whole selection, and **none at all**
    when no OpenCode ref was selected — a Claude Code preview has nothing to
    check, so it does not spend a subprocess on the question.

    A listing that could not be fetched returns the adapter's
    :class:`~ciao.import_sources.opencode.SourceError` instead of an empty set,
    so the caller can refuse with the reason it was refused rather than with a
    claim that the session does not exist: "OpenCode could not be listed" and
    "this is not one of your sessions" are different facts.
    """
    if not any(ref.provider == PROVIDER_OPENCODE for ref in selected):
        return frozenset()
    try:
        return frozenset(ref.source_id for ref in discover_opencode_sessions(root))
    except SourceError as exc:
        logger.info("import preview: opencode listing refused (%s)", exc.reason)
        return exc


def _summarize_claude_code(
    ref: SourceRef, root: Path, known_own: Collection[tuple[str, str]]
) -> ConversationSummary:
    """One Claude Code session, resolved from the configured slug directory."""
    resolved = _resolve_claude_code_ref(root, ref)
    if isinstance(resolved, ConversationSummary):
        return resolved
    session = read_claude_code_session(resolved)
    return _session_summary(session, ref, known_own)


def _summarize_opencode(
    ref: SourceRef,
    known_own: Collection[tuple[str, str]],
    workspace_ids: frozenset[str] | SourceError,
) -> ConversationSummary:
    """One OpenCode session, exported through the supported V2 CLI.

    Only an id this workspace's own listing named is exported.
    ``opencode session export <id>`` resolves ids across every project on the
    machine, so an id a caller posts proves nothing about whose conversation it
    is — the listing is the evidence, and it is checked *before* the export, so
    another project's session is never read in order to refuse it. A listing
    that could not be fetched refuses every row with the adapter's own reason,
    because a check that did not run cannot pass.
    """
    if isinstance(workspace_ids, SourceError):
        return ConversationSummary(
            ref=ref,
            state=STATE_UNREADABLE,
            reason=workspace_ids.reason,
            message=str(workspace_ids),
        )
    if ref.source_id not in workspace_ids:
        return ConversationSummary(
            ref=ref,
            state=STATE_UNREADABLE,
            reason=REASON_UNREADABLE,
            message="Not one of this workspace's OpenCode sessions, so it is not read.",
        )
    try:
        session = read_opencode_session(ref.source_id)
    except SourceError as exc:
        return ConversationSummary(
            ref=ref,
            state=STATE_UNREADABLE,
            reason=exc.reason,
            message=str(exc),
        )
    return _session_summary(session, ref, known_own)


def _session_summary(
    session: NormalizedSession,
    ref: SourceRef,
    known_own: Collection[tuple[str, str]],
) -> ConversationSummary:
    """The summary of a read session, or the refusal its own turn decides.

    This is where the third rule of the classification finally applies: the
    session's opening turn has been read, so ``external`` is now a fact rather
    than an absence of evidence, and anything else is refused with the reason a
    screen shows.
    """
    verdict = _classify(ref, known_own, session.first_user_turn)
    if verdict != EXTERNAL:
        reason = REASON_CIAOBOT_OWN if verdict == CIAOBOT_OWN else AMBIGUOUS
        message = (
            "Created or driven by Ciaobot; it is not your own history."
            if verdict == CIAOBOT_OWN
            else "Could not be decided from its own opening turn, so it is not imported."
        )
        return ConversationSummary(
            ref=ref, state=STATE_EXCLUDED, classification=verdict, reason=reason, message=message
        )
    chars = sum(len(message.text) for message in session.messages)
    first_date = ""
    for turn in session.messages:
        # The source's own date, never a file mtime and never import time: a
        # Claude Code transcript carries none, so this stays empty rather than
        # reaching for one.
        stamp = (turn.timestamp or "").strip()[:10]
        if stamp:
            first_date = stamp
            break
    return ConversationSummary(
        ref=ref,
        state=STATE_READY,
        classification=EXTERNAL,
        message_count=len(session.messages),
        estimated_chars=chars,
        first_date=first_date,
        omitted=session.omission_counts(),
        truncated=session.truncated,
    )


def _resolve_claude_code_ref(
    root: Path, ref: SourceRef
) -> Path | ConversationSummary:
    """The one file this id may name, or the refusal of it.

    The selection carries a session id, and the path is rebuilt from the
    workspace's own slug directory rather than taken from the ref: a caller that
    could name a path could name any path. ``source_id`` is therefore checked to
    be a plain file name before it is joined, so a ``..``, a separator or an
    absolute path is refused rather than resolved.
    """
    name = ref.source_id.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or os.sep in name:
        return ConversationSummary(
            ref=ref,
            state=STATE_UNREADABLE,
            reason=REASON_UNREADABLE,
            message="Not a session name this importer can resolve.",
        )
    path = claude_projects_dir(root) / f"{name}.jsonl"
    refusal = _refusal(SourceRef(ref.provider, ref.source_id, ref.project_hint, str(path)))
    if refusal is not None:
        return ConversationSummary(
            ref=ref,
            state=STATE_UNREADABLE,
            reason=refusal.reason,
            message=refusal.message,
        )
    return path