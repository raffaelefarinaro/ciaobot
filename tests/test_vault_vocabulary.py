"""The closed `type:` vocabulary and the generated VOCABULARY.md.

`type:` was free text, so every synonym the agent invented became a first-class
category: `doc (1)` rendered next to `document (1)` in INDEX.md, and a vault
grew 21 types where 16 were meant. Types are now a closed set enforced by
`vault_lint`; tags stay open and are only stratified by use, because closing a
382-value vocabulary would destroy what tags are for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.vault_index import (
    CANONICAL_TYPES,
    DIR_TYPE_MAP,
    TYPE_ALIASES,
    canonical_type,
    format_vocabulary,
    main,
    scan_vault,
    vocabulary_report,
)
from ciao.vault_lint import _frontmatter_error, _VaultFile


def _note(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _lint(relative: str, content: str) -> dict[str, str] | None:
    return _frontmatter_error(
        _VaultFile(path=Path(relative), relative=Path(relative), content=content)
    )


# ---- the vocabulary itself --------------------------------------------------


def test_path_inference_can_never_produce_a_rejected_type() -> None:
    """`_infer_type` types the frontmatter-less files, so its whole range must
    be canonical — otherwise the linter would reject notes nobody mistyped."""
    assert set(DIR_TYPE_MAP.values()) <= CANONICAL_TYPES


def test_every_alias_target_is_canonical() -> None:
    """No alias chains: one rename always lands on a final value."""
    for source, target in TYPE_ALIASES.items():
        assert target in CANONICAL_TYPES, f"{source} -> {target}"
        assert target not in TYPE_ALIASES, f"{source} -> {target} is itself aliased"


def test_no_alias_shadows_a_canonical_type() -> None:
    assert not (set(TYPE_ALIASES) & CANONICAL_TYPES)


def test_canonical_type_maps_canonical_alias_and_unknown() -> None:
    assert canonical_type("project") == "project"
    assert canonical_type("  project  ") == "project"
    assert canonical_type("discussion-prep") == "note"
    assert canonical_type("frobnicate") == ""
    assert canonical_type("") == ""
    assert canonical_type(None) == ""  # type: ignore[arg-type]


# ---- enforcement -----------------------------------------------------------


def test_canonical_type_passes_the_linter() -> None:
    assert _lint("personal/People/Alba.md", "---\ntype: person\n---\n# Alba\n") is None


def test_aliased_type_is_reported_with_its_target() -> None:
    error = _lint("work/x.md", "---\ntype: discussion-prep\n---\n# X\n")
    assert error is not None
    assert error["kind"] == "unknown_type"
    # The target is named so the hygiene routine can apply it as a safe fix.
    assert "note" in error["message"]


def test_unknown_type_with_no_alias_is_reported_without_a_suggestion() -> None:
    error = _lint("work/y.md", "---\ntype: frobnicate\n---\n# Y\n")
    assert error is not None
    assert error["kind"] == "unknown_type"
    assert "use '" not in error["message"]


def test_missing_type_still_reports_missing_not_unknown() -> None:
    error = _lint("work/z.md", "---\ntitle: Z\n---\n# Z\n")
    assert error is not None
    assert error["kind"] == "missing_type"


def test_reserved_filenames_stay_exempt_from_the_vocabulary() -> None:
    """index.md / memory.md / log.md carry no frontmatter by design (and are
    OKF's reserved names); the new check must not start flagging them."""
    for name in ("index.md", "memory.md", "log.md", "INDEX.md"):
        assert _lint(f"personal/{name}", "# heading only\n") is None


# ---- the report ------------------------------------------------------------


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    _note(vault, "personal/People/Alba.md", "---\ntype: person\ntags: [family]\n---\n# Alba\n")
    _note(vault, "personal/notes/a.md", "---\ntype: discussion-prep\ntags: [family, once]\n---\n# A\n")
    _note(vault, "work/People/Aymen.md", "---\ntype: person\ntags: [customer]\n---\n# Aymen\n")
    _note(vault, "work/x.md", "---\ntype: frobnicate\n---\n# X\n")
    return vault


def test_report_separates_canonical_counts_from_drift(tmp_path: Path) -> None:
    report = vocabulary_report(scan_vault(_vault(tmp_path)))

    assert report["types"]["person"] == 2
    assert "discussion-prep" not in report["types"]

    assert report["type_drift"]["discussion-prep"]["suggested"] == "note"
    # Paths keep the vault-dir prefix `Entry.path` carries, so a reported path
    # is one the user can open directly from the repo root.
    assert report["type_drift"]["discussion-prep"]["paths"] == ["memory-vault/personal/notes/a.md"]
    # No canonical equivalent: reported, but with nothing to apply.
    assert report["type_drift"]["frobnicate"]["suggested"] == ""


def test_report_records_which_workspaces_use_a_tag(tmp_path: Path) -> None:
    report = vocabulary_report(scan_vault(_vault(tmp_path)))

    assert report["tags"]["family"] == 2
    assert report["tag_workspaces"]["family"] == ["personal"]
    assert report["tag_workspaces"]["customer"] == ["work"]


def test_vocabulary_stratifies_tags_and_never_rejects_one(tmp_path: Path) -> None:
    body = format_vocabulary(scan_vault(_vault(tmp_path)))

    assert "## Types (canonical" in body
    assert "## Types (drift" in body
    assert "`discussion-prep` → `note`" in body
    # A one-off tag is surfaced as a candidate, not an error.
    assert "Tags (candidates)" in body
    assert "`once`" in body


def test_vocabulary_is_byte_deterministic(tmp_path: Path) -> None:
    """It carries no timestamp on purpose: the memory agent reads it before
    writing frontmatter, and a timestamp would dirty git on every rebuild."""
    entries = scan_vault(_vault(tmp_path))
    assert format_vocabulary(entries) == format_vocabulary(entries)


def test_write_emits_both_index_and_vocabulary(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    assert main(["--write", "--vault-root", str(vault)]) == 0

    assert (vault / "INDEX.md").is_file()
    vocabulary = (vault / "VOCABULARY.md").read_text(encoding="utf-8")
    assert "do not edit by hand" in vocabulary
    assert "namespace/value" in vocabulary


# ---- generated files are not notes -----------------------------------------


def test_generated_files_are_never_indexed_or_linted(tmp_path: Path) -> None:
    """VOCABULARY.md is generated *about* the vault, so it must be excluded
    everywhere INDEX.md is. Missing one place made it an ordinary indexed note
    (a god-node in the Memory Map) plus a permanent `missing_frontmatter`
    finding, which would have made `os-audit` exit 1 forever.

    Casefolded because OKF spells the reserved names lowercase, so an imported
    bundle or an agent-written folder index produces `index.md`, not `INDEX.md`.
    """
    from ciao.vault_lint import run_validation

    vault = tmp_path / "memory-vault"
    _note(vault, "personal/a.md", "---\ntype: person\n---\n# A\n")
    _note(vault, "personal/index.md", "# Folder index\n")
    _note(vault, "personal/MEMORY.md", "# Curated\n")

    assert main(["--write", "--vault-root", str(vault)]) == 0

    indexed = {Path(entry.path).name for entry in scan_vault(vault)}
    assert indexed == {"a.md"}
    assert run_validation(vault)["frontmatter_errors"] == []


def test_log_md_is_still_content(tmp_path: Path) -> None:
    """`log.md` is a note (a project's chronological history), not a generated
    file — exempt from frontmatter, but it belongs in the index."""
    from ciao.vault_index import is_generated_vault_file

    assert not is_generated_vault_file("log.md")
    for name in ("INDEX.md", "index.md", "VOCABULARY.md", "vocabulary.md", "MEMORY.md"):
        assert is_generated_vault_file(name), name


# ---- root resolution -------------------------------------------------------


def test_a_relative_vault_root_resolves_against_the_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """The bundled engine's launcher `cd`s into `Ciaobot.app/.../ciao-runtime`
    before exec'ing Python, so resolving a relative `CIAO_VAULT_ROOT` against the
    cwd pointed inside the app bundle. Every vault command run from a routine —
    whose prompts deliberately pass no `--vault-root` — died with a
    FileNotFoundError under the runtime directory, while the same command worked
    by hand from the workspace. Mirrors the fix already made for `.runtime`.
    """
    from ciao.cli import _resolve_vault_root
    from ciao.vault_index import default_vault_root

    workspace = tmp_path / "workspace"
    (workspace / "memory-vault").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    monkeypatch.chdir(elsewhere)

    expected = (workspace / "memory-vault").resolve()
    assert default_vault_root() == expected
    assert _resolve_vault_root() == expected
    # A relative --vault-root gets the same treatment, for the same reason.
    assert _resolve_vault_root("memory-vault") == expected


def test_an_absolute_vault_root_is_still_honoured(tmp_path: Path, monkeypatch) -> None:
    from ciao.cli import _resolve_vault_root

    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path / "workspace"))
    target = tmp_path / "somewhere" / "vault"
    target.mkdir(parents=True)

    assert _resolve_vault_root(target) == target.resolve()


