"""Synthetic starting vaults and effect observations for the care measurement harness.

``build_seed`` writes one scenario's public synthetic starting vault into a
:class:`Layout` the caller owns and returns a manifest whose digest is the fixture's
``fixture_sha256``. ``observe`` reads only the files that run left behind and
returns the ``Mapping[str, bytes | None]`` that :func:`care_eval_fixtures.grade_fixture`
grades. It is the one place where real effects are translated into the oracle's
symbolic bytes.

The engine keeps the guide at the agent root and the vault beneath it, so the
layout has two roots. :func:`layout_for_workspace` names them the way a booted
isolated engine does. :func:`observe_cursor` and :func:`observe_effects` are
separate readers for the curation cursor and the receipt journal; they need the
settled run's holder id, which the caller must prove, so :func:`observe` never
emits their keys.

Nothing here runs a schedule, boots an engine, calls a model or reads the
installed state. Paths are always under the roots the caller passes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from ciao import curation_logs, curation_run, memory_receipts
from ciao import note_verification as nv
from ciao import proposal_kinds
from ciao.curation_logs import CurationLogError
from ciao.curation_run import (
    CURATION_LOG_RELATIVE,
    PASS_PROPOSALS,
    PROPOSALS_RELATIVE,
    WEEKLY_PASS_DAYS,
    CurationStateError,
)
from ciao.memory_receipts import content_revision
from ciao.task_eval import WORKSPACE
from ciao.task_eval_contract import Scenario
from ciao.workspace_guide import GUIDE_NAME

# Symbolic bytes the oracle expects (see care_eval_fixtures.CARE_FIXTURES). A real
# observation is translated to these only when it is the seeded fact.
SYMBOL_GUIDE = b"fixture-guide-v1"
SYMBOL_NOTE_REVISION = b"fixture-note-rev-1"
SYMBOL_AWAITING_HUMAN = b"awaiting_human"
SYMBOL_WEEKLY_MARKER = b"2026-11-18"

GUIDE_RELATIVE = GUIDE_NAME
GUIDE_TEXT = SYMBOL_GUIDE
VAULT_DIRNAME = "memory-vault"
NOTE_RELATIVE = "Notes/Fixture Note.md"
NOTE_TEXT = b"# Fixture note\n\nThe fixture note body.\n"
PROPOSAL_TEXT = "Prefer short status updates"
PROPOSAL_ROW = f"- [memory] {PROPOSAL_TEXT} _(decide: you; needs a human call)_\n"
# Far enough before any run that the weekly pass is due; a fixed date keeps the
# fixture digest independent of when it was built.
SEED_MARKER = "2026-11-01"
# Two plain routable rows for budget_continuation: the proposals pass leads the plan and
# keeps queue order, so a one-item budget takes the first row and the second is resumed.
# `[user]` is not a region kind, so each row is one worklist key with no predict stage.
CONTINUATION_PROPOSAL_TEXTS = ("Prefers morning meetings", "Prefers written summaries")

CursorStatus = Literal["ok", "absent", "unreadable"]


class _Unknown(Exception):
    """A file exists but cannot be read as the observation needs: the key is omitted."""


@dataclass(frozen=True)
class Layout:
    """Where the guide and the vault live.

    The guide is at ``agent_root``; every other seeded file is under ``vault_root``.
    """

    agent_root: Path
    vault_root: Path


def layout_for_workspace(workspace: Path) -> Layout:
    """The layout a booted isolated engine uses under ``workspace`` (``prepare_workspace``)."""
    personal = Path(workspace) / WORKSPACE
    return Layout(agent_root=personal, vault_root=personal / VAULT_DIRNAME)


@dataclass(frozen=True)
class DispatchClock:
    """The trusted start of one settled dispatch, from its job-run row."""

    started_at: datetime

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("dispatch started_at must be timezone-aware")

    @property
    def utc_date(self) -> date:
        return self.started_at.astimezone(UTC).date()


@dataclass(frozen=True)
class SeedFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class SeedManifest:
    scenario: str
    files: tuple[SeedFile, ...]
    fixture_sha256: str
    # Worklist keys of the seeded items, in plan order. Empty unless the scenario has
    # effect-count obligations; the digest above covers them.
    item_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class CursorObservation:
    """The curation cursor's done keys. Empty unless ``status`` is ``ok``."""

    done_keys: frozenset[str]
    status: CursorStatus


