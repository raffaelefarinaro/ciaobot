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
  `build_filename_index`/`resolve_related` pair the index uses, so a link the
  graph could follow becomes a link that points at the same file.
* **Frontmatter `related:` values** are normalised to *bare* refs
  (`[[People/Mo]]` -> `People/Mo`) and never to markdown links: YAML sees
  `[Mo](./People/Mo.md)` as a plain string and hands that literal text to
  `resolve_related`, which fails. Bare refs already work.
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
Applied migrations publish exact before and after images to a vault-bound receipt
before replacing notes. A stable advisory lock serializes managed operations;
current images classify retries and undo without trusting a progress counter.
Legacy span maps are upgraded to prepared images before undo. External edits
that do not match a recorded image are left untouched for manual recovery.
Receipts are bound to the vault directory's inode, so a vault replaced at the
same path never inherits them. Schema-1 and schema-2 receipts are adopted once,
on the first read, with their exact original bytes archived beside the receipt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import stat
import uuid
from bisect import bisect_right
from contextlib import contextmanager
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, NamedTuple

from ciao.os_support.locks import lock_exclusive
from ciao.os_support.private import carry_mode, open_private
from ciao.git_proc import EXACT_BYTES
from ciao.vault_index import (
    EXCLUDED_TOP_DIRS,
    build_filename_index,
    is_excluded,
    is_generated_vault_file,
    load_vault_ignore,
    markdown_destination,
    resolve_related,
    scan_vault,
)
from ciao.vault_lint import EXCLUDE_DIRS
from ciao.vault_links import (
    FENCED_CODE_RE,
    FRONTMATTER_RE,
    INLINE_CODE_RE,
    WIKILINK_RE,
    is_escaped,
    parse_wikilink,
)

logger = logging.getLogger(__name__)

RECEIPT_NAME = "vault-links.json"

VAULT_PREFIX = "memory-vault"

MARKER_NAME = ".ciao-vault-links.json"
MARKER_FORMAT = 1
MARKER_STATES = frozenset({"migrating", "migrated", "undoing", "unmigrated"})
LEGACY_ARCHIVE_PREFIX = "vault-links-legacy-"  # + sha256(raw).hexdigest() + ".json"
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")  # fullmatch; uuid4().hex

RECOVERY_ADVICE = "restore the original vault layout or recover manually from archived receipts or git history"
_REPLACED_MESSAGE = "receipt was recorded for a different directory at this vault path (the vault was replaced, restored or re-created)"
_REPLACED_ADVICE = "restore the original vault directory, or move this receipt aside to start a new migration; nothing was changed"
_UNOBSERVED_MESSAGE = "vault directory cannot be observed ({exc})"
_STALE_MESSAGE = "vault link state was changed by another Ciaobot runtime or another copy of this vault (marker operation {marker}, receipt operation {receipt})"
_STALE_ADVICE = "this recovery evidence is stale and was not applied; it is kept for manual recovery, and running the migration again starts a new one"
_MARKER_UNSAFE_MESSAGE = "vault link marker is a link or not a regular file"
_MARKER_MALFORMED_MESSAGE = "vault link marker is malformed"
_MARKER_UNREADABLE_MESSAGE = "vault link marker is unreadable ({exc})"
_MARKER_ADVICE = "replace it with the version from the vault's git history"
_MARKER_CHANGED_MESSAGE = "vault link state changed during this operation; another Ciaobot runtime is working on this vault"

Binding = Literal["current", "staged", "stale"]

# `is_excluded` covers Logs/Templates/.obsidian; `EXCLUDE_DIRS` adds the
# directories that are not vault prose at all (`.git`, `.venv`, `node_modules`,
# agent state). Rewriting a checked-out dependency's README is never wanted, and
# both sets already exist, so the migration honours the union of them.
_SKIP_DIRS = EXCLUDED_TOP_DIRS | EXCLUDE_DIRS


# ---- receipt ---------------------------------------------------------------


def receipt_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / "migration" / RECEIPT_NAME


class ReceiptUnavailable(Exception):
    """Receipt evidence cannot safely authorize a migration or undo."""

    def __init__(self, path: Path, message: str, *, advice: str = RECOVERY_ADVICE) -> None:
        self.path = path
        super().__init__(f"{path}: {message}; {advice}")


def _valid_receipt_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and not PureWindowsPath(value).drive and path.as_posix() == value and all(part not in ("", ".", "..") for part in value.split("/"))


def _valid_marker(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"format", "operation_id", "state"}
            and type(value["format"]) is int and value["format"] == MARKER_FORMAT
            and isinstance(value["operation_id"], str) and _OPERATION_ID.fullmatch(value["operation_id"]) is not None
            and value["state"] in MARKER_STATES)


