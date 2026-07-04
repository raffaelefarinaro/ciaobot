"""Provider orchestration and active-operation tracking."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from ciao.config import BridgeConfig
from ciao.models import AgentRequest, StreamEvent
from ciao.providers import ClaudeProvider
from ciao.providers.base import ActiveHandle

ProviderImpl = ClaudeProvider


class ProviderService:
    """Routes requests to the Claude provider and tracks the live handle."""

    def __init__(self, config: BridgeConfig, provider: str = "") -> None:
        self._config = config
        self._provider: ProviderImpl | None = None
        self._active_handle: ActiveHandle | None = None
        if provider:
            self._ensure_provider(provider)

    def _ensure_provider(self, provider: str) -> ProviderImpl:
        """Create the provider instance on first use based on provider name."""
        if self._provider is None:
            if provider == "claude":
                self._provider = ClaudeProvider(self._config.workspace_root, config=self._config)
            else:
                raise ValueError(f"Unknown provider '{provider}'")
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

    async def steer(self, request: AgentRequest) -> bool:
        """Inject a user message into the provider's active turn.

        Returns True if accepted, False if no active client.
        """
        if self._provider is None:
            return False
        return await self._provider.steer(request)

    @property
    def provider(self) -> ProviderImpl | None:
        return self._provider

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