def _install_with_client_workspace(tmp_path: Path) -> Path:
    """An install whose registry names a `client` workspace, as the PWA writes it."""
    import json

    root = tmp_path / "install"
    runtime = root / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json.dumps([
            {"name": "personal", "vault_root": "memory-vault/personal"},
            {"name": "client", "vault_root": "memory-vault/client"},
        ]),
        encoding="utf-8",
    )
    return root


def test_default_vault_root_follows_the_active_workspace(tmp_path: Path, monkeypatch) -> None:
    """With no CIAO_VAULT_ROOT, the default vault is the active workspace's.

    Chats export CIAO_ACTIVE_WORKSPACE, and a routine run that wrote its index to
    `<workspace>/memory-vault` while the chat named another vault was the bug.
    """
    import os

    from ciao.cli import _resolve_vault_root
    from ciao.config import CiaoConfig, installed_workspace_env
    from ciao.vault_index import default_vault_root

    root = _install_with_client_workspace(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "client")
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.chdir(elsewhere)

    source = {**installed_workspace_env(dict(os.environ)), "PWA_AUTH_TOKEN": "test"}
    expected = CiaoConfig.from_env(source).agent_vault_root("client").resolve()
    assert default_vault_root() == expected
    assert _resolve_vault_root() == expected


def test_explicit_vault_root_env_still_wins_over_the_active_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    from ciao.vault_index import default_vault_root

    root = _install_with_client_workspace(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "client")
    explicit = tmp_path / "explicit-vault"
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(explicit))

    assert default_vault_root() == explicit.resolve()


