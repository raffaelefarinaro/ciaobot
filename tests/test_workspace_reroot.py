"""V2 gate: fixture assertions for the per-workspace re-rooting plan.

The plan half is read only, so these prove classification and refusal. The
apply half is gated on this file plus the real-data rehearsal, because a
migration that can stop halfway is worse than one that refuses outright.

Every shape the census reports on the real vault needs a fixture counterpart
here; one that does not is a coverage hole. The reference vault reports: notes
under two registered workspaces, a Logs/ tree holding most of them, a
Templates/ folder, an .obsidian/ directory, three generated notes loose at the
vault root, and a .DS_Store beside them.
"""

from __future__ import annotations

import errno
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from ciao import workspace_reroot
from ciao.workspace_reroot import (
    apply,
    receipt_path,
    dirty_tracked_paths,
    format_skill_triage,
    index_workspace_prefixes,
    plan,
    plan_skills_triage,
    read_receipt,
    ensure_rollback_history,
    rebuild_indexes,
    rebuild_search_index,
    rehearse,
    split_guide,
    undo,
    write_receipt,
)


def _vault(tmp_path: Path, *, workspaces: tuple[str, ...] = ("personal", "work")) -> Path:
    """A vault shaped like the real one, small enough to assert over."""
    vault = tmp_path / "memory-vault"
    for name in workspaces:
        (vault / name / "People").mkdir(parents=True)
        (vault / name / "People" / "Peter.md").write_text(
            "---\ntype: person\n---\n# Peter\n", encoding="utf-8"
        )
    (vault / "Logs" / "Chats" / "chat-1").mkdir(parents=True)
    (vault / "Logs" / "Chats" / "chat-1" / "session.md").write_text("log\n", encoding="utf-8")
    (vault / "Templates").mkdir()
    (vault / "Templates" / "person.md").write_text("tpl\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "app.json").write_text("{}\n", encoding="utf-8")
    for generated in ("INDEX.md", "MEMORY.md", "VOCABULARY.md"):
        (vault / generated).write_text("generated\n", encoding="utf-8")
    (vault / ".DS_Store").write_bytes(b"\x00")
    return vault


def _tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_plan_classifies_every_path_and_does_not_refuse(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is False
    assert result.unclassified == []
    assert result.refusals == []

    moves = {(m.source, m.destination) for m in result.moves}
    assert moves == {
        ("memory-vault/.obsidian", ".obsidian"),
        ("memory-vault/Logs", "Logs"),
        ("memory-vault/Templates", "templates-src"),
        ("memory-vault/personal", "personal/memory-vault"),
        ("memory-vault/work", "work/memory-vault"),
    }
    # .obsidian is promoted rather than kept in place, which is what lets the
    # vault directory end up empty and be removed instead of lingering.
    assert result.global_keeps == []
    assert sorted(result.regenerated) == [
        "memory-vault/INDEX.md",
        "memory-vault/MEMORY.md",
        "memory-vault/VOCABULARY.md",
    ]
    assert result.ignored == ["memory-vault/.DS_Store"]


def test_plan_is_read_only(tmp_path: Path) -> None:
    """The load-bearing guarantee: planning cannot change the vault.

    Hashes every file before and after, and asserts no path appeared or vanished.
    """
    vault = _vault(tmp_path)
    before = _tree_hashes(vault)

    plan(tmp_path, vault, ["personal", "work"])

    after = _tree_hashes(vault)
    assert before == after


def test_every_path_lands_in_exactly_one_bucket(tmp_path: Path) -> None:
    """Conservation at the top level: no path is dropped and none is counted twice."""
    vault = _vault(tmp_path)
    result = plan(tmp_path, vault, ["personal", "work"])

    classified = (
        [m.source for m in result.moves]
        + result.global_keeps
        + result.regenerated
        + result.ignored
    )
    assert len(classified) == len(set(classified)), "a path was classified twice"

    on_disk = {f"memory-vault/{p.name}" for p in vault.iterdir()}
    assert set(classified) == on_disk


def test_an_unregistered_directory_refuses(tmp_path: Path) -> None:
    """A directory nobody registered has no destination, so the plan refuses.

    This is the gap the census exists to find: it reported .obsidian, Logs and
    Templates as unregistered, and each needed an explicit decision before the
    migration could be trusted.
    """
    vault = _vault(tmp_path)
    (vault / "research").mkdir()

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("research" in item for item in result.unclassified)


def test_an_unrecognised_loose_file_refuses(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "scratch.md").write_text("notes\n", encoding="utf-8")

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("scratch.md" in item for item in result.unclassified)


def test_a_symlink_refuses_rather_than_being_moved(tmp_path: Path) -> None:
    """A symlink can point outside the vault, so moving it is not conservative."""
    vault = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "linked").symlink_to(outside)

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("symlink" in item for item in result.unclassified)


def test_a_registered_workspace_with_no_vault_is_reported_not_guessed(tmp_path: Path) -> None:
    vault = _vault(tmp_path, workspaces=("personal",))

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("work" in r and "no vault directory" in r for r in result.refusals)


def test_a_non_empty_destination_refuses(tmp_path: Path) -> None:
    """Never write into a root that already has content."""
    vault = _vault(tmp_path)
    (tmp_path / "personal").mkdir()
    (tmp_path / "personal" / "leftover.md").write_text("x\n", encoding="utf-8")

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("already exists and is not empty" in r for r in result.refusals)


def test_no_registered_workspace_refuses(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    result = plan(tmp_path, vault, [])

    assert result.refused is True
    assert any("no registered workspace" in r for r in result.refusals)


def test_a_missing_vault_refuses(tmp_path: Path) -> None:
    result = plan(tmp_path, tmp_path / "absent", ["personal"])

    assert result.refused is True
    assert any("not a directory" in r for r in result.refusals)


def test_rehearse_never_records_migrated(tmp_path: Path) -> None:
    """A rehearsal must not make the real migration look already done.

    This is the D4 defect that bit the earlier migrations: read_receipt gated on
    the receipt existing, so a survey receipt permanently blocked the real run.
    """
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    payload = rehearse(tmp_path, vault, ["personal", "work"], runtime)

    assert payload["status"] == "surveyed"
    assert read_receipt(runtime) is None


def test_rehearse_records_a_refusal_as_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "research").mkdir()
    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    payload = rehearse(tmp_path, vault, ["personal", "work"], runtime)

    assert payload["status"] == "refused"
    assert read_receipt(runtime) is None


def test_read_receipt_only_accepts_migrated(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    write_receipt(runtime, {"status": "surveyed"})
    assert read_receipt(runtime) is None

    write_receipt(runtime, {"status": "migrated", "moves": []})
    assert read_receipt(runtime) is not None


def test_write_receipt_keeps_the_earlier_one(tmp_path: Path) -> None:
    """An earlier receipt holds the reverse map, so it is never overwritten away."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    write_receipt(runtime, {"status": "migrated", "marker": "first"})
    write_receipt(runtime, {"status": "migrated", "marker": "second"})

    archived = list((runtime / "migration").glob("workspace-rooting.*.json"))
    assert archived, "the earlier receipt was lost"
    kept = json.loads(archived[0].read_text(encoding="utf-8"))
    assert kept["marker"] == "first"


def test_a_survey_never_downgrades_a_migrated_receipt(tmp_path: Path) -> None:
    """The receipt is the only layout discriminator, so a survey cannot replace it.

    `write_receipt` rotated any earlier receipt aside and wrote unconditionally,
    so one `ciao workspace-reroot --rehearse` on a migrated install replaced
    `status: "migrated"` with `surveyed` — and `config._rerooted` reads exactly
    that, so every `agent_root()` fell back to the install root (no guide, no
    skills) and `undo` answered `nothing_to_undo`.
    """
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    write_receipt(runtime, {"status": "migrated", "marker": "real", "applied": []})

    write_receipt(runtime, {"status": "surveyed", "marker": "survey"})
    write_receipt(runtime, {"status": "refused", "marker": "refusal"})

    receipt = read_receipt(runtime)
    assert receipt is not None, "a survey un-migrated the install"
    assert receipt["marker"] == "real"
    # And nothing was rotated aside either: there was nothing to preserve.
    assert not list((runtime / "migration").glob("workspace-rooting.*.json"))


def test_rehearse_on_a_migrated_install_leaves_the_receipt_alone(tmp_path: Path) -> None:
    """The whole path, as the CLI runs it: `workspace-reroot --rehearse`."""
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    write_receipt(
        runtime,
        {"status": "migrated", "install_root": str(tmp_path), "applied": [], "marker": "real"},
    )

    payload = rehearse(tmp_path, vault, ["personal", "work"], runtime)

    assert payload["status"] == "surveyed"
    receipt = read_receipt(runtime)
    assert receipt is not None and receipt["marker"] == "real"


# -- apply / undo round trip -------------------------------------------------


def _git(root: Path, *args: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    return (proc.stdout + proc.stderr).strip()


def _git_install(tmp_path: Path, **kwargs) -> tuple[Path, Path, Path]:
    """A committed git install holding a real-shaped vault."""
    install = tmp_path / "install"
    install.mkdir()
    _git(install, "init", "-b", "main")
    _git(install, "config", "user.email", "test@example.com")
    _git(install, "config", "user.name", "Test")
    vault = _vault(install, **kwargs)
    runtime = install / ".runtime"
    runtime.mkdir()
    (install / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "seed")
    return install, vault, runtime


def test_apply_then_undo_restores_a_byte_identical_tree(tmp_path: Path) -> None:
    """The direct test that the automated part moves and never rewrites.

    Every tracked file is hashed before and after the round trip. One changed
    hash fails the release.
    """
    install, vault, runtime = _git_install(tmp_path)
    before = _tree_hashes(install / "memory-vault")

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert applied["status"] == "migrated", applied.get("refusals")
    assert not (install / "memory-vault").exists()
    assert (install / "personal" / "memory-vault" / "People" / "Peter.md").is_file()
    assert (install / "Logs" / "Chats" / "chat-1" / "session.md").is_file()
    assert (install / "templates-src" / "person.md").is_file()

    result = undo(install, runtime)
    assert result["status"] == "undone", result

    after = _tree_hashes(install / "memory-vault")
    assert after == before


def test_apply_preserves_history_so_git_log_follow_works(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)

    apply(install, vault, ["personal", "work"], runtime, primary="personal")
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "reroot")

    log = _git(install, "log", "--follow", "--oneline", "--", "personal/memory-vault/People/Peter.md")
    assert "seed" in log, "history did not follow the move"


def test_apply_refuses_on_modified_tracked_files(tmp_path: Path) -> None:
    """A tracked modification blocks, because git checkout must stay a valid undo."""
    install, vault, runtime = _git_install(tmp_path)
    (vault / "personal" / "People" / "Peter.md").write_text("edited\n", encoding="utf-8")

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert applied["status"] == "refused"
    assert any("uncommitted" in r for r in applied["refusals"])
    assert (install / "memory-vault").is_dir(), "it moved something despite refusing"
    assert read_receipt(runtime) is None


def test_a_failure_after_the_moves_rolls_back_and_records_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-move failure must not leave the install half-rooted AND silent.

    Stashing the regenerated notes happens after every ``git mv`` and writes into
    the runtime root, which ``CIAO_RUNTIME_ROOT`` may put on another filesystem -
    where the move raised ``EXDEV``. Nothing recorded that: ``payload["applied"]``
    is only set once the whole stretch succeeds, so no receipt was written, undo
    had nothing to reverse, and startup swallowed the exception and carried on
    with a config still naming paths that had already moved.
    """
    install, vault, runtime = _git_install(tmp_path)

    def cross_device(src: str, dst: str) -> str:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(workspace_reroot.shutil, "move", cross_device)

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert applied["status"] == "refused"
    assert any("after moving files" in r for r in applied["refusals"])
    # Every git mv is reversed, so the tree is exactly as it was.
    assert (install / "memory-vault" / "personal").is_dir()
    assert not (install / "personal" / "memory-vault").exists()
    # And the refusal is on disk. read_receipt still says "not migrated", which
    # is what lets the next run retry rather than being blocked by a failure.
    record = json.loads(receipt_path(runtime).read_text(encoding="utf-8"))
    assert record["status"] == "refused"
    assert read_receipt(runtime) is None


def test_apply_does_not_refuse_on_untracked_files(tmp_path: Path) -> None:
    """Untracked files must NOT block.

    The reference install carries roughly 700 untracked Logs/Chats/chat-* dirs at
    any time, so a strict gate would refuse on every real install and nothing
    would ever migrate.
    """
    install, vault, runtime = _git_install(tmp_path)
    fresh = vault / "Logs" / "Chats" / "chat-untracked"
    fresh.mkdir(parents=True)
    (fresh / "session.md").write_text("new\n", encoding="utf-8")

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert applied["status"] == "migrated", applied.get("refusals")
    assert (install / "Logs" / "Chats" / "chat-untracked" / "session.md").is_file()


def test_apply_is_all_or_nothing_when_a_workspace_has_no_vault(tmp_path: Path) -> None:
    """Every registered workspace re-roots or none does."""
    install, vault, runtime = _git_install(tmp_path, workspaces=("personal",))

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert applied["status"] == "refused"
    assert (install / "memory-vault" / "personal").is_dir()
    assert not (install / "personal").exists()


def test_a_migrated_receipt_records_the_reverse_map(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)

    apply(install, vault, ["personal", "work"], runtime, primary="personal")
    receipt = read_receipt(runtime)

    assert receipt is not None
    pairs = {(e["source"], e["destination"]) for e in receipt["applied"]}
    assert ("memory-vault/personal", "personal/memory-vault") in pairs
    assert receipt["git_head_before"]


def test_undo_with_no_migrated_receipt_does_nothing(tmp_path: Path) -> None:
    install, _vault_root, runtime = _git_install(tmp_path)

    assert undo(install, runtime)["status"] == "nothing_to_undo"
    assert (install / "memory-vault").is_dir()


def test_dirty_tracked_paths_reports_the_first_path_intact(tmp_path: Path) -> None:
    """The first porcelain line must not lose a character.

    `git status --porcelain` puts the index and worktree status in columns 1 and
    2, so a modified-in-worktree line begins with a space. Stripping the combined
    output before splitting ate that space and truncated the first path to
    "emory-vault/...", which would have made a refusal message name a file that
    does not exist.
    """
    install, vault, runtime = _git_install(tmp_path)
    (vault / "VOCABULARY.md").write_text("edited\n", encoding="utf-8")
    (vault / "INDEX.md").write_text("edited\n", encoding="utf-8")

    dirty = dirty_tracked_paths(install, "memory-vault")

    assert dirty, "the gate saw nothing"
    for path in dirty:
        assert path.startswith("memory-vault/"), f"truncated path: {path!r}"
        assert (install / path).exists(), f"reported a path that does not exist: {path!r}"



# -- P10.4: the guide split -------------------------------------------------


_GUIDE = """# Ciaobot

Standing directive: always check first, then claim.

<!-- ciao:memory:start cap=2200 -->
## Agent memory

A fact about the engine.
§
A second fact
that spans two lines.
<!-- ciao:memory:end -->

<!-- ciao:profile:start cap=1375 -->
## User profile

Raffa prefers direct implementation.
<!-- ciao:profile:end -->
"""


def test_primary_root_keeps_the_guide_verbatim() -> None:
    split = split_guide(_GUIDE, ["personal", "work"], "personal")

    assert split.per_root["personal"] == _GUIDE
    assert split.queued.get("personal") is None


def test_every_root_gets_the_unbounded_body_verbatim() -> None:
    """Standing directives apply everywhere, so the body is copied, not split."""
    split = split_guide(_GUIDE, ["personal", "work"], "personal")

    assert "Standing directive: always check first" in split.per_root["work"]
    assert "# Ciaobot" in split.per_root["work"]


def test_a_secondary_root_gets_empty_regions() -> None:
    """No heuristic classification: the regions arrive empty, not guessed at."""
    split = split_guide(_GUIDE, ["personal", "work"], "personal")
    work = split.per_root["work"]

    assert "ciao:memory:start" in work and "ciao:memory:end" in work
    assert "ciao:profile:start" in work and "ciao:profile:end" in work
    assert "A fact about the engine" not in work
    assert "Raffa prefers direct implementation" not in work


def test_the_primary_regions_are_queued_for_every_other_root() -> None:
    """Nothing is lost: a human accepts what belongs in that workspace."""
    split = split_guide(_GUIDE, ["personal", "work"], "personal")
    queued = split.queued["work"]

    texts = " ".join(queued)
    assert "A fact about the engine" in texts
    assert "Raffa prefers direct implementation" in texts
    assert any(b.startswith("- [memory]") for b in queued)
    assert any(b.startswith("- [profile]") for b in queued)


def test_queued_bullets_hold_the_one_bullet_per_line_invariant() -> None:
    """The queue is parsed line by line, so a multi-line bullet corrupts it.

    proposal_kinds.BULLET_RE matches per line, so a bullet spanning lines leaves
    its continuation as loose prose in Memory-Proposals.md: uncountable by every
    counter and invisible to the dedupe check. The real guide contains exactly
    such a multi-line entry, which is how this was found.
    """
    from ciao import proposal_kinds

    split = split_guide(_GUIDE, ["personal", "work"], "personal")

    for bullet in split.queued["work"]:
        assert "\n" not in bullet, f"multi-line bullet: {bullet!r}"
        assert proposal_kinds.parse_bullet(bullet) is not None, f"unparseable: {bullet!r}"


def test_a_region_heading_is_not_queued_as_a_fact() -> None:
    """`## Agent memory` is region scaffolding, not something to remember."""
    from ciao import proposal_kinds

    split = split_guide(_GUIDE, ["personal", "work"], "personal")

    for bullet in split.queued["work"]:
        parsed = proposal_kinds.parse_bullet(bullet)
        assert parsed is not None
        assert not parsed.text.lstrip().startswith("#")
        assert "Agent memory" not in parsed.text
        assert "User profile" not in parsed.text


def test_a_single_workspace_install_queues_nothing() -> None:
    split = split_guide(_GUIDE, ["personal"], "personal")

    assert split.per_root == {"personal": _GUIDE}
    assert split.queued == {}


# -- registry and the agent_root flip ---------------------------------------


def _registry(runtime: Path, entries: list[dict]) -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "workspaces.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")


def test_apply_repoints_the_registry_at_the_new_roots(tmp_path: Path) -> None:
    """Without this the install is broken: the registry names a path that is gone."""
    install, vault, runtime = _git_install(tmp_path)
    _registry(runtime, [
        {"name": "personal", "vault_root": "memory-vault/personal", "future_key": 1},
        {"name": "work", "vault_root": "memory-vault/work"},
    ])

    apply(install, vault, ["personal", "work"], runtime, primary="personal")

    after = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    roots = {e["name"]: e["vault_root"] for e in after}
    assert roots == {"personal": "personal/memory-vault", "work": "work/memory-vault"}
    # An unknown key a future release adds must survive a migration that only
    # means to change one field.
    assert after[0]["future_key"] == 1


def test_undo_restores_the_previous_registry(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)
    original = [
        {"name": "personal", "vault_root": "memory-vault/personal"},
        {"name": "work", "vault_root": "memory-vault/work"},
    ]
    _registry(runtime, original)

    apply(install, vault, ["personal", "work"], runtime, primary="personal")
    undo(install, runtime)

    after = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    assert after == original


def test_agent_root_flips_only_after_a_migrated_receipt(tmp_path: Path) -> None:
    """The flip is per install and atomic, gated on the receipt rather than a date.

    Returning a real subdirectory before the files have moved, or keeping the old
    answer after they have, is the half-rooted state the release must not allow.
    """
    from ciao.config import CiaoConfig, reset_reroot_cache

    install, vault, runtime = _git_install(tmp_path)
    _registry(runtime, [
        {"name": "personal", "vault_root": "memory-vault/personal"},
        {"name": "work", "vault_root": "memory-vault/work"},
    ])
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=install,
        vault_root=vault,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )

    reset_reroot_cache()
    assert config.agent_root("work") == install, "flipped before migrating"

    apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert config.agent_root("work") == install / "work"

    undo(install, runtime)
    assert config.agent_root("work") == install, "stayed flipped after undo"


