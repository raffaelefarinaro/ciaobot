"""A fresh install is CREATED in the per-workspace layout, not migrated into it.

Setup used to scaffold `memory-vault/personal` plus agent assets at the install
root — so every new user was manufactured into exactly the state the re-rooting
exists to fix, and met a blocking "migrate now" tile on first boot. The migration
engine had an audience that regenerated itself.

The receipt matters as much as the folders: `agent_root()` answers per-root only
when a receipt says so, and files nested with a gate that still says "shared" is
the one combination that breaks every layout-dependent path.
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from ciao.cli import setup_workspace
from ciao.config import CiaoConfig, reset_reroot_cache
from ciao.operator_actions import DetectionContext, detect_actions
from ciao.web.agent_assets import workspace_health


def _fresh(tmp_path: Path) -> CiaoConfig:
    setup_workspace(tmp_path, auth_token="t")
    reset_reroot_cache()
    return CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_VAULT_ROOT": str(tmp_path / "memory-vault"),
    })


def test_a_fresh_setup_builds_the_nested_layout(tmp_path: Path) -> None:
    config = _fresh(tmp_path)

    assert (tmp_path / "personal" / "memory-vault").is_dir()
    assert not (tmp_path / "memory-vault").exists()
    assert config.agent_root("personal") == tmp_path / "personal"
    assert config.workspace_vault_root("personal") == tmp_path / "personal" / "memory-vault"


def test_default_setup_provenance_allows_local_index_refresh(tmp_path: Path) -> None:
    """The generated CIAO_VAULT_ROOT env value does not make Ciao's own vault external."""
    from ciao.main import _refresh_vault_index

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    old_index = vault / "INDEX.md"
    old_index.write_text("old generated index\n", encoding="utf-8")
    note = vault / "People" / "Ada.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntype: person\ntitle: Ada\n---\n# Ada\n", encoding="utf-8")

    assert config.ciao_owned_vault_index("personal")
    assert _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=tmp_path / ".runtime",
    )
    assert "Ada" in old_index.read_text(encoding="utf-8")


def test_setup_marks_existing_notes_folder_as_external_for_index_refresh(
    tmp_path: Path,
) -> None:
    from ciao.main import _refresh_vault_index

    (tmp_path / "note.md").write_text("# Existing note\n", encoding="utf-8")
    prior = tmp_path / "INDEX.md"
    prior.write_text("external snapshot\n", encoding="utf-8")
    setup_workspace(tmp_path, auth_token="t", vault_mode="existing")
    from ciao.setup_marker import read_vault_index_ownership

    # An adopted existing folder is recorded as one external aggregate root;
    # nested legacy sources below it inherit that provenance.
    assert read_vault_index_ownership(tmp_path / ".runtime") == {
        str(tmp_path.resolve()): "external"
    }
    reset_reroot_cache()
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_VAULT_ROOT": str(tmp_path),
    })

    assert not config.ciao_owned_vault_index("personal")
    assert not _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots=set(),
        runtime_dir=tmp_path / ".runtime",
    )
    assert prior.read_text(encoding="utf-8") == "external snapshot\n"


def test_existing_folder_setup_never_seeds_an_index_on_first_run_or_rerun(tmp_path: Path) -> None:
    from ciao.config import reset_reroot_cache

    (tmp_path / "original.md").write_text("# Original\n")
    for _ in range(2):
        reset_reroot_cache()
        setup_workspace(tmp_path, auth_token="t", vault_mode="existing")
        assert not (tmp_path / "INDEX.md").exists()


def test_scratch_marker_without_exact_map_does_not_prove_ownership(
    tmp_path: Path,
) -> None:
    from ciao.main import _refresh_vault_index

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_bytes(b"old generated index\r\n")
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Ada.md").write_text("# orchid\n", encoding="utf-8")
    (tmp_path / ".runtime" / "vault-index-ownership.json").unlink()
    reset_reroot_cache()
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_VAULT_ROOT": str(tmp_path / "memory-vault"),
    })

    assert config.ciao_owned_vault_index("personal")
    assert _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=tmp_path / ".runtime",
    )
    assert index.read_bytes().startswith(b"<!-- generated by ciao vault-index")


