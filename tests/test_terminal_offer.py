"""The transient terminal offer: its text contract and its in-memory state.

A terminal offer is the assistant's *suggested* command for a chat: plain
text the user reads, copies and runs themselves. It is never executed and
never typed into a shell. It never reaches Ciaobot's own chat registry, its
audit log or a backup of either — a command can name a credential or a
path, and a registry file is the wrong place for that — but it can still
appear in the provider's tool-call transcript, which is the provider's own
record of the arguments it was handed.

Two halves are covered here:

* the pure text contract (`ciao/terminal_offer.py`): what a command may
  contain, how it is normalized, and the refusal it earns. A refusal names a
  static reason and never the rejected text, so a secret cannot ride out in
  an error string.
* the manager state (`ProjectChatManager`): one optional offer per chat, a
  fresh random id per accepted change, the events it publishes, and the
  lifecycle transitions that drop it.

The state half is built on a real `ProjectChatManager` over a temporary
registry, the pattern the pin tests use: a stub could agree with itself about
a field the registry never persisted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import ciao.terminal_offer
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.terminal_offer import (
    TERMINAL_OFFER_MAX_BYTES,
    TerminalOfferError,
    new_terminal_offer,
    normalize_terminal_offer,
    terminal_offer_payload,
)
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import (
    ChatInfo,
    ProjectChatManager,
    TerminalOfferConflictError,
)


# ── The text contract ─────────────────────────────────────────────────────


def test_a_command_of_exactly_the_byte_bound_is_accepted() -> None:
    """4096 bytes is inside the bound; the limit is not off by one."""
    command = "a" * TERMINAL_OFFER_MAX_BYTES

    assert normalize_terminal_offer(command) == command


def test_one_byte_over_the_bound_is_refused() -> None:
    with pytest.raises(TerminalOfferError):
        normalize_terminal_offer("a" * (TERMINAL_OFFER_MAX_BYTES + 1))


def test_the_bound_counts_bytes_not_characters() -> None:
    """A multibyte command is measured as it is stored, not as it is typed.

    1365 three-byte characters are 4095 bytes and fit; 1366 are 4098 and do
    not, even though both are well under 4096 characters.
    """
    assert normalize_terminal_offer("€" * 1365) == "€" * 1365

    with pytest.raises(TerminalOfferError):
        normalize_terminal_offer("€" * 1366)


def test_the_raw_bound_applies_before_the_text_is_trimmed() -> None:
    """A command that arrives huge is refused even when it trims to nothing.

    The bound is on what arrived, not on what would be kept: 4096 spaces
    around "ls" is 4098 raw bytes, and the text that would be stored is two
    characters. One space fewer is exactly the bound, and it is accepted.
    """
    with pytest.raises(TerminalOfferError) as excinfo:
        normalize_terminal_offer(" " * 4096 + "ls")
    assert excinfo.value.reason == "too_long"

    assert normalize_terminal_offer(" " * 4094 + "ls") == "ls"

    # The same in bytes rather than characters: 1365 three-byte characters
    # are 4095 bytes and fit on their own, so two spaces around them are
    # already over the raw bound even though the text that would be kept
    # still fits.
    with pytest.raises(TerminalOfferError) as excinfo:
        normalize_terminal_offer("  " + "€" * 1365)
    assert excinfo.value.reason == "too_long"


@pytest.mark.parametrize(
    "value",
    [None, 7, 3.5, True, b"ls -la", ["ls"], {"command": "ls"}, object()],
    ids=[
        "none",
        "int",
        "float",
        "bool",
        "bytes",
        "list",
        "dict",
        "object",
    ],
)
def test_a_value_that_is_not_text_is_refused(value: object) -> None:
    with pytest.raises(TerminalOfferError):
        normalize_terminal_offer(value)  # type: ignore[arg-type]


def test_a_lone_surrogate_is_refused() -> None:
    """A surrogate is not text: it has no UTF-8 encoding at all.

    The card copies the command into a shell, so a value that cannot be
    encoded has to be refused rather than mangled.
    """
    with pytest.raises(TerminalOfferError):
        normalize_terminal_offer("ls -la \ud800")


@pytest.mark.parametrize(
    "character",
    [
        "\x00",  # NUL
        "\x07",  # BEL
        "\x0b",  # vertical tab
        "\x1b",  # ESC
        "\x7f",  # DEL
        "\x85",  # C1 NEL
        "\x9b",  # C1 CSI
        "\u202a",  # RLO
        "\u202e",  # PDF
        "\u2066",  # LRI
        "\u2069",  # PDI
        "\u200b",  # zero-width space
        "\u2028",  # line separator
        "\u2029",  # paragraph separator
    ],
    ids=[
        "nul",
        "bel",
        "vt",
        "esc",
        "del",
        "c1-nel",
        "c1-csi",
        "bidi-rlo",
        "bidi-pdf",
        "bidi-lri",
        "bidi-pdi",
        "zero-width-space",
        "line-separator",
        "paragraph-separator",
    ],
)
def test_a_control_or_invisible_character_is_refused(character: str) -> None:
    """Invisible characters are refused, not stripped.

    A right-to-left override or a zero-width space inside a command is
    invisible on the card and changes what the user copies into their shell,
    so it never reaches the state at all.
    """
    with pytest.raises(TerminalOfferError):
        normalize_terminal_offer(f"ls{character}-la")


def test_line_feeds_and_tabs_are_allowed() -> None:
    """A multi-line command is ordinary: a shell script is several lines."""
    assert normalize_terminal_offer("cd /tmp\n\tls -la") == "cd /tmp\n\tls -la"


def test_carriage_returns_become_line_feeds() -> None:
    """CRLF and a bare CR are line endings, not control characters."""
    assert normalize_terminal_offer("ls\r\n-la\r") == "ls\n-la"


def test_outer_whitespace_is_stripped() -> None:
    assert normalize_terminal_offer("  ls -la  ") == "ls -la"
    assert normalize_terminal_offer("\n ls -la \n") == "ls -la"


@pytest.mark.parametrize("value", ["", "   ", "\n", "\t\n "], ids=["empty", "spaces", "newline", "mixed"])
def test_a_command_with_nothing_left_in_it_is_refused(value: str) -> None:
    with pytest.raises(TerminalOfferError):
        normalize_terminal_offer(value)


#: A credential-shaped value used to prove that a refusal never carries the
#: text it refused. It is not a real credential.
_SENTINEL = "sk-live-0123456789abcdef"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        pytest.param(None, "invalid_type", id="not-text"),
        pytest.param(
            f"{_SENTINEL}\ud800", "invalid_encoding", id="lone-surrogate"
        ),
        pytest.param(_SENTINEL * 200, "too_long", id="too-long-ascii"),
        pytest.param("€" * 1366 + _SENTINEL, "too_long", id="too-long-multibyte"),
        pytest.param(" " * 4096 + _SENTINEL, "too_long", id="too-long-raw-only"),
        pytest.param(" \t\n ", "empty", id="whitespace-only"),
        pytest.param(f"{_SENTINEL}\x00", "invalid_characters", id="nul"),
        pytest.param(f"{_SENTINEL}\u202e", "invalid_characters", id="bidi-override"),
    ],
)
def test_a_refusal_names_its_exact_static_reason(
    value: object, reason: str
) -> None:
    """Every refusal has one closed reason, and never the text.

    The reason is a fixed word, so a caller can map it to a status without
    parsing prose. The rejected text is model-authored and can name a
    credential, and the refusal ends up in a log line and a telemetry
    field, so the text is nowhere in it.
    """
    with pytest.raises(TerminalOfferError) as excinfo:
        normalize_terminal_offer(value)  # type: ignore[arg-type]

    assert excinfo.value.reason == reason
    assert str(excinfo.value) == reason
    assert _SENTINEL not in str(excinfo.value)
    assert _SENTINEL not in excinfo.value.reason
    # The error carries the reason and nothing else: one argument, and no
    # cause. A refused command is model-authored, so the error must not
    # carry it along in a traceback either.
    assert excinfo.value.args == (reason,)
    assert excinfo.value.__cause__ is None
    assert (
        excinfo.value.__context__ is None
        or excinfo.value.__suppress_context__ is True
    )
    assert _SENTINEL not in repr(excinfo.value)


# ── The value type ────────────────────────────────────────────────────────


def test_an_offer_id_is_a_random_128_bit_hex() -> None:
    offer = new_terminal_offer("ls -la")

    assert re.fullmatch(r"[0-9a-f]{32}", offer.id)
    # A fresh id per offer, never a counter: a counter that restarts with
    # the process would let a stale tab dismiss a newer offer.
    assert new_terminal_offer("ls -la").id != offer.id


def test_the_id_is_minted_from_the_random_source(monkeypatch) -> None:
    """The id is 16 random bytes, and it is the source's value verbatim.

    Two different managers in one process would still differ, so id
    uniqueness alone does not pin this: what has to hold is that the id
    comes from a random source and not from a counter or a hash of the
    text, because a counter that restarts with the process repeats itself
    across a restart and hands a stale client a valid id.
    """
    calls: list[int] = []

    def fake_token_hex(nbytes: int) -> str:
        calls.append(nbytes)
        return "ab" * 16

    monkeypatch.setattr(
        ciao.terminal_offer.secrets, "token_hex", fake_token_hex
    )

    offer = new_terminal_offer("ls -la")

    assert calls == [16]
    assert offer.id == "ab" * 16
    assert offer.command == "ls -la"


def test_an_offer_holds_the_normalized_command() -> None:
    offer = new_terminal_offer("  ls -la\r\n")

    assert offer.command == "ls -la"


def test_the_payload_is_a_fresh_dict_for_every_caller() -> None:
    """A caller holding the payload cannot reach the state through it."""
    offer = new_terminal_offer("ls -la")

    payload = terminal_offer_payload(offer)
    assert payload == {"id": offer.id, "command": "ls -la"}

    payload["command"] = "rm -rf /"
    assert terminal_offer_payload(offer) == {"id": offer.id, "command": "ls -la"}
    assert terminal_offer_payload(None) is None


# ── The manager state ──────────────────────────────────────────────────────


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _restart(manager: ProjectChatManager) -> ProjectChatManager:
    """Build a second manager over the same registry, as a restart would."""
    config = manager._config
    return ProjectChatManager(
        config,
        state_store=StateStore(
            config.state_path, config.workspace_root, config.media_root
        ),
        transcript_store=TranscriptStore(
            Path(config.state_path).parent,
            Path(config.state_path).parent / "transcripts",
        ),
        path=manager._path,
    )


def _one_chat(manager: ProjectChatManager) -> ChatInfo:
    project = manager.create_project("Offers", workspace="work")
    return manager.create_chat(project.project_id, title="Offered")


@contextmanager
def _record_events(manager: ProjectChatManager) -> Iterator[list[dict]]:
    """Collect the events the manager publishes while the block runs."""
    published: list[dict] = []
    hub = manager._events
    original = hub.publish
    hub.publish = published.append  # type: ignore[method-assign]
    try:
        yield published
    finally:
        hub.publish = original  # type: ignore[method-assign]


def _registry(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )


def _offer_events(published: list[dict]) -> list[dict]:
    """Only the terminal-offer events, so a test can count them."""
    return [e for e in published if e["type"] == "chat_terminal_offer_changed"]


def _null_offer_event(chat_id: str) -> dict:
    """The one event a cleared offer publishes."""
    return {
        "type": "chat_terminal_offer_changed",
        "chat_id": chat_id,
        "terminal_offer": None,
    }


def test_a_new_offer_is_published_once_and_lands_in_the_chat_payload(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        payload = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    assert payload is not None
    assert payload["command"] == "npm install -g ciao"
    assert published == [
        {
            "type": "chat_terminal_offer_changed",
            "chat_id": chat.chat_id,
            "terminal_offer": payload,
        }
    ]
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == payload


def test_a_chat_without_an_offer_still_carries_the_field(tmp_path: Path) -> None:
    """The field is always present, so a client never has to guess."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] is None


