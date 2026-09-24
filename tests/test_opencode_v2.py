"""Focused mocked coverage for the opencode V2 server API."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    ImageAttachment,
    PermissionRequestEvent,
    ToolUseEvent,
)
from ciao.providers.opencode import (
    OpencodeProvider,
    _api_path,
    _catalog_from_providers,
    _normalize_messages,
    _opencode_messages_signature,
    _probe_api_version,
    _read_message_list,
    _v2_prompt_body,
    _set_api_version,
    missing_required_paths,
)


class _Response:
    def __init__(self, payload: object = None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _V2Client:
    def __init__(self) -> None:
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, object]] = []
        self.delete_calls: list[str] = []
        self.responses: dict[str, object] = {}
        _set_api_version(self, "v2")

    async def get(self, path: str, **_kwargs: Any) -> _Response:
        self.get_calls.append(path)
        return _Response(self.responses.get(path))

    async def post(self, path: str, json: object = None) -> _Response:
        self.post_calls.append((path, json))
        return _Response({"data": {"id": "ses_v2"}})

    async def delete(self, path: str) -> _Response:
        self.delete_calls.append(path)
        return _Response(status_code=204)


@pytest.mark.asyncio
async def test_v2_session_creation_uses_v2_rules_and_data_envelope(tmp_path: Path) -> None:
    provider = OpencodeProvider(tmp_path)
    client = _V2Client()
    provider._client = client  # type: ignore[assignment]
    request = AgentRequest(
        prompt="hello",
        model="opencode/space-bunny-free",
        mode="auto",
        provider="opencode",
        thinking_level="high",
    )
    assert await provider._ensure_session(request) == "ses_v2"
    assert client.post_calls[0][0] == "/api/session"
    body = client.post_calls[0][1]
    assert isinstance(body, dict)
    assert body["permissions"][0] == {"action": "*", "resource": "*", "effect": "allow"}
    assert {"action": "shell", "resource": "*", "effect": "ask"} in body["permissions"]
    assert all(rule["action"] != "bash" for rule in body["permissions"])
    assert body["model"] == {
        "id": "space-bunny-free",
        "providerID": "opencode",
        "variant": "high",
    }


@pytest.mark.asyncio
async def test_v2_empty_model_uses_server_default_for_new_sessions(tmp_path: Path) -> None:
    provider = OpencodeProvider(tmp_path)
    client = _V2Client()
    client.responses["/api/model/default"] = {"data": {
        "modelID": "default-model", "providerID": "opencode", "id": "opencode/default-model",
    }}
    provider._client = client  # type: ignore[assignment]
    request = AgentRequest(prompt="hello", model="", mode="auto", provider="opencode")
    assert await provider._ensure_session(request) == "ses_v2"
    body = client.post_calls[0][1]
    assert isinstance(body, dict)
    assert body["model"] == {"id": "default-model", "providerID": "opencode"}


@pytest.mark.asyncio
async def test_v2_empty_model_on_resume_retains_session_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ciao.providers.opencode as module

    provider = OpencodeProvider(tmp_path)
    client = _V2Client()
    client.responses["/api/session/ses_resume"] = {"data": {
        "id": "ses_resume", "permissions": [],
    }}
    provider._client = client  # type: ignore[assignment]
    monkeypatch.setattr(module, "_session_permission_matches", lambda *_args: True)
    request = AgentRequest(
        prompt="continue", model="", mode="auto", provider="opencode",
        resume_session="ses_resume",
    )
    assert await provider._ensure_session(request) == "ses_resume"
    assert client.post_calls == [(
        "/api/session/ses_resume/agent", {"agent": "build"},
    )]


def test_v2_contract_does_not_require_a_missing_children_endpoint() -> None:
    spec = {"paths": {
        "/api/info": {}, "/api/event": {}, "/api/session": {},
        "/api/session/{sessionID}": {}, "/api/session/{sessionID}/fork": {},
        "/api/session/{sessionID}/message": {}, "/api/session/{sessionID}/prompt": {},
        "/api/session/{sessionID}/interrupt": {}, "/api/session/{sessionID}/agent": {},
        "/api/session/{sessionID}/model": {},
        "/api/session/{sessionID}/permission": {},
        "/api/session/{sessionID}/permission/{requestID}/reply": {},
        "/api/session/{sessionID}/form": {},
        "/api/session/{sessionID}/form/{formID}": {},
        "/api/session/{sessionID}/form/{formID}/reply": {},
        "/api/provider": {}, "/api/model": {}, "/api/model/default": {},
    }}
    assert missing_required_paths(spec, "v2") == ()


class _V1InfoClient:
    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def get(self, path: str, **_kwargs: Any) -> _Response:
        self.get_calls.append(path)
        if path == "/api/info":
            return _Response(status_code=404)
        return _Response({"healthy": True, "version": "1.18.18"})


class _InfoClient:
    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def get(self, path: str, **_kwargs: Any) -> _Response:
        self.get_calls.append(path)
        if path == "/api/info":
            return _Response({"version": "2.0.16"})
        return _Response({"healthy": True, "version": "1.18.18"})


@pytest.mark.asyncio
async def test_v1_health_remains_the_fallback() -> None:
    client = _V1InfoClient()
    version, _status, _error = await _probe_api_version(client)
    assert version == "v1"
    assert client.get_calls == ["/api/info", "/global/health"]


@pytest.mark.asyncio
async def test_v2_info_wins_over_legacy_health() -> None:
    client = _InfoClient()
    version, _status, _error = await _probe_api_version(client)
    assert version == "v2"
    assert client.get_calls == ["/api/info"]


def test_v2_event_translation_handles_data_envelope_and_form() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-test"))
    text = provider._event_to_stream({
        "type": "session.text.delta",
        "data": {"sessionID": "ses_v2", "assistantMessageID": "msg_1", "ordinal": 0, "delta": "OK"},
    })
    assert isinstance(text[0], AssistantTextDelta)
    assert text[0].text == "OK"

    form = provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {
            "id": "frm_1",
            "sessionID": "ses_v2",
            "fields": [{
                "key": "choice",
                "title": "Choice",
                "type": "string",
                "description": "Choose carefully",
                "options": [{"value": "a", "label": "A", "description": "First"}],
            }],
        }},
    })
    assert isinstance(form[0], ToolUseEvent)
    form_payload = json.loads(form[0].tool_input)
    assert form_payload["questions"][0]["header"] == "Choice"
    assert form_payload["questions"][0]["question"] == "Choose carefully"
    assert form_payload["questions"][0]["options"] == [{
        "label": "A", "value": "a", "description": "First"
    }]
    pending = provider._question_requests["frm_1"]
    assert pending.form is True
    assert pending.question_ids == ("choice",)


@pytest.mark.asyncio
async def test_v2_permission_uses_session_scoped_decision_body() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-test"))
    client = _V2Client()
    provider._client = client  # type: ignore[assignment]
    permission_events = provider._event_to_stream({
        "type": "permission.v2.asked",
        "data": {
            "id": "per_1",
            "sessionID": "ses_v2",
            "action": "shell",
            "resources": ["echo hi"],
            "message": "needs operator approval",
        },
    })
    assert isinstance(permission_events[0], PermissionRequestEvent)
    assert permission_events[0].tool_input == "needs operator approval"
    assert isinstance(provider._event_to_stream({
        "type": "permission.v2.asked",
        "data": {"id": "per_2", "sessionID": "ses_v2", "action": "shell", "resources": ["echo hi"]},
    })[0], PermissionRequestEvent)
    assert provider.send_permission_response("per_1", True) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert client.post_calls == [
        ("/api/session/ses_v2/permission/per_1/reply", {"decision": "once"})
    ]


@pytest.mark.asyncio
async def test_v2_form_reply_preserves_semantics_and_empty_optional_values() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-form"))
    client = _V2Client()
    provider._client = client  # type: ignore[assignment]
    event = provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {
            "id": "frm_semantics", "sessionID": "ses_v2", "title": "Details",
            "fields": [
                {"key": "choice", "type": "string", "required": True,
                 "options": [{"value": "a", "label": "A"}], "when": [{"key": "show", "op": "eq", "value": "yes"}]},
                {"key": "show", "type": "string", "required": True, "options": [{"value": "yes", "label": "Yes"}]},
                {"key": "optional", "type": "string", "required": False},
                {"key": "optional_number", "type": "number", "required": False},
                {"key": "optional_bool", "type": "boolean", "required": False},
                {"key": "auth", "type": "external", "url": "https://example.test/auth"},
                {"key": "secretish", "type": "string", "hidden": True, "required": True},
            ],
        }},
    })
    payload = json.loads(event[0].tool_input)
    fields = {field["id"]: field for field in payload["questions"]}
    assert fields["choice"]["required"] is True
    assert fields["choice"]["when"] == [{"key": "show", "op": "eq", "value": "yes"}]
    assert fields["auth"]["url"] == "https://example.test/auth"
    assert fields["auth"]["required"] is True
    assert fields["secretish"]["hidden"] is True

    assert await provider.send_question_response_async("frm_semantics", {
        "show": ["Yes"], "choice": ["A"], "optional": [""],
        "optional_number": [""], "optional_bool": [""], "auth": ["acknowledged"],
    }) is True
    assert client.post_calls[-1] == (
        "/api/session/ses_v2/form/frm_semantics/reply",
        {"answer": {"choice": "a", "show": "yes", "optional": "", "auth": True}},
    )


@pytest.mark.asyncio
async def test_v2_form_distinguishes_submit_with_no_optional_scalar_from_cancel() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-empty-scalar"))
    client = _V2Client()
    provider._client = client  # type: ignore[assignment]
    provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {
            "id": "frm_scalar", "sessionID": "ses_v2", "fields": [
                {"key": "amount", "type": "number", "required": False},
            ],
        }},
    })
    assert await provider.send_question_response_async(
        "frm_scalar", {"amount": [""]}
    ) is True
    assert client.post_calls[-1] == (
        "/api/session/ses_v2/form/frm_scalar/reply", {"answer": {}}
    )

    provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {
            "id": "frm_closed", "sessionID": "ses_v2", "fields": [
                {"key": "choice", "type": "string", "required": False,
                 "options": [{"value": "yes", "label": "Yes"}]},
            ],
        }},
    })
    assert await provider.send_question_response_async(
        "frm_closed", {"choice": [""]}
    ) is True
    assert client.post_calls[-1] == (
        "/api/session/ses_v2/form/frm_closed/reply", {"answer": {}}
    )

    provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {
            "id": "frm_cancel_scalar", "sessionID": "ses_v2", "fields": [
                {"key": "amount", "type": "number", "required": False},
            ],
        }},
    })
    assert await provider.send_question_response_async(
        "frm_cancel_scalar", {}, cancel=True
    ) is True
    assert client.delete_calls[-1] == "/api/session/ses_v2/form/frm_cancel_scalar"


@pytest.mark.asyncio
async def test_v2_form_conditions_use_typed_number_and_boolean_answers() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-typed-form"))
    client = _V2Client()
    provider._client = client  # type: ignore[assignment]
    provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {
            "id": "frm_typed", "sessionID": "ses_v2", "fields": [
                {"key": "amount", "type": "number", "required": True},
                {"key": "enabled", "type": "boolean", "required": True},
                {"key": "amount_detail", "type": "string", "required": False,
                 "when": [{"key": "amount", "op": "eq", "value": 2}]},
                {"key": "enabled_detail", "type": "string", "required": False,
                 "when": [{"key": "enabled", "op": "eq", "value": True}]},
            ],
        }},
    })
    assert await provider.send_question_response_async("frm_typed", {
        "amount": ["2"], "enabled": ["true"],
        "amount_detail": ["yes"], "enabled_detail": ["yes"],
    }) is True
    assert client.post_calls[-1][1] == {"answer": {
        "amount": 2, "enabled": True,
        "amount_detail": "yes", "enabled_detail": "yes",
    }}


@pytest.mark.asyncio
async def test_v2_empty_multiselect_still_satisfies_a_neq_condition() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-empty-condition"))
    client = _V2Client()
    provider._client = client  # type: ignore[assignment]
    provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {
            "id": "frm_empty_condition", "sessionID": "ses_v2", "fields": [
                {"key": "tags", "type": "multiselect", "required": False,
                 "options": [{"value": "blocked", "label": "Blocked"}]},
                {"key": "detail", "type": "string", "required": False,
                 "when": [{"key": "tags", "op": "neq", "value": "blocked"}]},
            ],
        }},
    })
    assert await provider.send_question_response_async("frm_empty_condition", {
        "tags": [], "detail": ["still needed"],
    }) is True
    assert client.post_calls[-1][1] == {"answer": {
        "tags": [], "detail": "still needed",
    }}


@pytest.mark.asyncio
async def test_v2_explicit_cancel_uses_the_reject_endpoint_for_non_form_questions() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-question-cancel"))
    client = _V2Client()
    provider._client = client  # type: ignore[assignment]
    provider._event_to_stream({
        "type": "question.v2.asked",
        "data": {
            "id": "q_cancel",
            "sessionID": "ses_v2",
            "questions": [{"id": "choice", "question": "Continue?"}],
        },
    })
    assert await provider.send_question_response_async("q_cancel", {}, cancel=True) is True
    assert client.post_calls[-1] == ("/api/question/q_cancel/reject", {})


@pytest.mark.asyncio
async def test_v2_failed_form_delivery_is_reported_and_keeps_pending_request() -> None:
    class FailingClient(_V2Client):
        async def post(self, path: str, json: object = None) -> _Response:
            self.post_calls.append((path, json))
            return _Response({}, status_code=503)

    provider = OpencodeProvider(Path("/tmp/opencode-v2-form-failure"))
    client = FailingClient()
    provider._client = client  # type: ignore[assignment]
    provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {"id": "frm_fail", "sessionID": "ses_v2", "fields": [
            {"key": "optional", "type": "string", "required": False},
        ]}},
    })
    assert await provider.send_question_response_async("frm_fail", {"optional": [""]}) is False
    assert "frm_fail" in provider._question_requests


@pytest.mark.asyncio
async def test_v2_form_reply_uses_answer_mapping_and_cancel_uses_delete() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-test"))
    client = _V2Client()
    provider._client = client  # type: ignore[assignment]
    provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {
            "id": "frm_1",
            "sessionID": "ses_v2",
            "fields": [
                {"key": "one", "type": "string", "title": "One"},
                {"key": "many", "type": "multiselect", "title": "Many"},
            ],
        }},
    })
    assert provider.send_question_response("frm_1", {"one": ["A"], "many": ["x", "y"]}) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert client.post_calls == [
        ("/api/session/ses_v2/form/frm_1/reply", {"answer": {"one": "A", "many": ["x", "y"]}})
    ]
    provider._event_to_stream({
        "type": "form.created",
        "data": {"form": {"id": "frm_2", "sessionID": "ses_v2", "fields": [{"key": "one", "type": "string"}]}},
    })
    assert provider.send_question_response("frm_2", {}) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert client.delete_calls == ["/api/session/ses_v2/form/frm_2"]


@pytest.mark.asyncio
async def test_v2_children_fallback_filters_parent_and_normalizes_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ciao.providers.opencode as module

    module._COLLAB_CACHE.clear()
    client = _V2Client()
    client.responses = {
        "/api/session?parentID=ses_parent&limit=1000": {"data": [
            {"id": "ses_child", "parentID": "ses_parent"},
            {"id": "ses_other", "parentID": "ses_elsewhere"},
        ]},
        "/api/session/ses_child/message?order=asc": {"data": [
            {"id": "msg_u", "type": "user", "text": "child prompt"},
        ]},
    }

    class Server:
        def __init__(self, _root: Path) -> None:
            pass

        async def __aenter__(self) -> _V2Client:
            return client

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(module, "_EphemeralServer", Server)
    tree = await OpencodeProvider.read_collab_tree(tmp_path, "ses_parent")
    assert tree == [{
        "info": {"id": "ses_child", "parentID": "ses_parent"},
        "messages": [{
            "info": {"id": "msg_u", "role": "user", "time": None},
            "parts": [{"type": "text", "text": "child prompt", "id": "msg_u:text"}],
        }],
    }]
    assert client.get_calls == [
        "/api/session?parentID=ses_parent&limit=1000",
        "/api/session/ses_child/message?order=asc",
    ]


@pytest.mark.asyncio
async def test_v2_message_reads_follow_cursor_pages() -> None:
    client = _V2Client()
    client.responses = {
        "/api/session/ses_v2/message?order=asc": {
            "data": [{"id": "u", "type": "user", "text": "one"}],
            "cursor": {"next": "cursor-2"},
        },
        "/api/session/ses_v2/message?cursor=cursor-2": {
            "data": [{"id": "a", "type": "assistant", "content": [
                {"type": "text", "text": "two"},
            ]}],
            "cursor": {"next": None},
        },
    }
    messages = await _read_message_list(client, "ses_v2", "v2")
    assert [message["info"]["role"] for message in messages] == ["user", "assistant"]
    assert client.get_calls == [
        "/api/session/ses_v2/message?order=asc",
        "/api/session/ses_v2/message?cursor=cursor-2",
    ]


@pytest.mark.asyncio
async def test_v2_model_catalog_retries_the_startup_empty_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ciao.providers.opencode as module

    module._MODEL_CACHE.clear()
    client = _V2Client()
    original_get = client.get

    async def get(path: str, **kwargs: Any) -> _Response:
        if len(client.get_calls) == 0:
            client.get_calls.append(path)
            return _Response({"data": []})
        return await original_get(path, **kwargs)

    client.get = get  # type: ignore[method-assign]
    client.responses = {"/api/model": {"data": [{
        "modelID": "model", "providerID": "opencode", "name": "Model",
        "capabilities": {"input": ["text"]}, "variants": [],
    }]}}

    class Server:
        def __init__(self, _root: Path) -> None:
            pass

        async def __aenter__(self) -> _V2Client:
            return client

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(module, "_EphemeralServer", Server)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    result = await OpencodeProvider.model_catalog(tmp_path, force=True)
    assert result[0]["model"] == "opencode/model"
    assert client.get_calls == ["/api/model", "/api/model"]


def test_v2_model_catalog_flattens_model_endpoint() -> None:
    payload = {"data": [{
        "id": "openrouter/model",
        "modelID": "model",
        "providerID": "openrouter",
        "name": "Model",
        "capabilities": {"input": ["text", "image"]},
        "variants": [{"id": "high"}, {"id": "low"}],
        "limit": {"context": 1234},
    }]}
    assert _catalog_from_providers(payload) == [{
        "model": "openrouter/model",
        "label": "Model (openrouter)",
        "variants": ["high", "low"],
        "images": True,
    }]
    text_only = {"data": [{
        "modelID": "text", "providerID": "opencode", "name": "Text",
        "capabilities": {"input": ["text"]}, "variants": [],
    }]}
    assert _catalog_from_providers(text_only)[0]["images"] is False


@pytest.mark.asyncio
async def test_v2_pending_requests_are_recovered_after_a_reconnect(tmp_path: Path) -> None:
    provider = OpencodeProvider(tmp_path)
    client = _V2Client()
    client.responses = {
        "/api/session/ses_v2/permission": {"data": [{
            "id": "per_pending", "sessionID": "ses_v2", "action": "shell", "resources": ["echo"],
        }]},
        "/api/session/ses_v2/form": {"data": [{
            "id": "frm_pending", "sessionID": "ses_v2",
            "fields": [{"key": "choice", "type": "string"}],
        }]},
    }
    provider._client = client  # type: ignore[assignment]
    events = await provider._recover_pending_v2_requests(client, "ses_v2")
    assert len(events) == 2
    assert "per_pending" in provider._permission_requests
    assert "frm_pending" in provider._question_requests


def test_v2_normalizes_message_envelope_for_transcript_renderers() -> None:
    messages = _normalize_messages({"data": [
        {"id": "msg_u", "type": "user", "text": "hello"},
        {"id": "msg_a", "type": "assistant", "model": {"id": "m", "providerID": "p"}, "content": [
            {"type": "text", "text": "answer"},
        ]},
    ]}, "v2")
    assert messages[0]["info"]["role"] == "user"
    assert messages[1]["parts"][0]["text"] == "answer"


def test_v1_helpers_remain_the_default() -> None:
    client = SimpleNamespace()
    assert _api_path(client, "/old", "/api/new") == "/old"
    assert missing_required_paths({"paths": {"/global/health": {}, "/session": {}}}) != ()


def test_v2_permission_policy_denies_root_credentials_and_broad_searches() -> None:
    from ciao.providers.opencode import _mode_rules_for_version

    _agent, rules = _mode_rules_for_version("bypass", "v2")
    assert {"action": "read", "resource": ".env", "effect": "deny"} in rules
    assert {"action": "edit", "resource": ".env", "effect": "deny"} in rules
    for action in ("glob", "grep", "list"):
        assert {"action": action, "resource": "*", "effect": "deny"} in rules
    assert not any(rule["action"] == "bash" for rule in rules)


def test_v2_prompt_keeps_core_and_runtime_in_supported_text() -> None:
    request = AgentRequest(
        prompt="Do the work",
        model="",
        mode="auto",
        provider="opencode",
        extra_env={"CIAO_ACTIVE_WORKSPACE": "personal"},
    )
    body = _v2_prompt_body(request, system_context="Ciaobot core\ntoday=2026-09-24")
    assert set(body) <= {"text", "files"}
    assert "Ciaobot core" in body["text"]
    assert "today=2026-09-24" in body["text"]
    assert "Do the work" in body["text"]


def test_v2_prompt_keeps_attachments_in_the_supported_uri_shape(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"abc")
    request = AgentRequest(
        prompt="Inspect",
        model="opencode/model",
        mode="auto",
        provider="opencode",
        images=[ImageAttachment(image_path, "image/png", "image.png", "caption")],
    )
    body = _v2_prompt_body(request)
    assert body["files"] == [{
        "uri": image_path.resolve().as_uri(),
        "name": "image.png",
        "description": "caption",
    }]


@pytest.mark.asyncio
async def test_v2_future_major_is_not_classified_as_v2() -> None:
    class FutureClient:
        async def get(self, path: str, **_kwargs: Any) -> _Response:
            if path == "/api/info":
                return _Response({"version": "3.0.0"})
            return _Response({"paths": {"/api/session": {}}})

    version, _status, error = await _probe_api_version(FutureClient())
    assert version is None
    assert "unsupported opencode server major version 3" in str(error)

    class HealthFutureClient:
        async def get(self, path: str, **_kwargs: Any) -> _Response:
            if path == "/api/info":
                return _Response({"error": "not found"}, status_code=404)
            if path == "/global/health":
                return _Response({"healthy": True, "version": "5.0.0"})
            return _Response({"paths": {"/api/session": {}}})

    version, _status, error = await _probe_api_version(HealthFutureClient())
    assert version is None
    assert "unsupported opencode server major version 5" in str(error)


@pytest.mark.asyncio
async def test_v2_health_fails_immediately_on_future_major(tmp_path: Path) -> None:
    class FutureClient:
        async def get(self, path: str, **_kwargs: Any) -> _Response:
            return _Response({"version": "4.1.0"})

    provider = OpencodeProvider(tmp_path)
    provider._client = FutureClient()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="unsupported opencode server major version 4"):
        await provider._await_health()


def test_v2_normalizes_inline_and_uri_attachments() -> None:
    messages = _normalize_messages({"data": [{
        "id": "msg_u",
        "type": "user",
        "text": "files",
        "files": [
            {"name": "inline.png", "mime": "image/png", "data": "YWJj", "source": {"type": "inline"}},
            {"name": "remote.txt", "mime": "text/plain", "source": {"type": "uri", "uri": "file:///tmp/remote.txt"}},
        ],
    }]}, "v2")
    parts = messages[0]["parts"]
    assert parts[1]["url"] == "data:image/png;base64,YWJj"
    assert parts[2]["url"] == "file:///tmp/remote.txt"

    nested = _normalize_messages({"data": [{
        "info": {"id": "msg_nested", "role": "user"},
        "parts": [{
            "type": "file", "name": "nested.txt", "mime": "text/plain",
            "source": {"type": "uri", "uri": "file:///tmp/nested.txt"},
        }],
    }]}, "v2")
    assert nested[0]["parts"][0]["url"] == "file:///tmp/nested.txt"


def test_message_signature_changes_when_a_tool_settles() -> None:
    running = [{
        "info": {"id": "a", "role": "assistant"},
        "parts": [{"id": "t1", "type": "tool", "name": "bash", "state": {"status": "running", "input": {"command": "echo hi"}}}],
    }]
    completed = [{
        "info": {"id": "a", "role": "assistant"},
        "parts": [{"id": "t1", "type": "tool", "name": "bash", "state": {"status": "completed", "input": {"command": "echo hi"}, "content": [{"type": "text", "text": "hi"}]}}],
    }]
    assert _opencode_messages_signature(running) != _opencode_messages_signature(completed)


def test_v2_tool_lifecycle_announces_and_settles_once() -> None:
    provider = OpencodeProvider(Path("/tmp/opencode-v2-tool-lifecycle"))
    started = provider._event_to_stream({
        "type": "session.tool.input.started",
        "data": {"callID": "call_1", "name": "read", "input": ""},
    })
    assert started == []
    ended = provider._event_to_stream({
        "type": "session.tool.input.ended",
        "data": {"callID": "call_1", "name": "read", "text": json.dumps({"filePath": "README.md"})},
    })
    assert [event.type for event in ended] == ["tool_use"]
    assert ended[0].tool_input  # type: ignore[attr-defined]
    assert provider._event_to_stream({
        "type": "session.tool.called",
        "data": {"callID": "call_1", "tool": "read", "input": {"filePath": "README.md"}},
    }) == []
    settled = provider._event_to_stream({
        "type": "session.tool.success",
        "data": {"callID": "call_1", "tool": "read"},
    })
    assert [event.type for event in settled] == ["tool_result"]
    assert provider._event_to_stream({
        "type": "session.tool.success",
        "data": {"callID": "call_1", "tool": "read"},
    }) == []
    assert provider._settled_tools == {"call_1"}


@pytest.mark.asyncio
async def test_v2_child_read_follows_cursor_pages() -> None:
    from ciao.providers.opencode import _read_v2_child_sessions

    class Client:
        def __init__(self) -> None:
            self.paths: list[str] = []

        async def get(self, path: str) -> _Response:
            self.paths.append(path)
            if "cursor=" not in path:
                return _Response({"data": [{"id": "c1", "parentID": "p"}], "cursor": {"next": "next-1"}})
            return _Response({"data": [{"id": "c2", "parentID": "p"}], "cursor": {"next": None}})

    client = Client()
    children = await _read_v2_child_sessions(client, "p")
    assert [child["id"] for child in children] == ["c1", "c2"]
    assert client.paths == [
        "/api/session?parentID=p&limit=1000",
        "/api/session?cursor=next-1",
    ]