def test_scratch_install_repointed_to_an_external_root_is_not_owned(
    tmp_path: Path,
) -> None:
    from ciao.config import WorkspaceConfig

    config = _fresh(tmp_path)
    external = tmp_path / "external-vault"
    external.mkdir()
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        vault_root=external,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root=str(external))
        },
    )

    assert config.vault_index_ownership("personal") != "ciao"


def test_synchronized_destination_is_external_even_with_exact_ciao_map(
    tmp_path: Path, monkeypatch,
) -> None:
    from ciao import setup_status
    from ciao.main import _refresh_vault_index

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_bytes(b"external sentinel\r\n")
    monkeypatch.setattr(
        setup_status, "cloud_synced_location",
        lambda path, **kwargs: "Google Drive",
    )

    assert config.vault_index_ownership("personal") == "external"
    assert not config.ciao_owned_vault_index("personal")
    assert _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={
            vault for vault in [config.agent_vault_root("personal")]
            if config.ciao_owned_vault_index("personal")
        },
        runtime_dir=tmp_path / ".runtime",
    )
    assert index.read_bytes() == b"external sentinel\r\n"


def test_legacy_scratch_install_without_provenance_is_refreshed(
    tmp_path: Path,
) -> None:
    """Maintainer decision: an existing local workspace index is rebuilt even
    when an older install has no ownership provenance."""
    from ciao.main import _refresh_vault_index

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_bytes(b"pre-provenance complete index\r\n")
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Ada.md").write_text("# orchid orchard\n", encoding="utf-8")
    (tmp_path / ".runtime" / "vault-index-ownership.json").unlink()
    (tmp_path / ".runtime" / "setup-completed-at").unlink()
    reset_reroot_cache()
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_VAULT_ROOT": str(tmp_path / "memory-vault"),
    })

    assert config.ciao_owned_vault_index("personal")
    assert _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=tmp_path / ".runtime",
    )
    assert b"Ada" in index.read_bytes()


def test_existing_shared_install_without_provenance_upgrades_to_refresh(
    tmp_path: Path,
) -> None:
    """An upgraded legacy shared install rebuilds its aggregate INDEX.md.

    There is no ownership map and no per-root receipt yet; the maintainer
    decision treats the registered shared vault's generated index as Ciao's.
    """
    import json as json_module

    from ciao.main import _refresh_vault_index

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    vault = tmp_path / "memory-vault"
    (vault / "personal" / "People").mkdir(parents=True)
    (vault / "personal" / "People" / "Ada.md").write_text(
        "---\ntype: person\n---\n# Ada\n", encoding="utf-8"
    )
    index = vault / "INDEX.md"
    index.write_bytes(b"legacy generated index\r\n")
    (runtime / "workspaces.json").write_text(
        json_module.dumps([{
            "name": "personal",
            "vault_root": "memory-vault/personal",
        }]),
        encoding="utf-8",
    )
    reset_reroot_cache()
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(runtime),
        "CIAO_VAULT_ROOT": str(vault),
    })

    assert config.ciao_owned_vault_index("personal")
    assert _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=runtime,
    )
    assert b"Ada" in index.read_bytes()


def test_a_fresh_setup_puts_the_agent_assets_in_the_workspace(tmp_path: Path) -> None:
    """`agent_roots_for` reads the receipt for the gate and the REGISTRY for the
    names, so both must exist before the asset loop — with no registry it falls
    back to the install root, which is how the first attempt still left
    `.claude/`, `commands/` and a stock CLAUDE.md beside the nested vault."""
    _fresh(tmp_path)

    for asset in ("AGENTS.md", "commands", "subagents", ".claude"):
        assert (tmp_path / "personal" / asset).exists(), asset
        assert not (tmp_path / asset).exists(), f"litter at the install root: {asset}"


