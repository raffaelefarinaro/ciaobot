"""PWA permission-response round-trip.

Validates the full chain from the client-facing route through the provider
response contract:

    ws client  →  respond_permission(chat_id, request_id, approved, reason, session_id)
               →  ProjectChatManager.respond_permission
                →  ProviderService.provider.respond_permission
"""

from __future__ import annotations

import asyncio
import ast
from dataclasses import MISSING, FrozenInstanceError, fields
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig
from ciao.models import PermissionRequestEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.providers.acp.agents import OpencodeAcpProvider
from ciao.providers.acp.permissions import PermissionBroker
from ciao.providers.base import (
    BaseProvider, ProviderCapabilities, QuestionResponseResult,
)
from ciao.web.chat_streaming import ChatStream
from ciao.web.project_chats import ProjectChatManager


class _MinimalProvider(BaseProvider):
    async def run_streaming(self, request, register_handle):
        if False:
            yield None


class _ThirdPartyProvider(_MinimalProvider):
    def __init__(
        self, root: Path, *, capabilities: ProviderCapabilities,
        response_session_id: str = "",
    ) -> None:
        super().__init__(root)
        self.capabilities = capabilities
        self._response_session_id = response_session_id
        self.permissions: dict[str, tuple[str, str]] = {}
        self.questions: dict[str, str] = {}
        self.permission_calls: list[tuple[str, bool, str, str]] = []
        self.question_calls: list[tuple[str, dict, bool, str]] = []
        self.permission_result = QuestionResponseResult(True)
        self.question_result = QuestionResponseResult(True)
        self.permission_wait: tuple[asyncio.Event, asyncio.Event] | None = None
        self.question_wait: tuple[asyncio.Event, asyncio.Event] | None = None
        self.raise_on_response = False

    @property
    def response_session_id(self) -> str:
        return self._response_session_id

    def has_permission_request(self, request_id: str, session_id: str = "") -> bool:
        request = self.permissions.get(request_id)
        return bool(request and (not session_id or request[0] == session_id))

    def permission_request_session(self, request_id: str) -> str:
        return self.permissions.get(request_id, ("", ""))[0]

    def tool_use_id_for_request(self, request_id: str, session_id: str = "") -> str:
        request = self.permissions.get(request_id)
        return request[1] if request and (not session_id or request[0] == session_id) else ""

    def has_question_request(self, request_id: str, session_id: str = "") -> bool:
        session = self.questions.get(request_id)
        return bool(session and (not session_id or session == session_id))

    def question_request_session(self, request_id: str) -> str:
        return self.questions.get(request_id, "")

    async def respond_permission(
        self, request_id: str, approved: bool, reason: str = "", *,
        session_id: str = "",
    ) -> QuestionResponseResult:
        if self.raise_on_response:
            raise AssertionError("unsupported permission response was dispatched")
        self.permission_calls.append((request_id, approved, reason, session_id))
        self.permissions.pop(request_id, None)
        if self.permission_wait:
            entered, release = self.permission_wait
            entered.set()
            await release.wait()
        return self.permission_result

    async def respond_question(
        self, request_id, answers, *, cancel=False, session_id=""
    ) -> QuestionResponseResult:
        if self.raise_on_response:
            raise AssertionError("unsupported question response was dispatched")
        self.question_calls.append((request_id, answers, cancel, session_id))
        self.questions.pop(request_id, None)
        if self.question_wait:
            entered, release = self.question_wait
            entered.set()
            await release.wait()
        return self.question_result


def _third_party_chat(tmp_path: Path, provider: BaseProvider):
    from ciao.web.chat_broker import ChatStream as BrokerChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="third provider")
    chat.session_id = "ses_1"
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)
    stream = BrokerChatStream("turn")
    pcm._broker.register(chat.chat_id, stream)
    return pcm, chat, stream


@pytest.mark.asyncio
async def test_base_provider_response_defaults_are_explicitly_unsupported(
    tmp_path: Path,
) -> None:
    provider = _MinimalProvider(tmp_path)
    assert provider.response_session_id == ""
    assert not provider.has_permission_request("request")
    assert provider.permission_request_session("request") == ""
    assert provider.tool_use_id_for_request("request") == ""
    assert not provider.has_question_request("request")
    assert provider.question_request_session("request") == ""
    permission = await provider.respond_permission("request", True)
    question = await provider.respond_question("request", {})
    assert permission == QuestionResponseResult(
        False, "Permission responses are not supported by this provider", False
    )
    assert question == QuestionResponseResult(
        False, "Native question responses are not supported by this provider", False
    )


