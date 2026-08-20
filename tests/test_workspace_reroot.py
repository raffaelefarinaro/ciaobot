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

import hashlib
import json
from pathlib import Path

from ciao.workspace_reroot import (
    apply,
    dirty_tracked_paths,
    format_skill_triage,
    plan,
    plan_skills_triage,
    read_receipt,
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
    write_receipt(runtime, {"status": "surveyed", "marker": "second"})

    archived = list((runtime / "migration").glob("workspace-rooting.*.json"))
    assert archived, "the earlier receipt was lost"
    kept = json.loads(archived[0].read_text(encoding="utf-8"))
    assert kept["marker"] == "first"


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
