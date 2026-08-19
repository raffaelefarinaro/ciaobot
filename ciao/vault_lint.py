"""Vault hygiene linter logic."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from ciao.vault_index import (
    canonical_type,
    is_generated_vault_file,
    resolve_vault_link,
    vault_link_ref,
)

# Fenced code blocks and inline code spans are prose *about* links, not real
# links — guides and changelogs routinely document link syntax. Strip them
# before extracting links so documented syntax isn't flagged.
_FENCE_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
# Reserved structural filenames that carry no frontmatter by design (the
# generated ones plus OKF's `log.md`). Already casefolded at the call site.
_FRONTMATTER_EXEMPT = {"index.md", "memory.md", "vocabulary.md", "log.md"}

_REFERENCE_DESTINATION_RE = re.compile(
    r"(?m)^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(<[^>\n]+>|[^\s]+)"
)
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class _VaultFile:
    path: Path
    relative: Path
    content: str

# Common structural filenames that legitimately recur across folders (one
# README/log/etc. per project). Same stem across folders is not a duplicate.
_COMMON_STEMS = {
    "readme", "index", "log", "notes", "general", "overview",
    "changelog", "todo", "template",
}

# Directories that aren't vault content: app state, generated projections, tool
# caches, and any venv/node_modules checked out inside the vault root (#129).
EXCLUDE_DIRS = {
    "Logs", "Templates", ".obsidian",
    ".venv", "venv", "node_modules", ".git",
    ".claude", ".agents", ".codex", ".opencode", "__pycache__",
}


class VaultTraversalError(RuntimeError):
    """Raised when the vault cannot be fully inspected."""

    def __init__(self, errors: Sequence[Exception]) -> None:
        self.errors = tuple(errors)
        detail = "; ".join(str(error) for error in errors)
        super().__init__(f"vault traversal failed: {detail}")


def _is_excluded(relative: Path) -> bool:
    return any(part in EXCLUDE_DIRS for part in relative.parts)


def _discover_paths(vault_root: Path) -> list[tuple[Path, Path]]:
    errors: list[OSError] = []

    def onerror(error: OSError) -> None:
        errors.append(error)

    discovered: list[tuple[Path, Path]] = []
    try:
        for directory, directories, files in os.walk(
            vault_root,
            topdown=True,
            onerror=onerror,
            followlinks=False,
        ):
            directories.sort()
            files.sort()
            directory_path = Path(directory)
            for name in [*directories, *files]:
                path = directory_path / name
                try:
                    relative = path.relative_to(vault_root)
                except ValueError:
                    continue
                discovered.append((path, relative))
    except OSError as error:
        errors.append(error)

    if errors:
        raise VaultTraversalError(errors)
    return discovered


def _canonical_relative(path: Path, vault_root: Path) -> Path | None:
    try:
        return path.resolve(strict=False).relative_to(vault_root)
    except ValueError:
        return None
    except (OSError, RuntimeError) as error:
        raise VaultTraversalError([error]) from error


def _markdown_source_paths(
    vault_root: Path,
    *,
    discovered: list[tuple[Path, Path]] | None = None,
) -> list[tuple[Path, Path]]:
    try:
        root = vault_root.resolve()
    except (OSError, RuntimeError) as error:
        raise VaultTraversalError([error]) from error
    paths = discovered if discovered is not None else _discover_paths(vault_root)
    return [
        (path, relative)
        for path, relative in paths
        if relative.suffix.lower() == ".md"
        and not _is_excluded(relative)
        and _canonical_relative(path, root) is not None
    ]


def _links_in(text: str):
    """Yield the in-vault markdown link refs in ``text``.

    Refs come back *note-relative* and extension-less (`./People/Mo`,
    `../Projects/Foo`): resolving them needs the containing note's path, which
    only the caller has. Reuses `_markdown_destinations_in`, so code
    spans/fences, backslash-escaped brackets, and reference-style definitions
    behave exactly as they do for broken-link validation — one parser, one set
    of rules. Anything that is not an in-vault markdown target (a URL, an
    image, a `<placeholder>`, a bare `#anchor`, a leftover `[[wikilink]]`) is
    dropped.
    """
    for destination in _markdown_destinations_in(text):
        ref = vault_link_ref(destination)
        if ref:
            yield ref


def _is_template(stem: str) -> bool:
    return "template" in stem.lower()


def _frontmatter_error(file: _VaultFile) -> dict[str, str] | None:
    if file.relative.name.lower() in _FRONTMATTER_EXEMPT:
        return None

    match = _FRONTMATTER_RE.match(file.content)
    if match is None:
        return {
            "source": file.relative.as_posix(),
            "kind": "missing_frontmatter",
            "message": "frontmatter is missing",
        }

    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        metadata = None
    if not isinstance(metadata, dict):
        return {
            "source": file.relative.as_posix(),
            "kind": "malformed_frontmatter",
            "message": "frontmatter is malformed",
        }

    if "type" not in metadata:
        return {
            "source": file.relative.as_posix(),
            "kind": "missing_type",
            "message": "frontmatter type is missing or empty",
        }
    page_type = metadata["type"]
    if isinstance(page_type, str) and not page_type.strip():
        return {
            "source": file.relative.as_posix(),
            "kind": "missing_type",
            "message": "frontmatter type is missing or empty",
        }
    if not isinstance(page_type, str):
        return {
            "source": file.relative.as_posix(),
            "kind": "invalid_type",
            "message": "frontmatter type must be a string",
        }
    # `type:` is a closed vocabulary. Without this the index grows one section
    # per synonym and the agent has no reason to reuse an existing category.
    # An aliased value names its target so the fix is a rename, not a decision.
    canonical = canonical_type(page_type)
    if canonical != page_type.strip():
        suggestion = f"; use '{canonical}'" if canonical else ""
        return {
            "source": file.relative.as_posix(),
            "kind": "unknown_type",
            "message": (
                f"frontmatter type '{page_type.strip()}' is not canonical"
                f"{suggestion}"
            ),
        }
    return None


def _without_code(text: str) -> str:
    return _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _matching_bracket(text: str, opening: int) -> int | None:
    depth = 0
    index = opening
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _markdown_title_end(text: str, start: int) -> int | None:
    if start >= len(text):
        return None
    opener = text[start]
    if opener in {"\"", "'"}:
        index = start + 1
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == opener:
                return index + 1
            index += 1
        return None
    if opener != "(":
        return None

    depth = 1
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _inline_destination(text: str, opening: int) -> tuple[str, int] | None:
    index = opening + 1
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return None

    if text[index] == "<":
        start = index + 1
        index = start
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == ">":
                raw = text[start:index]
                index += 1
                break
            index += 1
        else:
            return None
    else:
        start = index
        depth = 0
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                if depth == 0:
                    raw = text[start:index]
                    break
                depth -= 1
            elif text[index].isspace() and depth == 0:
                raw = text[start:index]
                break
            index += 1
        else:
            return None

    while index < len(text) and text[index].isspace():
        index += 1
    if index < len(text) and text[index] == ")":
        return raw, index + 1

    title_end = _markdown_title_end(text, index)
    if title_end is None:
        return None
    index = title_end
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != ")":
        return None
    return raw, index + 1


def _inline_markdown_destinations(text: str):
    destinations: list[tuple[int, str]] = []
    index = 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "!" and index + 1 < len(text) and text[index + 1] == "[":
            label_opening = index + 1
            source = index
        elif text[index] == "[":
            label_opening = index
            source = index
        else:
            index += 1
            continue
        if _is_escaped(text, source):
            index += 1
            continue

        label_close = _matching_bracket(text, label_opening)
        if label_close is None:
            index += 1
            continue
        after_label = label_close + 1
        while after_label < len(text) and text[after_label].isspace():
            after_label += 1
        if after_label >= len(text) or text[after_label] != "(":
            index = label_close + 1
            continue
        parsed = _inline_destination(text, after_label)
        if parsed is None:
            index = label_close + 1
            continue
        raw, end = parsed
        destinations.append((source, raw))
        index = end

    return destinations


def _unescape_markdown_destination(target: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(target):
        if target[index] == "\\" and index + 1 < len(target):
            result.append(target[index + 1])
            index += 2
            continue
        result.append(target[index])
        index += 1
    return "".join(result)


def _markdown_destinations_in(text: str):
    stripped = _without_code(text)
    matches = [
        *[(position, raw) for position, raw in _inline_markdown_destinations(stripped)],
        *[
            (match.start(), match.group(1))
            for match in _REFERENCE_DESTINATION_RE.finditer(stripped)
            if not _is_escaped(stripped, match.start())
        ],
    ]
    for _, raw in sorted(matches, key=lambda item: item[0]):
        target = raw.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        yield _unescape_markdown_destination(target)


def _markdown_link_error(
    *,
    vault_root: Path,
    file: _VaultFile,
    target: str,
    valid_paths: set[str],
) -> dict[str, str] | None:
    if not target or "<" in target or ">" in target:
        return None
    if target.startswith(("//", "/", "#")):
        return None
    if _URI_SCHEME_RE.match(target):
        return None

    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    decoded_path = unquote(parsed.path)
    if not decoded_path or _is_non_local_decoded_path(decoded_path):
        return None
    if any(ord(character) < 32 for character in decoded_path):
        return {
            "source": file.relative.as_posix(),
            "target": target,
            "resolved": target,
            "kind": "missing_target",
        }

    lexical = Path(
        os.path.normpath((file.relative.parent / decoded_path).as_posix())
    )
    if lexical == Path("..") or Path("..") in lexical.parents:
        return {
            "source": file.relative.as_posix(),
            "target": target,
            "resolved": lexical.as_posix(),
            "kind": "outside_vault",
        }

    root = vault_root.resolve()
    try:
        resolved = (file.path.parent / decoded_path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return {
            "source": file.relative.as_posix(),
            "target": target,
            "resolved": target,
            "kind": "missing_target",
        }
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return {
            "source": file.relative.as_posix(),
            "target": target,
            "resolved": lexical.as_posix(),
            "kind": "outside_vault",
        }

    if relative.as_posix() in valid_paths:
        return None
    return {
        "source": file.relative.as_posix(),
        "target": target,
        "resolved": relative.as_posix(),
        "kind": "missing_target",
    }


def _is_non_local_decoded_path(path: str) -> bool:
    if path.startswith(("//", "/", "#")):
        return True
    windows_path = PureWindowsPath(path)
    return windows_path.is_absolute() or bool(windows_path.root)


def run_validation(vault_root: Path) -> dict:
    """Read-only scan for four vault health result lists.

    The returned keys are ``orphans``, ``duplicates``, ``frontmatter_errors``,
    and ``broken_markdown_links``. Unreadable or non-UTF-8 Markdown sources are
    skipped here; ``os-audit`` reports those files separately as scan errors.

    There is no separate ``broken_links`` bucket any more. It reported dead
    wikilinks, and with markdown links the only dialect, a dead link is a dead
    markdown link — already found by ``_markdown_link_error`` over every file,
    with a resolved path and a kind. Two buckets for one condition could only
    disagree.
    """
    issues: dict[str, list[Any]] = {
        "orphans": [],
        "duplicates": [],
        "frontmatter_errors": [],
        "broken_markdown_links": [],
    }

    discovered = _discover_paths(vault_root)
    valid_paths = {relative.as_posix() for _, relative in discovered}
    valid_paths.add(Path(".").as_posix())
    vault_files: list[_VaultFile] = []
    for path, rel in _markdown_source_paths(vault_root, discovered=discovered):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        vault_files.append(_VaultFile(path=path, relative=rel, content=content))

    for file in vault_files:
        error = _frontmatter_error(file)
        if error is not None:
            issues["frontmatter_errors"].append(error)

    files_to_scan: list[_VaultFile] = []
    # Keyed by vault-relative path without suffix — the one form a resolved
    # markdown link produces. A wikilink could name a bare stem from anywhere in
    # the vault; a relative link always names a location, so a second stem-keyed
    # bucket would now be dead weight.
    incoming_links: dict[str, list[str]] = {}

    link_target_files = [
        file
        for file in vault_files
        if not is_generated_vault_file(file.relative.name)
    ]
    files_to_scan = [
        file
        for file in link_target_files
        if not _is_template(file.path.stem)
    ]

    normalized_names: dict[str, list[str]] = {}

    for file in link_target_files:
        target_stem = file.path.stem
        incoming_links[file.relative.with_suffix("").as_posix()] = []

        # Duplicate detection: skip common structural stems (README/log/etc.)
        # that legitimately repeat per folder, and template files.
        if target_stem.lower() not in _COMMON_STEMS and not _is_template(target_stem):
            norm = target_stem.lower().replace("-", "").replace("_", "")
            normalized_names.setdefault(norm, []).append(file.relative.as_posix())

    for norm, paths in normalized_names.items():
        if len(paths) > 1:
            issues["duplicates"].append(paths)

    for file in files_to_scan:
        rel_str = file.relative.as_posix()
        for ref in _links_in(file.content):
            target = resolve_vault_link(file.relative, ref)
            # A link to a file that does not exist is reported below as a
            # broken markdown link; here it simply grants no incoming edge.
            if target in incoming_links:
                incoming_links[target].append(rel_str)

    # Check links from workspace memory roots. Any directly nested MEMORY.md in
    # the already discovered/read records counts, so alternate workspace names
    # retain the same behavior without a second fallible root traversal.
    memory_roots = sorted(
        (
            file
            for file in vault_files
            if len(file.relative.parts) == 2
            and file.relative.name == "MEMORY.md"
        ),
        key=lambda file: file.relative.as_posix(),
    )
    memory_links = set()
    for record in memory_roots:
        for ref in _links_in(record.content):
            target = resolve_vault_link(record.relative, ref)
            if target:
                memory_links.add(target)

    orphan_candidate_dirs = {"People", "Projects", "Ideas", "Resources", "Places", "projects", "references"}

    for file in files_to_scan:
        rel_path = file.relative
        rel_no_sfx = rel_path.with_suffix("").as_posix()
        rel_str = file.relative.as_posix()

        if not any(part in orphan_candidate_dirs for part in rel_path.parts):
            continue

        if not incoming_links.get(rel_no_sfx) and rel_no_sfx not in memory_links:
            issues["orphans"].append(rel_str)

    for file in vault_files:
        for target in _markdown_destinations_in(file.content):
            error = _markdown_link_error(
                vault_root=vault_root,
                file=file,
                target=target,
                valid_paths=valid_paths,
            )
            if error is not None:
                issues["broken_markdown_links"].append(error)

    return issues
