"""Which provider sessions are Ciaobot's own, and which facts may cite one.

A conversation scanner on the engine host sees one flat pile of provider
sessions: the user's own standalone history, and the sessions Ciaobot itself
created or drove (its chats, the memory pass, proposal helpers, subagents,
forked lineages, prompt-scaffolded turns). Matching on a chat row's
``session_id`` + ``previous_session_ids`` — the "Ciaobot session identity"
#980 named — is necessary and **not sufficient**, for two reasons this module
exists to make explicit:

1. Reclaiming a provider session is **fail-open**. ``archive_chat`` and
   ``delete_chat`` both schedule ``_reclaim_provider_sessions_async``
   (``ciao/web/project_chats.py``), which logs and continues when the provider
   is unavailable. A session that *should* have been reclaimed can still be on
   disk. The idle sweep does not help: ``reap_idle_providers`` only
   *disconnects* a provider idle past ``_PROVIDER_IDLE_TIMEOUT_SECONDS``,
   leaving the chat row, its ``session_id`` and its file untouched. So "the
   chat is archived, therefore the file is gone" is not an assumption this
   module is allowed to make — the archived and deleted-but-recorded rows stay
   in the exclusion set precisely because the file may not have gone.
2. Ciaobot drives sessions it keeps **no row** for (an OpenCode child session,
   a helper whose row is gone). A recorded id cannot see those; evidence in the
   session itself can.

So this module states three rules, and only these three.

**The exclusion set** (:func:`ciaobot_own_session_ids`) is built from Ciaobot's
own registry and state on the engine host — ``<runtime>/web_projects.json`` and
``<runtime>/state.json``, the same two files the managers own — and never from
any provider's own storage. It covers every chat row whatever its lifecycle
(live, archived, helper) because ``_archive_chat_unlocked`` deliberately keeps
``session_id`` on an archived row, and every context the state store still
holds, which is the copy that survives a chat row whose deletion landed in the
registry but not in the state file.

**The classification** (:func:`classify_session`) answers one question per
discovered session and is allowed three answers: ``ciaobot_own`` (a recorded
id, a Ciaobot chat id, or a Ciaobot marker in the session's own first user
turn), ``external``, and ``ambiguous``. ``ambiguous`` means *not read well
enough to decide*, and it is the answer a caller must refuse to import — the
rule is that a scan never guesses its way past it.

**The provenance rule** (:func:`assert_external_provenance`) is the durable
half: every imported fact names its **external** source as
``provider:session_id:anchor``, and a tag naming a Ciaobot chat id or a
recorded Ciaobot-own session is refused rather than filed.

**Mixed sessions.** Once Ciaobot has used a provider session, it holds Ciaobot's
prompt scaffolding, injected context, memory-pass output and compaction
summaries, so it no longer cleanly represents the user's standalone usage. The
policy is :data:`MIXED_SESSION_POLICY` — **exclude the whole session**, never
import the turns that look like the user's. A per-turn carve-out needs a
reliable "was this turn typed by the user" signal inside a foreign file, and
the only such signal Ciaobot has (``user_turn_unattended``) lives in its own
registry, not in the session.

Reads no operator history: no ``~/.claude``, no ``~/.opencode``, no account
export, no provider session listing. It never starts an engine or a model
turn. Every helper here is pure over an injected snapshot except
:func:`ciaobot_own_session_ids`, which reads the two Ciaobot-owned files and
nothing else.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from ciao.config import CiaoConfig
from ciao.provider_registry import provider_ids

logger = logging.getLogger(__name__)


# ── Vocabulary ────────────────────────────────────────────────────────────

SessionClassification = Literal["ciaobot_own", "external", "ambiguous"]

CIAOBOT_OWN: SessionClassification = "ciaobot_own"
EXTERNAL: SessionClassification = "external"
AMBIGUOUS: SessionClassification = "ambiguous"

#: The chat registry the PWA managers own. ``ProjectChatManager`` is
#: constructed with this path in ``ciao/main.py``, so it is *the* registry, not
#: a second store that has to be kept in step with it.
REGISTRY_FILENAME = "web_projects.json"

#: The markers wrapping the capsule Ciaobot prepends to every prompt it seeds a
#: provider session with (``ProjectChatManager._build_prompt_prefix`` for Claude,
#: ``OpencodeProvider``'s text rewrite for opencode). It is the only evidence in
#: the session itself that Ciaobot drove it, and it is the same literal the
#: transcript renderers strip before showing history.
CIAO_CONTEXT_BEGIN = "[CIAO_CONTEXT_BEGIN]"
CIAO_CONTEXT_END = "[CIAO_CONTEXT_END]"

#: Ciaobot mints every chat id as ``chat-<8 hex>``
#: (``ProjectChatManager.create_chat``). No provider mints a session id of this
#: shape, so a discovered session carrying one is Ciaobot bookkeeping rather
#: than a conversation — the exact ``source_chat_id`` overload the feasibility
#: report's dedupe section rules out.
CIAOBOT_CHAT_ID_RE = re.compile(r"^chat-[0-9a-f]{8}$")

#: One provider, two vocabularies. Ciaobot's registry and state store record
#: ``claude``/``opencode`` (``ciao.provider_registry.provider_ids``), while an
#: importer's source adapter reports the very same session as ``claude_code``
#: (the feasibility report's ``SourceRef.provider``). A literal comparison would
#: miss every Ciaobot-own Claude session and classify it as the user's history,
#: which is the one failure this module exists to prevent — so both sides of a
#: lookup go through :func:`canonical_provider`.
_PROVIDER_ALIASES = {"claude_code": "claude"}


def canonical_provider(provider: str) -> str:
    """The provider id Ciaobot's own records use for ``provider``.

    Unmapped names are returned as they are: a provider Ciaobot has no record
    for cannot be excluded either way, and inventing an id for it would drop a
    Ciaobot-own session out of the set rather than keep it.
    """
    text = provider.strip()
    return _PROVIDER_ALIASES.get(text, text)


MIXED_SESSION_POLICY = "exclude_whole_session"
"""What an importer does with a session Ciaobot has also used.