def _validate_receipt(data: Any, path: Path, vault_root: Path) -> dict[str, Any]:
    def invalid(message: str) -> None:
        raise ReceiptUnavailable(path, message)

    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") not in (1, 2, 3):
        invalid("invalid or unsupported receipt")
    recorded = data.get("vault_root")
    if not isinstance(recorded, str) or not recorded or not Path(recorded).is_absolute():
        invalid("receipt has no valid absolute vault root")
    try:
        if Path(recorded).resolve(strict=True) != Path(vault_root).resolve(strict=True):
            invalid(f"receipt belongs to another vault ({recorded})")
    except OSError as exc:
        raise ReceiptUnavailable(path, f"receipt vault identity cannot be verified ({exc})") from exc
    version = data["schema_version"]
    if data.get("status", "migrated" if version == 1 else None) not in (("migrated", "partial") if version == 1 else ("prepared", "partial", "migrated")):
        invalid("invalid receipt status")
    if version >= 2 and data.get("operation") not in ("migrate", "undo"):
        invalid("invalid receipt operation")
    rewrites = data.get("rewrites")
    if not isinstance(rewrites, list):
        invalid("invalid rewrite map")
    for entry in rewrites:
        if (not isinstance(entry, dict) or not _valid_receipt_path(entry.get("path"))
            or type(entry.get("offset")) is not int or entry["offset"] < 0
            or not isinstance(entry.get("from"), str) or not entry["from"]
            or not isinstance(entry.get("to"), str) or not entry["to"]):
            invalid("invalid rewrite entry")
    if version >= 2:
        files = data.get("files")
        legacy = data.get("legacy_rewrites")
        if not isinstance(files, list) or not isinstance(legacy, list):
            invalid("invalid file images")
        paths: set[str] = set()
        for image in files:
            if not isinstance(image, dict) or not _valid_receipt_path(image.get("path")) or image["path"] in paths:
                invalid("invalid or duplicate image path")
            paths.add(image["path"])
            before, after = image.get("before_text"), image.get("after_text")
            if not isinstance(before, str) or not isinstance(after, str) or before == after:
                invalid("invalid file image")
            for name, value in (("before_revision", before), ("after_revision", after)):
                if image.get(name) != hashlib.sha256(value.encode("utf-8")).hexdigest():
                    invalid("file image hash mismatch")
        if any(not isinstance(e, dict) or e not in rewrites or e.get("path") in paths for e in legacy):
            invalid("invalid legacy rewrite map")
        for entry in rewrites:
            if entry not in legacy:
                matching = next((f for f in files if f["path"] == entry["path"]), None)
                if matching is None or matching["after_text"][entry["offset"]:entry["offset"] + len(entry["to"])] != entry["to"]:
                    invalid("rewrite does not match file image")
        for image in files:
            entries = sorted((e for e in rewrites if e["path"] == image["path"]), key=lambda e: e["offset"])
            if not entries:
                invalid("file image has no rewrite evidence")
            previous_end = 0
            restored = image["after_text"]
            for entry in entries:
                if entry["offset"] < previous_end:
                    invalid("overlapping rewrite spans")
                previous_end = entry["offset"] + len(entry["to"])
            for entry in reversed(entries):
                start = entry["offset"]
                restored = restored[:start] + entry["from"] + restored[start + len(entry["to"]):]
            if restored != image["before_text"]:
                invalid("rewrite map does not reconstruct the before image")
    else:
        legacy = rewrites
    if version == 3:
        inode, operation_id = data.get("vault_inode"), data.get("operation_id")
        if (not {"vault_inode", "operation_id", "base_marker"} <= set(data)
                or (inode is not None and (type(inode) is not int or inode <= 0))
                or (operation_id is not None and not (isinstance(operation_id, str) and _OPERATION_ID.fullmatch(operation_id)))
                or (data["base_marker"] is not None and not _valid_marker(data["base_marker"]))):
            invalid("invalid vault binding")
    for path_key in {e["path"] for e in legacy}:
        entries = sorted((e for e in legacy if e["path"] == path_key), key=lambda e: e["offset"])
        if any(a["offset"] + len(a["to"]) > b["offset"] for a, b in zip(entries, entries[1:])):
            invalid("overlapping legacy rewrite spans")
    result: dict[str, Any] = data
    return result


def _unsafe_receipt_path(path: Path) -> bool:
    return (path.parent.is_symlink() or path.parent.is_junction()
            or path.is_symlink() or path.is_junction())


class _Loaded(NamedTuple):
    receipt: dict[str, Any]  # schema-3 view
    source_version: int  # 1, 2 or 3
    raw: bytes  # exact bytes read


def _vault_inode(root: Path) -> int | None:
    """The directory's inode, or None where the filesystem reports 0."""
    return os.stat(root).st_ino or None


def _read_receipt_file(runtime_root: Path) -> tuple[bytes, Any] | None:
    path = receipt_path(runtime_root)
    if _unsafe_receipt_path(path):
        raise ReceiptUnavailable(path, "receipt path is a link")
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ReceiptUnavailable(path, "receipt is not a regular file")
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReceiptUnavailable(path, f"receipt is unreadable ({exc})") from exc
    try:
        return raw, json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReceiptUnavailable(path, "receipt is malformed") from exc


def _load_receipt(runtime_root: Path, vault_root: Path) -> _Loaded | None:
    """Validate the receipt, check it against the directory now at the vault path."""
    found = _read_receipt_file(runtime_root)
    if found is None:
        return None
    raw, data = found
    path = receipt_path(runtime_root)
    try:
        receipt = _validate_receipt(data, path, vault_root)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ReceiptUnavailable(path, "receipt contains invalid data") from exc
    try:
        inode = _vault_inode(Path(vault_root).resolve(strict=True))
    except OSError as exc:
        raise ReceiptUnavailable(path, _UNOBSERVED_MESSAGE.format(exc=exc)) from exc
    version = receipt["schema_version"]
    if version == 3:
        if receipt["vault_inode"] != inode:
            raise ReceiptUnavailable(path, _REPLACED_MESSAGE, advice=_REPLACED_ADVICE)
        return _Loaded(receipt, 3, raw)
    view = dict(receipt, schema_version=3, vault_inode=inode, operation_id=None, base_marker=None)
    if version == 1:
        view.update(status=receipt.get("status", "migrated"), operation="migrate", files=[], legacy_rewrites=list(receipt["rewrites"]))
    return _Loaded(view, version, raw)