def test_a_fresh_setup_needs_no_migration_and_shows_no_tile(tmp_path: Path) -> None:
    """The point of the whole change: a new user never sees migration UX."""
    from ciao.workspace_reroot import migrate_if_needed

    config = _fresh(tmp_path)

    assert migrate_if_needed(config)["status"] == "already_migrated"
    # A fresh install is a configured install, so the GitHub-star nudge would
    # legitimately surface; it is not migration UX, so silence it here.
    (tmp_path / ".runtime" / "star-receipt.json").write_text(
        json.dumps({"status": "starred", "at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    kinds = {a.kind for a in detect_actions(
        DetectionContext(config=config, runtime_dir=tmp_path / ".runtime")
    )}
    assert "workspace-unmigrated" not in kinds
    assert kinds == set(), kinds


def test_a_fresh_setup_is_healthy_before_its_first_boot(tmp_path: Path) -> None:
    """Setup builds the generated catalogs too. Without that a brand-new install
    showed nine Workspace Health warnings and an assets tile on an install where
    nothing was wrong — it just had not synced yet."""
    config = _fresh(tmp_path)

    health = workspace_health(config)

    assert health["status"] == "ok", [
        c for c in health["checks"] if c["status"] != "ok"
    ]


def test_the_receipt_records_that_it_was_born_this_way(tmp_path: Path) -> None:
    """A reader can tell "created per-root" from "migrated" without inferring it
    from an empty move list."""
    _fresh(tmp_path)

    receipt = json.loads(
        (tmp_path / ".runtime" / "migration" / "workspace-rooting.json").read_text()
    )

    assert receipt["status"] == "migrated"
    assert receipt["born_per_root"] is True
    assert receipt["origin"] == "born"
    assert receipt["moves"] == []


def test_an_existing_notes_folder_kept_in_place_needs_no_migration(tmp_path: Path) -> None:
    """Choosing an existing notes folder keeps the notes where they are, so the
    vault is the install folder itself. That is a supported layout: the next
    boot must not try to re-root it (the plan cannot tell notes from install
    files and refused on every start), and Home must not offer a relocation
    that `vault-relocate` refuses for this shape (#2021)."""
    from dotenv import dotenv_values

    from ciao.migration_notices import (
        RELOCATION_INSTALL_FOLDER,
        relocation_state,
        vault_location_findings,
    )
    from ciao.workspace_reroot import migrate_if_needed

    (tmp_path / "existing.md").write_text("# Existing note\n", encoding="utf-8")
    setup_workspace(tmp_path, auth_token="t", vault_mode="existing")
    reset_reroot_cache()
    env = {k: v for k, v in dotenv_values(tmp_path / ".env").items() if v is not None}
    config = CiaoConfig.from_env({
        **env,
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
    })

    assert Path(config.vault_root).resolve() == tmp_path.resolve()
    # Fresh adoption now has the same born-per-root receipt as empty-folder
    # setup, so startup retires migration before it can snapshot any notes.
    assert migrate_if_needed(config)["status"] == "already_migrated"
    assert vault_location_findings(config) == []
    # The Home card asks the same question of the same predicate (#2051: the
    # card still offered a move on a Windows install after the notice stopped).
    assert all(
        relocation_state(config, name) == RELOCATION_INSTALL_FOLDER
        for name in config.workspace_names()
    )
    assert (tmp_path / "existing.md").read_text(encoding="utf-8") == "# Existing note\n"
    assert not (tmp_path / ".git").exists()


def test_a_fresh_setup_writes_the_vocabulary_the_first_memory_pass_types_from(
    tmp_path: Path, monkeypatch,
) -> None:
    """A new workspace has no VOCABULARY.md until the vault index runs, so the
    first memory pass cannot type a note: it reads the Categories section of
    that file and asks the owner to create it. Setup writes it up front."""
    from ciao import vault_index

    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(tmp_path / ".runtime"))
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(tmp_path / "memory-vault"))

    vault = tmp_path / "personal" / "memory-vault"
    written = setup_workspace(tmp_path, auth_token="t")

    vocabulary = vault / "VOCABULARY.md"
    assert vocabulary.is_file()
    assert vocabulary.resolve() in {Path(p).resolve() for p in written}
    text = vocabulary.read_text(encoding="utf-8")
    assert "## Categories" in text
    assert "- `project`" in text
    assert "- `person`" in text
    assert "## Types (canonical" in text

    # Byte parity with `ciao vault-index --write`, the generator the weekly
    # hygiene pass runs: setup must not produce a different file.
    before = vocabulary.read_bytes()
    assert vault_index.main(["--vault-root", str(vault), "--write"]) == 0
    assert vocabulary.read_bytes() == before


