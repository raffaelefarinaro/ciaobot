"""The managed `verify_note` operation (#726-D): scoping, and the wiring #726-C
was written for and had no caller.

`tests/test_note_verification.py` owns the autonomy rule and the check state,
`tests/test_note_edit_proposals.py` owns the proposal kind. What is pinned here
is the operation between them, and it is pinned on the properties that only exist
once the two are joined:

* **A verdict travels as a file, never as prose on a command line.** The payload
  is confined to the caller's own workspace root, capped, and refused when it is
  not a JSON object — a verification carries a note's full before/after text and
  a list of citations, all of it arbitrary user prose.
* **It cannot reach outside its scope.** A payload naming another workspace is
  refused, because the check state and the sidecar are filed per workspace and
  pairing one workspace's name with another's vault records a verdict about a
  vault nobody claimed.
* **A `needs_review` verdict becomes exactly one `note_edit` proposal, and the
  check is pinned to its queue row.** This is the gap `docs/UPKEEP.md` carried:
  the kind was complete and nothing in production produced one, so a refused
  verdict recorded a check and asked nobody. Auto-applied verdicts file nothing,
  and *every* way the filing can fail is reported rather than raised — a failure
  after the check is recorded is the case where a verdict exists and nobody was
  asked, which is the exact failure this child exists to remove.
* **Retirement is never applied.** It reaches the same `needs_review` a note with
  no frontmatter reaches, and its proposal is a human click all the way down.
* **An oversized input is `unverified`, not `applied`.** A note nobody could read
  in full has not been verified by anyone; reporting it as a completed
  verification would pin a verdict about text nobody showed the service.
* **Coalescing is per question, not per note.** Two callers about the same
  revision of the same note share the off-loop read only when they ask the same
  question; a second caller's different verdict must be evaluated, not answered
  with the first caller's.
* **Provenance follows the turn.** An interactive verification is not journaled
  as the nightly job.

Everything here runs against a synthetic vault under `tmp_path` and a config
that answers where its notes live. No real vault, engine, service or scheduler is
started, and nothing outside `tmp_path` is written.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import control_plane as cp
from ciao import memory_receipts as mr
from ciao import note_edit_proposals as nep
from ciao import note_verification as nv

NOTE = "People/Sofia.md"
TODAY = date(2026, 3, 14)

PLAIN = (
    "---\ntype: person\nupdated: 2024-01-05\n---\n\n"
    "# Sofia\n\nSofia runs the release train.\n"
)
FOURTH = PLAIN.replace("release train", "platform team")

CITATION = nv.Evidence(
    source_type="chat",
    source_ref="chat-2026-03-02",
    quoted="Sofia moved to the platform team",
    supports=f"{NOTE}: who Sofia runs",
    observed_at="2026-03-02",
).as_dict()
UNCITED = nv.Evidence(
    source_type="chat",
    source_ref="",
    quoted="I think she moved teams",
    supports=f"{NOTE}: who Sofia runs",
).as_dict()


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every lock file out of the shared temp root the install uses."""
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture(autouse=True)
def _executor() -> Any:
    """A fresh off-loop executor per test, so no read outlives one."""
    from ciao import async_reads

    async_reads.reset_vault_read_executor()
    yield
    async_reads.reset_vault_read_executor()


class _Chat:
    mode = "auto"
    user_turn_count = 1
    user_turn_unattended: dict[str, bool] = {}

    def __init__(self, *, unattended: bool = False) -> None:
        if unattended:
            # One turn, and the flag for the turn being asked right now — which
            # is what makes it a scheduled turn rather than a chat that has ever
            # been scheduled.
            self.user_turn_unattended = {"0": True}


class _Install:
    """The three answers `verify_note` asks this install for.

    `agent_root` is the workspace directory the payload file must live under,
    `workspace_vault_root` is the vault the notes are in, and `workspace` is the
    one the principal is scoped to — the same trio `file_surface` and
    `memory_update` use.
    """

    def __init__(self, root: Path, vault: Path, workspace: str = "personal") -> None:
        self._root = root
        self._vault = vault
        self._workspace = workspace
        self.state_path = str(root / ".runtime" / "state.json")

    def agent_root(self, workspace: str) -> Path:
        assert workspace == self._workspace
        return self._root

    def workspace_vault_root(self, workspace: str) -> Path:
        assert workspace == self._workspace
        return self._vault

    def workspace(self, name: str) -> Any:
        return SimpleNamespace(name=name) if name == self._workspace else None


def _install(tmp_path: Path, *, text: str = PLAIN) -> tuple[_Install, Path, Path]:
    """A workspace with one note in it; returns (install, workspace root, note path)."""
    root = tmp_path / "ws"
    vault = root / "memory-vault"
    (vault / "People").mkdir(parents=True)
    (vault / "Workspace").mkdir(parents=True)
    note = vault / NOTE
    note.write_bytes(text.encode("utf-8"))
    return _Install(root, vault), root, note


def _principal(workspace: str = "personal") -> cp.AgentPrincipal:
    return cp.AgentPrincipal(
        token_id="t1",
        chat_id="chat-1",
        project_id="p1",
        workspace=workspace,
        provider="claude",
    )


