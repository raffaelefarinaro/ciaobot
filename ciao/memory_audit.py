"""Rot detection for the always-loaded bounded-memory surface.

The bounded ``ciao:memory`` / ``ciao:profile`` regions load into every single
chat turn, so a wrong claim there is more expensive than a wrong claim anywhere
else in the vault: it is asserted to the model before the user has said a word.
:mod:`ciao.os_audit` already checks the *mechanical* health of those regions
(caps, expiry, exact duplicates, invisible Unicode). This module checks whether
their *content* has rotted.

It follows one rule: every remembered fact is either **state** (a current value
that gets replaced when it changes) or an **event** (a thing that happened,
appended and never edited). The regions are a state surface. Rot is what you get
when events pile up in them, when a path they cite stops existing, or when a new
value is appended next to the old one instead of replacing it.

The same state/event rule decides how *age* is read on vault notes: an entity
note (a person, a project) asserts current state, so going unverified for a
long time is a candidate for review; a log or journal entry records an event,
and events never go stale no matter how old they are. Age alone is never a
defect — it is evidence for the curation routine to judge, which is why these
findings are informational and do not raise audit status.

It also decides how *age* is read one level in. A note has no single age: one
list item in it can be two years out of date while its neighbours were checked
last week, and a note-level verdict has nowhere to put that. So
:func:`find_stale_notes` and :func:`find_stale_entries` are siblings rather than
a detector and a special case — the same horizon, the same aliases, the same
exempt event types, the same state/event rule — measured over notes and over the
entries inside them. Both are built on :func:`note_verification`, so a surface
cannot show a note fresh and its oldest bullet overdue.

Deliberately model-free. A model asked to tally a few hundred entries returns a
confident number, and a different one tomorrow. The detectors here count; the
curation routine that consumes them judges. That means they are tuned for
precision over recall: a detector that cries wolf trains the reader to skip the
report, which is worse than a detector that stays quiet.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ciao import note_entries as ne

if TYPE_CHECKING:
    # Type-only, so importing this module still costs no YAML: the registry's
    # views are the annotation, and `load_entity_types` is imported lazily by
    # the one function that has a vault root to load it from.
    from ciao.entity_types import EntityTypeRegistry

# Matches the excerpt width os_audit already uses for memory findings, so the
# two reports do not disagree on truncation.
EXCERPT_CHARS = 160

# Transcript residue. Each of these says "this entry is a record of something
# that happened in a chat", which belongs in a log, not in the surface that is
# asserted on every turn. Kept narrow on purpose: "User prefers X" and "User
# runs Ubuntu" are durable state and must not match.
_EVENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "quoted-user-turn",
        re.compile(
            r"\b(?:the\s+)?User\s+(?:said|asked|wrote|replied|requested|"
            r"corrected|redirected|rejected|objected|pushed\s+back|"
            r"insisted|clarified|told)\b",
            re.IGNORECASE,
        ),
    ),
    ("quoted-user-turn", re.compile(r"\bUser\s*:\s*[\"“'']")),
    # Only real arrows. An em dash or `--` before "assistant" is ordinary prose
    # punctuation — "Prefers terse replies — assistant should skip preamble" is
    # durable state, and flagging it pins the audit at needs-attention with no
    # way to clear it short of rewriting a correct entry. A genuine event
    # written with an em dash still matches the verb pattern below.
    ("assistant-action", re.compile(r"(?:->|→)\s*assistant\b", re.IGNORECASE)),
    (
        "assistant-action",
        re.compile(
            r"\bassistant\s+(?:then\s+)?(?:corrected|confirmed|bumped|changed|"
            r"switched|fixed|updated|replied|noted|clarified|applied|"
            r"implemented|reformatted|rephrased|rewrote|restored|replaced|"
            r"removed|added|responded|accepted|declined)\b",
            re.IGNORECASE,
        ),
    ),
    # Ciaobot's own memory-proposal format cites source turns as [idx=12,34].
    # Surviving into a region means a proposal was promoted verbatim.
    ("transcript-citation", re.compile(r"\[idx\s*=")),
)

# Trailing characters that come from the surrounding sentence, not the path.
_PATH_TRAILING = ".,;:!?)]}>\"'`"


def _trim_path_token(raw: str) -> str:
    """Trim prose punctuation from the end of a path token only.

    Must not use ``str.strip(_PATH_TRAILING)``: that trims both ends, and the
    set contains ``.``, so ``./scripts/x.sh`` became ``/scripts/x.sh`` (now
    absolute, so it resolved outside the workspace and was written off as
    unverifiable) and ``.claude/settings.json`` became ``claude/settings.json``
    (no longer path-shaped, so it was dropped). Either way the stale-path
    detector silently stopped checking exactly the paths it should.
    """
    return raw.strip().rstrip(_PATH_TRAILING)


# A `path.py:12` or `path.py:12:5` source reference.
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")

_BACKTICK_RE = re.compile(r"`([^`\n]{2,200})`")

# snake_case identifiers: config keys, function names, env vars lowercased.
# Two segments minimum, so `memory` alone never becomes a subject.
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# Subjects too generic to imply two entries are about the same thing.
_SUBJECT_STOPWORDS = frozenset(
    {
        "ciao", "ciaobot", "claude", "codex", "user", "assistant", "memory",
        "profile", "vault", "workspace", "project", "chat", "session", "file",
        "note", "entry", "region",
    }
)

_MIN_SUBJECT_CHARS = 4


def _excerpt(entry: str) -> str:
    return entry[:EXCERPT_CHARS]


def find_event_shaped(region: str, entries: list[str]) -> list[dict[str, Any]]:
    """Entries that record a chat event instead of asserting current state."""
    findings: list[dict[str, Any]] = []
    for entry in entries:
        markers = sorted(
            {name for name, pattern in _EVENT_PATTERNS if pattern.search(entry)}
        )
        if markers:
            findings.append(
                {"region": region, "entry": _excerpt(entry), "markers": markers}
            )
    return findings


def _candidate_paths(entry: str) -> list[str]:
    """Path-shaped tokens in an entry, backticked or bare."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        token = _trim_path_token(raw)
        # A bare `~` or `/` carries no information. Globs and `<placeholder>`
        # segments are patterns, not paths that could be checked for existence.
        if len(token) < 3 or any(ch in token for ch in "*?[]{}<>"):
            return
        if "/" not in token or "://" in token or token.startswith(("http", "mailto:")):
            return
        if any(ch.isspace() for ch in token):
            return
        if token not in seen:
            seen.add(token)
            candidates.append(token)

    for match in _BACKTICK_RE.finditer(entry):
        add(match.group(1))
    for token in re.split(r"[\s,;]+", _BACKTICK_RE.sub(" ", entry)):
        add(token)
    return candidates


