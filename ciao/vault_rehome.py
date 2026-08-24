"""Re-home person notes a per-workspace curation bug filed in the wrong vault.

Why this exists
---------------
Memory curation used to run once, globally, with one workspace's vault as its
write target, so every contact it learned about — work colleagues included —
landed in that one workspace's `People/`. The routing bug is fixed (curation is
per-workspace now), but the backlog it produced is still on disk: on the vault
this was measured against, 95 notes sit in `personal/People/` and most of them
are work contacts. Nothing else in the app moves a note between workspaces, so
without this the only fix is by hand, one note and one inbound link at a time.

Tag-obvious versus judgement
----------------------------
The split is the whole design. A note tagged `scandit`/`colleague`/`customer`
already says which workspace it belongs to, so re-filing it is a mechanical
substitution with no decision in it — the same bar `vault_migration` uses for
"apply aliased renames, report the rest". A note with **no** workspace-naming tag
is a real judgement call about someone's relationship to the user, which a
migration has no business guessing: those are written to the workspace's
`Workspace/Memory-Proposals.md` queue (the promote/dismiss surface that already
exists) and **never moved**.

The signal is a tag → *role* map, not a tag → directory map. Workspace names
belong to the user: an install may have `home`/`office`, or one workspace, or
five, and none named `work`. `TAG_WORKSPACE_ROLES` names roles, and
:func:`resolve_role_workspaces` binds each role to whichever registered
workspace actually plays it. When a role binds to nothing, notes carrying its
tags are simply not candidates — there is nowhere to move them, and inventing a
directory is worse than leaving them where they are.

Moving a note means moving its edges
------------------------------------
A vault is its links, so a move that leaves the links behind trades 95 misfiled
notes for 70 broken ones. Two dialects have to survive the move, because a vault
may be mid-conversion (see `vault_migrate_links`):

* `[[personal/People/Mo]]`, `[[People/Mo|Mo]]`, `[[Mo]]` — resolved through the
  same `_build_filename_index`/`_resolve_related` pair the index uses, then
  rewritten to the **full** new vault-relative ref. A bare `[[Mo]]` would keep
  resolving on its own (the index also keys the filename stem), but spelling the
  path out is what makes the link survive a second note called `Mo` later.
* `[Mo](../People/Mo.md)` — a relative destination changes when *either* end
  moves, so the pass recomputes it against the linking note's directory. That
  also covers the moved note's **own** outbound links: `../projects/x.md` from
  `personal/People/` is not the same path from `work/People/`.
* Frontmatter refs naming the moved path, bare (`related: personal/People/Mo`)
  or still wikilinked, under any key.

Code spans, escaped brackets, images, `Logs/`, `Templates/` and the regenerated
files are skipped by importing the existing predicates rather than re-deriving
them — a second opinion about what counts as code is how a migration eats a code
sample.

Reversibility
-------------
`<runtime_root>/migration/vault-rehome.json` records every move as an exact
`from`/`to` pair and every link rewrite as an `(offset, from, to)` triple in the
*rewritten* text, so :func:`unrehome_people` is an inverse rather than a
re-derivation: it verifies each span still reads as the receipt says before
touching it, leaves a file completely alone on any mismatch, and only then moves
the notes back. Proposals already written to a review queue are deliberately
*not* withdrawn: they are the user's to resolve, and a note the user has since
promoted must not be silently un-proposed.

Which means the map may never *narrow*. A run that could not write some note or
could not move it records `status: "partial"` — so nothing reads the vault as
re-homed and the retry is not refused — and each later run adds its entries to
the map rather than replacing it, keeping every note an earlier run touched
restorable. Only entries a later run superseded are dropped: rewrites for a note
this run rewrote itself, whose offsets it has just shifted, and a move whose
destination this run moved away from again.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import shutil
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.memory_proposals import MemoryProposal, append_proposals
from ciao.vault_index import (
    DIR_TYPE_MAP,
    EXCLUDED_TOP_DIRS,
    Entry,
    FENCED_CODE_RE,
    FRONTMATTER_RE,
    INLINE_CODE_RE,
    MARKDOWN_LINK_RE,
    _build_filename_index,
    _FM_LIST_ITEM_RE,
    _FM_RELATED_KEY_RE,
    _is_link_start,
    _resolve_related,
    markdown_destination,
    resolve_vault_link,
    scan_targets,
    scan_vault,
    vault_link_ref,
)
from ciao.vault_lint import _is_escaped
from ciao.vault_migrate_links import (
    WIKILINK_RE,
    _is_skipped,
    _parse_wikilink,
    _run_git,
)

logger = logging.getLogger(__name__)

RECEIPT_NAME = "vault-rehome.json"
RECEIPT_VERSION = 1

VAULT_PREFIX = "memory-vault"

# The folder a person note lives in, taken from the directory→type map so the two
# cannot drift: `_infer_type` already treats `People/` as the person folder, and a
# vault that renames it would otherwise be invisible here.
PEOPLE_DIRS = frozenset(name for name, kind in DIR_TYPE_MAP.items() if kind == "person")

# Person-note FILENAMES (not directories, so distinct from `EXCLUDED_TOP_DIRS`)
# that are never candidates for re-homing. `People/User.md` is the operator's own
# identity note: `ciao/memory_proposals.py` names it as the canonical home for
# durable identity facts, and it belongs to the primary workspace by definition.
# Matching is by exact filename, casefolded, so `user.md` and `User.md` are both
# excluded while a note merely containing the word user (say `User-Group-Lead.md`)
# still moves like any other contact.
EXCLUDED_PERSON_FILENAMES = frozenset({"user.md"})

# Tag -> the workspace *role* the tag implies.
#
# Roles, not names. `scandit` cannot point at a directory called `work`, because
# workspace names are the user's and an install may have neither `personal` nor
# `work` (see `config.primary_workspace`'s docstring for the same rule). Every
# lookup here is casefolded. Keep this list short and unambiguous: a tag that
# only *usually* means work belongs in the judgement bucket, not in this map.
TAG_WORKSPACE_ROLES: dict[str, str] = {
    "work": "work",
    "colleague": "work",
    "ex-colleague": "work",
    "customer": "work",
    "partner": "work",
    "scandit": "work",
    "personal": "personal",
    "friend": "personal",
    "family": "personal",
}

# Role -> the workspace names that play it, in preference order. A role binds to
# the first of its names that is actually registered; a role that binds to
# nothing takes its tags out of play entirely.
ROLE_WORKSPACE_NAMES: dict[str, tuple[str, ...]] = {
    "work": ("work", "office", "professional"),
    "personal": ("personal", "private", "home"),
}

PROPOSAL_TARGET = "rehome"
PROPOSAL_SOURCE = "vault-rehome"


# ---- receipt ---------------------------------------------------------------


def receipt_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / "migration" / RECEIPT_NAME


def peek_receipt(runtime_root: Path) -> dict[str, Any] | None:
    """Read the receipt file whatever its status.

    Two callers need the reverse map regardless of whether the run that wrote it
    finished: :func:`unrehome_people`, because a partial run's map is still an
    exact inverse for the notes it *did* move and rewrite, and
    :func:`write_receipt`, because a retry has to carry those entries forward.
    Everything that asks "has this vault been re-homed?" wants
    :func:`read_receipt` instead.
    """
    path = receipt_path(runtime_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_receipt(runtime_root: Path) -> dict[str, Any] | None:
    """The receipt of a *completed* re-homing, or None.

    Gates on ``status`` rather than on the file existing, because a run that
    could not write or could not move some note left the vault half re-homed:
    reading that receipt as "already migrated" is what let the migration stop
    short of done and report that it had finished — the normal re-run was
    refused while the note it never moved stayed misfiled with every reference
    to it already repointed at a path it is not at.

    A receipt written before ``status`` existed records a completed run, so a
    missing field reads as ``"migrated"``; anything else would turn every
    install that already did the work into a permanent false positive. That
    tolerance lives here rather than in the callers precisely so the detection
    surfaces can ask this one question and get both halves right.
    """
    data = peek_receipt(runtime_root)
    if data is None:
        return None
    return data if data.get("status", "migrated") == "migrated" else None


def _carry_moves(
    previous: Any, fresh: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The earlier moves this run did not supersede.

    A move is whole-file, so an earlier `from`/`to` pair stays exactly true for
    as long as the note is still at `to` — which is every earlier move except
    one this run moved that same path away from again. Merging is therefore the
    safe default here, and the all-or-nothing rotation it replaces is what left
    a retry's receipt naming only the notes the retry itself moved, with the
    first batch's moves unrecoverable.

    A note reaching `to` and then moving *again* needs its tags rewritten
    between the two runs, since the destination is by construction the workspace
    its tags name. Those are dropped rather than composed into a single
    `from`→`to` hop: the case is unreachable without a hand edit, and dropping
    can only ever match today's behaviour, which drops every earlier move.
    """
    claimed = {str(move.get("from", "")) for move in fresh}
    claimed |= {str(move.get("to", "")) for move in fresh}
    carried: list[dict[str, Any]] = []
    for move in previous if isinstance(previous, list) else []:
        if not isinstance(move, dict):
            continue
        origin, current = str(move.get("from", "")), str(move.get("to", ""))
        if not origin or not current or current in claimed:
            continue
        carried.append(move)
    return carried