def test_the_same_command_again_is_not_a_change(tmp_path: Path) -> None:
    """A repeated offer of the same text keeps the id and fires nothing.

    Two spellings of one command are one command: the assistant repeating
    itself must not mint a new id (which would invalidate a dismissal the
    user is holding) and must not turn the event stream into noise.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        first = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")
        again = manager.set_terminal_offer(chat.chat_id, "  npm install -g ciao  ")

    assert again == first
    assert manager.get_chat(chat.chat_id).terminal_offer.id == first["id"]
    assert len(published) == 1


def test_a_different_command_replaces_the_offer_with_a_new_id(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        first = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")
        second = manager.set_terminal_offer(chat.chat_id, "npm install -g opencode")

    assert second["id"] != first["id"]
    assert second["command"] == "npm install -g opencode"
    assert len(published) == 2
    assert published[-1]["terminal_offer"] == second


def test_a_dismissal_removes_the_offer_and_publishes_nothing_else(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    payload = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    with _record_events(manager) as published:
        dismissed = manager.dismiss_terminal_offer(chat.chat_id, payload["id"])

    assert dismissed == payload
    assert manager.get_chat(chat.chat_id).terminal_offer is None
    assert published == [
        {
            "type": "chat_terminal_offer_changed",
            "chat_id": chat.chat_id,
            "terminal_offer": None,
        }
    ]
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] is None


def test_a_second_dismissal_is_idempotent(tmp_path: Path) -> None:
    """Dismissing what is not there changes nothing and says nothing."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    payload = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")
    manager.dismiss_terminal_offer(chat.chat_id, payload["id"])

    with _record_events(manager) as published:
        again = manager.dismiss_terminal_offer(chat.chat_id, payload["id"])

    assert again is None
    assert published == []


