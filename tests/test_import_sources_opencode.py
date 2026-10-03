"""The OpenCode adapter, against synthetic fixtures and a faked CLI only.

Every export and listing here is written for these tests (see
``tests/fixtures/import/README.md``): no real opencode session, no real
``~/.opencode`` and no real conversation is read. **The opencode CLI is never
executed.** The bounded runner is replaced, or — where the runner's own
refusals are what is under test — :func:`subprocess.run` is replaced, so no test
reaches the network, spawns a server, or touches a session database.

What is asserted is the contract's three promises for this source: roles and
anchors are the OpenCode session's own, every part that is not prose is counted,
and nothing is invented — plus the refusals that make the adapter safe to point
at somebody's history, and the one thing it must not do, which is decide whether
a session is Ciaobot's own.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ciao.import_decouple import (
    AMBIGUOUS,
    CIAOBOT_OWN,
    EXTERNAL,
    canonical_provider,
    classify_session,
)
from ciao.import_sources.contract import (
    MAX_SESSION_BYTES,
    OMISSION_ENTRY_TYPE,
    OMISSION_NON_TEXT_CONTENT,
    OMISSION_TRUNCATED,
    OMISSION_UNREADABLE_LINE,
    PROVIDER_OPENCODE,
    ROLE_ASSISTANT,
    ROLE_USER,
)
from ciao.import_sources.opencode import (
    DISCOVERY_MAX_COUNT,
    REASON_FAILED,
    REASON_TIMEOUT,
    REASON_UNAVAILABLE,
    REASON_UNREADABLE,
    REASON_UNSUPPORTED,
    SourceError,
    _run_opencode_json,
    discover_opencode_sessions,
    read_opencode_session,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "import"

#: What a supported install answers. Only the parts the adapter parses matter:
#: `_server_version_error` reads the version out of it.
SUPPORTED_VERSION = "opencode v2.0.22"

#: What a V1 install answers — below the enforced `(2, 0, 16)` floor.
UNSUPPORTED_VERSION = "opencode v1.9.4"

#: A binary that does not exist. Which path the adapter resolved is the provider's
#: business and has its own tests; these tests care about what it then asks for.
SYNTHETIC_BINARY = "/nonexistent/opencode"


def _fixture(name: str) -> Any:
    """A synthetic export or listing, parsed. Nothing under here is real history."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fake_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = SUPPORTED_VERSION,
    payload: Any = None,
    reason: str | None = None,
) -> list[tuple[str, ...]]:
    """Install a fake opencode CLI and return the list its argv lands in.

    Replaces both bounded calls the adapter makes — ``opencode --version`` and
    the JSON command — so a test can assert on *what was asked for* (which flags,
    which working directory) and *what came back* (a fixture) without a binary
    existing anywhere on this machine. The returned list is the argv of each JSON
    command, in order.
    """
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        "ciao.import_sources.opencode._resolve_binary",
        lambda binary: SYNTHETIC_BINARY,
    )
    monkeypatch.setattr(
        "ciao.import_sources.opencode._require_supported_v2",
        lambda binary, *, timeout: version,
    )

    def _run(binary: str, args: Sequence[str], timeout: float, **_: object) -> Any:
        calls.append(tuple(args))
        if reason is not None:
            raise SourceError(reason, "the fake CLI refused")
        return payload

    monkeypatch.setattr("ciao.import_sources.opencode._run_opencode_json", _run)
    return calls


def _fake_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = SUPPORTED_VERSION,
    returncode: int = 0,
    stdout: str | None = None,
    stderr: str = "",
    timeout: bool = False,
    os_error: bool = False,
) -> list[list[str]]:
    """Replace :func:`subprocess.run` so the real runner can be exercised.

    The runner *is* what several refusals live in, and a test that stops short of
    it cannot prove them. Patching one level below the adapter keeps the whole
    runner — the argv, the timeout, the return-code check, the byte cap, the JSON
    parse — on the code path, and returns the argv of every call so the test can
    see that nothing else was run.
    """
    argv: list[list[str]] = []

    # Which binary was resolved is the provider's business and is tested there;
    # what runs underneath it is what these tests are about.
    monkeypatch.setattr(
        "ciao.import_sources.opencode._resolve_binary",
        lambda binary: SYNTHETIC_BINARY,
    )

    def _run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        argv.append(command)
        if command[1:2] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, version + "\n", stderr)
        if os_error:
            raise OSError("cannot execute")
        if timeout:
            raise subprocess.TimeoutExpired(command, 1.0)
        return subprocess.CompletedProcess(command, returncode, stdout or "", stderr)

    monkeypatch.setattr(subprocess, "run", _run)
    return argv