def _carry_rewrites(
    previous: Any,
    fresh: Sequence[dict[str, Any]],
    moves: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The earlier spans this run's writes did not invalidate.

    Merging two receipts is safe *per note*, which is the distinction the old
    rotation missed. An offset only means anything against the exact bytes it was
    recorded against, so an entry for a note this run rewrote is stale and is
    dropped — but a note this run left alone is byte for byte as its own entries
    describe it, and those carry over untouched.

    Carried entries are first re-keyed through this run's moves. A move does not
    touch a byte of the note's content, so a span recorded against the old path
    is still exact at the new one; without the re-keying the undo would look for
    it where the note no longer is, fail to read it, and report a mismatch it
    invented. The re-keying is also what makes the drop test correct, since this
    run records its own spans against the path the note *ends up* at.

    The stale ones are dropped rather than shifted arithmetically. Recomputing
    them is not merely fiddly: one mismatched span disqualifies the *whole* file
    in :func:`unrehome_vault_people`, so a wrong entry would take this run's good
    entries for that note down with it. The superseded receipt is archived beside
    the active one either way.
    """
    moved = {
        str(move.get("from", "")): str(move.get("to", ""))
        for move in moves
        if str(move.get("from", "")) and str(move.get("to", ""))
    }
    rewritten = {str(entry.get("path", "")) for entry in fresh}
    carried: list[dict[str, Any]] = []
    for entry in previous if isinstance(previous, list) else []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        if not path:
            continue
        path = moved.get(path, path)
        if path in rewritten:
            continue
        carried.append({**entry, "path": path})
    return carried


def write_receipt(runtime_root: Path, summary: dict[str, Any]) -> Path:
    """Persist a receipt atomically, absorbing any earlier reverse map.

    Written through a `.tmp` sibling and `replace()` so a crash mid-write cannot
    leave a truncated reverse map — a half-written receipt is worse than none,
    because unrehoming would restore part of a file and move nothing back.

    The active receipt carries every run's entries for the notes that run still
    describes, not just the last run's. Replacing it outright lost the earlier
    batch: after a run that could not move one note, the retry that moved it left
    an active map naming only that note, so the notes moved first could no longer
    be put back — precisely the promise the reverse map exists to make, and a
    promise `--force` could break on a vault that was never broken.
    :func:`_carry_moves` and :func:`_carry_rewrites` decide per note which
    earlier entries still hold. The superseded file is kept under a timestamped
    name either way, as the raw record of what one run did.

    ``status`` is ``"partial"`` whenever the run left failures behind. That is
    what stops a half re-homed vault from presenting itself as finished.

    ``needs_judgement`` and ``proposals`` are the current run's alone, not a
    union: every run re-derives the plan, so the newest values already describe
    the whole backlog, and merging frozen snapshots is how the operator tile
    ended up quoting four different counts for one thing.
    """
    path = receipt_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = peek_receipt(runtime_root) or {}
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path.replace(path.with_name(f"{path.stem}.{stamp}{path.suffix}"))
    moves = list(summary.get("moves", []))
    rewrites = list(summary.get("rewrites", []))
    payload = {
        "schema_version": RECEIPT_VERSION,
        "status": "partial" if summary.get("failed") else "migrated",
        "rehomed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "vault_root": summary.get("vault_root", ""),
        "git_head_before": summary.get("git_head_before", ""),
        "moves": [*_carry_moves(previous.get("moves"), moves), *moves],
        "rewrites": [
            *_carry_rewrites(previous.get("rewrites"), rewrites, moves),
            *rewrites,
        ],
        "needs_judgement": summary.get("needs_judgement", []),
        "proposals": summary.get("proposals", []),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def remove_receipt(runtime_root: Path) -> bool:
    """Drop the receipt so a later run is not gated by a reverted one."""
    path = receipt_path(runtime_root)
    try:
        path.unlink()
    except OSError:
        return False
    return True


# ---- git rail --------------------------------------------------------------


def vault_git_state(vault_root: Path, *, touched: Iterable[str] = ()) -> dict[str, Any]:
    """Report whether the notes this operation would write are uncommitted.

    Deliberately scoped to ``touched`` — the exact vault-relative paths the plan
    names — and not to the vault subtree. A whole-subtree check refused to run on
    a vault whose only uncommitted change was an untracked chat log under
    `Logs/`, which this never touches; the rail exists to keep `git checkout` a
    working undo for the *rewritten and moved* files, so anything else being
    dirty is none of its business.

    A vault that is not a repo reports ``dirty: False`` — there is nothing to be
    dirty against, and the receipt is then the only undo.
    """
    root = Path(vault_root)
    state: dict[str, Any] = {"is_repo": False, "head": "", "dirty": False, "dirty_paths": []}
    if not root.is_dir() or shutil.which("git") is None:
        return state
    code, out = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return state
    state["is_repo"] = True
    code, out = _run_git(root, "rev-parse", "HEAD")
    if code == 0:
        state["head"] = out.strip()
    wanted = {str(path) for path in touched}
    if not wanted:
        return state
    code, out = _run_git(root, "status", "--porcelain", "--", ".")
    if code != 0:
        return state
    dirty: list[str] = []
    for line in out.splitlines():
        entry = line[3:].strip().strip('"')
        # Renames report "old -> new"; both ends matter, because either could be
        # a note the plan is about to rewrite.
        candidates = entry.split(" -> ") if " -> " in entry else [entry]
        for candidate in candidates:
            name = candidate.strip().strip('"')
            if name and name in wanted:
                dirty.append(name)
    state["dirty"] = bool(dirty)
    state["dirty_paths"] = sorted(set(dirty))[:20]
    return state


# ---- workspaces and roles --------------------------------------------------


def vault_workspaces(vault_root: Path) -> list[str]:
    """Workspace directories present in the vault, when no registry is given.

    `Logs/`, `Templates/`, dotfolders and the *note-type* folders of a legacy
    single-root vault (`People/`, `Projects/`, …) are not workspaces — the same
    distinction `_workspace_of` draws by testing `DIR_TYPE_MAP` membership.
    Callers with a config should pass `config.workspace_names()` instead: the
    registry is the truth, and a stray directory is not a workspace just because
    it exists.
    """
    root = Path(vault_root)
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.startswith(".")
        and child.name not in EXCLUDED_TOP_DIRS
        and child.name not in DIR_TYPE_MAP
    )


def resolve_role_workspaces(workspaces: Sequence[str]) -> dict[str, str]:
    """Bind each role in :data:`TAG_WORKSPACE_ROLES` to a registered workspace.

    Matching is by name, casefolded, against :data:`ROLE_WORKSPACE_NAMES`. A role
    with no registered workspace is absent from the result, which is what makes
    the whole module inert on an install whose workspaces this map cannot name —
    the alternative, guessing that "the other workspace" must be the work one,
    would move a user's notes on the strength of a coin flip.
    """
    lowered = {name.casefold(): name for name in workspaces}
    bound: dict[str, str] = {}
    for role, names in ROLE_WORKSPACE_NAMES.items():
        for candidate in names:
            if candidate in lowered:
                bound[role] = lowered[candidate]
                break
    return bound


# ---- detection -------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One person note that may be sitting in the wrong workspace."""

    path: str  # vault-relative, as it is on disk today
    destination: str  # vault-relative path it would move to ("" if undecidable)
    workspace: str
    target_workspace: str
    bucket: str  # "mechanical" | "needs_judgement"
    reason: str
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "destination": self.destination,
            "workspace": self.workspace,
            "target_workspace": self.target_workspace,
            "bucket": self.bucket,
            "reason": self.reason,
            "tags": list(self.tags),
        }

    def as_proposal(self) -> MemoryProposal:
        """Render as a queue entry mirroring the memory-proposal bullet shape.

        The path is backticked, which also keeps it out of the reference-rewriting
        pass (inline code is excluded): a queue entry is a historical record of
        what was proposed, not an edge that should follow the note around.
        """
        where = f"`{self.destination}`" if self.destination else "another workspace"
        text = (
            f"Re-home `{self.path}` to {where}? "
            f"Uncertain: {self.reason}. Move it and its links with "
            f"`ciao vault-rehome --apply` only after tagging it."
        )
        return MemoryProposal(
            target=PROPOSAL_TARGET, text=text, source_section=PROPOSAL_SOURCE
        )