def test_a_dismissal_with_a_stale_id_conflicts_and_keeps_the_offer(
    tmp_path: Path,
) -> None:
    """A device that raced a newer offer must not remove it.

    The dismissal quotes the id it saw. When the current offer carries
    another id, the write is a conflict and the offer stays exactly as it
    is — the user is looking at a command the other device never saw.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    payload = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    with _record_events(manager) as published:
        with pytest.raises(TerminalOfferConflictError) as excinfo:
            manager.dismiss_terminal_offer(chat.chat_id, "0" * 32)

    # The conflict carries the authoritative copy so the caller can re-apply.
    assert excinfo.value.terminal_offer == payload
    assert manager.get_chat(chat.chat_id).terminal_offer.id == payload["id"]
    assert published == []


def test_a_re_offer_of_the_same_command_mints_a_new_id(tmp_path: Path) -> None:
    """The same text after a dismissal is a new offer, not the old one.

    A counter would hand the second offer the id the first one had, and the
    stale tab would then dismiss the command the user is looking at.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    first = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")
    manager.dismiss_terminal_offer(chat.chat_id, first["id"])

    second = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    assert second["id"] != first["id"]


def test_a_stale_dismissal_cannot_remove_a_re_offered_command(tmp_path: Path) -> None:
    """The race the random id exists for.

    A tab that still holds the first id dismisses after the assistant
    offered the same command again. It has to lose.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    first = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")
    manager.dismiss_terminal_offer(chat.chat_id, first["id"])
    second = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    with pytest.raises(TerminalOfferConflictError):
        manager.dismiss_terminal_offer(chat.chat_id, first["id"])

    assert manager.get_chat(chat.chat_id).terminal_offer.id == second["id"]


def test_a_re_offer_after_a_restart_mints_a_new_id(tmp_path: Path) -> None:
    """A restart ends the offer, and the next one is not the old one.

    The offer is transient, so a restart clears it. If the id were derived
    from the text it would come back identical, and a tab that survived the
    restart would hold a valid id for an offer that does not exist yet.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    first = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    restored = _restart(manager)
    assert restored.get_chat(chat.chat_id).terminal_offer is None

    second = restored.set_terminal_offer(chat.chat_id, "npm install -g ciao")
    assert second["id"] != first["id"]

    # And the id the first offer had cannot dismiss this one.
    with pytest.raises(TerminalOfferConflictError):
        restored.dismiss_terminal_offer(chat.chat_id, first["id"])
    assert restored.get_chat(chat.chat_id).terminal_offer.id == second["id"]


