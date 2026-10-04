"""Run one filed import batch into review proposals, and nothing else.

:mod:`ciao.import_discover` (C5) decides what a machine holds and what a person
consented to, :mod:`ciao.import_store` (C6) remembers the selection, and
:mod:`ciao.import_extract` (C4) owns the extraction contract. Until this module
nothing *drove* them: there was no backend flow that took a selected
conversation through the adapters, ran C4's turn, recorded progress and
provenance, and made the filed proposals reachable in the ordinary review queue.
This is that flow, and it is the only new module in C7.

**The only durable write in this child is C4's ``append_proposals``.** No region,
doc, entity or learning write, no control plane, no chat, no model call of its
own. A person still presses accept, through the existing
:func:`ciao.memory_proposals.accept_region_fact` path, with the fact's own
``[as-of:]`` date — the *source* message's, which C4 appends from the session
and never the import date.

What this module adds is the wiring around that one write:

* :func:`run_import_batch` moves one batch ``queued → running → done | failed |
  partial``, one selected source at a time, recording
  :meth:`~ciao.import_store.ImportStore.record_source`,
  :meth:`~ciao.import_store.ImportStore.record_progress` and
  :meth:`~ciao.import_store.ImportStore.record_fact_provenance` as it goes;
* each source is resolved through the **C5 adapters**, never through a path a
  caller supplied: ``_resolve_claude_code_ref`` rebuilds the file from the
  workspace's own slug directory and ``read_claude_code_session`` /
  ``read_opencode_session`` do the reading. An OpenCode id must additionally be
  one this workspace's own listing named, because the CLI resolves ids across
  projects;
* C4 is called **exactly as its docstring requires**: with
  ``known_own_ids=ciaobot_own_session_ids(config, workspace)`` and the chat ids
  derived from it, so a Ciaobot-own session is refused by
  :func:`ciao.import_decouple.classify_session` before any turn runs. Left at
  the default ``()`` the check is still a refusal on shape, but a Ciaobot-own
  session whose id does not look like a chat id would not be recognised;
* the model and provider are resolved the way the post-archive memory pass
  resolves its call (``archive_pipeline._insights_model_for`` →
  ``insights._resolve_insights_call``), never from a request body. A caller may
  pass them explicitly — the route never does, and a test must — but the
  configuration is the answer otherwise, which is also the answer the C5 preview
  showed the person before they filed the batch;
* cancellation is checked between sources: a cancelled batch stops extracting,
  keeps every proposal it already filed, and never tries to finish.

**Dedupe is C4's and C6's, not a new one.** The per-fact dedupe is
``append_proposals``' exact-text one (a fact already queued, or already decided,
is not appended again), the batch-level
``(provider, source_id, content_digest, destination, extraction_revision)`` key
is the store's, and ``content_digest`` is recorded here as the SHA-256 of the
normalized session this run read. A changed conversation is a **new attempt**,
never a silent overwrite: the old bullets stay queued and the new ones are
appended beside them.

Reads the batch store, the two adapters, Ciaobot's own two state files (for the
own-id exclusion set) and the destination queue. Writes the batch store and the
queue, and nothing else.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.fact_candidates import external_anchor
from ciao.import_decouple import canonical_provider, ciaobot_own_session_ids
from ciao.import_discover import (
    ConversationSummary,
    _opencode_membership,
    _resolve_claude_code_ref,
)
from ciao.import_extract import PROPOSALS_RELATIVE, ExtractionResult, extract_facts
from ciao.import_sources import (
    PROVIDER_CLAUDE_CODE,
    PROVIDER_OPENCODE,
    NormalizedSession,
    SourceError,
    SourceRef,
    read_opencode_session,
)
from ciao.import_sources.claude_code import read_claude_code_session
from ciao.import_store import (
    CANCELLED,
    CONFLICT,
    DONE,
    FAILED,
    PARTIAL,
    SOURCE_EXTRACTED,
    SOURCE_REFUSED,
    SOURCE_SKIPPED,
    BatchSource,
    FactProvenance,
    ImportBatch,
    ImportStore,
    ImportStoreError,
    engine_store_path,
)
from ciao.insights import _resolve_insights_call, resolve_insights_model
from ciao.memory_proposals import list_proposals
from ciao.workspaces import agent_root_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportRunResult:
    """What one batch run did, counted on the store rather than inferred.

    Every count here is a fact the C6 store also holds, so a caller can compare
    the two and a test can pin one against the other. ``status`` is the batch's
    settled state — ``done``, ``partial``, ``failed``, or ``cancelled`` when a
    cancel landed mid-run, in which case the batch is already terminal and this
    run never finished it.

    ``cancelled`` is separate from ``status`` because the two are different
    events: a run can be cancelled and still have filed everything it had
    extracted so far, which is the behaviour the store's cancellation promises.
    """

    batch_id: str
    status: str
    sources_extracted: int = 0
    sources_skipped: int = 0
    sources_refused: int = 0
    proposals_filed: int = 0
    skipped: int = 0
    cancelled: bool = False
    error: str = ""


def batch_store(config: CiaoConfig) -> ImportStore:
    """The engine's batch store: ``<runtime>/import/import-batches.json``.

    The same construction :func:`ciao.web.routes_import.import_batch_store`
    does, through the one place that path is built
    (:func:`ciao.import_store.engine_store_path`), so the runner and the routes
    cannot drift onto two different files.
    """
    return ImportStore(engine_store_path(config))


def _content_digest(session: NormalizedSession) -> str:
    """The SHA-256 of one normalized session, as the store's key records it.

    Over the session's own snapshot rather than the source file's bytes: two
    reads of the same conversation agree, and a file whose formatting differs
    but whose turns do not is not a different conversation. The digest is
    recorded, never compared here — C6 owns that comparison.
    """
    payload = json.dumps(session.to_json(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_call(
    config: CiaoConfig,
    *,
    workspace: str,
    source_provider: str,
    model: str | None,
    provider: str | None,
) -> tuple[str, str]:
    """``(model, provider)`` for one source's turn, from configuration.

    Exactly the two answers the post-archive memory pass resolves its own call
    with (``ciao/web/memory_pass.py``): the per-provider insights override when
    the operator set one, else :func:`ciao.insights.resolve_insights_model`,
    then :func:`ciao.insights._resolve_insights_call` to turn a routed
    ``opencode:`` prefix into its provider. The provider the override is keyed
    by is the **source's**, canonicalised (``claude_code`` → ``claude``), so an
    imported OpenCode conversation is extracted by the OpenCode insights model
    and a Claude Code one by Claude's — the same rule
    ``archive_pipeline._insights_model_for`` applies to a chat's own provider.
    It is resolved per source for that reason: a batch may select a Claude Code
    and an OpenCode conversation at once, and they are read by different models.

    ``model``/``provider`` are explicit overrides for a caller that already
    knows (a test, a future CLI verb). No HTTP route passes them: a client
    choosing the model that reads a user's history would be a capability this
    child does not have.
    """
    canonical = canonical_provider(source_provider)
    wanted_model = model
    if wanted_model is None:
        overrides = getattr(config, "provider_insights_models", {}) or {}
        wanted_model = overrides.get(canonical, "") or resolve_insights_model(
            config, workspace, canonical
        )
    effective_model, routed_provider, _note = _resolve_insights_call(
        config,
        wanted_model,
        provider=provider if provider is not None else canonical,
    )
    # An explicit provider wins over the routing one: the caller asked for that
    # runner, and `_resolve_insights_call` only ever *adds* one for an
    # `opencode:`-prefixed model.
    return effective_model, (provider if provider is not None else routed_provider)


def _read_source(
    config: CiaoConfig,
    workspace: str,
    source: BatchSource,
    *,
    opencode_ids: frozenset[str] | SourceError,
) -> tuple[NormalizedSession | None, str]:
    """``(session, why)`` for one selected source.

    ``(None, reason)`` is every refusal, and a refusal is a result rather than an
    exception: a link, a vanished file, a session over the byte cap, an OpenCode
    id this workspace's own listing never named, or a provider with no reader.
    The resolution is C5's own, imported rather than reimplemented — a second
    copy of "which file may this id name" is how the two could disagree about a
    path, which is the failure the consent boundary exists to prevent.
    """
    ref = SourceRef(provider=source.provider, source_id=source.source_id)
    root = agent_root_for(config, workspace)
    if source.provider == PROVIDER_CLAUDE_CODE:
        resolved = _resolve_claude_code_ref(root, ref)
        if isinstance(resolved, ConversationSummary):
            return None, resolved.message or resolved.reason
        try:
            return read_claude_code_session(resolved), ""
        except (SourceError, OSError) as exc:
            return None, str(exc)
    if source.provider == PROVIDER_OPENCODE:
        if isinstance(opencode_ids, SourceError):
            return None, str(opencode_ids)
        if ref.source_id not in opencode_ids:
            return None, (
                "Not one of this workspace's OpenCode sessions, so it is not read."
            )
        try:
            return read_opencode_session(ref.source_id), ""
        except SourceError as exc:
            return None, str(exc)
    return None, f"{source.provider} has no reader yet."


def _known_chat_ids(known_own: frozenset[tuple[str, str]]) -> frozenset[str]:
    """The chat ids derived from Ciaobot's own session ids.

    ``extract_facts`` takes two caller answers: the ``(provider, session_id)``
    pairs and the chat ids. The second is exactly the session-id half of the
    first — the ids Ciaobot drove — because that is what a chat-id refusal needs
    to catch an id whose shape alone would not look like ``chat-<8 hex>``. It is
    derived here rather than collected twice, so the two answers can never
    disagree about what Ciaobot owns.
    """
    return frozenset(session_id for _provider, session_id in known_own)


def _queue_rows(vault_root: Path) -> dict[str, dict[str, str]]:
    """The destination queue as ``{text: row}``, for a before/after diff.

    Read off the queue rather than inferred from what the run asked about:
    ``append_proposals`` answers a path for a mixed batch after silently
    dropping the rows the queue already held, so the rows this run actually
    *filed* are only knowable from the queue afterwards. The diff is therefore
    also what decides which anchors get recorded as provenance.
    """
    return {row["text"]: row for row in list_proposals(vault_root / PROPOSALS_RELATIVE)}


def _fact_provenance(row: dict[str, str], *, destination: str) -> FactProvenance | None:
    """The C6 provenance row for one filed bullet, or ``None``.

    The tag comes from the bullet's own ``_(from: …)_`` tail, which C4 built
    from the message the row cites. It is recognised by
    :func:`ciao.fact_candidates.external_anchor` — the same rule the accepted
    fact's receipt is stamped under — so a row whose source is not an external
    three-part tag yields no provenance rather than a guess: the store refuses a
    tag it cannot verify, and dropping one row's evidence beats recording
    evidence nobody checked.
    """
    tag = external_anchor(str(row.get("source") or ""))
    if not tag:
        return None
    provider, source_id, anchor = (part.strip() for part in tag.split(":", 2))
    return FactProvenance(
        provider=provider,
        source_id=source_id,
        anchor=anchor,
        destination=destination,
        accepted=False,
        note="filed as a review proposal",
    )


async def run_import_batch(
    config: CiaoConfig,
    batch_id: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    store: ImportStore | None = None,
    batch: ImportBatch | None = None,
) -> ImportRunResult:
    """Extract every selected source in ``batch_id`` into the review queue.

    The one entry point C8's route drives. It resolves the batch from the C6
    store, moves it to ``running``, and then walks the selection in the order it
    was filed: read the conversation through C5's adapters, run C4's one
    tool-less turn over it, record the progress and the per-fact provenance, and
    settle the batch. Everything durable it does beyond C4's own write is the
    store's own record.

    Raises :class:`~ciao.import_store.ImportStoreError` when the batch cannot be
    run at all — ``not_found`` for an unknown id, ``conflict`` for one that is
    not ``queued`` (``begin`` refuses), so a route can answer 404/409 without
    catching anything else. Everything *inside* a source is absorbed: one
    unreadable conversation, one refused session or one failing turn costs that
    source, never the batch.

    ``batch`` is the **already-begun** batch a route got back from
    :meth:`~ciao.import_store.ImportStore.begin`. Handing it over is how the
    caller keeps the single-run gate in the request, where the answer to a second
    press can be accurate: the route moves the batch to ``running`` and rejects
    the second press while it is there, and the runner must not then claim it
    again. Without it the runner claims the batch itself. Either way exactly one
    run owns the batch — ``begin`` is the gate and it is not re-entered.

    ``store`` is injectable so a caller (and a test) may own the store object; it
    is built through :func:`batch_store` otherwise.
    """
    runner = store if store is not None else batch_store(config)
    started = (
        batch if batch is not None else await asyncio.to_thread(runner.begin, batch_id)
    )
    batch_id = started.batch_id
    workspace = started.workspace
    destination = started.destination
    vault_root = config.workspace_vault_root(destination)

    known_own = frozenset(ciaobot_own_session_ids(config, workspace))
    known_chat_ids = _known_chat_ids(known_own)
    # One OpenCode listing for the whole batch, and none at all when no OpenCode
    # source was selected — the same economy `preview_selected` keeps.
    opencode_ids = _opencode_membership(
        [
            SourceRef(provider=item.provider, source_id=item.source_id)
            for item in started.sources
            if item.provider == PROVIDER_OPENCODE
        ],
        agent_root_for(config, workspace),
    )

    extracted = 0
    unread = 0
    refused = 0
    filed = 0
    skipped = 0

    for source in started.sources:
        current = await asyncio.to_thread(runner.get, batch_id)
        if current.status == CANCELLED:
            return _cancelled(batch_id, extracted, unread, refused, filed, skipped)
        try:
            await asyncio.to_thread(
                runner.record_progress, batch_id, current_source_id=source.source_id
            )
        except ImportStoreError as exc:
            if exc.code == CONFLICT:
                return _cancelled(batch_id, extracted, unread, refused, filed, skipped)
            raise

        session, why = await asyncio.to_thread(
            _read_source,
            config,
            workspace,
            source,
            opencode_ids=opencode_ids,
        )
        if session is None:
            unread += 1
            skipped += 1
            logger.info(
                "import run: %s session %s is not readable (%s)",
                source.provider,
                source.source_id,
                why,
            )
            await _record_source(
                runner, batch_id, source, SOURCE_SKIPPED, content_digest="", why=why
            )
            await _advance(runner, batch_id, extracted, unread, refused, filed, skipped)
            continue

        digest = _content_digest(session)
        effective_model, effective_provider = _resolve_call(
            config,
            workspace=workspace,
            source_provider=source.provider,
            model=model,
            provider=provider,
        )
        before = _queue_rows(vault_root)
        try:
            result: ExtractionResult = await extract_facts(
                session,
                model=effective_model,
                destination_workspace=vault_root,
                provider=effective_provider,
                known_own_ids=known_own,
                known_chat_ids=known_chat_ids,
            )
        except Exception:  # noqa: BLE001 — one turn must not strand the batch
            logger.exception(
                "import run: extraction failed for %s session %s",
                source.provider,
                source.source_id,
            )
            unread += 1
            skipped += 1
            await _record_source(
                runner,
                batch_id,
                source,
                SOURCE_SKIPPED,
                content_digest=digest,
                why="the extraction turn did not complete",
            )
            await _advance(runner, batch_id, extracted, unread, refused, filed, skipped)
            continue

        if _refused_session(result):
            # C4 refused the session itself — Ciaobot's own, or undecided from
            # its own opening turn — before any turn ran and before anything
            # was read into a prompt. Nothing was filed and nothing may be.
            refused += 1
            skipped += result.skipped
            await _record_source(
                runner,
                batch_id,
                source,
                SOURCE_REFUSED,
                content_digest=digest,
                why=f"refused as Ciaobot's own or undecided: {result.refused_anchor}",
            )
            await _advance(runner, batch_id, extracted, unread, refused, filed, skipped)
            continue

        extracted += 1
        filed += result.proposals_filed
        skipped += result.skipped
        await _record_source(
            runner,
            batch_id,
            source,
            SOURCE_EXTRACTED,
            content_digest=digest,
            why=f"filed {result.proposals_filed} proposal(s)",
        )
        after = _queue_rows(vault_root)
        for text in set(after) - set(before):
            evidence = _fact_provenance(after[text], destination=destination)
            if evidence is None:
                continue
            try:
                await asyncio.to_thread(
                    runner.record_fact_provenance, batch_id, evidence
                )
            except ImportStoreError as exc:
                if exc.code == CONFLICT:
                    return _cancelled(
                        batch_id, extracted, unread, refused, filed, skipped
                    )
                raise
        await _advance(runner, batch_id, extracted, unread, refused, filed, skipped)

    status = DONE if not (unread or refused) else (PARTIAL if extracted else FAILED)
    error = "" if status == DONE else _settle_note(unread, refused)
    try:
        settled = await asyncio.to_thread(runner.finish, batch_id, status, error=error)
    except ImportStoreError as exc:
        if exc.code == CONFLICT:
            # A cancel that landed between the last source and this settle owns
            # the outcome: cancellation is terminal and a cancelled batch is never
            # finished. Everything already filed stays filed.
            return _cancelled(batch_id, extracted, unread, refused, filed, skipped)
        raise
    return ImportRunResult(
        batch_id=batch_id,
        status=settled.status,
        sources_extracted=extracted,
        sources_skipped=unread,
        sources_refused=refused,
        proposals_filed=filed,
        skipped=skipped,
        cancelled=False,
        error=error,
    )


def _refused_session(result: ExtractionResult) -> bool:
    """Whether C4 refused **the session itself** rather than one of its rows.

    :attr:`~ciao.import_extract.ExtractionResult.refused_anchor` names both
    cases by design — the session's own ``source_id`` when
    :func:`ciao.import_decouple.classify_session` refused it before the turn, and
    the first row anchor whose provenance tag
    :func:`~ciao.import_decouple.assert_external_provenance` refused. A runner
    has to tell them apart, because only the second one leaves a conversation
    that was read and partly extracted.

    The discriminator is the usage record, and it is C4's own: the session
    refusal returns before anything is read into a prompt, so
    ``messages_in_prompt`` is ``0`` while every row-refusal path ran a turn over
    a rendered transcript and therefore put turns in it.
    """
    return bool(result.refused_anchor) and result.usage.messages_in_prompt == 0


def _settle_note(unread: int, refused: int) -> str:
    """One line saying why a batch settled as ``partial`` or ``failed``."""
    parts = []
    if unread:
        parts.append(f"{unread} source(s) could not be read")
    if refused:
        parts.append(f"{refused} source(s) were refused")
    return "; ".join(parts)


def _cancelled(
    batch_id: str,
    extracted: int,
    unread: int,
    refused: int,
    filed: int,
    skipped: int,
) -> ImportRunResult:
    """What a run that met a cancellation reports.

    The batch is already ``cancelled`` — cancellation is terminal and owns the
    final state, so this run neither finishes it nor rewrites the progress it
    retained. Every proposal filed before the cancel stays filed.
    """
    return ImportRunResult(
        batch_id=batch_id,
        status=CANCELLED,
        sources_extracted=extracted,
        sources_skipped=unread,
        sources_refused=refused,
        proposals_filed=filed,
        skipped=skipped,
        cancelled=True,
    )


async def _record_source(
    runner: ImportStore,
    batch_id: str,
    source: BatchSource,
    status: str,
    *,
    content_digest: str,
    why: str,
) -> None:
    """Record one source's outcome; a cancel mid-step is not a failure.

    ``why`` is the reader's own sentence — a refusal reason, a filing note — and
    it goes to the engine log beside the outcome, because the store keeps the
    outcome and the digest but has nowhere to keep a reason of its own and a
    ``skipped`` row with no explanation is not something a person can act on.
    Never transcript text: ``why`` is always one of this module's own strings.
    """
    logger.info(
        "import run: %s session %s → %s (%s)",
        source.provider,
        source.source_id,
        status,
        why,
    )
    try:
        await asyncio.to_thread(
            runner.record_source,
            batch_id,
            source.provider,
            source.source_id,
            content_digest=content_digest,
            status=status,
        )
    except ImportStoreError as exc:
        if exc.code != CONFLICT:
            raise
        logger.info(
            "import run: %s session %s was cancelled before it was recorded",
            source.provider,
            source.source_id,
        )


async def _advance(
    runner: ImportStore,
    batch_id: str,
    extracted: int,
    unread: int,
    refused: int,
    filed: int,
    skipped: int,
) -> None:
    """Move the running batch's counters; a cancel mid-step is not a failure."""
    try:
        await asyncio.to_thread(
            runner.record_progress,
            batch_id,
            completed_sources=extracted + unread + refused,
            proposals_filed=filed,
            skipped=skipped,
            current_source_id="",
        )
    except ImportStoreError as exc:
        if exc.code != CONFLICT:
            raise