def test_a_setup_rerun_keeps_an_owner_edited_vocabulary(tmp_path: Path) -> None:
    """Setup only fills in what is missing; it never rewrites a vocabulary the
    owner or the vault index already maintains."""
    _fresh(tmp_path)
    vocabulary = tmp_path / "personal" / "memory-vault" / "VOCABULARY.md"
    vocabulary.write_text("edited by hand\n", encoding="utf-8")

    setup_workspace(tmp_path, auth_token="t")

    assert vocabulary.read_text(encoding="utf-8") == "edited by hand\n"


def test_truncated_ownership_map_fails_closed_and_refuses_update(
    tmp_path: Path,
) -> None:
    import pytest

    from ciao.main import _refresh_vault_index
    from ciao.setup_marker import (
        VAULT_INDEX_OWNERSHIP_FILENAME,
        VaultIndexOwnershipUnreadable,
        read_vault_index_ownership,
        record_vault_index_ownership,
    )

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_bytes(b"external sentinel\r\n")
    ownership_path = tmp_path / ".runtime" / VAULT_INDEX_OWNERSHIP_FILENAME
    ownership_path.write_text('{"roots": [', encoding="utf-8")
    reset_reroot_cache()

    assert read_vault_index_ownership(tmp_path / ".runtime") == {}
    assert not config.ciao_owned_vault_index("personal")
    with pytest.raises(VaultIndexOwnershipUnreadable):
        record_vault_index_ownership(tmp_path / ".runtime", vault, owner="external")
    assert ownership_path.read_text(encoding="utf-8") == '{"roots": ['

    assert _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots=set(),
        runtime_dir=tmp_path / ".runtime",
    )
    assert index.read_bytes() == b"external sentinel\r\n"

    ownership_path.unlink()
    record_vault_index_ownership(tmp_path / ".runtime", vault, owner="external")
    assert not config.ciao_owned_vault_index("personal")


def test_external_declaration_invalidates_a_cached_owned_config(
    tmp_path: Path,
) -> None:
    from ciao.main import _refresh_vault_index
    from ciao.setup_marker import record_vault_index_ownership

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_bytes(b"external sentinel\r\n")
    assert config.ciao_owned_vault_index("personal")

    record_vault_index_ownership(tmp_path / ".runtime", vault, owner="external")

    assert not config.ciao_owned_vault_index("personal")
    # The stale pre-declaration owned set must not authorize the final write:
    # the authorizer is re-read under the ownership lock at the publication seam.
    assert not _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=tmp_path / ".runtime",
        authorize_root=lambda root: config.vault_index_ownership(None, root) == "ciao",
    )
    assert index.read_bytes() == b"external sentinel\r\n"


def test_registry_reader_rejects_malformed_shared_brain_rows(tmp_path: Path) -> None:
    from ciao.workspaces import authoritative_shared_links

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    registry = runtime / "workspaces.json"
    registry.write_bytes(b'[{"name":"personal","shared_brains":\xff}]')
    assert authoritative_shared_links(runtime) is None

    registry.write_text(
        json.dumps([{"name": "personal", "shared_brains": "not-a-list"}]),
        encoding="utf-8",
    )
    assert authoritative_shared_links(runtime) is None

    registry.write_text(
        json.dumps([{"name": "personal", "shared_brains": [{"name": "team"}]}]),
        encoding="utf-8",
    )
    assert authoritative_shared_links(runtime) is None

    duplicate_workspaces = [
        {"name": "personal"},
        {"name": "personal"},
    ]
    registry.write_text(json.dumps(duplicate_workspaces), encoding="utf-8")
    assert authoritative_shared_links(runtime) is None

    duplicate_links = [{
        "name": "personal",
        "shared_brains": [
            {"name": "team", "path": str(tmp_path / "team"), "identity": "id-1"},
            {"name": "other", "path": str(tmp_path / "team"), "identity": "id-2"},
        ],
    }]
    registry.write_text(json.dumps(duplicate_links), encoding="utf-8")
    assert authoritative_shared_links(runtime) is None

    registry.write_text(
        json.dumps([{
            "name": "personal",
            "shared_brains": [{
                "name": "team",
                "path": str(tmp_path / "team"),
                "identity": "github.com/example/team",
            }],
        }]),
        encoding="utf-8",
    )
    assert authoritative_shared_links(runtime) == (tmp_path / "team",)


