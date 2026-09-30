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
  verdict recorded a check and asked nobody. Auto-applied verdicts file nothing.
* **Retirement is never applied.** It reaches the same `needs_review` a note with
  no frontmatter reaches, and its proposal is a human click all the way down.
* **An oversized input is `unverified`, not `applied`.** A note nobody could read
  in full has not been verified by anyone; reporting it as a completed
  verification would pin a verdict about text nobody showed the service.

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

    def __init__(self, install: _Install, *, mode: str = "auto") -> None:
        self.config = install
        self.pcm = SimpleNamespace(get_chat=lambda _chat_id: _Chat())
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
    _verification_payload = cp.CiaoControlPlane._verification_payload
    _workspace_vault = cp.CiaoControlPlane._workspace_vault


def _plane(install: _Install, *, mode: str = "auto") -> _Plane:
    return _Plane(install, mode=mode)


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