def peek_receipt(runtime_root: Path, *, vault_root: Path) -> dict[str, Any] | None:
    """Read validated, vault-bound evidence as a schema-3 view, regardless of status. Writes nothing."""
    loaded = _load_receipt(Path(runtime_root), Path(vault_root))
    return loaded.receipt if loaded else None


def _archive_legacy(runtime_root: Path, raw: bytes) -> Path:
    """Keep a legacy receipt's exact bytes under a name derived from them. Caller holds the lock."""
    directory = receipt_path(runtime_root).parent
    target = directory / f"{LEGACY_ARCHIVE_PREFIX}{hashlib.sha256(raw).hexdigest()}.json"
    if target.is_symlink() or target.is_junction():
        raise OSError("unsafe receipt archive")
    if target.exists():
        if target.read_bytes() == raw:
            return target
        raise OSError("legacy receipt archive exists with different bytes")
    fd, temporary = tempfile.mkstemp(prefix=".vault-links-legacy-", suffix=".tmp", dir=directory)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
        os.replace(temporary, target)
        replaced = True
    finally:
        if not replaced:
            Path(temporary).unlink(missing_ok=True)
    return target


def _adopt_legacy(runtime_root: Path, vault_root: Path) -> None:
    with _transaction_lock(runtime_root):
        loaded = _load_receipt(runtime_root, vault_root)
        if loaded is None or loaded.source_version == 3:
            return
        _archive_legacy(runtime_root, loaded.raw)
        _publish_receipt(runtime_root, loaded.receipt, archive=False)


def _new_operation_id() -> str:
    return uuid.uuid4().hex


def _marker_path(root: Path) -> Path:
    return root / MARKER_NAME


def _marker_is_unsafe(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(mode) or path.is_junction() or not stat.S_ISREG(mode)


def _read_marker(root: Path) -> dict[str, Any] | None:
    path = _marker_path(root)
    try:
        if _marker_is_unsafe(path):
            raise ReceiptUnavailable(path, _MARKER_UNSAFE_MESSAGE, advice=_MARKER_ADVICE)
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReceiptUnavailable(path, _MARKER_UNREADABLE_MESSAGE.format(exc=exc), advice=_MARKER_ADVICE) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:  # UnicodeDecodeError is one
        raise ReceiptUnavailable(path, _MARKER_MALFORMED_MESSAGE, advice=_MARKER_ADVICE) from exc
    if not _valid_marker(data):
        raise ReceiptUnavailable(path, _MARKER_MALFORMED_MESSAGE, advice=_MARKER_ADVICE)
    marker: dict[str, Any] = data
    return marker


def _publish_marker(root: Path, marker: dict[str, Any], *, expected: dict[str, Any] | None) -> None:
    """Replace the marker, but only if it is still what this operation last saw."""
    current = _read_marker(root)
    if current != expected:
        raise OSError(_MARKER_CHANGED_MESSAGE)
    if current == marker:
        return
    path = _marker_path(root)
    encoded = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".ciao-vault-links.", suffix=".tmp", dir=root)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
        if _marker_is_unsafe(path):
            raise OSError(_MARKER_UNSAFE_MESSAGE)
        os.replace(temporary, path)
        replaced = True
    finally:
        if not replaced:
            Path(temporary).unlink(missing_ok=True)


def _set_marker(root: Path, operation_id: str, state: str, expected: dict[str, Any] | None, detail: str) -> dict[str, Any]:
    """Publish an in-flight marker before notes change; return what is now on disk."""
    marker = {"format": MARKER_FORMAT, "operation_id": operation_id, "state": state}
    if marker != expected:
        try:
            _publish_marker(root, marker, expected=expected)
        except (ReceiptUnavailable, OSError, ValueError, MemoryError) as exc:
            raise OSError(f"cannot publish vault link state; {detail}: {exc}") from exc
    return marker


def _binding(receipt: dict[str, Any], marker: dict[str, Any] | None) -> Binding:
    operation = receipt["operation_id"]
    if operation is None:
        return "current" if marker is None else "stale"
    if marker is not None and marker["operation_id"] == operation:
        return "current"
    if receipt["status"] in ("prepared", "partial") and marker == receipt["base_marker"]:
        return "staged"
    return "stale"


def _stale(receipt: dict[str, Any], marker: dict[str, Any] | None, runtime: Path) -> ReceiptUnavailable:
    message = _STALE_MESSAGE.format(marker=marker["operation_id"] if marker else "none", receipt=receipt["operation_id"] or "legacy")
    return ReceiptUnavailable(receipt_path(runtime), message, advice=_STALE_ADVICE)


def _is_complete(receipt: dict[str, Any], marker: dict[str, Any] | None) -> bool:
    return (receipt["status"] == "migrated" and receipt["operation"] == "migrate"
            and (marker is None or marker["state"] == "migrated"))


def read_receipt(runtime_root: Path, *, vault_root: Path) -> dict[str, Any] | None:
    """Return only a validated completed migration for this vault.

    Completion is the runtime's own current receipt, else the vault marker's
    `migrated` state (reported with `observed_only`). The first read of a
    schema-1 or schema-2 receipt adopts it: the exact bytes are archived and the
    schema-3 view is published, inside the runtime only. An adoption that cannot
    write leaves the legacy receipt meaning what it meant.
    """
    runtime, root = Path(runtime_root), Path(vault_root)
    loaded = _load_receipt(runtime, root)
    if loaded is not None and loaded.source_version != 3:
        try:
            _adopt_legacy(runtime, root)
        except (OSError, ValueError, MemoryError) as exc:
            logger.debug("link receipt adoption deferred: %s", exc)
    canonical = root.resolve()
    marker = _read_marker(canonical)
    if loaded is not None and _binding(loaded.receipt, marker) == "current":
        return loaded.receipt if _is_complete(loaded.receipt, marker) else None
    if marker is None or marker["state"] != "migrated":
        return None
    return {"schema_version": 3, "observed_only": True, "status": "migrated", "operation": "migrate",
            "operation_id": marker["operation_id"], "vault_root": str(canonical)}