def test_independent_process_registration_is_blocked_at_publication(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys
    import time

    from ciao.main import _refresh_vault_index

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_bytes(b"external sentinel\r\n")
    runtime = tmp_path / ".runtime"
    ready = tmp_path / "registrar-ready"
    script = """
import fcntl, json, os, sys
runtime, vault, ready = sys.argv[1:4]
lock = os.path.join(runtime, "locks", "workspaces-registry.lock")
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
open(ready, "w").write("ready")
fcntl.flock(fd, fcntl.LOCK_EX)
path = os.path.join(runtime, "workspaces.json")
rows = json.load(open(path))
for row in rows:
    if row.get("name") == "personal":
        row.setdefault("shared_brains", []).append({
            "name": "team", "path": vault, "identity": "github.com/example/team",
        })
tmp = path + ".tmp"
with open(tmp, "w") as stream:
    stream.write(json.dumps(rows))
os.replace(tmp, path)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""
    registrar: subprocess.Popen[bytes] | None = None

    def authorize(_root: Path) -> bool:
        nonlocal registrar
        if registrar is None:
            registrar = subprocess.Popen(
                [sys.executable, "-c", script, str(runtime), str(vault), str(ready)]
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists()
            time.sleep(0.2)
            assert registrar.poll() is None, "registration was not fenced"
        return False

    assert not _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=runtime,
        authorize_root=authorize,
    )
    assert registrar is not None
    assert registrar.wait(timeout=5) == 0
    assert not config.ciao_owned_vault_index("personal")
    assert index.read_bytes() == b"external sentinel\r\n"


def test_publication_succeeds_with_independent_unrelated_registration(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys
    import time

    from ciao.main import _refresh_vault_index
    from ciao.workspaces import authoritative_shared_links

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_text("previous complete index\n", encoding="utf-8")
    runtime = tmp_path / ".runtime"
    other = tmp_path / "other-brain"
    other.mkdir()
    ready = tmp_path / "registrar-ready"
    script = """
import fcntl, json, os, sys
runtime, path, ready = sys.argv[1:4]
lock = os.path.join(runtime, "locks", "workspaces-registry.lock")
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
open(ready, "w").write("ready")
fcntl.flock(fd, fcntl.LOCK_EX)
registry = os.path.join(runtime, "workspaces.json")
rows = json.load(open(registry))
for row in rows:
    if row.get("name") == "personal":
        row.setdefault("shared_brains", []).append({
            "name": "other", "path": path, "identity": "github.com/example/other",
        })
tmp = registry + ".tmp"
with open(tmp, "w") as stream:
    stream.write(json.dumps(rows))
os.replace(tmp, registry)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""
    registrar: subprocess.Popen[bytes] | None = None

    def authorize(_root: Path) -> bool:
        nonlocal registrar
        if registrar is None:
            registrar = subprocess.Popen(
                [sys.executable, "-c", script, str(runtime), str(other), str(ready)]
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists()
            time.sleep(0.2)
            assert registrar.poll() is None, "registration was not fenced"
        return True

    assert _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=runtime,
        authorize_root=authorize,
    )
    assert registrar is not None
    assert registrar.wait(timeout=5) == 0
    assert index.read_bytes().startswith(b"<!-- generated by ciao vault-index")
    assert authoritative_shared_links(runtime) == (other,)


def test_registration_during_scan_blocks_markdown_publication(
    tmp_path: Path,
) -> None:
    from ciao.main import _refresh_vault_index

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_bytes(b"external sentinel\r\n")
    assert config.ciao_owned_vault_index("personal")

    # A separate process links this exact destination while the stale scan
    # still believes the root is Ciao-owned.
    (tmp_path / ".runtime" / "workspaces.json").write_text(
        json.dumps([{
            "name": "personal",
            "vault_root": "personal/memory-vault",
            "shared_brains": [{
                "name": "team",
                "path": str(vault),
                "identity": "github.com/example/team",
            }],
        }]),
        encoding="utf-8",
    )
    assert not config.ciao_owned_vault_index("personal")
    assert not _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=tmp_path / ".runtime",
        authorize_root=lambda root: config.vault_index_ownership(None, root) == "ciao",
    )
    assert index.read_bytes() == b"external sentinel\r\n"