@pytest.mark.asyncio
async def test_unknown_provider_captures_tool_id_before_acknowledgement(
    tmp_path: Path,
) -> None:
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.session_id = "ses_1"
    chat.pending_permission = json.dumps({"request_id": "request_newer", "session_id": "ses_1"})
    entered = asyncio.Event()
    release = asyncio.Event()
    captured: list[tuple[str, str]] = []

    class ThirdPartyProvider(_MinimalProvider):
        capabilities = ProviderCapabilities(permissions=True)

        def has_permission_request(self, request_id: str, session_id: str = "") -> bool:
            return request_id == "request_child" and session_id in {"", "ses_child"}

        def permission_request_session(self, request_id: str) -> str:
            return "ses_child"

        def tool_use_id_for_request(self, request_id: str, session_id: str = "") -> str:
            captured.append((request_id, session_id))
            return "call_child"

        async def respond_permission(
            self, request_id: str, approved: bool, reason: str = "", *,
            session_id: str = "",
        ) -> QuestionResponseResult:
            assert (request_id, approved, session_id) == (
                "request_child", False, "ses_child"
            )
            entered.set()
            await release.wait()
            return QuestionResponseResult(True)

    provider = ThirdPartyProvider(tmp_path)
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)
    stream = ChatStream("turn")
    pcm._broker.register(chat.chat_id, stream)
    stream.publish({
        "type": "tool_use", "tool_name": "write", "tool_use_id": "call_child",
        "file_touch": {"file_path": "child.txt", "action": "created"},
    })

    reply = asyncio.create_task(pcm.respond_permission(
        chat.chat_id, request_id="request_child", approved=False
    ))
    await entered.wait()
    assert captured == [("request_child", "ses_child")]
    assert not any(
        event.get("type") == "tool_denied" for event in stream.buffered_events()
    )
    release.set()
    result = await reply

    assert result.ok
    assert json.loads(chat.pending_permission)["request_id"] == "request_newer"
    assert any(
        event.get("type") == "tool_denied" and event.get("tool_use_id") == "call_child"
        for event in stream.buffered_events()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "approved", "action"),
    [
        ("permission", True, "reply"),
        ("permission", False, "deny"),
        ("question", True, "reply"),
        ("question", True, "cancel"),
    ],
)
async def test_unknown_provider_held_child_responses_use_the_contract(
    tmp_path: Path, operation: str, approved: bool, action: str
) -> None:
    from ciao.web.chat_broker import ChatStream as BrokerChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="third provider")
    chat.session_id = "ses_1"
    chat.pending_permission = json.dumps({
        "request_id": "request_newer", "session_id": "ses_1"
    })
    chat.pending_question = json.dumps({
        "request_id": "question_newer", "session_id": "ses_1",
        "questions": [{"id": "q", "question": "Newest?"}],
    })
    entered, release = asyncio.Event(), asyncio.Event()
    requests = {"request_child": ("ses_child", "call_child")}
    forms = {"question_child": "ses_child"}
    calls: list[tuple] = []

    class ThirdPartyProvider(_MinimalProvider):
        capabilities = ProviderCapabilities(
            permissions=True, structured_questions=True
        )

        @property
        def response_session_id(self) -> str:
            return "ses_1"

        def has_permission_request(self, request_id: str, session_id: str = "") -> bool:
            return request_id in requests and (
                not session_id or requests[request_id][0] == session_id
            )

        def permission_request_session(self, request_id: str) -> str:
            return requests.get(request_id, ("", ""))[0]

        def tool_use_id_for_request(self, request_id: str, session_id: str = "") -> str:
            source = requests.get(request_id)
            return source[1] if source and (not session_id or source[0] == session_id) else ""

        def has_question_request(self, request_id: str, session_id: str = "") -> bool:
            return request_id in forms and (
                not session_id or forms[request_id] == session_id
            )

        def question_request_session(self, request_id: str) -> str:
            return forms.get(request_id, "")

        async def respond_permission(
            self, request_id: str, approved: bool, reason: str = "", *,
            session_id: str = "",
        ) -> QuestionResponseResult:
            calls.append(("permission", request_id, approved, reason, session_id))
            requests.pop(request_id)
            entered.set()
            await release.wait()
            return QuestionResponseResult(True)

        async def respond_question(
            self, request_id, answers, *, cancel=False, session_id=""
        ) -> QuestionResponseResult:
            calls.append(("question", request_id, answers, cancel, session_id))
            forms.pop(request_id)
            entered.set()
            await release.wait()
            return QuestionResponseResult(True)

    provider = ThirdPartyProvider(tmp_path)
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)
    stream = BrokerChatStream("turn")
    pcm._broker.register(chat.chat_id, stream)
    other_tab: asyncio.Queue[dict] = asyncio.Queue()
    stream._subs.add(other_tab)
    stream.publish({
        "type": "tool_use", "tool_name": "write", "tool_use_id": "call_child",
        "file_touch": {"file_path": "child.txt", "action": "created"},
    })
    if operation == "permission" and not approved:
        stream.publish({
            "type": "tool_use", "tool_name": "write", "tool_use_id": "call_unrelated",
            "file_touch": {"file_path": "unrelated.txt", "action": "created"},
        })

    reason = "child permission reason"
    if operation == "permission":
        reply = asyncio.create_task(pcm.respond_permission(
            chat.chat_id, request_id="request_child", approved=approved, reason=reason
        ))
    else:
        reply = asyncio.create_task(pcm.respond_question(
            chat.chat_id, request_id="question_child", answers={"q": ["answer"]},
            action=action,
        ))
    await entered.wait()
    assert (
        not requests if operation == "permission" else not forms
    ), "adapter must consume ownership mapping before acknowledgement resumes"
    if operation == "permission":
        assert calls == [(
            "permission", "request_child", approved, reason, "ses_child",
        )]
    else:
        assert calls == [(
            "question", "question_child", {"q": ["answer"]},
            action == "cancel", "ses_child",
        )]
    if operation == "permission" and not approved:
        before_ack = stream.buffered_events()
        assert [event for event in before_ack if event.get("type") == "tool_denied"] == []
        assert {
            event.get("tool_use_id") for event in before_ack
            if event.get("type") == "tool_use" and event.get("file_touch")
        } == {"call_child", "call_unrelated"}
    release.set()
    result = await reply

    assert result.ok
    assert json.loads(chat.pending_permission)["request_id"] == "request_newer"
    assert json.loads(chat.pending_question)["request_id"] == "question_newer"
    frames = [other_tab.get_nowait() for _ in range(other_tab.qsize())]
    resolved_type = "permission_resolved" if operation == "permission" else "question_resolved"
    assert {
        "type": resolved_type,
        "request_id": "request_child" if operation == "permission" else "question_child",
        "session_id": "ses_child",
    } in frames
    if operation == "permission" and not approved:
        events = stream.buffered_events()
        assert [
            event for event in events if event.get("type") == "tool_denied"
        ] == [{"type": "tool_denied", "tool_use_id": "call_child"}]
        touches = {
            event.get("tool_use_id"): bool(event.get("file_touch"))
            for event in events if event.get("type") == "tool_use"
        }
        assert touches == {"call_child": False, "call_unrelated": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["permission", "question"])
