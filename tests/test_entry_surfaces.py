"""Tests for the entry-level surfaces: review, history, map and the writers.

The detector itself is covered in ``test_entry_staleness.py``; what is pinned
here is that each surface *reads it* rather than re-deriving it, that a mixed
note looks mixed everywhere, that an entry-level candidate links its pending
`note_edit` proposal rather than duplicating the decision, and that an ordinary
save cannot leave a `[verified:]` stamp on text it changed.
"""

from __future__ import annotations

import datetime
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

TODAY = datetime.date(2026, 9, 30)


def _person_note(*, related: str = "", updated: str = "2026-09-29") -> str:
    """A note whose second fact is from 2019, whatever its own date says.

    The shape the whole level exists for: re-stamping the file last week cleared
    the whole-note flag and silently re-certified everything in it. ``related``
    adds a resolved frontmatter ref, which is how a fixture gets rid of the
    `unlinked` signal so a test can look at one finding at a time. ``updated``
    moves the note's *own* date, which is the other axis: a test that wants the
    row queued on the whole-note signal rather than the entry one sets an old
    date, and a test that wants the row queued *only* because of an overdue fact
    leaves it current. The paragraph between the heading and the bullets is the
    same-section prose a fact's context is allowed to show.
    """
    link = f"related: [{related}]\n" if related else ""
    # Composed rather than interpolated into a dedented template: `dedent`
    # measures its common prefix across every line, and one interpolated line
    # with no indentation pins that prefix at "" — so the whole note would keep
    # the template's own eight spaces, have no frontmatter at all, and make every
    # assertion below pass for the wrong reason.
    return (
        "---\n"
        "type: person\n"
        f"updated: {updated}\n"
        "tags: [person]\n"
        "aliases: [Ali]\n"
        f"{link}"
        "---\n"
        "# Alice\n"
        "\n"
        "Tenancy notes from the letting agency.\n"
        "\n"
        "- Lives in Porto and works remotely for a design studio "
        "[verified: 2026-09-28]\n"
        "- Landlord is Mr Silva, who handles the building repairs "
        "[verified: 2019-05-01]\n"
    )


def _entry(text: str, relative: str, prefix: str):
    """The parsed entry whose text starts with ``prefix``, or a loud failure.

    Parsed with the same coordinates every other reader in these tests uses, so
    the identity it mints is the one the vault's check state is keyed by — an
    identity minted with a different `note_path` is a different key that resolves
    to nothing, which is exactly the silent failure the workspace coordinate is
    there to prevent.
    """
    from ciao import note_entries as ne

    for entry in ne.parse_note_entries(
        text, note_path=relative, workspace="personal"
    ).entries:
        if entry.text.startswith(prefix):
            return entry
    raise AssertionError(f"no entry starting {prefix!r}")


# --- vault review ----------------------------------------------------------


def _review_vault(
    tmp_path: Path, *, related: str = "People/Bob.md", updated: str = "2026-09-29"
) -> Path:
    people = tmp_path / "People"
    people.mkdir(parents=True)
    (people / "Bob.md").write_text(
        textwrap.dedent(
            """\
            ---
            type: person
            updated: 2026-09-29
            tags: [person]
            ---
            # Bob

            - Works at Radix
            """
        ),
        encoding="utf-8",
    )
    (people / "Alice.md").write_text(
        _person_note(related=related, updated=updated), encoding="utf-8"
    )
    (tmp_path / "Workspace").mkdir(exist_ok=True)
    return tmp_path


# --- history ---------------------------------------------------------------


def _history_detail(proposal: object) -> dict:
    rows: list[dict] = [
        {
            "kind": "note_edit",
            "action": "accepted",
            "proposal_id": proposal.proposal_id,
            "destination": proposal.relative_path,
        }
    ]
    with pytest.MonkeyPatch.context() as patch:
        import ciao.web.routes_api as routes

        patch.setattr(
            routes,
            "list_sidecars",
            lambda config, workspace: [proposal],
            raising=False,
        )
        import ciao.note_edit_proposals as nep

        patch.setattr(nep, "list_sidecars", lambda config, workspace: [proposal])
        routes._attach_note_edit_detail(SimpleNamespace(), "personal", rows)
    return rows[0]["note_edit"]


# --- the memory map --------------------------------------------------------


def test_the_map_node_reports_coverage_and_never_raises_the_stale_flag(
    tmp_path: Path,
) -> None:
    """The map cannot stop at the whole-note flag, and does not start at it either.

    `stale` is the file-level verdict and the nightly worklist is where an
    overdue bullet becomes a plan, so the map reports coverage *beside* the flag
    rather than raising it — otherwise the map and the plan would be answering
    two different questions about the same note.
    """
    from ciao.memory_audit import note_entry_coverage

    text = _person_note()
    coverage, selected, _doc = note_entry_coverage(
        text,
        note_type="person",
        updated="2026-09-29",
        mtime=0.0,
        note_path="People/Alice.md",
        rendered="memory-vault/People/Alice.md",
        today=TODAY,
    )
    # The file is one day old against the 30-day horizon: not stale *as a file*.
    assert coverage.age_days == 1
    # And the fact beside it is seven years old: selected, and not clean.
    assert [v.reason_code for v in selected] == ["aged"]
    assert coverage.fully_verified is False
    assert coverage.stale == 1
    assert coverage.coverage_ratio > 0.5