class _Plane:
    """A control plane with the one chat `chat_mode` reads."""

    def __init__(self, install: _Install, *, mode: str = "auto", unattended: bool = False) -> None:
        self.config = install
        self.pcm = SimpleNamespace(get_chat=lambda _chat_id: _Chat(unattended=unattended))
        self._mode = mode

    def chat_mode(self, _principal: cp.AgentPrincipal) -> str:
        return self._mode

    def _workspace(self, principal: cp.AgentPrincipal, requested: str = "") -> str:
        workspace = requested.strip() or principal.workspace
        if not workspace:
            raise cp.ControlPlaneError("workspace_required", "No active workspace.")
        if workspace != principal.workspace:
            raise cp.ControlPlaneError(
                "workspace_forbidden", f"scoped to '{principal.workspace}'"
            )
        if self.config.workspace(workspace) is None:
            raise cp.ControlPlaneError("workspace_not_found", workspace)
        return workspace

    def _safe_relative(self, root: Path, relative_path: str, *, must_exist: bool = False) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or "\x00" in relative_path:
            raise cp.ControlPlaneError("invalid_path", "Use a relative path inside the active root.")
        target = (root / raw).resolve()
        if not target.is_relative_to(root.resolve()):
            raise cp.ControlPlaneError("path_forbidden", "outside the active root")
        if must_exist and not target.exists():
            raise cp.ControlPlaneError("file_not_found", relative_path)
        return target

    def _search_runtime_dir(self) -> Path:
        return Path(self.config.state_path).parent

    verify_note = cp.CiaoControlPlane.verify_note
    _verify_one_entry = cp.CiaoControlPlane._verify_one_entry
    _verification_payload = cp.CiaoControlPlane._verification_payload
    _unattended_turn = cp.CiaoControlPlane._unattended_turn


def _plane(install: _Install, *, mode: str = "auto", unattended: bool = False) -> _Plane:
    return _Plane(install, mode=mode, unattended=unattended)


def _payload(root: Path, fields: dict[str, Any], *, name: str = "verify.json") -> str:
    (root / name).write_text(json.dumps(fields), encoding="utf-8")
    return name


def _verdict(vault: Path, note: Path, **overrides: Any) -> dict[str, Any]:
    """A well-formed `still_valid` payload for the note as it stands on disk."""
    fields: dict[str, Any] = {
        "relative_path": NOTE,
        "expected_revision": mr.content_revision(note.read_bytes().decode("utf-8")),
        "outcome": nv.STILL_VALID,
        "coverage": nv.COVERAGE_COMPLETE,
        "evidence": [CITATION],
        "reason": "the March release notes still name her",
    }
    fields.update(overrides)
    return fields