def test_utf8_invalid_ownership_map_fails_closed(tmp_path: Path) -> None:
    import pytest

    from ciao.setup_marker import (
        VAULT_INDEX_OWNERSHIP_FILENAME,
        VaultIndexOwnershipUnreadable,
        read_vault_index_ownership,
        record_vault_index_ownership,
    )

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    path = runtime / VAULT_INDEX_OWNERSHIP_FILENAME
    path.write_bytes(b'{"roots":[{"path":"\xff","owner":"ciao"}]}')

    assert read_vault_index_ownership(runtime) == {}
    with pytest.raises(VaultIndexOwnershipUnreadable):
        record_vault_index_ownership(runtime, tmp_path / "vault", owner="external")
    assert path.read_bytes().startswith(b'{"roots"')


def test_stale_owned_scan_cannot_write_after_external_declaration(
    tmp_path: Path,
) -> None:
    from ciao.main import _refresh_vault_index
    from ciao.setup_marker import record_vault_index_ownership

    config = _fresh(tmp_path)
    vault = config.agent_vault_root("personal")
    index = vault / "INDEX.md"
    index.write_bytes(b"external sentinel\r\n")
    record_vault_index_ownership(tmp_path / ".runtime", vault, owner="external")

    # The stale set models a scan classified before the declaration. The write
    # seam rechecks under the shared ownership lock and must refuse.
    assert not _refresh_vault_index(
        tmp_path,
        targets=config.vault_scan_targets(),
        ciao_owned_roots={vault},
        runtime_dir=tmp_path / ".runtime",
        authorize_root=lambda root: config.ciao_owned_vault_index("personal"),
    )
    assert index.read_bytes() == b"external sentinel\r\n"