``exclude_whole_session``, never a per-turn carve-out. See the module
docstring; the short form is that the only "was this turn the user's" signal
Ciaobot keeps is in its own registry, so a mixed foreign file cannot be split
honestly and a partial import is indistinguishable from an import of Ciaobot's
own work.
"""


class ProvenanceNotExternal(ValueError):
    """A provenance tag named Ciaobot's own work, so it cannot be a source."""


class RegistrySnapshotError(RuntimeError):
    """Ciaobot's own record could not be read, so nothing could be excluded.

    Raised rather than degraded to an empty exclusion set: an import that
    cannot see Ciaobot's registry would classify Ciaobot's own sessions as the
    user's history, which is the failure this module exists to prevent.
    """


# ── The exclusion set ─────────────────────────────────────────────────────


def has_ciaobot_marker(text: str) -> bool:
    """Whether a session's own text carries Ciaobot's injected-context marker.

    Either half counts. A session holding a stray ``[CIAO_CONTEXT_END]`` with no
    begin marker is malformed rather than clean, and the cheap direction to
    fail in is towards ``ciaobot_own``.
    """
    return CIAO_CONTEXT_BEGIN in text or CIAO_CONTEXT_END in text


def is_ciaobot_chat_id(session_id: str) -> bool:
    """Whether ``session_id`` is a Ciaobot chat id rather than a provider's."""
    return bool(CIAOBOT_CHAT_ID_RE.match(session_id.strip()))


def _row_provider(row: Mapping[str, Any]) -> str:
    """The provider a chat row names, with the migration default applied.

    ``ProjectChatManager._load`` reads a row without a ``provider`` key as
    ``"claude"``; the same default is applied here so a legacy row is excluded
    against the provider it actually ran on.
    """
    raw = row.get("provider")
    provider = raw.strip() if isinstance(raw, str) else ""
    return provider or "claude"


def _row_session_ids(row: Mapping[str, Any]) -> list[str]:
    """Every provider session id one chat row records, oldest first.

    The current ``session_id`` plus ``previous_session_ids`` (the lineage a
    rotation pushes onto, so an autocompact or a resume-failure fork cannot
    hide behind the current id).
    """
    ids: list[str] = []
    current = row.get("session_id")
    if isinstance(current, str) and current.strip():
        ids.append(current.strip())
    previous = row.get("previous_session_ids")
    if isinstance(previous, list):
        for item in previous:
            text = item.strip() if isinstance(item, str) else ""
            if text and text not in ids:
                ids.append(text)
    return ids