def receipt_token(runtime_root: Path, *, vault_root: Path) -> str:
    """Identity of the evidence behind completion: receipt, marker and vault directory.

    A few `stat` calls and no reads, so a Home poll can key its stored answer on it.
    """

    def stamp(path: Path) -> str:
        try:
            info = path.stat()
        except OSError:
            return "absent"
        return f"{info.st_mtime_ns}:{info.st_size}"

    try:
        inode = str(_vault_inode(Path(vault_root)))
    except OSError:
        inode = "unobserved"
    return "|".join([str(runtime_root), str(vault_root), stamp(receipt_path(runtime_root)),
                     stamp(_marker_path(Path(vault_root))), inode])


def _publish_receipt(runtime_root: Path, payload: dict[str, Any], *, archive: bool) -> Path:
    """Serialize fully, then replace the active evidence without moving it away."""
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    path = receipt_path(runtime_root)
    if _unsafe_receipt_path(path):
        raise OSError("unsafe receipt path")
    path.parent.mkdir(parents=True, exist_ok=True)
    if _unsafe_receipt_path(path):
        raise OSError("unsafe receipt path")
    fd, temporary = tempfile.mkstemp(prefix=".vault-links-", suffix=".tmp", dir=path.parent)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
        if archive and path.exists():
            archive_fd, archive_name = tempfile.mkstemp(prefix="vault-links.", suffix=".json", dir=path.parent)
            try:
                with os.fdopen(archive_fd, "wb") as stream:
                    stream.write(path.read_bytes())
                    stream.flush()
            except BaseException:
                Path(archive_name).unlink(missing_ok=True)
                raise
        if _unsafe_receipt_path(path):
            raise OSError("unsafe receipt path")
        os.replace(temporary, path)
        replaced = True
    finally:
        if not replaced:
            Path(temporary).unlink(missing_ok=True)
    return path