def test_stale_config_save_refuses_busy_then_changed_and_preserves_link(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys
    import threading
    import time

    from ciao.workspaces import WorkspaceRegistryBusy, WorkspaceRegistryChanged

    config = _fresh(tmp_path)
    runtime = tmp_path / ".runtime"
    registry = runtime / "workspaces.json"
    (runtime / "locks").mkdir(parents=True, exist_ok=True)
    other = tmp_path / "other-brain"
    other.mkdir()
    ready = tmp_path / "save-registrar-ready"
    release = tmp_path / "save-registrar-release"
    script = """
import fcntl, json, os, sys, time
runtime, path, ready, release = sys.argv[1:5]
lock = os.path.join(runtime, "locks", "workspaces-registry.lock")
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(ready, "w").write("ready")
deadline = time.monotonic() + 30
while not os.path.exists(release):
    if time.monotonic() > deadline:
        sys.exit(2)
    time.sleep(0.02)
registry = os.path.join(runtime, "workspaces.json")
rows = json.load(open(registry))
for row in rows:
    if row.get("name") == "personal":
        row.setdefault("shared_brains", []).append({
            "name": "other", "path": path, "identity": "github.com/example/other",
        })
tmp = registry + ".tmp"
with open(tmp, "w") as stream:
    stream.write(json.dumps(rows))
os.replace(tmp, registry)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""
    registrar = subprocess.Popen(
        [sys.executable, "-c", script, str(runtime), str(other), str(ready), str(release)]
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    errors: list[BaseException] = []

    def save() -> None:
        try:
            config.persist_workspace_registry()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    writer = threading.Thread(target=save)
    try:
        writer.start()
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], WorkspaceRegistryBusy)
    finally:
        release.write_text("go", encoding="utf-8")
        assert registrar.wait(timeout=5) == 0
    with pytest.raises(WorkspaceRegistryChanged):
        config.persist_workspace_registry()
    rows = json.loads(registry.read_text(encoding="utf-8"))
    personal = next(row for row in rows if row["name"] == "personal")
    assert personal["shared_brains"][0]["path"] == str(other)


def test_rollback_restore_preserves_concurrent_link_written_by_other_process(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys
    import threading
    import time

    from ciao.workspaces import restore_registry_entries

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    registry = runtime / "workspaces.json"
    prior = [{"name": "personal", "vault_root": "old-personal"}]
    expected = [{"name": "personal", "vault_root": "personal/memory-vault"}]
    (runtime / "locks").mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps([
            expected[0],
            {"name": "work", "vault_root": "work/memory-vault"},
        ]),
        encoding="utf-8",
    )
    ready = tmp_path / "rollback-registrar-ready"
    release = tmp_path / "rollback-registrar-release"
    script = """
import fcntl, json, os, sys, time
runtime, ready, release = sys.argv[1:4]
lock = os.path.join(runtime, "locks", "workspaces-registry.lock")
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(ready, "w").write("ready")
deadline = time.monotonic() + 30
while not os.path.exists(release):
    if time.monotonic() > deadline:
        sys.exit(2)
    time.sleep(0.02)
path = os.path.join(runtime, "workspaces.json")
rows = json.load(open(path))
for row in rows:
    if row.get("name") == "work":
        row["shared_brains"] = [{
            "name": "team", "path": "/tmp/team", "identity": "github.com/example/team",
        }]
tmp = path + ".tmp"
with open(tmp, "w") as stream:
    stream.write(json.dumps(rows))
os.replace(tmp, path)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""
    registrar = subprocess.Popen(
        [sys.executable, "-c", script, str(runtime), str(ready), str(release)]
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    results: list[bool] = []
    rollback = threading.Thread(
        target=lambda: results.append(
            restore_registry_entries(runtime, prior, expected)
        )
    )
    rollback.start()
    time.sleep(0.2)
    assert rollback.is_alive()
    release.write_text("go", encoding="utf-8")
    assert registrar.wait(timeout=5) == 0
    rollback.join(timeout=5)
    assert not rollback.is_alive()
    # Only the transaction-owned personal row is restored; the unrelated
    # work-space link is outside the target set and survives.
    assert results == [True]
    rows = json.loads(registry.read_text(encoding="utf-8"))
    by_name = {row["name"]: row for row in rows}
    assert by_name["personal"]["vault_root"] == "old-personal"
    assert by_name["work"]["shared_brains"][0]["path"] == "/tmp/team"


def test_publication_reuses_the_existing_registry_owner(tmp_path: Path) -> None:
    from ciao.vault_index import write_index_file
    from ciao.workspaces import acquire_registry_at, active_registry_owner

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (runtime / "workspaces.json").write_text("[]\n", encoding="utf-8")
    owner = acquire_registry_at(runtime, blocking=True)
    seen: list[bool] = []
    try:
        def authorize() -> bool:
            seen.append(active_registry_owner(runtime) is owner)
            return True

        write_index_file(
            [],
            vault / "INDEX.md",
            replace_existing=True,
            authorize=authorize,
            ownership_runtime=runtime,
            registration_runtime=runtime,
            registry_owner=owner,
        )
    finally:
        owner.close()

    assert seen == [True]
    assert (vault / "INDEX.md").is_file()


def test_multi_row_target_mismatch_leaves_registry_bytes_unchanged(
    tmp_path: Path,
) -> None:
    from ciao.workspaces import restore_registry_entries

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    registry = runtime / "workspaces.json"
    registry.write_text(
        json.dumps([
            {"name": "personal", "vault_root": "personal/memory-vault"},
            {"name": "work", "vault_root": "work/changed"},
        ]),
        encoding="utf-8",
    )
    before = registry.read_bytes()
    prior = [
        {"name": "personal", "vault_root": "memory-vault/personal"},
        {"name": "work", "vault_root": "memory-vault/work"},
    ]
    expected = [
        {"name": "personal", "vault_root": "personal/memory-vault"},
        {"name": "work", "vault_root": "work/memory-vault"},
    ]

    restored = restore_registry_entries(
        runtime, prior, expected, fields=("vault_root",)
    )

    assert restored is False
    assert registry.read_bytes() == before


def test_ownership_updates_are_serialized_with_readers(tmp_path: Path) -> None:
    import threading

    from ciao.setup_marker import (
        read_vault_index_ownership,
        vault_index_ownership_lock,
        write_vault_index_ownership,
    )

    runtime = tmp_path / ".runtime"
    vault = tmp_path / "vault"
    runtime.mkdir()
    vault.mkdir()
    write_vault_index_ownership(runtime, [vault], owner="ciao")
    entered = threading.Event()
    release = threading.Event()
    observed: list[dict[str, str]] = []

    def hold_update() -> None:
        with vault_index_ownership_lock(runtime):
            entered.set()
            assert release.wait(2)

    holder = threading.Thread(target=hold_update)
    holder.start()
    assert entered.wait(2)
    reader = threading.Thread(
        target=lambda: observed.append(read_vault_index_ownership(runtime))
    )
    reader.start()
    import time

    time.sleep(0.1)
    assert reader.is_alive()
    release.set()
    holder.join(2)
    reader.join(2)
    assert not holder.is_alive() and not reader.is_alive()
    assert observed == [{str(vault.resolve()): "ciao"}]


def test_a_failed_vocabulary_write_does_not_fail_setup(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The vocabulary is a scaffold step like the rest: a scan that raises is
    reported on stderr, and setup still finishes with its registry and receipt
    in place."""
    from ciao import vault_index

    def _boom(*args, **kwargs):
        raise OSError("disk says no")

    monkeypatch.setattr(vault_index, "scan_vault", _boom)

    written = setup_workspace(tmp_path, auth_token="t")

    vault = tmp_path / "personal" / "memory-vault"
    assert (tmp_path / ".runtime" / "workspaces.json").is_file()
    assert written
    assert not (vault / "VOCABULARY.md").exists()
    err = capsys.readouterr().err
    assert "vocabulary not written for" in err
    assert str(vault.resolve()) in err


def test_api_created_workspace_gets_a_vault_index_on_the_next_refresh(
    tmp_path: Path,
) -> None:
    """#3661: setup records only the first workspace's root in the ownership map.

    A workspace added through POST /api/workspaces is a registered local root,
    which the classifier treats as Ciao-owned by name; the publication recheck
    used to ask without the name, saw an unrecorded root as ambiguous and
    refused ("index ownership changed before publication").
    """
    from ciao.main import _refresh_configured_vault_index
    from ciao.setup_marker import read_vault_index_ownership
    from tests.test_workspace_settings_routes import _client

    setup_workspace(tmp_path, auth_token="t")
    reset_reroot_cache()
    client, config, _ = _client(tmp_path, {
        "CIAO_VAULT_ROOT": str(tmp_path / "memory-vault"),
    })
    created = client.post("/api/workspaces", json={"name": "beta"})
    assert created.status_code == 201
    reset_reroot_cache()
    beta = config.agent_vault_root("beta")
    beta.mkdir(parents=True, exist_ok=True)
    (beta / "People").mkdir(exist_ok=True)
    (beta / "People" / "Bea.md").write_text(
        "---\ntype: person\ntitle: Bea\n---\n# Bea\n", encoding="utf-8",
    )
    assert str(beta.resolve()) not in read_vault_index_ownership(tmp_path / ".runtime")

    assert _refresh_configured_vault_index(config)
    assert "Bea" in (beta / "INDEX.md").read_text(encoding="utf-8")
