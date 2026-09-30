"""What the managed remedies actually accept once the install is re-rooted.

`docs/VAULT_MIGRATION_PROMPT.md` was rewritten twice: first to point at the
managed commands, then to stop claiming a per-workspace invocation the commands
do not support. Both corrections rest on facts about paths and receipts, and
prose cannot check a path. These are the fixtures that check them, on synthetic
trees — no real vault, no engine, no network, and every `ciao` invocation below
passes `--runtime-root` so a receipt cannot land in the checkout's `.runtime`.

What is pinned, and why each is a trap rather than a detail:

* **After re-rooting there is no install-root `memory-vault/`.** The vault
  default for `vault-migrate-links`, `vault-migrate`, `vault-rehome`,
  `vault-lint` and `workspace-census` is `CIAO_VAULT_ROOT` or `./memory-vault`,
  so a bare invocation from the install root after the migration resolves a path
  that is not there. That is the single fact the document's ordering advice
  ("run the vault migrations before Step 1") rests on.
* **The link migration's receipt is per install, not per vault.**
  `vault_migrate_links.read_receipt` takes a runtime root and nothing else, so
  the first converted root marks the whole install converted and the second is
  refused. A document that told an operator to convert each workspace's vault
  would be wrong on the second one.
* **`vault-rehome` plans `<vault>/<workspace>/People`, and nothing else.** It
  is the one remedy here with no per-root form, because a per-root vault has no
  workspace segment for a note to be misfiled out of. The fixture asserts both
  halves: empty for a root, non-empty for the same notes under a segment, so
  the empty answer is the layout and not a broken detector.
* **`--force` overrides the nesting rail, and the inverse commands accept and
  ignore it.** The nesting rail gates the preview as well as the write, so
  `--force` is the only way to look at a too-narrow root at all. Both halves are
  the kind of thing a document gets backwards and a reader loses a receipt over.
* **`ciao vault-migrate --apply` does not write the vocabulary receipt.** The
  rename lands and nothing is recorded, so the Home card cannot clear from a CLI
  run — `sync-skills` and the card's own button are what write it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao import cli
from ciao.vault_migrate_links import has_unmigrated_links
from ciao.vault_migrate_links import read_receipt as read_links_receipt
from ciao.vault_rehome import plan_rehome
from ciao.workspace_reroot import mark_born_per_root
from ciao.workspace_reroot import read_receipt as read_reroot_receipt


def _note(path: Path, body: str = "Mo.", *, tags: list[str] | None = None) -> None:
    """One person note, in the shape the re-homing detector reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = "tags:\n" + "".join(f"  - {tag}\n" for tag in tags or [])
    path.write_text(
        f"---\ntype: person\nupdated: 2026-01-01\n{stamped}---\n\n{body}\n",
        encoding="utf-8",
    )


def _re_rooted_install(root: Path, names: tuple[str, ...] = ("personal", "work")) -> Path:
    """An install laid out the way `workspace-reroot --apply` leaves it.

    Built by hand rather than by running the migration: the point of the fixture
    is the shape the migration produces, and asserting the post-condition by
    running the thing under test would prove nothing. The receipt is written
    because the receipt is what makes `agent_root` answer per-root, so a tree
    without it is not the shape being claimed.
    """
    install = root / "install"
    runtime = install / ".runtime"
    for name in names:
        _note(install / name / "memory-vault" / "People" / "Mo.md")
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": name} for name in names]), encoding="utf-8"
    )
    mark_born_per_root(install, runtime, list(names))
    return install