def _counterpart(
    workspace: str, workspaces: Sequence[str], roles: dict[str, str]
) -> str:
    """The workspace an untagged person note would most plausibly move to.

    Only ever a *suggestion* printed next to a judgement case. Answered from the
    role binding when the note's workspace plays a known role, else from a
    two-workspace install having exactly one other candidate. "" when neither
    holds — an unlabelled guess among three workspaces is noise.
    """
    role_of = {name: role for role, name in roles.items()}
    role = role_of.get(workspace, "")
    if role:
        others = [name for other, name in roles.items() if other != role and name != workspace]
        if len(others) == 1:
            return others[0]
    others = [name for name in workspaces if name != workspace]
    return others[0] if len(others) == 1 else ""


def _scanned_people(
    root: Path,
    entries: list[Entry] | None,
    targets: Sequence[tuple[Path, str, Path]] | None,
) -> list[tuple[Entry, str, Path, Path]]:
    """(entry, workspace stamp, rendered prefix, path on disk) per note.

    ``targets`` comes from ``CiaoConfig.vault_scan_targets()``: one shared vault
    before the re-rooting and one per agent root after. Without it, today's
    single-vault behaviour, so every existing caller is unchanged.

    The path on disk is carried because ``Entry.path`` is the rendered identity,
    not a location: under per-root targets it is prefixed with the workspace name
    and no such directory exists. A caller that needs to re-read the note — the
    linked-counterpart check does, since the scan drops cross-workspace refs —
    cannot reconstruct it from the entry alone.
    """
    if targets:
        out: list[tuple[Entry, str, Path, Path]] = []
        for vault, stamp, prefix in targets:
            if not Path(vault).is_dir():
                continue
            for entry in scan_vault(Path(vault), workspace=stamp, path_prefix=Path(prefix)):
                try:
                    source = Path(vault) / entry.path.relative_to(prefix)
                except ValueError:
                    continue
                out.append((entry, stamp, Path(prefix), source))
        return out
    scanned = entries if entries is not None else scan_vault(root)
    out = []
    for entry in scanned:
        try:
            source = Path(root) / entry.path.relative_to(VAULT_PREFIX)
        except ValueError:
            source = Path(root) / entry.path
        out.append((entry, "", Path(VAULT_PREFIX), source))
    return out


def _slug(value: str) -> str:
    """A name reduced to how it would appear in a filename stem."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")


def _names_same_person(stem: str, aliases: Sequence[str], target_stem: str) -> bool:
    """Whether ``target_stem`` is the same person as this note.

    Deliberately narrow, and narrower than it first looks like it should be.
    A note links to plenty of things in another workspace that are not itself —
    Oliver's own note links to David Blazevic — so a bare "links into the other
    workspace" test would silence real candidates.

    Only two matches count: an exact stem, or an alias that slugifies to the
    target (`Oliver`, aliased "Oliver Akermann", matches `Oliver-Akermann`).
    Both are explicit identity claims made in the note itself.

    A "longer stem extending this one at a name boundary" rule was written here
    first, to catch `Ipek` -> `Ipek-Kahraman-Scandit`, and it was wrong: those
    are two different people who share a name, and the work note says so in
    prose ("the name collision in the vault is intentional — do not merge").
    Name shape is not identity. Requiring an alias means the vault has to *say*
    two notes are the same person before this suppresses a queue row.
    """
    a, b = _slug(stem), _slug(target_stem)
    if not a or not b:
        return False
    if a == b:
        return True
    return any(_slug(alias) == b for alias in aliases if alias)


def _counterpart_file(source: Path, workspace: str, other: str, tail: Sequence[str]) -> Path | None:
    """Where ``other``'s copy of ``tail`` would live, in this vault's layout.

    Derived from the note's own path rather than from a layout constant, because
    the workspace segment sits at a different depth in each: ``<vault>/<ws>/People``
    before the re-rooting and ``<root>/<ws>/memory-vault/People`` after it.
    Whatever lies between the workspace segment and ``People/`` is reused as-is,
    so this needs no knowledge of which layout it is in.
    """
    parts = list(source.parts)
    if len(parts) < 3:
        return None
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == workspace:
            interior = parts[i + 1 : len(parts) - 2]
            return Path(*parts[:i], other, *interior, *tail)
    return None


def _links_back(target: Path, workspace: str, stem: str) -> bool:
    """Whether ``target`` names this note in its own ``related``.

    A mutual link is the strongest identity claim the vault can make, and unlike
    any name test it cannot be produced by coincidence: both notes had to be
    edited to say it. It is what lets two notes be recognised as one person when
    the filenames cannot say so — `Ipek` and `Ipek-Kahraman-Scandit`, where the
    work note carries a disambiguating suffix no real alias would contain.
    """
    if not target.is_file():
        return False
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return False
    for ref in _frontmatter_related_refs(text):
        parts = Path(_new_ref(ref)).parts
        if not parts:
            continue
        # Prefixed (`personal/People/Ipek`) or the legacy unprefixed form
        # (`People/Ipek`), which can only mean the other root once split.
        if parts[0] == workspace and len(parts) >= 3:
            candidate = parts[-1]
        elif parts[0] in PEOPLE_DIRS and len(parts) >= 2:
            candidate = parts[-1]
        else:
            continue
        if _slug(Path(candidate).stem) == _slug(stem):
            return True
    return False


def _linked_counterpart(
    source: Path,
    workspace: str,
    aliases: Sequence[str],
    registered: Iterable[str],
) -> str:
    """The other workspace holding this person's other note, or "".

    Reads the note's own ``related`` refs rather than the scanned entry's,
    because the per-root scan resolves refs against ONE root's filename index
    and therefore drops every ref naming another workspace — which is precisely
    the ref this asks about. (14 such refs exist on the operator's vault; see
    the progress ledger.)
    """
    others = {name for name in registered if name and name != workspace}
    if not others or not source.is_file():
        return ""
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return ""
    stem = source.stem
    for ref in _frontmatter_related_refs(text):
        parts = Path(_new_ref(ref)).parts
        if len(parts) < 3 or parts[0] not in others or parts[1] not in PEOPLE_DIRS:
            continue
        target_stem = Path(parts[-1]).stem
        if _names_same_person(stem, aliases, target_stem):
            return parts[0]
        # Names that cannot match still describe one person when both notes say
        # so. Read the far side only when the cheap test failed.
        tail = [*parts[1:-1], f"{target_stem}.md"]
        target = _counterpart_file(source, workspace, parts[0], tail)
        if target is not None and _links_back(target, workspace, stem):
            return parts[0]
    return ""


def _frontmatter_related_refs(text: str) -> list[str]:
    """``related`` values from a note's frontmatter, block or inline form."""
    if not text.startswith("---"):
        return []
    chunks = text.split("---", 2)
    if len(chunks) < 3:
        return []
    refs: list[str] = []
    in_block = False
    for line in chunks[1].splitlines():
        inline = re.match(r"^related:\s*\[(.*)\]\s*$", line)
        if inline:
            refs += [part.strip() for part in inline.group(1).split(",") if part.strip()]
            continue
        if re.match(r"^related:\s*$", line):
            in_block = True
            continue
        if in_block:
            item = re.match(r"^\s+-\s*(.+?)\s*$", line)
            if item:
                refs.append(item.group(1))
                continue
            if line.strip():
                in_block = False
    return [ref.strip().strip('"').strip("'") for ref in refs if ref.strip()]


