"""Shared provider helpers."""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.models import (
    AgentRequest,
    BridgeMode,
    ImageAttachment,
    StreamEvent,
)


def _legacy_workspace_context(raw: str | None) -> str:
    value = (raw or "").strip()
    return value if value in {"personal", "work"} else ""


@dataclass(slots=True)
class ActiveHandle:
    """Base handle for an active provider operation."""

    async def stop(self) -> None:
        """Stop the active operation."""


def build_prompt(request: AgentRequest) -> str:
    """Build the shared prompt text for a request."""
    if not request.images:
        return request.prompt

    lines = [request.prompt, "", "[INCOMING IMAGES]"]
    for index, image in enumerate(request.images, start=1):
        summary = f"{index}. {image.original_filename}"
        if image.caption:
            summary = f"{summary} - caption: {image.caption}"
        lines.append(summary)
    return "\n".join(lines).strip()


def build_claude_message_content(request: AgentRequest) -> str | list[dict[str, Any]]:
    """Build Claude SDK message content with native image blocks when present."""
    prompt = build_prompt(request)
    if not request.images:
        return prompt

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in request.images:
        content.append(_claude_image_block(image))
    return content


def build_claude_message_stream(
    request: AgentRequest,
    *,
    session_id: str = "default",
) -> AsyncIterable[dict[str, Any]]:
    """Build a one-message async stream for Claude structured input."""

    async def _stream() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": build_claude_message_content(request),
            },
            "parent_tool_use_id": None,
            "session_id": session_id,
        }

    return _stream()


def build_runtime_context(request: AgentRequest) -> str:
    """Compact per-request runtime context for system_prompt append.

    Keeps the model in sync with today's date, active workspace, and
    GWS profile without requiring Raffa to restate them in every prompt.
    Returns "" when nothing meaningful can be derived, so the caller can
    skip appending entirely.
    """
    env = {**os.environ, **(request.extra_env or {})}
    lines: list[str] = []
    lines.append(f"today={datetime.now(UTC).date().isoformat()}")
    workspace = (
        env.get("CIAO_ACTIVE_WORKSPACE")
        or _legacy_workspace_context(env.get("CIAO_WORKSPACE"))
        or env.get("GWS_PROFILE")
    )
    if workspace:
        lines.append(f"workspace={workspace}")
    gws_profile = env.get("GWS_PROFILE")
    if gws_profile and gws_profile != workspace:
        lines.append(f"gws_profile={gws_profile}")
    project = env.get("CIAO_ACTIVE_PROJECT")
    if project:
        lines.append(f"active_project={project}")
    chat = env.get("CIAO_CHAT_ID")
    if chat:
        lines.append(f"chat_id={chat}")
    if len(lines) == 1:  # only today=
        return lines[0]
    return "\n".join(lines)


def rate_limit_quota_payload(rate_limit_info: Any) -> dict[str, str]:
    """Normalize Claude rate-limit info into persisted quota fields."""
    quota: dict[str, str] = {}
    status = getattr(rate_limit_info, "status", None)
    if status:
        quota["status"] = str(status)
    rate_limit_type = getattr(rate_limit_info, "rate_limit_type", None)
    if rate_limit_type:
        quota["rateLimitType"] = str(rate_limit_type)
    resets_at = getattr(rate_limit_info, "resets_at", None)
    if resets_at is not None:
        quota["resetsAt"] = str(resets_at)
    utilization = getattr(rate_limit_info, "utilization", None)
    if utilization is not None:
        quota["utilization"] = f"{float(utilization):.3f}"
    overage_status = getattr(rate_limit_info, "overage_status", None)
    if overage_status:
        quota["overageStatus"] = str(overage_status)
    overage_resets_at = getattr(rate_limit_info, "overage_resets_at", None)
    if overage_resets_at is not None:
        quota["overageResetsAt"] = str(overage_resets_at)
    overage_disabled_reason = getattr(rate_limit_info, "overage_disabled_reason", None)
    if overage_disabled_reason:
        quota["overageDisabledReason"] = str(overage_disabled_reason)
    return quota


def rate_limit_status_text(quota: dict[str, str]) -> str:
    """Render a short human-facing rate-limit status."""
    parts = ["Rate limit"]
    status = quota.get("status")
    if status:
        parts.append(str(status))
    rate_limit_type = quota.get("rateLimitType")
    if rate_limit_type:
        parts.append(f"({rate_limit_type})")
    utilization = quota.get("utilization")
    if utilization:
        try:
            pct = float(utilization) * 100
            parts.append(f"{pct:.1f}% used")
        except ValueError:
            parts.append(f"utilization {utilization}")
    return ": ".join([parts[0], " ".join(parts[1:])]) if len(parts) > 1 else parts[0]


class BaseProvider(ABC):
    """Abstract provider interface."""

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        self.workspace_root = workspace_root
        self.config = config

    @abstractmethod
    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run one request and stream normalized events."""

    async def disconnect(self) -> None:
        """Discard any provider-side connection state."""


class BaseSDKProvider(BaseProvider):
    """Shared state and helpers for SDK-backed providers."""

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        super().__init__(workspace_root, config=config)
        self._current_model: str = ""
        self._current_mode: BridgeMode = "auto"

    def _settings_changed(self, request: AgentRequest) -> bool:
        return (
            request.model != self._current_model
            or request.mode != self._current_mode
        )

    def _remember_settings(self, request: AgentRequest) -> None:
        self._current_model = request.model
        self._current_mode = request.mode

    def _reset_settings(self) -> None:
        self._current_model = ""
        self._current_mode = "auto"


def _claude_image_block(image: ImageAttachment) -> dict[str, Any]:
    data = base64.b64encode(image.path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.mime_type,
            "data": data,
        },
    }