def _looks_like_path(token: str, workspace_dir: Path) -> bool:
    """Whether ``token`` is meant as a path into this workspace.

    Requiring positive evidence is what keeps this detector quiet: without it,
    every slash in a sentence becomes a missing-file finding.

    A file extension is deliberately *not* enough on its own. Ciaobot's engine
    and vault live in sibling repos, so a durable entry legitimately cites
    ``ciao/cli.py`` while standing in the vault repo, where no ``ciao/`` exists.
    Treating that as rot would push curation to "fix" a correct entry, so the
    token must be explicitly rooted or start at a directory that exists here.
    """
    if token.startswith(("~/", "/", "./", "../")) or token.endswith("/"):
        return True
    first = _LINE_SUFFIX_RE.sub("", token).split("/", 1)[0]
    if not first or first in {".", ".."}:
        return False
    try:
        return (workspace_dir / first).is_dir()
    except OSError:
        return False


def _resolve(token: str, workspace_dir: Path) -> tuple[bool, bool]:
    """Return ``(exists, verifiable)`` for a path-shaped token.

    A path outside both the workspace and the user's home is left unverified: it
    may well belong to another machine, and calling that "stale" would be a
    guess dressed up as a finding.
    """
    target = _LINE_SUFFIX_RE.sub("", token).rstrip("/")
    if not target:
        return True, False

    expanded = Path(target).expanduser()
    if expanded.is_absolute():
        try:
            home = Path.home().resolve()
        except (OSError, RuntimeError):
            home = None
        try:
            resolved = expanded.resolve()
            workspace = workspace_dir.resolve()
        except OSError:
            return True, False
        inside_workspace = resolved == workspace or workspace in resolved.parents
        inside_home = bool(home) and (resolved == home or home in resolved.parents)
        if not (inside_workspace or inside_home):
            return True, False
        return expanded.exists(), True

    try:
        return (workspace_dir / target).exists(), True
    except OSError:
        return True, False


def find_stale_paths(
    region: str, entries: list[str], *, workspace_dir: Path
) -> tuple[list[dict[str, Any]], int, int]:
    """Entries citing a path that no longer exists.

    Returns ``(findings, checked, unverifiable)``. The counts are reported so an
    empty finding list is not mistaken for full coverage.
    """
    findings: list[dict[str, Any]] = []
    checked = 0
    unverifiable = 0
    for entry in entries:
        for token in _candidate_paths(entry):
            if not _looks_like_path(token, workspace_dir):
                continue
            exists, verifiable = _resolve(token, workspace_dir)
            if not verifiable:
                unverifiable += 1
                continue
            checked += 1
            if not exists:
                findings.append(
                    {
                        "region": region,
                        "entry": _excerpt(entry),
                        "path": token,
                        "message": f"path does not exist in this workspace: {token}",
                    }
                )
    return findings, checked, unverifiable


def _subjects(entry: str, workspace_dir: Path) -> set[str]:
    """Distinctive things an entry makes a claim about."""
    subjects: set[str] = set()
    for match in _BACKTICK_RE.finditer(entry):
        token = _trim_path_token(match.group(1))
        if len(token) >= _MIN_SUBJECT_CHARS and not any(ch.isspace() for ch in token):
            subjects.add(token.lower())
    for match in _SNAKE_RE.finditer(entry):
        subjects.add(match.group(0).lower())
    for token in _candidate_paths(entry):
        if _looks_like_path(token, workspace_dir):
            subjects.add(_LINE_SUFFIX_RE.sub("", token).lower())
    return {
        subject
        for subject in subjects
        if len(subject) >= _MIN_SUBJECT_CHARS and subject not in _SUBJECT_STOPWORDS
    }


def find_superseded_state(
    region: str, entries: list[str], *, workspace_dir: Path
) -> list[dict[str, Any]]:
    """Several entries in one region asserting state about the same subject.

    This is the state-appended-instead-of-replaced failure: the old value stays
    in the prompt next to the new one, and the model has no way to tell which
    one is current. Reported as a candidate rather than a defect, because two
    entries can legitimately describe different facets of one subject.
    """
    by_subject: dict[str, list[str]] = {}
    for entry in entries:
        for subject in _subjects(entry, workspace_dir):
            by_subject.setdefault(subject, []).append(_excerpt(entry))
    return [
        {"region": region, "subject": subject, "entries": excerpts}
        for subject, excerpts in sorted(by_subject.items())
        if len(excerpts) > 1
    ]


# ---- Temporal validity --------------------------------------------------
#
# Two stamps, two clocks. `[as-of: YYYY-MM-DD]` is world time: the fact was
# true as of that date and may have silently changed since. The trailing
# `[YYYY-MM-DD]` learned-at stamp is system time: when the fact was promoted
# into the region. Both are read here as aging evidence for the curation
# routine to re-verify — informational, like every age signal in this module,
# because age alone is never a defect.

_AS_OF_RE = re.compile(r"\[as-of:\s*(\d{4}-\d{2}-\d{2})\]")
_LEARNED_STAMP_RE = re.compile(r"\s*\[(\d{4}-\d{2}-\d{2})\]\s*$")
# Every trailing stamp, not just the last. Stripping one was enough while
# nothing could write two, but a reconcile bug did: the model echoes the old
# entry's stamp into its merge and the promoter stamped on top, leaving
# `fact [2026-01-01] [2026-09-02]` in the region. Because the pattern is
# `$`-anchored, stripping one of those still left a stamp attached, so the
# text never compared equal to the fact again and the duplicate guard let it
# be appended a second time. Those rows are already on disk in real installs,
# so the strip has to heal them, not just stop making new ones.
_LEARNED_STAMPS_RE = re.compile(r"(?:\s*\[\d{4}-\d{2}-\d{2}\])+\s*$")

# An `[as-of]` fact declares itself a snapshot, so it ages fast; a plain
# learned-at entry claims to be standing state and gets the default horizon
# vault notes use.
AS_OF_AGING_DAYS = 90
LEARNED_AGING_DAYS = 180


def strip_learned_stamp(entry: str) -> str:
    """The entry text without its trailing learned-at stamp.

    Promotion dedupe compares through this: the same fact promoted on two
    different days must still count as a duplicate — and so must one an older
    build left carrying two stamps (see :data:`_LEARNED_STAMPS_RE`).

    Only the extraction of a learned date still reads a single stamp: the last
    one is the most recent promotion, which is what aging should measure from.
    """
    return _LEARNED_STAMPS_RE.sub("", entry).rstrip()


def find_aging_state(
    region: str,
    entries: list[str],
    *,
    today: datetime.date | None = None,
) -> list[dict[str, Any]]:
    """Entries whose declared date has aged past its horizon. Informational."""
    current = today or datetime.date.today()
    findings: list[dict[str, Any]] = []
    for entry in entries:
        as_of = _AS_OF_RE.search(entry)
        if as_of:
            kind, raw, horizon = "as-of", as_of.group(1), AS_OF_AGING_DAYS
        else:
            learned = _LEARNED_STAMP_RE.search(entry)
            if not learned:
                continue
            kind, raw, horizon = "learned", learned.group(1), LEARNED_AGING_DAYS
        # Shape-valid but impossible dates (2025-02-30) come back None; the
        # expiration-tag checks own malformed-stamp reporting, aging must not
        # guess.
        stamped = parse_verified_date(raw)
        if stamped is None:
            continue
        age_days = (current - stamped).days
        if age_days < horizon:
            continue
        findings.append(
            {
                "region": region,
                "entry": _excerpt(entry),
                "kind": kind,
                "date": raw,
                "age_days": age_days,
                "threshold_days": horizon,
            }
        )
    return findings