# ── Reading one export ─────────────────────────────────────────────────────


def test_reading_asks_the_cli_for_that_session_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Roles come from the message's ``type`` tag and the anchor from its ``id``.

    OpenCode has no ``role`` field and no ``{info, parts}`` nesting: a message is
    one flat object discriminated on ``type``, and an assistant turn's prose
    lives in ``content[]``. So the role is the tag and the anchor is the message
    id (``msg_…``) — the same two facts every other adapter supplies, read from
    this source's own fields. The argv is asserted rather than assumed too: it is
    the only place the adapter touches opencode, so a change here is a change in
    what an import reads.
    """
    calls = _fake_cli(monkeypatch, payload=_fixture("opencode_export_minimal.json"))

    session = read_opencode_session("ses_7a1b2c3d4e5f60718293a4b5c6d7e8f9")

    assert calls == [
        ("session", "export", "ses_7a1b2c3d4e5f60718293a4b5c6d7e8f9"),
    ]
    assert [message.role for message in session.messages] == [
        ROLE_USER,
        ROLE_ASSISTANT,
    ]
    assert [message.anchor for message in session.messages] == [
        "msg_000000000000000000000000000001",
        "msg_000000000000000000000000000002",
    ]
    assert session.messages[0].text == "What did we decide about the release checklist?"
    assert session.messages[1].text == "The checklist ships with the release notes."
    assert session.omission_counts() == {}
    assert session.truncated is False


def test_the_session_names_itself_from_the_export_not_the_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``source_id`` and ``project_hint`` come from ``info``, and there is no path.

    An OpenCode session lives in a server's database, not in a document the user
    could open, so ``path`` is empty rather than a fabricated file name — the
    contract's field is "where the file was read from" and there is no file.
    ``project_hint`` is ``info.location.directory``: for this source it is a real
    directory, not Claude Code's lossy slug, and it is held under the same field.
    """
    _fake_cli(monkeypatch, payload=_fixture("opencode_export_minimal.json"))

    session = read_opencode_session("ses_wrong000000000000000000000000")

    assert session.source.provider == PROVIDER_OPENCODE
    assert session.source.source_id == "ses_7a1b2c3d4e5f60718293a4b5c6d7e8f9"
    assert session.source.project_hint == "/synthetic/workspace/release-checklist"
    assert session.source.path == ""