def detect_misfiled_people(
    vault_root: Path,
    *,
    workspaces: Sequence[str] | None = None,
    entries: list[Entry] | None = None,
    targets: Sequence[tuple[Path, str, Path]] | None = None,
) -> list[Candidate]:
    """Bucket every person note into mechanical, judgement, or correctly filed.

    A note is only examined when it sits at `<workspace>/People/…` for a
    *registered* workspace: `People/Mo.md` in a legacy single-root vault has no
    other workspace to be misfiled from.

    ``entries`` lets a caller that already scanned the vault hand the scan over —
    the planner needs the same pass to build its filename index, and reading every
    note twice for one migration is waste, not caution.
    """
    root = Path(vault_root)
    names = list(workspaces) if workspaces is not None else vault_workspaces(root)
    roles = resolve_role_workspaces(names)
    role_of = {name: role for role, name in roles.items()}
    registered = set(names)

    out: list[Candidate] = []
    for entry, stamp, prefix, source in _scanned_people(root, entries, targets):
        try:
            within = entry.path.relative_to(prefix)
        except ValueError:
            continue
        if stamp:
            # A root's vault holds exactly one workspace, so there is no
            # workspace segment in the path — the scan stamped it instead.
            workspace, tail = stamp, within.parts
        else:
            parts = within.parts
            if len(parts) < 2 or parts[0] not in registered:
                continue
            workspace, tail = parts[0], parts[1:]
        if len(tail) < 2 or tail[0] not in PEOPLE_DIRS:
            continue
        if tail[-1].casefold() in EXCLUDED_PERSON_FILENAMES:
            continue
        # The IDENTITY, and it is deliberately the same string in both layouts:
        # `<workspace>/People/Mo.md`. That string is written into the queue
        # bullets and is what `_rehome_signal` joins on, so deriving it from the
        # on-disk path would make every existing bullet stop matching its own
        # note the moment an install migrated — silently, because a failed join
        # renders as "no signal" rather than as an error.
        relative = Path(workspace, *tail)
        tags = tuple(sorted({tag.strip().casefold() for tag in entry.tags if tag.strip()}))
        signals = {tag: TAG_WORKSPACE_ROLES[tag] for tag in tags if tag in TAG_WORKSPACE_ROLES}
        signalled_roles = set(signals.values())
        # Roles that name somewhere else, and that actually exist here. A tag
        # naming an unregistered role is no signal at all: there is no directory
        # to move to, so the note is not a candidate rather than a judgement call.
        target_workspaces = sorted(
            {roles[role] for role in signalled_roles if role in roles and roles[role] != workspace}
        )
        own_role = role_of.get(workspace, "")

        # An existing, linked note for the same person in another workspace means
        # the split has already been made on purpose: this note is one half of
        # it, and the answer to "should it move?" is no. Without this, a person
        # who is genuinely both — tagged `friend` AND `colleague`, with the work
        # half already filed and cross-linked — sat in the queue permanently,
        # because tags naming two workspaces is the one case the tag rules refuse
        # to decide. Checked before the buckets so it covers the untagged case
        # too, where the proposal was to move the note on top of its counterpart.
        if _linked_counterpart(source, workspace, entry.aliases, registered):
            continue

        if not signalled_roles:
            destination = _counterpart(workspace, names, roles)
            out.append(
                Candidate(
                    path=relative.as_posix(),
                    destination=_moved_path(relative, workspace, destination),
                    workspace=workspace,
                    target_workspace=destination,
                    bucket="needs_judgement",
                    reason="no tag names a workspace",
                    tags=tags,
                )
            )
            continue
        if not target_workspaces:
            continue  # tags agree with where it already is, or name nowhere real
        if own_role and own_role in signalled_roles:
            out.append(
                Candidate(
                    path=relative.as_posix(),
                    destination=_moved_path(relative, workspace, target_workspaces[0])
                    if len(target_workspaces) == 1
                    else "",
                    workspace=workspace,
                    target_workspace=target_workspaces[0] if len(target_workspaces) == 1 else "",
                    bucket="needs_judgement",
                    reason=(
                        f"tags name both {workspace} and "
                        f"{', '.join(target_workspaces)} ({', '.join(sorted(signals))})"
                    ),
                    tags=tags,
                )
            )
            continue
        if len(target_workspaces) > 1:
            out.append(
                Candidate(
                    path=relative.as_posix(),
                    destination="",
                    workspace=workspace,
                    target_workspace="",
                    bucket="needs_judgement",
                    reason=f"tags name more than one other workspace: {', '.join(target_workspaces)}",
                    tags=tags,
                )
            )
            continue
        out.append(
            Candidate(
                path=relative.as_posix(),
                destination=_moved_path(relative, workspace, target_workspaces[0]),
                workspace=workspace,
                target_workspace=target_workspaces[0],
                bucket="mechanical",
                reason=f"tagged {', '.join(sorted(signals))}",
                tags=tags,
            )
        )
    return sorted(out, key=lambda candidate: candidate.path)


def _moved_path(relative: Path, workspace: str, target: str) -> str:
    """Same path with its leading workspace segment swapped, or "" for no target."""
    if not target or target == workspace:
        return ""
    return Path(target, *relative.parts[1:]).as_posix()


# ---- reference rewriting ---------------------------------------------------


@dataclass(frozen=True)
class _Edit:
    """One span of a note to replace, with the metadata the receipt records."""

    start: int
    end: int
    replacement: str
    kind: str  # "wikilink" | "markdown" | "frontmatter"


def _new_ref(text: str) -> str:
    """Strip a ref down to the `memory-vault`-less, extension-less form."""
    ref = text.strip()
    if ref.startswith(f"{VAULT_PREFIX}/"):
        ref = ref[len(VAULT_PREFIX) + 1 :]
    if ref.endswith(".md"):
        ref = ref[:-3]
    return ref


def _excluded_spans(body: str) -> list[tuple[int, int]]:
    spans = [(match.start(), match.end()) for match in FENCED_CODE_RE.finditer(body)]
    spans += [(match.start(), match.end()) for match in INLINE_CODE_RE.finditer(body)]
    return spans


def _relative_destination(source_after: str, target_ref: str, *, dotted: bool) -> str:
    relative = posixpath.relpath(
        f"{target_ref}.md", posixpath.dirname(source_after) or "."
    )
    if dotted and not relative.startswith("."):
        # `./` is not required by the syntax, but a link that had it should keep
        # it: the point is to change the path, not the house style.
        relative = f"./{relative}"
    return relative


def _wikilink_edits(
    body: str,
    body_start: int,
    moved_by_repo: dict[str, str],
    filename_index: dict[str, list[Path]],
) -> list[_Edit]:
    """Repoint wikilinks whose *resolved* target moved.

    Resolution goes through `_resolve_related`, so `[[Mo]]`, `[[People/Mo]]` and
    `[[personal/People/Mo]]` are all recognised as the same edge — which is the
    only way to catch the partial refs a real vault is full of. The replacement
    is always the full new path: a bare stem would keep resolving today and
    silently pick the wrong note the day a second `Mo` exists.
    """
    excluded = _excluded_spans(body)
    edits: list[_Edit] = []
    for match in WIKILINK_RE.finditer(body):
        start = match.start()
        if any(low <= start < high for low, high in excluded):
            continue
        if _is_escaped(body, start):
            continue
        ref, anchor, alias = _parse_wikilink(match.group(0))
        if not ref:
            continue
        target = _resolve_related(ref, filename_index)
        if target is None:
            continue
        new_ref = moved_by_repo.get(target.as_posix())
        if not new_ref or _new_ref(ref) == new_ref:
            continue
        rendered = new_ref + (f"#{anchor}" if anchor else "") + (f"|{alias}" if alias else "")
        edits.append(
            _Edit(
                start=body_start + start,
                end=body_start + match.end(),
                replacement=f"[[{rendered}]]",
                kind="wikilink",
            )
        )
    return edits


def _markdown_edits(
    body: str,
    body_start: int,
    source_before: str,
    source_after: str,
    moved_by_ref: dict[str, str],
    known: set[str] | None = None,
) -> list[_Edit]:
    """Recompute relative destinations that either end of the move invalidated.

    Two independent reasons a destination changes: the *target* moved, or **this
    note** moved and every relative path in it is now measured from a different
    directory. The second is the one that is easy to forget and breaks the
    moved note's own links.

    The link is resolved against ``source_before`` — the text on disk was written
    relative to where the note is now — and re-spelled against ``source_after``.
    Only the destination span is replaced, so the label, title and any fragment
    survive.
    """
    if not moved_by_ref and source_after == source_before:
        return []
    excluded = _excluded_spans(body)
    edits: list[_Edit] = []
    for match in MARKDOWN_LINK_RE.finditer(body):
        start = match.start()
        if any(low <= start < high for low, high in excluded):
            continue
        if not _is_link_start(body, start):
            continue  # an image embed, or an escaped bracket documenting syntax
        angle = match.group("angle")
        raw = angle if angle is not None else (match.group("bare") or "")
        if not raw:
            continue
        # The fragment is not part of the path but it is the user's content, so
        # it is carried across rather than dropped while relocating a file.
        path_part, separator, fragment = raw.partition("#")
        ref = vault_link_ref(path_part)
        if not ref:
            continue
        target = resolve_vault_link(source_before, ref)
        if not target:
            continue
        if known is not None and target not in known:
            # Unresolvable, so there is nothing to recompute against. Measured on
            # the real vault: the moved note held links spelled in the
            # workspace-qualified dialect (`work/People/Florin-Dobre`), which
            # `resolve_vault_link` reads as a RELATIVE path and turns into
            # `personal/memory-vault/work/People/...`. Re-spelling that from the
            # new location produced `../../../personal/work/People/...` — a link
            # that was already broken, made differently broken. Only rewrite what
            # can be proven to point at a note.
            continue
        new_target = moved_by_ref.get(target, target)
        if new_target == target and source_after == source_before:
            continue
        destination = markdown_destination(
            _relative_destination(
                source_after, new_target, dotted=path_part.startswith("./")
            )
            + separator
            + fragment
        )
        if angle is not None:
            span = (match.start("angle") - 1, match.end("angle") + 1)
        else:
            span = (match.start("bare"), match.end("bare"))
        if body[span[0] : span[1]] == destination:
            continue
        edits.append(
            _Edit(
                start=body_start + span[0],
                end=body_start + span[1],
                replacement=destination,
                kind="markdown",
            )
        )
    return edits


