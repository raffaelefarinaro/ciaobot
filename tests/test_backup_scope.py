"""Tests for ciao/backup_scope.py: what an unattended backup may commit.

The whole point of the module is that the answer is a *scope*, so the tests
pin the boundary from both sides: the durable trees are in, and the shapes
that must never leave the machine — credentials, runtime state, the provider
mirrors, caches, the derived transcript archive — are out. A scope that only
ever grows into an allowlist-with-exceptions is the failure this file exists to
catch, so the exclusion cases are written as an explicit table rather than
sprinkled through the tests.

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
    "memory-vault/Logs/Chats/2026-09-28/session.md": "the derived transcript archive",
    "Logs/Chats/2026-09-28/session.md": "the derived transcript archive",
    ".archived-workspaces/old/memory-vault/Logs/Chats/s.md": "the transcript archive",
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
    )


def test_ineligible_names_every_refusal(tmp_path: Path) -> None:
    """Setup and the backup status report from this tuple, so every shape the
    scope refuses has to appear in it."""
    _workspace, config = _install(tmp_path)

    refused = backup_scope.ineligible(config)
    for name in (
        ".env", ".envrc", "secrets/", ".runtime/", ".claude/", ".agents/",
        ".opencode/", "opencode.json", "node_modules/", "Logs/",
        "memory-vault/Logs/", ".venv/", "__pycache__/", ".git/",
    ):
        assert name in refused, name


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