def test_tool_reasoning_file_and_compaction_parts_are_all_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing that is not prose reaches the extractor, and all of it is counted.

    The fixture's assistant turn carries prose, a ``reasoning`` part and a ``tool``
    part whose ``state`` holds both an input and an output; a later user turn
    carries a ``files`` attachment; and the session holds a ``shell`` record and a
    ``compaction`` summary. None of that is text a fact extractor may read, and
    dropping any of it without saying so is the failure the contract exists to
    prevent — so the tool and reasoning parts and the attachment are counted as
    ``non_text_content``, and the two whole records as ``other_entry_type``.
    """
    _fake_cli(
        monkeypatch,
        payload=_fixture("opencode_export_tool_and_compaction.json"),
    )

    session = read_opencode_session("ses_8b2c3d4e5f60718293a4b5c6d7e8f901")

    assert [message.anchor for message in session.messages] == [
        "msg_000000000000000000000000000011",
        "msg_000000000000000000000000000012",
        "msg_000000000000000000000000000015",
        "msg_000000000000000000000000000016",
    ]
    # The tool part's input and output are inside one part, and one part is one
    # count: the count is of parts, not of bytes or of fields.
    assert session.omission_counts() == {
        OMISSION_NON_TEXT_CONTENT: 3,
        OMISSION_ENTRY_TYPE: 2,
    }
    # Nothing the tool said is presented as something the user or model said.
    texts = [message.text for message in session.messages]
    assert "git tag --list" not in texts
    assert "ship the notes" not in " ".join(texts)
    assert "A rollback step is the one the previous release notes omit." not in texts
    assert "The checklist is missing the rollback step." in texts


def test_an_unsettled_assistant_turn_is_excluded_and_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn with no ``time.completed`` is a turn ``isSettled`` would have dropped.

    The CLI filters an assistant message out unless ``time.completed`` is set, so
    an interrupted turn is normally absent from the export entirely — and an
    adapter that only read what arrived would report a shorter conversation with
    nothing to show for the gap. So the rule is applied here as well: the
    half-finished turn becomes no message and is counted, and its truncated prose
    is never presented as something the model said.
    """
    _fake_cli(monkeypatch, payload=_fixture("opencode_export_unsettled.json"))

    session = read_opencode_session("ses_9c3d4e5f60718293a4b5c6d7e8f90112")

    assert [message.anchor for message in session.messages] == [
        "msg_000000000000000000000000000021",
        "msg_000000000000000000000000000022",
    ]
    assert session.omission_counts() == {OMISSION_ENTRY_TYPE: 1}
    texts = [message.text for message in session.messages]
    assert not any(text.endswith("and then") for text in texts)


def test_no_timestamp_is_fabricated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message's own instant, rendered; ``None`` where the export has none.

    ``DateTimeUtcFromMillis`` encodes to a number, so the JSON the CLI writes
    holds milliseconds while the contract's field is a string — so the message's
    own instant is rendered as ISO-8601 UTC. A message with no ``time`` reports
    ``None``: there is no file behind this source, so there is no mtime to reach
    for, and a date this reader did not read is not a date it may supply.
    """
    for name in ("opencode_export_minimal.json", "opencode_export_tool_and_compaction.json"):
        _fake_cli(monkeypatch, payload=_fixture(name))
        session = read_opencode_session("ses_synthetic")
        assert session.messages
        for message in session.messages:
            assert message.timestamp is not None
            assert message.timestamp.startswith("2026-03-02T")
            assert message.timestamp.endswith("+00:00")

    payload = _fixture("opencode_export_minimal.json")
    del payload["messages"][0]["time"]
    _fake_cli(monkeypatch, payload=payload)
    session = read_opencode_session("ses_synthetic")
    assert session.messages[0].timestamp is None
    # The turn's own `created`, not its `completed` and not the session's.
    assert session.messages[1].timestamp == "2026-03-02T09:15:02+00:00"


def test_the_reader_makes_no_ciaobot_own_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter hands the caller the two facts C5 needs and decides nothing.

    ``first_user_turn`` and the source id are exactly what
    :func:`ciao.import_decouple.classify_session` reads, and the provider
    vocabulary is crossed through :func:`canonical_provider` rather than compared
    literally. All three answers have to be reachable from the session alone: an
    adapter that made the call itself would have to be trusted with a decision
    that belongs to the caller's own registry, and would be the one place in the
    chain where "Ciaobot's own" could be got wrong without the exclusions.
    """
    _fake_cli(monkeypatch, payload=_fixture("opencode_export_minimal.json"))

    session = read_opencode_session("ses_7a1b2c3d4e5f60718293a4b5c6d7e8f9")

    assert session.first_user_turn == "What did we decide about the release checklist?"
    assert classify_session(
        session.source.provider,
        session.source.source_id,
        session.first_user_turn,
        known_ids=(),
    ) == EXTERNAL
    # The recorded-id rule reaches it through canonical_provider on both sides.
    assert canonical_provider(session.source.provider) == "opencode"
    assert (
        classify_session(
            session.source.provider,
            session.source.source_id,
            session.first_user_turn,
            known_ids=((canonical_provider(session.source.provider), session.source.source_id),),
        )
        == CIAOBOT_OWN
    )
    # Nothing readable is `ambiguous`, and a caller must refuse that answer.
    assert (
        classify_session(
            session.source.provider, session.source.source_id, "", known_ids=()
        )
        == AMBIGUOUS
    )