def test_vault_index_write_refuses_when_env_and_active_workspace_differ(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao.vault_index import main as vault_index_main

    root = _install_with_client_workspace(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "client")
    shared = tmp_path / "shared-vault"
    shared.mkdir()
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(shared))

    assert vault_index_main(["--write"]) == 2
    assert not (shared / "INDEX.md").exists()
    err = capsys.readouterr().err
    assert str(shared.resolve()) in err
    assert "name different vaults" in err


def test_vault_index_write_with_an_explicit_vault_root_is_not_refused(
    tmp_path: Path, monkeypatch
) -> None:
    from ciao.vault_index import main as vault_index_main

    root = _install_with_client_workspace(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "client")
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(tmp_path / "shared-vault"))
    target = tmp_path / "named-vault"
    target.mkdir()

    assert vault_index_main(["--write", "--vault-root", str(target)]) == 0
    assert (target / "INDEX.md").is_file()


class _Reached(Exception):
    """Raised by a stub standing in for the write, to prove the guard let the call through."""


def _conflicting_install(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """An install whose active workspace vault differs from CIAO_VAULT_ROOT."""
    root = _install_with_client_workspace(tmp_path)
    shared = tmp_path / "shared-vault"
    shared.mkdir()
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "client")
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(shared))
    return root, shared


def _stub_write(monkeypatch, module: str, attr: str) -> list[tuple]:
    """Replace one library write with a stub that records the call and then stops."""
    import importlib

    calls: list[tuple] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        raise _Reached

    monkeypatch.setattr(importlib.import_module(module), attr, stub)
    return calls


# (command argv, library module, library callable the handler reaches once it proceeds)
_VAULT_WRITERS = [
    (["vault-migrate"], "ciao.vault_migration", "retain_retired_stock_types"),
    (["vault-migrate-links"], "ciao.vault_migrate_links", "migrate_links"),
    (["vault-unmigrate-links"], "ciao.vault_migrate_links", "unmigrate_links"),
    (["vault-rehome"], "ciao.vault_rehome", "rehome_people"),
    (["vault-unrehome"], "ciao.vault_rehome", "unrehome_people"),
]


def _vault_writer_cases():
    return [pytest.param(argv, mod, attr, id=argv[0]) for argv, mod, attr in _VAULT_WRITERS]