def test_the_install_root_has_no_vault_after_re_rooting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default every vault command resolves is gone once Step 1 has run.

    The environment is pinned because both halves of this depend on it.
    `ciao.cli._resolve_vault_root` reads `CIAO_VAULT_ROOT` and resolves a
    relative default against `CIAO_WORKSPACE`, and the repo's own
    `tests/conftest.py` does not clear either — so an ambient value from a
    developer's shell, or from a running Ciaobot chat, would decide what the
    default resolves to and the assertion would be about the environment rather
    than about the layout. `chdir` into the install root as well, since the
    fallback for both roots is the cwd.
    """
    for name in ("CIAO_VAULT_ROOT", "CIAO_RUNTIME_ROOT", "CIAO_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)

    install = _re_rooted_install(tmp_path)
    runtime = install / ".runtime"
    monkeypatch.chdir(install)

    assert read_reroot_receipt(runtime) is not None, (
        "the fixture is not a re-rooted install: the receipt is what makes "
        "agent_root answer per-root"
    )
    for name in ("personal", "work"):
        assert (install / name / "memory-vault").is_dir()
    assert not (install / "memory-vault").exists(), (
        "a re-rooted install has no install-root memory-vault, which is the "
        "whole reason the document orders Steps 3-5 before Step 1"
    )

    # The cwd fallback resolves to that missing path, so a bare invocation is
    # refused rather than quietly doing nothing.
    assert (install / "memory-vault").exists() is False
    assert cli.main(["vault-migrate-links", "--runtime-root", str(runtime)]) == 1


def test_the_link_migration_receipt_is_per_install_not_per_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One conversion marks the install converted; the second root is refused.

    `read_receipt(runtime_root)` has no vault parameter at all, so there is
    nowhere for a second vault to be recorded. A document promising a
    per-workspace loop here would be promising a second refused run.
    """
    install = _re_rooted_install(tmp_path)
    runtime = install / ".runtime"
    assert read_links_receipt(runtime) is None

    first = install / "personal" / "memory-vault"
    _note(first / "projects" / "a" / "note.md", "See [[Mo]] for the person.")
    assert has_unmigrated_links(first), "the fixture must actually hold a wikilink"

    exit_code = cli.main(
        [
            "vault-migrate-links",
            "--vault-root", str(first),
            "--runtime-root", str(runtime),
            "--apply",
            "--json",
        ]
    )
    capsys.readouterr()
    assert exit_code == 0

    receipt = read_links_receipt(runtime)
    assert receipt is not None, "an applied conversion must leave a receipt"
    assert receipt.get("status") == "migrated"

    # The second root, same runtime root. The receipt says the install is done.
    second = install / "work" / "memory-vault"
    _note(second / "projects" / "a" / "note.md", "See [[Mo]] for the person.")
    exit_code = cli.main(
        [
            "vault-migrate-links",
            "--vault-root", str(second),
            "--runtime-root", str(runtime),
            "--apply",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1, "a second per-root conversion must be refused"
    assert "already migrated" in payload.get("skipped", "")
    assert has_unmigrated_links(second), (
        "the refused run must not have rewritten the second root's notes"
    )
    assert read_links_receipt(runtime) == receipt, (
        "the refusal must leave the first run's reverse map exactly as it was"
    )


def test_vault_rehome_only_plans_a_shared_vault(tmp_path: Path) -> None:
    """A per-root vault has no workspace segment, so there is nothing to plan.

    This is the one remedy with no per-root form, and the fixture is why the
    document says so instead of offering a per-workspace invocation. Passing a
    workspace name does not change the answer either, because the tested thing
    is the note's path, not the registry.
    """
    install = _re_rooted_install(tmp_path)

    per_root = install / "personal" / "memory-vault"
    _note(per_root / "People" / "Mo.md", "Filed under the wrong workspace.", tags=["colleague"])

    assert plan_rehome(per_root)["mechanical"] == []
    assert plan_rehome(per_root, workspaces=["personal", "work"])["mechanical"] == []

    # The same note under a workspace segment in a shared vault does plan, which
    # is what makes the empty answer above about the layout rather than about a
    # detector that never fires.
    shared = tmp_path / "shared" / "memory-vault"
    _note(shared / "personal" / "People" / "Mo.md", tags=["colleague"])
    planned = plan_rehome(shared, workspaces=["personal", "work"])
    assert [c["destination"] for c in planned["mechanical"]], (
        "the shared layout must plan a move, or the empty answer above is a "
        "detector bug and not the layout it is claimed to be"
    )


def test_force_overrides_the_nesting_rail_and_the_inverse_ignores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--force` reaches the nesting rail; the inverse commands discard it.

    The nesting rail gates the *preview* as well as the write, so `--force` is
    the only way to look at a too-narrow root at all — which is why a document
    that says "dry-run by default, `--force` for a dirty tree" is wrong twice
    over. The inverse side matters just as much: `vault-unmigrate-links` takes
    `--force`, does `del force`, and has no nesting rail of its own, so a reader
    who assumes it is being protected is not.
    """
    install = tmp_path / "nested"
    configured = install / "memory-vault"
    narrow = configured / "personal"
    _note(narrow / "People" / "Mo.md", "See [[Someone]].")
    _note(configured / "People" / "Someone.md")

    monkeypatch.setenv("CIAO_VAULT_ROOT", str(configured))
    base = [
        "vault-migrate-links",
        "--vault-root", str(narrow),
        "--runtime-root", str(install / ".runtime"),
    ]

    # Refused, and refused on the preview too: the message says so.
    assert cli.main(base) == 1
    refused = capsys.readouterr().err
    assert "inside the vault at" in refused
    assert "Pass `--force` if you really mean this root." in refused

    # Forced, the same preview runs and reports the too-narrow root as the
    # reason its ref resolves to nothing.
    assert cli.main([*base, "--force"]) == 0
    forced = capsys.readouterr().out
    assert "resolving to nothing" in forced or "No wikilinks found" in forced

    # Forced apply writes the receipt; the inverse accepts --force, ignores it,
    # and reverses anyway. `--json` on the inverse is what proves the receipt is
    # gone, since its output carries `receipt_removed`.
    assert cli.main([*base, "--force", "--apply", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(
        [
            "vault-unmigrate-links",
            "--vault-root", str(narrow),
            "--runtime-root", str(install / ".runtime"),
            "--apply",
            "--force",
            "--json",
        ]
    ) == 0
    undone = json.loads(capsys.readouterr().out)
    assert undone.get("receipt_removed") is True
    assert read_links_receipt(install / ".runtime") is None


def test_vault_migrate_applies_without_writing_the_vocabulary_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`vault-migrate --apply` renames and writes no receipt at all.

    `ciao/cli.py`'s handler calls `migrate_vault_vocabulary` and
    `retain_retired_stock_types` and stops. Neither takes a runtime root, and
    the handler never resolves one, so there is nothing for the CLI to write a
    receipt into.

    The runtime root is pinned by environment because the command has no
    `--runtime-root` flag: `_resolve_runtime_root` would fall back to
    `CIAO_RUNTIME_ROOT` or the cwd, and the first version of this test created a
    `.runtime` by hand and asserted against that directory, which the CLI never
    looks at — so it passed for the wrong reason and would have kept passing if
    the CLI had started writing a receipt somewhere else entirely. The
    assertion is now that **no** migration receipt exists anywhere under the
    pinned root, which is the claim the document actually makes.
    """
    from ciao.vault_migration import read_receipt as read_vocab_receipt

    for name in ("CIAO_VAULT_ROOT", "CIAO_RUNTIME_ROOT", "CIAO_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)

    vault = tmp_path / "vault"
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _note(vault / "journal.md")
    (vault / "journal.md").write_text(
        "---\ntype: project-log\nupdated: 2026-01-01\n---\n\nMo.\n",
        encoding="utf-8",
    )

    assert cli.main(
        ["vault-migrate", "--vault-root", str(vault), "--apply", "--json"]
    ) == 0
    summary = json.loads(capsys.readouterr().out)

    # The rename landed.
    assert summary["renamed"], "the fixture must actually rename something"
    assert "type: journal" in (vault / "journal.md").read_text(encoding="utf-8")
    # No receipt did, anywhere the command could have written one.
    assert not (runtime / "migration").exists(), (
        f"vault-migrate --apply must not create a migration directory; found "
        f"{sorted(p.name for p in (runtime / 'migration').iterdir())}"
    )
    assert read_vocab_receipt(runtime, vault) is None
    assert read_vocab_receipt(runtime) is None, (
        "no vocabulary receipt of any shape, keyed or unkeyed, may exist after "
        "ciao vault-migrate --apply"
    )


def test_vault_migrate_keeps_retired_categories_and_exits_nonzero_on_unresolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both effects of the one command, and the exit code that reports them.

    The command does not only rename: it also copies the retired stock
    categories a vault still uses into that vault's own `entity-types.yaml`,
    which is a write to a file the document has to name. And it exits `1` while
    anything is unresolved, so a run that did every mechanical rename it could
    still reports failure on purpose.
    """
    vault = tmp_path / "vault"
    _note(vault / "log.md")
    (vault / "log.md").write_text(
        "---\ntype: product\nupdated: 2026-01-01\n---\n\nMo.\n",
        encoding="utf-8",
    )
    _note(vault / "odd.md")
    (vault / "odd.md").write_text(
        "---\ntype: something-nobody-defined\nupdated: 2026-01-01\n---\n\nMo.\n",
        encoding="utf-8",
    )

    # The dry run filters the type the retention is about to claim out of its
    # `unresolved` list, because nothing has been written yet and it is not the
    # operator's decision to make. Only the type nobody defined is theirs.
    exit_code = cli.main(
        ["vault-migrate", "--vault-root", str(vault), "--json"]
    )
    dry = json.loads(capsys.readouterr().out)
    assert dry["applied"] is False
    assert dry["retained"] == ["product"]
    assert list(dry["unresolved"]) == ["something-nobody-defined"]
    assert exit_code == 1, "an unresolvable type keeps the exit code non-zero"

    exit_code = cli.main(
        ["vault-migrate", "--vault-root", str(vault), "--apply", "--json"]
    )
    applied = json.loads(capsys.readouterr().out)

    assert applied["retained"] == dry["retained"]
    assert (vault / "entity-types.yaml").is_file(), (
        "retaining a retired category writes this vault's own entity-types.yaml"
    )
    assert "product" in (vault / "entity-types.yaml").read_text(encoding="utf-8")
    # Nothing was renamed (neither type is an alias), and the one type with no
    # canonical equivalent is still the operator's to categorise.
    assert applied["renamed"] == []
    assert list(applied["unresolved"]) == ["something-nobody-defined"]
    assert exit_code == 1, "an unresolved type must keep the exit code non-zero"
