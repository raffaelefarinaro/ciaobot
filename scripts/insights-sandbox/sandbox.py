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

Everything here is filesystem, ``git`` plumbing and a loopback socket: no
network off this machine, no model, no server. That is what keeps
``tests/test_insights_sandbox.py`` fast and offline, and it is why the safety
argument can be tested instead of trusted. :func:`pick_port` is the one place
that touches a socket, and only ever on ``127.0.0.1``, to find or clear a port
for the harness's own server.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Iterable
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


def _is_path_bearing(value: str) -> bool:
    """A ``.env`` value that names a filesystem location rather than a setting.

    Keeping ``CIAO_*``/``PWA_*`` is not enough on its own: a retained key can
    still carry a path that points at the live install. ``CIAO_VAULT_ROOT`` is
    the shape that bites -- a real workspace sets it, the prep keeps the key,
    and the spawned ``ciao run`` loads the clone's ``.env`` and writes the
    original vault. ``start_server`` refuses such a run from the effective
    configuration it probes; this is the earlier of the two defences, so a
    path-bearing value is not carried into the clone at all.
    """
    text = value.strip().strip("\"'")
    return os.path.isabs(text) or text.startswith("~") or ".." in text


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


def assert_contained(paths: Iterable[Path], clone: Path) -> None:
    """Refuse the first path in ``paths`` that is not inside ``clone``.

    :func:`assert_sandbox_path` proves a *clone* lives in the sandbox tree.
    This proves the other direction, and it is the check the clone cannot make
    for itself: an install may configure a vault, a workspace root or a project
    doc *outside* its workspace, and those settings are preserved on purpose
    -- ``CiaoConfig.workspace_vault_root`` returns an absolute ``vault_root``
    unchanged. ``prepare_clone`` cannot rebase them, because they live in
    ``.runtime/workspaces.json`` and in a project's ``vault_doc_path``, and
    both are legitimate absolute paths.

    Cloned as-is, every writer that resolves those settings -- the one-shot
    arm, the server, the agent with Bash -- writes the original files, and
    nothing the harness can undo afterwards. So it fails closed instead: build
    the config the arm is about to use, ask for every path it will resolve,
    and refuse the run when one of them lands outside the clone.

    The clone counts as inside itself, because ``vault_root: "."`` (a
    workspace that is its own vault) is a supported layout and resolves to the
    clone root. Every comparison is on resolved paths, so a symlink or a
    ``..`` in the value cannot smuggle a different target past the check.
    """
    root = Path(clone).expanduser().resolve()
    for path in paths:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_relative_to(root):
            raise SandboxError(
                f"{resolved} is outside the clone {root}; refusing to run, "
                "because the arm would write the original files"
            )


# ── git shape ────────────────────────────────────────────────────────────