def test_an_unknown_chat_gets_no_offer(tmp_path: Path) -> None:
    """A chat that does not exist is not created by an offer."""
    manager = _make_manager(tmp_path)

    assert manager.set_terminal_offer("chat-nope", "npm install -g ciao") is None
    assert manager.dismiss_terminal_offer("chat-nope", "0" * 32) is None
    assert manager.chat_terminal_offer_states == {}


def test_an_archived_chat_refuses_a_new_offer(tmp_path: Path) -> None:
    """An archived chat is read-only history, not a place for new guidance."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    chat.archived = True

    with pytest.raises(ValueError):
        manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    assert manager.get_chat(chat.chat_id).terminal_offer is None


def test_an_invalid_command_never_reaches_the_state(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        with pytest.raises(TerminalOfferError):
            manager.set_terminal_offer(chat.chat_id, "npm install -g ciao\x00")

    assert manager.get_chat(chat.chat_id).terminal_offer is None
    assert published == []


def test_an_invalid_replacement_leaves_the_current_offer_alone(
    tmp_path: Path,
) -> None:
    """A refused replacement is not a change of the offer that is there.

    The user is looking at the command the engine already holds; a
    refused write must not drop it, replace it with something else, or
    say anything about it.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    payload = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    with _record_events(manager) as published:
        with pytest.raises(TerminalOfferError):
            manager.set_terminal_offer(chat.chat_id, "npm install -g ciao\x00")

    offer = manager.get_chat(chat.chat_id).terminal_offer
    assert offer.id == payload["id"]
    assert offer.command == "npm install -g ciao"
    assert manager.chat_terminal_offer_states == {chat.chat_id: payload}
    assert _offer_events(published) == []


