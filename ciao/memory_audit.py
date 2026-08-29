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

Deliberately model-free. A model asked to tally a few hundred entries returns a
confident number, and a different one tomorrow. The detectors here count; the
curation routine that consumes them judges. That means they are tuned for
precision over recall: a detector that cries wolf trains the reader to skip the
report, which is worse than a detector that stays quiet.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

# Matches the excerpt width os_audit already uses for memory findings, so the
# Automation page and the audit markdown do not disagree on truncation.
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
# `[YYYY-MM-DD]` learned-at stamp is system time: when auto-promotion wrote
# the entry. Both are read here as aging evidence for the curation routine to
# re-verify — informational, like every age signal in this module, because
# age alone is never a defect.

_AS_OF_RE = re.compile(r"\[as-of:\s*(\d{4}-\d{2}-\d{2})\]")
_LEARNED_STAMP_RE = re.compile(r"\s*\[(\d{4}-\d{2}-\d{2})\]\s*$")

# An `[as-of]` fact declares itself a snapshot, so it ages fast; a plain
# learned-at entry claims to be standing state and gets the default horizon
# vault notes use.
AS_OF_AGING_DAYS = 90
LEARNED_AGING_DAYS = 180


def strip_learned_stamp(entry: str) -> str:
    """The entry text without its trailing learned-at stamp.

    Promotion dedupe compares through this: the same fact promoted on two
    different days must still count as a duplicate.
    """
    return _LEARNED_STAMP_RE.sub("", entry).rstrip()


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
        try:
            stamped = datetime.date.fromisoformat(raw)
        except ValueError:
            # Shape-valid but impossible date; the expiration-tag checks own
            # malformed-stamp reporting, aging must not guess.
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


def note_threshold_days(note_type: str) -> int:
    """Days of silence after which this note type counts as unverified."""
    return STALE_NOTE_THRESHOLDS_DAYS.get(note_type, STALE_NOTE_DEFAULT_DAYS)


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


def find_stale_notes(
    entries: list[Any],
    *,
    vault_root: Path | None = None,
    path_prefix: Path | None = None,
    mtimes: dict[str, float] | None = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Vault notes whose facts have gone unverified past their type's horizon.

    ``entries`` are :class:`ciao.vault_index.Entry` objects (anything with
    ``path``/``title``/``type``/``updated`` works). Age comes from frontmatter
    ``updated:`` when present, else file mtime — supplied per rendered path via
    ``mtimes``, or stat'ed here by stripping ``path_prefix`` off the rendered
    path and joining onto ``vault_root``.

    Precision-first like every detector in this module: exempt event types,
    report age and threshold side by side so a reader can disagree with the
    verdict without losing the evidence, and return checked/exempt counts so
    an empty list is not mistaken for full coverage.
    """
    current = today or datetime.date.today()
    prefix = Path("memory-vault") if path_prefix is None else Path(path_prefix)
    findings: list[dict[str, Any]] = []
    checked = 0
    exempt = 0

    for entry in entries:
        note_type = (entry.type or "").strip()
        if note_type in STALE_NOTE_EXEMPT_TYPES:
            exempt += 1
            continue
        threshold = note_threshold_days(note_type)
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
        verified, source = note_last_verified(entry.updated, mtime)
        if verified is None:
            # Unverifiable is not stale: calling it so would be a guess.
            continue
        checked += 1
        age_days = (current - verified).days
        if age_days < threshold:
            continue
        findings.append(
            {
                "path": rendered,
                "title": entry.title,
                "type": note_type or "note",
                "age_days": age_days,
                "threshold_days": threshold,
                "last_verified": verified.isoformat(),
                "source": source,
            }
        )

    findings.sort(key=lambda f: (-f["age_days"], f["path"]))
    return {
        "stale_notes": findings,
        "notes_checked": checked,
        "notes_exempt": exempt,
    }