# -- P10.5: skills triage ----------------------------------------------------
#
# The reference install's catalog has a shape no fixture had before: a directory
# with NO SKILL.md (its only content is an ignored __pycache__), which the
# Settings inventory cannot see because it globs `*/SKILL.md`. It still moves, so
# the triage sheet has to account for it or the sheet is an incomplete record of
# what the migration did.


def _catalog(install: Path) -> None:
    """A skill catalog shaped like the real one."""
    skills = install / "skills"
    (skills / "jira-tickets").mkdir(parents=True)
    (skills / "jira-tickets" / "SKILL.md").write_text(
        "---\nname: jira-tickets\ndescription: |\n  File a Jira ticket.\n  Work only.\n---\n# Jira\n",
        encoding="utf-8",
    )
    (skills / "linkedin-writing").mkdir()
    (skills / "linkedin-writing" / "SKILL.md").write_text(
        "---\nname: linkedin-writing\ndescription: Draft a post | with a pipe\n---\n# LinkedIn\n",
        encoding="utf-8",
    )
    # The husk: no SKILL.md, so no root ever loads it, but it is still on disk.
    (skills / "adversarial-review" / "scripts").mkdir(parents=True)
    (skills / "adversarial-review" / "scripts" / "run.py").write_text("x\n", encoding="utf-8")
    (skills / "README.md").write_text("catalog\n", encoding="utf-8")
    (install / "skills-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "defuddle": {
                        "source": "kepano/obsidian-skills",
                        "sourceType": "github",
                        "skillPath": "skills/defuddle/SKILL.md",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _install_hashes(install: Path) -> dict[str, str]:
    """Every tracked-or-not file in the install except git and runtime state."""
    out: dict[str, str] = {}
    for path in sorted(install.rglob("*")):
        relative = path.relative_to(install)
        if relative.parts and relative.parts[0] in {".git", ".runtime"}:
            continue
        if path.is_file():
            out[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_triage_lists_every_catalog_directory_including_one_without_a_skill_md(
    tmp_path: Path,
) -> None:
    install, _vault_root, _runtime = _git_install(tmp_path)
    _catalog(install)

    triage = plan_skills_triage(install, "personal")

    names = [entry.name for entry in triage.entries]
    assert names == ["adversarial-review", "jira-tickets", "linkedin-writing", "defuddle"]
    husk = next(entry for entry in triage.entries if entry.name == "adversarial-review")
    assert husk.note, "a directory with no SKILL.md must be reported, not silently moved"
    described = next(entry for entry in triage.entries if entry.name == "jira-tickets")
    assert "File a Jira ticket" in described.description
    assert triage.refusals == []


def test_triage_planning_is_read_only(tmp_path: Path) -> None:
    install, _vault_root, _runtime = _git_install(tmp_path)
    _catalog(install)
    before = _install_hashes(install)

    plan_skills_triage(install, "personal")

    assert _install_hashes(install) == before


def test_triage_refuses_when_the_primary_already_holds_a_catalog(tmp_path: Path) -> None:
    install, _vault_root, _runtime = _git_install(tmp_path)
    _catalog(install)
    (install / "personal" / "skills").mkdir(parents=True)

    triage = plan_skills_triage(install, "personal")

    assert any("skills" in reason for reason in triage.refusals)


def test_triage_refuses_without_a_primary(tmp_path: Path) -> None:
    install, _vault_root, _runtime = _git_install(tmp_path)
    _catalog(install)

    assert plan_skills_triage(install, "").refusals


def test_no_catalog_at_all_is_not_a_refusal(tmp_path: Path) -> None:
    install, _vault_root, _runtime = _git_install(tmp_path)

    triage = plan_skills_triage(install, "personal")

    assert triage.refusals == []
    assert triage.moves == []
    assert triage.entries == []


def test_apply_moves_the_catalog_to_the_primary_and_copies_it_nowhere(
    tmp_path: Path,
) -> None:
    """The whole point of triage: exactly one root gets the catalog."""
    install, vault, runtime = _git_install(tmp_path)
    _catalog(install)
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "catalog")

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["status"] == "migrated", result.get("refusals")
    assert (install / "personal" / "skills" / "jira-tickets" / "SKILL.md").is_file()
    assert (install / "personal" / "skills-lock.json").is_file()
    assert not (install / "skills").exists()
    assert not (install / "work" / "skills").exists()
    assert not (install / "work" / "skills-lock.json").exists()
    # The husk's untracked content comes along, because the move is a rename.
    assert (install / "personal" / "skills" / "adversarial-review" / "scripts" / "run.py").is_file()


def test_apply_writes_a_triage_sheet_with_every_destination_blank(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)
    _catalog(install)
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "catalog")

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    sheet = install / "personal" / "memory-vault" / "Workspace" / "Skill-Triage.md"
    assert sheet.is_file()
    assert str(sheet.relative_to(install)) in result["created_files"]
    text = sheet.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    assert len(rows) == 4, rows
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert cells[2] == "", f"destination must be blank, got {cells[2]!r}"