def _queue(vault: Path) -> str:
    try:
        return (vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _sidecars(vault: Path) -> list[str]:
    directory = vault / "Workspace" / "Memory-Note-Edit-Proposals"
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


# ── Scoping: the payload is a file, inside this workspace ──────────────────


def test_prose_never_arrives_as_an_argument(tmp_path: Path) -> None:
    """The operation takes a path. There is no argument a verdict's text could use.

    Not a style preference: the payload is a note's full before/after text plus
    its citations, and as a shell argument it would be mangled by `$()`,
    backticks and quotes and would sit in the process table besides.
    """
    install, root, note = _install(tmp_path)
    plane = _plane(install)

    with pytest.raises(cp.ControlPlaneError) as excinfo:
        import asyncio

        asyncio.run(plane.verify_note(_principal(), payload_file=""))
    assert excinfo.value.code == "payload_required"

    # And the tool schema itself offers nothing but the path: no argument exists
    # through which a verdict's text could reach the service.
    import inspect

    from ciao import mcp_server

    operation = mcp_server.OPERATIONS_BY_NAME["verify_note"]
    parameters = inspect.signature(operation.fn).parameters
    assert set(parameters) == {"service", "payload_file"}
    assert parameters["payload_file"].default == ""
    # The CLI offers the same one flag, and no way to pass prose.
    from ciao import agent_cli

    args = agent_cli.build_parser().parse_args(["note", "verify", "--payload-file", "v.json"])
    assert agent_cli.resolve(args) == ("verify_note", {"payload_file": "v.json"})


def test_a_payload_outside_the_workspace_root_is_refused(tmp_path: Path) -> None:
    """Sibling workspaces live beside this one; the read must not cross."""
    import asyncio

    install, _root, _note = _install(tmp_path)
    plane = _plane(install)
    outside = tmp_path / "other-ws"
    outside.mkdir()
    (outside / "verify.json").write_text(json.dumps(_verdict(install._vault, install._vault / NOTE)), encoding="utf-8")

    with pytest.raises(cp.ControlPlaneError) as excinfo:
        asyncio.run(plane.verify_note(_principal(), payload_file="../other-ws/verify.json"))
    assert excinfo.value.code == "path_forbidden"
    assert _sidecars(install._vault) == []


def test_a_payload_naming_another_workspace_is_refused(tmp_path: Path) -> None:
    """The check state and the sidecar are filed per workspace.

    Pairing one workspace's name with another's vault would record a verdict in a
    vault nobody claimed and pin the wrong note's cooldown — so the payload's
    `workspace` must restate the caller's own, or say nothing at all.
    """
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(root, _verdict(install._vault, note, workspace="someone-elses"))

    with pytest.raises(cp.ControlPlaneError) as excinfo:
        asyncio.run(plane.verify_note(_principal(), payload_file=name))
    assert excinfo.value.code == "workspace_forbidden"
    assert nv.read_note_checks(install._vault) == {}


def test_a_non_payload_document_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """Malformed JSON, a non-object, an unknown outcome and a bad coverage all
    fail at the boundary, and none of them reaches the vault."""
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    good = _verdict(install._vault, note)

    cases: list[tuple[str, str, Any]] = [
        ("broken.json", "{not json", "payload_invalid"),
        ("list.json", json.dumps([good]), "payload_invalid"),
        ("outcome.json", json.dumps({**good, "outcome": "delete"}), "payload_invalid"),
        ("missing.json", json.dumps({"relative_path": NOTE}), "payload_invalid"),
        (
            "coverage.json",
            json.dumps({**good, "coverage": "mostly"}),
            "payload_invalid",
        ),
        ("typed.json", json.dumps({**good, "reason": ["a", "b"]}), "payload_invalid"),
    ]
    for name, text, code in cases:
        (root / name).write_text(text, encoding="utf-8")
        with pytest.raises(cp.ControlPlaneError) as excinfo:
            asyncio.run(plane.verify_note(_principal(), payload_file=name))
        assert excinfo.value.code == code, name
    assert note.read_bytes() == PLAIN.encode("utf-8")
    assert _sidecars(install._vault) == []
    assert nv.read_note_checks(install._vault) == {}


def test_an_oversized_payload_is_refused(tmp_path: Path) -> None:
    """A cap on work one call can ask of the server, checked before the read."""
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    (root / "huge.json").write_text(
        json.dumps(_verdict(install._vault, note, reason="x" * (cp.MAX_VERIFY_PAYLOAD_BYTES + 1))),
        encoding="utf-8",
    )

    with pytest.raises(cp.ControlPlaneError) as excinfo:
        asyncio.run(plane.verify_note(_principal(), payload_file="huge.json"))
    assert excinfo.value.code == "payload_too_large"
    assert note.read_bytes() == PLAIN.encode("utf-8")


def test_plan_mode_refuses_the_operation(tmp_path: Path) -> None:
    """The gate is the existing one, and it is reached through the operation's
    own `mutating=True` annotation rather than a second check here."""
    from ciao import mcp_server

    operation = mcp_server.OPERATIONS_BY_NAME["verify_note"]
    assert operation.annotations.readOnlyHint is False
    assert operation.annotations.destructiveHint is False
    # A plan-mode chat is refused by the service's shared gate.
    install, root, note = _install(tmp_path)
    assert _plane(install, mode="plan").chat_mode(_principal()) == "plan"


# ── The wiring: needs_review files exactly one proposal and pins the check ──


def test_a_needs_review_verdict_files_one_proposal_and_pins_the_check(
    tmp_path: Path,
) -> None:
    """Acceptance: the gap #726-D exists to close.

    The uncited `update` is the honest refusal: an agent's replacement that no
    citation carries. Before this wiring it recorded a check and asked nobody.
    Now exactly one `note_edit` row is queued, and the check carries that row's
    id, which is the only thing keeping the note off a second proposal.
    """
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(
        root,
        _verdict(
            install._vault,
            note,
            outcome=nv.UPDATE,
            before=PLAIN,
            after=FOURTH,
            evidence=[UNCITED],
        ),
    )

    envelope = asyncio.run(plane.verify_note(_principal(), payload_file=name))

    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["status"] == nv.NEEDS_REVIEW
    assert data["auto_applied"] is False
    # The note itself is untouched: the whole point of a refused verdict.
    assert data["receipt_id"] == ""
    assert note.read_bytes() == PLAIN.encode("utf-8")
    # Exactly one sidecar, one queue row.
    sidecars = _sidecars(install._vault)
    assert len(sidecars) == 1
    queue = _queue(install._vault)
    assert queue.count("- [note_edit ") == 1
    # And the check is pinned to that row. The id is the queue ROW's, not the
    # sidecar's, and the row is found by parsing the queue — which is the same
    # lookup the accept takes.
    from ciao import proposal_tracking

    entries = [
        entry
        for entry in proposal_tracking.walk_proposal_queue(
            "personal", "personal/Workspace/Memory-Proposals.md", queue
        )
        if entry.bullet.kind == nep.KIND
    ]
    assert len(entries) == 1
    check = nv.read_note_checks(install._vault)[NOTE]
    assert check.proposal_id == entries[0].proposal_id
    assert data["check"]["proposal_id"] == check.proposal_id
    assert data["proposal"]["operation"] == nep.REPLACE
    assert data["proposal"]["relative_path"] == NOTE
    assert data["proposal"]["queued"] is True
    # The plan's own reason is what the reviewer is shown: it names which gap
    # sent this to a person.
    assert "empty source_ref" in data["message"]


def test_a_retirement_is_never_applied_and_asks_a_person(tmp_path: Path) -> None:
    """Acceptance: retirement never auto-applies.

    `note_verification` cannot reach a delete primitive, and the operation does
    not add one: a `retire` verdict comes back `needs_review` and becomes a
    `retire` proposal, whose accept is an attended human click.
    """
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(
        root,
        _verdict(install._vault, note, outcome=nv.RETIRE, evidence=[CITATION]),
    )

    data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert data["status"] == nv.NEEDS_REVIEW
    assert data["auto_applied"] is False
    assert note.read_bytes() == PLAIN.encode("utf-8")
    assert data["proposal"]["operation"] == nep.RETIRE
    assert nv.read_note_checks(install._vault)[NOTE].proposal_id


def test_a_restamp_with_no_frontmatter_becomes_a_restamp_proposal(
    tmp_path: Path,
) -> None:
    """The third `needs_review` shape, and the one that is not a rewrite.

    A note with no `updated:` to stamp cannot be re-stamped without
    restructuring a file the service was asked to verify, so it is asked about
    instead — as a `restamp`, which computes its own bytes at accept time.
    """
    import asyncio

    bare = "# Sofia\n\nSofia runs the release train.\n"
    install, root, note = _install(tmp_path, text=bare)
    plane = _plane(install)
    name = _payload(root, _verdict(install._vault, note))

    data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert data["status"] == nv.NEEDS_REVIEW
    assert data["proposal"]["operation"] == nep.RESTAMP
    assert note.read_text(encoding="utf-8") == bare


def test_an_auto_applied_verdict_files_no_proposal(tmp_path: Path) -> None:
    """A cited `still_valid` from complete coverage re-stamps `updated:` and
    asks nobody. One receipt, one check, and an empty queue."""
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(root, _verdict(install._vault, note))

    data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert data["status"] == nv.APPLIED
    assert data["auto_applied"] is True
    assert data["receipt_id"]
    assert "proposal" not in data or data["proposal"] is None
    assert _sidecars(install._vault) == []
    assert "[note_edit]" not in _queue(install._vault)
    stamped = note.read_text(encoding="utf-8")
    assert f"updated: {date.today().isoformat()}" in stamped
    # The check describes the revision the note was LEFT at, so the next pass
    # sees a check for the text it will read.
    check = nv.read_note_checks(install._vault)[NOTE]
    assert check.content_revision == mr.content_revision(stamped)
    assert check.proposal_id == ""


def test_an_oversized_note_is_unverified_not_applied(tmp_path: Path) -> None:
    """A note nobody could read in full has not been verified by anyone.

    Reporting it as `applied` would pin a verdict about text nobody showed the
    service, so the honest `unknown` is the answer — and the note and the check
    state are both left alone.
    """
    import asyncio

    install, root, _note = _install(tmp_path)
    big = install._vault / NOTE
    big.write_text("x" * (cp.MAX_VERIFY_NOTE_BYTES + 1), encoding="utf-8")
    plane = _plane(install)
    name = _payload(
        root,
        {
            "relative_path": NOTE,
            "expected_revision": mr.content_revision(
                big.read_bytes().decode("utf-8")
            ),
            "outcome": nv.STILL_VALID,
            "coverage": nv.COVERAGE_COMPLETE,
            "evidence": [CITATION],
            "reason": "x",
        },
    )

    data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert data["status"] == nv.UNVERIFIED
    assert data["auto_applied"] is False
    assert "cap" in data["message"]
    assert nv.read_note_checks(install._vault) == {}
    assert _sidecars(install._vault) == []


def test_a_stale_revision_is_a_conflict_and_writes_nothing(tmp_path: Path) -> None:
    """The caller read text that is no longer there: nothing is written and no
    check is recorded, so it can re-read and judge the note that exists."""
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(
        root,
        _verdict(install._vault, note, expected_revision=mr.content_revision("stale")),
    )

    data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert data["status"] == nv.CONFLICT
    assert data["check"] is None
    assert note.read_bytes() == PLAIN.encode("utf-8")
    assert nv.read_note_checks(install._vault) == {}


def test_a_filing_failure_is_reported_rather_than_hidden(tmp_path: Path) -> None:
    """The verdict stands, but nobody was asked — and that must be visible.

    A `needs_review` with a filed proposal and a `needs_review` whose filing
    failed look identical from outside unless the reply distinguishes them, and
    the second is the failure mode the whole child exists to remove. So a filing
    that raises is reported in its own field, the check stays recorded (the note
    genuinely was judged), and the queue is left empty rather than looking
    answered.
    """
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(
        root, _verdict(install._vault, note, outcome=nv.RETIRE, evidence=[CITATION])
    )

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise nep.NoteEditRefused("the proposal queue is read-only")

    monkey = nep.file_note_edit
    nep.file_note_edit = refuse  # type: ignore[assignment]
    try:
        data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]
    finally:
        nep.file_note_edit = monkey  # type: ignore[assignment]

    assert data["status"] == nv.NEEDS_REVIEW
    assert data["proposal"] is None
    assert "could not be filed" in data["proposal_error"]
    assert "read-only" in data["proposal_error"]
    # The verdict is still on the record; only the asking failed.
    assert nv.read_note_checks(install._vault)[NOTE].outcome == nv.RETIRE
    assert _sidecars(install._vault) == []