async def test_unknown_provider_unheld_session_mismatch_does_not_dispatch(
    tmp_path: Path, operation: str,
) -> None:
    provider = _ThirdPartyProvider(
        tmp_path,
        capabilities=ProviderCapabilities(permissions=True, structured_questions=True),
        response_session_id="ses_new",
    )
    pcm, chat, stream = _third_party_chat(tmp_path, provider)
    other_tab: asyncio.Queue[dict] = asyncio.Queue()
    stream._subs.add(other_tab)
    if operation == "permission":
        chat.pending_permission = json.dumps({
            "request_id": "same-id", "session_id": "ses_old"
        })
        call = pcm.respond_permission(
            chat.chat_id, request_id="same-id", approved=True
        )
    else:
        chat.pending_question = json.dumps({
            "request_id": "same-id", "session_id": "ses_old",
            "questions": [{"id": "q", "question": "Old?"}],
        })
        call = pcm.respond_question(
            chat.chat_id, request_id="same-id", answers={"q": ["x"]}
        )
    result = await call

    assert result.ok
    assert provider.permission_calls == []
    assert provider.question_calls == []
    assert (
        chat.pending_permission if operation == "permission" else chat.pending_question
    ) == ""
    stale_type = "permission_resolved" if operation == "permission" else "question_resolved"
    stale_frames = [other_tab.get_nowait() for _ in range(other_tab.qsize())]
    assert any(
        event.get("type") == stale_type and event.get("request_id") == "same-id"
        for event in stale_frames
    )

    # An explicit old-session submit for the same id must not clear the newer
    # persisted slot that now owns that id.
    if operation == "permission":
        chat.pending_permission = json.dumps({
            "request_id": "same-id", "session_id": "ses_new"
        })
        result = await pcm.respond_permission(
            chat.chat_id, request_id="same-id", approved=True,
            session_id="ses_old",
        )
        assert json.loads(chat.pending_permission)["session_id"] == "ses_new"
    else:
        chat.pending_question = json.dumps({
            "request_id": "same-id", "session_id": "ses_new",
            "questions": [{"id": "q", "question": "New?"}],
        })
        result = await pcm.respond_question(
            chat.chat_id, request_id="same-id", answers={"q": ["x"]},
            session_id="ses_old",
        )
        assert json.loads(chat.pending_question)["session_id"] == "ses_new"
    assert result.ok
    assert provider.permission_calls == provider.question_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["permission", "question"])
async def test_response_feature_flags_prevent_dispatch(
    tmp_path: Path, operation: str,
) -> None:
    provider = _ThirdPartyProvider(tmp_path, capabilities=ProviderCapabilities())
    provider.raise_on_response = True
    pcm, chat, stream = _third_party_chat(tmp_path, provider)
    if operation == "permission":
        chat.pending_permission = json.dumps({
            "request_id": "live-card", "session_id": "ses_1"
        })
        stream.publish({
            "type": "tool_use", "tool_name": "write", "tool_use_id": "call-1",
            "file_touch": {"file_path": "notes.md", "action": "created"},
        })
        result = await pcm.respond_permission(
            chat.chat_id, request_id="live-card", approved=False
        )
        assert result == QuestionResponseResult(
            False, "Permission responses are not supported by this provider", False
        )
        assert chat.pending_permission
    else:
        chat.pending_question = json.dumps({
            "request_id": "live-card", "session_id": "ses_1",
            "questions": [{"id": "q", "question": "Continue?"}],
        })
        result = await pcm.respond_question(
            chat.chat_id, request_id="live-card", answers={"q": ["yes"]}
        )
        assert result == QuestionResponseResult(
            False, "Native question responses are not supported by this provider", False
        )
        assert chat.pending_question
    assert provider.permission_calls == provider.question_calls == []
    assert not any(
        event.get("type") in {"tool_denied", "permission_resolved", "question_resolved"}
        for event in stream.buffered_events()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["permission", "question"])