# ── The bound, and every refusal ───────────────────────────────────────────


def test_an_export_over_the_cap_is_truncated_and_never_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the bound the payload is not parsed at all, and the session says so.

    An export is one JSON object, so there is no whole-message boundary to stop
    on the way to :data:`MAX_SESSION_BYTES`, and a half-recovered conversation
    presented as whole is worse than a shorter one that admits it is shorter. So
    the runner refuses the answer before :func:`json.loads` and the reader
    returns zero messages with ``truncated`` set and a ``truncated`` omission:
    what follows the cap is unknown, not empty.
    """
    oversize = json.dumps(
        {
            "info": {"id": "ses_oversize00000000000000000000"},
            "messages": [
                {
                    "id": "msg_000000000000000000000000000001",
                    "type": "user",
                    "text": "x" * (MAX_SESSION_BYTES + 1),
                    "time": {"created": 1772442860000},
                }
            ],
        }
    )
    _fake_subprocess(monkeypatch, stdout=oversize)

    session = read_opencode_session("ses_oversize00000000000000000000")

    assert session.messages == ()
    assert session.truncated is True
    assert session.omission_counts() == {OMISSION_TRUNCATED: 1}
    assert session.source.source_id == "ses_oversize00000000000000000000"


def test_the_runners_bound_is_the_contracts_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """The byte cap is ``MAX_SESSION_BYTES``, the constant the contract owns.

    Restating the number here would be a second promise to keep, and a
    disagreement between the two would be invisible until somebody read past the
    wrong one. The cap is also applied *before* the JSON parse, which is where an
    unbounded payload would actually be built in memory.
    """
    _fake_subprocess(monkeypatch, stdout=json.dumps({"info": {}, "messages": []}))

    with pytest.raises(SourceError) as refused:
        _run_opencode_json("/nonexistent/opencode", ["session", "list"], 1.0, max_bytes=4)

    assert refused.value.reason == "over_cap"


@pytest.mark.parametrize(
    ("returncode", "reason"),
    [(1, REASON_FAILED), (2, REASON_FAILED)],
    ids=("exit-1", "exit-2"),
)
def test_a_non_zero_exit_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, returncode: int, reason: str
) -> None:
    """A failing command is a refusal with a reason, never an empty session.

    "This session has nothing in it" and "the command that would read it failed"
    are different statements, and returning the first for the second would tell a
    consent screen that a user's history is empty when nothing was read at all.
    The first line of the CLI's own stderr is carried into the message, because
    that is the only thing an operator can act on.
    """
    _fake_subprocess(monkeypatch, returncode=returncode, stderr="no such session\n")

    with pytest.raises(SourceError) as refused:
        read_opencode_session("ses_missing0000000000000000000000")

    assert refused.value.reason == reason
    assert "no such session" in str(refused.value)


def test_a_command_that_does_not_answer_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout is refused as a timeout, and the bound is the one that fired.

    Both commands resolve — or start — a server connection, so they can genuinely
    hang. Waiting forever would hang an import scan with it, and answering with
    what did arrive would present a partial conversation as a whole one.
    """
    _fake_subprocess(monkeypatch, timeout=True)

    with pytest.raises(SourceError) as refused:
        read_opencode_session("ses_slow00000000000000000000000000")

    assert refused.value.reason == REASON_TIMEOUT


def test_output_that_is_not_json_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI that answers with anything but JSON is refused, not parsed as it.

    This is the case that makes ``session export`` unusable for import: a TTY
    prompt, a progress bar or a server error page all arrive on stdout with a
    zero exit code. Guessing at any of them would mean importing a fragment of
    something else.
    """
    _fake_subprocess(monkeypatch, stdout="Pass a session ID when running without an interactive terminal\n")

    with pytest.raises(SourceError) as refused:
        read_opencode_session("ses_synthetic00000000000000000000000")

    assert refused.value.reason == REASON_UNREADABLE


def test_an_unsupported_opencode_is_refused_and_never_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the V2 floor the adapter refuses instead of reading a familiar shape.

    The floor is the provider's own, so an importer and the engine cannot
    disagree about which opencode is supported. The point is not that a V1 export
    looks different — it is that the export schema this adapter was written
    against was read from the tagged source *at* the floor, so anything below it
    is a format nobody has verified, and importing it would be the guess the
    feasibility report rules out. An unparseable version is refused the same way:
    "unknown" is not a version.
    """
    _fake_subprocess(monkeypatch, version=UNSUPPORTED_VERSION)

    with pytest.raises(SourceError) as refused:
        read_opencode_session("ses_synthetic00000000000000000000000")

    assert refused.value.reason == REASON_UNSUPPORTED
    assert "2.0.16" in str(refused.value)