# --- the writers -----------------------------------------------------------


def test_an_ordinary_save_cannot_carry_a_stamp_it_did_not_earn() -> None:
    """The rule the accept path enforces, and the prompt only asks for.

    A fold rewrites the whole file, and a rewrite is exactly how a
    `[verified: today]` ends up on the sentence most likely to have changed. The
    test pins all three outcomes: a changed bullet loses its stamp, an untouched
    neighbour keeps it, and a pure re-stamp is untouched (which is what makes the
    rule safe to run on every save).
    """
    from ciao import note_entries as ne

    before = textwrap.dedent(
        """\
        - Lives in Porto [verified: 2026-09-01]
        - Landlord is Mr Silva [verified: 2020-01-01]
        """
    )
    after = textwrap.dedent(
        """\
        - Lives in Lisbon now [verified: 2026-09-30]
        - Landlord is Mr Silva [verified: 2020-01-01]
        - Works at Radix [verified: 2026-09-30]
        """
    )
    cleaned, dropped = ne.invalidate_stale_stamps(before, after)
    assert dropped == 2
    assert cleaned == (
        "- Lives in Lisbon now\n"
        "- Landlord is Mr Silva [verified: 2020-01-01]\n"
        "- Works at Radix\n"
    )
    # The changed bullets now read as unverified to the detector, which is the
    # honest state for a fact nobody checked. The note is aged on purpose: an
    # unstamped bullet inherits the note's date, so in a fresh note it is current
    # exactly as far as the file is and there is nothing left to re-read.
    from ciao.memory_audit import note_entry_coverage

    _coverage, selected, _doc = note_entry_coverage(
        cleaned,
        note_type="person",
        updated="2024-01-02",
        mtime=0.0,
        note_path="People/A.md",
        rendered="memory-vault/People/A.md",
        today=TODAY,
    )
    assert {v.reason_code for v in selected} == {"no-stamp", "aged"}


def test_an_untouched_bullet_survives_an_unrelated_save(tmp_path: Path) -> None:
    """The case the rule must NOT touch, which is the whole risk of adding it.

    A save that rewrites one bullet in a file must leave every other bullet's
    stamp exactly as it was — including the case this rule is *not* about: a
    bullet whose own date moved because the `still_valid` verdict wrote it. That
    write is a verification and goes through
    `ciao.note_receipts.apply_entry_edit`, which never runs this function; an
    ordinary save that merely happens to leave the token alone must be a no-op.
    """
    from ciao import note_entries as ne

    before = (
        "- Lives in Porto [verified: 2026-09-20]\n"
        "- Landlord is Mr Silva [verified: 2020-01-01]\n"
    )
    after = (
        "- Lives in Lisbon [verified: 2026-09-20]\n"
        "- Landlord is Mr Silva [verified: 2020-01-01]\n"
    )
    cleaned, changed = ne.invalidate_stale_stamps(before, after)
    # Only the bullet whose words changed, and only because its claim is gone.
    assert changed == 1
    assert cleaned == (
        "- Lives in Lisbon\n- Landlord is Mr Silva [verified: 2020-01-01]\n"
    )


def test_a_redated_unchanged_bullet_is_put_back() -> None:
    """The half a fingerprint cannot see, and the one that mattered.

    The fingerprint ignores the stamp by design — that is what makes a re-stamp
    invisible to it — so "the words are unchanged" cannot also mean "the stamp is
    fine". A save that re-dates a bullet it did not touch is the exact failure
    the writers exist to stop: today's date, attached to a sentence somebody
    checked six years ago. The date ``before`` carried is restored, not merely
    removed, because removing it would also lose a real verification.
    """
    from ciao import note_entries as ne

    before = "- Lives in Berlin [verified: 2020-01-01]\n"
    after = "- Lives in Berlin [verified: 2026-09-30]\n"
    cleaned, changed = ne.invalidate_stale_stamps(before, after)
    assert changed == 1
    assert cleaned == before


def test_a_stamp_added_to_an_unchanged_bullet_is_cut() -> None:
    """The other shape of the same failure: none before, one after.

    Nothing was verified about those exact words on any day, and a save is not a
    check, so the token goes rather than being dated back to something `before`
    never held.
    """
    from ciao import note_entries as ne

    cleaned, changed = ne.invalidate_stale_stamps(
        "- Lives in Berlin\n", "- Lives in Berlin [verified: 2026-09-30]\n"
    )
    assert changed == 1
    assert cleaned == "- Lives in Berlin\n"


