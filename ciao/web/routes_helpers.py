from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

from starlette.responses import JSONResponse, Response

_LINE_SUFFIX_RE = re.compile(r":\d+$")


def _allowed_roots(config, for_write: bool = False) -> list[Path]:
    """Resolve the union of roots the viewer endpoints may serve from.

    The primary workspace root always comes first (relative paths anchor
    here, preserving the historical "the workspace is your project" mental
    model). Extra roots come from ``config.extra_workspace_roots`` and let
    the viewer reach files outside the project tree, e.g. repos under
    ``~/repos`` linked into projects via the repo-link feature.

    ``for_write`` controls the unrestricted-viewer escape hatch: when the
    config opts in (``unrestricted_file_viewer``), read-only endpoints get
    a catch-all ``/`` root so any absolute path serves. Writes never get
    ``/`` — the editor stays sandboxed to the real roots regardless, so a
    loosened read policy can't be turned into arbitrary file creation.
    """
    roots: list[Path] = [config.workspace_root.resolve()]
    vault_root = getattr(config, "vault_root", None)
    if vault_root is not None:
        try:
            r = Path(vault_root).expanduser().resolve()
        except (OSError, ValueError):
            r = None
        if r is not None and r not in roots:
            roots.append(r)
    for extra in getattr(config, "extra_workspace_roots", None) or []:
        try:
            r = Path(extra).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if r not in roots:
            roots.append(r)
    # Unrestricted mode: accept any absolute path. We model this as a
    # catch-all root of ``/`` so the existing ``is_relative_to`` checks in
    # the resolver pass for every absolute path. Relative paths still
    # anchor to ``roots[0]`` (the workspace), so workspace semantics are
    # unchanged. ``_find_fuzzy_match`` prunes ``/`` from its walk list so
    # a missing path never triggers a full-filesystem walk.
    if getattr(config, "unrestricted_file_viewer", False) and not for_write:
        roots.append(Path("/"))
    return roots


def _find_fuzzy_match(roots: list[Path], candidate: Path) -> Path | None:
    """Find a file in the allowed roots that matches the candidate path.

    Matches by:
    1. Case-sensitive suffix match
    2. Case-insensitive suffix match
    3. Case-sensitive suffix match ignoring file extension
    4. Case-insensitive suffix match ignoring file extension
    5. Filename match
    6. Case-insensitive filename match
    7. Filename match ignoring file extension
    8. Case-insensitive filename match ignoring file extension

    Returns the resolved Path to the best matching file, or None if no match.
    """
    candidate_parts = candidate.parts
    if not candidate_parts:
        return None

    candidate_stem = candidate.stem
    candidate_name = candidate.name

    matches = []
    # If the candidate is a relative path, we only search the primary workspace root.
    # If it is absolute, we can search all allowed roots. Never walk a catch-all
    # ``/`` root (unrestricted mode) — a full-filesystem walk is never sane and
    # would hang the request. The direct-resolve path handles real absolute paths
    # before fuzzy ever runs.
    search_roots = [roots[0]] if not candidate.is_absolute() else [
        r for r in roots if r != Path("/")
    ]

    for root in search_roots:
        try:
            orig_root_idx = roots.index(root)
        except ValueError:
            orig_root_idx = len(roots)
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune directory search path in-place
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and d not in {"node_modules", "__pycache__", "dist", "build", "ciao.egg-info"}
            ]

            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    resolved_fpath = fpath.resolve()
                except (OSError, ValueError):
                    continue

                # Security boundary: ensure the resolved path actually resides under an allowed root
                if not any(resolved_fpath.is_relative_to(r) for r in roots):
                    continue

                try:
                    rel_parts = resolved_fpath.relative_to(root).parts
                except ValueError:
                    continue

                fname_lower = fname.lower()
                candidate_name_lower = candidate_name.lower()
                fstem_lower = resolved_fpath.stem.lower()
                candidate_stem_lower = candidate_stem.lower()

                # Rule 1 & 2: Suffix matches
                if len(rel_parts) >= len(candidate_parts):
                    suffix_parts = rel_parts[-len(candidate_parts):]
                    if suffix_parts == candidate_parts:
                        matches.append((1, orig_root_idx, len(resolved_fpath.parts), resolved_fpath))
                        continue
                    if tuple(p.lower() for p in suffix_parts) == tuple(p.lower() for p in candidate_parts):
                        matches.append((2, orig_root_idx, len(resolved_fpath.parts), resolved_fpath))
                        continue

                # Rule 3 & 4: Suffix matches ignoring extension
                if len(rel_parts) >= len(candidate_parts):
                    suffix_parts = list(rel_parts[-len(candidate_parts):])
                    suffix_parts[-1] = Path(suffix_parts[-1]).stem
                    cand_parts_no_ext = list(candidate_parts)
                    cand_parts_no_ext[-1] = candidate_stem
                    if suffix_parts == cand_parts_no_ext:
                        matches.append((3, orig_root_idx, len(resolved_fpath.parts), resolved_fpath))
                        continue
                    if [p.lower() for p in suffix_parts] == [p.lower() for p in cand_parts_no_ext]:
                        matches.append((4, orig_root_idx, len(resolved_fpath.parts), resolved_fpath))
                        continue

                # Rule 5 & 6: Filename matches
                if fname == candidate_name:
                    matches.append((5, orig_root_idx, len(resolved_fpath.parts), resolved_fpath))
                    continue
                if fname_lower == candidate_name_lower:
                    matches.append((6, orig_root_idx, len(resolved_fpath.parts), resolved_fpath))
                    continue

                # Rule 7 & 8: Filename matches ignoring extension
                if resolved_fpath.stem == candidate_stem:
                    matches.append((7, orig_root_idx, len(resolved_fpath.parts), resolved_fpath))
                    continue
                if fstem_lower == candidate_stem_lower:
                    matches.append((8, orig_root_idx, len(resolved_fpath.parts), resolved_fpath))
                    continue

    if not matches:
        return None

    # Sort matches by:
    # 1. Rule index (1-8, lower is better)
    # 2. Original root order (lower index is better)
    # 3. Path depth (shorter path is better)
    # 4. Lexicographical order of path representation
    matches.sort(key=lambda x: (x[0], x[1], x[2], str(x[3])))
    return matches[0][3]