# ── The boundary: no CLI, no server, no session ────────────────────────────


def test_a_full_read_and_listing_never_shell_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With the bounded runner replaced, not one process is started.

    OpenCode is the one source that needs a *service* to be readable: both
    commands resolve (or start) a server connection. That makes it the source
    where a test shortcut is most tempting and most damaging — a real
    ``opencode`` here would reach the operator's own sessions and their
    database. So the replacement is total: :func:`subprocess.run` itself raises if
    anything reaches it, and a read and a listing still come back complete.

    This is the test that would fail if somebody later "helpfully" attached to a
    running server or read the SQLite file to skip the CLI, which is exactly the
    two things the design rules out.
    """
    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a test started a process: {args!r}")

    project = tmp_path / "workspace"
    project.mkdir()
    _fake_cli(monkeypatch, payload=_fixture("opencode_export_minimal.json"))
    monkeypatch.setattr(subprocess, "run", _forbidden)

    session = read_opencode_session("ses_7a1b2c3d4e5f60718293a4b5c6d7e8f9")
    _fake_cli(monkeypatch, payload=_fixture("opencode_list_page.json"))
    refs = discover_opencode_sessions(project)

    assert session.messages
    assert [ref.source_id for ref in refs] == [
        "ses_7a1b2c3d4e5f60718293a4b5c6d7e8f9",
        "ses_8b2c3d4e5f60718293a4b5c6d7e8f901",
        "ses_a0b1c2d3e4f5061728394a5b6c7d8e9",
    ]


def test_a_cli_that_is_not_installed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No binary is a refusal, and the version floor is never consulted for it.

    ``resolve_opencode_binary`` is the provider's own, so ``CIAO_OPENCODE_BIN``
    and the login-shell PATH mean here what they mean everywhere else. A missing
    CLI is "this source cannot be read", never "this session is empty".
    """
    monkeypatch.setattr(
        "ciao.import_sources.opencode.resolve_opencode_binary", lambda: None
    )

    with pytest.raises(SourceError) as refused:
        read_opencode_session("ses_synthetic00000000000000000000000")

    assert refused.value.reason == REASON_UNAVAILABLE


def test_a_binary_that_cannot_be_executed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH entry that is not runnable is "installed but broken".

    ``resolve_opencode_binary`` can raise ``OSError`` for a PATH entry that is a
    directory or a wrapper whose entry point is missing. Reporting that as "not
    installed" would send the operator to install something they already have, so
    it is reported as what happened.
    """
    def _broken() -> str:
        raise OSError("Permission denied")

    monkeypatch.setattr(
        "ciao.import_sources.opencode.resolve_opencode_binary", _broken
    )

    with pytest.raises(SourceError) as refused:
        read_opencode_session("ses_synthetic00000000000000000000000")

    assert refused.value.reason == REASON_UNAVAILABLE
    assert "Permission denied" in str(refused.value)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"messages": []},
        {"info": {"title": "no id"}, "messages": []},
        {"info": {"id": "ses_synthetic00000000000000000000000"}},
        {"info": {"id": "ses_synthetic00000000000000000000000"}, "messages": {}},
    ],
    ids=("not-an-object", "no-info", "no-info-id", "no-messages", "messages-not-a-list"),
)
def test_a_payload_that_is_not_the_declared_shape_is_refused(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    """``{info, messages}`` is checked, not assumed.

    An export missing its ``info`` id cannot be cited against a source, and one
    whose ``messages`` is not a list is not the schema the mapping was written
    against. Both are refused for a stated reason rather than partially trusted —
    a reader that imported the half it understood would be reporting what it
    found as what the session said.
    """
    _fake_cli(monkeypatch, payload=payload)

    with pytest.raises(SourceError) as refused:
        read_opencode_session("ses_synthetic00000000000000000000000")

    assert refused.value.reason == REASON_UNREADABLE


def test_an_empty_session_id_is_refused_before_the_cli_is_touched() -> None:
    """A session with no id is not a session, and nothing is run for it."""
    with pytest.raises(SourceError) as refused:
        read_opencode_session("   ")

    assert refused.value.reason == REASON_UNREADABLE


def test_records_this_reader_cannot_anchor_or_read_are_still_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two shapes of unusable record, two counts, and the conversation survives.

    An array element that is not an object is corruption and is counted as
    unreadable; a well-formed object with no id has nothing to cite it by and is
    counted with the other non-message records. Neither takes the turns beside it
    down with it — that is the difference between a degraded read and a refusal.
    """
    payload = _fixture("opencode_export_minimal.json")
    payload["messages"] = [
        "not an object",
        {"type": "user", "text": "a record with no id"},
        *payload["messages"],
    ]
    _fake_cli(monkeypatch, payload=payload)

    session = read_opencode_session("ses_synthetic00000000000000000000000")

    assert [message.role for message in session.messages] == [ROLE_USER, ROLE_ASSISTANT]
    assert session.omission_counts() == {
        OMISSION_UNREADABLE_LINE: 1,
        OMISSION_ENTRY_TYPE: 1,
    }


