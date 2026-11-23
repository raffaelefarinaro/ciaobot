#!/usr/bin/env python3
"""Report which upstream projects Ciaobot depends on have gone quiet.

`/ciao-upkeep` compared each pin with upstream's latest release and never asked
how old that release was, so `gws` sat dormant for eight months while its row
read "installed = latest". This script asks that question.

It reads the dependency list from the files that declare it (``pyproject.toml``,
``web/package.json``, the ACP adapter pins, ``mac/Package.swift`` and the
workflow ``uses:`` lines), looks up the date of each project's latest release
and flags a project when that release is older than ``STALE_DAYS``, when the
registry marks it deprecated or inactive, or when its GitHub repo is archived.

Read-only: it changes nothing. Needs network access and an authenticated ``gh``
for the GitHub lookups.

    python3 scripts/upstream-health.py            # flagged projects only
    python3 scripts/upstream-health.py --all      # every project
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: A latest release older than this sends the project to the activity check.
STALE_DAYS = 180

#: Window for the activity check: a project with an old release but recent
#: substantive commits is "slow", one without is "dormant".
ACTIVE_DAYS = 90

#: Commits that keep a repo looking alive without anyone working on the code.
_HOUSEKEEPING = re.compile(r"^(ci|chore|build|docs|style|test)(\(.*?\))?!?:|^(bump|update) .*dependenc|^merge ", re.I)

#: Upstreams no manifest in this repo declares: CLIs installed on the user's
#: machine that Ciaobot drives or shells out to.
EXTRA_GITHUB = ("anomalyco/opencode", "googleworkspace/cli")


@dataclass
class Health:
    source: str
    name: str
    latest: str = "?"
    released: datetime | None = None
    flags: tuple[str, ...] = ()
    repo: str = ""
    verdict: str = ""

    @property
    def age_days(self) -> int | None:
        return (datetime.now(UTC) - self.released).days if self.released else None


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ciao-upkeep"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def _github_repo(*urls: object) -> str:
    for url in urls:
        m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?]|$)", str(url or ""))
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return ""


def _req_name(spec: str) -> str:
    return re.split(r"[\s\[<>=!~;]", spec.strip(), maxsplit=1)[0]


def python_deps() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    specs = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        specs.extend(extra)
    return sorted({_req_name(s) for s in specs})


def npm_deps() -> list[str]:
    pkg = json.loads((ROOT / "web" / "package.json").read_text())
    names = set(pkg.get("dependencies", {})) | set(pkg.get("devDependencies", {}))
    adapters = (ROOT / "ciao" / "adapter_install.py").read_text()
    names |= set(re.findall(r'AdapterPin\(\s*"([^"]+)"', adapters))
    return sorted(names)


def github_deps() -> list[str]:
    repos = set(EXTRA_GITHUB)
    swift = ROOT / "mac" / "Package.swift"
    if swift.exists():
        repos |= set(re.findall(r'url:\s*"https://github\.com/([^/"]+/[^/".]+)', swift.read_text()))
    for wf in (ROOT / ".github" / "workflows").glob("*.y*ml"):
        for owner_repo in re.findall(r"uses:\s*([\w.-]+/[\w.-]+)(?:/[^@\s]*)?@", wf.read_text()):
            repos.add(owner_repo)
    return sorted(repos)


def check_pypi(name: str) -> Health:
    h = Health("pypi", name)
    data = _get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    h.latest = data["info"]["version"]
    files = data["releases"].get(h.latest) or []
    if files:
        h.released = max(_when(f["upload_time_iso_8601"]) for f in files)
    flags = []
    if any("7 - Inactive" in c for c in data["info"].get("classifiers") or []):
        flags.append("inactive classifier")
    if data["info"].get("yanked"):
        flags.append("latest yanked")
    h.flags = tuple(flags)
    urls = data["info"].get("project_urls") or {}
    h.repo = _github_repo(*urls.values(), data["info"].get("home_page"))
    return h


def check_npm(name: str) -> Health:
    h = Health("npm", name)
    data = _get_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}")
    h.latest = data["dist-tags"]["latest"]
    h.released = _when(data["time"][h.latest])
    if data["versions"].get(h.latest, {}).get("deprecated"):
        h.flags = ("deprecated",)
    repo = data.get("repository")
    h.repo = _github_repo(repo.get("url") if isinstance(repo, dict) else repo, data.get("homepage"))
    return h


def _gh(path: str) -> dict | list:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"gh api {path} failed")
    return json.loads(out.stdout)


def check_github(repo: str) -> Health:
    h = Health("github", repo, repo=repo)
    meta = _gh(f"repos/{repo}")
    flags = ["archived"] if meta.get("archived") else []
    releases = _gh(f"repos/{repo}/releases?per_page=1")
    if releases:
        h.latest = releases[0]["tag_name"]
        h.released = _when(releases[0]["published_at"])
    else:
        tags = _gh(f"repos/{repo}/tags?per_page=1")
        h.latest = tags[0]["name"] if tags else "no release"
        h.released = _when(meta["pushed_at"])
        flags.append("no releases; date is last push")
    h.flags = tuple(flags)
    return h


def recent_work(repo: str) -> int:
    """Substantive commits on the default branch in the last ``ACTIVE_DAYS``."""
    since = datetime.fromtimestamp(datetime.now(UTC).timestamp() - ACTIVE_DAYS * 86400, UTC)
    commits = _gh(f"repos/{repo}/commits?per_page=100&since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    count = 0
    for c in commits:
        login = ((c.get("author") or {}).get("login") or "").lower()
        subject = c["commit"]["message"].splitlines()[0]
        if login.endswith("[bot]") or _HOUSEKEEPING.search(subject):
            continue
        count += 1
    return count


def judge(h: Health) -> Health:
    stale = h.age_days is not None and h.age_days > STALE_DAYS
    if not (stale or h.flags):
        return h
    if not h.repo:
        h.verdict = "check by hand (no GitHub repo found)"
        return h
    try:
        work = recent_work(h.repo)
    except Exception as exc:
        h.verdict = f"activity lookup failed: {exc}"
        return h
    if work:
        h.verdict = f"slow: {work} substantive commits in {ACTIVE_DAYS} days"
    else:
        h.verdict = f"DORMANT: no substantive commits in {ACTIVE_DAYS} days"
    return h


def _safe(fn, name: str, source: str) -> Health:
    try:
        return fn(name)
    except Exception as exc:  # report the lookup failure, keep checking the rest
        return Health(source, name, flags=(f"lookup failed: {exc}",))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--all", action="store_true", help="list every project, not only flagged ones")
    args = parser.parse_args(argv)

    jobs = [(check_pypi, n, "pypi") for n in python_deps()]
    jobs += [(check_npm, n, "npm") for n in npm_deps()]
    jobs += [(check_github, n, "github") for n in github_deps()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: judge(_safe(*j)), jobs))

    dormant = slow = 0
    for h in sorted(results, key=lambda r: -(r.age_days or 0)):
        stale = h.age_days is not None and h.age_days > STALE_DAYS
        notes = list(h.flags) + ([f"no release in {h.age_days} days"] if stale else [])
        if h.verdict:
            notes.append(h.verdict)
        mark = "ok"
        if h.verdict.startswith("slow"):
            mark, slow = "slow", slow + 1
        elif notes:
            mark, dormant = "FLAG", dormant + 1
        if mark != "ok" or args.all:
            day = h.released.date().isoformat() if h.released else "?"
            print(f"{mark:4}  {h.source:6}  {h.name:42} {h.latest:16} {day}  {'; '.join(notes)}")
    print(
        f"\n{len(results)} upstreams checked: {dormant} FLAG (decide in docs/UPKEEP.md), "
        f"{slow} slow (old release, repo still active)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
