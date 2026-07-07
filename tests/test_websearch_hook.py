"""Tests for the PostToolUse WebSearch backfill hook.

On Ollama-cloud-routed chats, Claude Code's built-in WebSearch returns an
empty boilerplate because Ollama's Anthropic-compat layer doesn't execute
the server-side web_search tool. The hook reruns the query against Ollama's
standalone /api/web_search and injects the real results as additionalContext.
These tests stub the network call and pin the gating logic.
"""

from __future__ import annotations

import asyncio

import pytest

from ciao.observability.hooks import (
    _format_search_results,
    _ollama_cloud_route,
    _openrouter_route,
    _openrouter_web_search,
    _websearch_response_has_results,
    build_web_search_post_tooluse_hook,
)

EMPTY_BOILERPLATE = (
    'Web search results for query: "capital of France"\n\n'
    "I'll search the web for that query right away.\n\n\n"
    "REMINDER: You MUST include the sources above in your response to the user "
    "using markdown hyperlinks."
)

REAL_RESULTS = (
    'Web search results for query: "capital of France"\n\n'
    "1. Paris - https://www.britannica.com/place/Paris\n"
    "Paris is the capital of France."
)


# --- response detection ------------------------------------------------------

def test_empty_boilerplate_detected_as_no_results() -> None:
    assert _websearch_response_has_results(EMPTY_BOILERPLATE) is False


def test_real_results_with_http_detected() -> None:
    assert _websearch_response_has_results(REAL_RESULTS) is True


def test_none_response_is_no_results() -> None:
    assert _websearch_response_has_results(None) is False


# --- route gating ------------------------------------------------------------

def test_ollama_cloud_route_returns_credentials() -> None:
    route = _ollama_cloud_route(
        {"ANTHROPIC_BASE_URL": "https://ollama.com", "ANTHROPIC_AUTH_TOKEN": "key123"}
    )
    assert route == ("https://ollama.com", "key123")


def test_local_daemon_route_is_skipped() -> None:
    # The local daemon (localhost:11434, literal "ollama" token) doesn't
    # expose the standalone /api/web_search API.
    route = _ollama_cloud_route(
        {"ANTHROPIC_BASE_URL": "http://localhost:11434", "ANTHROPIC_AUTH_TOKEN": "ollama"}
    )
    assert route is None


def test_non_ollama_route_is_skipped() -> None:
    # Anthropic-path chats have no ANTHROPIC_BASE_URL in extra_env.
    route = _ollama_cloud_route({"GWS_PROFILE": "personal"})
    assert route is None


def test_kill_switch_disables_route() -> None:
    route = _ollama_cloud_route(
        {
            "ANTHROPIC_BASE_URL": "https://ollama.com",
            "ANTHROPIC_AUTH_TOKEN": "key123",
            "CIAO_OLLAMA_WEBSEARCH_HOOK": "0",
        }
    )
    assert route is None


def test_openrouter_route_returns_credentials_and_search_model() -> None:
    route = _openrouter_route(
        {
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            "ANTHROPIC_AUTH_TOKEN": "sk-or-1",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-haiku-4.5",
        }
    )
    assert route == ("https://openrouter.ai/api", "sk-or-1", "anthropic/claude-haiku-4.5")


def test_openrouter_route_falls_back_to_default_search_model() -> None:
    route = _openrouter_route(
        {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_AUTH_TOKEN": "sk-or-1"}
    )
    assert route is not None
    assert route[2] == "openai/gpt-4o-mini"


def test_openrouter_route_skipped_on_other_backends() -> None:
    assert _openrouter_route({"GWS_PROFILE": "personal"}) is None
    assert _openrouter_route(
        {"ANTHROPIC_BASE_URL": "https://ollama.com", "ANTHROPIC_AUTH_TOKEN": "key123"}
    ) is None


def test_openrouter_kill_switch_disables_route() -> None:
    route = _openrouter_route(
        {
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            "ANTHROPIC_AUTH_TOKEN": "sk-or-1",
            "CIAO_OPENROUTER_WEBSEARCH_HOOK": "0",
        }
    )
    assert route is None


# --- formatting --------------------------------------------------------------

def test_format_caps_results_and_truncates_content() -> None:
    results = [
        {"title": f"T{i}", "url": f"https://x.io/{i}", "content": "C" * 1000}
        for i in range(10)
    ]
    out = _format_search_results("q", results, max_results=3, content_chars=50)
    assert "1. T0" in out
    assert "2. T1" in out
    assert "3. T2" in out
    # 4th result is dropped by the cap
    assert "T3" not in out
    # content truncated
    assert "..." in out
    assert "C" * 1000 not in out


# --- callback end-to-end (network stubbed) -----------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_callback_backfills_empty_websearch_on_ollama_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.observability.hooks._ollama_web_search",
        lambda base_url, api_key, query, timeout_s=10.0: [
            {"title": "Paris", "url": "https://britannica.com/place/Paris", "content": "Capital of France."},
        ],
    )
    hook = build_web_search_post_tooluse_hook(
        {"ANTHROPIC_BASE_URL": "https://ollama.com", "ANTHROPIC_AUTH_TOKEN": "key123"}
    )
    out = _run(hook(
        {"tool_name": "WebSearch", "tool_input": {"query": "capital of France"},
         "tool_response": EMPTY_BOILERPLATE},
        None,
        None,
    ))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Paris" in ctx
    assert "https://britannica.com/place/Paris" in ctx
    assert "Capital of France." in ctx


