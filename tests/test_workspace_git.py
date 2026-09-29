from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ciao.cli import ensure_vault_git, ensure_workspace_git, setup_workspace


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )


def _ignored(root: Path, path: str) -> bool:
    """Whether git refuses to track ``path`` here.

    Asked of git rather than read off the file: what these rules are *for* is
    which lines git honours and in what order, and that is a question only git
    answers correctly.
    """
    return _git(root, "check-ignore", "-q", path).returncode == 0


def test_ensure_workspace_git_initializes_fresh_dir(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "CLAUDE.md").write_text("hi\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

    ensure_workspace_git(root)

    assert _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    assert _git(root, "branch", "--show-current").stdout.strip() == "main"

    log = _git(root, "log", "--oneline")
    assert log.returncode == 0
    assert "Initialize Ciaobot workspace" in log.stdout
    assert len(log.stdout.strip().splitlines()) == 1

    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    for entry in (
        ".env",
        "**/.runtime/*",
        "!/.runtime/schedules.json",
        ".claude/",
        ".agents/",
        "*.log",
    ):
        assert entry in gitignore.splitlines()

    tracked = _git(root, "ls-files").stdout.splitlines()
    # Written by hand above, at the root this test controls: `ensure_workspace_git`
    # commits what is there and does not scaffold a layout of its own.
    assert "CLAUDE.md" in tracked
    assert ".gitignore" in tracked
    assert ".env" not in tracked


def test_the_workspace_gitignore_re_includes_the_automation_store(
    tmp_path: Path,
) -> None:
    """The template has to be in git's grammar, not just list the entry.

    The backup scope commits `.runtime/schedules.json` and refuses every other
    path under that root, so the ignore rules have to say the same thing — and
    a `.runtime/` line cannot: git never descends into an ignored directory, so
    a re-include written beside it is dead and the automations stay outside the
    backup while the status page lists them inside it. Asserted against git
    rather than against the text, because the text is the thing that was wrong
    twice (#734).
    """
    root = tmp_path / "ws"
    root.mkdir()
    runtime = root / ".runtime"
    runtime.mkdir()
    (runtime / "schedules.json").write_text('{"schedules": []}\n', encoding="utf-8")
    (runtime / "custom_providers.json").write_text("{}\n", encoding="utf-8")
    # Nested runtime directories, which the old unanchored `.runtime/` covered
    # and a root-anchored `.runtime/*` would not. These hold credentials.
    for relative in ("client/.runtime", "a/b/.runtime", "sub/.runtime"):
        (root / relative).mkdir(parents=True)
    (root / "client" / ".runtime" / "bootstrap-auth-token").write_text(
        "token\n", encoding="utf-8"
    )
    (root / "a" / "b" / ".runtime" / "x").write_text("state\n", encoding="utf-8")
    (root / "sub" / ".runtime" / "schedules.json").write_text("{}\n", encoding="utf-8")

    ensure_workspace_git(root)

    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    # The re-include has to follow the glob it overrides; the reverse order is
    # ignored like any other line.
    assert lines.index("**/.runtime/*") < lines.index("!/.runtime/schedules.json")
    assert ".runtime/" not in lines

    assert not _ignored(root, ".runtime/schedules.json"), "the store must be tracked"
    assert _ignored(root, ".runtime/custom_providers.json"), "the rest must not be"
    assert _ignored(root, "client/.runtime/bootstrap-auth-token")
    assert _ignored(root, "a/b/.runtime/x")
    # A nested store is not the carve-out: the negation is pinned to the root,
    # so this stays ignored with everything else in that directory.
    assert _ignored(root, "sub/.runtime/schedules.json")

    # ...and it is therefore actually committable, which is the whole point.
    assert _git(root, "add", "-A").returncode == 0
    tracked = _git(root, "ls-files").stdout.splitlines()
    assert ".runtime/schedules.json" in tracked
    assert ".runtime/custom_providers.json" not in tracked


def test_the_template_still_ignores_nested_runtime_directories(tmp_path: Path) -> None:
    """A separate assertion for the regression the previous fix introduced.

    ``.runtime/*`` contains a slash, so git anchors it to the repository root
    and it stops matching ``client/.runtime`` — silently, with every test that
    only looked at the root still green. The scoped backup refuses those paths
    either way, but the manual sync path still stages the whole tree, so a
    nested `bootstrap-auth-token` would leave the machine. Asked of git, since
    that is the only thing that got the spelling wrong (#734).
    """
    root = tmp_path / "ws"
    root.mkdir()
    for relative in ("client/.runtime", "a/b/.runtime", "sub/.runtime"):
        (root / relative).mkdir(parents=True)
        (root / relative / "x").write_text("state\n", encoding="utf-8")

    ensure_workspace_git(root)

    for path in (
        "client/.runtime/x",
        "a/b/.runtime/x",
        "sub/.runtime/x",
        ".runtime/x",
    ):
        assert _ignored(root, path), path
    # And nothing nested shows up in what a blanket add would stage.
    staged = _git(root, "add", "-A", "-n").stdout
    assert ".runtime/" not in staged


def test_a_bare_runtime_directory_rule_is_repaired_in_place(tmp_path: Path) -> None:
    """An install scaffolded before #734 has `.runtime/` in its `.gitignore`,
    and appending the pair beside it would not help: the directory rule wins
    because git never looks inside. Only a rewrite makes the carve-out real, so
    that is what happens — narrowly, in place, and without touching anything
    else the operator wrote.
    """
    root = tmp_path / "ws"
    root.mkdir()
    assert _git(root.parent, "init", "-b", "trunk", str(root)).returncode == 0
    (root / ".gitignore").write_text(
        "# mine\n.runtime/\nnode_modules/\n", encoding="utf-8"
    )
    runtime = root / ".runtime"
    runtime.mkdir()
    (runtime / "schedules.json").write_text("{}", encoding="utf-8")

    ensure_workspace_git(root)

    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".runtime/" not in lines
    assert lines.count("**/.runtime/*") == 1
    assert lines.count("!/.runtime/schedules.json") == 1
    assert lines.index("**/.runtime/*") < lines.index("!/.runtime/schedules.json")
    # The operator's own lines are untouched, in place and in order.
    assert lines[:2] == ["# mine", "**/.runtime/*"]
    assert "node_modules/" in lines
    # The repair is what makes the carve-out real, not just the spelling.
    assert not _ignored(root, ".runtime/schedules.json")
    # The repair is idempotent, so a second setup rewrites nothing.
    before = (root / ".gitignore").read_text(encoding="utf-8")
    ensure_workspace_git(root)
    assert (root / ".gitignore").read_text(encoding="utf-8") == before


def test_every_hand_written_spelling_of_the_runtime_rule_is_repaired(
    tmp_path: Path,
) -> None:
    """`.runtime`, `/.runtime` and `/.runtime/` are the spellings a person
    writes, and each of them ignores the directory well enough that a re-include
    appended beside it is dead — the carve-out would then silently fail on that
    install while the status page listed the file as backed up.

    Checked against git rather than against the text, because a rule that looks
    right in the file and does nothing is the whole failure mode here.
    """
    for index, spelling in enumerate((".runtime", "/.runtime", "/.runtime/")):
        root = tmp_path / f"ws-{index}"
        root.mkdir()
        assert _git(root.parent, "init", "-b", "trunk", str(root)).returncode == 0
        (root / ".gitignore").write_text(f"{spelling}\n", encoding="utf-8")
        (root / ".runtime").mkdir()
        (root / ".runtime" / "schedules.json").write_text("{}", encoding="utf-8")

        ensure_workspace_git(root)

        assert not _ignored(root, ".runtime/schedules.json"), spelling
        assert _ignored(root, ".runtime/state.json"), spelling
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert lines.count("**/.runtime/*") == 1, spelling
        assert spelling not in lines, spelling


def test_a_duplicate_runtime_rule_yields_one_pair(tmp_path: Path) -> None:
    """A file carrying the rule twice is repaired once, not twice.

    The second line is dropped rather than rewritten: the pair already covers
    everything it covered, so keeping it would only leave a rule in the file
    that contradicts the pair's ordering.
    """
    root = tmp_path / "ws"
    root.mkdir()
    assert _git(root.parent, "init", "-b", "trunk", str(root)).returncode == 0
    (root / ".gitignore").write_text(".runtime/\n.runtime/\n", encoding="utf-8")

    ensure_workspace_git(root)

    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count("**/.runtime/*") == 1
    assert lines.count("!/.runtime/schedules.json") == 1
    assert ".runtime/" not in lines


def test_a_windows_line_ending_survives_the_repair(tmp_path: Path) -> None:
    """The repair rewrites the file it appends to, so it has to leave the line
    ending it found.

    A CRLF `.gitignore` is a Windows editor's file, and the read would otherwise
    have translated it to LF before the check could see it — so the file comes
    back with a whole-file diff the operator never asked for, on a file whose
    only intended change is two lines.
    """
    root = tmp_path / "ws"
    root.mkdir()
    assert _git(root.parent, "init", "-b", "trunk", str(root)).returncode == 0
    (root / ".gitignore").write_bytes(b"# mine\r\n.runtime/\r\n")

    ensure_workspace_git(root)

    written = (root / ".gitignore").read_bytes()
    assert b"\r\n" in written
    assert b"\n" not in written.replace(b"\r\n", b"")
    assert written.startswith(b"# mine\r\n**/.runtime/*\r\n")


def test_a_nested_runtime_rule_is_not_the_workspace_rule(tmp_path: Path) -> None:
    """Only Ciaobot's own root-level line is rewritten.

    `client/.runtime/` is somebody else's directory, in somebody else's
    project: replacing it with a re-include would put that project's runtime
    state back into a snapshot, which is the opposite of what this repair is
    for. The template's own `**/` glob is what ignores it, not a rewrite.
    """
    root = tmp_path / "ws"
    root.mkdir()
    assert _git(root.parent, "init", "-b", "trunk", str(root)).returncode == 0
    (root / ".gitignore").write_text("client/.runtime/\n", encoding="utf-8")
    (root / "client" / ".runtime").mkdir(parents=True)
    (root / "client" / ".runtime" / "x").write_text("state\n", encoding="utf-8")

    ensure_workspace_git(root)

    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "client/.runtime/"
    assert _ignored(root, "client/.runtime/x")


def test_ensure_workspace_git_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    ensure_workspace_git(root)
    ensure_workspace_git(root)

    log = _git(root, "log", "--oneline")
    assert len(log.stdout.strip().splitlines()) == 1


def test_ensure_workspace_git_leaves_existing_repo_alone(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    assert _git(root.parent, "init", "-b", "trunk", str(root)).returncode == 0
    (root / ".gitignore").write_text("# mine\nnode_modules/\n.env\n", encoding="utf-8")

    ensure_workspace_git(root)

    # No commit was created and the branch is untouched.
    assert _git(root, "rev-parse", "HEAD").returncode != 0
    assert _git(root, "branch", "--show-current").stdout.strip() == "trunk"

    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    lines = gitignore.splitlines()
    assert lines[:3] == ["# mine", "node_modules/", ".env"]
    assert lines.count(".env") == 1
    for entry in ("**/.runtime/*", "!/.runtime/schedules.json", ".claude/", "*.log"):
        assert entry in lines


def test_ensure_workspace_git_skips_without_git_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("ciao.cli.shutil.which", lambda name: None)

    def fail_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("git must not be invoked when the binary is missing")

    monkeypatch.setattr("ciao.cli.subprocess.run", fail_run)

    ensure_workspace_git(root)

    assert not (root / ".git").exists()
    assert "skipping workspace git init" in capsys.readouterr().err


def test_ensure_vault_git_initializes_fresh_vault(tmp_path: Path) -> None:
    vault = tmp_path / "brain"
    vault.mkdir()
    (vault / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

    ensure_vault_git(vault)

    assert _git(vault, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    assert _git(vault, "branch", "--show-current").stdout.strip() == "main"

    log = _git(vault, "log", "--oneline")
    assert "Initialize Ciaobot vault" in log.stdout
    assert len(log.stdout.strip().splitlines()) == 1

    gitignore = (vault / ".gitignore").read_text(encoding="utf-8")
    for entry in (".DS_Store", ".obsidian/workspace*"):
        assert entry in gitignore.splitlines()

    tracked = _git(vault, "ls-files").stdout.splitlines()
    assert "MEMORY.md" in tracked
    assert ".gitignore" in tracked


def test_ensure_vault_git_appends_gitignore_to_existing_vault_repo(tmp_path: Path) -> None:
    vault = tmp_path / "notes"
    vault.mkdir()
    assert _git(vault.parent, "init", "-b", "trunk", str(vault)).returncode == 0
    (vault / ".gitignore").write_text("# mine\n.DS_Store\n", encoding="utf-8")

    ensure_vault_git(vault)

    # No commit was created and the branch is untouched.
    assert _git(vault, "rev-parse", "HEAD").returncode != 0
    assert _git(vault, "branch", "--show-current").stdout.strip() == "trunk"

    lines = (vault / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# mine", ".DS_Store"]
    assert lines.count(".DS_Store") == 1
    assert ".obsidian/workspace*" in lines


def test_ensure_vault_git_leaves_nested_vault_untouched(tmp_path: Path) -> None:
    """A vault inside the workspace repo (the default layout) is never
    double-initialized and gets no nested .gitignore."""
    ws = tmp_path / "ws"
    vault = ws / "memory-vault"
    vault.mkdir(parents=True)
    (vault / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    ensure_workspace_git(ws)

    ensure_vault_git(vault)

    assert not (vault / ".git").exists()
    assert not (vault / ".gitignore").exists()
    assert _git(ws, "status", "--porcelain").stdout.strip() == ""


def test_ensure_vault_git_skips_without_git_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    vault = tmp_path / "brain"
    vault.mkdir()
    monkeypatch.setattr("ciao.cli.shutil.which", lambda name: None)

    def fail_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("git must not be invoked when the binary is missing")

    monkeypatch.setattr("ciao.cli.subprocess.run", fail_run)

    ensure_vault_git(vault)

    assert not (vault / ".git").exists()
    assert "skipping vault git init" in capsys.readouterr().err


def test_setup_workspace_creates_git_repo_without_committing_env(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    setup_workspace(
        ws,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert (ws / ".env").is_file()
    assert _git(ws, "branch", "--show-current").stdout.strip() == "main"
    log = _git(ws, "log", "--oneline")
    assert "Initialize Ciaobot workspace" in log.stdout

    tracked = _git(ws, "ls-files").stdout.splitlines()
    assert ".env" not in tracked
    # The runtime root is ignored except for the automation store, so a fresh
    # install's own initial commit carries that one file and nothing else under
    # `.runtime/` (#734). The assertion used to be a flat "no `.runtime/` path":
    # true while the whole directory was ignored, and the thing that made the
    # backup scope's carve-out inert on exactly the installs this scaffolds.
    runtime_tracked = [p for p in tracked if p.startswith(".runtime/")]
    assert all(p == ".runtime/schedules.json" for p in runtime_tracked), runtime_tracked
    assert not any(path.startswith(".claude/") for path in tracked)
    assert not any(path.startswith(".agents/") for path in tracked)
    assert "personal/AGENTS.md" in tracked
    assert "personal/AGENTS.md" in tracked
    assert _git(ws, "status", "--porcelain").stdout.strip() == ""

    # Default layout: the vault lives inside the workspace repo and is
    # tracked there — no second repo is created.
    assert not (ws / "memory-vault" / ".git").exists()
    assert "personal/memory-vault/MEMORY.md" in tracked


def test_setup_workspace_rejects_a_traversal_workspace_name_before_writes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match="workspace name"):
        setup_workspace(
            workspace,
            workspace_name="../outside",
            launch_agents_dir=tmp_path / "LaunchAgents",
            app_dir=tmp_path / "Applications",
        )

    assert not workspace.exists()
    assert not (tmp_path / "outside").exists()


def test_setup_workspace_git_inits_external_vault(tmp_path: Path) -> None:
    """A vault outside the workspace gets its own repo with the vault
    .gitignore, and .env records the absolute vault path."""
    ws = tmp_path / "workspace"
    vault = tmp_path / "brain"
    setup_workspace(
        ws,
        vault_root=vault,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert _git(vault, "branch", "--show-current").stdout.strip() == "main"
    log = _git(vault, "log", "--oneline")
    assert "Initialize Ciaobot vault" in log.stdout

    gitignore = (vault / ".gitignore").read_text(encoding="utf-8")
    for entry in (".DS_Store", ".obsidian/workspace*"):
        assert entry in gitignore.splitlines()

    tracked = _git(vault, "ls-files").stdout.splitlines()
    assert "MEMORY.md" in tracked

    env_text = (ws / ".env").read_text(encoding="utf-8")
    assert f"CIAO_VAULT_ROOT={vault}" in env_text
    # The workspace repo neither tracks nor contains the external vault.
    ws_tracked = _git(ws, "ls-files").stdout.splitlines()
    assert not any(path.startswith("memory-vault/") for path in ws_tracked)


def test_setup_workspace_rerun_honors_existing_env_vault_root(
    tmp_path: Path,
) -> None:
    """Re-running setup with a wrong/blank vault_root must not relocate the
    vault: the existing .env's CIAO_VAULT_ROOT wins, so scaffolding is not
    re-scattered at the argument's location."""
    ws = tmp_path / "workspace"
    setup_workspace(
        ws,
        vault_root="brain-a",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )
    assert (ws / "brain-a" / "MEMORY.md").is_file()
    env_before = (ws / ".env").read_text(encoding="utf-8")

    # Re-run with a bogus vault_root; .env already exists so it is the source
    # of truth for where the vault lives.
    setup_workspace(
        ws,
        vault_root="wrong-b",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    # Scaffolding stays in the real vault and is not re-scattered under wrong-b.
    assert (ws / "brain-a" / "MEMORY.md").is_file()
    assert not (ws / "wrong-b").exists()
    # .env is left untouched and still points at the original vault.
    assert (ws / ".env").read_text(encoding="utf-8") == env_before
    assert "CIAO_VAULT_ROOT=brain-a" in env_before


def test_setup_workspace_rerun_without_arguments_uses_registered_external_root(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    setup_workspace(
        ws,
        vault_root="brain-a",
        workspace_name="research",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )
    registry = ws / ".runtime" / "workspaces.json"
    registry_before = registry.read_text(encoding="utf-8")

    setup_workspace(
        ws,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert registry.read_text(encoding="utf-8") == registry_before
    assert (ws / "brain-a" / "MEMORY.md").is_file()
    assert not (ws / "brain-a" / "personal").exists()
    assert not (ws / "brain-a" / "research").exists()


def test_setup_workspace_rerun_does_not_scaffold_the_vault_container(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    setup_workspace(
        ws,
        workspace_name="research",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )
    registry = ws / ".runtime" / "workspaces.json"
    registry_before = registry.read_text(encoding="utf-8")

    setup_workspace(
        ws,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert registry.read_text(encoding="utf-8") == registry_before
    # `<workspace>/memory-vault`, not `memory-vault/<workspace>`. The assertions
    # this replaces were about the shared CONTAINER — that setup must not scaffold
    # `memory-vault/` itself as though it were a vault. There is no container any
    # more, so its absence is what carries that intent.
    assert (ws / "research" / "memory-vault" / "MEMORY.md").is_file()
    assert not (ws / "memory-vault").exists()
    # And no root is invented for a workspace nobody registered.
    assert not (ws / "personal").exists()


def test_setup_workspace_rerun_supports_a_configured_vault_alias(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    external = tmp_path / "external-notes"
    external.mkdir()
    (ws / "memory-vault").symlink_to(external, target_is_directory=True)

    setup_workspace(
        ws,
        workspace_name="research",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )
    setup_workspace(
        ws,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert (external / "research" / "MEMORY.md").is_file()
    assert not (external / "personal").exists()
    assert not (external / "MEMORY.md").exists()


def test_setup_workspace_rerun_does_not_clobber_custom_agent_through_symlink(
    tmp_path: Path,
) -> None:
    """Setup mirrors a custom agent without overwriting its source on reruns."""
    ws = tmp_path / "workspace"
    setup_workspace(
        ws,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )
    # Per-root: the catalog and its generated mirror both live in the workspace.
    root = ws / "personal"
    name = "memory.md"
    assert not (root / ".claude" / "agents" / name).exists()

    custom = root / "subagents" / name
    custom.write_text("# custom override\n", encoding="utf-8")
    link = root / ".claude" / "agents" / name

    setup_workspace(
        ws,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert custom.read_text(encoding="utf-8") == "# custom override\n"
    assert link.is_symlink() and link.resolve() == custom.resolve()


def test_setup_workspace_existing_mode_adopts_folder_as_vault(
    tmp_path: Path,
) -> None:
    """Single-folder setup: with vault_mode=existing and no vault_root, the
    chosen folder itself is the vault, so the user's notes stay in place and
    the onboarding agent adapts them there."""
    ws = tmp_path / "notes"
    ws.mkdir()
    (ws / "ideas.md").write_text("# Ideas\n", encoding="utf-8")

    setup_workspace(
        ws,
        vault_mode="existing",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    # The vault is the folder root: scaffold lands next to the user's notes,
    # no memory-vault/ subfolder is created.
    assert "CIAO_VAULT_ROOT=." in (ws / ".env").read_text(encoding="utf-8")
    assert (ws / "MEMORY.md").is_file()
    assert (ws / "ideas.md").is_file()
    assert not (ws / "memory-vault").exists()


def test_setup_workspace_existing_mode_keeps_scaffolded_vault(
    tmp_path: Path,
) -> None:
    """A folder that already has a memory-vault/ (right shape) is adopted
    as-is: the vault stays at memory-vault, not the folder root."""
    ws = tmp_path / "prior-workspace"
    (ws / "memory-vault").mkdir(parents=True)

    setup_workspace(
        ws,
        vault_mode="existing",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert "CIAO_VAULT_ROOT=memory-vault" in (ws / ".env").read_text(encoding="utf-8")
    assert (ws / "memory-vault" / "MEMORY.md").is_file()
    # No adapt-in-place scaffold at the folder root.
    assert not (ws / "MEMORY.md").exists()