def git_dirs(clone: Path) -> tuple[Path, Path]:
    """``(git dir, common git dir)`` of the repository at ``clone``, absolute.

    Two directories because a repository has two in a linked worktree: the
    worktree's own ``.git/worktrees/<name>`` and the *common* directory the
    main repository owns. Both matter here, since either one outside the copy
    means the copy is not the repository ``git -C`` will touch.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(clone),
                "rev-parse",
                "--absolute-git-dir",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SandboxError(
            f"{clone} is not a git repository ({exc.stderr.strip() or exc}); "
            "the harness cannot attribute a per-chat diff without git"
        ) from exc
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        raise SandboxError(
            f"could not read the git directories of {clone}: {result.stdout.strip()!r}"
        )
    git_dir = Path(lines[0])
    common = Path(lines[1])
    if not common.is_absolute():
        # git prints the common dir relative to the repository it was asked
        # about, which for an ordinary clone is simply `.git`.
        common = Path(clone) / common
    return git_dir.resolve(), common.resolve()


def assert_self_contained_repo(clone: Path, *, subject: str) -> None:
    """Refuse a repository whose git metadata is not inside ``clone``.

    The shape this exists for is a **linked** worktree, whose ``.git`` is a
    text file pointing at the main repository's absolute common git directory.
    Copied or not, ``git -C`` on it operates on the *source* repository: the
    prep's ``git remote remove`` would strip the source's remote, and every
    per-chat snapshot commit would be written into the source's ``.git``. There
    is no way to rebase that, so it is refused before the first git mutation
    rather than repaired afterwards.
    """
    root = Path(clone).expanduser().resolve()
    for label, path in zip(("git dir", "common git dir"), git_dirs(root)):
        if not path.is_relative_to(root):
            raise SandboxError(
                f"{subject} is a linked git worktree: its {label} ({path}) lies "
                f"outside the copy at {root}; clone its main repository instead"
            )


def clone_workspace(live: Path, dest: Path) -> None:
    """Copy the live workspace to ``dest`` with APFS clonefile (``cp -c -R``).

    Instant and space-free, and -- unlike ``rsync`` or a manual copy -- it
    copies the ``.git`` directory too, which is what makes the per-chat
    ``git commit`` attribution in the report possible at all.

    An existing ``dest`` is refused rather than merged into: a leftover
    directory from an earlier run would silently be the baseline, and the
    whole measurement is only worth anything if the baseline is known.

    A ``--live`` that is a **linked** git worktree is refused, and the copy is
    deleted rather than left behind. ``cp`` copies a linked worktree's ``.git``
    *file*, which points at the source repository's absolute common git
    directory, so the copy is not a repository of its own: every later ``git
    -C <clone>`` -- ``remote remove`` in the prep, ``add -A`` and ``commit``
    per chat -- would operate on the source's metadata, and the first of those
    removes the **source's** remote, which is the one thing the prep exists to
    prevent. The check therefore runs before the first git mutation, and the
    message says what to clone instead.
    """
    live_path = Path(live).expanduser()
    dest_path = Path(dest).expanduser()
    if dest_path.exists():
        raise SandboxError(
            f"{dest_path} already exists; refusing to reuse a directory this run did not create"
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-c", "-R", str(live_path), str(dest_path)], check=True)
    try:
        assert_self_contained_repo(dest_path, subject="live workspace")
    except SandboxError:
        # The copy is a hazard, not a clone: leaving it would make a re-run
        # refuse on "already exists" and hide why the first one stopped.
        shutil.rmtree(dest_path, ignore_errors=True)
        raise


# ── port selection ───────────────────────────────────────────────────────

# A loopback probe that is refused should be refused immediately; a port that
# is held by something that never answers would otherwise stall boot on a
# multi-second TCP timeout.
_PROBE_TIMEOUT = 1.0


def pick_port(requested: int) -> int:
    """The loopback port the harness's own server may bind, or a refusal.

    ``0`` means "any free port", which is the default: bind ``("127.0.0.1", 0)``,
    read back the port the kernel chose, close it. Picking it this way means
    the harness can never collide with a Ciaobot server somebody else is
    already running, which is not hypothetical -- a second instance on ``*:8543``
    answers ``/api/auth`` with a 401 and the pilot dies before the first chat.

    An explicit port is *checked*, not trusted, and checked two ways because
    they fail for different reasons:

    - a connection attempt finds a process already listening there. Ciaobot
      binds ``*:port``, so a second instance cannot be bound on top of it and
      the harness's own server would sit behind the stranger's, answering
      nothing while the stranger answers everything.
    - a trial ``bind(("0.0.0.0", port))`` finds a port something else already
      holds, or one this machine will not let a fresh server have -- which the
      connection attempt alone would report as merely "not up yet".

    Neither check makes the port safe to share for the life of the run, so the
    caller still has to confirm the server it reached is its own.
    """
    if requested < 0:
        raise SandboxError(f"invalid port {requested}")
    if requested == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(_PROBE_TIMEOUT)
        if probe.connect_ex(("127.0.0.1", requested)) == 0:
            raise SandboxError(
                f"127.0.0.1:{requested} already accepts connections; another "
                "server may own it. Pass --port 0 to let the harness pick a free port."
            )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", requested))
        except OSError as exc:
            raise SandboxError(
                f"127.0.0.1:{requested} is not available for the harness server: {exc}. "
                "Pass --port 0 to let the harness pick a free port."
            ) from exc
    return requested


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


def neutralise_remotes(repo: Path) -> list[str]:
    """Remove every git remote from ``repo``; return the names it had.

    More than the install root needs this: ``_branch_backup_loop`` pushes
    ``local_session.sync_root(config)``, which is the repository containing the
    *vault* -- the same directory as the install root for the default layout,
    but a separate repository of its own when the vault lives elsewhere, which
    is a supported install. A clone of that shape carries two remotes and only
    the one at the top would be removed, so the second is handled the same way
    before anything runs.

    Asserts the result rather than trusting ``git remote remove``: a remote that
    survives means the 30-second push loop is live, and the harness must not
    find that out from a log line.
    """
    remotes = _git(repo, "remote").split()
    for name in remotes:
        _git(repo, "remote", "remove", name)
    leftover = _git(repo, "remote").split()
    if leftover:
        raise SandboxError(f"{repo} still has git remotes after removal: {leftover}")
    return remotes


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
    remotes = neutralise_remotes(clone)
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

    # 9. .env credentials: only the keys that address this clone survive, and
    #    a retained key may not carry a path -- see `_is_path_bearing`.
    env_path = clone / ".env"
    if env_path.exists():
        kept: list[str] = []
        dropped: list[str] = []
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                kept.append(line)
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if not _ENV_KEEP.match(key):
                continue
            if _is_path_bearing(value):
                dropped.append(key)
                continue
            kept.append(line)
        env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        notes.append(f"integrations: .env reduced to {len(kept)} line(s)")
        if dropped:
            # Named in `prep.log`, because a dropped key is a changed install
            # and the next question is always which one and why.
            notes.append(
                "integrations: .env dropped path-bearing key(s) "
                f"[{', '.join(sorted(dropped))}], which would address a path "
                "outside this clone"
            )

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
        if classify(target) == "logs":
            # Dropped here, not only from `by_class`: every a/m/d number the
            # report prints is the length of one of these three lists, so a log
            # path left in them would be counted in the totals and in the
            # per-chat cell while the report claims logs are excluded.
            continue
        if status == "A":
            added.append(target)
        elif status == "D":
            deleted.append(target)
        else:
            modified.append(target)
    by_class: dict[str, int] = {}
    for path in (*added, *modified, *deleted):
        group = classify(path)
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


# ── token accounting ─────────────────────────────────────────────────────

# The four counters a Claude transcript carries. Cache reads and cache writes
# are separate keys because they are billed differently from fresh input, and
# for a long agent chat they dwarf `input_tokens` -- a report that only counted
# fresh input would say a 200k-token pass was cheap.
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def sum_claude_usage(jsonl: Path) -> dict[str, int]:
    """Token usage for one Claude session transcript, counted once per message.

    A ``<session_id>.jsonl`` writes the same assistant record more than once:
    a real transcript observed during development carries each ``message.id``
    three or four times, as the stream is persisted, copied into a sidechain
    and re-read, and every copy carries the same ``usage`` block. Summing
    records would multiply the chat's real cost by the number of copies, so
    ``message.id`` is the dedupe key and a message is counted the first time
    its id is seen.

    A record with no usable id is still counted, under a key nothing else can
    produce, so an odd transcript loses no tokens instead of losing the first
    record and counting the rest twice. A missing or unreadable file is zero
    rather than an exception: the caller records a count it could not get, and
    the report shows ``-`` for it.

    Local parsing, no network and no model, so it stays testable offline.
    """
    totals: dict[str, int] = dict.fromkeys(_USAGE_FIELDS, 0)
    seen: set[str] = set()
    try:
        with Path(jsonl).open(encoding="utf-8", errors="replace") as handle:
            for index, raw in enumerate(handle):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                key = str(message.get("id") or "") or f"#{index}"
                if key in seen:
                    continue
                seen.add(key)
                for counter in _USAGE_FIELDS:
                    value = usage.get(counter)
                    if isinstance(value, int) and not isinstance(value, bool):
                        totals[counter] += value
    except OSError:
        return totals
    return totals
