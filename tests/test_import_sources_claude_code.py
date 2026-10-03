"""The Claude Code adapter, against synthetic fixtures only.

Every session here is written for these tests (see
``tests/fixtures/import/README.md``): no real transcript, no real ``~/.claude``
and no real conversation is read. What is asserted is the contract's three
promises — roles and anchors are real, every excluded entry is counted, and
nothing is invented — plus the two refusals that make the reader safe to point
at somebody's history.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ciao.agent_paths import claude_project_slug, claude_projects_dir
from ciao.import_sources.claude_code import (
    SourceRefusal,
    discover_claude_code_sessions,
    read_claude_code_session,
)
from ciao.import_sources.contract import (
    MAX_SESSION_BYTES,
    OMISSION_ENTRY_TYPE,
    OMISSION_NON_TEXT_CONTENT,
    OMISSION_OFF_CHAIN,
    OMISSION_SIDECHAIN,
    OMISSION_TRUNCATED,
    PROVIDER_CLAUDE_CODE,
    ROLE_ASSISTANT,
    ROLE_USER,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "import"


def _fixture(name: str) -> Path:
    """A synthetic session file. Nothing under here is anybody's history."""
    return FIXTURES / name


def _line(uuid: str, parent: str | None, kind: str, text: str) -> str:
    """One JSONL entry, in the shape Claude Code writes."""
    return json.dumps(
        {
            "type": kind,
            "uuid": uuid,
            "parentUuid": parent,
            "sessionId": "synthetic-session",
            "message": {"role": kind, "content": text},
        }
    )


def test_a_minimal_session_maps_roles_and_anchors() -> None:
    """Roles come from the entry's own ``type`` and the anchor from its uuid.

    The third entry is a ``user`` entry carrying only a tool result, so its text
    is empty while its role and anchor are still there: a turn with no prose is
    a real turn, and the block that filled it is counted as an omission rather
    than read as if it were something the user said.
    """
    session = read_claude_code_session(_fixture("claude_code_session_minimal.jsonl"))

    assert [message.role for message in session.messages] == [
        ROLE_USER,
        ROLE_ASSISTANT,
        ROLE_USER,
        ROLE_ASSISTANT,
    ]
    assert [message.anchor for message in session.messages] == [
        "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9",
        "2b3c4d5e-6f7a-4182-93a4-b5c6d7e8f9a0",
        "3c4d5e6f-7a8b-4293-a4b5-c6d7e8f9a0b1",
    ]
    assert session.messages[0].text == "What did we decide about the release checklist?"
    assert session.messages[1].text == "The checklist ships with the release notes."
    assert session.messages[2].text == ""
    assert session.messages[3].text == (
        "Then tag the release and publish the notes together."
    )
    # The tool call and the tool result are not text a fact extractor may read.
    assert session.omission_counts() == {OMISSION_NON_TEXT_CONTENT: 2}
    assert session.truncated is False


def test_the_session_names_itself_from_the_file_it_read(home_dir: Path) -> None:
    """``source_id`` is the session uuid Claude Code puts in the file name.

    ``project_hint`` is the project slug, which is the weakest locator Claude
    Code keeps and is not a path: ``claude_project_slug`` folds every
    non-alphanumeric to ``-``, so nothing downstream may present it as a
    directory. Reading through the real layout is what makes the slug here the
    same string ``claude_projects_dir`` names.
    """
    workspace = home_dir / "synthetic-project"
    project_dir = claude_projects_dir(workspace)
    project_dir.mkdir(parents=True)
    session_id = "9f8e7d6c-5b4a-4392-8101-2f3e4d5c6b7a"
    session_path = project_dir / f"{session_id}.jsonl"
    session_path.write_bytes(_fixture("claude_code_session_minimal.jsonl").read_bytes())

    session = read_claude_code_session(session_path)

    assert session.source.provider == PROVIDER_CLAUDE_CODE
    assert session.source.source_id == session_id
    assert session.source.project_hint == claude_project_slug(workspace)
    assert session.source.path == str(session_path)
    assert session.messages


