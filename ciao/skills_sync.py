#!/usr/bin/env python3
"""Change-detection for upstream skills, so install-custom-skills.sh runs
`skills update` only on skills whose source repo actually moved.

`skills update` (vercel-labs/skills) re-fetches every skill unconditionally
(~2 min for our set), so we gate it: cheaply read each source repo's remote
HEAD via `git ls-remote`, compare to a cached snapshot, and update only the
skills whose repo advanced (or that are new/uninstalled). When nothing moved,
the whole refresh is skipped.

The decision logic (`plan`) is pure and unit-tested. The CLI does the
`git ls-remote` I/O and is called by the bash installer.

CLI:
  ciao skills-sync plan  <lockfile> <cache> <installed.txt> <out.json>
      Reads the lockfile + cache + installed names, fetches remote HEADs,
      writes {"to_update", "to_prune", "skip", "heads"} to out.json and
      prints the space-joined to_update list on stdout.
  ciao skills-sync write-cache <lockfile> <heads.json> <out>
      Writes the {heads, skills} cache snapshot.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def desired_sources(lock: dict) -> dict[str, str]:
    """Map skill name -> source repo from a skills-lock.json structure."""
    return {
        name: entry["source"]
        for name, entry in lock.get("skills", {}).items()
        if entry.get("source")
    }


def plan(lock: dict, cache: dict, heads: dict[str, str], installed) -> dict:
    """Decide which skills to update / prune.

    A skill needs updating when its source repo's HEAD is *known* to have moved
    since the cached snapshot, when it isn't installed yet, or when its source
    changed. A repo whose HEAD we couldn't read (ls-remote failed, so it's
    absent from ``heads``) is treated as unchanged: a transient network failure
    must not trigger a slow full re-download of already-installed skills. A
    skill is pruned when it's installed but no longer in the lockfile.
    ``skip`` is true only when there's nothing to do.
    """
    desired = desired_sources(lock)
    cache_heads = cache.get("heads", {})
    cache_skills = cache.get("skills", {})
    installed = set(installed)
    to_update = sorted(
        name
        for name, source in desired.items()
        if (source in heads and heads[source] != cache_heads.get(source))
        or name not in installed
        or cache_skills.get(name) != source
    )
    to_prune = sorted(installed - set(desired))
    return {
        "to_update": to_update,
        "to_prune": to_prune,
        "skip": not to_update and not to_prune,
    }


def build_cache(lock: dict, heads: dict[str, str], old_cache: dict | None = None) -> dict:
    """Snapshot to persist after a run: the HEAD of each source repo, plus the
    name->source map. Newly-resolved HEADs win; for a repo whose ls-remote
    failed this run we keep its prior cached HEAD (so a transient failure
    doesn't drop the snapshot and force a re-fetch). Repos no longer in the
    lockfile are dropped."""
    desired = desired_sources(lock)
    repos = sorted(set(desired.values()))
    old_heads = (old_cache or {}).get("heads", {})
    merged = {**old_heads, **heads}
    return {
        "heads": {r: merged[r] for r in repos if r in merged},
        "skills": desired,
    }


# ── git ls-remote I/O (not unit-tested; thin) ────────────────────────────


def _ls_remote_head(repo: str) -> tuple[str, str | None]:
    """Return (repo, head_sha or None) for a GitHub ``owner/repo``."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return repo, None
        return repo, out.stdout.split()[0]
    except (subprocess.SubprocessError, OSError):
        return repo, None


def remote_heads(repos) -> dict[str, str]:
    """Resolve HEADs for all repos in parallel; drop the ones that failed."""
    repos = list(repos)
    if not repos:
        return {}
    heads: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        for repo, sha in pool.map(_ls_remote_head, repos):
            if sha:
                heads[repo] = sha
    return heads


def _load_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: ciao skills-sync {plan|write-cache} ...", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "plan":
        lockfile, cache_path, installed_path, out_path = argv[1:5]
        lock = _load_json(lockfile)
        cache = _load_json(cache_path)
        installed_file = Path(installed_path)
        installed = installed_file.read_text().split() if installed_file.exists() else []
        heads = remote_heads(set(desired_sources(lock).values()))
        result = plan(lock, cache, heads, installed)
        result["heads"] = heads
        Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(" ".join(result["to_update"]))
        return 0
    if cmd == "write-cache":
        # Merge into the existing cache at cache_path (in place) so HEADs for
        # repos that failed ls-remote this run are preserved.
        lockfile, heads_path, cache_path = argv[1:4]
        lock = _load_json(lockfile)
        heads = _load_json(heads_path)
        old_cache = _load_json(cache_path)
        Path(cache_path).write_text(
            json.dumps(build_cache(lock, heads, old_cache), indent=2),
            encoding="utf-8",
        )
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