def test_the_sheet_accounts_for_every_directory_that_moved(tmp_path: Path) -> None:
    """Conservation, keyed on the directories on disk rather than on the inventory."""
    install, vault, runtime = _git_install(tmp_path)
    _catalog(install)
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "catalog")

    apply(install, vault, ["personal", "work"], runtime, primary="personal")

    moved = {
        path.name
        for path in (install / "personal" / "skills").iterdir()
        if path.is_dir()
    }
    text = (install / "personal" / "memory-vault" / "Workspace" / "Skill-Triage.md").read_text(
        encoding="utf-8"
    )
    for name in moved:
        assert f"| `{name}` |" in text, f"{name} moved but is missing from the sheet"


def test_a_pipe_in_a_description_does_not_shred_the_table(tmp_path: Path) -> None:
    install, _vault_root, _runtime = _git_install(tmp_path)
    _catalog(install)

    text = format_skill_triage(plan_skills_triage(install, "personal"), ["personal", "work"])

    row = next(line for line in text.splitlines() if line.startswith("| `linkedin-writing`"))
    # Escaped pipes are still `|` characters, so count the separators only.
    assert row.replace("\\|", "").count("|") == 5, row
    assert "\\|" in row
    assert "with a pipe" in row


def test_apply_creates_skills_src_as_a_source_not_a_workspace(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)
    _catalog(install)
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "catalog")

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    source_dir = install / "skills-src"
    assert (source_dir / "README.md").is_file()
    assert "skills-src/README.md" in result["created_files"]
    assert not (source_dir / "memory-vault").exists()
    assert not (source_dir / "CLAUDE.md").exists()