def test_a_caller_cannot_mutate_the_state_through_the_payload(tmp_path: Path) -> None:
    """The payload a caller holds is a copy, not a handle on the state."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    payload = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    payload["command"] = "rm -rf /"
    assert manager.get_chat(chat.chat_id).terminal_offer.command == (
        "npm install -g ciao"
    )

    row = manager.get_chat(chat.chat_id).to_dict()
    row["terminal_offer"]["command"] = "rm -rf /"
    assert manager.get_chat(chat.chat_id).terminal_offer.command == (
        "npm install -g ciao"
    )


def test_the_returned_the_event_and_the_snapshot_payloads_are_separate_copies(
    tmp_path: Path,
) -> None:
    """Three readers of one offer get three copies, and share nothing.

    The write's return value, the published event and the snapshot map
    all describe the same offer, so a consumer that edits one of them
    must not reach the state or the other two.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        returned = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    event_payload = published[0]["terminal_offer"]
    snapshot_payload = manager.chat_terminal_offer_states[chat.chat_id]
    row_payload = manager.get_chat(chat.chat_id).to_dict()["terminal_offer"]

    expected = {"id": returned["id"], "command": "npm install -g ciao"}
    assert returned == expected
    assert event_payload == expected
    assert snapshot_payload == expected
    assert row_payload == expected
    # Same content, four different dicts.
    assert len({id(returned), id(event_payload), id(snapshot_payload), id(row_payload)}) == 4

    returned["command"] = "rm -rf /"
    event_payload["command"] = "rm -rf /"
    snapshot_payload["id"] = "0" * 32
    row_payload["command"] = "rm -rf /"

    offer = manager.get_chat(chat.chat_id).terminal_offer
    assert offer.command == "npm install -g ciao"
    assert offer.id == expected["id"]
    assert manager.chat_terminal_offer_states[chat.chat_id] == expected
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == expected


def test_the_sparse_map_lists_only_the_chats_that_have_an_offer(
    tmp_path: Path,
) -> None:
    """The map is sparse: a chat without an offer is simply absent.

    Absence is how the snapshot clears a stale offer, so a chat with no
    offer must not appear with an empty value.
    """
    manager = _make_manager(tmp_path)
    project = manager.create_project("Offers", workspace="work")
    offered = manager.create_chat(project.project_id, title="Offered")
    quiet = manager.create_chat(project.project_id, title="Quiet")
    payload = manager.set_terminal_offer(offered.chat_id, "npm install -g ciao")

    states = manager.chat_terminal_offer_states
    assert states == {offered.chat_id: payload}
    assert quiet.chat_id not in states

    manager.dismiss_terminal_offer(offered.chat_id, payload["id"])
    assert manager.chat_terminal_offer_states == {}


def test_setting_and_dismissing_never_write_the_registry(
    tmp_path: Path, monkeypatch
) -> None:
    """The offer is transient: a change to it is not a registry mutation.

    A command can name a credential, and the registry is a file that gets
    copied into backups. Nothing about an offer may reach the registry, its
    audit log or a backup of either.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    payload = manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    saves: list[str] = []
    original = manager._save

    def record_save(*args, **kwargs) -> None:
        saves.append("saved")
        original(*args, **kwargs)

    monkeypatch.setattr(manager, "_save", record_save)
    manager.dismiss_terminal_offer(chat.chat_id, payload["id"])
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    assert saves == []


def test_the_command_is_kept_out_of_the_repr(tmp_path: Path) -> None:
    """A repr is what ends up in a log line, a traceback and a debugger.

    The id names nothing and is useful in a log; the command can name a
    credential, so it is in neither the repr of an offer nor the repr of
    the chat that holds it.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, f"export TOKEN={_SENTINEL}")

    offer = manager.get_chat(chat.chat_id).terminal_offer
    assert offer is not None
    assert _SENTINEL not in repr(offer)
    assert offer.id in repr(offer)

    assert _SENTINEL not in repr(manager.get_chat(chat.chat_id))


