"""Tests for ciao/backup_scope.py: what an unattended backup may commit.

The whole point of the module is that the answer is a *scope*, so the tests
pin the boundary from both sides: the durable trees are in, and the shapes
that must never leave the machine — credentials, runtime state, the provider
mirrors, caches, the derived transcript archive — are out. A scope that only
ever grows into an allowlist-with-exceptions is the failure this file exists to
catch, so the exclusion cases are written as an explicit table rather than
sprinkled through the tests.

Three of those rows are the ones #734 asked about, because each was a refusal
that read as an accident: a folder the operator named `Logs`, the automation
store, and the durable roots that sit bare at the top of the data root. They
are answered deliberately rather than accidentally, so the `#734` section below
states each answer next to the reason it is the answer.

Everything runs on temporary directories: no real user vault, no network, and
no real repository is ever written to.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ciao import backup_scope
from ciao.config import CiaoConfig, reset_reroot_cache
from ciao.workspace_reroot import mark_born_per_root

# Durable user data, by the reason it is durable.
ELIGIBLE = (
    "memory-vault/Notes/day-1.md",
    "memory-vault/Workspace/Memory-Proposals.md",
    "memory-vault/INDEX.md",
    "AGENTS.md",
    "skills/recipe/SKILL.md",
    "subagents/researcher.md",
    "commands/daily-review.md",
    ".archived-workspaces/old-20260101-000000/memory-vault/Notes/day-9.md",
    ".archived-workspaces/old-20260101-000000/AGENTS.md",
)

# Not durable data, by what each one is.
INELIGIBLE = {
    ".env": "operator credentials",
    ".envrc": "operator credentials",
    "secrets/gws-personal/token.json": "operator credentials",
    ".runtime/state.json": "runtime state",
    ".runtime/app_settings.json": "runtime state",
    ".claude/agents/recipe.md": "a provider mirror sync-skills regenerates",
    ".agents/recipe.md": "a provider mirror sync-skills regenerates",
    ".opencode/recipe.md": "a provider mirror sync-skills regenerates",
    "opencode.json": "a provider mirror sync-skills regenerates",
    "memory-vault/Logs/Chats/2026-09-28/session.md": "the transcript archive",
    "Logs/Chats/2026-09-28/session.md": "a bare top-level directory, not a durable tree",
    "node_modules/left-pad/index.js": "a dependency tree",
    "web/node_modules/vite/dist/node.js": "a dependency tree",
    ".venv/lib/python3.13/site-packages/x.py": "a virtualenv",
    "__pycache__/local_session.cpython-313.pyc": "a build cache",
    "memory-vault/Projects/node_modules/x/y.js": "a dependency tree",
    "ciao/local_session.py": "application source",
    "docs/ARCHITECTURE.md": "application source",
    "Projects/client-repo/notes.md": "an unrelated project checkout",
    "pyproject.toml": "build configuration",
    "memory-vault": "a bare directory, not a file",
    "../outside.md": "a path that escapes the data root",
    "/etc/passwd": "a path outside the data root",
    "": "the data root itself",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(repo),
        },
    ).stdout.strip()


def _config(workspace: Path, vault: Path | None = None) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="t",
        workspace_root=workspace,
        state_path=workspace / ".runtime" / "state.json",
        media_root=workspace / ".runtime" / "media",
        vault_root=vault if vault is not None else workspace / "memory-vault",
    )


def _install(tmp_path: Path, *, vault: Path | None = None) -> tuple[Path, CiaoConfig]:
    """A workspace on the default (shared-vault) layout, as a git repository."""
    workspace = tmp_path / "install"
    (workspace / "memory-vault" / "Notes").mkdir(parents=True)
    (workspace / ".runtime").mkdir()
    _git(workspace, "init", "-q", "-b", "main")
    return workspace, _config(workspace, vault)


def _rerooted_install(tmp_path: Path) -> tuple[Path, CiaoConfig]:
    """An install that has been re-rooted per workspace, as a git repository.

    The shape matters to what follows, so it is spelled out rather than faked:
    the agent assets live one directory per workspace, the transcript archive
    has been promoted to ``<install>/Logs`` and is no longer inside the vault,
    and the vault is still a scope base in its own right at the data root. That
    is the install in which a folder the operator named ``Logs`` inside their
    own vault is a notes folder rather than the archive.
    """
    workspace = tmp_path / "install"
    (workspace / "memory-vault" / "Notes").mkdir(parents=True)
    (workspace / "personal" / "memory-vault").mkdir(parents=True)
    (workspace / ".runtime").mkdir()
    _git(workspace, "init", "-q", "-b", "main")
    mark_born_per_root(workspace, workspace / ".runtime", ["personal"])
    reset_reroot_cache()
    return workspace, _config(workspace)


# ── the allowlist ────────────────────────────────────────────────────────────


def test_the_scope_is_the_durable_data_and_nothing_else(tmp_path: Path) -> None:
    _workspace, config = _install(tmp_path)

    for rel in ELIGIBLE:
        assert backup_scope.is_eligible(rel, config) is True, rel
    for rel, reason in INELIGIBLE.items():
        assert backup_scope.is_eligible(rel, config) is False, f"{rel}: {reason}"


def test_eligible_relpaths_names_the_allowlist(tmp_path: Path) -> None:
    """The prefixes are the documented scope, so a reviewer can read the rule
    instead of inferring it from a table of cases."""
    _workspace, config = _install(tmp_path)

    assert backup_scope.eligible_relpaths(config) == (
        ".archived-workspaces/*/memory-vault/",
        ".archived-workspaces/*/skills/",
        ".archived-workspaces/*/subagents/",
        ".archived-workspaces/*/commands/",
        ".archived-workspaces/*/AGENTS.md",
        "memory-vault/",
        "skills/",
        "subagents/",
        "commands/",
        "AGENTS.md",
        # The one file carved out of a refused directory (#734). It is in the
        # allowlist rather than an exception to it, so the status and the setup
        # prompt can render the scope without a second list of names.
        ".runtime/schedules.json",
    )


def test_ineligible_names_every_refusal(tmp_path: Path) -> None:
    """Setup and the backup status report from this tuple, so every shape the
    scope refuses has to appear in it."""
    _workspace, config = _install(tmp_path)

    refused = backup_scope.ineligible(config)
    for name in (
        ".env", ".envrc", "secrets/", ".claude/", ".agents/",
        ".opencode/", "opencode.json", "node_modules/",
        "memory-vault/Logs/", ".venv/", "__pycache__/", ".git/",
    ):
        assert name in refused, name
    # `.runtime` is refused for everything but the one file the scope carves
    # out of it, so it is named as a glob: `.runtime/` beside a scope that
    # commits `.runtime/schedules.json` would be a claim the scope does not
    # make, and this tuple is what the setup prompt prints.
    assert ".runtime/" not in refused
    assert ".runtime/*" in refused
    # The transcript archive is refused where this install keeps it, which here
    # is inside the vault. The bare name is not refused at any depth any more
    # (#734): a name match cannot tell the archive from a notes folder the
    # operator called `Logs`.
    assert "Logs/" not in refused


def test_an_unknown_top_level_directory_is_not_eligible(tmp_path: Path) -> None:
    """The allowlist, not a denylist: a directory nobody declared is refused
    even though nothing about it is objectionable, so a new tree the app
    starts writing cannot be swept into a backup commit by default."""
    _workspace, config = _install(tmp_path)

    assert backup_scope.is_eligible("future-feature/data.json", config) is False


def test_classify_splits_a_mixed_set(tmp_path: Path) -> None:
    """The order the caller passed is preserved, and an absolute path inside
    the data root comes back in the relpath form commit_scoped wants."""
    workspace, config = _install(tmp_path)

    eligible, excluded = backup_scope.classify(
        [
            "memory-vault/Notes/day-1.md",
            ".env",
            "ciao/main.py",
            workspace / "AGENTS.md",
            "memory-vault/Notes/day-1.md",  # a duplicate is not reported twice
        ],
        config,
    )

    assert eligible == ["memory-vault/Notes/day-1.md", "AGENTS.md"]
    assert excluded == [".env", "ciao/main.py"]


def test_an_absolute_path_outside_the_data_root_is_excluded(tmp_path: Path) -> None:
    """The status walk yields absolute paths, and one of them can sit in a
    linked worktree. Naming it is not consent to commit it."""
    _workspace, config = _install(tmp_path)
    elsewhere = tmp_path / "somewhere-else" / "notes.md"

    assert backup_scope.is_eligible(elsewhere, config) is False
    assert backup_scope.classify([elsewhere], config) == ([], [str(elsewhere)])


def test_a_symlink_out_of_the_data_root_is_excluded(tmp_path: Path) -> None:
    """Resolution decides, not the spelling: a link called ``memory-vault``
    that points at another repository's tree is not this install's data."""
    workspace, config = _install(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "memory-vault" / "linked").symlink_to(outside, target_is_directory=True)

    assert backup_scope.is_eligible("memory-vault/linked/notes.md", config) is False
    assert backup_scope.is_eligible("memory-vault/Notes/day-1.md", config) is True


# ── #734: the three refusals that read as over-refusals ──────────────────────
#
# One row per decision, with the reason beside it, because each of the three is
# a rule a reader has to be able to check against the code rather than trust:
# the file carved out of `.runtime`, the archive pinned to its location, and the
# bare top-level roots that stay out on purpose.


def test_the_734_scope_table(tmp_path: Path) -> None:
    """Every path #734 is about, on the layout each answer is about.

    The re-rooted install is the one that separates the two things the archive
    rule used to conflate: the archive has been promoted to `<install>/Logs`, so
    a folder the operator named `Logs` inside their own vault is theirs.
    """
    _workspace, config = _rerooted_install(tmp_path)
    table = (
        # A notes folder the operator happens to have called `Logs`. The vault's
        # own layout is the app's canonical structure and its folder names are
        # theirs; a single markdown file is not a derived archive.
        ("memory-vault/Logs/x.md", True, "the operator's own notes folder"),
        ("memory-vault/Logs/Chats/note.md", True, "the operator's own notes folder"),
        # The archive, where this install resolves it: the promoted
        # `<install>/Logs`, refused by location rather than by the name alone.
        ("Logs/Chats/x.md", False, "the derived transcript archive"),
        ("Logs/Chats/2026-09-28/session.md", False, "the derived transcript archive"),
        ("Logs/anything-at-all.md", False, "the derived transcript archive"),
        # The one file `.runtime` does not refuse: the automation catalog the
        # product is built to restore from git.
        (".runtime/schedules.json", True, "the durable automation store"),
        # ...and the rest of the directory still is.
        (".runtime/custom_providers.json", False, "operator credentials"),
        (".runtime/node_state.json", False, "runtime state"),
        (".runtime/schedules.json.tmp", False, "not the file, a half-written one"),
        ("personal/.runtime/schedules.json", False, "the one path the carve-out names"),
        # Bare top-level durable roots: emitted only under a scope base, and no
        # base covers the top of the data root on this layout. Answered
        # deliberately in #734 — the answer for an install keeping its catalog
        # at the data root is a workspace subfolder, not a wider base, because
        # a top-level `skills/` is application source in a checkout.
        ("commands/x.md", False, "a bare top-level root, not a durable tree"),
        ("skills/recipe/SKILL.md", False, "a bare top-level root, not a durable tree"),
        ("memory-vault/Notes/x.md", True, "the vault is a scope base of its own"),
        ("personal/commands/x.md", True, "an agent root, so the tree under it is durable"),
    )

    for rel, expected, reason in table:
        assert backup_scope.is_eligible(rel, config) is expected, f"{rel}: {reason}"


def test_task_records_are_durable(tmp_path: Path) -> None:
    """A task record is excluded from recall and kept by the backup (#1002).

    The two halves of one decision, and they are easy to confuse. Task records
    are reserved bookkeeping — out of the search index and the Memory Map —
    but the board is user-owned work, and a backup scope that followed the
    indexing exclusion would quietly stop preserving every task in the vault.
    `memory-vault` is a durable scope base, so nothing about the path changes
    the answer; the pin is here because the answer is load-bearing and silent.
    Both layouts are asked, because a scope base is the agent root's own vault
    and which of the two an install uses is not this test's decision to make.
    """
    _workspace, config = _rerooted_install(tmp_path)

    assert (
        backup_scope.is_eligible(
            "memory-vault/Workspace/Tasks/9f2c4a1b7e3d4f6a8b5c2d1e0f3a4b6c.md",
            config,
        )
        is True
    )
    # The per-workspace layout is the same answer under a different base.
    assert (
        backup_scope.is_eligible(
            "personal/memory-vault/Workspace/Tasks/9f2c4a1b7e3d4f6a8b5c2d1e0f3a4b6c.md",
            config,
        )
        is True
    )


def test_the_archive_denial_wins_where_the_vault_is_the_archive(tmp_path: Path) -> None:
    """The guard on the archive rule, and the reason it is anchored rather than
    matched by name.

    A vault configured at the archive's own path is still a scope base, and
    provenance would admit every file in it — including a multi-gigabyte derived
    archive the rule exists to protect. The archive refusal is evaluated first,
    so it wins there too, rather than being one install short of covering what
    it is for.
    """
    workspace = tmp_path / "install"
    (workspace / "Logs" / "Chats").mkdir(parents=True)
    (workspace / ".runtime").mkdir()
    (workspace / "personal" / "memory-vault").mkdir(parents=True)
    _git(workspace, "init", "-q", "-b", "main")
    mark_born_per_root(workspace, workspace / ".runtime", ["personal"])
    reset_reroot_cache()
    # The promoted archive is the configured vault: every note-shaped path
    # inside it is derived output, whatever the vault rule would admit.
    config = _config(workspace, vault=workspace / "Logs")

    assert backup_scope.data_root(config) == workspace.resolve()
    assert backup_scope.is_eligible("Logs/Chats/x.md", config) is False
    assert backup_scope.is_eligible("Logs/Notes/2026-09-28.md", config) is False
    # The rest of the scope is untouched: the refusal is one directory, not a
    # verdict on the install.
    assert backup_scope.is_eligible("personal/memory-vault/Notes/a.md", config) is True
    assert "Logs/" in backup_scope.ineligible(config)


def test_an_archived_workspaces_transcripts_stay_refused(tmp_path: Path) -> None:
    """The one place the archive anchor does not reach, pinned on both sides.

    `config.logs_root` is the *one* archive this install writes, and the
    re-rooting promotes it to the install root, so an archived agent root
    holding a `Logs` tree is a copy of a workspace as it was — derived output
    the resolved location cannot point at. `<logs_root>/Chats` is where
    transcripts actually live, so that subtree stays refused under an archived
    root even though the archive itself is refused by location rather than by
    name (#734).

    The refusal stops at the transcript subtree on purpose. A markdown note
    sitting beside it in the same `Logs` folder is the same shape as
    `memory-vault/Logs/x.md` on a live root, and the old name rule — which
    refused every `Logs` at every depth — is what a user's own notes folder of
    that name kept running into.
    """
    _workspace, config = _install(tmp_path)

    # Derived transcripts under an archived root: refused.
    assert backup_scope.is_eligible(
        ".archived-workspaces/old/memory-vault/Logs/Chats/2026-09-28/s.md", config
    ) is False
    assert backup_scope.is_eligible(
        ".archived-workspaces/old/memory-vault/Logs/Chats/s.md", config
    ) is False
    # A note beside them, and the rest of the archived root, are not.
    assert backup_scope.is_eligible(
        ".archived-workspaces/old/memory-vault/Logs/notes.md", config
    ) is True
    assert backup_scope.is_eligible(
        ".archived-workspaces/old/memory-vault/Notes/day-9.md", config
    ) is True
    # The archive this install does write is still refused, whole.
    assert backup_scope.is_eligible("memory-vault/Logs/Chats/s.md", config) is False


def test_a_live_root_is_not_an_archived_one(tmp_path: Path) -> None:
    """Where the archived-root refusal stops, on the layout the shape exists on.

    The re-rooted install is the one with both spellings side by side: an
    archived agent root copied from before the migration, and a live root whose
    vault is the operator's own. The transcript subtree of the live root is
    treated like the rest of its vault — by provenance, not by name — which is
    the answer #734 settled for a live root and the only reason the refusal
    above is scoped to `.archived-workspaces/`.
    """
    _workspace, config = _rerooted_install(tmp_path)

    assert backup_scope.is_eligible(
        "personal/memory-vault/Logs/Chats/2026-09-28/s.md", config
    ) is True
    assert backup_scope.is_eligible(
        ".archived-workspaces/old/memory-vault/Logs/Chats/2026-09-28/s.md", config
    ) is False


# ── layouts ──────────────────────────────────────────────────────────────────


def test_a_vault_in_its_own_repository_is_that_repository(tmp_path: Path) -> None:
    """The data root follows the vault, exactly as the existing push path does,
    so an externally located vault is not reported as uncovered and its notes
    are not mistaken for the install's."""
    workspace = tmp_path / "install"
    (workspace / ".runtime").mkdir(parents=True)
    brain = tmp_path / "brain"
    (brain / "Notes").mkdir(parents=True)
    _git(brain, "init", "-q", "-b", "main")
    config = _config(workspace, vault=brain)

    assert backup_scope.data_root(config) == brain.resolve()

    # The vault's own layout is the app's canonical structure, so its contents
    # are durable by provenance rather than by a fixed set of folder names.
    assert backup_scope.is_eligible("Notes/day-1.md", config) is True
    assert backup_scope.is_eligible("Logs/Chats/s.md", config) is False
    assert backup_scope.is_eligible("node_modules/x/y.js", config) is False
    assert backup_scope.is_eligible("secrets/token.json", config) is False


def test_an_operator_named_vault_is_still_scoped(tmp_path: Path) -> None:
    """`CIAO_VAULT_ROOT` may name the notes folder anything, so a vault the
    operator called `brain` must not end up outside the scope."""
    workspace = tmp_path / "install"
    (workspace / ".runtime").mkdir(parents=True)
    (workspace / "brain" / "People").mkdir(parents=True)
    config = _config(workspace, vault=workspace / "brain")

    assert backup_scope.is_eligible("brain/People/ada.md", config) is True
    assert backup_scope.is_eligible("brain/Logs/Chats/s.md", config) is False
    # The install root is still an agent root, so its own unrelated trees stay
    # out even though the vault sitting beside them is in.
    assert backup_scope.is_eligible("Projects/client/notes.md", config) is False
    assert backup_scope.is_eligible("AGENTS.md", config) is True


def test_a_per_workspace_install_scopes_each_agent_root(tmp_path: Path) -> None:
    """After the re-rooting the agent assets live one directory per workspace,
    and the install root is no longer one of them."""
    workspace = tmp_path / "install"
    (workspace / ".runtime").mkdir(parents=True)
    (workspace / "personal" / "memory-vault").mkdir(parents=True)
    mark_born_per_root(workspace, workspace / ".runtime", ["personal"])
    reset_reroot_cache()
    config = _config(workspace)

    assert backup_scope.is_eligible("personal/memory-vault/Notes/a.md", config) is True
    assert backup_scope.is_eligible("personal/AGENTS.md", config) is True
    assert backup_scope.is_eligible("personal/.claude/agents/a.md", config) is False
    # The install root holds the promoted archive and the operator's
    # credentials, neither of which is per-workspace durable data.
    assert backup_scope.is_eligible("Logs/Chats/s.md", config) is False
    assert backup_scope.is_eligible("AGENTS.md", config) is False


# ── already-tracked files the scope refuses ──────────────────────────────────


def test_tracked_excluded_reports_a_committed_env(tmp_path: Path) -> None:
    """.gitignore only governs what git starts tracking, so a credential that
    was already committed has to be reported rather than skipped — the manual
    sync path still stages the whole tree and would carry it off the machine.
    """
    workspace, config = _install(tmp_path)
    (workspace / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (workspace / "memory-vault" / "Notes" / "day-1.md").write_text("hi\n", encoding="utf-8")
    _git(workspace, "add", "-f", "-A")
    _git(workspace, "commit", "-q", "-m", "oops")

    assert backup_scope.tracked_excluded(config) == [".env"]


def test_tracked_excluded_is_empty_for_a_clean_data_repo(tmp_path: Path) -> None:
    workspace, config = _install(tmp_path)
    (workspace / "memory-vault" / "Notes" / "day-1.md").write_text("hi\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("# guide\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")

    assert backup_scope.tracked_excluded(config) == []


def test_tracked_excluded_reports_an_application_checkout(tmp_path: Path) -> None:
    """A repository that is also a developer checkout is not data-only, and
    saying so is the answer — the fix is a repository for the data, not a
    wider scope."""
    workspace, config = _install(tmp_path)
    (workspace / "ciao").mkdir()
    (workspace / "ciao" / "main.py").write_text("# app\n", encoding="utf-8")
    (workspace / "memory-vault" / "Notes" / "day-1.md").write_text("hi\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")

    assert backup_scope.tracked_excluded(config) == ["ciao/main.py"]


def test_tracked_excluded_is_empty_outside_a_repository(tmp_path: Path) -> None:
    """A data folder that is not a repository has nothing tracked; the report
    is empty rather than an error, because "not configured for backup" is a
    state the caller already knows how to handle."""
    workspace = tmp_path / "plain"
    (workspace / "memory-vault").mkdir(parents=True)

    assert backup_scope.tracked_excluded(_config(workspace)) == []
