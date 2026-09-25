"""Safety and bookkeeping helpers for the insights sandbox harness (#594).

The harness in ``run.py`` answers #594: does a *full* Ciaobot chat session --
same system prompt, same skills, same ``ciao`` CLI -- do a better end-of-
conversation memory pass than today's one-shot extraction, measured on the
same archived chats?

Answering that means letting a real agent really write, which is only safe
because of where it runs: three disposable APFS clones of the live workspace,
each prepared here so that nothing in it can reach outside. The two ways a
clone could escape are (a) a path that is not a clone at all, refused by
:func:`assert_sandbox_path`, and (b) a clone that is still wired to the live
install -- its git remote, its push subscriptions, its schedules, its startup
triage, its integrations -- neutralised by :func:`prepare_clone`.

Everything here is pure filesystem and ``git`` plumbing: no network, no model,
no server. That is what keeps ``tests/test_insights_sandbox.py`` fast and
offline, and it is why the safety argument can be tested instead of trusted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ciao.schedules import system_schedule_id


class SandboxError(RuntimeError):
    """The harness refused to touch a path, or a clone could not be prepared."""


# A .mcp.json under one of these is not a workspace integration: one is git
# metadata, one is a vendored tree, and one is the transcript archive, where a
# chat's own text may simply be named .mcp.json.
_MCP_SKIP_DIRS = frozenset({".git", "node_modules", "Logs"})

# The harness clones keep only the keys that make a clone *be* a clone. Every
# other key in .env is a credential for something outside the sandbox -- a
# Notion token, a mail account, a push key -- and a full agent has Bash.
_ENV_KEEP = re.compile(r"^(CIAO_|PWA_)")

# The canonical doc both arms are pointed at, and the queue a fact goes to
# when the agent is unsure. Both are named in the plan; kept here so the
# classifier and the diff counter cannot drift apart.
_PROPOSALS_SUFFIX = "Workspace/Memory-Proposals.md"
_GUIDE_NAMES = ("AGENTS.md", "CLAUDE.md", "MEMORY.md")


# ── path safety ──────────────────────────────────────────────────────────


def assert_sandbox_path(path: Path, *, sandbox_root: Path, live: Path) -> Path:
    """Return the resolved ``path``, or refuse it.

    Three refusals, and all three matter:

    - ``path`` is not strictly inside ``sandbox_root`` -- so a path outside
      the sandbox tree is never written, and ``sandbox_root`` itself is not a
      clone (it holds the run directory and the report).
    - ``path`` is ``live`` or inside it -- the live workspace is the one
      place a mistake is unrecoverable, since a full agent with Bash may have
      pushed, messaged or emailed by the time anything noticed.
    - ``path`` contains ``live`` -- the mirror image: a clone whose root
      swallowed the live workspace would let a write escape upward.

    Resolution happens before every comparison, so a symlink or a ``..`` in
    the argument cannot smuggle a different target past the check.
    """
    resolved = Path(path).expanduser().resolve()
    root = Path(sandbox_root).expanduser().resolve()
    live_resolved = Path(live).expanduser().resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise SandboxError(
            f"{resolved} is not strictly inside the sandbox root {root}"
        )
    if resolved == live_resolved or resolved.is_relative_to(live_resolved):
        raise SandboxError(
            f"{resolved} is the live workspace ({live_resolved}) or inside it"
        )
    if live_resolved.is_relative_to(resolved):
        raise SandboxError(
            f"{resolved} contains the live workspace ({live_resolved})"
        )
    return resolved


def clone_workspace(live: Path, dest: Path) -> None:
    """Copy the live workspace to ``dest`` with APFS clonefile (``cp -c -R``).

    Instant and space-free, and -- unlike ``rsync`` or a manual copy -- it
    copies the ``.git`` directory too, which is what makes the per-chat
    ``git commit`` attribution in the report possible at all.

    An existing ``dest`` is refused rather than merged into: a leftover
    directory from an earlier run would silently be the baseline, and the
    whole measurement is only worth anything if the baseline is known.
    """
    live_path = Path(live).expanduser()
    dest_path = Path(dest).expanduser()
    if dest_path.exists():
        raise SandboxError(
            f"{dest_path} already exists; refusing to reuse a directory this run did not create"
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-c", "-R", str(live_path), str(dest_path)], check=True)


# ── clone preparation ────────────────────────────────────────────────────


def _git(clone: Path, *args: str) -> str:
    """Run one ``git -C clone`` command and return its stripped stdout."""
    result = subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _read_json(path: Path) -> object:
    """Read JSON, or ``{}`` when the file is missing or unreadable.

    A missing file is the normal case for most of these stores on a fresh
    clone, and a clone must still be prepared rather than aborted.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _workspace_names(runtime: Path) -> list[str]:
    """Workspace names from ``.runtime/workspaces.json`` (a list of dicts)."""
    data = _read_json(runtime / "workspaces.json")
    if not isinstance(data, list):
        return []
    return [
        str(row["name"])
        for row in data
        if isinstance(row, dict) and str(row.get("name") or "")
    ]