def test_a_second_pass_over_the_same_refused_note_files_nothing_new(
    tmp_path: Path,
) -> None:
    """The pinned `proposal_id` is what stops a second row, and it is the reason
    the filing re-reads the check before replying.

    A nightly run that reaches the same verdict about the same text twice must
    leave the owner ONE question, not two: the second pass sees the check
    settled by a pending proposal and answers `already_checked` without writing.
    """
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(
        root, _verdict(install._vault, note, outcome=nv.RETIRE, evidence=[CITATION])
    )

    first = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]
    second = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert first["status"] == nv.NEEDS_REVIEW
    assert first["check"]["proposal_id"] == first["proposal"]["proposal_id"]
    assert second["status"] == nv.ALREADY_CHECKED
    assert second["proposal"] is None
    assert len(_sidecars(install._vault)) == 1
    assert _queue(install._vault).count("- [note_edit ") == 1


def test_a_note_path_that_leaves_the_vault_is_refused(tmp_path: Path) -> None:
    """The service's own path confinement, reached rather than reimplemented.

    A `..` in the payload is not this operation's refusal to make — the whole
    point of calling `note_verification.verify_note` is that its vault-relative
    rule, its symlink walk and its `.md` check still apply. What matters is that
    nothing was written and no check was recorded on the way to the refusal.
    """
    import asyncio

    install, root, _note = _install(tmp_path)
    outside = tmp_path / "escape.md"
    outside.write_text("---\ntype: person\n---\n\n# Escape\n", encoding="utf-8")
    plane = _plane(install)
    name = _payload(
        root, _verdict(install._vault, install._vault / NOTE, relative_path="../escape.md")
    )

    data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert data["status"] == nv.FAILED
    assert data["check"] is None
    assert nv.read_note_checks(install._vault) == {}
    assert outside.read_text(encoding="utf-8") == "---\ntype: person\n---\n\n# Escape\n"