def test_callback_backfills_empty_websearch_on_openrouter_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.observability.hooks._openrouter_web_search",
        lambda base_url, api_key, model, query, timeout_s=20.0: [
            {"title": "Paris", "url": "https://britannica.com/place/Paris", "content": "Capital of France."},
        ],
    )
    hook = build_web_search_post_tooluse_hook(
        {
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            "ANTHROPIC_AUTH_TOKEN": "sk-or-1",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-haiku-4.5",
        }
    )
    out = _run(hook(
        {"tool_name": "WebSearch", "tool_input": {"query": "capital of France"},
         "tool_response": EMPTY_BOILERPLATE},
        None,
        None,
    ))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "via OpenRouter" in ctx
    assert "https://britannica.com/place/Paris" in ctx


def test_openrouter_web_search_parses_url_citations(monkeypatch) -> None:
    class _FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def read(self) -> bytes:
            import json as _json
            return _json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    payload = {
        "choices": [
            {
                "message": {
                    "content": "Paris is the capital.",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {
                                "url": "https://britannica.com/place/Paris",
                                "title": "Paris",
                                "content": "Capital of France.",
                            },
                        },
                        {"type": "other", "data": {}},
                    ],
                }
            }
        ]
    }
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=20.0: _FakeResponse(payload)
    )
    results = _openrouter_web_search(
        "https://openrouter.ai/api", "sk-or-1", "anthropic/claude-haiku-4.5", "capital of France"
    )
    assert results == [
        {
            "title": "Paris",
            "url": "https://britannica.com/place/Paris",
            "content": "Capital of France.",
        }
    ]


def test_callback_noop_when_results_already_present(monkeypatch) -> None:
    # If native WebSearch worked, never call Ollama.
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not be called")

    monkeypatch.setattr("ciao.observability.hooks._ollama_web_search", _boom)
    hook = build_web_search_post_tooluse_hook(
        {"ANTHROPIC_BASE_URL": "https://ollama.com", "ANTHROPIC_AUTH_TOKEN": "key123"}
    )
    out = _run(hook(
        {"tool_name": "WebSearch", "tool_input": {"query": "x"}, "tool_response": REAL_RESULTS},
        None, None,
    ))
    assert out == {}
    assert called["n"] == 0


def test_callback_noop_on_anthropic_path(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.observability.hooks._ollama_web_search",
        lambda *a, **k: pytest.fail("should not be called"),
    )
    hook = build_web_search_post_tooluse_hook({"GWS_PROFILE": "personal"})
    out = _run(hook(
        {"tool_name": "WebSearch", "tool_input": {"query": "x"},
         "tool_response": EMPTY_BOILERPLATE},
        None, None,
    ))
    assert out == {}


def test_callback_noop_for_other_tools(monkeypatch) -> None:
    hook = build_web_search_post_tooluse_hook(
        {"ANTHROPIC_BASE_URL": "https://ollama.com", "ANTHROPIC_AUTH_TOKEN": "key123"}
    )
    out = _run(hook(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "a\nb"},
        None, None,
    ))
    assert out == {}


def test_callback_noop_when_ollama_returns_no_results(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.observability.hooks._ollama_web_search", lambda *a, **k: []
    )
    hook = build_web_search_post_tooluse_hook(
        {"ANTHROPIC_BASE_URL": "https://ollama.com", "ANTHROPIC_AUTH_TOKEN": "key123"}
    )
    out = _run(hook(
        {"tool_name": "WebSearch", "tool_input": {"query": "x"},
         "tool_response": EMPTY_BOILERPLATE},
        None, None,
    ))
    assert out == {}


def test_callback_failopen_on_exception(monkeypatch) -> None:
    def _raise(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("ciao.observability.hooks._ollama_web_search", _raise)
    hook = build_web_search_post_tooluse_hook(
        {"ANTHROPIC_BASE_URL": "https://ollama.com", "ANTHROPIC_AUTH_TOKEN": "key123"}
    )
    out = _run(hook(
        {"tool_name": "WebSearch", "tool_input": {"query": "x"},
         "tool_response": EMPTY_BOILERPLATE},
        None, None,
    ))
    assert out == {}