def test_no_timestamp_is_fabricated() -> None:
    """``get_session_messages_full`` reads no date, so neither does this.

    A session file's own ``timestamp`` is not what a reader may use, and the
    file's mtime is when the engine looked at it — not when the conversation
    happened. An adapter that filled either in would be asserting a date about
    somebody's history that nothing established.
    """
    for name in ("claude_code_session_minimal.jsonl", "claude_code_session_branched.jsonl"):
        session = read_claude_code_session(_fixture(name))
        assert session.messages
        assert [message.timestamp for message in session.messages] == [None] * len(
            session.messages
        )
        assert session.truncated is False


def test_a_later_sidechain_leaf_loses_to_the_main_chain_and_is_recorded() -> None:
    """A subagent's turn is not the user's conversation, however late it is.

    The fixture's two leaves share a parent, and the sidechain one is the later
    of the two in the file. Picking the latest leaf without excluding it would
    import the subagent's note as the last thing the user said; dropping it
    silently would hide that the file held a fork at all. It has to be excluded
    **and** declared, which is the contract's whole rule about omissions.
    """
    session = read_claude_code_session(_fixture("claude_code_session_branched.jsonl"))

    assert [message.anchor for message in session.messages] == [
        "a0000000-0000-4000-8000-000000000001",
        "a0000000-0000-4000-8000-000000000002",
        "a0000000-0000-4000-8000-000000000004",
        "a0000000-0000-4000-8000-000000000005",
        "a0000000-0000-4000-8000-000000000006",
        "a0000000-0000-4000-8000-000000000007",
    ]
    texts = [message.text for message in session.messages]
    assert "Subagent note: I read both files in a sidechain branch." not in texts
    assert texts[-1] == "Opened the pull request against main."
    assert session.omission_counts() == {
        OMISSION_SIDECHAIN: 1,
        OMISSION_ENTRY_TYPE: 1,
    }


