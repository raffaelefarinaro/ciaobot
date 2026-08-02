"""User-managed OpenAI/Anthropic-compatible provider endpoints.

Provider definitions are workspace settings: their names, URLs, runners, and
model lists live in the tracked ``.ciao/custom_providers.json`` file. Tokens
are credentials, so they stay in the gitignored runtime directory and are
never returned by the API.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_RUNNERS = frozenset({"claude", "codex"})


@dataclass(frozen=True, slots=True)
class CustomProvider:
    id: str
    name: str
    base_url: str
    token: str
    runner: str
    models: tuple[str, ...] = ()


def providers_path(config) -> Path:
    workspace_root = getattr(config, "workspace_root", None)
    if workspace_root is None:
        workspace_root = Path(config.state_path).expanduser().resolve().parent.parent
    return Path(workspace_root).expanduser().resolve() / ".ciao" / "custom_providers.json"


def provider_tokens_path(config) -> Path:
    return Path(config.state_path).expanduser().resolve().parent / "custom_provider_tokens.json"


def _legacy_providers_path(config) -> Path:
    return Path(config.state_path).expanduser().resolve().parent / "custom_providers.json"


def _clean_models(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        model = str(item).strip()
        if model and model not in seen and len(model) <= 256:
            seen.add(model)
            result.append(model)
    return tuple(result)


def normalize_base_url(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("custom provider url is required")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("custom provider url must be an http(s) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("custom provider url must not contain credentials or a fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), parsed.query, ""))


def _provider_from_mapping(raw: object, *, existing: CustomProvider | None = None) -> CustomProvider:
    if not isinstance(raw, dict):
        raise ValueError("each custom provider must be an object")
    provider_id = str(raw.get("id") or "").strip().lower()
    if not _ID_RE.fullmatch(provider_id):
        raise ValueError("custom provider id must start with a letter and contain only letters, numbers, '-' or '_'")
    name = str(raw.get("name") or provider_id).strip()
    if not name or len(name) > 80:
        raise ValueError("custom provider name must be 1-80 characters")
    runner = str(raw.get("runner") or "").strip().lower()
    if runner not in _RUNNERS:
        raise ValueError("custom provider runner must be 'claude' or 'codex'")
    token = str(raw["token"]) if "token" in raw else (existing.token if existing else "")
    return CustomProvider(
        id=provider_id,
        name=name,
        base_url=normalize_base_url(raw.get("url") or raw.get("base_url")),
        token=token.strip(),
        runner=runner,
        models=_clean_models(raw.get("models")),
    )


def load_custom_providers(config) -> tuple[CustomProvider, ...]:
    if not getattr(config, "workspace_root", None) and not getattr(config, "state_path", None):
        return ()
    path = providers_path(config)
    if not path.exists():
        # Migrate transparently from the original runtime-only layout. The
        # next settings save writes the split tracked/secret representation.
        path = _legacy_providers_path(config)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return ()
    entries = raw.get("providers") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return ()
    result: list[CustomProvider] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            provider = _provider_from_mapping(entry)
        except (TypeError, ValueError):
            continue
        try:
            tokens = json.loads(provider_tokens_path(config).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            tokens = {}
        if isinstance(tokens, dict):
            provider = CustomProvider(
                id=provider.id,
                name=provider.name,
                base_url=provider.base_url,
                token=str(tokens.get(provider.id, provider.token)).strip(),
                runner=provider.runner,
                models=provider.models,
            )
        if provider.id not in seen:
            seen.add(provider.id)
            result.append(provider)
    return tuple(result)


def save_custom_providers(config, entries: object) -> tuple[CustomProvider, ...]:
    if not isinstance(entries, list):
        raise ValueError("custom_providers must be an array")
    existing = {item.id: item for item in load_custom_providers(config)}
    result: list[CustomProvider] = []
    seen: set[str] = set()
    for entry in entries:
        provider = _provider_from_mapping(entry, existing=existing.get(str(entry.get("id", "")).strip().lower()) if isinstance(entry, dict) else None)
        if provider.id in seen:
            raise ValueError(f"duplicate custom provider id '{provider.id}'")
        seen.add(provider.id)
        result.append(provider)
    old_tokens = {
        item.id: item.token for item in load_custom_providers(config) if item.token
    }
    path = providers_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"providers": [
            {"id": item.id, "name": item.name, "url": item.base_url,
             "runner": item.runner, "models": list(item.models)}
            for item in result
        ]}, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    token_path = provider_tokens_path(config)
    tokens = {
        item.id: item.token
        for item in result
        if item.token or old_tokens.get(item.id)
    }
    for item in result:
        if not item.token and old_tokens.get(item.id):
            tokens[item.id] = old_tokens[item.id]
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    return tuple(result)


def public_provider(provider: CustomProvider) -> dict[str, object]:
    return {
        "id": provider.id,
        "name": provider.name,
        "url": provider.base_url,
        "runner": provider.runner,
        "models": list(provider.models),
        "token_configured": bool(provider.token),
    }


def encode_model(provider_id: str, model: str) -> str:
    return f"custom:{provider_id}:{model}"


def parse_model(model: str) -> tuple[str, str] | None:
    if not model.startswith("custom:"):
        return None
    parts = model.split(":", 2)
    if len(parts) != 3:
        return None
    _, provider_id, raw_model = parts
    if not _ID_RE.fullmatch(provider_id) or not raw_model:
        return None
    return provider_id, raw_model


def provider_for_model(config, model: str) -> CustomProvider | None:
    parsed = parse_model(model)
    if parsed is None:
        return None
    provider_id, _ = parsed
    return next((item for item in load_custom_providers(config) if item.id == provider_id), None)


def runtime_model(model: str) -> str:
    parsed = parse_model(model)
    return parsed[1] if parsed else model


def env_for_model(config, model: str) -> dict[str, str]:
    provider = provider_for_model(config, model)
    if provider is None:
        return {}
    if provider.runner == "codex":
        return {"OPENAI_BASE_URL": provider.base_url, "OPENAI_API_KEY": provider.token}
    env = {
        "ANTHROPIC_BASE_URL": provider.base_url,
        "ANTHROPIC_AUTH_TOKEN": provider.token,
        "ANTHROPIC_API_KEY": "",
    }
    host = urlsplit(provider.base_url).hostname or ""
    if host in {"localhost", "127.0.0.1", "::1"}:
        env.update({
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
    return env


def models_url(base_url: str) -> str:
    return base_url if base_url.rstrip("/").endswith("/models") else base_url.rstrip("/") + ("/models" if base_url.rstrip("/").endswith("/v1") else "/v1/models")


def discover_models(provider: CustomProvider, *, timeout: float = 8.0) -> tuple[str, ...]:
    headers = {"Authorization": f"Bearer {provider.token}"} if provider.token else {}
    payload: object = {}
    urls = [models_url(provider.base_url)]
    # Ollama's native tags endpoint predates its OpenAI-compatible surface and
    # remains the reliable discovery fallback for local daemons.
    if provider.base_url.rstrip("/").endswith(":11434"):
        urls.append(provider.base_url.rstrip("/") + "/api/tags")
    for url in urls:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            continue
        if isinstance(payload, dict) and (payload.get("data") or payload.get("models")):
            break
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    if isinstance(payload, dict) and payload.get("models"):
        entries = payload["models"]
    result = []
    for entry in entries:
        model = (
            entry.get("id") or entry.get("name")
            if isinstance(entry, dict)
            else None
        )
        if isinstance(model, str) and model.strip():
            result.append(model.strip())
    return _clean_models(result)