def test_the_offer_never_reaches_the_registry_or_its_audit(
    tmp_path: Path,
) -> None:
    """A later, unrelated save must not drag the command into the registry.

    The offer is not in the registry's own state, so a save that writes
    other fields of the same chat must not pick it up on the way. The
    registry and its audit log are what a backup of them copies.
    """
    sentinel = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, f"export TOKEN={sentinel}")

    # An unrelated change to the same chat, which does save the registry.
    manager.update_chat(chat.chat_id, title="Renamed")

    registry_text = (tmp_path / ".runtime" / "web_projects.json").read_text(
        encoding="utf-8"
    )
    audit_text = (tmp_path / ".runtime" / "web_projects.audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert sentinel not in registry_text
    assert sentinel not in audit_text
    assert "terminal_offer" not in _registry(tmp_path)["chats"][chat.chat_id]

    # And a restart has no offer to restore.
    restored = _restart(manager)
    assert restored.get_chat(chat.chat_id).terminal_offer is None
    assert restored.chat_terminal_offer_states == {}


def test_a_stale_offer_in_the_registry_is_not_restored(tmp_path: Path) -> None:
    """A registry that somehow holds an offer must not resurrect it.

    The offer is transient, so there is no compatibility shim and no
    migration that reads one back: a record with a `terminal_offer` key is
    read like any other field the engine does not know and ignored, so
    the chat comes back with no offer.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    registry = tmp_path / ".runtime" / "web_projects.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["chats"][chat.chat_id]["terminal_offer"] = {
        "id": "ab" * 16,
        "command": "rm -rf /",
    }
    registry.write_text(json.dumps(payload), encoding="utf-8")

    restored = _restart(manager)

    chat_again = restored.get_chat(chat.chat_id)
    assert chat_again is not None
    assert chat_again.terminal_offer is None
    assert restored.chat_terminal_offer_states == {}
    assert restored.get_chat(chat.chat_id).to_dict()["terminal_offer"] is None


async def test_an_archive_drops_the_offer(tmp_path: Path) -> None:
    """An archived chat is history; its guidance does not outlive it."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    with _record_events(manager) as published:
        await manager.archive_chat(chat.chat_id)

    assert manager.get_chat(chat.chat_id).terminal_offer is None
    assert manager.chat_terminal_offer_states == {}
    # Exactly one event, and it is the clear: not a membership test, so a
    # second event would fail here.
    assert _offer_events(published) == [_null_offer_event(chat.chat_id)]


async def test_an_archive_of_a_chat_without_an_offer_says_nothing_about_one(
    tmp_path: Path,
) -> None:
    """A real archive, and no offer event out of it.

    The archive really runs — it announces itself — and the clear that
    follows a lifecycle transition is guarded: a chat with no offer has
    nothing to clear, so it says nothing.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        await manager.archive_chat(chat.chat_id)

    assert [e for e in published if e["type"] == "chat_archived"]
    assert _offer_events(published) == []


def test_a_new_session_of_a_chat_without_an_offer_says_nothing_about_one(
    tmp_path: Path,
) -> None:
    """A repeated reset on a chat with no offer still says nothing.

    The reset really happens (the session is gone), and the guarded clear
    holds the second time round as well.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    chat.context_digest = "digest-of-the-old-session"

    with _record_events(manager) as published:
        manager.new_session(chat.chat_id)
        manager.new_session(chat.chat_id)

    assert manager.get_chat(chat.chat_id).context_digest == ""
    assert _offer_events(published) == []


def test_a_handover_of_a_chat_without_an_offer_says_nothing_about_one(
    tmp_path: Path,
) -> None:
    """A handover with no offer to drop publishes no offer event."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)

    with _record_events(manager) as published:
        manager.handover_chat(chat.chat_id, provider="claude", model="haiku")

    assert manager.get_chat(chat.chat_id).model == "haiku"
    assert _offer_events(published) == []


def test_a_new_session_drops_the_offer(tmp_path: Path) -> None:
    """A fresh provider session is a fresh conversation."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    with _record_events(manager) as published:
        manager.new_session(chat.chat_id)

    assert manager.get_chat(chat.chat_id).terminal_offer is None
    assert manager.chat_terminal_offer_states == {}
    assert _offer_events(published) == [_null_offer_event(chat.chat_id)]


def test_a_handover_drops_the_offer(tmp_path: Path) -> None:
    """The command was offered for the old provider's conversation."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    with _record_events(manager) as published:
        manager.handover_chat(chat.chat_id, provider="claude", model="haiku")

    assert manager.get_chat(chat.chat_id).terminal_offer is None
    assert manager.chat_terminal_offer_states == {}
    assert _offer_events(published) == [_null_offer_event(chat.chat_id)]


def test_a_fork_starts_without_an_offer(tmp_path: Path) -> None:
    """A fork is a new conversation, not a copy of the panel."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    fork = manager.fork_chat(
        chat.chat_id,
        messages=[
            {"role": "user", "content": "Question", "turn_index": 0},
            {"role": "assistant", "content": "Answer"},
        ],
        turn_index=0,
    )

    assert manager.get_chat(fork.chat_id).terminal_offer is None
    assert fork.chat_id not in manager.chat_terminal_offer_states


