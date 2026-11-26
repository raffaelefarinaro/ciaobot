"""Repointing a note's references at notes that moved.

One note's text goes in with a map of what moved; the text comes back with every
wikilink, relative markdown link and frontmatter reference rewritten, plus the
exact changes made. Code spans, fenced blocks and escaped links are left alone.
Used when a project folder moves (``ciao.project_folder_move``).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao.vault_index import (
    markdown_destination,
    resolve_related,
    resolve_vault_link,
    vault_link_ref,
)
from ciao.vault_links import (
    FENCED_CODE_RE,
    FM_LIST_ITEM_RE,
    FM_RELATED_KEY_RE,
    FRONTMATTER_RE,
    INLINE_CODE_RE,
    MARKDOWN_LINK_RE,
    WIKILINK_RE,
    alias_separator,
    is_escaped,
    is_link_start,
    parse_wikilink,
)

VAULT_PREFIX = "memory-vault"


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

    Resolution goes through `resolve_related`, so `[[Mo]]`, `[[People/Mo]]` and
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
        if is_escaped(body, start):
            continue
        ref, anchor, alias = parse_wikilink(match.group(0))
        if not ref:
            continue
        target = resolve_related(ref, filename_index)
        if target is None:
            continue
        new_ref = moved_by_repo.get(target.as_posix())
        if not new_ref or _new_ref(ref) == new_ref:
            continue
        # `alias_separator`, not a bare `|`: a wikilink in a table cell spells
        # its alias pipe `\|`, and re-emitting it unescaped closes the cell.
        separator = alias_separator(match.group(0))
        rendered = (
            new_ref
            + (f"#{anchor}" if anchor else "")
            + (f"{separator}{alias}" if alias else "")
        )
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
        if not is_link_start(body, start):
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
    (`related: [People/Mo, Ideas/Thing]`) `FM_RELATED_KEY_RE` documents. Each
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
    string, which `resolve_related` cannot resolve.
    """
    block = match.group(1)
    base = match.start(1)
    edits: list[_Edit] = []
    covered: list[tuple[int, int]] = []

    for link in WIKILINK_RE.finditer(block):
        ref, anchor, alias = parse_wikilink(link.group(0))
        if not ref:
            continue
        target = resolve_related(ref, filename_index)
        if target is None:
            continue
        new_ref = moved_by_repo.get(target.as_posix())
        if not new_ref or _new_ref(ref) == new_ref:
            continue
        # `alias_separator`, not a bare `|`: a wikilink in a table cell spells
        # its alias pipe `\|`, and re-emitting it unescaped closes the cell.
        separator = alias_separator(link.group(0))
        rendered = (
            new_ref
            + (f"#{anchor}" if anchor else "")
            + (f"{separator}{alias}" if alias else "")
        )
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
        key = FM_RELATED_KEY_RE.match(line)
        item = FM_LIST_ITEM_RE.match(line) if in_related else None
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
            target = resolve_related(token, filename_index)
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
    # `resolve_related` answers with the repo-relative path *including* `.md`
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
