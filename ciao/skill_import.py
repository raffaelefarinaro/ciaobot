"""Validated skill import from zip archives."""

from __future__ import annotations

import io
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from ciao.skill_evolution import MAX_SKILL_BYTES

# One import at a time per skill name. Two concurrent imports of the SAME
# skill otherwise interleave between the exists() check and the final rename:
# both pass the overwrite=False check, the later one deletes the directory the
# earlier one just installed, and both requests report success while one
# archive has silently replaced the other.
_SKILL_IMPORT_LOCKS_GUARD = threading.Lock()
_SKILL_IMPORT_LOCKS: dict[str, threading.Lock] = {}


def _skill_import_lock(name: str) -> threading.Lock:
    with _SKILL_IMPORT_LOCKS_GUARD:
        lock = _SKILL_IMPORT_LOCKS.get(name)
        if lock is None:
            lock = threading.Lock()
            _SKILL_IMPORT_LOCKS[name] = lock
        return lock

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Hard caps on decompressed archive contents. The upload is bounded at 10 MB on
# the wire, but a highly compressed member can expand far beyond that once
# decompressed, so a small archive could otherwise exhaust server memory or
# disk. These caps bound the memory/disk a single import may consume.
MAX_SKILL_ASSET_BYTES = 5 * 1024 * 1024  # per non-SKILL.md member
MAX_SKILL_TOTAL_BYTES = 20 * 1024 * 1024  # whole archive, decompressed

