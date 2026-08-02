from __future__ import annotations

import json
from types import SimpleNamespace

from ciao.custom_providers import (
    CustomProvider,
    discover_models,
    encode_model,
    env_for_model,
    load_custom_providers,
    parse_model,
    save_custom_providers,
)
from ciao.app_settings import AppSettingsStore


def _config(tmp_path):
    return SimpleNamespace(state_path=tmp_path / ".runtime" / "state.json")


def test_custom_provider_round_trip_masks_token_and_routes_to_claude(tmp_path):
    config = _config(tmp_path)
    saved = save_custom_providers(config, [{
        "id": "lm-studio",
        "name": "LM Studio",
        "url": "http://localhost:1234/v1",
        "token": "local-secret",
        "runner": "claude",
        "models": "qwen2.5-coder, qwen2.5-coder",
    }])

    model = encode_model("lm-studio", "qwen2.5-coder")
    assert parse_model(model) == ("lm-studio", "qwen2.5-coder")
    assert env_for_model(config, model)["ANTHROPIC_BASE_URL"] == "http://localhost:1234/v1"
    assert env_for_model(config, model)["ANTHROPIC_AUTH_TOKEN"] == "local-secret"
    assert saved[0].models == ("qwen2.5-coder",)
    assert "local-secret" not in (tmp_path / ".ciao" / "custom_providers.json").read_text()
    assert "local-secret" in (tmp_path / ".runtime" / "custom_provider_tokens.json").read_text()
    assert load_custom_providers(config)[0].id == "lm-studio"


def test_custom_provider_routes_codex_to_openai_compat_env(tmp_path):
    config = _config(tmp_path)
    save_custom_providers(config, [{
        "id": "unsloth",
        "name": "Unsloth",
        "url": "http://127.0.0.1:30000/v1",
        "token": "token",
        "runner": "codex",
        "models": ["Qwen3-Coder"],
    }])

    env = env_for_model(config, encode_model("unsloth", "Qwen3-Coder"))
    assert env == {
        "OPENAI_BASE_URL": "http://127.0.0.1:30000/v1",
        "OPENAI_API_KEY": "token",
    }


def test_existing_token_is_preserved_when_settings_edit_omits_it(tmp_path):
    config = _config(tmp_path)
    save_custom_providers(config, [{
        "id": "ollama",
        "name": "Ollama",
        "url": "http://localhost:11434",
        "token": "ollama-token",
        "runner": "claude",
        "models": ["llama3"],
    }])
    save_custom_providers(config, [{
        "id": "ollama",
        "name": "Ollama local",
        "url": "http://localhost:11434",
        "runner": "claude",
        "models": ["llama3"],
    }])
    assert load_custom_providers(config)[0].token == "ollama-token"


def test_custom_provider_model_discovery(monkeypatch):
    payload = {"data": [{"id": "model-a"}, {"id": "model-a"}, {"id": "model-b"}]}

    class Response:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    provider = CustomProvider("local", "Local", "http://localhost:1234/v1", "", "claude")
    assert discover_models(provider) == ("model-a", "model-b")


def test_custom_tier_routes_are_persisted_as_user_settings(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    store.update({
        "custom_routing": {
            "lm-studio": {"haiku": "custom:lm-studio:qwen2.5-coder"},
        }
    })
    reloaded = AppSettingsStore(tmp_path / "app_settings.json")
    assert reloaded.settings.custom_routing == {
        "lm-studio": {"haiku": "custom:lm-studio:qwen2.5-coder"}
    }
