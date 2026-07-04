"""Shared request, response, and stream event models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ExecutionMode = Literal["provider_prompt", "provider_cli_arg", "bot_handler"]
BridgeMode = Literal["normal", "plan", "auto", "bypass"]


@dataclass(frozen=True, slots=True)
class ModelOption:
    """One selectable model."""

    value: str
    label: str


@dataclass(slots=True)
class ProviderSessionData:
    """Minimal saved session state."""

    session_id: str = ""
    message_count: int = 0


@dataclass(frozen=True, slots=True)
class ChatContext:
    """Identifies a unique conversation context (DM, forum topic, or web chat)."""

    chat_id: int
    thread_id: int | None = None
    key_override: str | None = None

    @property
    def key(self) -> str:
        if self.key_override is not None:
            return self.key_override
        if self.thread_id is not None:
            return f"{self.chat_id}:{self.thread_id}"
        return str(self.chat_id)

    @classmethod
    def for_web(cls, chat_id: str) -> ChatContext:
        """Create a context for a PWA web chat."""
        return cls(chat_id=0, key_override=chat_id)


ContextMode = str


@dataclass(slots=True)
class ContextState:
    """Per-context conversation state."""

    active_model: str = "opus"
    last_effective_model: str = ""
    mode: BridgeMode = "auto"
    session: ProviderSessionData = field(default_factory=ProviderSessionData)
    context_mode: ContextMode = "auto"


@dataclass(slots=True)
class BotState:
    """Persisted bot-wide global state."""

    workspace_root: str = "."
    media_root: str = ".runtime/telegram_media"
    usage: dict[str, str] = field(default_factory=dict)
    quota: dict[str, str] = field(default_factory=dict)
    quota_buckets: dict[str, dict[str, str]] = field(default_factory=dict)
    cost: float = 0.0


@dataclass(slots=True)
class ImageAttachment:
    """Saved image metadata forwarded to a provider."""

    path: Path
    mime_type: str
    original_filename: str
    caption: str | None = None


# Native thinking/reasoning levels per provider, surfaced as-is in the PWA
# model picker. Empty string = provider default: no flag/option is sent.
# Maps to ``ClaudeAgentOptions.effort``.
THINKING_LEVELS: dict[str, tuple[str, ...]] = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
}


@dataclass(slots=True)
class AgentRequest:
    """Provider request payload."""

    prompt: str
    model: str
    mode: BridgeMode
    # Routing key for ProviderService. Public builds currently accept
    # "claude"; backend choice is handled by model/model_bucket routing.
    provider: str = "claude"
    resume_session: str | None = None
    images: list[ImageAttachment] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    # Tools the spawned CLI must refuse to call. Forwarded to the SDK's
    # ``ClaudeAgentOptions.disallowed_tools``. Lets per-workspace policy
    # (e.g. block claude.ai connectors for personal chats) reach the
    # subprocess without leaking through ``extra_env``.
    disallowed_tools: list[str] = field(default_factory=list)
    # Provider-native thinking/reasoning level (see THINKING_LEVELS).
    # Empty = provider default, nothing is forwarded.
    thinking_level: str = ""


@dataclass(slots=True)
class StreamEvent:
    """Base normalized stream event."""

    type: str


@dataclass(slots=True)
class AssistantTextDelta(StreamEvent):
    """Assistant text delta.

    ``parent_tool_use_id`` is set when the SDK emits this delta from inside
    a Task subagent (the parent's ``tool_use_id`` for the Task dispatch).
    Lets the UI attribute streamed text to the right agent instead of
    blending subagent prose into the parent's reply.
    """

    text: str = ""
    parent_tool_use_id: str | None = None


@dataclass(slots=True)
class ToolUseEvent(StreamEvent):
    """Tool use event.

    ``tool_use_id`` is the SDK's stable id for the call (used to map a
    later stream event back to its dispatch). ``parent_tool_use_id`` is
    set when this tool fires inside a Task subagent — its value matches
    the parent-level Task dispatch's ``tool_use_id``, so the client can
    look up the subagent's description and label the activity line.
    """

    tool_name: str = ""
    tool_input: str = ""  # summarized input (e.g. file path, command)
    tool_use_id: str | None = None
    parent_tool_use_id: str | None = None


@dataclass(slots=True)
class ThinkingEvent(StreamEvent):
    """Reasoning event."""

    text: str = ""
    parent_tool_use_id: str | None = None


@dataclass(slots=True)
class SystemStatusEvent(StreamEvent):
    """System status event."""

    status: str | None = None


@dataclass(slots=True)
class ResultEvent(StreamEvent):
    """Terminal stream event."""

    result: str = ""
    session_id: str | None = None
    is_error: bool = False
    effective_model: str = ""
    usage: dict[str, str] = field(default_factory=dict)
    quota: dict[str, str] = field(default_factory=dict)
    cost_usd: float | None = None


@dataclass(slots=True)
class PermissionRequestEvent(StreamEvent):
    """Emitted when the provider subprocess needs permission approval.

    ``request_id`` correlates this prompt to the client's eventual
    ``permission_response`` reply. Set to the SDK's ``tool_use_id`` when
    available (stable across retries) and falls back to a freshly-minted
    UUID for synthetic prompts.
    """

    message: str = ""
    tool_name: str = ""
    tool_input: str = ""
    request_id: str = ""