def _chat_workspace(
    row: Mapping[str, Any],
    projects: Mapping[str, Any],
    known_workspaces: Collection[str] | None = None,
) -> str:
    """The workspace a chat row belongs to, or ``""`` when unattributable.

    ``""`` means the row cannot be claimed for another workspace: either it has
    no project or its project names none, which is exactly the case
    ``ProjectChatManager._workspace_for_chat`` sends to ``primary_workspace()``.

    A project naming a workspace this install no longer has is that same case:
    the manager sends an unknown name to ``primary_workspace()`` as well. This
    module does not model which workspace is primary — and picking one would
    *drop* the row from the set for every workspace but that one — so an
    unconfigured name is reported as unattributable and the row is kept
    everywhere. ``known_workspaces`` is ``config.workspace_names()``; ``None``
    means the caller has no configured registry to check against (the pure
    snapshot helper), and the name is then taken at face value.
    """
    project_id = row.get("project_id")
    project = projects.get(project_id) if isinstance(project_id, str) else None
    if isinstance(project, Mapping):
        raw = project.get("workspace")
        if isinstance(raw, str) and raw.strip():
            workspace = raw.strip()
            if known_workspaces is None or workspace in known_workspaces:
                return workspace
    return ""


def own_session_ids_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    workspace: str = "",
    known_workspaces: Collection[str] | None = None,
) -> set[tuple[str, str]]:
    """``(provider, session_id)`` pairs Ciaobot owns, from a chat-registry snapshot.

    Pure over the injected snapshot — the payload
    ``ProjectChatManager._state_payload`` writes — so it is unit-testable with a
    literal dict and no engine, no filesystem and no running install.

    Every chat row counts whatever its lifecycle. A live chat and an archived
    one are scanned identically on purpose: ``_archive_chat_unlocked`` keeps
    ``session_id`` on the archived row precisely so the row keeps naming the
    session it reclaimed, and reclaim is fail-open, so an archived row is the
    evidence that a still-present file is Ciaobot's. Helper chats (the hidden
    Memory project's pass, a proposal helper, an update-task chat) are ordinary
    rows in that registry and carry their own ``session_id``, so there is no
    second helper list to keep in step with the first.

    ``workspace`` narrows to one logical workspace. A row whose workspace cannot
    be resolved is kept for **every** workspace: it is the fallback case, and
    excluding it from a workspace it may well belong to is the direction that
    would re-ingest Ciaobot's own work. ``known_workspaces`` is the install's
    configured workspace names (see :func:`_chat_workspace`); a caller without
    them cannot tell an unconfigured workspace from a live one.
    """
    projects = snapshot.get("projects")
    chats = snapshot.get("chats")
    project_rows: Mapping[str, Any] = (
        projects if isinstance(projects, Mapping) else {}
    )
    chat_rows: Mapping[str, Any] = chats if isinstance(chats, Mapping) else {}

    own: set[tuple[str, str]] = set()
    for row in chat_rows.values():
        if not isinstance(row, Mapping):
            continue
        if workspace and _chat_workspace(
            row, project_rows, known_workspaces
        ) not in (
            "",
            workspace,
        ):
            continue
        provider = _row_provider(row)
        for session_id in _row_session_ids(row):
            own.add((provider, session_id))
    return own


def own_session_ids_from_state(
    state_snapshot: Mapping[str, Any],
    providers_by_chat: Mapping[str, str] | None = None,
) -> set[tuple[str, str]]:
    """``(provider, session_id)`` pairs Ciaobot owns, from a state-store snapshot.

    ``.runtime/state.json`` records one provider session per context, and every
    context in it was driven by Ciaobot — this is the copy that outlives a chat
    row, so it is what covers a **deleted** chat whose registry row is gone but
    whose reclaim failed and whose session file is therefore still on disk.
    ``StateStore.delete_context`` writes a different file from the registry's
    ``delete_chat``, so the two can disagree; the disagreement is the case this
    half exists for.

    The state store records **no provider** — one provider-agnostic slot per
    context, filled from whichever provider last ran the chat
    (``chat_streaming``). ``providers_by_chat`` supplies the provider for the
    contexts whose chat row survives; a context with no surviving row is
    excluded against **every** supported provider rather than guessed at one.
    That over-excludes, which is the recoverable direction: an external session
    wrongly withheld is the user's to notice, while an imported Ciaobot turn is
    indistinguishable from the user's own history once filed.

    A ``state.json`` with no ``contexts`` key returns an empty set rather than
    raising: that is a pre-v3 file (v2 filed sessions under
    ``sessions.<provider>.session_id``), and the engine migrates one on load and
    rewrites it as v3, so a file still on disk in the old shape is not a set of
    Ciaobot sessions this module could have read.
    """
    contexts = state_snapshot.get("contexts")
    if not isinstance(contexts, Mapping):
        return set()

    known = providers_by_chat or {}
    own: set[tuple[str, str]] = set()
    for key, entry in contexts.items():
        if not isinstance(entry, Mapping):
            continue
        session = entry.get("session")
        if not isinstance(session, Mapping):
            continue
        raw = session.get("session_id")
        session_id = raw.strip() if isinstance(raw, str) else ""
        if not session_id:
            continue
        provider = known.get(str(key), "")
        if provider:
            own.add((provider, session_id))
            continue
        for candidate in provider_ids():
            own.add((candidate, session_id))
    return own