# ── Discovery ──────────────────────────────────────────────────────────────


def test_discovery_lists_metadata_only_and_states_the_max_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A listing is ids and directories, and a full page says it is a full page.

    ``session list`` has no cursor and no offset, so one call is all there is: a
    project holding more root sessions than the cap has sessions this scan cannot
    see, and a result that looked complete would be a lie. So the cap is passed
    explicitly rather than inherited from the CLI's own default, and a page that
    filled it is logged. ``path`` is empty for the same reason the export's is —
    there is no file behind a listing — and no message is fetched, so discovery
    cannot read a conversation before the user has chosen one.
    """
    project = tmp_path / "workspace"
    project.mkdir()
    rows = _fixture("opencode_list_page.json")
    calls = _fake_cli(monkeypatch, payload=rows)
    caplog.set_level(logging.WARNING, logger="ciao.import_sources.opencode")

    refs = discover_opencode_sessions(project)

    assert calls == [
        (
            "session",
            "list",
            "--max-count",
            str(DISCOVERY_MAX_COUNT),
            "--format",
            "json",
        ),
    ]
    assert [(ref.provider, ref.source_id, ref.project_hint, ref.path) for ref in refs] == [
        (
            PROVIDER_OPENCODE,
            "ses_7a1b2c3d4e5f60718293a4b5c6d7e8f9",
            "/synthetic/workspace/release-checklist",
            "",
        ),
        (
            PROVIDER_OPENCODE,
            "ses_8b2c3d4e5f60718293a4b5c6d7e8f901",
            "/synthetic/workspace/release-checklist",
            "",
        ),
        (
            PROVIDER_OPENCODE,
            "ses_a0b1c2d3e4f5061728394a5b6c7d8e9",
            "/synthetic/workspace/other-notes",
            "",
        ),
    ]
    assert "reached" not in caplog.text
    assert DISCOVERY_MAX_COUNT == 100


def test_discovery_asks_for_the_given_directory_as_its_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The working directory is the project, because that is how the CLI scopes.

    ``session list`` resolves its project from ``process.cwd()``. Run from
    anywhere else it answers with a *different* project's sessions, under the
    name of the one asked about — the worst kind of wrong for a consent screen,
    which would show sessions the user never chose.
    """
    project = tmp_path / "workspace"
    project.mkdir()
    seen: list[str | None] = []

    monkeypatch.setattr(
        "ciao.import_sources.opencode._resolve_binary",
        lambda binary: SYNTHETIC_BINARY,
    )
    monkeypatch.setattr(
        "ciao.import_sources.opencode._require_supported_v2",
        lambda binary, *, timeout: SUPPORTED_VERSION,
    )

    def _run(binary: str, args: Sequence[str], timeout: float, **kwargs: object) -> Any:
        seen.append(kwargs.get("cwd"))
        return _fixture("opencode_list_page.json")

    monkeypatch.setattr("ciao.import_sources.opencode._run_opencode_json", _run)

    discover_opencode_sessions(project)

    assert seen == [str(project)]