def test_a_continued_chat_starts_without_an_offer(tmp_path: Path) -> None:
    """A continued chat is a new conversation about the same history.

    The archived chat is put in its archived shape by hand, so the source
    still carries an offer at the moment of the continuation: that is what
    makes this a test of "not copied" rather than of an empty source.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    transcript = tmp_path / "logs" / "archived.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "## Turn 1\n\n"
        "- Time: 2026-06-10T12:00:00Z\n"
        "### User\n\n```text\nQuestion\n```\n\n"
        "### Assistant\n\n```text\nAnswer\n```\n",
        encoding="utf-8",
    )
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")
    chat.archived = True
    chat.archive_path = transcript.relative_to(tmp_path).as_posix()
    manager._save()

    continued = manager.continue_archived_chat(chat.chat_id)

    assert manager.get_chat(continued.chat_id).terminal_offer is None
    assert continued.chat_id not in manager.chat_terminal_offer_states


def test_a_deleted_chat_leaves_no_offer(tmp_path: Path) -> None:
    """A deleted chat owns nothing; the delete event is the whole answer."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    assert manager.delete_chat(chat.chat_id) is True

    assert manager.get_chat(chat.chat_id) is None
    assert manager.chat_terminal_offer_states == {}


def test_a_refused_transition_keeps_the_offer(tmp_path: Path) -> None:
    """A transition that did not happen must not drop the offer either."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    with _record_events(manager) as published:
        with pytest.raises(ValueError):
            manager.handover_chat(chat.chat_id, provider="nope", model="haiku")

    assert manager.get_chat(chat.chat_id).terminal_offer is not None
    assert chat.chat_id in manager.chat_terminal_offer_states
    assert _offer_events(published) == []


def test_a_transition_that_cannot_be_saved_keeps_the_offer(
    tmp_path: Path, monkeypatch
) -> None:
    """The offer goes only when the transition itself went through.

    A handover that could not be saved did not happen, so the offer it
    would have ended is still there, and no clear was announced.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    def fail_save(*args, **kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save", fail_save)
    with _record_events(manager) as published:
        with pytest.raises(OSError):
            manager.handover_chat(chat.chat_id, provider="claude", model="haiku")

    assert manager.get_chat(chat.chat_id).terminal_offer is not None
    assert _offer_events(published) == []


async def test_a_failed_archive_keeps_the_offer(
    tmp_path: Path, monkeypatch
) -> None:
    """An archive that could not be saved did not archive anything.

    The clear follows the transition's own save, so a save that failed
    leaves the offer exactly as it was and announces nothing.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    def fail_save(*args, **kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save", fail_save)
    with _record_events(manager) as published:
        with pytest.raises(OSError):
            await manager.archive_chat(chat.chat_id)

    assert manager.get_chat(chat.chat_id).terminal_offer is not None
    assert _offer_events(published) == []


def test_a_failed_new_session_keeps_the_offer(
    tmp_path: Path, monkeypatch
) -> None:
    """A new session that could not be saved did not start.

    The offer belonged to the session that is still there, so it stays and
    no clear is announced.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    manager.set_terminal_offer(chat.chat_id, "npm install -g ciao")

    def fail_save(*args, **kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save", fail_save)
    with _record_events(manager) as published:
        with pytest.raises(OSError):
            manager.new_session(chat.chat_id)

    assert manager.get_chat(chat.chat_id).terminal_offer is not None
    assert _offer_events(published) == []