async def test_unknown_provider_failed_ack_preserves_pending_state(
    tmp_path: Path, operation: str,
) -> None:
    provider = _ThirdPartyProvider(
        tmp_path, capabilities=ProviderCapabilities(permissions=True, structured_questions=True)
    )
    entered, release = asyncio.Event(), asyncio.Event()
    provider.permission_wait = provider.question_wait = (entered, release)
    provider.permission_result = provider.question_result = QuestionResponseResult(
        False, "temporary", True
    )
    pcm, chat, stream = _third_party_chat(tmp_path, provider)
    if operation == "permission":
        provider.permissions["old"] = ("ses_old", "call_old")
        chat.pending_permission = json.dumps({"request_id": "old", "session_id": "ses_old"})
        stream.publish({"type": "permission_request", "request_id": "old", "session_id": "ses_old"})
        reply = asyncio.create_task(pcm.respond_permission(
            chat.chat_id, request_id="old", approved=False
        ))
    else:
        provider.questions["old"] = "ses_old"
        chat.pending_question = json.dumps({
            "request_id": "old", "session_id": "ses_old",
            "questions": [{"id": "q", "question": "Old?"}],
        })
        stream.publish({
            "type": "tool_use", "tool_name": "AskUserQuestion",
            "request_id": "old", "session_id": "ses_old",
        })
        reply = asyncio.create_task(pcm.respond_question(
            chat.chat_id, request_id="old", answers={"q": ["x"]}
        ))
    await entered.wait()
    if operation == "permission":
        provider.permissions["new"] = ("ses_new", "call_new")
        chat.pending_permission = json.dumps({"request_id": "new", "session_id": "ses_new"})
        stream.publish({"type": "permission_request", "request_id": "new", "session_id": "ses_new"})
    else:
        provider.questions["new"] = "ses_new"
        chat.pending_question = json.dumps({
            "request_id": "new", "session_id": "ses_new",
            "questions": [{"id": "q", "question": "New?"}],
        })
        stream.publish({
            "type": "tool_use", "tool_name": "AskUserQuestion",
            "request_id": "new", "session_id": "ses_new",
        })
    release.set()
    result = await reply

    assert result == QuestionResponseResult(False, "temporary", True)
    state = chat.pending_permission if operation == "permission" else chat.pending_question
    assert json.loads(state)["request_id"] == "new"
    events = stream.buffered_events()
    assert any(event.get("request_id") == "old" for event in events)
    assert any(event.get("request_id") == "new" for event in events)
    assert not any(event.get("type") == "tool_denied" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["permission", "question"])
async def test_successful_ack_does_not_clear_a_replacement_slot(
    tmp_path: Path, operation: str,
) -> None:
    provider = _ThirdPartyProvider(
        tmp_path, capabilities=ProviderCapabilities(permissions=True, structured_questions=True)
    )
    entered, release = asyncio.Event(), asyncio.Event()
    provider.permission_wait = provider.question_wait = (entered, release)
    pcm, chat, stream = _third_party_chat(tmp_path, provider)
    other_tab: asyncio.Queue[dict] = asyncio.Queue()
    stream._subs.add(other_tab)
    if operation == "permission":
        provider.permissions["old"] = ("ses_old", "call_old")
        chat.pending_permission = json.dumps({"request_id": "old", "session_id": "ses_old"})
        stream.publish({"type": "permission_request", "request_id": "old", "session_id": "ses_old"})
        reply = asyncio.create_task(pcm.respond_permission(
            chat.chat_id, request_id="old", approved=True
        ))
    else:
        provider.questions["old"] = "ses_old"
        chat.pending_question = json.dumps({
            "request_id": "old", "session_id": "ses_old",
            "questions": [{"id": "q", "question": "Old?"}],
        })
        stream.publish({
            "type": "tool_use", "tool_name": "AskUserQuestion",
            "request_id": "old", "session_id": "ses_old",
        })
        reply = asyncio.create_task(pcm.respond_question(
            chat.chat_id, request_id="old", answers={"q": ["x"]}
        ))
    await entered.wait()
    if operation == "permission":
        provider.permissions["new"] = ("ses_new", "call_new")
        chat.pending_permission = json.dumps({"request_id": "new", "session_id": "ses_new"})
        stream.publish({"type": "permission_request", "request_id": "new", "session_id": "ses_new"})
    else:
        provider.questions["new"] = "ses_new"
        chat.pending_question = json.dumps({
            "request_id": "new", "session_id": "ses_new",
            "questions": [{"id": "q", "question": "New?"}],
        })
        stream.publish({
            "type": "tool_use", "tool_name": "AskUserQuestion",
            "request_id": "new", "session_id": "ses_new",
        })
    release.set()
    assert (await reply).ok

    state = chat.pending_permission if operation == "permission" else chat.pending_question
    assert json.loads(state)["request_id"] == "new"
    events = stream.buffered_events()
    assert not any(event.get("request_id") == "old" for event in events)
    assert any(event.get("request_id") == "new" for event in events)
    frames = [other_tab.get_nowait() for _ in range(other_tab.qsize())]
    assert {
        "type": "permission_resolved" if operation == "permission" else "question_resolved",
        "request_id": "old", "session_id": "ses_old",
    } in frames
    assert not any(
        event.get("request_id") == "new"
        and event.get("type") in {"permission_resolved", "question_resolved"}
        for event in frames
    )