def test_re_verifying_a_settled_revision_is_a_no_op(tmp_path: Path) -> None:
    """The cooldown is the reason the plan can re-ask tomorrow without asking
    twice tonight, and it is reported rather than silently re-applied."""
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(root, _verdict(install._vault, note))

    first = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]
    second = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert first["status"] == nv.APPLIED
    assert second["status"] == nv.ALREADY_CHECKED
    assert second["auto_applied"] is False
    assert _sidecars(install._vault) == []


# ── Two callers, one note: the read is coalesced per question ──────────────


def test_a_different_verdict_about_the_same_revision_is_not_answered_by_the_first(
    tmp_path: Path,
) -> None:
    """Coalescing exists so two agents cannot race each other to write the same
    note. Keyed on the note alone it also threw away the *question*: a second
    caller's `retire` joined an in-flight `still_valid`, was handed that reply,
    and its own outcome, evidence and text were never evaluated by anything.

    The reply is the dangerous half: a caller that asked to retire a note and was
    told `applied` believes a decision was taken that no rule ever reached, and
    the queue holds no row for it.

    The first read is held open so the two really are in flight at once — the read
    executor is bounded and does share work by key, so overlap is the only way
    this property exists at all. That ordering is also what makes the assertions
    below deterministic: the second call's read is admitted while the first
    waits, so the retire verdict is recorded first and the re-stamp that unblocks
    finds the revision already settled.
    """
    import asyncio
    import threading

    from ciao import note_verification as service

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    still = _payload(root, _verdict(install._vault, note))
    retire = _payload(
        root,
        _verdict(install._vault, note, outcome=nv.RETIRE, evidence=[CITATION]),
        name="retire.json",
    )

    entered = threading.Event()
    release = threading.Event()
    seen: list[str] = []
    verify = service.verify_note

    def hold_first_open(request: Any, **kwargs: Any) -> Any:
        seen.append(str(request.outcome))
        if len(seen) == 1:
            entered.set()
            # Bounded: a key that *is* coalesced makes the second caller wait for
            # this one, and the test would rather take the wait than hang.
            release.wait(2.0)
        return verify(request, **kwargs)

    service.verify_note = hold_first_open  # type: ignore[assignment]
    try:

        async def both() -> tuple[dict[str, Any], dict[str, Any]]:
            first = asyncio.create_task(plane.verify_note(_principal(), payload_file=still))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            second = await plane.verify_note(_principal(), payload_file=retire)
            release.set()
            return await first, second

        first, second = asyncio.run(both())
    finally:
        release.set()
        service.verify_note = verify  # type: ignore[assignment]

    # Both payloads reached the rule: the second was evaluated, not short-circuited.
    assert sorted(seen) == sorted([nv.STILL_VALID, nv.RETIRE])
    # The retire caller was told its own verdict, and there is a row for it.
    assert second["data"]["status"] == nv.NEEDS_REVIEW
    assert second["data"]["proposal"]["operation"] == nep.RETIRE
    assert _queue(install._vault).count("- [note_edit ") == 1
    # The re-stamp is told the revision was settled while it waited rather than
    # being handed the retirement's answer, and writes nothing over it.
    assert first["data"]["status"] == nv.ALREADY_CHECKED
    assert note.read_text(encoding="utf-8").count("updated: 2024-01-05") == 1


