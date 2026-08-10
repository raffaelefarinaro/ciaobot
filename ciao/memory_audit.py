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

Deliberately model-free. A model asked to tally a few hundred entries returns a
confident number, and a different one tomorrow. The detectors here count; the
curation routine that consumes them judges. That means they are tuned for
precision over recall: a detector that cries wolf trains the reader to skip the
report, which is worse than a detector that stays quiet.
"""

from __future__ import annotations

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
    ("quoted-user-turn", re.compile(r"\bUser\s+(?:said|asked|wrote|replied)\b", re.IGNORECASE)),
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
            r"switched|fixed|updated|replied|noted|clarified)\b",
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


def audit_entries(
    region_entries: dict[str, list[str]],
    *,
    workspace_dir: Path,
) -> dict[str, Any]:
    """Run every rot detector over the bounded-memory regions.

    ``region_entries`` maps a region name to its parsed entries, as
    :func:`ciao.memory_tool.read_region` returns them.
    """
    event_shaped: list[dict[str, Any]] = []
    stale_paths: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
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

    return {
        "event_shaped_entries": event_shaped,
        "stale_path_entries": stale_paths,
        "superseded_state_candidates": superseded,
        "paths_checked": checked,
        "paths_unverifiable": unverifiable,
    }