def _resolve_workspace_path(roots: list[Path], raw: str, allow_fuzzy: bool = False) -> Path | Response:
    """Shared path resolver for the workspace-file/binary/image endpoints.

    Returns the canonicalised ``Path`` on success, or a ``Response`` carrying
    the appropriate HTTP error (400 bad request, 403 forbidden, 404 missing).
    Extension and size checks live in the callers since they differ per
    endpoint.

    Relative paths anchor against the first root (the primary workspace).
    Absolute paths are accepted if they canonicalise under any root in the
    list. This keeps the common case (clicking a workspace-relative path in
    a chat bubble) unchanged while letting the PWA open files in linked
    repos and any other admin-configured extra roots.
    """
    if not raw:
        return JSONResponse({"error": "missing path"}, status_code=400)
    # Reject NUL bytes and other non-printable chars up front: Path() would
    # raise on NUL but happily accept control chars that never belong in a
    # filesystem path from a web request.
    if "\x00" in raw or not raw.isprintable():
        return JSONResponse({"error": "bad path"}, status_code=400)
    raw = _LINE_SUFFIX_RE.sub("", raw)

    try:
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            # Relative paths anchor to the primary workspace root; we don't
            # try each root in turn because that would silently shadow files
            # in the workspace with same-named files in repos and produce
            # surprising routing.
            resolved = (roots[0] / candidate).resolve()
    except (OSError, ValueError):
        return JSONResponse({"error": "bad path"}, status_code=400)

    if resolved.is_file() and any(resolved.is_relative_to(root) for root in roots):
        return resolved

    if allow_fuzzy:
        fuzzy_resolved = _find_fuzzy_match(roots, candidate)
        if fuzzy_resolved and fuzzy_resolved.is_file() and any(fuzzy_resolved.is_relative_to(root) for root in roots):
            return fuzzy_resolved

    if not any(resolved.is_relative_to(root) for root in roots):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not resolved.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return resolved


async def _commit_and_push(workspace: Path, message: str) -> tuple[bool, str]:
    """Stage everything, commit, push. Returns (ok, details).

    If the working tree is clean, skips commit and still pushes.
    """
    async def _git(*args: str) -> tuple[int, str]:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        combined = (out + ("\n" + err if err else "")).strip()
        return result.returncode, combined[:1000]

    rc, out = await _git("add", "-A")
    if rc != 0:
        return False, f"git add failed: {out}"

    rc_diff, _ = await _git("diff", "--quiet", "--cached")
    if rc_diff != 0:
        rc, out = await _git("commit", "-m", message)
        if rc != 0:
            return False, f"git commit failed: {out}"

    rc, out = await _git("pull")
    if rc != 0:
        return False, f"git pull failed: {out}"

    rc, out = await _git("push")
    if rc != 0:
        return False, f"git push failed: {out}"

    return True, out or "pushed"