def test_the_latest_main_leaf_wins_and_the_branch_it_dropped_is_recorded(
    tmp_path: Path,
) -> None:
    """Of two branches of the user's own, the later one is the conversation.

    Claude Code appends, so the branch the user last took is the later one; that
    is a display choice the import reader inherits rather than invents. The
    branch it did not take held real user turns, so they are counted under
    ``not_on_main_chain`` — a fork the reader dropped is exactly the fact a user
    has to be shown before a model reads their history.
    """
    path = tmp_path / "two-branches.jsonl"
    path.write_text(
        "\n".join(
            (
                _line("u1", None, "user", "start here"),
                _line("u2", "u1", "user", "one question"),
                _line("a2", "u2", "assistant", "the first answer"),
                _line("u3", "u2", "user", "a different question"),
                _line("a3", "u3", "assistant", "the second answer"),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    session = read_claude_code_session(path)

    assert [message.anchor for message in session.messages] == ["u1", "u2", "u3", "a3"]
    assert session.omission_counts() == {OMISSION_OFF_CHAIN: 1}
    assert "the first answer" not in [message.text for message in session.messages]


def test_a_compaction_boundary_is_stitched_through_logical_parent_uuid() -> None:
    """The half of the conversation before a compaction comes back.

    A ``compact_boundary`` entry has no ``parentUuid`` of its own and names the
    entry it replaced through ``logicalParentUuid`` instead. A walk that read
    only ``parentUuid`` would stop there and return a session that begins at the
    summary — which is the one thing a user would notice as missing, since the
    summary is a paraphrase of what came before it.
    """
    session = read_claude_code_session(_fixture("claude_code_session_branched.jsonl"))

    anchors = [message.anchor for message in session.messages]
    assert anchors[0] == "a0000000-0000-4000-8000-000000000001"
    assert anchors[1] == "a0000000-0000-4000-8000-000000000002"
    assert session.messages[0].text == "Where does the parser live?"
    assert session.messages[1].text == "It lives in the ingest module."
    # The boundary itself is not a turn, so it is counted rather than imported.
    assert session.omission_counts()[OMISSION_ENTRY_TYPE] == 1


def test_oversize_read_stops_at_a_message_boundary_and_the_file_is_untouched(
    tmp_path: Path,
) -> None:
    """A session over the cap is read up to its last whole line, and flagged.

    The padded entry is the one that crosses :data:`MAX_SESSION_BYTES`, so a
    reader that ignored the cap would import a message ending mid-word and one
    that cut at an arbitrary byte would import a line that does not parse. Both
    are worse than a shorter conversation which says it is shorter: that is what
    ``truncated`` is for, and it is recorded as an omission rather than left for
    the caller to infer from a message count.

    The file is not modified — the reader opens it read-only and writes nothing,
    so the bytes after the read are the bytes before it.
    """
    path = tmp_path / "oversized.jsonl"
    path.write_text(
        "\n".join(
            (
                _line("u1", None, "user", "first question"),
                _line("a1", "u1", "assistant", "a long answer " + "x" * MAX_SESSION_BYTES),
                _line("u2", "a1", "user", "a question past the cap"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert path.stat().st_size > MAX_SESSION_BYTES
    before = path.read_bytes()

    session = read_claude_code_session(path)

    assert session.truncated is True
    assert session.omission_counts() == {OMISSION_TRUNCATED: 1}
    # The last line that ended before the cap, and nothing of the one that did not.
    assert [message.anchor for message in session.messages] == ["u1"]
    assert "a long answer" not in [message.text for message in session.messages]
    assert "a question past the cap" not in [message.text for message in session.messages]
    assert path.read_bytes() == before


def test_a_symlinked_session_is_refused_and_its_target_is_untouched(
    tmp_path: Path,
) -> None:
    """A link at the session path is not read, and what it points at is not touched.

    The refusal happens in the open itself (``O_NOFOLLOW`` on POSIX, the
    reparse-point check on Windows), so there is no window between deciding the
    path is safe and reading through it.
    """
    target = tmp_path / "real.jsonl"
    target.write_bytes(_fixture("claude_code_session_minimal.jsonl").read_bytes())
    before = target.read_bytes()
    link = tmp_path / "linked.jsonl"
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without Developer Mode or admin
        pytest.skip(f"cannot create a symlink here: {exc}")

    with pytest.raises(SourceRefusal):
        read_claude_code_session(link)

    assert target.read_bytes() == before


def test_a_directory_named_like_a_session_is_refused(tmp_path: Path) -> None:
    """A non-regular file is not a session, however well it is named.

    POSIX opens a directory happily, so the refusal is the ``fstat`` on the open
    descriptor rather than the open itself; Windows refuses the open. One
    exception type either way, so a caller has one thing to catch.
    """
    directory = tmp_path / "not-a-session.jsonl"
    directory.mkdir()

    with pytest.raises(SourceRefusal):
        read_claude_code_session(directory)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on Windows")
def test_a_fifo_named_like_a_session_is_refused_rather_than_opened(
    tmp_path: Path,
) -> None:
    """A pipe where a transcript should be is refused, and the open does not hang.

    ``os.open`` on a FIFO read-only blocks until a writer arrives, so a reader
    that reached its ``fstat`` check by opening would wait on a file nobody ever
    writes to. The non-blocking flag is what makes "non-regular files are
    refused" true for this type as well as for a directory.
    """
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)

    with pytest.raises(SourceRefusal):
        read_claude_code_session(fifo)


def test_discovery_lists_session_files_as_metadata_only(home_dir: Path) -> None:
    """Discovery is a listing: names, ids and the slug, and nothing is read.

    ``project_hint`` is the slug Claude Code uses, which is not reversible into
    a workspace path, so a scan that could name the workspace from it would be
    claiming something the format does not keep. A file that is not a session,
    and a link that is not a file Claude Code wrote, are both left out of the
    listing rather than offered to the user to import.
    """
    workspace = home_dir / "synthetic-project"
    project_dir = claude_projects_dir(workspace)
    project_dir.mkdir(parents=True)
    session_id = "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"
    session = project_dir / f"{session_id}.jsonl"
    session.write_bytes(_fixture("claude_code_session_minimal.jsonl").read_bytes())
    (project_dir / "notes.txt").write_text("not a session", encoding="utf-8")
    try:
        (project_dir / "linked.jsonl").symlink_to(session)
    except OSError:  # Windows without Developer Mode or admin
        pass

    refs = discover_claude_code_sessions(project_dir)

    assert [(ref.provider, ref.source_id, ref.project_hint) for ref in refs] == [
        (PROVIDER_CLAUDE_CODE, session_id, claude_project_slug(workspace))
    ]
    assert refs[0].path == str(session)
    # A workspace nobody ran Claude Code in is an empty list, not a failure.
    assert discover_claude_code_sessions(project_dir / "no-such-slug") == []