def test_a_full_page_is_reported_as_a_full_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A page that reached the cap is logged, because the scan is not complete.

    The list is silently truncated to the most recent sessions and there is no
    cursor to ask for the next page, so a full page means "there are older
    sessions this scan did not see". Saying so is the whole difference between a
    bounded scan and a claim to completeness, and it is why the cap is a recorded
    decision rather than a default inherited from the CLI.
    """
    project = tmp_path / "workspace"
    project.mkdir()
    full = [
        {"id": f"ses_synthetic{i:020d}", "directory": "/synthetic/workspace"}
        for i in range(DISCOVERY_MAX_COUNT)
    ]
    _fake_cli(monkeypatch, payload=full)
    caplog.set_level(logging.WARNING, logger="ciao.import_sources.opencode")

    refs = discover_opencode_sessions(project)

    assert len(refs) == DISCOVERY_MAX_COUNT
    assert "--max-count" in caplog.text
    assert str(DISCOVERY_MAX_COUNT) in caplog.text


def test_discovery_in_a_workspace_opencode_never_ran_is_an_empty_list(
    tmp_path: Path,
) -> None:
    """A directory that is not there is an empty list, and nothing is run.

    A workspace nobody used opencode in is the ordinary case, not an error, and
    it is the one answer that needs no CLI: the cap on what a scan may claim is
    not the point of refusing to look where there is nothing to look.
    """
    assert discover_opencode_sessions(tmp_path / "never-used") == []


def test_discovery_refuses_a_listing_it_could_not_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed listing raises, because a scan that saw nothing must not say so.

    An empty list is a claim — that the project holds no sessions. A command that
    failed, timed out, answered with something other than a list, or ran against
    an opencode below the floor has established nothing of the kind, and
    returning ``[]`` would report the failure as the user's history being absent.
    """
    project = tmp_path / "workspace"
    project.mkdir()

    _fake_cli(monkeypatch, reason=REASON_UNAVAILABLE)
    with pytest.raises(SourceError):
        discover_opencode_sessions(project)

    _fake_cli(monkeypatch, payload={"sessions": []})
    with pytest.raises(SourceError) as refused:
        discover_opencode_sessions(project)
    assert refused.value.reason == REASON_UNREADABLE


def test_discovery_skips_a_row_it_cannot_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A row with no id is not offered to the user to import.

    The contract refuses a :class:`SourceRef` that names no session, so a row like
    that cannot be represented. It is skipped and logged rather than guessed at
    from a title or a directory, and the sessions around it still list.
    """
    project = tmp_path / "workspace"
    project.mkdir()
    _fake_cli(
        monkeypatch,
        payload=[
            {"title": "no id here", "directory": "/synthetic/workspace"},
            "not an object",
            {"id": "ses_synthetic00000000000000000000000", "directory": None},
        ],
    )

    refs = discover_opencode_sessions(project)

    assert [ref.source_id for ref in refs] == ["ses_synthetic00000000000000000000000"]
    assert refs[0].project_hint == ""


def test_an_explicit_binary_is_used_and_an_empty_one_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit path is the seam for an unusual install; a blank one is nothing."""
    project = tmp_path / "workspace"
    project.mkdir()
    seen: list[str] = []

    monkeypatch.setattr(
        "ciao.import_sources.opencode._require_supported_v2",
        lambda binary, *, timeout: binary,
    )

    def _run(binary: str, args: Sequence[str], timeout: float, **_: object) -> Any:
        seen.append(binary)
        return []

    monkeypatch.setattr("ciao.import_sources.opencode._run_opencode_json", _run)

    discover_opencode_sessions(project, binary="/opt/synthetic/opencode")

    assert seen == ["/opt/synthetic/opencode"]
    with pytest.raises(SourceError) as refused:
        discover_opencode_sessions(project, binary="  ")
    assert refused.value.reason == REASON_UNAVAILABLE