def test_an_identical_verdict_does_share_one_read(tmp_path: Path) -> None:
    """The other half, so the fix is not "stop coalescing".

    Two callers asking the same question about the same revision is exactly the
    race coalescing exists for: one read, one write, one receipt, and both
    callers told the same true thing.
    """
    import asyncio
    import threading

    from ciao import note_verification as service

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(root, _verdict(install._vault, note))

    entered = threading.Event()
    release = threading.Event()
    calls = 0
    verify = service.verify_note

    def count_calls(request: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(2.0)
        return verify(request, **kwargs)

    service.verify_note = count_calls  # type: ignore[assignment]
    try:

        async def both() -> tuple[dict[str, Any], dict[str, Any]]:
            first = asyncio.create_task(plane.verify_note(_principal(), payload_file=name))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            second = await plane.verify_note(_principal(), payload_file=name)
            release.set()
            return await first, second

        first, second = asyncio.run(both())
    finally:
        release.set()
        service.verify_note = verify  # type: ignore[assignment]

    assert calls == 1
    assert first["data"]["status"] == second["data"]["status"] == nv.APPLIED
    assert first["data"]["receipt_id"] == second["data"]["receipt_id"]


@pytest.mark.parametrize("failure", [mr.QueueLockError, OSError])
def test_a_filing_failure_the_caller_could_not_see_is_still_reported(
    tmp_path: Path, failure: type[BaseException]
) -> None:
    """`file_note_edit` can fail on more than a refusal.

    A lock it cannot take and a filesystem that says no were not in the caught
    set, so they escaped the operation as an `internal_error` — after
    `note_verification` had already recorded the check and its cooldown. The
    agent was told the call failed, the queue held no row, and nothing in the
    reply said a verdict existed: the note was judged, the owner was never asked,
    and the one state that said so was the one the caller could not read.
    """
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install)
    name = _payload(
        root, _verdict(install._vault, note, outcome=nv.RETIRE, evidence=[CITATION])
    )

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise failure("the queue lock is held by another process")

    monkey = nep.file_note_edit
    nep.file_note_edit = refuse  # type: ignore[assignment]
    try:
        data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]
    finally:
        nep.file_note_edit = monkey  # type: ignore[assignment]

    assert data["status"] == nv.NEEDS_REVIEW
    assert data["proposal"] is None
    assert "could not be filed" in data["proposal_error"]
    # The verdict is still on the record; only the asking failed.
    assert nv.read_note_checks(install._vault)[NOTE].outcome == nv.RETIRE
    assert _sidecars(install._vault) == []


# ── Provenance: whose decision was this? ───────────────────────────────────


@pytest.mark.parametrize(
    ("unattended", "source"),
    [(False, "chat"), (True, "curation")],
)
def test_provenance_follows_the_turn_not_a_fixed_label(
    tmp_path: Path, unattended: bool, source: str
) -> None:
    """A receipt says who decided.

    `source="curation"` on every call filed an interactive verification as the
    nightly job, so History showed the owner a decision the schedule made during
    a conversation they were part of. The turn is the answer: an unattended
    schedule's turn is the curation run, and an attended chat's is the chat.
    """
    import asyncio

    install, root, note = _install(tmp_path)
    plane = _plane(install, unattended=unattended)
    name = _payload(root, _verdict(install._vault, note))

    data = asyncio.run(plane.verify_note(_principal(), payload_file=name))["data"]

    assert data["status"] == nv.APPLIED
    journal = mr.journal_path(install._vault, None)
    receipt = mr.find_receipt(journal, data["receipt_id"])
    assert receipt is not None
    assert receipt["source"] == source


# ── The same call, one entry ───────────────────────────────────────────────
#
# A payload carrying an `entry` identity and the `entry_fingerprint` it was read
# at is a request about one list item rather than the note, and it runs the same
# pipeline one level in. What is pinned here is that it really is the *same*
# pipeline — the same confinement, the same scoping, the same "a refused verdict
# files exactly one proposal" wiring — and that the two scopes cannot be confused
# for one another.

ENTRY_NOTE = "People/Sofia.md"
ENTRY_PLAIN = (
    "---\ntype: person\nupdated: 2024-01-05\n---\n\n"
    "# Sofia\n\n"
    "- Sofia runs the release train [verified: 2024-01-05]\n"
    "- Sofia runs the platform team\n"
)
ENTRY_TRAIN = "- Sofia runs the release train [verified: 2024-01-05]"


def _entry_install(tmp_path: Path) -> tuple[_Install, Path, Path, Any]:
    install, root, note = _install(tmp_path, text=ENTRY_PLAIN)
    from ciao import note_entries as ne

    entry = ne.parse_note_entries(
        ENTRY_PLAIN, note_path=ENTRY_NOTE, workspace="personal"
    ).entries[0]
    return install, root, note, entry


def _entry_verdict(note: Path, entry: Any, **overrides: Any) -> dict[str, Any]:
    """A well-formed entry `still_valid` payload for the entry as it stands."""
    fields: dict[str, Any] = {
        "relative_path": ENTRY_NOTE,
        "entry": entry.identity,
        "entry_fingerprint": entry.fingerprint,
        "expected_revision": mr.content_revision(note.read_bytes().decode("utf-8")),
        "outcome": nv.STILL_VALID,
        "coverage": nv.COVERAGE_COMPLETE,
        "evidence": [CITATION],
        "reason": "the March release notes still name her",
    }
    fields.update(overrides)
    return fields


