"""Ownership of proposal queue kinds and their accept semantics.

Three modules once each defined their own regex over the same proposal-queue
file, and the three disagreed: ``agent_assets`` never matched ``[profile]``,
and none matched ``[rehome]`` (written by :mod:`ciao.vault_rehome`). On the
reference vault that left 64 real queue rows invisible to every counter.

The fix is to own the kinds in exactly one place. Adding a producer kind is a
single edit to :data:`KINDS`; the bullet regex is derived from that table so a
new kind is matched and counted everywhere without a third copy to update.

Each kind also declares its own accept semantics (decision D3 of the agent
roots plan). ``[rehome]`` is a file move with a destination and a reason, not
a bounded-region edit, so its accept descriptor is a distinct type and can
never be mistaken for an "add to memory" action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

KINDS: tuple[str, ...] = ("memory", "profile", "user", "rehome")
"""Ordered registry of proposal kinds. Adding a kind here is the only edit a
new producer needs; the regex below derives from this table."""

# One alternation for the whole table, escaped so a kind with regex metachar
# still matches literally. Case-insensitive so the producers' casing never
# silently hides a row from the counters.
_KIND_ALT = "|".join(re.escape(k) for k in KINDS)

# The bullet shape all three old regexes shared: optional leading whitespace,
# a dash, whitespace, the bracketed kind, whitespace, then non-empty content.
# The trailing `_(from: …)_` source tag, when present, is captured separately.
BULLET_RE = re.compile(
    rf"^\s*-\s*\[({_KIND_ALT})\]\s+(.+?)(?:\s+_\(from:\s*(.+?)\)_)?\s*$",
    re.IGNORECASE,
)
"""Public shared bullet pattern. Consumers import this, never a private copy:
three private copies drifting apart is the defect this module removes."""


class UnknownKindError(KeyError):
    """Lookup for a kind that is not in the registry."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"unknown proposal kind: {kind!r}")


@dataclass(frozen=True, slots=True)
class ProposalBullet:
    """One matched queue bullet, split into its structural parts."""

    kind: str
    text: str
    source: str = ""


def parse_bullet(line: str) -> ProposalBullet | None:
    """Parse one queue line into a :class:`ProposalBullet`, or None.

    Returns None for any line that is not a proposal bullet for a registered
    kind (headings, blank lines, prose, an unregistered kind). It never
    reports a silent zero for a kind that exists; unknown kinds are covered by
    the lookup guard on the accept table.
    """
    match = BULLET_RE.match(line)
    if match is None:
        return None
    return ProposalBullet(
        kind=match.group(1),
        text=match.group(2).strip(),
        source=(match.group(3) or "").strip(),
    )


# ── Accept descriptors ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RegionAccept:
    """Accept a proposal by editing one bounded region of the guide."""

    action: Literal["edit_region"] = "edit_region"
    region: Literal["memory", "profile"] = "memory"


@dataclass(frozen=True, slots=True)
class RehomeAccept:
    """Accept a proposal by moving a file to a destination, with a reason.

    Structurally a different descriptor than :class:`RegionAccept`, so a
    caller branching on the descriptor type can never route a rehome through
    the region-edit path.
    """

    action: Literal["move_file"] = "move_file"
    destination: str = ""
    reason: str = ""


_ACCEPT: dict[str, RegionAccept | RehomeAccept] = {
    "memory": RegionAccept(region="memory"),
    "profile": RegionAccept(region="profile"),
    # "user" is the legacy queue label for the ciao:profile region. Its accept
    # is the same region edit as [profile]; the label is preserved on the
    # bullet and normalized only at resolve time (control_plane maps it via
    # memory_tool.resolve_region).
    "user": RegionAccept(region="profile"),
    "rehome": RehomeAccept(destination="", reason=""),
}


def accept_for(kind: str) -> RegionAccept | RehomeAccept:
    """The accept descriptor for a kind.

    Raises :class:`UnknownKindError` for an unregistered kind. A silent zero
    here is exactly the class of bug this module exists to remove, so the
    lookup never returns None."""
    if kind not in _ACCEPT:
        raise UnknownKindError(kind)
    return _ACCEPT[kind]


def kinds() -> tuple[str, ...]:
    """Registered kinds as a public tuple (a mutable copy-free view)."""
    return KINDS