def _seed_files(scenario: Scenario) -> dict[str, bytes]:
    guide = {GUIDE_RELATIVE: GUIDE_TEXT}
    if scenario == "nightly":
        return {
            **guide,
            NOTE_RELATIVE: NOTE_TEXT,
            PROPOSALS_RELATIVE: f"# Memory proposals\n\n{PROPOSAL_ROW}".encode(),
        }
    if scenario == "weekly_due":
        marker = curation_logs.with_full_pass_marker(None, SEED_MARKER)
        return {**guide, CURATION_LOG_RELATIVE: marker}
    rows = "".join(f"- [user] {text}\n" for text in CONTINUATION_PROPOSAL_TEXTS)
    return {**guide, PROPOSALS_RELATIVE: f"# Memory proposals\n\n{rows}".encode()}


def seeded_item_keys(scenario: str, files: Mapping[str, bytes]) -> tuple[str, ...]:
    """The worklist keys of the seeded proposal rows, derived with the product's own key function.

    Only ``budget_continuation`` has item keys. They are read back out of the seed's
    proposals file, never typed in, so a seed edit cannot leave a stale key behind.
    """
    if scenario != "budget_continuation":
        return ()
    from ciao.proposal_tracking import walk_proposal_queue

    text = files[PROPOSALS_RELATIVE].decode("utf-8")
    return tuple(
        curation_run.item_key(PASS_PROPOSALS, curation_run._proposal_subject(entry.bullet, entry.dup))
        for entry in walk_proposal_queue("", PROPOSALS_RELATIVE, text)
    )


def _root_for(layout: Layout, relative: str) -> Path:
    return layout.agent_root if relative == GUIDE_RELATIVE else layout.vault_root