def _verify(
    plane: _Plane, root: Path, fields: dict[str, Any], *, name: str = "entry.json"
) -> dict[str, Any]:
    """One managed `verify_note` call over a payload file, and its reported body."""
    import asyncio

    reply = asyncio.run(
        plane.verify_note(_principal(), payload_file=_payload(root, fields, name=name))
    )
    assert reply["ok"], reply
    return reply["data"]


def test_an_entry_verdict_restamps_only_that_entry(tmp_path: Path) -> None:
    """The managed operation one level in, and the note's other fact survives.

    The whole-note path would have stamped the frontmatter and claimed every
    fact in the file. This changes one bullet's own date, journals it through the
    same receipt, and leaves the neighbour alone — which is the whole reason the
    entry service exists.
    """
    install, root, note, entry = _entry_install(tmp_path)
    plane = _plane(install)

    body = _verify(plane, root, _entry_verdict(note, entry))

    assert body["status"] == nv.APPLIED
    assert body["scope"] == "entry"
    assert body["auto_applied"] is True
    assert body["proposal"] is None, "an applied verdict files nothing"
    after = note.read_text(encoding="utf-8")
    assert f"[verified: {date.today().isoformat()}]" in after
    assert "updated: 2024-01-05" in after, "the note's own date is not this pass's job"
    assert "- Sofia runs the platform team" in after
    assert body["check"]["identity"] == entry.identity
    assert body["check"]["content_fingerprint"] == entry.fingerprint, (
        "a re-stamp leaves the fingerprint alone, so a verified fact stops being work"
    )
    from ciao import entry_verification as ev

    assert not ev.should_check_entry(
        install.workspace_vault_root("personal"), entry.identity, entry.fingerprint,
        today=date.today(),
    )


def test_an_entry_verdict_that_needs_a_person_files_one_entry_proposal(
    tmp_path: Path,
) -> None:
    """The #726-C wiring, at the entry's width: one proposal, and the check pinned
    to its queue row.

    A `retire` is the interesting case because it is the one an operation could
    most plausibly apply, and the one it must not: it files a `retire_entry` for a
    person to decide, and the bullet is still in the file.
    """
    install, root, note, entry = _entry_install(tmp_path)
    vault = install.workspace_vault_root("personal")
    plane = _plane(install)

    body = _verify(plane, root, _entry_verdict(note, entry, outcome=nv.RETIRE))

    assert body["status"] == nv.NEEDS_REVIEW
    assert body["scope"] == "entry"
    assert body["proposal"]["operation"] == nep.RETIRE_ENTRY
    assert body["proposal"]["queued"] is True
    assert body["proposal"]["entry_identity"] == entry.identity
    assert body["proposal"]["entry_span"] == [entry.start, entry.end]
    assert body["proposal_error"] == ""
    # The check the reply carries is the re-read one, so it names the queue row.
    assert body["check"]["proposal_id"] == body["proposal"]["proposal_id"]
    assert note.read_text(encoding="utf-8") == ENTRY_PLAIN, "nothing was written"
    assert f"[{nep.KIND} " in _queue(vault)
    assert _sidecars(vault) == [f"{body['proposal']['id']}.json"]


def test_an_entry_update_files_a_replace_entry_proposal(tmp_path: Path) -> None:
    """An update the evidence cannot carry comes back as an entry replacement.

    The image is the whole note with one span spliced, because that is what the
    accept applies — and the entry's own text is recovered from the recorded span
    rather than carried separately, so the record cannot be read as a whole-note
    rewrite wearing an entry's name.
    """
    from ciao import note_receipts as nr

    install, root, note, entry = _entry_install(tmp_path)
    vault = install.workspace_vault_root("personal")
    plane = _plane(install)
    replacement = "- Sofia runs the platform team [verified: 2024-01-05]"

    body = _verify(
        plane,
        root,
        _entry_verdict(
            note,
            entry,
            outcome=nv.UPDATE,
            before=entry.text,
            after=replacement,
            # One good citation and one uncited row: every row must be a citation,
            # so this is the `needs_review` a person decides rather than a write.
            evidence=[CITATION, UNCITED],
        ),
    )

    assert body["status"] == nv.NEEDS_REVIEW
    assert body["proposal"]["operation"] == nep.REPLACE_ENTRY
    filed = nep.read_sidecar(install, "personal", body["proposal"]["id"])
    assert filed is not None
    assert filed.before == ENTRY_PLAIN
    assert filed.after == ENTRY_PLAIN[: entry.start] + replacement + ENTRY_PLAIN[entry.end :]
    assert nep.entry_replacement(filed) == replacement
    assert filed.after == nr.compose_entry_edit(ENTRY_PLAIN, entry, replacement=replacement)[0]


def test_an_entry_selector_needs_its_fingerprint(tmp_path: Path) -> None:
    """An identity that cannot say which version was read could name any version.

    Refused by the operation rather than by the service, so the caller is told the
    payload is wrong instead of receiving a verdict-shaped `conflict` it would
    reasonably record as "this fact could not be checked".
    """
    install, root, note, entry = _entry_install(tmp_path)
    plane = _plane(install)
    fields = _entry_verdict(note, entry)
    del fields["entry_fingerprint"]

    with pytest.raises(cp.ControlPlaneError) as excinfo:
        _verify(plane, root, fields)

    assert excinfo.value.code == "payload_invalid"
    assert "entry_fingerprint" in str(excinfo.value)


