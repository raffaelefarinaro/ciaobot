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
never be mistaken for an "add to memory" action. The destination kinds added
by the proposal-routing work follow the same rule: ``[project <doc-path>]``,
``[people <Name>]``, and
``[learnings]`` each carry a descriptor of their own, and ``[review]`` — the
"the model was not sure" bucket — deliberately accepts only by manual routing,
so no one-click action can guess a destination for it.

``[category <id>]`` is the seventh: the owner added categories by hand until
issue #647 made the vault propose them, and accepting one appends an entry to
the category registry and retypes the notes that were already typed that way.
The id is the bullet's payload, the way ``[people]``'s name is; the label,
folder, description and the note list itself live in a sidecar beside the
queue, because a queue bullet is one line and a list of paths is not.

``[note_edit <id>]`` is the eighth and follows the same shape: a note
verification that could not be applied unattended (#726-B) is queued as a row a
person decides, and the operation — a whole-note replacement, a verification
re-stamp, or a retirement — plus the before/after images and the evidence live
in a sidecar, because none of that is one line. Its accept is its own
descriptor rather than a region edit or a file move, so a caller branching on
the descriptor cannot route a whole-note rewrite into the workspace guide.

Kinds may carry a payload inside the brackets (``[people Mo]``,
``[project ./projects/x/doc.md]``, ``[category recipe-book]``). The payload is
exposed as :attr:`ProposalBullet.target`; legacy bullets without one parse
unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

KINDS: tuple[str, ...] = (
    "memory",
    "profile",
    "user",
    "rehome",
    "project",
    "people",
    "learnings",
    "review",
    "category",
    "note_edit",
)
"""Ordered registry of proposal kinds. Adding a kind here is the only edit a
new producer needs; the regex below derives from this table."""

# One alternation for the whole table, escaped so a kind with regex metachar
# still matches literally. Case-insensitive so the producers' casing never
# silently hides a row from the counters.
_KIND_ALT = "|".join(re.escape(k) for k in KINDS)

# The bullet shape all three old regexes shared: optional leading whitespace,
# a dash, whitespace, the bracketed kind, whitespace, then non-empty content.
# An optional payload sits inside the brackets after the kind word and is
# captured verbatim (paths and names are written by producers, never free
# user prose). The trailing `_(from: …)_` source tag, when present, is captured
# separately, and a `_(request: …)_` tag after it names the user request a
# ``/remember`` of a lesson belongs to.
#
# The request group is second and optional, so a bullet written before it
# existed parses unchanged and the source tag keeps its group number. It is a
# separate field rather than more text in the source because the two are
# different facts — one is a transcript somebody can re-read, the other is a
# request identifier — and a request id folded into the source would read back
# as a chat id.
BULLET_RE = re.compile(
    rf"^\s*-\s*\[({_KIND_ALT})(?:[ \t]+([^\]]*))?\]\s+(.+?)"
    rf"(?:\s+_\(from:\s*(.+?)\)_)?"
    rf"(?:\s+_\(request:\s*(.+?)\)_)?\s*$",
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
    target: str = ""
    request: str = ""


def parse_bullet(line: str) -> ProposalBullet | None:
    """Parse one queue line into a :class:`ProposalBullet`, or None.

    Returns None for any line that is not a proposal bullet for a registered
    kind (headings, blank lines, prose, an unregistered kind). It never
    reports a silent zero for a kind that exists; unknown kinds are covered by
    the lookup guard on the accept table.

    The kind is lowercased to the registry spelling. ``BULLET_RE`` is
    case-insensitive (so a producer's casing never hides a row from the
    counters) but :data:`_ACCEPT` is keyed by the lowercase names in
    :data:`KINDS`, so returning ``match.group(1)`` verbatim let the regex and
    the lookup disagree: ``- [Profile] x`` matched, then ``accept_for("Profile")``
    raised :class:`UnknownKindError`. The queue scan in
    ``ciao.web.routes_api`` calls ``accept_for`` unguarded, so one capitalised
    bullet 500'd ``/api/proposals`` and emptied the whole review queue in the
    app. Normalising here keeps that impossible for every consumer at once.
    """
    match = BULLET_RE.match(line)
    if match is None:
        return None
    return ProposalBullet(
        kind=match.group(1).lower(),
        text=match.group(3).strip(),
        source=(match.group(4) or "").strip(),
        target=(match.group(2) or "").strip(),
        request=(match.group(5) or "").strip(),
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


@dataclass(frozen=True, slots=True)
class DocFoldAccept:
    """Accept a `[project]` proposal by folding it into a canonical doc.

    ``doc_path`` is the path exactly as the producer wrote it in the payload;
    resolvers turn it into an absolute path at accept time.
    """

    action: Literal["fold_doc"] = "fold_doc"
    doc_path: str = ""


@dataclass(frozen=True, slots=True)
class PeopleAccept:
    """Accept a `[people]` proposal by writing/updating a person note."""

    action: Literal["write_people_note"] = "write_people_note"
    name: str = ""


@dataclass(frozen=True, slots=True)
class LearningsAccept:
    """Accept a `[learnings]` proposal by appending to Workspace/Learnings.md."""

    action: Literal["append_learnings"] = "append_learnings"


@dataclass(frozen=True, slots=True)
class ReviewAccept:
    """A `[review]` row has no known destination, so nothing is automatic.

    Accepting one means deciding what it is first; the descriptor exists so
    every kind has an entry in the table (a missing entry raises) while still
    being distinguishable from every actionable type.
    """

    action: Literal["route_manually"] = "route_manually"


@dataclass(frozen=True, slots=True)
class CategoryAccept:
    """Accept a `[category]` proposal by adding it to the category registry.

    The fields declare the SHAPE of what this accept acts on, and their values
    are resolved at accept time from the row and the sidecar, exactly as
    ``DocFoldAccept.doc_path`` comes from the bullet and ``PeopleAccept.name``
    from its payload. What a category adds is not one file: it is a registry
    entry plus a list of notes to retype, which is why the whole set is named
    here even though the queue bullet carries only the id. Naming them is what
    keeps a caller branching on the descriptor from reaching for a file it does
    not have.
    """

    action: Literal["add_category"] = "add_category"
    category_id: str = ""
    label: str = ""
    folder: str = ""
    description: str = ""
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NoteEditAccept:
    """Accept a `[note_edit]` proposal by rewriting or retiring ONE note.

    A note verification that the autonomy rule would not apply unattended comes
    back as a decision for a person, and this is the shape of that decision. The
    fields name what the accept acts on and nothing else, for the reason
    :class:`CategoryAccept` names its set: a queued bullet is one line, so the
    operation, the exact replacement text and the evidence behind it are in a
    sidecar keyed by ``sidecar_id``, resolved at accept time.

    The two names are deliberately different. ``relative_path`` is the
    vault-relative note the row is about — what a reader needs to see on the row
    — and ``sidecar_id`` is the only thing that names the operation. A caller
    that reached for a note without a sidecar id would have a file and no
    instruction, which is how a whole-note rewrite becomes a guess.
    """

    action: Literal["note_edit"] = "note_edit"
    relative_path: str = ""
    sidecar_id: str = ""


AcceptDescriptor = (
    RegionAccept
    | RehomeAccept
    | DocFoldAccept
    | PeopleAccept
    | LearningsAccept
    | ReviewAccept
    | CategoryAccept
    | NoteEditAccept
)

_ACCEPT: dict[str, AcceptDescriptor] = {
    "memory": RegionAccept(region="memory"),
    "profile": RegionAccept(region="profile"),
    # "user" is the legacy queue label for the ciao:profile region. Its accept
    # is the same region edit as [profile]; the label is preserved on the
    # bullet and normalized only at resolve time (control_plane maps it via
    # memory_tool.resolve_region).
    "user": RegionAccept(region="profile"),
    "rehome": RehomeAccept(destination="", reason=""),
    "project": DocFoldAccept(),
    "people": PeopleAccept(),
    "learnings": LearningsAccept(),
    "review": ReviewAccept(),
    "category": CategoryAccept(),
    "note_edit": NoteEditAccept(),
}


def accept_for(kind: str) -> AcceptDescriptor:
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