@pytest.mark.parametrize("argv,module,attr", _vault_writer_cases())
def test_implicit_vault_apply_is_refused_on_conflict(
    argv, module, attr, tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao.cli import main as cli_main

    _conflicting_install(tmp_path, monkeypatch)
    calls = _stub_write(monkeypatch, module, attr)

    assert cli_main([*argv, "--apply"]) == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "name different vaults" in err
    assert str((tmp_path / "shared-vault").resolve()) in err


@pytest.mark.parametrize("argv,module,attr", _vault_writer_cases())
def test_implicit_vault_apply_with_explicit_vault_root_proceeds(
    argv, module, attr, tmp_path: Path, monkeypatch
) -> None:
    from ciao.cli import main as cli_main

    _conflicting_install(tmp_path, monkeypatch)
    named = tmp_path / "named-vault"
    named.mkdir()
    _stub_write(monkeypatch, module, attr)

    with pytest.raises(_Reached):
        cli_main([*argv, "--apply", "--vault-root", str(named)])


@pytest.mark.parametrize("argv,module,attr", _vault_writer_cases())
def test_implicit_vault_dry_run_is_not_refused(
    argv, module, attr, tmp_path: Path, monkeypatch
) -> None:
    from ciao.cli import main as cli_main

    _conflicting_install(tmp_path, monkeypatch)
    _stub_write(monkeypatch, module, attr)

    # Without --apply the handler still reaches its library call (the preview),
    # so the conflict must not short-circuit it with exit 2.
    with pytest.raises(_Reached):
        cli_main(argv)


def test_workspace_reroot_apply_is_refused_on_conflict(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao.cli import main as cli_main

    _conflicting_install(tmp_path, monkeypatch)
    calls = _stub_write(monkeypatch, "ciao.workspace_reroot", "apply")

    assert cli_main(["workspace-reroot", "--apply"]) == 2
    assert calls == []
    assert "name different vaults" in capsys.readouterr().err


def test_workspace_reroot_apply_with_explicit_workspace_proceeds(
    tmp_path: Path, monkeypatch
) -> None:
    from ciao.cli import main as cli_main

    root, _ = _conflicting_install(tmp_path, monkeypatch)
    _stub_write(monkeypatch, "ciao.workspace_reroot", "apply")

    with pytest.raises(_Reached):
        cli_main(["workspace-reroot", "--apply", "--workspace", str(root)])


def test_workspace_reroot_plan_is_not_refused(tmp_path: Path, monkeypatch) -> None:
    from ciao.cli import main as cli_main

    _conflicting_install(tmp_path, monkeypatch)
    _stub_write(monkeypatch, "ciao.workspace_reroot", "plan")

    with pytest.raises(_Reached):
        cli_main(["workspace-reroot"])


# The review-queue writers resolve their vault through _proposal_config. The
# refusal sits before any input is read, so the stub is the first thing that can
# observe a call.
def _skill_writer_argv(tmp_path: Path, command: str) -> list[str]:
    payload = tmp_path / "input.json"
    payload.write_text("{}", encoding="utf-8")
    return {
        "skill-proposal-add": ["skill-proposal-add", "some-skill", "--input-file", str(payload)],
        "skill-proposal-remove": ["skill-proposal-remove", "some-skill"],
        "skill-draft-add": ["skill-draft-add", "--input-file", str(payload)],
        "skill-draft-approve": ["skill-draft-approve", "draft-1"],
        "skill-draft-reject": ["skill-draft-reject", "draft-1"],
    }[command]


_SKILL_WRITERS = [
    "skill-proposal-add",
    "skill-proposal-remove",
    "skill-draft-add",
    "skill-draft-approve",
    "skill-draft-reject",
]


@pytest.mark.parametrize("command", _SKILL_WRITERS)
def test_skill_queue_write_is_refused_on_conflict(
    command, tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    _conflicting_install(tmp_path, monkeypatch)
    calls = _stub_write(monkeypatch, "ciao.cli", "_proposal_config")

    assert cli.main(_skill_writer_argv(tmp_path, command)) == 2
    assert calls == []
    assert "name different vaults" in capsys.readouterr().err


@pytest.mark.parametrize("command", _SKILL_WRITERS)
def test_skill_queue_write_with_explicit_vault_root_proceeds(
    command, tmp_path: Path, monkeypatch
) -> None:
    from ciao import cli

    _conflicting_install(tmp_path, monkeypatch)
    named = tmp_path / "named-vault"
    named.mkdir()
    # The add commands read their input before resolving the vault. An empty
    # payload is not a finding, so the readers are stubbed to reach the stop
    # point; the test is about the vault choice, not the payload's shape.
    monkeypatch.setattr(cli, "_read_skill_proposal_input", lambda path: ({}, ""))
    monkeypatch.setattr(cli, "_read_skill_draft_input", lambda path: ({}, ""))
    _stub_write(monkeypatch, "ciao.cli", "_proposal_config")

    argv = _skill_writer_argv(tmp_path, command) + ["--vault-root", str(named)]
    with pytest.raises(_Reached):
        cli.main(argv)