def test_a_neighbour_is_left_alone_while_a_redated_sibling_is_put_back() -> None:
    """The two rules together, on one file.

    A save that rewrote one bullet and re-dated another has to produce a file
    where the untouched bullet keeps its real date, the changed one has none, and
    the re-dated one is back to what it was — the whole point being that the
    writer is a filter, not a reset.
    """
    from ciao import note_entries as ne

    before = "- Speaks Greek [verified: 2020-01-01]\n- Works at Acme [verified: 2019-01-01]\n"
    after = "- Speaks Greek [verified: 2026-09-30]\n- Works at Helios [verified: 2026-09-30]\n"
    cleaned, changed = ne.invalidate_stale_stamps(before, after)
    assert changed == 2
    assert cleaned == (
        "- Speaks Greek [verified: 2020-01-01]\n- Works at Helios\n"
    )


def test_the_fold_writer_strips_stamps_before_it_writes(tmp_path: Path) -> None:
    """An entry update touches one span; a stamp it did not touch survives.

    The folds no longer rewrite the file, so there is no rewritten whole to
    scrub: a `[verified:]` stamp on an untouched bullet is unchanged because
    its bytes are unchanged. This pins that through the update half of the
    fold write.
    """
    from ciao import file_kernel as fk
    from ciao import note_entries as ne
    from ciao import note_receipts as nr

    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    relative = "notes/mo.md"
    note = vault / relative
    note.parent.mkdir(parents=True, exist_ok=True)
    # Byte-exact IO: the revision below must hash the file's own bytes, and
    # `write_text`/`read_text` translate newlines on Windows, which would
    # hand the writer a revision of text the file does not hold.
    note.write_bytes(
        "- Landlord is Mr Silva [verified: 2020-01-01]\n- Speaks Greek\n".encode(
            "utf-8"
        )
    )
    text = note.read_bytes().decode("utf-8")
    entries = ne.parse_note_entries(
        text, note_path=relative, workspace="personal"
    ).entries
    nr.apply_entry_edit(
        vault_root=vault,
        relative_path=relative,
        expected_revision=fk.content_revision(text),
        identity=entries[1].identity,
        fingerprint=entries[1].fingerprint,
        replacement="- Speaks Portuguese",
        actor="proposal-accept",
        source="fold",
        workspace="personal",
    )
    assert note.read_bytes().decode("utf-8").splitlines(keepends=True)[0] == (
        "- Landlord is Mr Silva [verified: 2020-01-01]\n"
    )


def test_the_fold_writer_puts_a_redated_bullet_back(tmp_path: Path) -> None:
    """An appended bullet does not move the stamps already in the note.

    The add half of the fold write: the new bullet lands under `## Notes`
    and the untouched bullet keeps the exact stamp it carried, including its
    date.
    """
    from ciao import file_kernel as fk
    from ciao import note_receipts as nr

    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    relative = "notes/mo.md"
    note = vault / relative
    note.parent.mkdir(parents=True, exist_ok=True)
    # Byte-exact IO, as above: a translated revision is a different revision.
    note.write_bytes(
        "# Mo\n\n## Notes\n- Prefers terse replies [verified: 2024-03-01]\n".encode(
            "utf-8"
        )
    )
    text = note.read_bytes().decode("utf-8")
    nr.append_list_item(
        vault_root=vault,
        relative_path=relative,
        expected_revision=fk.content_revision(text),
        section="Notes",
        item="- Based in Lisbon.",
        actor="proposal-accept",
        source="fold",
        workspace="personal",
    )
    after = note.read_bytes().decode("utf-8")
    assert "- Prefers terse replies [verified: 2024-03-01]\n" in after
    assert "- Based in Lisbon.\n" in after


def test_the_authoring_guidance_names_the_entry_contract() -> None:
    """The stock prompts, pinned on the words the detector depends on.

    A prompt is prose and prose rots, so the two things a writer must not get
    wrong — a bullet is the unit, and a stamp is a claim about actual
    verification — are asserted rather than left to be re-read.
    """
    from importlib import resources

    skill = (
        resources.files("ciao.stock")
        .joinpath("skills/ciao-capabilities/references/memory-operations/guide.md")
        .read_text(encoding="utf-8")
    )
    command = (
        resources.files("ciao.stock")
        .joinpath("commands/remember.md")
        .read_text(encoding="utf-8")
    )
    # One copy of the rules (#1462): the skill carries them, and the command and
    # the care run's pass-3 reference point at that section instead of restating.
    assert "one fact per bullet" in skill.lower()
    assert "after actually going and checking" in skill
    assert "you did not earn" not in command  # not restated in the command
    assert "updated:" in skill
    assert 'follow the "Writing entity notes" steps in the durable-memory guide' in command
    assert "Writing entity notes" in skill
    assert "uncovered" in skill


def test_remember_command_uses_the_engine_resolved_guide_from_core_prompt() -> None:
    from importlib import resources
    from pathlib import Path

    from ciao.core_prompt import unified_core

    command = resources.files("ciao.stock").joinpath("commands/remember.md").read_text(encoding="utf-8")
    guide = Path(str(resources.files("ciao.stock").joinpath(
        "skills/ciao-capabilities/references/memory-operations/guide.md"
    ))).resolve()
    actual_prompt = unified_core()
    assert "engine-packaged durable-memory guide named in the Ciaobot system instructions" in command
    assert str(guide) in actual_prompt
    assert "relative to the installed `ciao-capabilities`" not in command
