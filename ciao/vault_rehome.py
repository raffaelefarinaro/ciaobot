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


def read_receipt(runtime_root: Path) -> dict[str, Any] | None:
    path = receipt_path(runtime_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_receipt(runtime_root: Path, summary: dict[str, Any]) -> Path:
    """Persist the reverse map atomically, keeping any earlier one.

    Written through a `.tmp` sibling and `replace()` so a crash mid-write cannot
    leave a truncated reverse map — a half-written receipt is worse than none,
    because unrehoming would restore part of a file and move nothing back.

    A forced re-run would otherwise overwrite the receipt of the run that did the
    real work, and the two cannot be merged: the second pass rewrites text the
    first pass already shifted. So an existing receipt is moved aside under a
    timestamped name instead of being lost.
    """
    path = receipt_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path.replace(path.with_name(f"{path.stem}.{stamp}{path.suffix}"))
    payload = {
        "schema_version": RECEIPT_VERSION,
        "rehomed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "vault_root": summary.get("vault_root", ""),
        "git_head_before": summary.get("git_head_before", ""),
        "moves": summary.get("moves", []),
        "rewrites": summary.get("rewrites", []),
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


def detect_misfiled_people(
    vault_root: Path,
    *,
    workspaces: Sequence[str] | None = None,
    entries: list[Entry] | None = None,
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
    for entry in entries if entries is not None else scan_vault(root):
        relative = entry.path.relative_to(VAULT_PREFIX)
        parts = relative.parts
        if len(parts) < 3 or parts[0] not in registered or parts[1] not in PEOPLE_DIRS:
            continue
        if parts[-1].casefold() in EXCLUDED_PERSON_FILENAMES:
            continue
        workspace = parts[0]
        tags = tuple(sorted({tag.strip().casefold() for tag in entry.tags if tag.strip()}))
        signals = {tag: TAG_WORKSPACE_ROLES[tag] for tag in tags if tag in TAG_WORKSPACE_ROLES}
        signalled_roles = set(signals.values())
        # Roles that name somewhere else, and that actually exist here. A tag
        # naming an unregistered role is no signal at all: there is no directory
        # to move to, so the note is not a candidate rather than a judgement call.
        targets = sorted(
            {roles[role] for role in signalled_roles if role in roles and roles[role] != workspace}
        )
        own_role = role_of.get(workspace, "")

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
        if not targets:
            continue  # tags agree with where it already is, or name nowhere real
        if own_role and own_role in signalled_roles:
            out.append(
                Candidate(
                    path=relative.as_posix(),
                    destination=_moved_path(relative, workspace, targets[0])
                    if len(targets) == 1
                    else "",
                    workspace=workspace,
                    target_workspace=targets[0] if len(targets) == 1 else "",
                    bucket="needs_judgement",
                    reason=(
                        f"tags name both {workspace} and "
                        f"{', '.join(targets)} ({', '.join(sorted(signals))})"
                    ),
                    tags=tags,
                )
            )
            continue
        if len(targets) > 1:
            out.append(
                Candidate(
                    path=relative.as_posix(),
                    destination="",
                    workspace=workspace,
                    target_workspace="",
                    bucket="needs_judgement",
                    reason=f"tags name more than one other workspace: {', '.join(targets)}",
                    tags=tags,
                )
            )
            continue
        out.append(
            Candidate(
                path=relative.as_posix(),
                destination=_moved_path(relative, workspace, targets[0]),
                workspace=workspace,
                target_workspace=targets[0],
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
    moved_by_repo = {
        posixpath.join(VAULT_PREFIX, f"{old}.md"): new for old, new in moved_by_ref.items()
    }
    frontmatter = FRONTMATTER_RE.match(text)
    body_start = frontmatter.end() if frontmatter is not None else 0
    body = text[body_start:]

    edits: list[_Edit] = []
    if frontmatter is not None:
        edits += _frontmatter_edits(frontmatter, moved_by_repo, filename_index)
    edits += _wikilink_edits(body, body_start, moved_by_repo, filename_index)
    edits += _markdown_edits(body, body_start, source_before, source_after, moved_by_ref)
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
    for candidate in candidates:
        if candidate.bucket != "mechanical" or not candidate.destination:
            continue
        if (root / candidate.destination).exists():
            # Two people with the same filename in both trees. Merging notes is a
            # content decision, so it is reported rather than resolved.
            plan["conflicts"].append(
                {**candidate.as_dict(), "error": "a note already exists at the destination"}
            )
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

    for before, record in sorted(plan["files"].items()):
        if apply:
            try:
                (root / before).write_text(record["text"], encoding="utf-8")
            except OSError as exc:
                summary["failed"].append({"path": before, "error": str(exc)})
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
    that workspace's proposals, and `memory_proposal_resolve` already knows how
    to dismiss an entry there. Grouped by the workspace the note is in *today*,
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
    """
    receipt = read_receipt(runtime_root)
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
    # A forced re-run over an already-rehomed vault moves nothing, and recording
    # *that* would replace a usable reverse map with an empty one — leaving
    # `vault-unrehome` with nothing to undo. A first run with nothing to do still
    # writes one, so the vault is marked done.
    if apply and "skipped" not in summary and (summary["moves"] or receipt is None):
        summary["receipt_path"] = str(write_receipt(runtime_root, summary))
    elif receipt is not None:
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
    """
    receipt = read_receipt(runtime_root)
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