def test_question_response_result_preserves_dataclass_contract() -> None:
    result_fields = fields(QuestionResponseResult)
    assert [item.name for item in result_fields] == [
        "ok", "error", "retryable", "delivered_as_message", "expired",
        "already_resolved",
    ]
    assert result_fields[0].default is MISSING
    assert [item.default for item in result_fields[1:]] == [
        "", True, False, False, False,
    ]
    assert (
        QuestionResponseResult(True).ok,
        QuestionResponseResult(True).error,
        QuestionResponseResult(True).retryable,
        QuestionResponseResult(True).delivered_as_message,
        QuestionResponseResult(True).expired,
        QuestionResponseResult(True).already_resolved,
    ) == (True, "", True, False, False, False)
    positional = QuestionResponseResult(False, "retry", False, True, True, True)
    assert (
        positional.ok,
        positional.error,
        positional.retryable,
        positional.delivered_as_message,
        positional.expired,
        positional.already_resolved,
    ) == (False, "retry", False, True, True, True)
    assert positional == QuestionResponseResult(
        ok=False, error="retry", retryable=False, delivered_as_message=True,
        expired=True, already_resolved=True,
    )
    with pytest.raises(TypeError):
        QuestionResponseResult()  # type: ignore[call-arg]


def test_question_response_result_remains_frozen_and_slotted() -> None:
    result = QuestionResponseResult(True)
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.ok = False  # type: ignore[misc]
    assert result == QuestionResponseResult(True)