def fixture_digest(
    scenario: str, files: Mapping[str, bytes], item_keys: tuple[str, ...] = ()
) -> str:
    """SHA-256 over the scenario name, the sorted (path, sha256) manifest and the item keys.

    The item keys are part of the seed vault's identity: a different key set is a
    different fixture even if the files were somehow equal. Scenarios without item
    keys hash exactly as before.
    """
    rows = [[path, hashlib.sha256(files[path]).hexdigest()] for path in sorted(files)]
    document: dict[str, object] = {"scenario": scenario, "files": rows}
    if item_keys:
        document["item_keys"] = list(item_keys)
    payload = json.dumps(document, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_seed(scenario: Scenario, layout: Layout, *, dispatch_date: date) -> SeedManifest:
    """Write the scenario's starting vault into ``layout`` and return its manifest.

    The guide goes under the agent root, every vault file under the vault root.
    Refuses to overwrite a file. A weekly-due seed must have its last full pass
    at least :data:`WEEKLY_PASS_DAYS` before ``dispatch_date``.
    """
    if scenario == "weekly_due":
        age = dispatch_date - date.fromisoformat(SEED_MARKER)
        if age < timedelta(days=WEEKLY_PASS_DAYS):
            raise ValueError("weekly_due seed needs its last full pass to be older than a week")
    files = _seed_files(scenario)
    for relative in sorted(files):
        target = _root_for(layout, relative) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(files[relative])
    entries = tuple(
        SeedFile(path=path, sha256=hashlib.sha256(files[path]).hexdigest())
        for path in sorted(files)
    )
    item_keys = seeded_item_keys(scenario, files)
    return SeedManifest(
        scenario=scenario,
        files=entries,
        fixture_sha256=fixture_digest(scenario, files, item_keys),
        item_keys=item_keys,
    )


def observe(
    scenario: Scenario,
    layout: Layout,
    manifest: SeedManifest,
    dispatch: DispatchClock,
) -> dict[str, bytes | None]:
    """The oracle's observations for one finished run of ``scenario`` under ``layout``.

    A key is omitted when the effect cannot be read (unknown), and maps to
    ``None`` when the effect is observably absent (failed). Index/audit results
    and effect counts are never emitted here; see :func:`observe_cursor` and
    :func:`observe_effects`.
    """
    vault = Path(layout.vault_root)
    observed: dict[str, bytes | None] = {}
    readers: list[tuple[str, Callable[[], bytes | None]]] = [
        ("guide_bytes", lambda: _guide(Path(layout.agent_root), manifest))
    ]
    if scenario == "nightly":
        readers += [
            ("note_revision_verified", lambda: _note(vault)),
            ("pending_proposal_disposition", lambda: _disposition(vault)),
        ]
    elif scenario == "weekly_due":
        readers.append(("weekly_marker", lambda: _marker(vault, dispatch)))
    for key, reader in readers:
        try:
            observed[key] = reader()
        except _Unknown:
            continue
    return observed


def observe_cursor(vault_root: Path) -> CursorObservation:
    """Read the curation cursor's done keys. Read-only; never raises on a bad file.

    A corrupt state file is ``unreadable`` with no keys, so nothing is credited
    from it. A missing one is ``absent``.
    """
    try:
        state = curation_run.load_state(Path(vault_root))
    except CurationStateError:
        return CursorObservation(done_keys=frozenset(), status="unreadable")
    if not curation_run.state_path(Path(vault_root)).is_file():
        return CursorObservation(done_keys=frozenset(), status="absent")
    return CursorObservation(done_keys=frozenset(state.done_keys), status="ok")


@dataclass(frozen=True)
class LastRun:
    """The curation state's last-run summary, as far as the continuation check needs it."""

    run_id: str | None
    planned: int
    deferred: int


def observe_last_run(vault_root: Path) -> LastRun | None:
    """The last run's holder id and work counts. Read-only; None when unreadable or absent."""
    try:
        state = curation_run.load_state(Path(vault_root))
    except CurationStateError:
        return None
    summary = state.last_run
    if not summary:
        return None
    run_id = summary.get("run_id")
    return LastRun(
        run_id=run_id if isinstance(run_id, str) and run_id else None,
        planned=int(summary["planned"]),
        deferred=int(summary["deferred"]),
    )


def observe_effects(
    vault_root: Path, run_id: str, item_keys: Iterable[str]
) -> Mapping[str, int | None]:
    """How many applied receipts the run ``run_id`` left for each ``item_key``.

    Counts the effective (latest row per receipt id) ``applied`` receipts whose
    ``run_id`` and ``item_key`` match. A prepared-only or undone receipt counts 0.
    An absent journal counts 0; an unreadable one, or a symlinked one, is
    ``None`` for every key. ``run_id`` must be the settled run's proven holder:
    an empty one would match receipts written without a run id, so it is refused.
    """
    if not run_id:
        raise ValueError("observe_effects needs the settled run's holder id")
    keys = tuple(item_keys)
    journal = memory_receipts.journal_path(Path(vault_root))
    if not _journal_readable(journal):
        return dict.fromkeys(keys)
    counts = dict.fromkeys(keys, 0)
    for row in memory_receipts.read_receipts(journal):
        if row.get("status") != memory_receipts.APPLIED or row.get("run_id") != run_id:
            continue
        key = row.get("item_key")
        if isinstance(key, str) and key in counts:
            counts[key] += 1
    return counts


def _journal_readable(journal: Path) -> bool:
    # read_receipts folds any OSError into an empty journal, so unreadable is
    # decided here: an absent journal is genuinely empty, anything else is not.
    if journal.is_symlink():
        return False
    try:
        with journal.open("rb") as handle:
            handle.read(1)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _read(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _Unknown(str(exc)) from exc


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Unknown(str(exc)) from exc


def _guide(root: Path, manifest: SeedManifest) -> bytes | None:
    data = _read(root / GUIDE_RELATIVE)
    if data is None:
        return None
    seeded = {entry.path: entry.sha256 for entry in manifest.files}.get(GUIDE_RELATIVE)
    if seeded is not None and hashlib.sha256(data).hexdigest() == seeded:
        return SYMBOL_GUIDE
    return data


def _note(root: Path) -> bytes | None:
    data = _read(root / NOTE_RELATIVE)
    if data is None:
        return None
    revision = content_revision(_text(data))
    _, skipped = nv._load_check_document(nv.note_check_state_path(root))
    if skipped:
        raise _Unknown(skipped)
    check = nv.read_note_checks(root).get(NOTE_RELATIVE)
    if check is None or check.content_revision != revision:
        return None
    # Only a still-valid verdict is a verification; update, retire and unverified
    # (and any other outcome) leave the note unconfirmed.
    if check.outcome != nv.STILL_VALID:
        return None
    return SYMBOL_NOTE_REVISION


def _disposition(root: Path) -> bytes | None:
    data = _read(root / PROPOSALS_RELATIVE)
    if data is None:
        return None
    for line in _text(data).splitlines():
        bullet = proposal_kinds.parse_bullet(line)
        if (
            bullet is not None
            and bullet.kind == "memory"
            and bullet.text == PROPOSAL_TEXT
            and bullet.decider == "you"
        ):
            return SYMBOL_AWAITING_HUMAN
    return None


def _marker(root: Path, dispatch: DispatchClock) -> bytes | None:
    try:
        snapshot = curation_logs.read_log(root / CURATION_LOG_RELATIVE)
    except CurationLogError as exc:
        raise _Unknown(str(exc)) from exc
    if snapshot.data is None:
        return None
    stamp = curation_logs.marker_value(_text(snapshot.data))
    if not stamp:
        return None
    if stamp == dispatch.utc_date.isoformat():
        return SYMBOL_WEEKLY_MARKER
    return stamp.encode("utf-8")