# A skill directory name is a single, non-dot path segment. Rejecting "." and
# ".." (and any leading-dot or separator forms) keeps extraction inside
# ``skills/<name>/`` so the imported skill is discoverable by inventory and
# sync, which only look under ``skills/<name>/SKILL.md``.
def _is_valid_skill_dir_name(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    if name.startswith("."):
        return False
    if "/" in name or "\\" in name:
        return False
    return True


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    raw = m.group(1)
    data: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"\'')
    return data


def validate_skill_zip(zip_bytes: bytes) -> tuple[str | None, list[str]]:
    """Validate a skill zip archive.

    Returns (skill_name, errors). If errors is non-empty the zip is invalid.
    Checks: zip integrity, zip-slip, exactly one top-level folder, SKILL.md
    exists, frontmatter name/description present, SKILL.md size ≤ MAX_SKILL_BYTES,
    folder name matches frontmatter name, folder name is a valid non-dot skill
    directory name, and decompressed contents stay within the per-file and
    total size caps.
    """
    errors: list[str] = []
    if not zip_bytes:
        return None, ["Empty file."]
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None, ["Invalid zip file."]
    try:
        names = zf.namelist()
        if not names:
            return None, ["Zip is empty."]
        # zip-slip check
        for name in names:
            # Normalize
            if ".." in Path(name).parts or name.startswith("/") or name.startswith("\\"):
                return None, ["Zip contains path traversal (../)."]
            # Also reject absolute or drive-absolute entries
            if "\x00" in name:
                return None, ["Zip contains invalid entry."]
        # Bound decompressed contents before any member is materialized. A
        # highly compressed member can expand far beyond the 10 MB upload cap,
        # so validate the declared uncompressed sizes up front.
        total_uncompressed = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.file_size > MAX_SKILL_ASSET_BYTES:
                return None, [
                    f"Archive member '{info.filename}' exceeds "
                    f"{MAX_SKILL_ASSET_BYTES} bytes uncompressed."
                ]
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_SKILL_TOTAL_BYTES:
                return None, [
                    f"Archive exceeds {MAX_SKILL_TOTAL_BYTES} bytes uncompressed."
                ]
        # Determine top-level folders: first segment before "/"
        top_levels: set[str] = set()
        for name in names:
            # ignore directory entries like "foo/"? still counts.
            parts = name.split("/")
            # Filter empty due to trailing slash
            if parts[0]:
                top_levels.add(parts[0])
            # Also detect entries at root without folder (e.g. SKILL.md at root)
            # That would imply not in a folder; we will error later.
        # Remove empty and handle entries like "__MACOSX"
        # But strictly: must be exactly one top-level folder
        # Filter out files that are directly at root (no slash) – they indicate missing wrapper folder
        # If any entry has no slash and is not a directory, then it's a file at root
        has_root_file = any("/" not in n.rstrip("/") and not n.endswith("/") for n in names if n.strip("/"))
        # For validation, we require exactly one top-level folder
        # Exclude hidden __MACOSX entries from count? Plan says exactly one top-level folder, so be strict
        # But ignore __MACOSX folders for tolerance
        filtered_tops = {t for t in top_levels if t != "__MACOSX"}
        if len(filtered_tops) != 1:
            return None, ["Zip must contain exactly one top-level folder."]
        skill_folder = next(iter(filtered_tops))
        if not _is_valid_skill_dir_name(skill_folder):
            return None, [f"Skill folder '{skill_folder}' is not a valid skill directory name."]
        # Check that there is a SKILL.md inside that folder at top level
        skill_md_path = f"{skill_folder}/SKILL.md"
        # Need to check case sensitive
        # Zip entries may have directory prefix without exact match
        if skill_md_path not in names:
            # also handle if SKILL.md is inside but with extra nesting? Must be exactly at top level folder
            return None, [f"Missing {skill_folder}/SKILL.md."]
        # Read SKILL.md and validate size and frontmatter
        try:
            data = zf.read(skill_md_path)
        except KeyError:
            return None, [f"Missing {skill_folder}/SKILL.md."]
        if len(data) > MAX_SKILL_BYTES:
            return None, [f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes (cap {MAX_SKILL_BYTES})."]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, ["SKILL.md is not valid UTF-8."]
        fm = _parse_frontmatter(text)
        name = fm.get("name", "").strip()
        description = fm.get("description", "").strip()
        if not name:
            errors.append("SKILL.md frontmatter must contain 'name'.")
        if not description:
            errors.append("SKILL.md frontmatter must contain 'description'.")
        if errors:
            return None, errors
        if name != skill_folder:
            return None, [f"Frontmatter name '{name}' must match folder '{skill_folder}'."]
        # Also ensure SKILL.md itself is valid (starts with ---)
        if not text.startswith("---"):
            return None, ["SKILL.md must start with frontmatter '---'."]
        return name, []
    finally:
        try:
            zf.close()
        except Exception:
            pass


def extract_skill_zip(zip_bytes: bytes, dest_root: Path, *, overwrite: bool = False) -> tuple[str | None, list[str]]:
    """Validate and extract zip to skills/<name>/.

    Returns (skill_name, errors). On success extracts to dest_root/<name>/ .
    Extraction is transactional: members are written to a temporary directory
    and the target is replaced atomically only after every member succeeds, so
    a truncated or corrupt member never leaves a partially installed skill.
    """
    name, errors = validate_skill_zip(zip_bytes)
    if errors or not name:
        return None, errors
    # Serialize same-name imports: the existence check and the final
    # rename must not interleave, or two concurrent non-force imports of
    # the same skill both pass the check and the second deletes the
    # first's just-installed directory (the route dispatches extraction
    # via asyncio.to_thread, so this genuinely runs in parallel).
    lock = _skill_import_lock(name)
    with lock:
        return _extract_locked(zip_bytes, dest_root, name, overwrite=overwrite)


def _extract_locked(
    zip_bytes: bytes, dest_root: Path, name: str, *, overwrite: bool
) -> tuple[str | None, list[str]]:
    target = dest_root / name
    if target.exists() and not overwrite:
        return None, [f"Skill '{name}' already exists. Use force to overwrite."]
    # Extract into a uniquely named sibling temp dir, then move into place only
    # on success. A unique name (not PID-based) keeps concurrent imports of the
    # same skill from colliding on the same directory.
    tmp_target = Path(tempfile.mkdtemp(prefix=f".{name}.tmp-", dir=dest_root))

    def _fail(errors: list[str]) -> tuple[str | None, list[str]]:
        _remove_path(tmp_target)
        return None, errors

    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    try:
        total_written = 0
        for info in zf.infolist():
            # Zip slip already checked, but also sanitize extraction path
            filename = info.filename
            # Skip directory entries for the top level itself? We'll extract all under folder
            # Ensure we only extract under dest_root/name
            # info.filename starts with skill_folder/
            if filename.startswith(f"{name}/"):
                rel = filename[len(f"{name}/") :]
            elif filename == f"{name}/" or filename == name:
                continue
            else:
                # ignore entries outside expected folder (e.g. __MACOSX)
                if filename.startswith("__MACOSX"):
                    continue
                # Should not happen due to validation, skip
                continue
            if not rel:
                continue
            dest_path = (tmp_target / rel).resolve()
            # Ensure dest_path is inside tmp_target
            try:
                dest_path.relative_to(tmp_target.resolve())
            except ValueError:
                return _fail(["Zip contains path traversal."])
            if info.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                # Stream the member through the per-file and total caps so a
                # zip that lies about its declared uncompressed size cannot
                # exhaust memory or disk during extraction.
                written = 0
                with zf.open(info) as src, dest_path.open("wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_SKILL_ASSET_BYTES:
                            return _fail([
                                f"Archive member '{info.filename}' exceeds "
                                f"{MAX_SKILL_ASSET_BYTES} bytes uncompressed."
                            ])
                        total_written += len(chunk)
                        if total_written > MAX_SKILL_TOTAL_BYTES:
                            return _fail([
                                f"Archive exceeds {MAX_SKILL_TOTAL_BYTES} bytes uncompressed."
                            ])
                        out.write(chunk)
    except Exception:
        _remove_path(tmp_target)
        raise
    finally:
        zf.close()
    # All members succeeded: atomically replace the target.
    if target.exists():
        _remove_path(target)
    tmp_target.rename(target)
    return name, []