def _stock_schedule_ids(stock_schedules: Path) -> list[str]:
    """``schedule_id`` of every packaged system schedule."""
    data = _read_json(Path(stock_schedules))
    rows = data.get("schedules") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        str(row["schedule_id"])
        for row in rows
        if isinstance(row, dict) and str(row.get("schedule_id") or "")
    ]


def prepare_clone(
    clone: Path, *, stock_schedules: Path, disable_insights: bool
) -> list[str]:
    """Neutralise everything in ``clone`` that reaches outside it.

    Returns human-readable notes for ``prep.log``; the caller is expected to
    write them down *before* starting a server, so a pilot run that went
    somewhere unexpected can be read back against the list of things that were
    supposed to be stopped.

    The risks are the ones in ``ciao/main.py``'s startup at ``d68beb59``: a
    push every 30 s, phone notifications, a nightly memory-curation chat, a
    startup triage chat, an MCP server, and -- for the agent clone only --
    the harness's own chats triggering the archive pipeline underneath it.
    """
    clone = Path(clone)
    runtime = clone / ".runtime"
    notes: list[str] = []

    # 1. The git remote. `_branch_backup_loop` pushes every 30 s, so a clone
    #    left with one would publish the agent's work to the live repository.
    remotes = _git(clone, "remote").split()
    for name in remotes:
        _git(clone, "remote", "remove", name)
    leftover = _git(clone, "remote").split()
    if leftover:
        raise SandboxError(f"{clone} still has git remotes after removal: {leftover}")
    notes.append(
        f"git: removed {len(remotes)} remote(s) [{', '.join(remotes) or 'none'}]"
    )

    # 2. Push subscriptions: no phone notifications from sandbox activity.
    subs = runtime / "push_subscriptions.json"
    if subs.exists():
        subs.unlink()
    notes.append("push: removed .runtime/push_subscriptions.json")

    # 3. User schedules: a tick would dispatch a real chat on the clone.
    user_path = runtime / "schedules.json"
    user_data = _read_json(user_path)
    user_rows = user_data.get("schedules") if isinstance(user_data, dict) else None
    if isinstance(user_rows, list):
        for row in user_rows:
            if isinstance(row, dict):
                row["enabled"] = False
        _write_json(user_path, user_data)
    notes.append(f"schedules: disabled {len(user_rows or [])} user schedule(s)")

    # 4. System schedules, both the base ids and their per-workspace fan-out:
    #    memory curation and skill evolution are the machinery under test, and
    #    running them on the clone would write facts the report would count.
    sys_path = runtime / "system_schedules_state.json"
    sys_data = _read_json(sys_path)
    if not isinstance(sys_data, dict):
        sys_data = {}
    sys_rows = sys_data.get("schedules")
    if not isinstance(sys_rows, dict):
        sys_rows = {}
    workspaces = _workspace_names(runtime)
    wanted: set[str] = set(sys_rows)
    for base_id in _stock_schedule_ids(stock_schedules):
        wanted.add(base_id)
        for workspace in workspaces:
            wanted.add(system_schedule_id(base_id, workspace))
    for schedule_id in sorted(wanted):
        entry = sys_rows.get(schedule_id)
        if not isinstance(entry, dict):
            entry = {}
            sys_rows[schedule_id] = entry
        entry["enabled"] = False
    sys_data["schedules"] = sys_rows
    _write_json(sys_path, sys_data)
    notes.append(
        f"schedules: disabled {len(wanted)} system schedule entries "
        f"(stock ids + {len(workspaces)} workspace fan-out(s))"
    )

    # 5. Startup triage: without a fresh stamp, a server boot that finds
    #    errors in the clone's own logs starts a triage chat.
    _write_json(
        runtime / "startup_triage.json",
        {"last_dispatched_at": datetime.now(UTC).isoformat()},
    )
    notes.append("triage: stamped .runtime/startup_triage.json")

    # 6. Background tasks: anything queued by the live install is woken on
    #    boot and runs against the clone.
    background = runtime / "background"
    if background.exists():
        shutil.rmtree(background)
    background.mkdir(parents=True, exist_ok=True)
    notes.append("background: emptied .runtime/background/")

    # 7. The agent clone's chats must not also trigger the archive pipeline.
    if disable_insights:
        settings_path = runtime / "app_settings.json"
        settings = _read_json(settings_path)
        if not isinstance(settings, dict):
            settings = {}
        settings["insights_enabled"] = False
        settings["trajectories_enabled"] = False
        _write_json(settings_path, settings)
        notes.append("settings: insights_enabled=false, trajectories_enabled=false")

    # 8. MCP servers: a full agent may start one, and each one is a tool the
    #    harness cannot account for in a diff.
    removed = 0
    for mcp in clone.rglob(".mcp.json"):
        if any(part in _MCP_SKIP_DIRS for part in mcp.relative_to(clone).parts[:-1]):
            continue
        mcp.unlink()
        removed += 1
    notes.append(f"integrations: removed {removed} .mcp.json file(s)")

    # 9. .env credentials: only the keys that address this clone survive.
    env_path = clone / ".env"
    if env_path.exists():
        kept: list[str] = []
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                kept.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if _ENV_KEEP.match(key):
                kept.append(line)
        env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        notes.append(f"integrations: .env reduced to {len(kept)} line(s)")

    # 10. The per-workspace integration switches, which is how an install
    #     enables a named MCP server or a Google Workspace profile.
    ws_path = runtime / "workspaces.json"
    ws_data = _read_json(ws_path)
    if isinstance(ws_data, list):
        for row in ws_data:
            if not isinstance(row, dict):
                continue
            row["allowed_mcp_servers"] = []
            row["claude_ai_mcps"] = False
            row["gws_profile"] = ""
        _write_json(ws_path, ws_data)
    notes.append("integrations: cleared per-workspace MCP servers and gws profile")

    return notes