def test_apply_refuses_when_the_primary_is_not_registered(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)

    result = apply(install, vault, ["personal", "work"], runtime, primary="archive")

    assert result["status"] == "refused"
    assert any("archive" in reason for reason in result["refusals"])
    assert (install / "memory-vault").is_dir(), "a refusal must move nothing"


def test_apply_refuses_on_a_dirty_tracked_skill(tmp_path: Path) -> None:
    """The clean-tree gate covers everything that moves, not only the vault."""
    install, vault, runtime = _git_install(tmp_path)
    _catalog(install)
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "catalog")
    (install / "skills" / "jira-tickets" / "SKILL.md").write_text("edited\n", encoding="utf-8")

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["status"] == "refused"
    assert result["dirty_tracked"] == ["skills/jira-tickets/SKILL.md"]
    # The message names the file, not the list of watched roots.
    assert "skills/jira-tickets/SKILL.md" in result["refusals"][0]
    assert (install / "skills").is_dir()


def test_apply_then_undo_restores_the_whole_install_byte_identical(tmp_path: Path) -> None:
    """Round trip over the whole install, not just the vault.

    The vault-only version of this test cannot see a leftover skills-src/, a
    stranded triage sheet, or a catalog that did not come back.
    """
    install, vault, runtime = _git_install(tmp_path)
    _catalog(install)
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "catalog")
    before = _install_hashes(install)

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert applied["status"] == "migrated", applied.get("refusals")

    result = undo(install, runtime)
    assert result["status"] == "undone", result

    assert _install_hashes(install) == before
    assert not (install / "skills-src").exists()
    assert not (install / "personal").exists()
    assert not (install / "memory-vault" / "personal" / "Workspace").exists()


# -- P10.11: --repair -------------------------------------------------------


def _migrated(tmp_path: Path, **kwargs) -> tuple[Path, Path]:
    """A committed install that has completed the re-rooting."""
    install, vault, runtime = _git_install(tmp_path, **kwargs)
    _registry(
        runtime,
        [
            {"name": "personal", "vault_root": "memory-vault/personal"},
            {"name": "work", "vault_root": "memory-vault/work"},
        ],
    )
    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert result["status"] == "migrated", result.get("refusals")
    return install, runtime


def _repair(install: Path, runtime: Path, **kwargs) -> dict:
    from ciao.workspace_reroot import repair

    return repair(install, runtime, ["personal", "work"], **kwargs)


def _drifts(result: dict, key: str = "repaired") -> set[str]:
    return {item["drift"] for item in result[key]}


def test_repair_refuses_on_an_install_that_has_not_re_rooted(tmp_path: Path) -> None:
    """Not caution: a prefixed shared INDEX.md is CORRECT before the migration.

    The prefix is what `_entity_visible_in_workspace` filters on, so stripping it
    there would leave no filter over the index and make every entity visible in
    every session — the same fail-open state the deletions are gated on, reached
    from the other direction.
    """
    install, _vault, runtime = _git_install(tmp_path)
    before = _install_hashes(install)

    result = _repair(install, runtime)

    assert result["status"] == "not_rerooted"
    assert "--apply" in result["reason"]
    assert _install_hashes(install) == before


def test_repair_is_a_no_op_on_a_correct_install(tmp_path: Path) -> None:
    install, runtime = _migrated(tmp_path)
    rebuild_indexes(install, ["personal", "work"])
    _repair(install, runtime)  # settle the first-run asset install

    result = _repair(install, runtime)

    assert result["status"] == "clean", result["repaired"]
    assert result["repaired"] == []


def test_repair_recreates_a_missing_root(tmp_path: Path) -> None:
    import shutil

    install, runtime = _migrated(tmp_path)
    shutil.rmtree(install / "work")

    result = _repair(install, runtime)

    assert "root_missing" in _drifts(result)
    assert (install / "work").is_dir()
    assert (install / "work" / "CLAUDE.md").is_file()
    assert (install / "work" / "AGENTS.md").is_symlink()


def test_a_root_with_no_vault_is_reported_and_never_invented(tmp_path: Path) -> None:
    """Which notes belong to a workspace is a question about the user's material."""
    import shutil

    install, runtime = _migrated(tmp_path)
    shutil.rmtree(install / "work" / "memory-vault")

    result = _repair(install, runtime)

    assert "vault_missing" in _drifts(result, "reported")
    assert "vault_missing" not in _drifts(result)
    assert not (install / "work" / "memory-vault").exists()


def test_repair_relinks_an_unlinked_agents_guide(tmp_path: Path) -> None:
    install, runtime = _migrated(tmp_path)
    _repair(install, runtime)
    agents = install / "work" / "AGENTS.md"
    agents.unlink()
    agents.write_text("a hand-written copy\n", encoding="utf-8")

    result = _repair(install, runtime)

    assert "agents_unlinked" in _drifts(result)


def test_repair_leaves_a_user_authored_agents_file_alone(tmp_path: Path) -> None:
    """`_ensure_linked_workspace_guides` only replaces a missing file or the
    packaged stock copy, and repair must not reach past that."""
    install, runtime = _migrated(tmp_path)
    _repair(install, runtime)
    agents = install / "work" / "AGENTS.md"
    agents.unlink()
    agents.write_text("# my own AGENTS.md\n", encoding="utf-8")

    _repair(install, runtime)

    assert agents.read_text(encoding="utf-8") == "# my own AGENTS.md\n"


def test_repair_rebuilds_an_index_that_still_carries_workspace_prefixes(
    tmp_path: Path,
) -> None:
    install, runtime = _migrated(tmp_path)
    _repair(install, runtime)
    index = install / "personal" / "memory-vault" / "INDEX.md"
    index.write_text(
        "# Vault Index\n\n- [personal/People/Peter](./personal/People/Peter.md)\n",
        encoding="utf-8",
    )

    result = _repair(install, runtime)

    assert "index_prefixed" in _drifts(result)
    assert index_workspace_prefixes(index, ["personal", "work"]) == []


def test_repair_writes_a_missing_index(tmp_path: Path) -> None:
    install, runtime = _migrated(tmp_path)
    _repair(install, runtime)
    (install / "work" / "memory-vault" / "INDEX.md").unlink()

    result = _repair(install, runtime)

    assert "index_missing" in _drifts(result)
    assert (install / "work" / "memory-vault" / "INDEX.md").is_file()


def test_a_shared_source_skill_is_linked_into_every_root(tmp_path: Path) -> None:
    """`skills-src/` is a mirror source: one edit, N roots, no divergence."""
    install, runtime = _migrated(tmp_path)
    shared = install / "skills-src" / "web-research"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text(
        "---\nname: web-research\ndescription: look things up\n---\n# Research\n",
        encoding="utf-8",
    )

    result = _repair(install, runtime)

    assert "skills_unmirrored" in _drifts(result)
    for name in ("personal", "work"):
        link = install / name / ".claude" / "skills" / "web-research"
        assert link.is_symlink(), name
        assert link.resolve() == shared.resolve()


def test_a_roots_own_copy_of_a_shared_skill_wins(tmp_path: Path) -> None:
    install, runtime = _migrated(tmp_path)
    shared = install / "skills-src" / "web-research"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("---\nname: web-research\n---\nshared\n", encoding="utf-8")
    own = install / "work" / "skills" / "web-research"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: web-research\n---\nmine\n", encoding="utf-8")

    _repair(install, runtime)

    link = install / "work" / ".claude" / "skills" / "web-research"
    assert link.resolve() == own.resolve()
    assert (install / "personal" / ".claude" / "skills" / "web-research").resolve() == shared.resolve()


def test_a_missing_mcp_file_is_reported_and_never_composed(tmp_path: Path) -> None:
    """An MCP entry grants credentialed access. The earlier attempt at inferring
    reachability silently removed two working integrations from a live install."""
    install, runtime = _migrated(tmp_path)
    (install / "skills-src").mkdir(exist_ok=True)
    (install / "skills-src" / ".mcp.json").write_text(
        '{"mcpServers": {"notion": {}}}\n', encoding="utf-8"
    )

    result = _repair(install, runtime)

    assert "mcp_drift" in _drifts(result, "reported")
    assert not (install / "work" / ".mcp.json").exists()


def test_repair_rebuilds_a_search_index_pointing_at_moved_paths(tmp_path: Path) -> None:
    import sqlite3

    from ciao.fts_search import init_db

    install, runtime = _migrated(tmp_path)
    _repair(install, runtime)
    db = runtime / "test-fts.db"
    conn = sqlite3.connect(db)
    init_db(conn)
    conn.execute(
        "INSERT INTO vault_meta (path, mtime, indexed_at) VALUES "
        "('memory-vault/personal/People/Peter.md', 1.0, 'x')"
    )
    conn.commit()
    conn.close()

    result = _repair(install, runtime, db_path=db)

    assert "search_index_stale" in _drifts(result)
    conn = sqlite3.connect(db)
    paths = {row[0] for row in conn.execute("SELECT path FROM vault_meta")}
    conn.close()
    assert "memory-vault/personal/People/Peter.md" not in paths
    assert any(p.startswith("personal/memory-vault/") for p in paths), paths


