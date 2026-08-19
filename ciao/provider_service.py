"""Provider orchestration and active-operation tracking."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import logging
from pathlib import Path
from typing import cast

from ciao import provider_registry
from ciao.config import BridgeConfig
from ciao.models import AgentRequest, StreamEvent
from ciao.memory_tool import prune_expired_entries
from ciao.providers.base import ActiveHandle, BaseProvider, ProviderCapabilities

ProviderImpl = BaseProvider
logger = logging.getLogger(__name__)


def supported_providers() -> tuple[str, ...]:
    """Provider names accepted by chats, schedules, and the CLI."""
    return provider_registry.provider_ids()


def capabilities_for(provider: str) -> ProviderCapabilities:
    """Static capabilities of a provider, without instantiating it.

    Lets routes and the PWA describe a provider the current chat is not
    running. Unknown ids get the all-``False`` default rather than raising, so
    a stale chat record degrades to "supports nothing" instead of a 500.
    """
    descriptor = provider_registry.get(provider)
    if descriptor is None:
        return ProviderCapabilities()
    return descriptor.factory().capabilities


class ProviderService:
    """Routes requests to a provider and tracks its live operation."""

    def __init__(
        self,
        config: BridgeConfig,
        provider: str = "",
        *,
        agent_root: Path | None = None,
    ) -> None:
        self._config = config
        self._agent_root = agent_root
        self._provider: ProviderImpl | None = None
        self._active_handle: ActiveHandle | None = None
        if provider:
            self._ensure_provider(provider)

    def _ensure_provider(self, provider: str) -> ProviderImpl:
        """Create the provider instance on first use based on provider name."""
        if self._provider is None:
            factory = provider_registry.require(provider).factory()
            # The agent root is data threaded from the caller, not re-derived
            # here. A caller that supplies none still lands on ``workspace_root``,
            # which matches ``CiaoConfig.agent_root`` today, so this is a no-op
            # seam until the re-rooting release flips ``agent_root``.
            root = self._agent_root if self._agent_root is not None else self._config.workspace_root
            self._provider = factory(root, config=self._config)
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
        # Native provider guide loaders read CLAUDE.md/AGENTS.md at session
        # start. Remove only entries whose explicit expiry has passed before
        # that happens; memory remains in the guide and is never duplicated
        # into a provider-specific prompt block.
        try:
            result = prune_expired_entries(
                Path(getattr(self._config, "workspace_root", ".")) / "CLAUDE.md"
            )
            memory_changed = bool(
                result.get("removed", {}).get("memory", 0)
                or result.get("removed", {}).get("profile", 0)
            )
            if memory_changed:
                logger.info("Pruned expired workspace memory entries: %s", result.get("removed"))
                # Native guide content is loaded when the provider process or
                # session starts. Reconnect before resuming so the current
                # turn cannot keep a just-expired fact in its native snapshot.
                if self._provider is not None:
                    await self.disconnect()
        except Exception:  # noqa: BLE001 - memory hygiene must not block a turn
            logger.warning("Could not prune expired workspace memory", exc_info=True)
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
        return cast(bool, await steer(request))

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