def test_an_entry_verdict_cannot_name_another_workspace(tmp_path: Path) -> None:
    """The scope rule is the operation's, so it holds for an entry too."""
    install, root, note, entry = _entry_install(tmp_path)
    plane = _plane(install)

    with pytest.raises(cp.ControlPlaneError) as excinfo:
        import asyncio

        asyncio.run(
            plane.verify_note(
                _principal(),
                payload_file=_payload(
                    root, _entry_verdict(note, entry, workspace="work")
                ),
            )
        )

    assert excinfo.value.code == "workspace_forbidden"


def test_an_entry_verdict_out_of_scope_is_refused_before_the_service(
    tmp_path: Path,
) -> None:
    """Payload confinement, symmetry with the note path.

    The document is still a file, still capped, still inside the caller's own
    workspace root — an entry's identity does not loosen any of that.
    """
    install, root, note, entry = _entry_install(tmp_path)
    plane = _plane(install)
    fields = _entry_verdict(note, entry)
    (install.workspace_vault_root("personal") / "verdict.json").write_text(
        json.dumps(fields), encoding="utf-8"
    )

    with pytest.raises(cp.ControlPlaneError) as excinfo:
        import asyncio

        asyncio.run(
            plane.verify_note(
                _principal(), payload_file="../memory-vault/verdict.json"
            )
        )

    assert excinfo.value.code == "path_forbidden"


def test_an_entry_that_is_not_the_one_the_payload_read_is_a_conflict(
    tmp_path: Path,
) -> None:
    """The same fail-closed direction as a stale note revision, one level in.

    The note is at exactly the revision the payload names, so the only thing left
    to prove is that the bullet is still the bullet. It is not, so nothing is
    written, no check is recorded, and the reply says so.
    """
    install, root, note, entry = _entry_install(tmp_path)
    plane = _plane(install)

    body = _verify(plane, root, _entry_verdict(note, entry, entry_fingerprint="b" * 64))

    assert body["status"] == nv.CONFLICT
    assert body["check"] is None
    assert note.read_text(encoding="utf-8") == ENTRY_PLAIN
    from ciao import entry_verification as ev

    assert ev.read_entry_checks(install.workspace_vault_root("personal")) == {}
    assert _sidecars(install.workspace_vault_root("personal")) == []


def test_provenance_follows_the_turn_for_an_entry_too(tmp_path: Path) -> None:
    """A scheduled entry verification is not journaled as an interactive one."""
    install, root, note, entry = _entry_install(tmp_path)
    vault = install.workspace_vault_root("personal")
    plane = _plane(install, unattended=True)

    _verify(plane, root, _entry_verdict(note, entry))

    from ciao import note_receipts as nr

    receipt = [
        row
        for row in mr.read_receipts(mr.journal_path(vault, None))
        if row["kind"] == nr.NOTE_APPLY
    ][-1]
    assert receipt["source"] == "curation"
    assert receipt["actor"] == "agent"
    assert receipt["provenance"]["entry_identity"] == entry.identity
    assert receipt["workspace"] == "personal"


def test_a_note_verdict_and_an_entry_verdict_are_not_the_same_read(
    tmp_path: Path,
) -> None:
    """Coalescing is per question, and the two scopes are two questions.

    A payload without `entry` is a whole-note request and one with it is an entry
    request; the off-loop read is keyed on the identity, the fingerprint and a
    digest of the verdict, so neither can be answered with the other's.
    """
    install, root, note, entry = _entry_install(tmp_path)
    vault = install.workspace_vault_root("personal")
    plane = _plane(install)
    plain = _verdict(vault, note)
    assert "entry" not in plain

    # A retirement, so the entry verdict needs a person and files one: the two
    # scopes then have something of their own on file over the same bytes.
    entry_body = _verify(plane, root, _entry_verdict(note, entry, outcome=nv.RETIRE))
    assert entry_body["proposal"]["operation"] == nep.RETIRE_ENTRY

    # The note's own verdict runs next and applies, over the same bytes.
    fields = dict(plain)
    # The entry verdict above re-stamped a bullet, so the note's revision moved;
    # read it again the way a caller would.
    fields["expected_revision"] = mr.content_revision(
        note.read_bytes().decode("utf-8")
    )
    fields["evidence"] = [
        nv.Evidence(
            source_type="chat",
            source_ref="chat-2026-03-02",
            quoted="Sofia runs the platform team",
            supports=f"{NOTE}: who Sofia runs",
        ).as_dict()
    ]
    # The note payload names no entry, so it is a whole-note request.
    note_body = _verify(plane, root, fields, name="note.json")

    assert entry_body["scope"] == "entry"
    assert "scope" not in note_body
    assert note_body["status"] == nv.APPLIED
    assert note_body["proposal"] is None
    assert len(_sidecars(vault)) == 1, "only the entry verdict needed a person"
    from ciao import entry_verification as ev

    checks = ev.read_entry_checks(vault)
    assert list(checks) == [entry.identity]
    assert nv.read_note_checks(vault)[ENTRY_NOTE].content_revision == (
        mr.content_revision(note.read_bytes().decode("utf-8"))
    )
    # And the note's verdict re-stamped the frontmatter rather than the bullet.
    assert "updated: " + date.today().isoformat() in note.read_text(encoding="utf-8")
