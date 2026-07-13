"""Provider orchestration and active-operation tracking."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from ciao.config import BridgeConfig
from ciao.models import AgentRequest, StreamEvent
from ciao.providers import ClaudeProvider, CodexProvider
from ciao.providers.base import ActiveHandle, BaseProvider, ProviderCapabilities

ProviderImpl = BaseProvider

_PROVIDER_FACTORIES = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
}


def supported_providers() -> tuple[str, ...]:
    """Provider names accepted by chats, schedules, and the CLI."""
    return tuple(_PROVIDER_FACTORIES)


class ProviderService:
    """Routes requests to a provider and tracks its live operation."""

    def __init__(self, config: BridgeConfig, provider: str = "") -> None:
        self._config = config
        self._provider: ProviderImpl | None = None
        self._active_handle: ActiveHandle | None = None
        if provider:
            self._ensure_provider(provider)

    def _ensure_provider(self, provider: str) -> ProviderImpl:
        """Create the provider instance on first use based on provider name."""
        if self._provider is None:
            factory = _PROVIDER_FACTORIES.get(provider)
            if factory is None:
                raise ValueError(f"Unknown provider '{provider}'")
            self._provider = factory(self._config.workspace_root, config=self._config)
        return self._provider

    def has_active_process(self) -> bool:
        return self._active_handle is not None

    def _register_handle(self, handle: ActiveHandle | None) -> None:
        self._active_handle = handle

    async def stop_active(self) -> bool:
        if self._active_handle is None:
            return False
        await self._active_handle.stop()
        self._active_handle = None
        return True

    async def execute_streaming(
        self, request: AgentRequest
    ) -> AsyncGenerator[StreamEvent, None]:
        provider = self._ensure_provider(request.provider)
        async for event in provider.run_streaming(request, self._register_handle):
            yield event

    @property
    def can_drain(self) -> bool:
        """True when the provider has a live client to drain between turns."""
        return bool(self._provider is not None and getattr(self._provider, "can_drain", False))

    async def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        """Yield between-turns provider events (see ClaudeProvider.drain_events)."""
        if self._provider is None:
            return
        drain = getattr(self._provider, "drain_events", None)
        if not callable(drain):
            return
        async for event in drain():
            yield event

    async def steer(self, request: AgentRequest) -> bool:
        """Inject a user message into the provider's active turn.

        Returns True if accepted, False if no active client.
        """
        if self._provider is None:
            return False
        steer = getattr(self._provider, "steer", None)
        if not callable(steer):
            return False
        return await steer(request)

    @property
    def provider(self) -> ProviderImpl | None:
        return self._provider

    @property
    def capabilities(self) -> ProviderCapabilities:
        if self._provider is None:
            return ProviderCapabilities()
        return self._provider.capabilities

    @property
    def current_session_id(self) -> str | None:
        """Session id as currently known to the underlying provider."""
        if self._provider is None:
            return None
        return getattr(self._provider, "current_session_id", None)

    async def disconnect(self) -> None:
        """Disconnect the provider (e.g. SDK client)."""
        if self._provider is not None:
            await self._provider.disconnect()
            self._provider = None
