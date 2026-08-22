"""One-off conversion of a vault's `[[wikilinks]]` to relative markdown links.

Why convert at all
------------------
A wikilink is opaque body text to anything that is not Obsidian or Ciaobot, so
the vault's *edges* do not travel with the notes: a third-party reader of the
folder sees pages and no graph. `[Mo](./People/Mo.md)` is the same edge written
in a dialect every markdown tool already understands, which is the whole point
of keeping the vault as a portable bundle of files.

Why *relative* and not `/People/Mo.md`
--------------------------------------
A relative destination resolves in Obsidian, on GitHub, and in any plain
markdown editor; a leading-slash one resolves in a bundle-aware consumer and
nowhere else. It also keeps the link inside the linter's reach —
`vault_lint._markdown_link_error` deliberately returns None for absolute
destinations, so bundle-relative links would be silently exempt from the
broken-link check the vault already has.

What this module will and will not touch
----------------------------------------
* **Body wikilinks** become markdown links, resolved through the same
  `_build_filename_index`/`_resolve_related` pair the index uses, so a link the
  graph could follow becomes a link that points at the same file.
* **Frontmatter `related:` values** are normalised to *bare* refs
  (`[[People/Mo]]` -> `People/Mo`) and never to markdown links: YAML sees
  `[Mo](./People/Mo.md)` as a plain string and hands that literal text to
  `_resolve_related`, which fails. Bare refs already work.
* **Skipped entirely:** `Logs/`, `Templates/`, `.obsidian/` (already excluded
  from index, lint, and search), the regenerated `INDEX.md`/`VOCABULARY.md`
  (but *not* the hand-curated `MEMORY.md`, whose links are real content),
  anything inside app state or a checked-out venv, and any
  match inside a fenced block, an inline code span, or behind an escaping
  backslash. The skip logic is imported rather than re-derived — a second
  opinion about what counts as code is how a migration eats a code sample.
* **Anchors are dropped, not lost.** Nothing in the reader scrolls to a heading
  today (the frontend already parses the anchor and discards it), so the anchor
  has no destination to survive into; it is recorded in the receipt so teaching
  the viewer anchor scrolling later does not need the notes back.
* **Unresolvable refs are converted anyway**, to a best-effort sibling path.
  Leaving them as `[[...]]` would keep a second dialect alive forever, and a
  link whose target does not exist is a *broken* link, not a malformed one — it
  simply stays a `broken_markdown_links` finding, which is now the only bucket.

Reversibility
-------------
Every rewrite is recorded in `<runtime_root>/migration/vault-links.json` as an
exact `(offset, from, to)` triple in the *migrated* text, so
`unmigrate_vault_links` is an inverse rather than a re-derivation: it walks each
file's edits back to front, checks the text still reads as the receipt says, and
restores the original bytes. That is what makes rewriting a user's own notes
defensible.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.vault_index import (
    EXCLUDED_TOP_DIRS,
    FENCED_CODE_RE,
    FRONTMATTER_RE,
    INLINE_CODE_RE,
    _build_filename_index,
    _is_excluded,
    _resolve_related,
    markdown_destination,
    scan_vault,
)
from ciao.vault_lint import EXCLUDE_DIRS, _is_escaped

logger = logging.getLogger(__name__)

RECEIPT_NAME = "vault-links.json"
RECEIPT_VERSION = 1

VAULT_PREFIX = "memory-vault"

# `_is_excluded` covers Logs/Templates/.obsidian; `EXCLUDE_DIRS` adds the
# directories that are not vault prose at all (`.git`, `.venv`, `node_modules`,
# agent state). Rewriting a checked-out dependency's README is never wanted, and
# both sets already exist, so the migration honours the union of them.
_SKIP_DIRS = EXCLUDED_TOP_DIRS | EXCLUDE_DIRS

# The retired dialect, kept alive here and nowhere else. `vault_index` used to
# own these; once the readers stopped parsing wikilinks the pattern had no other
# caller, and this module is the one place that still has to *recognise* a
# wikilink in order to remove it. Same shape as the pattern the readers used, so
# the migration converts exactly what the graph used to follow: group 1 is the
# ref (anchor and alias excluded), group 2 the alias. `[[#Heading]]` cannot match
# — group 1 needs a non-`#` character — which is why a pure in-page anchor is
# left alone for free.
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")


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
    because unmigration would restore part of a file.

    A forced re-run would otherwise overwrite the receipt of the run that did the
    real work, and the two cannot be merged: the second pass shifts the offsets
    the first pass recorded. So an existing receipt is moved aside under a
    timestamped name instead of being lost.
    """
    path = receipt_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path.replace(path.with_name(f"{path.stem}.{stamp}{path.suffix}"))
    payload = {
        "schema_version": RECEIPT_VERSION,
        "migrated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "vault_root": summary.get("vault_root", ""),
        "git_head_before": summary.get("git_head_before", ""),
        "files_scanned": summary.get("files_scanned", 0),
        "files_rewritten": summary.get("files_rewritten", 0),
        "rewrites": summary.get("rewrites", []),
        "unresolved": summary.get("unresolved", []),
        "anchors_dropped": summary.get("anchors_dropped", []),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def remove_receipt(runtime_root: Path) -> bool:
    """Drop the receipt so a later `migrate` is not gated by a reverted run."""
    path = receipt_path(runtime_root)
    try:
        path.unlink()
    except OSError:
        return False
    return True


# ---- git rail --------------------------------------------------------------


def _run_git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # git vanished between the which() and the call
        logger.debug("git %s failed in %s: %s", args, root, exc)
        return 1, ""
    return proc.returncode, proc.stdout


def vault_git_state(vault_root: Path) -> dict[str, Any]:
    """Report whether the vault subtree is version-controlled and clean.

    Used for two different things: the CLI refuses to rewrite notes that have
    uncommitted changes (so `git checkout` stays a working undo alongside the
    receipt), and the recorded HEAD tells a later reader which commit the
    migrated text diverged from. Dirtiness is scoped to the vault directory, so
    unrelated edits elsewhere in a workspace repo do not block the migration.
    A vault that is not a repo reports `dirty: False` — there is nothing to be
    dirty against, and the receipt is then the only undo.
    """
    root = Path(vault_root)
    state: dict[str, Any] = {"is_repo": False, "head": "", "dirty": False}
    if not root.is_dir() or shutil.which("git") is None:
        return state
    code, out = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return state
    state["is_repo"] = True
    code, out = _run_git(root, "rev-parse", "HEAD")
    if code == 0:
        state["head"] = out.strip()
    code, out = _run_git(root, "status", "--porcelain", "--", ".")
    if code == 0:
        # Only dirt in files this migration could actually rewrite counts.
        # `--porcelain -- .` also reports untracked transcript folders under
        # `Logs/` and edits to non-markdown notes, none of which are ever
        # touched — so a whole-subtree check refused to run on a vault whose
        # only uncommitted change was an archived chat log. The rail exists to
        # keep `git checkout` a working undo for the *rewritten* files.
        dirty: list[str] = []
        for line in out.splitlines():
            entry = line[3:].strip().strip('"')
            # Renames report "old -> new"; the destination is what matters.
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            if not entry:
                continue
            candidate = root / entry
            if entry.endswith("/"):
                # An untracked directory: dirty only if it holds a migratable note.
                if any(
                    not _is_skipped(path.relative_to(root))
                    for path in candidate.rglob("*.md")
                ):
                    dirty.append(entry)
                continue
            if candidate.suffix.lower() != ".md":
                continue
            if not _is_skipped(Path(entry)):
                dirty.append(entry)
        state["dirty"] = bool(dirty)
        state["dirty_paths"] = sorted(dirty)[:20]
    return state


# ---- pure rewrite core -----------------------------------------------------


@dataclass(frozen=True)
class _Edit:
    """One span of a note to replace, with the metadata the receipt records."""

    start: int
    end: int
    replacement: str
    kind: str  # "body" | "frontmatter"
    ref: str
    anchor: str
    resolved: bool


def _vault_relative(path: Path) -> Path:
    """Strip the `memory-vault/` prefix that `Entry.path` carries.

    `_build_filename_index` keys by vault-relative stem but stores repo-relative
    paths, so a resolved target arrives one segment too long for computing a
    path relative to the note that links to it.
    """
    parts = path.parts
    if parts and parts[0] == VAULT_PREFIX:
        return Path(*parts[1:])
    return path


def _escape_label(label: str) -> str:
    """Keep a label from re-opening a bracket the renderer has to balance."""
    return label.replace("\\", "\\\\").replace("[", "\\[")


def _parse_wikilink(matched: str) -> tuple[str, str, str]:
    """Split a `[[ref#anchor|alias]]` match into its three parts.

    `WIKILINK_RE` captures ref and alias but leaves the anchor in a
    non-capturing group, and the anchor is exactly what the receipt has to keep.
    Parsing the already-matched text avoids a second wikilink pattern that could
    disagree with the first about what a wikilink even is.
    """
    inner = matched[2:-2]
    alias = ""
    if "|" in inner:
        inner, alias = inner.split("|", 1)
    anchor = ""
    if "#" in inner:
        inner, anchor = inner.split("#", 1)
    return inner.strip(), anchor.strip(), alias.strip()


def _resolved_destination(source_dir: str, target: Path) -> str:
    relative = Path(os.path.relpath(_vault_relative(target).as_posix(), source_dir)).as_posix()
    if not relative.startswith("."):
        # `./` is not required by the syntax, but it is what marks the
        # destination as a path rather than a bare word to a human skimming.
        relative = f"./{relative}"
    # Spelled by the shared emitter, not by hand: a destination with a space in
    # it has exactly one legal form, and this module is not the place to have a
    # second opinion about which one.
    return markdown_destination(relative)


def _unresolved_destination(ref: str) -> str:
    """Best-effort destination for a ref that resolves to nothing.

    Deliberately *not* relpath'd against an index that has no entry for it: the
    only honest reading of `[[Nowhere]]` is "a note beside this one", which is
    also where the user would create it.
    """
    relative = ref
    if relative.startswith(f"{VAULT_PREFIX}/"):
        relative = relative[len(VAULT_PREFIX) + 1 :]
    if relative.endswith(".md"):
        relative = relative[:-3]
    return markdown_destination(f"./{relative}.md")


def _body_edits(
    text: str,
    body_start: int,
    source_dir: str,
    filename_index: dict[str, list[Path]],
) -> list[_Edit]:
    body = text[body_start:]
    # Same exclusion pass as `_strip_body_links`, for the same reason: a
    # wikilink inside a code sample is documentation *about* the syntax.
    excluded: list[tuple[int, int]] = [
        (match.start(), match.end()) for match in FENCED_CODE_RE.finditer(body)
    ]
    excluded += [(match.start(), match.end()) for match in INLINE_CODE_RE.finditer(body)]

    edits: list[_Edit] = []
    for match in WIKILINK_RE.finditer(body):
        start = match.start()
        if any(low <= start < high for low, high in excluded):
            continue
        if _is_escaped(body, start):
            # `\[[People/Mo]]` is a deliberately un-linked mention.
            continue
        ref, anchor, alias = _parse_wikilink(match.group(0))
        if not ref:
            continue
        target = _resolve_related(ref, filename_index)
        destination = (
            _resolved_destination(source_dir, target)
            if target is not None
            else _unresolved_destination(ref)
        )
        label = alias or ref.rsplit("/", 1)[-1]
        edits.append(
            _Edit(
                start=body_start + start,
                end=body_start + match.end(),
                replacement=f"[{_escape_label(label)}]({destination})",
                kind="body",
                ref=ref,
                anchor=anchor,
                resolved=target is not None,
            )
        )
    return edits


def _frontmatter_edits(text: str, match: re.Match[str]) -> list[_Edit]:
    """Strip wikilinks out of frontmatter, differently per key.

    Two cases, because frontmatter holds two kinds of value:

    Never a markdown link, whichever key it is under: YAML sees
    `[Mo](./People/Mo.md)` as an opaque string, so anything reading the value
    gets that literal text. What replaces the wikilink depends on whether the
    value is a *reference* or *prose*, decided by whether the link is the entire
    value rather than by a list of key names — this vault's references live under
    `related:`, `project:`, `product:` and `people:`, and guessing from names
    would miss the next one someone invents:

    * **The whole value** (`product: [[work/products/slc]]`, a `related:` list
      item, `- "[[People/Mo|Mo]]"`) is a reference. It keeps the **bare ref** —
      the full path, not the label — because that is what `_resolve_related`
      reads and what a human needs to find the target again. Reducing
      `work/products/slc` to `slc` would throw the path away.
    * **Embedded in surrounding text** (`description: asked [[People/Mo|Mo]] to
      help`) is prose. `scan_vault` reads `description` into `Entry.description`
      and the index and PWA render it, so a wikilink there displays as literal
      `[[People/Mo|Mo]]` once nothing parses the dialect. It becomes its display
      text: the alias when one was written, else the last path segment.

    Only the bracketed span is touched either way, so quoting, indentation, key
    order and every other value survive byte for byte — the same surgical
    line-at-a-time approach `_strip_frontmatter_related` uses instead of
    round-tripping the block through yaml.safe_dump.
    """
    edits: list[_Edit] = []
    offset = match.start(1)
    for line in match.group(1).split("\n"):
        for link in WIKILINK_RE.finditer(line):
            ref, anchor, alias = _parse_wikilink(link.group(0))
            if not ref:
                continue
            if _is_whole_frontmatter_value(line, link):
                replacement = ref
            else:
                replacement = alias or ref.rsplit("/", 1)[-1]
            edits.append(
                _Edit(
                    start=offset + link.start(),
                    end=offset + link.end(),
                    replacement=replacement,
                    kind="frontmatter",
                    ref=ref,
                    anchor=anchor,
                    resolved=True,
                )
            )
        offset += len(line) + 1  # the newline `split` consumed
    return edits


def _is_whole_frontmatter_value(line: str, link: re.Match[str]) -> bool:
    """Whether ``link`` is the entire value on this frontmatter line.

    True for `key: [[ref]]`, `key: "[[ref]]"` and `- "[[ref]]"`; false when the
    wikilink sits inside a sentence. Everything except the link, the key or list
    marker, and optional quotes has to be whitespace.
    """
    before = line[: link.start()]
    after = line[link.end() :]
    before = re.sub(r"^\s*(?:-\s*|[A-Za-z0-9_.-]+:\s*)", "", before, count=1)
    return before.strip().strip("\"'") == "" and after.strip().strip("\"'") == ""


def rewrite_note(
    text: str,
    source_path: Path | str,
    filename_index: dict[str, list[Path]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert one note's wikilinks, returning the new text and what changed.

    `source_path` is the note's path relative to the vault root (a
    `memory-vault/` prefix is tolerated), because a relative destination is
    computed against the *containing note's directory*, not the bundle root.

    Each change carries its offset and line in the **new** text plus the exact
    `from`/`to` strings, which is what lets `unmigrate_vault_links` be an
    inverse. An empty change list means the note is already in the target
    dialect, so the driver can leave the file's mtime alone.
    """
    source_dir = _vault_relative(Path(source_path)).parent.as_posix()
    frontmatter = FRONTMATTER_RE.match(text)
    edits: list[_Edit] = []
    if frontmatter is not None:
        edits += _frontmatter_edits(text, frontmatter)
    edits += _body_edits(
        text,
        frontmatter.end() if frontmatter is not None else 0,
        source_dir,
        filename_index,
    )
    edits.sort(key=lambda edit: edit.start)

    parts: list[str] = []
    changes: list[dict[str, Any]] = []
    last = 0
    shift = 0
    for edit in edits:
        parts.append(text[last:edit.start])
        parts.append(edit.replacement)
        changes.append(
            {
                "kind": edit.kind,
                "offset": edit.start + shift,
                "from": text[edit.start : edit.end],
                "to": edit.replacement,
                "ref": edit.ref,
                "anchor": edit.anchor,
                "resolved": edit.resolved,
            }
        )
        shift += len(edit.replacement) - (edit.end - edit.start)
        last = edit.end
    parts.append(text[last:])
    new_text = "".join(parts)
    for change in changes:
        change["line"] = new_text.count("\n", 0, change["offset"]) + 1
    return new_text, changes


# ---- detection -------------------------------------------------------------


def has_unmigrated_links(vault_root: Path) -> str:
    """Return the first note still holding a wikilink, or "".

    Stops at the first hit: this runs from `os-audit`, which is already doing a
    full vault pass, and the answer is a yes/no. Uses the same skip and
    code-span rules as the migration so it cannot report a wikilink the
    migration would refuse to touch — a notice the user cannot act on is worse
    than no notice.
    """
    root = Path(vault_root)
    if not root.is_dir():
        return ""
    for path in sorted(root.rglob("*.md")):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if _is_skipped(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        stripped = INLINE_CODE_RE.sub("", FENCED_CODE_RE.sub("", text))
        for match in WIKILINK_RE.finditer(stripped):
            if not _is_escaped(stripped, match.start()):
                return relative.as_posix()
    return ""


# ---- driver ----------------------------------------------------------------


# `is_generated_vault_file` also covers `MEMORY.md`, which is right for the index
# and the linter — it carries no frontmatter and is not a note — but wrong here.
# MEMORY.md is hand-curated prose *with links in it*, so skipping it left the
# curator's own notes as the last wikilinks in the vault, rendering as literal
# `[[...]]`. Only the two files this app regenerates are safe to skip.
_REGENERATED_FILES = frozenset({"index.md", "vocabulary.md"})


def _is_skipped(relative: Path) -> bool:
    if relative.name.casefold() in _REGENERATED_FILES:
        return True
    if _is_excluded(relative):
        return True
    return any(part in _SKIP_DIRS for part in relative.parts[:-1])


def migrate_vault_links(vault_root: Path, *, apply: bool = False) -> dict[str, Any]:
    """Convert every note in a vault, recording an exact reverse map.

    With ``apply=False`` (the default) nothing is written and the summary is the
    diff preview. Idempotent: a converted note has no `[[` left to match, so a
    second run reports zero rewrites rather than mangling markdown links.

    A file is recorded only once its new text is safely on disk, so the receipt
    can never claim a rewrite that failed to land — unmigration trusts it
    literally.
    """
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "applied": bool(apply),
        "files_scanned": 0,
        "files_rewritten": 0,
        "rewrites": [],
        "unresolved": [],
        "anchors_dropped": [],
        "failed": [],
    }
    if not root.is_dir():
        summary["skipped"] = "vault root does not exist"
        return summary

    filename_index = _build_filename_index(scan_vault(root))
    for md_path in sorted(root.rglob("*.md")):
        relative = md_path.relative_to(root)
        if _is_skipped(relative):
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            summary["failed"].append({"path": relative.as_posix(), "error": str(exc)})
            continue
        summary["files_scanned"] += 1
        new_text, changes = rewrite_note(text, relative, filename_index)
        if not changes:
            continue
        if apply:
            try:
                md_path.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                summary["failed"].append({"path": relative.as_posix(), "error": str(exc)})
                continue
        summary["files_rewritten"] += 1
        path_key = relative.as_posix()
        for change in changes:
            summary["rewrites"].append(
                {
                    "path": path_key,
                    "line": change["line"],
                    "offset": change["offset"],
                    "from": change["from"],
                    "to": change["to"],
                }
            )
            if not change["resolved"]:
                summary["unresolved"].append(
                    {"path": path_key, "line": change["line"], "ref": change["ref"],
                     "target": change["to"]}
                )
            if change["anchor"]:
                summary["anchors_dropped"].append(
                    {"path": path_key, "line": change["line"], "ref": change["ref"],
                     "anchor": change["anchor"]}
                )
    return summary


def unmigrate_vault_links(
    vault_root: Path,
    receipt: dict[str, Any],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Restore the pre-migration text of every rewrite in ``receipt``.

    Edits are reversed back to front per file, so the offsets recorded against
    the migrated text stay valid as the text shrinks. A file whose current text
    disagrees with the receipt at any offset is reported and left **entirely**
    untouched: a half-reverted note is a worse outcome than a skipped one, and
    the mismatch means someone edited the note after the migration.
    """
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "applied": bool(apply),
        "files_restored": 0,
        "restored": [],
        "failed": [],
    }
    rewrites = receipt.get("rewrites") if isinstance(receipt, dict) else None
    if not isinstance(rewrites, list) or not rewrites:
        summary["skipped"] = "receipt records no rewrites to reverse"
        return summary

    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in rewrites:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        if not path:
            continue
        by_path[path].append(entry)

    for path_key, entries in sorted(by_path.items()):
        note = root / path_key
        try:
            text = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            summary["failed"].append({"path": path_key, "error": str(exc)})
            continue
        restored = text
        mismatch = False
        for entry in sorted(entries, key=lambda item: int(item.get("offset", 0)), reverse=True):
            offset = int(entry.get("offset", 0))
            migrated, original = str(entry.get("to", "")), str(entry.get("from", ""))
            if restored[offset : offset + len(migrated)] != migrated:
                summary["failed"].append(
                    {
                        "path": path_key,
                        "offset": offset,
                        "error": f"expected {migrated!r} — the note changed since migration",
                    }
                )
                mismatch = True
                break
            restored = restored[:offset] + original + restored[offset + len(migrated) :]
        if mismatch or restored == text:
            continue
        if apply:
            try:
                note.write_text(restored, encoding="utf-8")
            except OSError as exc:
                summary["failed"].append({"path": path_key, "error": str(exc)})
                continue
        summary["files_restored"] += 1
        summary["restored"].append(path_key)
    return summary


# ---- gated entry points ----------------------------------------------------


def migrate_links(
    vault_root: Path,
    runtime_root: Path,
    *,
    apply: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Run the migration behind its safety rails and record the receipt.

    Two refusals, both overridable with ``force``: an existing receipt (this
    vault was already converted, and a second pass would overwrite the reverse
    map that can undo the first), and uncommitted changes in the vault (so the
    user keeps `git checkout` as an undo that needs nothing from us).

    Neither refusal applies to a dry run. Both exist to protect a *write*, and
    gating the preview meant the only way to see what the migration would do to a
    dirty vault was to pass the flag that skips the check — exactly backwards.
    """
    receipt = read_receipt(runtime_root)
    git = vault_git_state(vault_root)
    if apply and not force:
        if receipt is not None:
            return {
                "skipped": "already migrated",
                "receipt_path": str(receipt_path(runtime_root)),
                "migrated_at": receipt.get("migrated_at", ""),
            }
        if git["dirty"]:
            return {"skipped": "vault has uncommitted changes", "git": git}

    summary = migrate_vault_links(vault_root, apply=apply)
    summary["git_head_before"] = git["head"]
    summary["forced"] = bool(force)
    # A forced re-run over an already-converted vault rewrites nothing, and
    # recording *that* would replace a usable reverse map with an empty one —
    # leaving `vault-unmigrate-links` with nothing to undo. A first run with
    # nothing to convert still writes one, so the vault is marked migrated and
    # the detect-and-offer path stops asking.
    if apply and "skipped" not in summary and (summary["rewrites"] or receipt is None):
        summary["receipt_path"] = str(write_receipt(runtime_root, summary))
    elif receipt is not None:
        summary["receipt_path"] = str(receipt_path(runtime_root))
    return summary


def unmigrate_links(
    vault_root: Path,
    runtime_root: Path,
    *,
    apply: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Reverse the recorded migration, then drop the receipt.

    The receipt is the only input: without it there is nothing to reverse
    exactly, and guessing which markdown links used to be wikilinks would
    rewrite links the user wrote by hand. The receipt is removed only after a
    clean applied run, so a partial revert stays revertible.

    There is deliberately no dirty-vault refusal here, unlike
    :func:`migrate_links`. A successful migration *is* what makes the vault
    dirty, so gating the reverse on cleanliness made recovery impossible in
    exactly the state it exists for — the only way to undo was the flag that
    skips the checks. Reversing is safe without the rail anyway: every span is
    re-checked against the receipt before it is touched, and a file with one
    mismatch is left completely alone.
    """
    receipt = read_receipt(runtime_root)
    if receipt is None:
        return {
            "skipped": "no migration receipt to reverse",
            "receipt_path": str(receipt_path(runtime_root)),
        }
    del force  # accepted for CLI symmetry; nothing here needs overriding

    summary = unmigrate_vault_links(vault_root, receipt, apply=apply)
    if apply and not summary["failed"] and "skipped" not in summary:
        summary["receipt_removed"] = remove_receipt(runtime_root)
    return summary