def test_repair_is_idempotent(tmp_path: Path) -> None:
    """Everything it fixes must be safe to run twice, or a tile's run button is
    not safe to press without reading anything first."""
    import shutil

    install, runtime = _migrated(tmp_path)
    shutil.rmtree(install / "work")
    (install / "personal" / "memory-vault" / "INDEX.md").unlink(missing_ok=True)

    first = _repair(install, runtime)
    second = _repair(install, runtime)
    third = _repair(install, runtime)

    assert first["status"] == "repaired"
    assert second["repaired"] == [], second["repaired"]
    assert third["status"] == "clean"


def test_index_prefix_detection_only_matches_registered_names(tmp_path: Path) -> None:
    """A note genuinely filed under a folder called `Projects/` is not drift."""
    index = tmp_path / "INDEX.md"
    index.write_text(
        "# Vault Index\n\n"
        "- [personal/People/Peter](./personal/People/Peter.md)\n"
        "- [Projects/active/thing](./Projects/active/thing.md)\n"
        "- [People/Peter](./People/Peter.md)\n",
        encoding="utf-8",
    )

    assert index_workspace_prefixes(index, ["personal", "work"]) == ["personal/People/Peter"]


def _unsplit(install: Path) -> None:
    """The state an install migrated by an EARLIER build is left in.

    `apply` now splits the guide, so a fresh migration cannot reach this. An
    install migrated before that landed can: roots with no guide, and the
    pre-migration one still at the install root.
    """
    for name in ("personal", "work"):
        (install / name / "CLAUDE.md").unlink(missing_ok=True)
        (install / name / "AGENTS.md").unlink(missing_ok=True)
    (install / "CLAUDE.md").write_text(
        "# The real guide\n\n<!-- ciao:memory:start -->\n- a fact\n"
        "<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )


def test_repair_refuses_to_seed_a_stock_guide_over_an_unsplit_one(tmp_path: Path) -> None:
    """The gap this guard exists for was measured, not imagined.

    On the reference clone, `--apply` then `--repair` left each root holding the
    2202-byte packaged stock guide with EMPTY memory regions, while the
    operator's real 27377-byte CLAUDE.md sat at the install root, which no
    session's cwd reads any more. Every remembered fact would have vanished from
    every session, and the repair would have reported success.
    """
    install, runtime = _migrated(tmp_path)
    _unsplit(install)

    result = _repair(install, runtime)

    assert "guide_unsplit" in _drifts(result, "reported")
    assert not (install / "personal" / "CLAUDE.md").exists()
    assert not (install / "work" / "CLAUDE.md").exists()


def test_repair_still_relinks_once_a_root_has_its_own_guide(tmp_path: Path) -> None:
    """The guard must not disable the repair it guards."""
    install, runtime = _migrated(tmp_path)
    _unsplit(install)
    (install / "work" / "CLAUDE.md").write_text("# work's own guide\n", encoding="utf-8")

    result = _repair(install, runtime)

    assert "guide_unsplit" in _drifts(result, "reported")  # personal still lacks one
    assert (install / "work" / "AGENTS.md").is_symlink()


# -- P10.4 applied: apply() gives every root its own guide -------------------
#
# `split_guide` had seven tests and no caller. Measured on the reference clone
# before this landed: after `--apply` plus `--repair`, each root held the
# 2202-byte packaged stock guide with EMPTY memory regions while the operator's
# real 27377-byte guide sat orphaned at `<install>/CLAUDE.md` — a path no
# session's cwd reads any more. All 20 remembered facts, gone, silently.

_REAL_GUIDE = """# Ciaobot

Standing directives that apply everywhere.

<!-- ciao:memory:start -->
## Agent memory

- the deploy script needs a tag
§
- releases go out on Thursdays
<!-- ciao:memory:end -->

<!-- ciao:profile:start -->
- prefers terse replies
<!-- ciao:profile:end -->
"""


def _with_guide(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A committed install holding a real shared guide and a queue per workspace."""
    install, vault, runtime = _git_install(tmp_path)
    (install / "CLAUDE.md").write_text(_REAL_GUIDE, encoding="utf-8")
    (install / "AGENTS.md").symlink_to("CLAUDE.md")
    for name in ("personal", "work"):
        queue = vault / name / "Workspace" / "Memory-Proposals.md"
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text(
            "---\ntags: [ciao, memory, proposals]\n---\n# Memory Proposals\n",
            encoding="utf-8",
        )
    _registry(
        runtime,
        [
            {"name": "personal", "vault_root": "memory-vault/personal"},
            {"name": "work", "vault_root": "memory-vault/work"},
        ],
    )
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "guide")
    return install, vault, runtime


def test_the_primary_inherits_the_real_guide_and_the_install_root_keeps_none(
    tmp_path: Path,
) -> None:
    """The shared guide MOVES, so history follows and nothing is left to leak.

    A surviving `<install>/CLAUDE.md` would be re-read as a parent guide from
    every root's cwd, re-injecting the primary's memory regions everywhere and
    undoing the split that had just run.
    """
    install, vault, runtime = _with_guide(tmp_path)

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["status"] == "migrated", result.get("refusals")
    assert not (install / "CLAUDE.md").exists()
    assert not (install / "AGENTS.md").exists()
    assert (install / "personal" / "CLAUDE.md").read_text(encoding="utf-8") == _REAL_GUIDE
    assert (install / "personal" / "AGENTS.md").is_symlink()
    # `git mv` stages the rename; history follows only once it is committed.
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "reroot")
    history = _git(install, "log", "--follow", "--oneline", "--", "personal/CLAUDE.md")
    assert "guide" in history, history


def test_a_secondary_root_gets_the_body_and_empty_regions(tmp_path: Path) -> None:
    install, vault, runtime = _with_guide(tmp_path)

    apply(install, vault, ["personal", "work"], runtime, primary="personal")

    text = (install / "work" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Standing directives that apply everywhere." in text
    assert "releases go out on Thursdays" not in text
    assert "prefers terse replies" not in text
    assert "<!-- ciao:memory:start" in text  # the marker carries a cap= attribute


def test_the_primary_regions_reach_every_other_roots_queue(tmp_path: Path) -> None:
    """Nothing is lost and nothing is guessed: the entries land where a human
    decides on them, one parseable bullet per line."""
    from ciao.proposal_kinds import BULLET_RE

    install, vault, runtime = _with_guide(tmp_path)

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["guide_split"]["queued"] == {"work": 3}
    queue = (install / "work" / "memory-vault" / "Workspace" / "Memory-Proposals.md")
    lines = [line for line in queue.read_text(encoding="utf-8").splitlines() if line.startswith("- [")]
    assert len(lines) == 3
    assert all(BULLET_RE.match(line) for line in lines), lines
    assert any("releases go out on Thursdays" in line for line in lines)
    # The region heading is scaffolding, not a remembered fact.
    assert not any("Agent memory" in line for line in lines)
    # The primary already holds these in its own regions, so it queues nothing.
    primary_queue = install / "personal" / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    assert not [
        line for line in primary_queue.read_text(encoding="utf-8").splitlines()
        if line.startswith("- [")
    ]


def test_every_root_is_usable_after_apply(tmp_path: Path) -> None:
    """Before this, apply left roots holding a vault and nothing else, so a
    session in one discovered no skill, agent or command at all."""
    install, vault, runtime = _with_guide(tmp_path)

    apply(install, vault, ["personal", "work"], runtime, primary="personal")

    for name in ("personal", "work"):
        root = install / name
        assert (root / "CLAUDE.md").is_file(), name
        assert (root / "AGENTS.md").is_symlink(), name
        assert (root / ".claude" / "skills").is_dir(), name
        assert any((root / ".claude" / "skills").iterdir()), name


def test_repair_is_a_no_op_immediately_after_apply(tmp_path: Path) -> None:
    """The invariant that makes the bootstrap trustworthy: both go through the
    same code, so a freshly migrated install has nothing left to reconcile."""
    install, vault, runtime = _with_guide(tmp_path)
    apply(install, vault, ["personal", "work"], runtime, primary="personal")
    rebuild_indexes(install, ["personal", "work"])

    result = _repair(install, runtime)

    assert result["status"] == "clean", result["repaired"]
    assert result["reported"] == [], result["reported"]


def test_apply_then_undo_restores_an_install_that_had_a_real_guide(
    tmp_path: Path,
) -> None:
    """The round trip has to cover what the split and the bootstrap wrote:
    per-root guides, the AGENTS.md links, `.claude/`, and the appended queue."""
    install, vault, runtime = _with_guide(tmp_path)
    before = _install_hashes(install)

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert applied["status"] == "migrated", applied.get("refusals")

    result = undo(install, runtime)
    assert result["status"] == "undone", result

    assert _install_hashes(install) == before
    assert not (install / "personal").exists()
    assert not (install / "work").exists()
    assert (install / "CLAUDE.md").read_text(encoding="utf-8") == _REAL_GUIDE


def test_a_dirty_guide_refuses_before_anything_moves(tmp_path: Path) -> None:
    """The guide moves, so the clean-tree gate has to cover it too."""
    install, vault, runtime = _with_guide(tmp_path)
    (install / "CLAUDE.md").write_text(_REAL_GUIDE + "\n- edited\n", encoding="utf-8")

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["status"] == "refused"
    assert result["dirty_tracked"] == ["CLAUDE.md"]
    assert (install / "memory-vault").is_dir()


# -- P10.6, session half -----------------------------------------------------
#
# Every live chat's provider session is keyed by the cwd it started in, and the
# migration changes that cwd. On the reference install that is 8 open chats, all
# holding a session, none flagged — so before this, running the migration made
# all 8 silently forget on their next turn.


def _chat_store(runtime: Path, chats: dict) -> Path:
    path = runtime / "web_projects.json"
    path.write_text(
        json.dumps({"version": 1, "revision": 3, "projects": {}, "chats": chats}, indent=2),
        encoding="utf-8",
    )
    return path


def _stored(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["chats"]


def test_apply_flags_every_open_chat_that_holds_a_session(tmp_path: Path) -> None:
    from ciao.workspace_reroot import flag_stranded_sessions

    install, vault, runtime = _git_install(tmp_path)
    store = _chat_store(
        runtime,
        {
            "chat-live": {"session_id": "s1", "archived": False, "mode": "auto"},
            "chat-archived": {"session_id": "s2", "archived": True},
            "chat-fresh": {"session_id": "", "archived": False},
            "chat-already": {
                "session_id": "s3",
                "archived": False,
                "handover_context_pending": True,
            },
        },
    )

    result = flag_stranded_sessions(runtime)

    assert result["flagged"] == ["chat-live"]
    chats = _stored(store)
    assert chats["chat-live"]["handover_context_pending"] is True
    # An archived chat has no live session; one that never started a provider
    # session has nothing to hand over; one already pending belongs to whatever
    # set it.
    assert "handover_context_pending" not in chats["chat-archived"]
    assert "handover_context_pending" not in chats["chat-fresh"]
    assert chats["chat-already"]["handover_context_pending"] is True
    # An unknown key a future release adds must survive.
    assert chats["chat-live"]["mode"] == "auto"


def test_apply_reports_the_stranded_session_count(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)
    _chat_store(runtime, {"chat-live": {"session_id": "s1", "archived": False}})

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["stranded_sessions"]["flagged"] == ["chat-live"]


def test_undo_clears_only_the_flags_this_migration_set(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)
    store = _chat_store(
        runtime,
        {
            "chat-live": {"session_id": "s1", "archived": False},
            "chat-already": {
                "session_id": "s2",
                "archived": False,
                "handover_context_pending": True,
            },
        },
    )
    apply(install, vault, ["personal", "work"], runtime, primary="personal")

    result = undo(install, runtime)

    assert result["cleared_handover_flags"] == 1
    chats = _stored(store)
    assert chats["chat-live"]["handover_context_pending"] is False
    assert chats["chat-already"]["handover_context_pending"] is True


def test_a_missing_or_unreadable_chat_store_does_not_fail_the_migration(
    tmp_path: Path,
) -> None:
    """A migration must not refuse because a derived state file is unreadable."""
    from ciao.workspace_reroot import flag_stranded_sessions

    install, vault, runtime = _git_install(tmp_path)
    assert flag_stranded_sessions(runtime)["flagged"] == []
    (runtime / "web_projects.json").write_text("{not json", encoding="utf-8")

    assert flag_stranded_sessions(runtime)["flagged"] == []
    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert result["status"] == "migrated", result.get("refusals")


# -- CLI: an explicit --workspace must win over the environment --------------


def test_reroot_cli_resolves_the_vault_under_the_named_workspace(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The same environment leak P2 fixed in the os-audit command.

    A running Ciaobot chat exports CIAO_VAULT_ROOT and CIAO_WORKSPACE for its OWN
    install. Resolving the vault from those while writing the NAMED install's
    registry would migrate one install's layout using another install's vault.
    Caught on the operator's machine: the plan reported "vault root is not a
    directory" pointing at the cwd, not at the workspace it was given.
    """
    from ciao.cli import main

    install, _vault, runtime = _git_install(tmp_path)
    _registry(
        runtime,
        [
            {"name": "personal", "vault_root": "memory-vault/personal"},
            {"name": "work", "vault_root": "memory-vault/work"},
        ],
    )
    foreign = tmp_path / "foreign"
    (foreign / "memory-vault").mkdir(parents=True)
    monkeypatch.setenv("CIAO_WORKSPACE", str(foreign))
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(foreign / "memory-vault"))
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test")

    code = main(["workspace-reroot", "--workspace", str(install)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload["refusals"]
    assert payload["vault_root"] == str(install / "memory-vault")
    assert "foreign" not in payload["vault_root"]


def test_the_dry_run_shows_every_move_the_apply_would_make(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A plan that understates the work is a plan nobody should approve.

    On the reference install it listed 5 moves while `--apply` made 9: the skill
    catalog, its lockfile, the guide and the guide's symlink were all missing.
    """
    from ciao.cli import main

    install, _vault, runtime = _git_install(tmp_path)
    _catalog(install)
    (install / "CLAUDE.md").write_text("# guide\n", encoding="utf-8")
    (install / "AGENTS.md").symlink_to("CLAUDE.md")
    _registry(
        runtime,
        [
            {"name": "personal", "vault_root": "memory-vault/personal"},
            {"name": "work", "vault_root": "memory-vault/work"},
        ],
    )
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test")

    main(["workspace-reroot", "--workspace", str(install)])

    payload = json.loads(capsys.readouterr().out)
    assert payload["primary"] == "personal"
    assert payload["total_moves"] == 9
    sources = (
        [m["source"] for m in payload["moves"]]
        + [m["source"] for m in payload["skills_triage"]["moves"]]
        + [m["source"] for m in payload["guide_moves"]]
    )
    assert "skills" in sources and "skills-lock.json" in sources
    assert "CLAUDE.md" in sources and "AGENTS.md" in sources


def test_stashing_a_tracked_aggregate_stages_its_removal(tmp_path: Path) -> None:
    """Moving a tracked file out of the worktree has to tell git.

    On the reference install two of the stashed aggregates are tracked (the vault
    root's MEMORY.md and VOCABULARY.md), so without this the index still claimed
    files that were gone: `git status` showed two unstaged deletions and whoever
    committed the migration would have carried two ghost entries.
    """
    install, vault, runtime = _git_install(tmp_path)

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert applied["status"] == "migrated", applied.get("refusals")
    tracked = [e for e in applied["stashed_files"] if e.get("tracked")]
    assert tracked, "the fixture's generated notes are tracked"
    status = _git(install, "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=no")
    unstaged_deletions = [line for line in status.splitlines() if line.startswith(" D")]
    assert unstaged_deletions == [], unstaged_deletions


def test_undo_restages_only_what_was_tracked(tmp_path: Path) -> None:
    """`git add` on a previously untracked file would newly track it, which is
    not a restoration."""
    install, vault, runtime = _git_install(tmp_path)
    # Make .DS_Store genuinely untracked, which is how the reference install has
    # it: gitignored, so it is stashed for undo but must not be staged.
    _git(install, "rm", "--cached", "--quiet", "--", "memory-vault/.DS_Store")
    (install / ".gitignore").write_text(".runtime/\n.DS_Store\n", encoding="utf-8")
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "ignore .DS_Store")

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")
    flags = {e["source"]: e["tracked"] for e in applied["stashed_files"]}
    assert flags["memory-vault/INDEX.md"] is True
    assert flags["memory-vault/.DS_Store"] is False

    undo(install, runtime)

    cached = _git(install, "ls-files", "--cached")
    assert "memory-vault/INDEX.md" in cached
    # Restoring an untracked file must not newly track it.
    assert "memory-vault/.DS_Store" not in cached
    assert (install / "memory-vault" / ".DS_Store").is_file()


def test_undo_is_resumable_after_a_partial_reversal(tmp_path: Path) -> None:
    """A first undo can stop partway; the second must finish, not fail.

    This happened on the reference install. A stale guide recreated at the old
    path blocked one reversal, and re-running then failed on the moves it had
    ALREADY reversed — "bad source", because the source was back where it
    belonged. There was no way forward but hand-editing the tree.
    """
    install, vault, runtime = _with_guide(tmp_path)
    apply(install, vault, ["personal", "work"], runtime, primary="personal")
    # Reverse one move by hand, exactly as a partial undo would leave it.
    _git(install, "mv", "personal/AGENTS.md", "AGENTS.md")

    result = undo(install, runtime)

    assert result["status"] == "undone", result
    assert "AGENTS.md" in result["already_reversed"]
    assert (install / "CLAUDE.md").read_text(encoding="utf-8") == _REAL_GUIDE
    assert (install / "memory-vault" / "personal").is_dir()
    assert not (install / "personal").exists()


def test_undo_stages_the_created_files_it_removes(tmp_path: Path) -> None:
    """Once the migration is COMMITTED those paths are tracked, so deleting them
    without staging leaves `git mv` of any ancestor failing with "bad source"."""
    install, vault, runtime = _with_guide(tmp_path)
    apply(install, vault, ["personal", "work"], runtime, primary="personal")
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "reroot")

    result = undo(install, runtime)

    assert result["status"] == "undone", result
    assert (install / "memory-vault" / "personal").is_dir()
    assert not (install / "personal").exists()
    status = _git(install, "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=no")
    assert " D " not in status, status


def test_an_empty_catalog_directory_is_not_moved(tmp_path: Path) -> None:
    """`git mv` refuses an empty directory, failing and rolling back the whole run.

    Hit on the reference install, which has an empty `subagents/`. An empty
    directory has nothing to move; the bootstrap creates one per root when it
    needs to.
    """
    install, vault, runtime = _git_install(tmp_path)
    _catalog(install)
    (install / "subagents").mkdir()
    (install / "commands").mkdir()
    (install / "commands" / "thing.md").write_text("# thing\n", encoding="utf-8")
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "catalog")

    triage = plan_skills_triage(install, "personal")
    sources = [m.source for m in triage.moves]

    assert "subagents" not in sources, "an empty directory has nothing to move"
    assert "commands" in sources, "a populated one does"

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert result["status"] == "migrated", result.get("refusals")
    assert (install / "personal" / "commands" / "thing.md").is_file()


# -- the upgrade trigger -----------------------------------------------------
#
# The design always intended this to run unattended at upgrade. An install
# nobody migrates gets a feature nobody reaches, and asking every user to run a
# CLI migration by hand is how one gets run against an engine that does not
# understand the result — which is exactly what happened once on the reference
# install.


def _trigger_config(install: Path, runtime: Path, workspaces=("personal", "work")):
    from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache

    reset_reroot_cache()
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=install,
        vault_root=install / "memory-vault",
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in workspaces
        },
    )


def test_the_trigger_migrates_a_committed_install(tmp_path: Path) -> None:
    from ciao.workspace_reroot import migrate_if_needed

    install, _vault, runtime = _with_guide(tmp_path)

    result = migrate_if_needed(_trigger_config(install, runtime))

    assert result["status"] == "migrated", result.get("refusals")
    assert (install / "personal" / "memory-vault").is_dir()
    assert (install / "work" / "CLAUDE.md").is_file()
    # Derived state rebuilt for the layout that now exists.
    assert result["indexes"]["rebuilt"], result["indexes"]
    assert (install / "personal" / "memory-vault" / "INDEX.md").is_file()


def test_the_trigger_is_idempotent(tmp_path: Path) -> None:
    """It runs on every start, so a second start must be a no-op."""
    from ciao.workspace_reroot import migrate_if_needed

    install, _vault, runtime = _with_guide(tmp_path)
    config = _trigger_config(install, runtime)
    assert migrate_if_needed(config)["status"] == "migrated"

    again = migrate_if_needed(config)

    assert again == {"status": "already_migrated"}


def test_the_trigger_refuses_without_raising(tmp_path: Path) -> None:
    """A migration that cannot run is a condition to surface, not a reason to
    fail an upgrade and leave the install half-started."""
    from ciao.workspace_reroot import migrate_if_needed, peek_receipt

    install, _vault, runtime = _with_guide(tmp_path)
    (install / "CLAUDE.md").write_text("edited, uncommitted\n", encoding="utf-8")

    result = migrate_if_needed(_trigger_config(install, runtime))

    assert result["status"] == "refused"
    assert result["refusals"], "the reason has to reach the receipt"
    assert (install / "memory-vault").is_dir(), "a refusal moves nothing"
    # And the reason is readable by the detector afterwards.
    assert peek_receipt(runtime)["status"] == "refused"


def test_the_trigger_does_nothing_without_a_vault(tmp_path: Path) -> None:
    """A fresh install still in setup has no vault to move."""
    from ciao.workspace_reroot import migrate_if_needed

    install = tmp_path / "install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)

    result = migrate_if_needed(_trigger_config(install, runtime))

    assert result["status"] == "not_applicable"
    assert "no vault" in result["reason"]


def test_the_trigger_migrates_an_install_that_skipped_setup(tmp_path: Path) -> None:
    """No REGISTERED workspaces, but the vault already has the two directories.

    `workspace_names` falls back to the bootstrap default here, and that fallback
    is load-bearing: nothing else seeds a registry for an install that never ran
    `ciao setup`. So this install does need separating, and the trigger is right
    to do it rather than treating an empty registry as "nothing to do".
    """
    from ciao.workspace_reroot import migrate_if_needed

    install, _vault, runtime = _with_guide(tmp_path)
    (runtime / "workspaces.json").unlink(missing_ok=True)

    result = migrate_if_needed(_trigger_config(install, runtime, workspaces=()))

    assert result["status"] == "migrated", result.get("refusals")
    assert (install / "personal" / "memory-vault").is_dir()


def test_a_failure_inside_apply_does_not_escape_the_trigger(tmp_path: Path) -> None:
    """An upgrade must not die because the migration hit something unexpected.

    Distinct from the refusal path: a refusal is a recorded decision, this is an
    exception nobody predicted, and it must still leave the install started and
    the condition surfaced rather than aborting startup.
    """
    from ciao import workspace_reroot

    install, _vault, runtime = _with_guide(tmp_path)
    config = _trigger_config(install, runtime)
    real_apply = workspace_reroot.apply

    def boom(*args, **kwargs):
        raise RuntimeError("disk fell over")

    workspace_reroot.apply = boom
    try:
        result = workspace_reroot.migrate_if_needed(config)
    finally:
        workspace_reroot.apply = real_apply

    assert result["status"] == "error"
    assert "disk fell over" in result["reason"]
    assert (install / "memory-vault").is_dir(), "nothing moved"


def test_the_trigger_never_raises_on_a_broken_config(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from ciao.workspace_reroot import migrate_if_needed

    result = migrate_if_needed(SimpleNamespace())

    assert result["status"] == "error"
    assert result["reason"]


def test_startup_runs_the_trigger_between_the_sync_and_the_index(tmp_path: Path) -> None:
    """Ordering is the caller's job and it matters both ways.

    After the git sync, so the clean-tree gate judges the real tree. Before the
    index refresh, so the indexes are rebuilt for the layout that now exists.
    """
    import inspect

    from ciao import main

    # Anchored on the actual CALLS, not on tracker labels: a commented-out
    # `tracker.start("reroot_workspaces")` still contains the string, so a
    # label-based check passed with the whole step disabled.
    # Comments stripped, including trailing ones: a call moved into a comment
    # still contains the string, so a text search over the raw source passed with
    # the whole step disabled.
    source = "\n".join(
        line.split("#", 1)[0] for line in inspect.getsource(main).splitlines()
    )
    sync = source.index("await sync_workspace(")
    reroot = source.index("migrate_if_needed, config")
    index = source.index("_refresh_vault_index,")
    assert sync < reroot < index, (sync, reroot, index)


def test_startup_restarts_after_a_successful_migration(tmp_path: Path) -> None:
    """The config that ran the migration cannot be trusted afterwards.

    It was loaded before the move, so every per-workspace vault path it holds
    points at a directory that has gone — `workspace_vault_root("personal")`
    still says `memory-vault/personal`. On the reference install that wrote the
    auto-project doc back into the emptied vault. Patching the object in place
    would leave anything already derived from it stale, so the process restarts
    into one that reads the new registry from disk.
    """
    import inspect

    from ciao import main

    source = "\n".join(
        line.split("#", 1)[0] for line in inspect.getsource(main).splitlines()
    )
    reroot = source.index("migrate_if_needed, config")
    restart = source.index("return config.restart_exit_code", reroot)
    serve = source.index("await server.serve()")
    assert reroot < restart < serve, "it must return before anything serves"


def test_startup_syncs_every_agent_root_not_the_install_root(tmp_path: Path) -> None:
    """After the re-rooting the install root is not an agent root.

    Syncing it there seeded a stock CLAUDE.md beside the real per-root guides and
    pruned the install root's stale `.agents/skills` links — 17 tracked deletions
    for mirrors nothing reads any more.
    """
    import inspect

    from ciao import main

    source = inspect.getsource(main)
    assert "config.agent_root_targets()" in source
    assert "update_skills(str(config.workspace_root))" not in source


# -- the vault directory's leaf is configurable -------------------------------


def _custom_leaf_install(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A committed install whose vault is `vault/`, not `memory-vault/`.

    `CIAO_VAULT_ROOT` sets this, so it is an ordinary install, not an exotic one.
    """
    install = tmp_path / "install"
    install.mkdir()
    _git(install, "init", "-b", "main")
    _git(install, "config", "user.email", "test@example.com")
    _git(install, "config", "user.name", "Test")
    default = _vault(install)
    vault = install / "vault"
    default.rename(vault)
    runtime = install / ".runtime"
    runtime.mkdir()
    (install / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "seed")
    return install, vault, runtime


def test_the_rebuilds_follow_the_vault_leaf_they_were_given(tmp_path: Path) -> None:
    """The migration reported success while rebuilding nothing.

    `plan()` derives the leaf and moves `vault/` to `<name>/vault/`; the rebuild
    helpers assumed `memory-vault`, found no vault under any root, and produced
    an install with no per-root INDEX.md at all — which reads to every consumer
    as "this workspace has no notes". The receipt still said `migrated`.
    """
    install, vault, runtime = _custom_leaf_install(tmp_path)

    applied = apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert applied["status"] == "migrated", applied.get("refusals")
    out = rebuild_indexes(install, ["personal", "work"], vault_name="vault")

    assert out["errors"] == []
    assert {row["workspace"] for row in out["rebuilt"]} == {"personal", "work"}
    for name in ("personal", "work"):
        assert (install / name / "vault" / "INDEX.md").is_file()
        assert (install / name / "vault" / "VOCABULARY.md").is_file()


def test_the_trigger_passes_the_real_leaf_to_the_rebuilds(tmp_path: Path) -> None:
    """End to end through `migrate_if_needed`, which is what an upgrade runs."""
    from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
    from ciao.workspace_reroot import migrate_if_needed

    install, vault, runtime = _custom_leaf_install(tmp_path)
    reset_reroot_cache()
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=install,
        vault_root=vault,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"vault/{name}")
            for name in ("personal", "work")
        },
    )

    result = migrate_if_needed(config)

    assert result["status"] == "migrated", result
    assert result["indexes"]["errors"] == []
    assert result["search"]["errors"] == []
    for name in ("personal", "work"):
        assert (install / name / "vault" / "INDEX.md").is_file()


def test_a_missing_per_root_vault_is_reported_not_skipped(tmp_path: Path) -> None:
    """`continue` alone made a wrong leaf look like a clean run."""
    install, vault, runtime = _git_install(tmp_path)
    apply(install, vault, ["personal", "work"], runtime, primary="personal")

    out = rebuild_search_index(
        install, ["personal", "work"], db_path=tmp_path / "fts.db", vault_name="not-a-vault"
    )

    assert out["indexed"] == []
    assert {row["workspace"] for row in out["errors"]} == {"personal", "work"}


# -- an install with no git history gets one -----------------------------------


def _no_git_install(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A real-shaped install that is not a git repository, holding secrets."""
    install = tmp_path / "install"
    install.mkdir()
    vault = _vault(install)
    runtime = install / ".runtime"
    runtime.mkdir()
    (install / ".env").write_text("PWA_AUTH_TOKEN=super-secret\n", encoding="utf-8")
    (install / ".env.example").write_text("PWA_AUTH_TOKEN=\n", encoding="utf-8")
    (install / "secrets").mkdir()
    (install / "secrets" / "sa.json").write_text('{"key": "private"}\n', encoding="utf-8")
    return install, vault, runtime


def test_an_install_with_no_repository_is_given_one_before_migrating(tmp_path: Path) -> None:
    """It used to refuse forever: the first `git mv` failed, so the install could
    never migrate while the blocking gate kept asking it to."""
    install, vault, runtime = _no_git_install(tmp_path)

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["status"] == "migrated", result.get("refusals")
    assert result["git_history"]["created_repo"] is True
    assert result["git_history"]["commit"]
    assert (install / "personal" / "memory-vault" / "People" / "Peter.md").is_file()


def test_the_snapshot_is_a_working_rollback_point(tmp_path: Path) -> None:
    """The reason the repository is created at all. `--undo` remains the proper
    path (it also clears the receipt); this is the floor underneath it."""
    install, vault, runtime = _no_git_install(tmp_path)
    apply(install, vault, ["personal", "work"], runtime, primary="personal")
    assert not (install / "memory-vault").exists()

    _git(install, "reset", "--hard", "HEAD")
    _git(install, "clean", "-fd")

    assert (install / "memory-vault" / "personal" / "People" / "Peter.md").is_file()
    assert not (install / "personal").exists()


def test_the_snapshot_never_captures_credentials(tmp_path: Path) -> None:
    """"We made you a backup" must not mean "we committed your provider keys"."""
    install, vault, runtime = _no_git_install(tmp_path)

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    # The snapshot COMMIT's tree, not the index: the index already carries the
    # staged renames, so it describes the migrated layout rather than the thing a
    # rollback would restore.
    snapshot = result["git_history"]["commit"]
    tracked = subprocess.run(
        ["git", "-C", str(install), "ls-tree", "-r", "--name-only", snapshot],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert ".env" not in tracked
    assert "secrets/sa.json" not in tracked
    assert ".runtime/state.json" not in tracked
    # Documentation is not a credential.
    assert ".env.example" in tracked
    # The vault itself is the thing being protected, so it must be in there.
    assert any(name.startswith("memory-vault/") for name in tracked)


def test_an_existing_repository_is_left_completely_alone(tmp_path: Path) -> None:
    install, vault, runtime = _git_install(tmp_path)
    before = subprocess.run(
        ["git", "-C", str(install), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    history = ensure_rollback_history(install)

    assert history["status"] == "existing"
    assert history["created_repo"] is False
    assert history["commit"] == before


def test_a_repository_with_no_commits_gets_the_snapshot(tmp_path: Path) -> None:
    """`git mv` works without a HEAD, but `git checkout` has nothing to return
    to, so an initialised-but-never-committed repo is not a rollback point."""
    install, vault, runtime = _no_git_install(tmp_path)
    _git(install, "init", "-b", "main")

    history = ensure_rollback_history(install)

    assert history["status"] == "seeded_empty_repo"
    assert history["created_repo"] is False
    assert history["commit"]


def test_an_empty_directory_does_not_refuse_the_migration(tmp_path: Path) -> None:
    """`git mv` fails on an empty directory and one failure refuses the whole run.

    So a vault holding an empty `Templates/` — an ordinary thing for someone who
    never used templates — could not migrate at all. Found by booting a released
    bundle against a synthetic pre-migration install, which is the path a real
    upgrade takes and the one no test covered.

    Nothing is moved because there is nothing to move: git cannot track an empty
    directory, so it carries no content and no history. It is removed and recorded,
    which also lets the vault directory itself be pruned afterwards.
    """
    install, vault, runtime = _git_install(tmp_path)
    empty = vault / "Templates"
    for child in list(empty.iterdir()):
        child.unlink()
    # Committed, because the clean-tree gate refuses on an uncommitted deletion —
    # correctly. The real shape is a directory git tracks nothing in, which is the
    # only shape an empty directory can have.
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "drop the templates")
    assert not any(empty.iterdir())

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["status"] == "migrated", result.get("refusals")
    assert "memory-vault/Templates" in result["pruned_empty"]
    assert not (install / "memory-vault").exists()
    assert (install / "personal" / "memory-vault" / "People" / "Peter.md").is_file()


def test_a_directory_with_content_is_still_moved_not_pruned(tmp_path: Path) -> None:
    """The skip must not become "delete anything that looks inconvenient"."""
    install, vault, runtime = _git_install(tmp_path)

    result = apply(install, vault, ["personal", "work"], runtime, primary="personal")

    assert result["pruned_empty"] == []
    assert (install / "templates-src" / "person.md").is_file()


def test_mark_migrated_records_a_hand_moved_install(tmp_path: Path) -> None:
    """The prompt route needs this: a vault moved by hand has no receipt, and
    `agent_root` answers per-root only when a receipt says so — so without it the
    files are nested while every consumer still resolves the install root."""
    from ciao.workspace_reroot import mark_born_per_root, read_receipt

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    for name in ("personal", "work"):
        (tmp_path / name / "memory-vault").mkdir(parents=True)

    mark_born_per_root(tmp_path, runtime, ["personal", "work"], origin="hand")

    receipt = read_receipt(runtime)
    assert receipt is not None
    assert receipt["status"] == "migrated"
    assert receipt["origin"] == "hand"
    # Not "born": it did not start life this way, and a receipt that says so
    # would claim a history the install does not have.
    assert receipt["born_per_root"] is False
    assert receipt["moves"] == []


def test_a_fresh_install_receipt_says_born(tmp_path: Path) -> None:
    from ciao.workspace_reroot import mark_born_per_root, read_receipt

    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    mark_born_per_root(tmp_path, runtime, ["personal"])

    receipt = read_receipt(runtime)
    assert receipt["origin"] == "born"
    assert receipt["born_per_root"] is True