def audit_entries(
    region_entries: dict[str, list[str]],
    *,
    workspace_dir: Path,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Run every rot detector over the bounded-memory regions.

    ``region_entries`` maps a region name to its parsed entries, as
    :func:`ciao.memory_tool.read_region` returns them.
    """
    event_shaped: list[dict[str, Any]] = []
    stale_paths: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    aging: list[dict[str, Any]] = []
    checked = 0
    unverifiable = 0

    for region, entries in region_entries.items():
        event_shaped.extend(find_event_shaped(region, entries))
        found, region_checked, region_unverifiable = find_stale_paths(
            region, entries, workspace_dir=workspace_dir
        )
        stale_paths.extend(found)
        checked += region_checked
        unverifiable += region_unverifiable
        superseded.extend(
            find_superseded_state(region, entries, workspace_dir=workspace_dir)
        )
        aging.extend(find_aging_state(region, entries, today=today))

    return {
        "event_shaped_entries": event_shaped,
        "stale_path_entries": stale_paths,
        "superseded_state_candidates": superseded,
        "aging_state_entries": aging,
        "paths_checked": checked,
        "paths_unverifiable": unverifiable,
    }


# ---- Vault-note aging -------------------------------------------------------
#
# The bounded regions are read every turn, so their rot is expensive and the
# detectors above watch them continuously. Vault notes are read on demand, but
# they rot too: a project note whose status nobody has checked in four months
# is asserted to whoever finally opens it, and the curation routine cannot
# review what nothing ever lists.

# Days after which a note's facts count as unverified, by note type. Two
# overrides over one default because entity types rot at different speeds: an
# active project's state changes weekly while a person's employer changes
# yearly. 90 days for people matches the weekly-review template's existing
# staleness rule, which until now was aspirational.
STALE_NOTE_THRESHOLDS_DAYS: dict[str, int] = {
    "project": 30,
    "person": 90,
}
STALE_NOTE_DEFAULT_DAYS = 180

# Event surfaces never age out — a log entry from two years ago is exactly as
# true as it was the day it was written. ``workspace`` covers the Workspace/
# queue files (proposals, learnings, skill triage), whose lifecycles are owned
# by the curation routines; flagging an inbox for being an inbox is noise.
STALE_NOTE_EXEMPT_TYPES = frozenset({"log", "journal", "workspace"})

_UPDATED_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def note_threshold_days(
    note_type: str, *, registry: EntityTypeRegistry | None = None
) -> int:
    """Days of silence after which this note type counts as unverified.

    ``registry`` is the vault's category list, optional for the same reason as
    everywhere else in this module: a caller that knows its vault passes one and
    a category's own ``stale_after_days`` applies, and a caller with no vault in
    hand keeps the two overrides above. A category with no opinion about ageing
    is simply absent from the registry's view, so it falls through to the
    default exactly as it does here.
    """
    if registry is None:
        return STALE_NOTE_THRESHOLDS_DAYS.get(note_type, STALE_NOTE_DEFAULT_DAYS)
    return registry.stale_thresholds().get(note_type, STALE_NOTE_DEFAULT_DAYS)


def parse_verified_date(raw: str) -> datetime.date | None:
    """Parse a frontmatter ``updated:`` value. None unless YYYY-MM-DD."""
    value = (raw or "").strip()
    if not _UPDATED_DATE_RE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        # Shape-valid but not a real date (2025-02-30). Treated as absent:
        # the detector falls back to mtime rather than guessing.
        return None


def note_last_verified(
    updated: str, mtime: float | None
) -> tuple[datetime.date | None, str]:
    """When a note's facts were last verified, and where that came from.

    Prefers frontmatter ``updated:`` — a deliberate claim that someone re-read
    the note — and falls back to mtime, which only says the file changed.
    Returns ``(date, source)`` with source ``""`` when neither is usable.
    """
    from_date = parse_verified_date(updated)
    if from_date is not None:
        return from_date, "frontmatter"
    if mtime and mtime > 0:
        return (
            datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).date(),
            "mtime",
        )
    return None, ""


def _stale_type_key(
    note_type: str, *, registry: EntityTypeRegistry | None = None
) -> str:
    """The type a note ages as: its canonical form, else the lowered raw value.

    Resolved through the vault's own category list so a spelling cannot move a
    note onto the wrong horizon: ``type: Person`` ages like ``person`` and
    ``type: hackathon-log`` (an alias of ``journal``) is a dated record that
    never ages at all. The three steps are the canonical form, the category that
    owns this alias, and finally the lowered raw value — a type nothing claims
    ages on its own spelling, which is the pre-existing behaviour.

    Imported lazily: this module is otherwise dependency-free and ``vault_index``
    pulls in YAML. *registry* is threaded the same way, so a category the owner
    added resolves here too, and a caller with no vault in hand (a pure unit
    seam) keeps the shipped alias table.
    """
    from ciao.vault_index import canonical_type

    raw = (note_type or "").strip()
    # ``canonical_type`` already resolves aliases (case-insensitively) against
    # the same registry, so a separate alias lookup here could never add an
    # owner it did not already return.
    canonical = canonical_type(raw, registry=registry)
    if canonical:
        return canonical
    return raw.lower()


def is_stale_exempt_type(
    note_type: str, *, registry: EntityTypeRegistry | None = None
) -> bool:
    """Whether notes of this type never age out (logs, journals, queues).

    The exempt set stays a constant of this module: it is a statement about
    event surfaces, not a per-category threshold, and no stock category states
    one.
    """
    return _stale_type_key(note_type, registry=registry) in STALE_NOTE_EXEMPT_TYPES


@dataclass(frozen=True, slots=True)
class NoteVerification:
    """How long a note's facts have gone unverified, against its horizon.

    The one answer to "is this note stale?" shared by ``find_stale_notes``
    (memory-audit), the Memory Map's ``stale`` flag and the vault-review
    ``unverified`` signal — three surfaces that used to compute it separately
    and disagreed on which notes counted.
    """

    age_days: int
    threshold_days: int
    last_verified: datetime.date
    source: str  # "frontmatter" | "mtime"
    exempt: bool

    @property
    def stale(self) -> bool:
        return not self.exempt and self.age_days >= self.threshold_days

    def as_evidence(self) -> dict[str, Any]:
        return {
            "age_days": self.age_days,
            "threshold_days": self.threshold_days,
            "last_verified": self.last_verified.isoformat(),
            "source": self.source,
        }


def note_verification(
    note_type: str,
    updated: str,
    mtime: float | None,
    *,
    today: datetime.date | None = None,
    registry: EntityTypeRegistry | None = None,
) -> NoteVerification | None:
    """Age and horizon for one note, or None when neither date is usable.

    Unverifiable is not stale — calling it so would be a guess. Exempt types
    still get an age (the map shows it) but are never ``stale``. Ages are
    clamped at zero so a future ``updated:`` reads as "verified today".

    ``registry`` is the vault's category list, threaded into both the type's
    resolved name and its horizon so a category's ``stale_after_days`` reaches
    the verdict rather than only the count beside it.
    """
    verified, source = note_last_verified(updated, mtime)
    if verified is None:
        return None
    current = today or datetime.date.today()
    key = _stale_type_key(note_type, registry=registry)
    return NoteVerification(
        age_days=max(0, (current - verified).days),
        threshold_days=note_threshold_days(key, registry=registry),
        last_verified=verified,
        source=source,
        exempt=key in STALE_NOTE_EXEMPT_TYPES,
    )


def find_stale_notes(
    entries: list[Any],
    *,
    vault_root: Path | None = None,
    path_prefix: Path | None = None,
    mtimes: dict[str, float] | None = None,
    today: datetime.date | None = None,
    registry: EntityTypeRegistry | None = None,
) -> dict[str, Any]:
    """Vault notes whose facts have gone unverified past their type's horizon.

    ``entries`` are :class:`ciao.vault_index.Entry` objects (anything with
    ``path``/``title``/``type``/``updated`` works). Age comes from frontmatter
    ``updated:`` when present, else file mtime — supplied per rendered path via
    ``mtimes``, or stat'ed here by stripping ``path_prefix`` off the rendered
    path and joining onto ``vault_root``.

    ``registry`` is the vault's category list, and it is optional: omitted and a
    ``vault_root`` is known, it is loaded from that root (resolved, so the
    loader's cache key is the same root the mtime probe below used); omitted
    with no root at all, the shipped thresholds stand, which is what keeps this
    function usable as a pure seam over entries a test built by hand.

    Precision-first like every detector in this module: exempt event types,
    report age and threshold side by side so a reader can disagree with the
    verdict without losing the evidence, and return checked/exempt counts so
    an empty list is not mistaken for full coverage.
    """
    current = today or datetime.date.today()
    if registry is None and vault_root is not None:
        from ciao.entity_types import load_entity_types

        registry = load_entity_types(Path(vault_root).resolve())
    prefix = Path("memory-vault") if path_prefix is None else Path(path_prefix)
    findings: list[dict[str, Any]] = []
    checked = 0
    exempt = 0

    for entry in entries:
        note_type = (entry.type or "").strip()
        if is_stale_exempt_type(note_type, registry=registry):
            exempt += 1
            continue
        rendered = str(entry.path)
        if mtimes is not None:
            mtime = mtimes.get(rendered, 0.0)
        elif vault_root is not None:
            rel = entry.path
            try:
                rel = Path(rendered).relative_to(prefix)
            except ValueError:
                pass
            try:
                mtime = (Path(vault_root) / rel).stat().st_mtime
            except OSError:
                mtime = 0.0
        else:
            mtime = 0.0
        verification = note_verification(
            note_type, entry.updated, mtime, today=current, registry=registry
        )
        if verification is None:
            # Unverifiable is not stale: calling it so would be a guess.
            continue
        checked += 1
        if not verification.stale:
            continue
        findings.append(
            {
                "path": rendered,
                "title": entry.title,
                "type": note_type or "note",
                **verification.as_evidence(),
            }
        )

    findings.sort(key=lambda f: (-f["age_days"], f["path"]))
    return {
        "stale_notes": findings,
        "notes_checked": checked,
        "notes_exempt": exempt,
    }


# ---- Entry-level freshness -------------------------------------------------
#
# `find_stale_notes` above is the right question about a *file*: is the thing this
# note asserts still true? It is the wrong question about a fact, because a note
# has no single age. One list item in a person note can be two years out of date
# while the bullet above it was re-checked last week, and a whole-note verdict
# claims both or neither — so re-verifying the address silently re-certifies the
# landlord's name from 2019 with it.
#
# What follows is the same verdict measured one bullet in, and it is deliberately
# a *sibling* rather than a second implementation: the horizon, the type aliases,
# the exempt event types and the state/event rule are the functions above, called
# rather than restated, and the aging itself is `note_verification`'s. A surface
# that disagrees with this one disagrees with the note-level verdict, not with
# some third answer.
#
# Precision-first, like everything in this module, and it leans harder here
# because a wrong finding at entry level points a person's attention at one
# sentence they then have to go and check:
#
# * **Only old and undated entries are selected.** An entry with a valid
#   `[verified:]` stamp inside its horizon is not a finding, whatever its note's
#   own `updated:` says — a whole-note selection re-stamped yesterday would
#   otherwise re-list a bullet from two years ago and a bullet checked this
#   morning in the same list.
# * **A missing or unusable stamp is `unverified` only once the horizon has
#   passed.** `[verified: 2026-13-01]` and `[verified: yesterday]` are always
#   findings: a stamp that cannot be believed is nobody having checked, and a
#   *future* stamp is nobody having checked either. A bullet with no stamp at all
#   **inherits the note's date**, so it is current exactly as far as the note is
#   — selecting it unconditionally made every bullet written before `[verified:]`
#   stamps existed read as never checked, and turned a nightly plan into a list
#   of one-day-old entries in any vault that had been re-stamped. That is the
#   failure the whole level was supposed to prevent, in a different costume.
# * **Event-shaped entries and explicit event sections are exempt.** A log entry
#   from 2019 is as true as the day it was written, and a heading that says
#   `## Events` is a stronger signal than any guess made from the shape of a
#   line. A calendar date on its own is *not* that signal: a lease that ends on
#   the 30th is a current-state assertion that happens to carry a date, and
#   exempting it would drop the one fact a person most needs to re-read.
# * **What was not read is reported, not swallowed.** A paragraph, a table or an
#   entry carrying a construct the entry model does not describe is `uncovered`:
#   never verified, never counted as clean. That is what makes a coverage claim
#   checkable — a note whose facts all live in a table cannot show a fresh badge
#   just because the one bullet beside the table was checked this morning.

# Why one entry was selected. Plain codes so a caller can count them without
# parsing prose and a test can pin one; `EntryVerdict.reason` says the same
# thing to a person, with the numbers beside it.
STALE_ENTRY_AGED = "aged"
"""Carries a valid `[verified:]` stamp and that day is past the horizon."""

STALE_ENTRY_NO_STAMP = "no-stamp"
"""No `[verified:]` stamp at all, so nobody has said this fact was checked."""

STALE_ENTRY_BAD_STAMP = "unusable-stamp"
"""A stamp that is malformed, an impossible day, or a day that has not come."""

# Sections whose entries are records of things that happened. A heading is an
# explicit statement of what the list beneath it is *for*, which is better
# evidence than any inference from a line's shape — so it exempts, and nothing
# softer does. Compared case-folded with whitespace collapsed; both numbers are
# spelled out because a heading is written by a person, not generated.
STALE_ENTRY_EXEMPT_SECTIONS = frozenset(
    {
        "event",
        "events",
        "event log",
        "event logs",
        "history",
        "activity log",
        "log",
        "logs",
        "journal",
        "journals",
        "diary",
        "changelog",
        "changelogs",
        "timeline",
        "meetings",
        "sessions",
        "visits",
        "correspondence",
    }
)

# A date in the leading position of an entry: `2026-03-04: signed the lease`,
# `On 4 March 2026 …`, `March 4, 2026 — …`. Leading is load-bearing. A date
# anywhere in the sentence is a date the fact *mentions*, and a lease that ends
# on the 30th mentions one.
_RECORD_LEADING_DATE_RE = re.compile(
    r"^[\s>*#-]*(?:on|in|by)?[\s]*"
    r"(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?[\s]+[A-Za-z]{3,9}\.?[\s]+\d{4}"
    r"|[A-Za-z]{3,9}\.?[\s]+\d{1,2}(?:st|nd|rd|th)?,?[\s]+\d{4})\b"
)

# A verb or auxiliary that can only be true of a finished thing. Deliberately
# short and deliberately past: "will meet", "is meeting" and "meets" describe
# something still to happen, which is a plan, and a plan in a state note is
# worth re-reading like any other claim.
_COMPLETED_TENSE_RE = re.compile(
    r"\b(?:signed|launched|shipped|finished|completed|attended|booked|met|"
    r"agreed|decided|closed|delivered|presented|resigned|hired|fired|joined|"
    r"left|arrived|departed|relocated|took place|happened|occurred|ended|"
    r"was|were|had|did)\b",
    re.IGNORECASE,
)

# Present-tense state. The counterpart to the pattern above, and the reason a
# past-tense verb alone exempts nothing: "the office moved to 12 Baker Street" is
# a claim about where the office is *now*, written in the past tense because
# people write about changes that way. A dated record that also says "is", "has"
# or "currently" is asserting something now, so it is not exempt.
_PRESENT_STATE_RE = re.compile(
    r"\b(?:is|are|am|was\s+not|has|have|had\s+been|lives?|live|works?|working|"
    r"prefers?|uses?|using|currently|now|still|owns?|runs?|remains?|"
    r"until|as\s+of|expires?)\b",
    re.IGNORECASE,
)

# One line's worth of "this is not an entry", for the coverage classifier.
# The heading and rule cases are structure, not assertions: a note's own title
# is not an unverified fact, and counting it would make every note in the vault
# permanently incomplete. The shapes are restated rather than imported from
# `note_entries` because that module's are private to its own line walk, and
# reaching into another module's internals to classify its output is how two
# definitions of the same markdown shape come to disagree.
_UNCOVERED_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}(?:[ \t]+|$)")
_UNCOVERED_RULE_RE = re.compile(
    r"^(?:[ ]{0,3}\*[ \t]*){3,}$"
    r"|^(?:[ ]{0,3}-[ \t]*){3,}$"
    r"|^(?:[ ]{0,3}_[ \t]*){3,}$"
)
_UNCOVERED_QUOTE_RE = re.compile(r"^[ ]{0,3}>")
_UNCOVERED_FENCE_RE = re.compile(r"^[ ]{0,3}(?:`{3,}|~{3,})")
_UNCOVERED_TABLE_RE = re.compile(r"^[ ]{0,3}\|")

# A line of inline markdown, stripped to judge how much prose is under it. Links
# and emphasis are not words, and a span of nothing but punctuation is not a
# hidden assertion.
_MARKUP_NOISE_RE = re.compile(r"[`*_{}\[\]()#!|>\-~]")

_CONTEXT_LINE_CHARS = 120
"""Width of one neighbouring line in an entry's context. Enough to recognise a
sibling, short enough that a card showing three of them stays a card."""


def _excerpt_line(value: str) -> str:
    """One line, trimmed, for a neighbour's context slot."""
    text = str(value or "").strip()
    return text[:_CONTEXT_LINE_CHARS]


def _context_for(text: str, entry: ne.NoteEntry) -> tuple[str, ...]:
    """The physical lines either side of one entry, and nothing else.

    Deliberately the file's own lines rather than the neighbouring *entries*: a
    reader deciding whether a bullet is current needs the prose around it — a
    heading that says which address this is, a stray sentence explaining the
    move — and a context made of sibling bullets would hide exactly the case
    that matters. Blank lines are dropped, because a gap is not context.
    """
    before = text[: entry.start].rstrip("\n").split("\n")
    after = text[entry.end :].lstrip("\n").split("\n")
    found: list[str] = []
    for line in reversed(before):
        shown = _excerpt_line(line)
        if shown:
            found.append(shown)
            break
    for line in after:
        shown = _excerpt_line(line)
        if shown:
            found.append(shown)
            break
    return tuple(found)


def _normalized_section(section: str) -> str:
    """A heading's text, case-folded and whitespace-collapsed, for the exempt set."""
    return " ".join(str(section or "").split()).casefold()


def _entry_event_reason(entry: ne.NoteEntry) -> str:
    """Why this entry is a record rather than a claim, or ``""``.

    Three signals, in descending order of how much they are worth. An explicit
    event section, because somebody wrote the heading saying so. The chat-event
    markers this module has always used, unchanged — a transcript turn belongs in
    a log whatever section it was pasted under. And a leading date beside a
    completed verb with no present-tense state in it, which is a dated record
    written the way a person writes one. That third rule is the narrow one, and
    it is narrow on purpose: a date alone is a date the fact mentions, and a
    past-tense verb alone is how people describe a change that is still the
    current state.
    """
    if _normalized_section(entry.section) in STALE_ENTRY_EXEMPT_SECTIONS:
        return "section"
    markers = sorted(
        {name for name, pattern in _EVENT_PATTERNS if pattern.search(entry.text)}
    )
    if markers:
        return f"event-shaped ({', '.join(markers)})"
    if (
        _RECORD_LEADING_DATE_RE.match(entry.text)
        and _COMPLETED_TENSE_RE.search(entry.text)
        and not _PRESENT_STATE_RE.search(entry.text)
    ):
        return "dated record"
    return ""


@dataclass(frozen=True, slots=True)
class EntryVerdict:
    """One entry's age, and the evidence a reader needs to disagree with it.

    Everything here is one entry's, never its note's. ``identity`` is
    :func:`ciao.note_entries.entry_identity` — the key the check state, the
    worklist and the proposal queue all use, and the one value a caller cannot
    rederive without reimplementing a hash. ``start``/``end`` are the entry's own
    character span, ``fingerprint`` is the text the verdict is about, and
    ``revision`` is the note's :func:`ciao.memory_receipts.content_revision`, so
    a caller can hand the whole thing to the managed operation without re-reading
    anything.

    ``last_verified`` is the *entry's* own date where it has one and the note's
    where it does not, and which of those two it was is ``own_date``: a bullet
    that inherited the note's date is exactly the case the whole level exists to
    expose, and a reader told "unverified for 400d" without being told that the
    400 days are the file's and not the fact's cannot act on it. ``age_days`` is
    ``None`` when there is no date at all — unverifiable, which is not stale and
    is not fresh either.
    """

    identity: str
    note_path: str
    path: str
    title: str
    note_type: str
    start: int
    end: int
    line_number: int
    section: str
    fingerprint: str
    revision: str
    excerpt: str
    context: tuple[str, ...]
    last_verified: datetime.date | None
    own_date: bool
    age_days: int | None
    threshold_days: int
    reason_code: str
    reason: str
    exempt: bool
    supported: bool

    def as_finding(self) -> dict[str, Any]:
        """The row every surface reads. Flat, JSON-safe, and self-describing."""
        return {
            "identity": self.identity,
            "note_path": self.note_path,
            "path": self.path,
            "title": self.title,
            "type": self.note_type,
            "start": self.start,
            "end": self.end,
            "line_number": self.line_number,
            "section": self.section,
            "fingerprint": self.fingerprint,
            "revision": self.revision,
            "excerpt": self.excerpt,
            "context": list(self.context),
            "last_verified": (
                self.last_verified.isoformat() if self.last_verified else None
            ),
            "own_date": self.own_date,
            "age_days": self.age_days,
            "threshold_days": self.threshold_days,
            "reason": self.reason_code,
            "detail": self.reason,
            "exempt": self.exempt,
            "supported": self.supported,
        }


@dataclass(frozen=True, slots=True)
class NoteEntryCoverage:
    """What one note's entries look like, and how much of it was read at all.

    The note-level answer, and the half the whole-note surfaces need: a note
    with five fresh bullets and one stale is neither fresh nor stale, and only
    these counts can say which. ``checked`` is the entries the detector judged,
    ``exempt`` the ones it deliberately did not, ``unverified`` the judged ones
    with no usable stamp, and ``uncovered`` the material that is not an entry at
    all and therefore cannot be verified by anything — an unsupported entry, a
    paragraph, a table. ``stale`` is the subset of ``checked`` whose own date is
    past the horizon.

    ``fully_verified`` is the property the surfaces actually want, and it is
    deliberately stricter than "no stale entry". A note is fully verified only
    when *every* in-scope assertion is covered: one fresh bullet must not put a
    clean badge on a note whose second bullet is two years old, or whose facts
    live in a table nobody has read since it was written.
    """

    path: str
    relative: str
    title: str
    note_type: str
    entries: int
    checked: int
    exempt: int
    unverified: int
    uncovered: int
    stale: int
    entry_chars: int
    uncovered_chars: int
    total_chars: int
    age_days: int | None
    threshold_days: int
    last_verified: datetime.date | None
    note_exempt: bool
    diagnostics: tuple[str, ...]

    @property
    def coverage_ratio(self) -> float:
        """Share of the note read as entries, ``0.0`` for an empty file."""
        return self.entry_chars / self.total_chars if self.total_chars else 0.0

    @property
    def fully_verified(self) -> bool:
        """Whether every in-scope assertion in this note is covered and current."""
        return not (self.stale or self.unverified or self.uncovered)

    def as_evidence(self) -> dict[str, Any]:
        """The note-level row a surface merges into its own aging fields."""
        return {
            "path": self.path,
            "relative_path": self.relative,
            "title": self.title,
            "type": self.note_type,
            "entries": self.entries,
            "entries_checked": self.checked,
            "entries_exempt": self.exempt,
            "entries_unverified": self.unverified,
            "entries_uncovered": self.uncovered,
            "entries_stale": self.stale,
            "entry_chars": self.entry_chars,
            "uncovered_chars": self.uncovered_chars,
            "total_chars": self.total_chars,
            "coverage_ratio": round(self.coverage_ratio, 4),
            "fully_verified": self.fully_verified,
            "age_days": self.age_days,
            "threshold_days": self.threshold_days,
            "last_verified": (
                self.last_verified.isoformat() if self.last_verified else ""
            ),
            "note_exempt": self.note_exempt,
        }


def _prose_words(line: str) -> int:
    """How many words of prose survive stripping a line's markdown punctuation."""
    return len([word for word in _MARKUP_NOISE_RE.sub(" ", line).split() if word])


def _line_asserts(bare: str) -> bool:
    """Whether one non-entry line can be carrying an assertion.

    Structure — a heading, a thematic break — is not an assertion, and counting
    it would make every note in the vault permanently incomplete. A fence, a
    table row, a quoted sentence and a line of three or more words of prose all
    can be, and all are counted: a table of a person's phone numbers is a set of
    facts about them that nothing here has read, and saying so is the point.
    """
    if _UNCOVERED_HEADING_RE.match(bare) or _UNCOVERED_RULE_RE.match(bare):
        return False
    if _UNCOVERED_FENCE_RE.match(bare) or _UNCOVERED_TABLE_RE.match(bare):
        return True
    if _UNCOVERED_QUOTE_RE.match(bare):
        return _prose_words(bare) >= 2
    return _prose_words(bare) >= 3


def _uncovered_blocks(text: str, document: ne.EntryDocument) -> tuple[int, int]:
    """Runs of material the parse did not read as entries, and their characters.

    The complement :attr:`ciao.note_entries.EntryDocument.uncovered` is exact but
    undifferentiated: it is a note's frontmatter, its headings, its blank lines
    *and* the paragraph its facts are actually written in, and counting all four
    as unverified would make every note in the vault permanently incomplete. So
    each line is classified and the lines that can carry an assertion are grouped
    into **runs** — a three-line paragraph is one place the detector did not look
    even though it is three lines, and a paragraph with a table under it is two.

    Frontmatter is skipped by *line* rather than by span, which matters because
    frontmatter and the first heading almost always share one span: a span-level
    skip would swallow the heading with it, and refusing to skip would count the
    frontmatter as assertions. Its answer comes from
    :func:`ciao.note_entries.frontmatter_span` — the parser's, not a second
    frontmatter rule here.

    Returns ``(runs, characters)``, the second counting only the assertion lines
    rather than their surrounding blank lines, so a coverage figure is not
    inflated by the newlines between them.
    """
    front = ne.frontmatter_span(text)
    start_at, stop_at = front if front is not None else (0, 0)
    runs = 0
    characters = 0
    open_run = False
    for begin, finish in document.uncovered:
        slice_ = text[begin:finish]
        if not slice_.strip():
            open_run = False
            continue
        offset = begin
        for line in slice_.split("\n"):
            position = offset
            offset += len(line) + 1
            bare = line.strip()
            if not bare:
                # A blank line ends a run without counting as one: a gap between
                # two paragraphs is not a third place the detector failed.
                open_run = False
                continue
            if front is not None and start_at <= position < stop_at:
                continue
            if _line_asserts(bare):
                if not open_run:
                    runs += 1
                    open_run = True
                characters += len(bare)
            else:
                open_run = False
    return runs, characters


def note_entry_coverage(
    text: str,
    *,
    note_type: str = "",
    updated: str = "",
    mtime: float | None = None,
    note_path: str = "",
    path_prefix: Path | None = None,
    rendered: str = "",
    title: str = "",
    workspace: str = "",
    today: datetime.date | None = None,
    registry: EntityTypeRegistry | None = None,
) -> tuple[NoteEntryCoverage, tuple[EntryVerdict, ...], ne.EntryDocument]:
    """One note's entries, aged on their own dates, and that note's coverage.

    The per-note core of :func:`find_stale_entries`, and the same function the
    curation worklist's entry pass calls — which is the whole point of it being
    here rather than beside the detector: a plan that aged entries its own way
    would list work the detector does not, and a detector that aged them its own
    way would show a note the plan is not going to ask about. One function, so
    the two cannot drift.

    The note's own age comes from :func:`note_verification` — the shared
    predicate, with the same aliases, exempt types and frontmatter-then-mtime
    rule the whole-note surfaces use — and only acts as the *default* an entry
    inherits. An entry with a valid `[verified:]` stamp is aged from that day
    alone; the note's date is what an unstamped or unusable-stamp entry falls back
    to, and ``EntryVerdict.own_date`` records which one it was.

    Inheriting is what makes an unstamped entry *current* when the note is: the
    two are the same claim at two widths, so a note re-stamped yesterday and its
    unstamped bullets agree. A bullet that inherits a date inside the horizon is
    counted as checked-and-current and selected for nothing; only an unstamped
    bullet past the horizon, or one with no date anywhere to age it from, is
    reported as unverified. An unusable stamp is always reported, whatever the
    note's date says, because a stamp that cannot be read is a claim nobody can
    act on and the note reads as verified anyway.

    Returns the coverage, the entries that were **selected** — the ones whose
    own date is past the horizon, plus the ones nobody ever verified — and the
    parse itself. The document travels because a caller holding a check state has
    to answer "does this check still describe an entry the note holds?", and the
    identities and fingerprints that answers it with are in there; a second parse
    to get them would be a second read of a body already in hand. Fresh entries,
    exempt entries and notes of an exempt type contribute counts and no verdict,
    which is what keeps a finding list short enough to read.
    """
    from ciao import memory_receipts as mr

    current = today or datetime.date.today()
    prefix = Path("memory-vault") if path_prefix is None else Path(path_prefix)
    note_type = str(note_type or "").strip()
    verification = note_verification(
        note_type, updated, mtime, today=current, registry=registry
    )
    relative = note_path
    if not relative:
        try:
            relative = Path(rendered).relative_to(prefix).as_posix()
        except ValueError:
            relative = Path(rendered or "").as_posix()
    document = ne.parse_note_entries(
        text, note_path=relative, workspace=workspace, today=current
    )
    threshold = (
        verification.threshold_days
        if verification is not None
        else note_threshold_days(_stale_type_key(note_type, registry=registry), registry=registry)
    )
    note_exempt = verification.exempt if verification is not None else is_stale_exempt_type(
        note_type, registry=registry
    )
    revision = mr.content_revision(text)
    shown = title or relative

    checked = exempt = unverified = stale = 0
    unsupported = 0
    selected: list[EntryVerdict] = []
    for entry in document.entries:
        if note_exempt:
            exempt += 1
            continue
        if not entry.supported:
            # Reported as uncovered rather than judged: a bullet carrying a
            # nested code block or a second block is text this entry model does
            # not describe, and hashing it would be a verdict about a shape the
            # detector cannot describe. It is never counted as verified.
            unsupported += 1
            continue
        event = _entry_event_reason(entry)
        if event:
            exempt += 1
            continue
        checked += 1
        own = entry.verified
        dated = own if own is not None else (
            verification.last_verified if verification is not None else None
        )
        age = (current - dated).days if dated is not None else None
        if entry.stamp is None:
            code = STALE_ENTRY_NO_STAMP
        elif not entry.stamp.valid:
            code = STALE_ENTRY_BAD_STAMP
        else:
            code = STALE_ENTRY_AGED
        if code == STALE_ENTRY_BAD_STAMP:
            # Always selected. A stamp that cannot be believed is not an old
            # check and not a fresh one — it is nobody having checked, and it is
            # the one case a reader most needs told, because the note reads as
            # verified and is not.
            unverified += 1
        elif age is None or age >= threshold:
            # Past the horizon, or with no date anywhere to age it from. Both are
            # real work; neither is an artefact of the note having been touched
            # yesterday.
            if code == STALE_ENTRY_NO_STAMP:
                unverified += 1
            else:
                stale += 1
        else:
            # Inside the horizon. A stamped entry is genuinely current, and an
            # unstamped one **inherits** the note's date — so it is current
            # exactly as far as the note is, and counting it as `unverified`
            # would make every bullet in a vault written before `[verified:]`
            # stamps existed read as never checked. Selecting those regardless
            # is what filled a nightly plan with one-day-old entries: the note
            # was re-stamped, not the facts.
            continue
        selected.append(
            _verdict_for(
                entry,
                document=document,
                text=text,
                code=code,
                dated=dated,
                own_date=own is not None,
                age=age,
                threshold=threshold,
                note_type=note_type,
                relative=relative,
                rendered=rendered or relative,
                title=shown,
                revision=revision,
            )
        )
    blocks, block_chars = _uncovered_blocks(text, document)
    coverage = NoteEntryCoverage(
        path=rendered or relative,
        relative=relative,
        title=shown,
        note_type=note_type or "note",
        entries=document.entry_count,
        checked=checked,
        exempt=exempt,
        unverified=unverified,
        uncovered=unsupported + blocks,
        stale=stale,
        entry_chars=document.entry_chars,
        uncovered_chars=block_chars,
        total_chars=document.total_chars,
        age_days=verification.age_days if verification is not None else None,
        threshold_days=threshold,
        last_verified=verification.last_verified if verification is not None else None,
        note_exempt=note_exempt,
        diagnostics=document.diagnostics,
    )
    return coverage, tuple(selected), document


def _verdict_for(
    entry: ne.NoteEntry,
    *,
    document: ne.EntryDocument,
    text: str,
    code: str,
    dated: datetime.date | None,
    own_date: bool,
    age: int | None,
    threshold: int,
    note_type: str,
    relative: str,
    rendered: str,
    title: str,
    revision: str,
) -> EntryVerdict:
    """One selected entry, with the sentence a reader needs beside the code."""
    stamp = entry.stamp
    if code == STALE_ENTRY_NO_STAMP:
        reason = (
            "nobody has recorded a [verified:] check on this entry"
            + (
                f"; it is unverified for {age}d on the note's own last-verified date"
                if age is not None
                else ", and the note has no usable verification date either"
            )
        )
    elif code == STALE_ENTRY_BAD_STAMP:
        why = stamp.reason if stamp is not None else "malformed"
        reason = (
            f"the [verified:] stamp on this entry is unusable ({why}), "
            "so nobody has recorded a check on it"
        )
    elif age is None:
        reason = "this entry carries a check but there is no date to age it from"
    else:
        reason = f"unverified for {age}d against a {threshold}d horizon"
    return EntryVerdict(
        identity=entry.identity,
        note_path=relative,
        path=rendered,
        title=title,
        note_type=note_type or "note",
        start=entry.start,
        end=entry.end,
        line_number=entry.line_number,
        section=entry.section,
        fingerprint=entry.fingerprint,
        revision=revision,
        excerpt=_excerpt(entry.text),
        context=_context_for(text, entry),
        last_verified=dated,
        own_date=own_date,
        age_days=age,
        threshold_days=threshold,
        reason_code=code,
        reason=reason,
        exempt=False,
        supported=True,
    )


def find_stale_entries(
    entries: list[Any],
    *,
    vault_root: Path | None = None,
    path_prefix: Path | None = None,
    workspace: str = "",
    mtimes: dict[str, float] | None = None,
    texts: dict[str, str] | None = None,
    today: datetime.date | None = None,
    registry: EntityTypeRegistry | None = None,
) -> dict[str, Any]:
    """The entries in a vault whose facts have gone unverified past their horizon.

    The entry-level sibling of :func:`find_stale_notes`, over the same
    :class:`ciao.vault_index.Entry` list, and the function every entry-aware
    surface should call: the curation worklist's ``stale_entry`` pass, the
    memory-audit report, the Memory Map's per-node coverage and the review
    queue's entry candidates. ``workspace`` is the *registered* workspace name,
    and it is an argument rather than read off ``vault_root.name`` for the reason
    :func:`ciao.note_entries.entry_identity` makes it one: the identity digests
    it, every consumer resolves the vault through the registry, and a directory
    name is not that name on every registered layout. An identity minted from the
    wrong coordinate names nothing, and the failure is silent.

    ``mtimes`` is the caller's own mtime map when it has one, because
    :func:`ciao.vault_index.scan_vault` has already read every note and the
    detector is the one place that needs the date. Left out, mtimes are probed
    exactly as ``find_stale_notes`` probes them. ``texts`` is the same idea for
    the bodies: a caller that has already read them (the entry pass, which reads
    every note anyway) hands them over rather than paying a second decode, and a
    caller with no vault at all can still use this as a pure seam over text it
    built by hand.

    The result carries the findings, the four counts that make an empty list
    honest (``checked``/``exempt``/``unverified``/``uncovered``), the per-note
    coverage rows, the parser's diagnostics, and the notes whose body could not
    be read at all. A note whose body is one table appears with ``uncovered``
    and no findings — which is the answer, and the only answer that does not read
    as a clean note.
    """
    current = today or datetime.date.today()
    if registry is None and vault_root is not None:
        from ciao.entity_types import load_entity_types

        registry = load_entity_types(Path(vault_root).resolve())
    prefix = Path("memory-vault") if path_prefix is None else Path(path_prefix)
    root = Path(vault_root) if vault_root is not None else None

    def _body(rendered: str, relative: str) -> str | None:
        """One note's text, from the caller's map or off the vault.

        ``None`` when neither can produce it, and the caller reports that as
        uncovered rather than as clean: a note this detector could not read is a
        note it knows nothing about, and "no findings" is the one answer that
        would read as a verdict.
        """
        if texts is not None:
            found = texts.get(rendered, texts.get(relative))
            if found is not None:
                return found
        if root is None:
            return None
        try:
            return (root / relative).read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            return None

    def _mtime(rendered: str, relative: str) -> float:
        if mtimes is not None:
            return mtimes.get(rendered, mtimes.get(relative, 0.0))
        if root is None:
            return 0.0
        try:
            return (root / relative).stat().st_mtime
        except OSError:
            return 0.0

    findings: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    unreadable: list[str] = []
    checked = exempt = unverified = uncovered = 0
    diagnostics: list[dict[str, str]] = []
    for entry in entries:
        rendered = str(entry.path)
        relative = _vault_relative_to(rendered, prefix)
        text = _body(rendered, relative)
        if text is None:
            # Unreadable is not clean. A note this detector could not read is
            # reported as such and contributes no findings, which is the same
            # recoverable direction as a note with no usable date: re-asking costs
            # a pass, while inventing a verdict costs the reader their trust.
            unreadable.append(rendered)
            continue
        coverage, selected, _document = note_entry_coverage(
            text,
            note_type=(entry.type or "").strip(),
            updated=entry.updated or "",
            mtime=_mtime(rendered, relative),
            note_path=relative,
            path_prefix=prefix,
            rendered=rendered,
            title=str(entry.title or "") or relative,
            workspace=workspace,
            today=current,
            registry=registry,
        )
        checked += coverage.checked
        exempt += coverage.exempt
        unverified += coverage.unverified
        uncovered += coverage.uncovered
        for code in coverage.diagnostics:
            diagnostics.append({"path": rendered, "diagnostic": code})
        if not (coverage.checked or coverage.uncovered or coverage.entries):
            continue
        notes.append(coverage.as_evidence())
        findings.extend(verdict.as_finding() for verdict in selected)

    # Oldest first, then path, then the entry's own position: the note-level
    # order with its tiebreak extended one level in, so two runs over the same
    # vault produce the same list and a short budget drops the youngest fact
    # rather than an arbitrary one. An entry with no date at all sorts after
    # every dated one, because there is no claim about how old it is to violate.
    findings.sort(
        key=lambda row: (
            -(row["age_days"] if row["age_days"] is not None else -1),
            str(row["path"]),
            int(row["start"]),
        )
    )
    return {
        "stale_entries": findings,
        "entries_checked": checked,
        "entries_exempt": exempt,
        "entries_unverified": unverified,
        "entries_uncovered": uncovered,
        "entry_coverage": notes,
        "entry_diagnostics": diagnostics,
        # Reported rather than counted, because a note that could not be read
        # has no entries to count and must not be folded into any of the four
        # totals: a caller that wants "how much of this vault did the detector
        # actually see" reads this list's length beside them.
        "notes_unreadable": unreadable,
    }


def _vault_relative_to(rendered: str, prefix: Path) -> str:
    """One rendered entry path as the vault-relative spelling, total.

    The same answer :func:`ciao.vault_index._strip_prefix` gives, and the same
    one :func:`find_stale_notes` reaches for: a rendered path under the prefix
    loses it, and a path that is not under the prefix — a hand-built entry in a
    test — is already relative. Refusing to report a note because its path was
    spelled unusually would be a detector that finds nothing in exactly the
    vaults a caller most needs it for.
    """
    try:
        return Path(rendered).relative_to(prefix).as_posix()
    except ValueError:
        return Path(rendered).as_posix()