def test_question_response_result_has_one_shared_owner() -> None:
    from ciao.providers import base
    from ciao.web import chat_streaming, project_chats

    canonical = base.QuestionResponseResult
    assert canonical.__module__ == "ciao.providers.base"
    assert chat_streaming.QuestionResponseResult is canonical
    assert project_chats.QuestionResponseResult is canonical

    root = Path(__file__).resolve().parents[1]
    definitions: list[str] = []
    wrong_imports: list[str] = []
    for source_path in (*root.joinpath("ciao").rglob("*.py"), *root.joinpath("tests").rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "QuestionResponseResult":
                definitions.append(source_path.relative_to(root).as_posix())
            if isinstance(node, ast.ImportFrom):
                imports_result = any(
                    alias.name in {"QuestionResponseResult", "*"}
                    for alias in node.names
                )
                if imports_result and (
                    node.module != "ciao.providers.base"
                    or node.level != 0
                    or any(alias.name == "*" for alias in node.names)
                ):
                    wrong_imports.append(source_path.relative_to(root).as_posix())
    assert definitions == ["ciao/providers/base.py"]
    assert wrong_imports == []


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def test_a_native_question_blocks_a_new_ordinary_turn(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.pending_question = json.dumps({
        "request_id": "form-1",
        "questions": [{"id": "choice", "question": "Continue?"}],
    })

    pcm._providers[chat.chat_id] = SimpleNamespace(provider=object())  # type: ignore[assignment]

    with pytest.raises(ValueError, match="Answer the open question"):
        pcm.start_stream(chat.chat_id, "send around the form")

    assert json.loads(chat.pending_question)["request_id"] == "form-1"


@pytest.mark.asyncio
async def test_respond_permission_keeps_matching_pending_without_ack(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.pending_permission = json.dumps({
        "request_id": "req-1", "tool_name": "Bash", "message": "Approve use of Bash?", "tool_input": "",
    })

    pcm._broker.register(chat.chat_id, ChatStream(prompt_text="x"))

    result = await pcm.respond_permission(
        chat.chat_id, request_id="req-1", approved=True, reason=""
    )

    # No provider acknowledgement means the persisted card stays retryable.
    assert result.ok is False
    assert result.retryable is True
    assert pcm._chats[chat.chat_id].pending_permission != ""


@pytest.mark.asyncio
async def test_respond_permission_ignores_stale_reply_for_a_superseded_request(
    tmp_path: Path,
) -> None:
    """A late reply for an already-superseded prompt must not wipe a newer one."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.pending_permission = json.dumps({
        "request_id": "req-2", "tool_name": "Bash", "message": "Approve use of Bash?", "tool_input": "",
    })

    await pcm.respond_permission(chat.chat_id, request_id="req-1", approved=True, reason="")

    assert json.loads(pcm._chats[chat.chat_id].pending_permission)["request_id"] == "req-2"


@pytest.mark.asyncio
async def test_persisted_question_without_a_provider_cancel_clears_it(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="persisted form", provider="opencode")
    chat.pending_question = json.dumps({
        "request_id": "form-1",
        "session_id": "ses_form",
        "questions": [{"id": "q1", "question": "Continue?"}],
    })

    result = await pcm.respond_question(
        chat.chat_id,
        request_id="form-1",
        answers={},
        action="cancel",
    )

    assert result.ok is True
    assert chat.pending_question == ""


@pytest.mark.asyncio
async def test_opencode_disconnect_is_not_authoritative_question_stale(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, title="connected form", provider="opencode"
    )
    chat.pending_question = json.dumps({
        "request_id": "form-1",
        "session_id": "ses_form",
        "questions": [{"id": "q1", "question": "Continue?"}],
    })
    provider = OpencodeAcpProvider(tmp_path)
    provider._session_id = "ses_form"

    async def disconnected(
        _request_id, _answers, *, cancel=False, session_id=""
    ):
        return QuestionResponseResult(False, "OpenCode is not connected", False)

    provider.respond_question = disconnected  # type: ignore[method-assign]
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)

    result = await pcm.respond_question(
        chat.chat_id,
        request_id="form-1",
        answers={},
        action="cancel",
    )

    assert result.ok is False
    assert json.loads(chat.pending_question)["request_id"] == "form-1"


@pytest.mark.asyncio
async def test_stale_question_reply_does_not_clear_newer_persisted_form(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, title="two forms", provider="opencode"
    )
    chat.pending_question = json.dumps({
        "request_id": "form-2",
        "session_id": "ses_two",
        "questions": [{"id": "q2", "question": "Newest?"}],
    })
    provider = OpencodeAcpProvider(tmp_path)
    provider._session_id = "ses_two"
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)

    result = await pcm.respond_question(
        chat.chat_id,
        request_id="form-1",
        answers={},
        action="cancel",
    )

    assert result.ok is True
    assert json.loads(chat.pending_question)["request_id"] == "form-2"


@pytest.mark.asyncio
async def test_stale_session_reply_does_not_clear_a_reused_request_id(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, title="reused permission", provider="opencode"
    )
    chat.pending_permission = json.dumps({
        "request_id": "permission-reused",
        "session_id": "ses_new",
        "tool_name": "shell",
        "message": "Approve the new session?",
    })
    provider = OpencodeAcpProvider(tmp_path)
    provider._session_id = "ses_new"
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)

    result = await pcm.respond_permission(
        chat.chat_id,
        request_id="permission-reused",
        approved=True,
        session_id="ses_old",
    )

    assert result.ok is True
    assert json.loads(chat.pending_permission)["session_id"] == "ses_new"


@pytest.mark.asyncio
async def test_stale_form_session_does_not_clear_a_reused_request_id(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, title="reused form", provider="opencode"
    )
    chat.pending_question = json.dumps({
        "request_id": "form-reused",
        "session_id": "ses_new",
        "questions": [{"id": "q1", "question": "Continue?"}],
    })
    provider = OpencodeAcpProvider(tmp_path)
    provider._session_id = "ses_new"
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)

    result = await pcm.respond_question(
        chat.chat_id,
        request_id="form-reused",
        answers={},
        action="cancel",
        session_id="ses_old",
    )

    assert result.ok is True
    assert json.loads(chat.pending_question)["session_id"] == "ses_new"


@pytest.mark.asyncio
async def test_respond_permission_returns_false_when_no_provider(tmp_path: Path) -> None:
    """A permission reply for a chat with no provider yet must be a no-op."""
    pcm = _make_manager(tmp_path)
    ok = await pcm.respond_permission("no-such-chat", request_id="x", approved=True, reason="")
    assert ok.ok is False


@pytest.mark.asyncio
async def test_respond_permission_unknown_request_id_is_acknowledged_on_the_acp_path(
    tmp_path: Path,
) -> None:
    """A stale id on the ACP provider (Claude) is already settled: acknowledged, so the card clears."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t", provider="claude")
    provider_service = pcm._get_provider(chat.chat_id)
    assert provider_service.provider.response_session_id == ""  # no live session behind the reply

    ok = await pcm.respond_permission(
        chat.chat_id, request_id="never-asked", approved=True, reason=""
    )

    # A stale id is already settled; acknowledge it so clients clear the card.
    assert ok.ok is True


@pytest.mark.asyncio
async def test_respond_permission_strips_buffered_event_from_active_stream_on_the_acp_path(
    tmp_path: Path,
) -> None:
    """An answered permission must not replay on the next subscribe, on the ACP path too.

    Repro: the user opens a chat, the agent asks for Bash approval, the user taps Approve,
    then reopens the chat. Without buffer cleanup the broker replays the original
    ``permission_request`` and the PWA renders a phantom card for a settled request.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t", provider="claude")

    stream = ChatStream("hi")
    pcm._broker.register(chat.chat_id, stream)
    stream.publish({
        "type": "permission_request",
        "tool_name": "Bash",
        "tool_input": "ls",
        "message": "Approve use of Bash?",
        "request_id": "tool-99",
    })

    pcm._get_provider(chat.chat_id)

    # Stale reply (the ACP provider holds nothing pending) must still strip the buffer.
    await pcm.respond_permission(
        chat.chat_id, request_id="tool-99", approved=True, reason=""
    )

    replay = stream.buffered_events()
    assert all(
        ev.get("type") != "permission_request" for ev in replay
    ), f"buffered permission_request leaked into replay: {replay}"


@pytest.mark.asyncio
async def test_denial_retracts_the_tool_card_before_v2_consumes_the_request(
    tmp_path: Path,
) -> None:
    """A V2 permission id can differ from the tool-call id it gates."""
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    class _Provider(_MinimalProvider):
        capabilities = ProviderCapabilities(permissions=True)

        def tool_use_id_for_request(self, _request_id: str, session_id: str = "") -> str:
            return "call-1"

        async def respond_permission(
            self, _request_id: str, _approved: bool, _reason: str = "", *,
            session_id: str = "",
        ) -> QuestionResponseResult:
            # The real adapter removes its pending map before returning.
            return QuestionResponseResult(True)

    pcm._providers[chat.chat_id] = SimpleNamespace(provider=_Provider(tmp_path))
    stream = ChatStream("hi")
    pcm._broker.register(chat.chat_id, stream)
    stream.publish({
        "type": "tool_use",
        "tool_name": "write",
        "tool_use_id": "call-1",
        "file_touch": {"file_path": "notes.md", "action": "created"},
    })

    result = await pcm.respond_permission(
        chat.chat_id, request_id="permission-1", approved=False
    )
    assert result.ok is True
    events = stream.buffered_events()
    assert any(
        event.get("type") == "tool_denied"
        and event.get("tool_use_id") == "call-1"
        for event in events
    )
    tool_event = next(event for event in events if event.get("type") == "tool_use")
    assert "file_touch" not in tool_event


_OPTIONS = [
    {"optionId": "allow", "kind": "allow_once", "name": "Allow"},
    {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
]
_ALLOW = {"outcome": "selected", "optionId": "allow"}
_REJECT = {"outcome": "selected", "optionId": "deny"}


class _AcpPermissionProvider(_MinimalProvider):
    """An ACP adapter's permission surface, backed by the real PermissionBroker.

    ``broker.request`` is what the agent's ``session/request_permission`` awaits. Setting
    ``gate`` holds the ``respond_permission`` call inside the provider, like a slow ACP
    write, so a cancel or a rival reply can land while it is in flight.
    """

    capabilities = ProviderCapabilities(permissions=True)

    def __init__(self, root: Path, session_id: str = "s1") -> None:
        super().__init__(root)
        self.events: list[object] = []
        self.broker = PermissionBroker(self.events.append)
        self._session_id = session_id
        self.calls: list[tuple[str, bool, str]] = []  # (request_id, approved, session_id)
        self.fail_with = ""
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    @property
    def response_session_id(self) -> str:
        return self._session_id

    def has_permission_request(self, request_id: str, session_id: str = "") -> bool:
        return self.broker.has(request_id, session_id)

    def permission_request_session(self, request_id: str) -> str:
        return self.broker.session_of(request_id)

    def tool_use_id_for_request(self, request_id: str, session_id: str = "") -> str:
        event = next((
            event for event in self.events
            if isinstance(event, PermissionRequestEvent)
            and event.request_id == request_id
            and (not session_id or event.session_id == session_id)
        ), None)
        return event.tool_name if event is not None else ""

    async def respond_permission(
        self, request_id: str, approved: bool, reason: str = "", *,
        session_id: str = "",
    ) -> QuestionResponseResult:
        self.calls.append((request_id, approved, session_id))
        if self.fail_with:
            return QuestionResponseResult(False, self.fail_with, True)
        if self.gate is not None:
            self.entered.set()
            await self.gate.wait()
        return self.broker.respond(request_id, approved, session_id=session_id)


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _acp_chat(tmp_path: Path, session_id: str = "s1"):
    from ciao.web.chat_broker import ChatStream as BrokerChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="acp permissions")
    provider = _AcpPermissionProvider(tmp_path, session_id)
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)
    stream = BrokerChatStream("turn")
    pcm._broker.register(chat.chat_id, stream)
    return pcm, chat.chat_id, provider, stream


def _persist(pcm: ProjectChatManager, chat_id: str, request_id: str, session_id: str) -> None:
    """The persisted slot the host writes when a permission_request event arrives."""
    pcm._chats[chat_id].pending_permission = json.dumps({
        "request_id": request_id, "session_id": session_id,
        "tool_name": "Bash", "message": "Approve?", "tool_input": "ls",
    })


def _host_request_id(
    provider: _AcpPermissionProvider, provider_tool_id: str, session_id: str = "s1"
) -> str:
    return next(
        event.request_id
        for event in provider.events
        if isinstance(event, PermissionRequestEvent)
        and event.tool_name == provider_tool_id
        and event.session_id == session_id
    )


def _agent(provider: _AcpPermissionProvider, request_id: str, session_id: str = "s1") -> asyncio.Task:
    return asyncio.create_task(
        provider.broker.request(
            session_id, {"toolCallId": request_id, "title": request_id}, _OPTIONS
        )
    )


@pytest.mark.asyncio
async def test_two_parallel_permissions_are_each_answered_by_their_id(tmp_path: Path) -> None:
    pcm, chat_id, provider, stream = _acp_chat(tmp_path)
    stream.publish({"type": "tool_use", "tool_name": "write", "tool_use_id": "t1"})
    first, second = _agent(provider, "t1"), _agent(provider, "t2")
    await _settle()
    first_id, second_id = _host_request_id(provider, "t1"), _host_request_id(provider, "t2")
    _persist(pcm, chat_id, second_id, "s1")

    approved = await pcm.respond_permission(chat_id, request_id=second_id, approved=True, session_id="s1")
    denied = await pcm.respond_permission(chat_id, request_id=first_id, approved=False, session_id="s1")

    assert approved.ok and denied.ok
    assert await second == _ALLOW
    assert await first == _REJECT
    assert provider.calls == [(second_id, True, "s1"), (first_id, False, "s1")]
    assert any(
        event.get("type") == "tool_denied" and event.get("tool_use_id") == "t1"
        for event in stream.buffered_events()
    )


@pytest.mark.asyncio
async def test_older_reply_does_not_clear_the_newer_persisted_slot(tmp_path: Path) -> None:
    pcm, chat_id, provider, _stream = _acp_chat(tmp_path)
    older, newer = _agent(provider, "t1"), _agent(provider, "t2")
    await _settle()
    older_id, newer_id = _host_request_id(provider, "t1"), _host_request_id(provider, "t2")
    _persist(pcm, chat_id, newer_id, "s1")

    result = await pcm.respond_permission(chat_id, request_id=older_id, approved=True, session_id="s1")

    assert result.ok is True
    assert await older == _ALLOW
    assert json.loads(pcm._chats[chat_id].pending_permission)["request_id"] == newer_id
    assert provider.broker.has(newer_id)
    provider.broker.cancel_all()
    assert await newer == {"outcome": "cancelled"}


@pytest.mark.asyncio
async def test_deny_losing_to_approve_is_already_resolved_and_keeps_the_card(tmp_path: Path) -> None:
    pcm, chat_id, provider, stream = _acp_chat(tmp_path)
    stream.publish({
        "type": "tool_use", "tool_name": "write", "tool_use_id": "t1",
        "file_touch": {"file_path": "notes.md", "action": "created"},
    })
    agent = _agent(provider, "t1")
    await _settle()
    request_id = _host_request_id(provider, "t1")
    _persist(pcm, chat_id, request_id, "s1")

    approve, deny = await asyncio.gather(
        pcm.respond_permission(chat_id, request_id=request_id, approved=True, session_id="s1"),
        pcm.respond_permission(chat_id, request_id=request_id, approved=False, session_id="s1"),
    )

    assert approve.ok is True and approve.already_resolved is False
    assert deny.ok is True and deny.already_resolved is True
    assert await agent == _ALLOW
    assert not any(event.get("type") == "tool_denied" for event in stream.buffered_events())


@pytest.mark.asyncio
async def test_reply_for_an_already_settled_request_is_already_resolved(tmp_path: Path) -> None:
    pcm, chat_id, provider, stream = _acp_chat(tmp_path)
    agent = _agent(provider, "t1")
    await _settle()
    request_id = _host_request_id(provider, "t1")
    _persist(pcm, chat_id, request_id, "s1")
    assert (await pcm.respond_permission(chat_id, request_id=request_id, approved=True, session_id="s1")).ok

    again = await pcm.respond_permission(chat_id, request_id=request_id, approved=False, session_id="s1")

    assert again.ok is True and again.already_resolved is True
    assert await agent == _ALLOW
    assert not any(event.get("type") == "tool_denied" for event in stream.buffered_events())


@pytest.mark.asyncio
async def test_subagent_permission_is_answered_on_its_own_session(tmp_path: Path) -> None:
    pcm, chat_id, provider, _stream = _acp_chat(tmp_path, session_id="parent")
    agent = _agent(provider, "c1", session_id="child")
    await _settle()
    request_id = _host_request_id(provider, "c1", "child")
    _persist(pcm, chat_id, request_id, "child")

    wrong = await pcm.respond_permission(chat_id, request_id=request_id, approved=True, session_id="parent")
    assert wrong.ok is True
    assert provider.calls == []
    assert provider.broker.has(request_id)

    right = await pcm.respond_permission(chat_id, request_id=request_id, approved=True, session_id="child")

    assert right.ok is True
    assert provider.calls == [(request_id, True, "child")]
    assert await agent == _ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_mid_reply", [False, True])
async def test_reply_and_cancel_race_resolves_the_agent_exactly_once(
    tmp_path: Path, cancel_mid_reply: bool
) -> None:
    """Cancel answers the pending prompt; the reply makes one provider call and loses cleanly."""
    pcm, chat_id, provider, _stream = _acp_chat(tmp_path)
    agent = _agent(provider, "t1")
    await _settle()
    request_id = _host_request_id(provider, "t1")
    _persist(pcm, chat_id, request_id, "s1")

    if cancel_mid_reply:
        provider.gate = asyncio.Event()
        reply_task = asyncio.create_task(
            pcm.respond_permission(chat_id, request_id=request_id, approved=True, session_id="s1")
        )
        await provider.entered.wait()  # the reply is held inside its provider call
        cancelled = provider.broker.cancel_all()
        provider.gate.set()
        reply = await reply_task
    else:
        cancelled = provider.broker.cancel_all()
        reply = await pcm.respond_permission(chat_id, request_id=request_id, approved=True, session_id="s1")

    assert cancelled == 1
    assert reply.ok is True and reply.already_resolved is True
    assert len(provider.calls) == 1
    assert await agent == {"outcome": "cancelled"}
    assert not provider.broker.has(request_id)


@pytest.mark.asyncio
async def test_failed_reply_log_omits_the_error_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pcm, chat_id, provider, _stream = _acp_chat(tmp_path)
    agent = _agent(provider, "t1")
    await _settle()
    request_id = _host_request_id(provider, "t1")
    _persist(pcm, chat_id, request_id, "s1")
    provider.fail_with = "adapter wrote /secret/path with token abc123"

    caplog.set_level("INFO")
    result = await pcm.respond_permission(chat_id, request_id=request_id, approved=True, session_id="s1")

    assert result.ok is False
    assert "failed" in caplog.text
    assert "abc123" not in caplog.text and "/secret/path" not in caplog.text
    provider.broker.cancel_all()
    assert await agent == {"outcome": "cancelled"}