def _frontmatter_value_spans(value: str, offset: int) -> list[tuple[int, int, str]]:
    """Split a frontmatter value into its individual ref tokens.

    Handles a single value (`related: People/Mo`) and the inline flow list
    (`related: [People/Mo, Ideas/Thing]`) `_FM_RELATED_KEY_RE` documents. Each
    token comes back with the exact span of its *unquoted* text, so a rewrite
    replaces the ref and leaves quoting, spacing and commas byte-identical.
    """
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        inner_start = value.index("[") + 1
        inner = value[inner_start : value.rindex("]")]
        spans: list[tuple[int, int, str]] = []
        cursor = 0
        for chunk in inner.split(","):
            spans.extend(_frontmatter_value_spans(chunk, offset + inner_start + cursor))
            cursor += len(chunk) + 1
        return spans
    lead = len(value) - len(value.lstrip())
    token = value.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'`":
        lead += 1
        token = token[1:-1]
    if not token:
        return []
    return [(offset + lead, offset + lead + len(token), token)]


def _frontmatter_edits(
    match: re.Match[str],
    moved_by_repo: dict[str, str],
    filename_index: dict[str, list[Path]],
) -> list[_Edit]:
    """Repoint frontmatter refs that name a moved note.

    Two shapes, because a vault may be mid-conversion: a wikilink anywhere in the
    block (any key — real vaults use `project:`, `product:`, `people:`, not just
    `related:`), and a bare ref under `related:`/`relatedTo:`, which is the form
    `_normalize_related_value` expects and the link migration normalises to.

    Never a markdown link: YAML reads `[Mo](./People/Mo.md)` as one opaque
    string, which `_resolve_related` cannot resolve.
    """
    block = match.group(1)
    base = match.start(1)
    edits: list[_Edit] = []
    covered: list[tuple[int, int]] = []

    for link in WIKILINK_RE.finditer(block):
        ref, anchor, alias = _parse_wikilink(link.group(0))
        if not ref:
            continue
        target = _resolve_related(ref, filename_index)
        if target is None:
            continue
        new_ref = moved_by_repo.get(target.as_posix())
        if not new_ref or _new_ref(ref) == new_ref:
            continue
        rendered = new_ref + (f"#{anchor}" if anchor else "") + (f"|{alias}" if alias else "")
        edits.append(
            _Edit(
                start=base + link.start(),
                end=base + link.end(),
                replacement=f"[[{rendered}]]",
                kind="frontmatter",
            )
        )
        covered.append((link.start(), link.end()))

    in_related = False
    offset = 0
    for line in block.split("\n"):
        key = _FM_RELATED_KEY_RE.match(line)
        item = _FM_LIST_ITEM_RE.match(line) if in_related else None
        if key is not None:
            in_related = True
            spans = _frontmatter_value_spans(key.group(2), offset + key.start(2))
        elif item is not None:
            spans = _frontmatter_value_spans(item.group(2), offset + item.start(2))
        else:
            # Any other unindented key ends the related block; a continuation
            # line inside it that is not a list item is not a ref.
            if line and not line[0].isspace():
                in_related = False
            offset += len(line) + 1
            continue
        for start, end, token in spans:
            if "[[" in token:
                continue  # already handled above; overlapping edits corrupt text
            if any(low <= start < high for low, high in covered):
                continue
            target = _resolve_related(token, filename_index)
            if target is None:
                continue
            new_ref = moved_by_repo.get(target.as_posix())
            if not new_ref or _new_ref(token) == new_ref:
                continue
            edits.append(
                _Edit(
                    start=base + start,
                    end=base + end,
                    replacement=new_ref,
                    kind="frontmatter",
                )
            )
        offset += len(line) + 1
    return edits