def _read_snapshot(path: Path) -> dict[str, Any]:
    """Read one Ciaobot-owned JSON record. Absent is empty; unreadable raises."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RegistrySnapshotError(f"Cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RegistrySnapshotError(f"Cannot parse {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistrySnapshotError(f"{path} is not a JSON object")
    return payload


def ciaobot_own_session_ids(
    config: CiaoConfig,
    workspace: str = "",
) -> set[tuple[str, str]]:
    """Every ``(provider, session_id)`` Ciaobot owns, from Ciaobot's own state.

    The one helper that answers "is this ``(provider, session_id)`` Ciaobot's
    own?", covering live chats, archived chats, chats whose row is gone but
    whose state context survives, and helper chats. It reads the two files the
    engine already owns — the chat registry at
    ``<state_path parent>/web_projects.json`` and the state store at
    ``state_path`` — and nothing else: no provider storage, no user history, no
    model.

    ``workspace`` narrows the chat rows (see
    :func:`own_session_ids_from_snapshot`); an empty value is install-wide. The
    narrowing reaches the **state** half too: every live chat has a state
    context, so a snapshot narrowed only over chat rows would still return every
    other workspace's chats. Contexts with no surviving chat row (a deleted
    chat) are kept for every workspace — they are exactly what the state half
    exists for.

    Raises :class:`RegistrySnapshotError` when a record exists but cannot be
    read. A caller that got an empty set from a corrupt registry would scan
    Ciaobot's own sessions as the user's history, so this is refused rather than
    reported as "nothing to exclude".
    """
    state_path = Path(config.state_path)
    registry = _read_snapshot(state_path.parent / REGISTRY_FILENAME)
    state = _read_snapshot(state_path)

    known_workspaces = config.workspace_names()
    projects = registry.get("projects")
    project_rows: Mapping[str, Any] = (
        projects if isinstance(projects, Mapping) else {}
    )
    rows = registry.get("chats")
    chat_rows: Mapping[str, Any] = rows if isinstance(rows, Mapping) else {}

    providers_by_chat = {
        str(chat_id): _row_provider(row)
        for chat_id, row in chat_rows.items()
        if isinstance(row, Mapping)
    }
    own = own_session_ids_from_snapshot(
        registry, workspace=workspace, known_workspaces=known_workspaces
    )

    contexts = state.get("contexts")
    if workspace and isinstance(contexts, Mapping):
        other = {
            str(chat_id)
            for chat_id, row in chat_rows.items()
            if isinstance(row, Mapping)
            and _chat_workspace(row, project_rows, known_workspaces)
            not in ("", workspace)
        }
        if other:
            # A copy, never an in-place edit: the caller's payload stays whole,
            # and a dropped context is dropped for this narrowing only.
            state = {
                **state,
                "contexts": {
                    key: entry
                    for key, entry in contexts.items()
                    if str(key) not in other
                },
            }
    return own | own_session_ids_from_state(state, providers_by_chat)


# ── Classification ────────────────────────────────────────────────────────


def classify_session(
    provider: str,
    session_id: str,
    first_user_turn: str,
    known_ids: Iterable[tuple[str, str]],
) -> SessionClassification:
    """Decide whether a discovered provider session is Ciaobot's own.

    The rules, in order, and none of them is a heuristic that could be argued
    the other way:

    * a recorded id — ``(provider, session_id)`` in ``known_ids`` — is
      ``ciaobot_own``;
    * a session id that is a Ciaobot chat id is ``ciaobot_own``, because no
      provider mints that shape and it is Ciaobot's own bookkeeping either way;
    * a Ciaobot marker in the session's own ``first_user_turn`` is
      ``ciaobot_own``. This is the only rule that can see a session Ciaobot
      drove without keeping a row for it;
    * nothing readable — no session id, or no first user turn to read — is
      ``ambiguous``. Absence of evidence is not evidence of being external;
    * otherwise ``external``.

    ``ambiguous`` is a result, not a failure. The contract for a caller is that
    it must never be imported: "Ciaobot's own session, not imported" and "this
    is not decided" are both refusals, and only the second one is honest when
    the first could not be established.

    ``known_ids`` is an iterable of ``(provider, session_id)`` pairs, as
    :func:`ciaobot_own_session_ids` returns. Both sides of that comparison go
    through :func:`canonical_provider`, so an adapter's ``claude_code`` is
    matched against a Ciaobot record's ``claude``.
    """
    normalized_provider = canonical_provider(provider)
    normalized_session = session_id.strip()
    turn = first_user_turn.strip()

    if (normalized_provider, normalized_session) in {
        (canonical_provider(str(p)), str(s).strip()) for p, s in known_ids
    }:
        return CIAOBOT_OWN
    if normalized_session and is_ciaobot_chat_id(normalized_session):
        return CIAOBOT_OWN
    if has_ciaobot_marker(turn):
        return CIAOBOT_OWN
    if not normalized_session or not turn:
        return AMBIGUOUS
    return EXTERNAL


# ── Provenance ────────────────────────────────────────────────────────────


def assert_external_provenance(
    anchor: str,
    *,
    known_own_ids: Iterable[tuple[str, str]] = (),
    known_chat_ids: Iterable[str] = (),
) -> None:
    """Refuse a provenance tag that does not name an external source.

    The tag shape is ``provider:session_id:anchor`` — the external source and
    the message inside it. This is the durable half of the decoupling: an
    imported fact has to stay attributable to the *user's* conversation after
    it is filed, so it may never cite Ciaobot's own chat id or a recorded
    Ciaobot-own session. :func:`classify_session` decides what may be read;
    this decides what may be written, and a scan that skipped it would file a
    perfectly-attributed fact from the wrong conversation.

    Refused: a blank tag; anything not in the three-part shape (a bare chat id
    is the misuse this exists to catch); a blank provider or session segment;
    a provider or session segment that is a Ciaobot chat id, by shape or by
    ``known_chat_ids``; and a ``(provider, session_id)`` in ``known_own_ids``.

    The keyword-only sets are the caller's own answers, so the check is exact
    rather than shape-only; called without them the shape rules still hold, and
    a Ciaobot chat id is refused either way. The ``known_own_ids`` comparison is
    made through :func:`canonical_provider`, like every other one.
    """
    text = anchor.strip()
    if not text:
        raise ProvenanceNotExternal("A provenance tag must name its source.")

    parts = text.split(":", 2)
    if len(parts) != 3:
        raise ProvenanceNotExternal(
            f"Provenance {text!r} is not 'provider:session_id:anchor'."
        )
    provider, session_id, message_anchor = (part.strip() for part in parts)
    if not provider or not session_id or not message_anchor:
        raise ProvenanceNotExternal(
            f"Provenance {text!r} has an empty provider, session or anchor."
        )
    if is_ciaobot_chat_id(session_id) or session_id in {
        str(chat_id).strip() for chat_id in known_chat_ids
    }:
        raise ProvenanceNotExternal(
            f"Provenance {text!r} names Ciaobot chat id {session_id!r}; "
            "an imported fact's source must be an external session."
        )
    if is_ciaobot_chat_id(provider) or provider in {
        str(chat_id).strip() for chat_id in known_chat_ids
    }:
        raise ProvenanceNotExternal(
            f"Provenance {text!r} names Ciaobot chat id {provider!r} as its "
            "provider; an imported fact's source must be an external session."
        )
    if (canonical_provider(provider), session_id) in {
        (canonical_provider(str(p)), str(s).strip()) for p, s in known_own_ids
    }:
        raise ProvenanceNotExternal(
            f"Provenance {text!r} names Ciaobot's own session "
            f"{session_id!r}; it is not a fact source."
        )