# ── baseline ─────────────────────────────────────────────────────────────


def strip_archives(clone: Path, rel_archives: list[str]) -> int:
    """Remove each archive's appended insights section; return how many changed.

    Both arms read the same 50 chats, and the previous extraction's answer is
    already in the archive the #586 run left behind. Leaving it in would let
    either arm copy the other one's baseline, so the section is stripped from
    all three clones before the baseline commit.
    """
    from ciao.insights_compare import strip_insights

    changed = 0
    for rel in rel_archives:
        path = Path(clone) / rel
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            continue
        body = strip_insights(original)
        if body != original:
            path.write_text(body, encoding="utf-8")
            changed += 1
    return changed


def commit_snapshot(clone: Path, message: str) -> str:
    """Commit everything in ``clone`` and return the new sha.

    One commit per chat per arm is what makes the report possible: the diff
    between two consecutive shas is exactly what that one chat changed. The
    identity is fixed rather than inherited because a clone carries the live
    repo's config and the operator's name may not be set in this environment.
    """
    clone = Path(clone)
    _git(clone, "add", "-A")
    _git(
        clone,
        "-c",
        "user.name=sandbox",
        "-c",
        "user.email=sandbox@localhost",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        message,
    )
    return _git(clone, "rev-parse", "HEAD")


# ── diff classification ──────────────────────────────────────────────────


def classify(path: str) -> str:
    """Which part of the workspace a changed file belongs to.

    Order is the order the classes are read in: the proposals queue and the
    guide are the two files an agent is *expected* to write, so they are named
    before the vault that also contains them. ``logs`` is last of the real
    classes because a transcript archive is evidence, not output.
    """
    if path.endswith(_PROPOSALS_SUFFIX):
        return "proposals"
    if path.rsplit("/", 1)[-1] in _GUIDE_NAMES:
        return "guide"
    under_logs = "/Logs/" in path or path.startswith("Logs/")
    if ("/memory-vault/" in path or path.startswith("memory-vault/")) and not under_logs:
        return "vault"
    if under_logs:
        return "logs"
    return "other"


def _git_lines(clone: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def diff_summary(clone: Path, a: str, b: str) -> dict:
    """What changed between two shas, grouped by :func:`classify`.

    ``Logs/`` is ignored: every arm writes transcripts (the server does, by
    design) and the archives are inputs, so counting them would make every
    chat look equally busy and hide the thing under test. ``queued`` counts
    the bullets appended to the review queue, because that is the one file
    where "how much" matters as much as "what".
    """
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    for line in _git_lines(clone, "diff", "--name-status", a, b):
        fields = line.split("\t")
        status = fields[0][:1]
        target = fields[-1]
        if status == "A":
            added.append(target)
        elif status == "D":
            deleted.append(target)
        else:
            modified.append(target)
    by_class: dict[str, int] = {}
    for path in (*added, *modified, *deleted):
        group = classify(path)
        if group == "logs":
            continue
        by_class[group] = by_class.get(group, 0) + 1
    queued = sum(
        1
        for line in _git_lines(clone, "diff", a, b, "--", f"**/{_PROPOSALS_SUFFIX}")
        if line.startswith("+- ")
    )
    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "by_class": by_class,
        "queued": queued,
    }