def _merge_notes(previous: Any, fresh: Any) -> list[dict[str, Any]]:
    """Concatenate two lists of offset-free records, dropping exact duplicates.

    For `unresolved` and `anchors_dropped`, which describe what a conversion cost
    rather than where it landed. A dropped anchor stays worth knowing about after
    a retry re-converts the note, so these are kept across runs even where the
    reverse map's entries for that note are not.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    left = previous if isinstance(previous, list) else []
    right = fresh if isinstance(fresh, list) else []
    for entry in [*left, *right]:
        if not isinstance(entry, dict):
            continue
        marker = json.dumps(entry, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(entry)
    return merged


# ---- git rail --------------------------------------------------------------


def run_git(root: Path, *args: str) -> tuple[int, str]:
    """Run one git command in ``root``, returning (exit code, stdout).

    Not the same helper as :func:`ciao.workspace_reroot.run_git`, which folds
    stderr into the output and lets a missing git raise. This one is called
    behind a `shutil.which("git")` check on a vault that may not be a repo at
    all, so a vanished git is a soft "no git state", not an error.
    """
    try:
        proc = subprocess.run(
            ["git", *EXACT_BYTES, "-C", str(root), *args],
            capture_output=True,
            text=True, encoding="utf-8",
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
    code, out = run_git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return state
    state["is_repo"] = True
    code, out = run_git(root, "rev-parse", "HEAD")
    if code == 0:
        state["head"] = out.strip()
    code, out = run_git(root, "status", "--porcelain", "--", ".")
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
                    not is_skipped(path.relative_to(root))
                    for path in candidate.rglob("*.md")
                ):
                    dirty.append(entry)
                continue
            if candidate.suffix.lower() != ".md":
                continue
            if not is_skipped(Path(entry)):
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

    `build_filename_index` keys by vault-relative stem but stores repo-relative
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
        if is_escaped(body, start):
            # `\[[People/Mo]]` is a deliberately un-linked mention.
            continue
        ref, anchor, alias = parse_wikilink(match.group(0))
        if not ref:
            continue
        target = resolve_related(ref, filename_index)
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
      the full path, not the label — because that is what `resolve_related`
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
            ref, anchor, alias = parse_wikilink(link.group(0))
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


_KEY_OR_DASH_RE = re.compile(r"^\s*(?:-\s*|[A-Za-z0-9_.-]+:\s*)")
# Where one item of an inline flow sequence begins and ends: an opening bracket
# or a comma from the item before it, and a comma or the closing bracket after.
# Quotes are optional on either side, and only the item's own delimiters count.
_FLOW_ITEM_OPENS_RE = re.compile(r"""(?:^|[\[,])\s*["']?\s*$""")
_FLOW_ITEM_CLOSES_RE = re.compile(r"""^\s*["']?\s*[,\]]""")


def _is_whole_frontmatter_value(line: str, link: re.Match[str]) -> bool:
    """Whether ``link`` is an entire value on this frontmatter line.

    True for `key: [[ref]]`, `key: "[[ref]]"`, `- "[[ref]]"` and for each item
    of an inline flow sequence, `related: ["[[a]]", "[[b]]"]`; false when the
    wikilink sits inside a sentence.

    The flow sequence is the case this used to get wrong, and it lost data
    rather than merely mis-classifying: a flow item read as *prose* is replaced
    by its display text, so `related: ["[[work/products/slc]]"]` became
    `related: ["slc"]` — the path thrown away, which is exactly what the
    reference branch exists to prevent. Two refs sharing a stem collapsed into
    the same ambiguous value, and since the rewrite resolved fine it was
    reported as a clean conversion with nothing unresolved.

    A flow sequence is recognised by what precedes the link rather than by what
    follows it: the text between the key and the link starts with `[`. Reading
    it off the *trailing* comma instead would swallow ordinary prose — the comma
    in `description: asked [[People/Mo|Mo]], then left` says nothing about
    whether the value is a reference, and treating it as a delimiter turned that
    sentence into a path. The link's own `[[` is never part of that text, so
    there is nothing to disambiguate against.
    """
    before = _KEY_OR_DASH_RE.sub("", line[: link.start()], count=1)
    after = line[link.end() :]
    if before.lstrip().startswith("["):
        return bool(
            _FLOW_ITEM_OPENS_RE.search(before) and _FLOW_ITEM_CLOSES_RE.match(after)
        )
    return before.strip().strip("\"'") == "" and after.strip().strip("\"'") == ""


def _normalized_text_and_boundaries(text: str) -> tuple[str, list[int]]:
    """Map universal-newline character boundaries back to exact text."""
    chars: list[str] = []
    boundaries = [0]
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            index += 2 if text[index:index + 2] == "\r\n" else 1
            chars.append("\n")
        else:
            chars.append(char)
            index += 1
        boundaries.append(index)
    return "".join(chars), boundaries


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
    `from`/`to` strings for the receipt's diagnostic map. An empty change list means the note is already in the target
    dialect, so the driver can leave the file's mtime alone.
    """
    source_dir = _vault_relative(Path(source_path)).parent.as_posix()
    normalized, boundaries = _normalized_text_and_boundaries(text)
    if normalized.startswith("\ufeff"):
        normalized, boundaries = normalized[1:], boundaries[1:]
    frontmatter = FRONTMATTER_RE.match(normalized)
    edits: list[_Edit] = []
    if frontmatter is not None:
        edits += _frontmatter_edits(normalized, frontmatter)
    edits += _body_edits(
        normalized,
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
        start, end = boundaries[edit.start], boundaries[edit.end]
        parts.append(text[last:start])
        parts.append(edit.replacement)
        changes.append(
            {
                "kind": edit.kind,
                "offset": start + shift,
                "from": text[start:end],
                "to": edit.replacement,
                "ref": edit.ref,
                "anchor": edit.anchor,
                "resolved": edit.resolved,
            }
        )
        shift += len(edit.replacement) - (end - start)
        last = end
    parts.append(text[last:])
    new_text = "".join(parts)
    normalized_output, output_boundaries = _normalized_text_and_boundaries(new_text)
    line_starts = [output_boundaries[i + 1] for i, char in enumerate(normalized_output) if char == "\n"]
    for change in changes:
        change["line"] = bisect_right(line_starts, change["offset"]) + 1
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
        if is_skipped(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        stripped = INLINE_CODE_RE.sub("", FENCED_CODE_RE.sub("", text))
        for match in WIKILINK_RE.finditer(stripped):
            if not is_escaped(stripped, match.start()):
                return relative.as_posix()
    return ""


# ---- driver ----------------------------------------------------------------


# `is_generated_vault_file` also covers `MEMORY.md`, which is right for the index
# and the linter — it carries no frontmatter and is not a note — but wrong here.
# MEMORY.md is hand-curated prose *with links in it*, so skipping it left the
# curator's own notes as the last wikilinks in the vault, rendering as literal
# `[[...]]`. Only the two files this app regenerates are safe to skip.
_REGENERATED_FILES = frozenset({"index.md", "vocabulary.md"})


def is_skipped(relative: Path) -> bool:
    if relative.name.casefold() in _REGENERATED_FILES:
        return True
    if is_excluded(relative):
        return True
    return any(part in _SKIP_DIRS for part in relative.parts[:-1])


def migrate_vault_links(vault_root: Path, *, exclude_paths: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Plan note conversions without writing; the transaction owns application."""
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "applied": False,
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

    _preflight(root)
    filename_index = build_filename_index(scan_vault(root))
    images: list[dict[str, Any]] = []
    for md_path in sorted(root.rglob("*.md")):
        relative = md_path.relative_to(root)
        if is_skipped(relative) or relative.as_posix() in exclude_paths:
            continue
        try:
            _safe_note(root, relative)
            text = md_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            summary["failed"].append({"path": relative.as_posix(), "error": str(exc)})
            continue
        summary["files_scanned"] += 1
        new_text, changes = rewrite_note(text, relative, filename_index)
        if not changes:
            continue
        images.append(_image(relative.as_posix(), text, new_text))
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
    summary["_images"] = images
    return summary


# ---- transaction helpers --------------------------------------------------


def _safe_note(root: Path, relative: Path) -> Path:
    if not _valid_receipt_path(relative.as_posix()):
        raise OSError("unsafe note path")
    current = root
    # These checks reject observed symlinks, not swaps after the final check.
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise OSError(f"link in vault path: {relative}")
    if not current.is_file():
        raise OSError(f"not a regular note: {relative}")
    return current


def _preflight(root: Path) -> None:
    for name in (".ciaoignore", "entity-types.yaml"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise OSError(f"unsafe vault input: {name}")
    ignore = load_vault_ignore(root)
    for directory, directories, _ in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(directory) / name
            relative = path.relative_to(root) / "placeholder.md"
            if not is_excluded(relative) or not is_skipped(relative):
                if path.is_symlink() or path.is_junction():
                    raise OSError(f"link in vault path: {path.relative_to(root)}")
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        index_candidate = not is_excluded(relative) and not ignore.matches(relative) and not is_generated_vault_file(relative.name)
        if index_candidate or not is_skipped(relative):
            _safe_note(root, relative)


def _image(path: str, before: str, after: str) -> dict[str, str]:
    return {"path": path, "before_text": before, "after_text": after,
            "before_revision": hashlib.sha256(before.encode("utf-8")).hexdigest(),
            "after_revision": hashlib.sha256(after.encode("utf-8")).hexdigest()}


def _replace_image(root: Path, image: dict[str, Any], *, undo: bool) -> bool:
    relative = Path(image["path"])
    target = _safe_note(root, relative)
    current = target.read_bytes()
    before, after = image["before_text"].encode("utf-8"), image["after_text"].encode("utf-8")
    expected, replacement = (after, before) if undo else (before, after)
    if current == replacement:
        return False
    if current != expected:
        raise OSError("note conflicts with recorded images; preserved")
    mode = stat.S_IMODE(target.stat().st_mode)
    if not mode & 0o222:
        raise PermissionError(f"Permission denied: {relative}")
    fd, name = tempfile.mkstemp(prefix=".vault-links-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            carry_mode(stream.fileno(), mode, temp=Path(name), original=target)
            stream.write(replacement)
            stream.flush()
        _safe_note(root, relative)
        if target.read_bytes() != expected:
            raise OSError("note changed while preparing replacement")
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return True


@contextmanager
def _transaction_lock(runtime: Path):
    root = runtime.resolve()
    directory = root / "migration"
    if directory.is_symlink() or directory.is_junction():
        raise OSError("unsafe migration directory")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or directory.is_junction():
        raise OSError("unsafe migration directory")
    lock = directory / "vault-links.lock"
    if lock.is_symlink():
        raise OSError("unsafe migration lock")
    fd = open_private(lock, os.O_RDWR | os.O_CREAT, 0o600, follow_symlinks=False)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("migration lock is not regular")
        lock_exclusive(fd, blocking=False)
        yield
    finally:
        os.close(fd)


def _failure(root: Path, runtime: Path, apply: bool, error: Exception, *, undo: bool = False) -> dict[str, Any]:
    message = str(error) or f"{type(error).__name__}: could not prepare recovery evidence; no notes changed"
    failed = [{"path": str(receipt_path(runtime)), "error": message}]
    if undo:
        return {"vault_root": str(root), "receipt_vault_root": "", "applied": apply,
                "files_restored": 0, "restored": [], "failed": failed}
    return {"vault_root": str(root), "applied": apply, "files_scanned": 0,
            "files_rewritten": 0, "rewrites": [], "unresolved": [], "anchors_dropped": [],
            "failed": failed, "complete": False}


def _legacy_image(root: Path, path: str, entries: list[dict[str, Any]]) -> dict[str, str]:
    target = _safe_note(root, Path(path))
    exact = target.read_bytes().decode("utf-8")
    normalized, boundaries = _normalized_text_and_boundaries(exact)
    spans: list[tuple[int, int, str]] = []
    for entry in sorted(entries, key=lambda item: item["offset"]):
        start = entry["offset"]
        end = start + len(entry["to"])
        if end > len(normalized) or normalized[start:end] != entry["to"] or (spans and start < spans[-1][1]):
            raise OSError("legacy receipt spans no longer match; recover manually from archived receipt or git history")
        spans.append((start, end, entry["from"]))
    restored = exact
    for start, end, original in reversed(spans):
        restored = restored[:boundaries[start]] + original + restored[boundaries[end]:]
    return _image(path, restored, exact)


def migrate_links(vault_root: Path, runtime_root: Path, *, apply: bool = False, force: bool = False) -> dict[str, Any]:
    root = Path(vault_root).resolve()
    runtime = Path(runtime_root).resolve()
    resumed: set[str] = set()
    resume_failed: list[dict[str, str]] = []
    recorded: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    def run() -> dict[str, Any]:
        nonlocal recorded, progress
        loaded = _load_receipt(runtime, root)
        recorded = loaded.receipt if loaded else None
        marker = _read_marker(root) if apply else None
        binding = _binding(recorded, marker) if apply and recorded else None
        if binding == "stale":
            recorded = None  # left on disk; the next publication archives it
        # Another runtime already converted this vault: nothing to write unless fresh wikilinks turn up.
        observer = apply and recorded is None and marker is not None and marker["state"] == "migrated" and not force
        git = vault_git_state(root)
        if apply and recorded and recorded.get("operation") == "undo":
            raise OSError("finish the interrupted undo first")
        if apply and recorded and binding == "current" and _is_complete(recorded, marker) and not force:
            return {"skipped": "already migrated", "receipt_path": str(receipt_path(runtime)), "migrated_at": recorded.get("migrated_at", "")}
        if apply and not force and git["dirty"] and not observer and (recorded is None or recorded["status"] == "migrated"):
            return {"skipped": "vault has uncommitted changes", "git": git}
        resume_files = (recorded or {}).get("files", []) if apply and recorded and recorded.get("status") in ("prepared", "partial") else []
        resume_paths = frozenset(f["path"] for f in resume_files)
        if recorded is not None and resume_files:
            _preflight(root)
            if recorded["operation_id"] is None:
                claimed = dict(recorded, operation_id=_new_operation_id(), base_marker=marker)
                try:
                    _publish_receipt(runtime, claimed, archive=True)
                except (OSError, ValueError, MemoryError) as exc:
                    raise OSError(f"cannot prepare recovery receipt; no notes changed: {exc}") from exc
                recorded = claimed
            marker = _set_marker(root, recorded["operation_id"], "migrating", marker, "no notes changed")
        for image in resume_files:
            try:
                if _replace_image(root, image, undo=False):
                    resumed.add(image["path"])
            except (OSError, UnicodeError) as exc:
                resume_failed.append({"path": image["path"], "error": str(exc)})
        summary = migrate_vault_links(root, exclude_paths=resume_paths)
        progress = summary
        if apply:
            summary["files_rewritten"] = len(resumed)
        summary["failed"].extend(resume_failed)
        attempted_paths = resumed | {failure["path"] for failure in resume_failed}
        summary["rewrites"].extend(entry for entry in (recorded or {}).get("rewrites", []) if entry["path"] in attempted_paths)
        if resumed:
            summary["resumed_files"] = len(resumed)
        images = summary.pop("_images", [])
        summary.update(applied=apply, git_head_before=git["head"], forced=bool(force), complete=not summary["failed"])
        if not apply or "skipped" in summary:
            return summary
        old_files = list((recorded or {}).get("files", []))
        old_legacy = list((recorded or {}).get("legacy_rewrites", []))
        legacy_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in old_legacy:
            legacy_by_path[entry["path"]].append(entry)
        for path, entries in legacy_by_path.items():
            try:
                _legacy_image(root, path, entries)
            except (OSError, UnicodeError) as exc:
                summary["failed"].append({"path": path, "error": str(exc)})
        old_paths = {f["path"] for f in old_files} | {e["path"] for e in old_legacy}
        fresh_images = []
        for image in images:
            existing = next((f for f in old_files if f["path"] == image["path"]), None)
            if existing is not None and image == existing:
                continue
            if image["path"] in old_paths:
                raise OSError("new rewrite conflicts with existing recovery evidence; undo or recover manually")
            fresh_images.append(image)
        fresh_paths = {f["path"] for f in fresh_images}
        if observer and fresh_images and git["dirty"]:
            return {"skipped": "vault has uncommitted changes", "git": git}
        if recorded is None and marker is not None and not fresh_images:
            if observer:
                summary["observed_only"] = True
            if observer or marker["state"] != "migrated":
                return summary
        if (recorded and recorded["status"] == "migrated"
                and not fresh_images and not summary["failed"]
                and all(_safe_note(root, Path(f["path"])).read_bytes() == f["after_text"].encode("utf-8") for f in old_files)):
            summary["receipt_path"] = str(receipt_path(runtime))
            settled = {"format": MARKER_FORMAT, "operation_id": recorded["operation_id"], "state": "migrated"}
            if recorded["operation_id"] is not None and marker != settled:
                try:
                    _publish_marker(root, settled, expected=marker)
                except (ReceiptUnavailable, OSError, ValueError, MemoryError) as exc:
                    summary["failed"].append({"path": str(_marker_path(root)), "error": f"settlement failed; recovery receipt retained: {exc}"})
                    summary["complete"] = False
            return summary
        rewrites = list((recorded or {}).get("rewrites", [])) + [e for e in summary["rewrites"] if e["path"] in fresh_paths]
        files = sorted(old_files + fresh_images, key=lambda image: image["path"])
        operation_id = (recorded or {}).get("operation_id") or (_new_operation_id() if files else None)
        payload = {"schema_version": 3, "status": "prepared", "operation": "migrate", "vault_root": str(root),
                   "vault_inode": _vault_inode(root), "operation_id": operation_id, "base_marker": marker,
                   "migrated_at": datetime.now(UTC).isoformat(), "git_head_before": git["head"],
                   "files_scanned": summary["files_scanned"], "files_rewritten": len({e["path"] for e in rewrites}),
                   "files": files, "legacy_rewrites": old_legacy, "rewrites": rewrites,
                   "unresolved": _merge_notes((recorded or {}).get("unresolved"), summary["unresolved"]),
                   "anchors_dropped": _merge_notes((recorded or {}).get("anchors_dropped"), summary["anchors_dropped"]),
                   "failed": summary["failed"]}
        detail = "prior recovery receipt retained" if resumed else "no notes changed"
        try:
            _publish_receipt(runtime, payload, archive=loaded is not None)
        except (OSError, ValueError, MemoryError) as exc:
            raise OSError(f"cannot prepare recovery receipt; {detail}: {exc}") from exc
        summary["receipt_path"] = str(receipt_path(runtime))
        if files and operation_id:
            marker = _set_marker(root, operation_id, "migrating", marker, detail)
        for image in files:
            try:
                if image["path"] not in resume_paths and _replace_image(root, image, undo=False):
                    summary["files_rewritten"] += 1
            except (OSError, UnicodeError) as exc:
                summary["failed"].append({"path": image["path"], "error": str(exc)})
        summary["complete"] = not summary["failed"]
        payload["status"] = "migrated" if summary["complete"] else "partial"
        payload["failed"] = summary["failed"]
        try:
            _publish_receipt(runtime, payload, archive=False)
        except (OSError, ValueError, MemoryError) as exc:
            summary["failed"].append({"path": str(receipt_path(runtime)), "error": f"settlement failed; recovery receipt retained: {exc}"})
            summary["complete"] = False
        else:
            if summary["complete"] and files:
                try:
                    _publish_marker(root, {"format": MARKER_FORMAT, "operation_id": operation_id, "state": "migrated"}, expected=marker)
                except (ReceiptUnavailable, OSError, ValueError, MemoryError) as exc:
                    summary["failed"].append({"path": str(_marker_path(root)), "error": f"settlement failed; recovery receipt retained: {exc}"})
                    summary["complete"] = False
        return summary
    try:
        if apply:
            with _transaction_lock(runtime):
                return run()
        return run()
    except (ReceiptUnavailable, OSError, UnicodeError, ValueError, MemoryError) as exc:
        failure = _failure(root, runtime, apply, exc)
        if progress is not None:
            failure.update({key: progress[key] for key in ("files_scanned", "rewrites", "unresolved", "anchors_dropped") if key in progress})
            failure["failed"] = list(progress.get("failed", [])) + failure["failed"]
            if apply:
                failure["files_rewritten"] = progress.get("files_rewritten", len(resumed))
            if progress.get("receipt_path"):
                failure["receipt_path"] = progress["receipt_path"]
        else:
            failure["failed"] = list(resume_failed) + failure["failed"]
            attempted_paths = resumed | {item["path"] for item in resume_failed}
            failure["rewrites"] = [entry for entry in (recorded or {}).get("rewrites", []) if entry["path"] in attempted_paths]
            failure["files_scanned"] = len(attempted_paths)
        if recorded is not None:
            failure["receipt_path"] = str(receipt_path(runtime))
        if resumed:
            if progress is None:
                failure["files_rewritten"] = len(resumed)
            failure["resumed_files"] = len(resumed)
        return failure


def unmigrate_links(vault_root: Path, runtime_root: Path, *, apply: bool = False, force: bool = False) -> dict[str, Any]:
    del force
    root = Path(vault_root).resolve()
    runtime = Path(runtime_root).resolve()
    def run() -> dict[str, Any]:
        loaded = _load_receipt(runtime, root)
        if loaded is None:
            return {"skipped": "no migration receipt to reverse", "receipt_path": str(receipt_path(runtime))}
        receipt = loaded.receipt
        marker = _read_marker(root)
        if _binding(receipt, marker) == "stale":
            raise _stale(receipt, marker, runtime)
        summary: dict[str, Any] = {"vault_root": str(root), "receipt_vault_root": receipt["vault_root"], "applied": apply,
                                   "files_restored": 0, "restored": [], "failed": []}
        files = list(receipt["files"])
        legacy = receipt["legacy_rewrites"]
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in legacy:
            grouped[entry["path"]].append(entry)
        for path, entries in grouped.items():
            try:
                files.append(_legacy_image(root, path, entries))
            except (OSError, UnicodeError) as exc:
                summary["failed"].append({"path": path, "error": str(exc)})
        if summary["failed"] and not files:
            return summary
        upgraded_paths = {f["path"] for f in files}
        remaining = [e for e in legacy if e["path"] not in upgraded_paths]
        rewrites = []
        for entry in receipt["rewrites"]:
            if entry in legacy and entry["path"] in upgraded_paths:
                image = next(f for f in files if f["path"] == entry["path"])
                normalized, boundaries = _normalized_text_and_boundaries(image["after_text"])
                start = entry["offset"]
                end = start + len(entry["to"])
                assert normalized[start:end] == entry["to"]
                entry = dict(entry, offset=boundaries[start], to=image["after_text"][boundaries[start]:boundaries[end]])
            rewrites.append(entry)
        operation_id = receipt["operation_id"] or (_new_operation_id() if files else None)
        payload = dict(receipt, schema_version=3, status="prepared", operation="undo", files=files, legacy_rewrites=remaining,
                       rewrites=rewrites, operation_id=operation_id, base_marker=marker)
        if apply:
            try:
                _publish_receipt(runtime, payload, archive=receipt.get("operation") != "undo")
            except (OSError, ValueError, MemoryError) as exc:
                raise OSError(f"cannot prepare recovery receipt; no notes changed: {exc}") from exc
            if files and operation_id:
                marker = _set_marker(root, operation_id, "undoing", marker, "no notes changed")
        for image in files:
            try:
                if apply:
                    changed = _replace_image(root, image, undo=True)
                else:
                    target = _safe_note(root, Path(image["path"]))
                    current = target.read_bytes()
                    if current not in (image["before_text"].encode(), image["after_text"].encode()):
                        raise OSError("note conflicts with recorded images; preserved")
                    changed = current == image["after_text"].encode()
                if changed:
                    summary["restored"].append(image["path"])
                    summary["files_restored"] += 1
            except (OSError, UnicodeError) as exc:
                summary["failed"].append({"path": image["path"], "error": str(exc)})
        if apply:
            if summary["failed"]:
                payload["status"] = "partial"
                try:
                    _publish_receipt(runtime, payload, archive=False)
                except (OSError, ValueError, MemoryError) as exc:
                    summary["failed"].append({"path": str(receipt_path(runtime)), "error": f"settlement failed; recovery receipt retained, retry undo: {exc}"})
            else:
                try:
                    if files:
                        _publish_marker(root, {"format": MARKER_FORMAT, "operation_id": operation_id, "state": "unmigrated"}, expected=marker)
                except (ReceiptUnavailable, OSError, ValueError, MemoryError) as exc:
                    summary["receipt_removed"] = False
                    summary["failed"].append({"path": str(_marker_path(root)), "error": f"settlement failed; recovery receipt retained, retry undo: {exc}"})
                else:
                    try:
                        active_receipt = receipt_path(runtime)
                        if _unsafe_receipt_path(active_receipt):
                            raise OSError("unsafe receipt path")
                        active_receipt.unlink()
                        summary["receipt_removed"] = True
                    except OSError as exc:
                        summary["receipt_removed"] = False
                        summary["failed"].append({"path": str(receipt_path(runtime)), "error": f"cleanup failed; retry undo: {exc}"})
        return summary
    try:
        if apply:
            with _transaction_lock(runtime):
                return run()
        return run()
    except (ReceiptUnavailable, OSError, UnicodeError, ValueError, MemoryError) as exc:
        return _failure(root, runtime, apply, exc, undo=True)