def rewrite_references(
    text: str,
    source_before: str,
    source_after: str,
    moved_by_ref: dict[str, str],
    filename_index: dict[str, list[Path]],
    *,
    moved_by_resolved: dict[str, str] | None = None,
    known_notes: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Repoint one note's references at the moved notes.

    ``source_before``/``source_after`` are the note's vault-relative path before
    and after this migration (equal for a note that is not itself moving).
    ``moved_by_ref`` maps a vault-relative ref (no extension) to its new one.

    Each change carries its offset in the **new** text plus the exact `from`/`to`
    strings, which is what lets :func:`unrehome_vault_people` be an inverse. An
    empty change list means nothing pointed at a moved note, so the driver can
    leave the file's mtime alone.
    """
    # `_resolve_related` answers with the repo-relative path *including* `.md`
    # (that is what `Entry.path` carries), so the lookup table has to be keyed the
    # same way — refs are extension-less, resolved targets are not.
    #
    # `moved_by_resolved` lets a caller supply that table itself. The construction
    # below prefixes one fixed `VAULT_PREFIX`, which is only correct while every
    # workspace shares one vault: after the re-rooting `Entry.path` is
    # `<workspace>/<leaf>/...`, so a per-root caller's refs matched nothing and its
    # rewrites silently did nothing at all.
    moved_by_repo = (
        dict(moved_by_resolved)
        if moved_by_resolved is not None
        else {
            posixpath.join(VAULT_PREFIX, f"{old}.md"): new
            for old, new in moved_by_ref.items()
        }
    )
    frontmatter = FRONTMATTER_RE.match(text)
    body_start = frontmatter.end() if frontmatter is not None else 0
    body = text[body_start:]

    edits: list[_Edit] = []
    if frontmatter is not None:
        edits += _frontmatter_edits(frontmatter, moved_by_repo, filename_index)
    edits += _wikilink_edits(body, body_start, moved_by_repo, filename_index)
    edits += _markdown_edits(
        body, body_start, source_before, source_after, moved_by_ref, known_notes
    )
    edits.sort(key=lambda edit: edit.start)

    parts: list[str] = []
    changes: list[dict[str, Any]] = []
    last = 0
    shift = 0
    for edit in edits:
        if edit.start < last:
            continue  # overlapping candidates: first one wins, never both
        parts.append(text[last : edit.start])
        parts.append(edit.replacement)
        changes.append(
            {
                "kind": edit.kind,
                "offset": edit.start + shift,
                "from": text[edit.start : edit.end],
                "to": edit.replacement,
            }
        )
        shift += len(edit.replacement) - (edit.end - edit.start)
        last = edit.end
    parts.append(text[last:])
    new_text = "".join(parts)
    for change in changes:
        change["line"] = new_text.count("\n", 0, change["offset"]) + 1
    return new_text, changes


# ---- planning and driver ---------------------------------------------------


# ---- moving ONE note between roots -----------------------------------------
#
# Everything here works in ONE namespace: the path each note has relative to the
# install root, which after the re-rooting is exactly `Entry.path`
# (`personal/memory-vault/People/Mo.md`). Choosing that space is what makes the
# rewrites correct, and the first attempt at this failed because it used the
# rendered IDENTITY space (`personal/People/Mo.md`) instead:
#
# * `rewrite_references` keyed its table with one fixed `VAULT_PREFIX`, so no
#   per-root ref ever matched and refs TO the moved note were left pointing at a
#   file that was no longer there — silently, with the function reporting success;
# * relative markdown links were re-spelled between identities, producing
#   `../../personal/People/Alba.md` for a path that is really three levels up and
#   inside another vault directory.
#
# In install-relative space both fall out for free: `resolve_vault_link` and
# `_relative_destination` are then doing real directory arithmetic over real
# paths.


def _no_ext(path: str) -> str:
    """Drop a trailing ``.md``, which is how link refs are spelled."""
    return path[:-3] if path.endswith(".md") else path


def _install_relative(entry: Entry) -> str:
    """The note's path relative to the install root."""
    return entry.path.as_posix()


def _ref_dialect(target: str, referring_root: str) -> str:
    """How a note under ``referring_root`` should spell a ref to ``target``.

    Same root: relative to that root's vault, because that is what every other
    ref in the file looks like and what the root's own index resolves. Another
    root: prefixed with the workspace, the only spelling that can name it — and
    the one the vault already uses (`related: work/People/Oliver-Akermann`).
    """
    parts = Path(target).parts
    without_ext = target[:-3] if target.endswith(".md") else target
    if referring_root and parts and parts[0] == referring_root:
        # Drop `<workspace>/<leaf>/`, leaving `People/Mo`.
        return Path(*Path(without_ext).parts[2:]).as_posix()
    # `<workspace>/People/Mo`: workspace, then the path inside its vault.
    trimmed = Path(without_ext).parts
    return Path(trimmed[0], *trimmed[2:]).as_posix() if len(trimmed) > 2 else without_ext


def _per_root_index(
    entries: Sequence[Entry], referring_root: str
) -> dict[str, list[Path]]:
    """A filename index as seen from inside one root.

    Refs are written root-relative in the per-root layout, so the same string
    means a different note depending on who is asking. This keys the referring
    root's notes root-relative (and by bare stem), and every note by its
    workspace-qualified form. One global index instead would resolve
    `People/Peter` to whichever root came first — and Peter exists in both on the
    reference install.
    """
    idx: dict[str, list[Path]] = defaultdict(list)
    for entry in entries:
        full = entry.path
        parts = full.parts
        if len(parts) < 3:
            continue
        workspace = parts[0]
        inside = Path(*parts[2:])                      # People/Mo.md
        idx[str(full.with_suffix(""))].append(full)     # personal/memory-vault/People/Mo
        idx[f"{workspace}/{inside.with_suffix('')}"].append(full)   # personal/People/Mo
        if workspace == referring_root:
            idx[str(inside.with_suffix(""))].append(full)            # People/Mo
            idx[full.stem].append(full)                              # Mo
    return idx


def move_note_between_roots(
    install_root: Path,
    source: str,
    target_workspace: str,
    *,
    targets: Sequence[tuple[Path, str, Path]],
    workspaces: Sequence[str],
    apply: bool = False,
) -> dict[str, Any]:
    """Move ONE note to another workspace, taking its links with it.

    ``source`` is install-relative (``personal/memory-vault/People/Mo.md``).

    The bulk pass moves only tag-obvious notes; everything else reaches the review
    queue as a judgement, and on the reference install EVERY queued row is a
    judgement — so the bulk mover would move none of them. This is the per-row
    counterpart: the operator picks the destination and one note moves.

    Both directions of every edge are rewritten, which is the whole reason this is
    not a ``git mv``:

    * refs from other notes TO this one — root-relative for notes in the
      destination root, since it becomes their neighbour, workspace-qualified for
      everyone else;
    * refs IN this note to notes that stay behind — root-relative today, and now
      crossing a root, so they gain the workspace they were left in.

    A cross-root ref is a real link the graph deliberately does not draw (see
    ``Entry.related_external``), so a note that leaves takes its edges out of its
    old root's rendered graph. That is a consequence of moving it, and it is why
    the destination is the operator's choice rather than a guess.

    ``apply=False`` computes every edit and writes nothing, which is what the
    caller shows before asking.
    """
    install_root = Path(install_root).resolve()
    names = [n for n in workspaces if n]
    out: dict[str, Any] = {
        "source": source,
        "destination": "",
        "target_workspace": target_workspace,
        "applied": False,
        "rewrites": [],
        "files_rewritten": 0,
        "refusals": [],
    }

    parts = Path(source).parts
    if len(parts) < 3:
        out["refusals"].append(f"'{source}' is not a note inside a workspace vault")
        return out
    source_workspace, leaf = parts[0], parts[1]
    if source_workspace not in names:
        out["refusals"].append(f"'{source_workspace}' is not a registered workspace")
    if target_workspace not in names:
        out["refusals"].append(f"'{target_workspace}' is not a registered workspace")
    if source_workspace == target_workspace:
        out["refusals"].append(f"'{source}' is already in {target_workspace}")
    if out["refusals"]:
        return out

    destination = Path(target_workspace, leaf, *parts[2:]).as_posix()
    out["destination"] = destination
    source_file = install_root / source
    dest_file = install_root / destination
    if not source_file.is_file():
        if dest_file.is_file():
            # Already there. Reached when a previous attempt moved the note and
            # then failed before its queue row was dropped — which a request
            # timeout mid-handler does exactly. Reporting "no note at ..." left
            # the row permanently unclickable: the operator could neither move it
            # (gone) nor see that the move had in fact happened.
            out["already_moved"] = True
            out["applied"] = True
            return out
        out["refusals"].append(f"no note at {source}")
        return out
    if dest_file.exists():
        # Merging two people's notes is a content decision, never a move.
        out["refusals"].append(f"a note already exists at {destination}")
        return out

    entries, _absolute = scan_targets(list(targets))
    known = {_no_ext(_install_relative(e)) for e in entries}
    stayed = {
        _install_relative(e) for e in entries
        if e.path.parts and e.path.parts[0] == source_workspace
    }
    stayed.discard(source)

    # The scan skips a vault's generated root notes, but only INDEX.md and
    # VOCABULARY.md are actually regenerated — MEMORY.md is written by hand and by
    # the curation agent, so nothing else fixes a link in it. Measured on the real
    # vault: moving a note left exactly one broken link, and it was there.
    rebuilt_by_index = {"INDEX.md", "VOCABULARY.md"}
    extra: list[tuple[str, str]] = []
    for vault, workspace, prefix in targets:
        for candidate in sorted(Path(vault).glob("*.md")):
            if candidate.name in rebuilt_by_index:
                continue
            extra.append(((Path(prefix) / candidate.name).as_posix(), workspace))

    sweep: list[tuple[str, str]] = [
        (_install_relative(e), e.path.parts[0] if e.path.parts else "") for e in entries
    ]
    sweep.extend(extra)

    # Any ref to the note contains its stem, whatever dialect it is written in:
    # `[[Federica]]`, `People/Federica`, `./Federica.md`. So a file without the
    # stem cannot mention it, and parsing it is wasted work. On the real vault
    # this is the difference between reading 590 notes and parsing 590 of them —
    # the sweep was 2s of synchronous work inside the event loop, which is what
    # let a request time out and be cancelled between the move and the bullet
    # removal, leaving the note moved and its row still queued.
    stem = Path(source).stem
    for note, here in sweep:
        note_file = install_root / note
        if not note_file.is_file():
            continue
        try:
            text = note_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        is_moved = note == source
        if not is_moved and stem not in text:
            continue
        # Two different roots, and conflating them was a bug: a ref is RESOLVED
        # in the root the text was written in, and RE-SPELLED for the root the
        # note will be read from. They differ only for the note being moved —
        # whose `related: People/Alba` was written in personal and must come out
        # as `personal/People/Alba` once it is read from work. Resolving it as if
        # it were already in work found nothing and left the ref dangling.
        reads_as = source_workspace if is_moved else here
        writes_as = target_workspace if is_moved else here
        # Frontmatter and wikilinks take a REF; a markdown link takes a PATH,
        # because its destination is recomputed relative to the note. Same edges,
        # two value spaces, which is why both tables are passed.
        refs = {source: _ref_dialect(destination, writes_as)}
        # Extension-less, because `resolve_vault_link` answers without one — the
        # same keying the bulk planner uses (`candidate.path[:-3]`). Keyed with
        # `.md` it matched nothing and every markdown link to the moved note was
        # left pointing at a file that is no longer there.
        paths = {_no_ext(source): _no_ext(destination)}
        if is_moved:
            # Its own outbound refs to notes left behind now cross a root.
            for other in stayed:
                refs[other] = _ref_dialect(other, writes_as)
                paths[_no_ext(other)] = _no_ext(other)
        new_text, changes = rewrite_references(
            text,
            note,
            destination if is_moved else note,
            paths,
            _per_root_index(entries, reads_as),
            moved_by_resolved=refs,
            known_notes=known,
        )
        if not changes:
            continue
        out["files_rewritten"] += 1
        for change in changes:
            out["rewrites"].append({
                "path": destination if is_moved else note,
                "line": change["line"],
                "from": change["from"],
                "to": change["to"],
            })
        if apply:
            try:
                note_file.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                out["refusals"].append(f"could not rewrite {note}: {exc}")
                return out

    if apply:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        code, output = _run_git(install_root, "mv", source, destination)
        if code != 0:
            # Untracked, or no repository: still a move, just without history
            # following it. Recorded so the caller can say which happened.
            try:
                source_file.replace(dest_file)
            except OSError as exc:
                out["refusals"].append(f"could not move the note: {exc}")
                return out
            out["git_mv"] = output.strip()[:200] or "not tracked"
        else:
            out["git_mv"] = "ok"
        out["applied"] = True
        # Both roots' generated files now name a note that is not where they say.
        # INDEX.md and MEMORY.md are derived, so they are rebuilt rather than
        # rewritten — leaving them stale is what made the real-vault check report
        # `INDEX.md -> ./People/User.md` as newly broken.
        # Imported here, not at module scope: `workspace_reroot` is the migration
        # and this is one of the things it drives, so a top-level import would
        # close the loop.
        from ciao.workspace_reroot import rebuild_indexes

        out["indexes"] = rebuild_indexes(
            install_root, sorted({source_workspace, target_workspace}), vault_name=leaf
        )
    return out


def plan_rehome(
    vault_root: Path, *, workspaces: Sequence[str] | None = None
) -> dict[str, Any]:
    """Everything the migration would do, computed without writing anything.

    Separate from applying it so the git rail can be scoped to exactly the files
    the plan names (see :func:`vault_git_state`) and so a dry run and a real run
    can never disagree about what was going to happen.
    """
    root = Path(vault_root)
    plan: dict[str, Any] = {
        "vault_root": str(root),
        "workspaces": [],
        "mechanical": [],
        "needs_judgement": [],
        "moves": [],
        "conflicts": [],
        "files": {},  # vault-relative path today -> (new text, changes, path after)
        "notes_scanned": 0,
    }
    if not root.is_dir():
        plan["skipped"] = "vault root does not exist"
        return plan

    names = list(workspaces) if workspaces is not None else vault_workspaces(root)
    plan["workspaces"] = names
    entries = scan_vault(root)
    candidates = detect_misfiled_people(root, workspaces=names, entries=entries)
    plan["needs_judgement"] = [
        candidate.as_dict() for candidate in candidates if candidate.bucket == "needs_judgement"
    ]

    moved_by_ref: dict[str, str] = {}
    mechanical = [
        candidate
        for candidate in candidates
        if candidate.bucket == "mechanical" and candidate.destination
    ]
    # Which candidates want each destination, counted BEFORE any of them is
    # accepted. The `exists()` test below cannot answer this: two candidates in
    # one run can name the same destination — `personal/People/Mo.md` and
    # `home/People/Mo.md`, both tagged `colleague`, both bound for
    # `work/People/Mo.md` — and at plan time neither has moved, so the
    # destination is free for both. Both then entered `moves`, the apply pass ran
    # `source.replace(destination)` twice, and the second note OVERWROTE the
    # first: two reported successes, an empty `failed`, one note permanently
    # gone, and a receipt whose reverse map now restores the survivor's content
    # over the loser's path. Which of two people keeps the filename is the same
    # content decision the on-disk collision declines to make, so it is refused
    # the same way rather than resolved by iteration order.
    claimants: dict[str, list[str]] = {}
    for candidate in mechanical:
        claimants.setdefault(candidate.destination, []).append(candidate.path)

    for candidate in mechanical:
        if (root / candidate.destination).exists():
            # Two people with the same filename in both trees. Merging notes is a
            # content decision, so it is reported rather than resolved.
            plan["conflicts"].append(
                {**candidate.as_dict(), "error": "a note already exists at the destination"}
            )
            continue
        rivals = [path for path in claimants[candidate.destination] if path != candidate.path]
        if rivals:
            plan["conflicts"].append({
                **candidate.as_dict(),
                "error": (
                    "another note in this run moves to the same destination: "
                    + ", ".join(rivals)
                ),
            })
            continue
        plan["mechanical"].append(candidate.as_dict())
        plan["moves"].append({"from": candidate.path, "to": candidate.destination})
        moved_by_ref[candidate.path[:-3]] = candidate.destination[:-3]

    filename_index = _build_filename_index(entries)
    for md_path in sorted(root.rglob("*.md")):
        relative = md_path.relative_to(root)
        if _is_skipped(relative):
            continue
        plan["notes_scanned"] += 1
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        before = relative.as_posix()
        after = moved_by_ref.get(before[:-3], "")
        after = f"{after}.md" if after else before
        new_text, changes = rewrite_references(
            text, before, after, moved_by_ref, filename_index
        )
        if not changes:
            continue
        plan["files"][before] = {"after": after, "text": new_text, "changes": changes}
    return plan


def _unwrite_own_links(
    root: Path,
    move: dict[str, Any],
    originals: dict[str, str],
    summary: dict[str, Any],
) -> None:
    """Take back the own-link rewrite of a note whose move then failed.

    The note's own relative links were recomputed against the directory it was
    going to. Leaving them that way while the note sits where it started is not
    merely untidy, it poisons the retry: the second pass reads those links from
    the old directory, resolves them to somewhere that does not exist, and
    faithfully "recomputes" that — turning a dangling link into a *different*
    dangling link that was never in the vault and that no receipt can undo. So
    the rewrite is reverted here, and the spans it recorded are dropped with it,
    because there is nothing left to reverse.

    If the revert itself cannot be written the spans are re-keyed to where the
    note actually sits instead. The map then still describes the file exactly,
    which is the one thing that must hold; the operator is told, because a note
    whose own links disagree with its location needs a human.
    """
    original = originals.get(move["to"])
    if original is not None:
        try:
            (root / move["from"]).write_text(original, encoding="utf-8")
        except OSError as exc:
            summary["failed"].append(
                {
                    "path": move["from"],
                    "error": f"its own links were left rewritten for the move: {exc}",
                }
            )
        else:
            kept = [item for item in summary["rewrites"] if item["path"] != move["to"]]
            if len(kept) != len(summary["rewrites"]):
                summary["files_rewritten"] -= 1
            summary["rewrites"][:] = kept
            return
    for entry in summary["rewrites"]:
        if entry["path"] == move["to"]:
            entry["path"] = move["from"]


def rehome_vault_people(
    vault_root: Path,
    *,
    apply: bool = False,
    workspaces: Sequence[str] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-file the tag-obvious person notes and repoint every reference to them.

    With ``apply=False`` (the default) nothing is written — not the moves, not
    the link rewrites, not the review queue — and the summary is the diff
    preview. Idempotent: a moved note is no longer under the old workspace, so a
    second run finds nothing to move and `append_proposals` drops the judgement
    entries it already recorded.

    References are rewritten *before* the note moves and recorded against the
    path the note ends up at, so the receipt describes the tree as it will be
    found afterwards.

    ``plan`` accepts the output of :func:`plan_rehome` so the gated entry point
    can scope its git rail to the plan's files and then execute *that* plan —
    re-planning would let the preview and the write disagree.
    """
    root = Path(vault_root)
    if plan is None:
        plan = plan_rehome(root, workspaces=workspaces)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "applied": bool(apply),
        "workspaces": plan.get("workspaces", []),
        "mechanical": plan.get("mechanical", []),
        "needs_judgement": plan.get("needs_judgement", []),
        "conflicts": plan.get("conflicts", []),
        "moves": [],
        "rewrites": [],
        "files_rewritten": 0,
        "notes_scanned": plan.get("notes_scanned", 0),
        "proposals": [],
        "failed": [],
    }
    if "skipped" in plan:
        summary["skipped"] = plan["skipped"]
        return summary

    # The pre-rewrite text of the notes that are about to *move*, keyed by where
    # they are going. A note's own links are recomputed against the directory it
    # is moving to, so that text is only correct once it gets there: if the move
    # then fails the rewrite has to be taken back (see the move loop below), and
    # this is what it is taken back to. Only the movers are held, so this is the
    # length of `moves`, not of the vault.
    originals: dict[str, str] = {}
    for before, record in sorted(plan["files"].items()):
        if apply:
            if record["after"] != before:
                try:
                    originals[record["after"]] = (root / before).read_text(
                        encoding="utf-8"
                    )
                except (OSError, UnicodeDecodeError):
                    pass
            try:
                (root / before).write_text(record["text"], encoding="utf-8")
            except OSError as exc:
                summary["failed"].append({"path": before, "error": str(exc)})
                # Nothing landed, so there is nothing to take back if the move
                # fails too — and attempting it would report a second failure
                # for a file this run never changed.
                originals.pop(record["after"], None)
                continue
        summary["files_rewritten"] += 1
        for change in record["changes"]:
            summary["rewrites"].append(
                {
                    # Keyed by where the note *ends up*: that is where the span
                    # lives when unrehoming reads it back.
                    "path": record["after"],
                    "line": change["line"],
                    "offset": change["offset"],
                    "from": change["from"],
                    "to": change["to"],
                }
            )

    for move in plan["moves"]:
        source, destination = root / move["from"], root / move["to"]
        if apply:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
            except OSError as exc:
                summary["failed"].append({"path": move["from"], "error": str(exc)})
                _unwrite_own_links(root, move, originals, summary)
                continue
        summary["moves"].append(move)

    if apply and summary["needs_judgement"]:
        summary["proposals"] = _queue_judgement_cases(root, plan["needs_judgement"])
    return summary


def _queue_judgement_cases(
    vault_root: Path, cases: list[dict[str, Any]]
) -> list[str]:
    """File the judgement cases in each workspace's proposal queue.

    Per workspace, not one global file: that is where the user already reviews
    that workspace's proposals, and `ciao memory-proposal-dismiss` already knows
    how to remove an entry there. Grouped by the workspace the note is in *today*,
    since that is the vault whose curator has to make the call.
    """
    written: list[str] = []
    grouped: dict[str, list[MemoryProposal]] = defaultdict(list)
    for case in cases:
        candidate = Candidate(
            path=case["path"],
            destination=case.get("destination", ""),
            workspace=case["workspace"],
            target_workspace=case.get("target_workspace", ""),
            bucket="needs_judgement",
            reason=case.get("reason", ""),
        )
        grouped[case["workspace"]].append(candidate.as_proposal())
    for workspace, proposals in sorted(grouped.items()):
        try:
            path = append_proposals(proposals, vault_root / workspace)
        except OSError:
            logger.exception("vault-rehome: could not queue proposals for %s", workspace)
            continue
        if path is not None:
            written.append(str(path))
    return written


def unrehome_vault_people(
    vault_root: Path,
    receipt: dict[str, Any],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Undo the recorded rewrites, then move the notes back.

    Text first, moves second — the exact reverse of the forward order, so every
    span is verified at the path the receipt recorded it against. Edits are
    reversed back to front per file so the offsets stay valid as the text
    shrinks, and a file whose current text disagrees with the receipt at any
    offset is reported and left **entirely** untouched: a half-reverted note is a
    worse outcome than a skipped one, and the mismatch means someone edited it
    after the migration. That note is not moved back either, because a file whose
    links still point at the new location is not restorable by moving it.
    """
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "applied": bool(apply),
        "files_restored": 0,
        "restored": [],
        "moves_reverted": [],
        "failed": [],
    }
    rewrites = receipt.get("rewrites") if isinstance(receipt, dict) else None
    moves = receipt.get("moves") if isinstance(receipt, dict) else None
    if not isinstance(rewrites, list):
        rewrites = []
    if not isinstance(moves, list):
        moves = []
    if not rewrites and not moves:
        summary["skipped"] = "receipt records nothing to reverse"
        return summary

    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in rewrites:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        if path:
            by_path[path].append(entry)

    unrestorable: set[str] = set()
    for path_key, entries in sorted(by_path.items()):
        note = root / path_key
        try:
            text = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            summary["failed"].append({"path": path_key, "error": str(exc)})
            unrestorable.add(path_key)
            continue
        restored = text
        mismatch = False
        for entry in sorted(entries, key=lambda item: int(item.get("offset", 0)), reverse=True):
            offset = int(entry.get("offset", 0))
            rewritten, original = str(entry.get("to", "")), str(entry.get("from", ""))
            if restored[offset : offset + len(rewritten)] != rewritten:
                summary["failed"].append(
                    {
                        "path": path_key,
                        "offset": offset,
                        "error": f"expected {rewritten!r} — the note changed since rehoming",
                    }
                )
                mismatch = True
                break
            restored = restored[:offset] + original + restored[offset + len(rewritten) :]
        if mismatch:
            unrestorable.add(path_key)
            continue
        if restored == text:
            continue
        if apply:
            try:
                note.write_text(restored, encoding="utf-8")
            except OSError as exc:
                summary["failed"].append({"path": path_key, "error": str(exc)})
                unrestorable.add(path_key)
                continue
        summary["files_restored"] += 1
        summary["restored"].append(path_key)

    for move in moves:
        if not isinstance(move, dict):
            continue
        origin, current = str(move.get("from", "")), str(move.get("to", ""))
        if not origin or not current:
            continue
        if current in unrestorable:
            summary["failed"].append(
                {"path": current, "error": "left in place: its text could not be restored"}
            )
            continue
        source, destination = root / current, root / origin
        if not source.is_file():
            summary["failed"].append({"path": current, "error": "no longer at this path"})
            continue
        if destination.exists():
            summary["failed"].append(
                {"path": origin, "error": "a note already exists at the original path"}
            )
            continue
        if apply:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
            except OSError as exc:
                summary["failed"].append({"path": current, "error": str(exc)})
                continue
        summary["moves_reverted"].append({"from": current, "to": origin})
    return summary


# ---- gated entry points ----------------------------------------------------


def _touched_paths(plan: dict[str, Any]) -> list[str]:
    """Vault-relative paths the plan would write, for the git rail."""
    touched = set(plan.get("files", {}))
    for move in plan.get("moves", []):
        touched.add(move["from"])
    return sorted(touched)


def rehome_people(
    vault_root: Path,
    runtime_root: Path,
    *,
    apply: bool = False,
    force: bool = False,
    workspaces: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run the re-homing behind its safety rails and record the receipt.

    Two refusals, both overridable with ``force``: an existing receipt (this
    vault was already re-homed, and a second pass would overwrite the reverse map
    that can undo the first), and uncommitted changes in **the files this run
    would write** (so the user keeps `git checkout` as an undo that needs nothing
    from us).

    Neither refusal applies to a dry run. Both exist to protect a *write*, and
    gating the preview meant the only way to see what would happen to a dirty
    vault was to pass the flag that skips the check — exactly backwards.

    The "already migrated" refusal reads a *completed* receipt only. A run that
    could not move some note has not re-homed the vault, and gating the retry on
    its receipt left that note misfiled with every reference to it already
    pointing at a path it never reached, while the CLI reported a finished
    migration. ``summary["complete"]`` says which kind of run this was.
    """
    receipt = read_receipt(runtime_root)
    recorded = peek_receipt(runtime_root)
    plan = plan_rehome(vault_root, workspaces=workspaces)
    git = vault_git_state(vault_root, touched=_touched_paths(plan))
    if apply and not force:
        if receipt is not None:
            return {
                "skipped": "already migrated",
                "receipt_path": str(receipt_path(runtime_root)),
                "migrated_at": receipt.get("rehomed_at", ""),
            }
        if git["dirty"]:
            return {"skipped": "vault has uncommitted changes", "git": git}

    summary = rehome_vault_people(vault_root, apply=apply, workspaces=workspaces, plan=plan)
    summary["git_head_before"] = git["head"]
    summary["forced"] = bool(force)
    summary["complete"] = not summary["failed"]
    # A forced re-run over an already-rehomed vault moves nothing, and recording
    # *that* would rewrite the receipt for no reason. A first run with nothing to
    # do still writes one, so the vault is marked done. Beyond that the receipt
    # is rewritten only when this run actually wrote something — a move, or the
    # link rewrites of a run whose every move then failed — or when the vault's
    # *status* moved: down to "partial" because something could not be written,
    # or up to "migrated" once a retry finds nothing left to fix. A retry that
    # changes neither is not recorded at all, so re-running a failing migration
    # does not bury the reverse map under timestamped copies.
    recorded_partial = (recorded or {}).get("status", "migrated") == "partial"
    should_record = (
        summary["moves"]
        or summary["rewrites"]
        or recorded is None
        or bool(summary["failed"]) != recorded_partial
    )
    if apply and "skipped" not in summary and should_record:
        summary["receipt_path"] = str(write_receipt(runtime_root, summary))
    elif recorded is not None:
        summary["receipt_path"] = str(receipt_path(runtime_root))
    return summary


def unrehome_people(
    vault_root: Path,
    runtime_root: Path,
    *,
    apply: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Reverse the recorded re-homing, then drop the receipt.

    The receipt is the only input: without it there is nothing to reverse
    exactly, and re-deriving "which people used to be filed personally" from tags
    would move notes the user filed by hand. The receipt is removed only after a
    clean applied run, so a partial revert stays revertible.

    There is deliberately no dirty-vault refusal here, unlike
    :func:`rehome_people`. A successful re-homing *is* what makes the vault
    dirty, so gating the reverse on cleanliness made recovery impossible in
    exactly the state it exists for. Reversing is safe without the rail anyway:
    every span is re-checked against the receipt before it is touched, and a file
    with one mismatch is left completely alone.

    Queued proposals are not withdrawn — the review queue is the user's, and a
    judgement case they have already resolved must not be silently un-proposed.

    Reads the receipt with :func:`peek_receipt`, so a *partial* re-homing is
    still undoable. Whether the run finished is the forward side's question; here
    the only question is what it moved and rewrote, which a partial receipt
    answers exactly for the notes it names. Refusing to reverse one would strand
    those notes with no way back, which is the opposite of the guarantee — and
    it is what happened to every receipt written before the ``status`` field
    existed, since the completed-only accessor read those as no receipt at all.
    """
    receipt = peek_receipt(runtime_root)
    if receipt is None:
        return {
            "skipped": "no migration receipt to reverse",
            "receipt_path": str(receipt_path(runtime_root)),
        }
    del force  # accepted for CLI symmetry; nothing here needs overriding

    summary = unrehome_vault_people(vault_root, receipt, apply=apply)
    if apply and not summary["failed"] and "skipped" not in summary:
        summary["receipt_removed"] = remove_receipt(runtime_root)
    return summary
