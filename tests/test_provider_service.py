"""Provider service dispatch and the typed optional provider contract.

Covers the BaseProvider defaults for ``can_drain``, ``cli_connected`` and
``drain_events`` (#3171) and the ProviderService forwarding of those members:
no-instance guards, third-adapter dispatch, event forwarding (identity,
order, laziness, exceptions, cancellation), built-in inheritance and
disconnect behavior. Characterization: all of that behavior is preserved
unchanged; only the service's duck-typed probes are removed.

Counters observe the actual boundaries, independently: the service's
``_ensure_provider``, ``execute_streaming`` and ``disconnect``; registry
lookups (``provider_registry.require``); factory resolution
(``ProviderDescriptor.factory``) *and* the returned class constructor
(distinct seams — the constructors of the built-in provider classes are
patched before any accessor runs, so a direct construction is counted
without any factory resolution); handle ``stop()``; handle registration
(``_register_handle``), which ``run_streaming`` receives; SDK client
construction (``ClaudeSDKClient``, the object ``_ensure_connected``
builds); server invocation (``httpx.AsyncClient``, the server connection
both OpenCode server paths converge on); and subprocess invocations
(``asyncio.create_subprocess_exec``, the exec boundary ``_spawn_server``
reaches). Liveness of every counting harness is proven by the
``*_fires_*`` tests; no counter is asserted only through an inferral from
final state.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from claude_agent_sdk import ClaudeSDKClient

from ciao import provider_registry
from ciao.config import CiaoConfig
from ciao.models import AgentRequest, ResultEvent, StreamEvent, TokenUsageEvent
from ciao.provider_registry import ProviderDescriptor
from ciao.provider_service import ProviderService
from ciao.providers.base import ActiveHandle, BaseProvider, ProviderCapabilities
from ciao.providers.claude import ClaudeProvider
from ciao.providers.opencode import OpencodeProvider


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / "state.json",
        media_root=tmp_path / "media",
    )


# The actual constructor boundaries the registry can resolve today.
_PROVIDER_CLASSES: tuple[type[BaseProvider], ...] = (ClaudeProvider, OpencodeProvider)


@dataclass
class _SeamCounts:
    """Independent counts, one per actual provider-seam boundary."""

    ensure: list[str] = field(default_factory=list)
    streams: list[AgentRequest] = field(default_factory=list)
    disconnects: list[bool] = field(default_factory=list)
    registrations: list[ActiveHandle | None] = field(default_factory=list)
    registry_lookups: list[str] = field(default_factory=list)
    factory_resolutions: list[str] = field(default_factory=list)
    constructions: list[str] = field(default_factory=list)
    sdk_constructions: list[str] = field(default_factory=list)
    server_invocations: list[str] = field(default_factory=list)
    subprocess_invocations: list[str] = field(default_factory=list)


def _patch_constructor(
    monkeypatch: pytest.MonkeyPatch, cls: type[BaseProvider], counts: _SeamCounts
) -> None:
    """Count one actual constructor, independent of factory resolution.

    The real ``__init__`` still runs, so the instance is fully built; only
    the boundary itself is observed.
    """
    real_init = cls.__init__

    def counting_init(
        instance: BaseProvider,
        workspace_root: Path,
        *,
        config: object | None = None,
    ) -> None:
        counts.constructions.append(cls.__name__)
        real_init(instance, workspace_root, config=config)

    monkeypatch.setattr(cls, "__init__", counting_init)


def _count_provider_seams(
    monkeypatch: pytest.MonkeyPatch, service: ProviderService
) -> _SeamCounts:
    """Wrap every real seam with a delegating counter (never a raising spy).

    Registry lookup and factory resolution are module/class-level seams; the
    built-in provider constructors are patched up front — before any
    accessor runs — so direct constructions are counted too, and resolution
    without construction stays observable.
    """
    counts = _SeamCounts()
    real_ensure = service._ensure_provider

    def counting_ensure(provider: str) -> BaseProvider:
        counts.ensure.append(provider)
        return real_ensure(provider)

    monkeypatch.setattr(service, "_ensure_provider", counting_ensure)

    real_stream = service.execute_streaming

    def counting_stream(request: AgentRequest) -> AsyncGenerator[StreamEvent, None]:
        counts.streams.append(request)
        return real_stream(request)

    monkeypatch.setattr(service, "execute_streaming", counting_stream)

    real_disconnect = service.disconnect

    async def counting_disconnect() -> None:
        counts.disconnects.append(True)
        await real_disconnect()

    monkeypatch.setattr(service, "disconnect", counting_disconnect)

    real_register = service._register_handle

    def counting_register(handle: ActiveHandle | None) -> None:
        counts.registrations.append(handle)
        real_register(handle)

    monkeypatch.setattr(service, "_register_handle", counting_register)

    real_require = provider_registry.require

    def counting_require(provider: str) -> ProviderDescriptor:
        counts.registry_lookups.append(provider)
        return real_require(provider)

    monkeypatch.setattr(provider_registry, "require", counting_require)

    real_factory = ProviderDescriptor.factory

    def counting_factory(descriptor: ProviderDescriptor) -> type[BaseProvider]:
        counts.factory_resolutions.append(descriptor.id)
        return real_factory(descriptor)

    monkeypatch.setattr(ProviderDescriptor, "factory", counting_factory)

    for cls in _PROVIDER_CLASSES:
        _patch_constructor(monkeypatch, cls, counts)

    # SDK client construction: the object ClaudeProvider._ensure_connected
    # builds (claude.py builds it with ``options=`` as a keyword).
    real_sdk_init = ClaudeSDKClient.__init__

    def counting_sdk_init(
        instance: ClaudeSDKClient, *args: Any, **kwargs: Any
    ) -> None:
        counts.sdk_constructions.append(type(instance).__name__)
        real_sdk_init(instance, *args, **kwargs)

    monkeypatch.setattr(ClaudeSDKClient, "__init__", counting_sdk_init)

    # Server invocation: the httpx.AsyncClient server connection both OpenCode
    # server paths converge on (per-chat ``_ensure_server_locked`` and the
    # ephemeral read-path server). The process-level start itself is covered
    # by the subprocess boundary below.
    real_client_init = httpx.AsyncClient.__init__

    def counting_client_init(
        instance: httpx.AsyncClient, *args: Any, **kwargs: Any
    ) -> None:
        counts.server_invocations.append(type(instance).__name__)
        real_client_init(instance, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", counting_client_init)

    # Subprocess invocations: the exec boundary every server spawn reaches
    # through ``_spawn_server``.
    real_exec = asyncio.create_subprocess_exec

    def counting_exec(
        *args: Any, **kwargs: Any
    ) -> Coroutine[Any, Any, asyncio.subprocess.Process]:
        counts.subprocess_invocations.append(str(args[0]) if args else "")
        return real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)
    return counts


class _MinimalProvider(BaseProvider):
    """Implements ``run_streaming`` and a counting ``disconnect``; inherits
    every optional drain/connection default."""

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        super().__init__(workspace_root, config=config)
        self.stream_calls = 0  # run_streaming invocation, at the method boundary
        self.disconnected = 0

    def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        self.stream_calls += 1
        return self._turn(register_handle)

    async def _turn(
        self, register_handle: Callable[[ActiveHandle | None], None]
    ) -> AsyncGenerator[StreamEvent, None]:
        register_handle(None)
        yield ResultEvent(type="result", result="ok", session_id="s")

    async def disconnect(self) -> None:
        self.disconnected += 1


async def test_minimal_adapter_inherits_the_default_flags(tmp_path: Path) -> None:
    """A minimal adapter satisfies the optional contract without overrides."""
    adapter = _MinimalProvider(tmp_path)

    assert adapter.can_drain is False
    assert adapter.cli_connected is True


async def test_minimal_adapter_drain_is_an_empty_async_generator(
    tmp_path: Path,
) -> None:
    """The inherited drain is a yield-bearing async generator, not a coroutine."""
    adapter = _MinimalProvider(tmp_path)

    gen = adapter.drain_events()
    assert inspect.isasyncgen(gen)
    assert inspect.iscoroutine(gen) is False
    assert [event async for event in gen] == []

    # A second call produces a fresh generator: nothing was cached or consumed.
    assert [event async for event in adapter.drain_events()] == []


async def test_minimal_adapter_defaults_do_no_extra_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the defaults builds no SDK client, server connection or subprocess,
    and starts no turn, disconnect, registration or stop."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _MinimalProvider(tmp_path)
    service._provider = adapter
    handle = _CountingHandle()
    service._active_handle = handle

    assert adapter.can_drain is False
    assert adapter.cli_connected is True
    assert [event async for event in adapter.drain_events()] == []

    # After exhausting the default drain: the flags still hold, the adapter
    # seams never ran, and no provider was looked up, resolved or built.
    assert adapter.can_drain is False
    assert adapter.cli_connected is True
    assert counts.ensure == []
    assert counts.streams == []
    assert counts.disconnects == []
    assert counts.registrations == []
    assert counts.registry_lookups == []
    assert counts.factory_resolutions == []
    assert counts.constructions == []
    assert counts.sdk_constructions == []
    assert counts.server_invocations == []
    assert counts.subprocess_invocations == []
    assert adapter.stream_calls == 0
    assert adapter.disconnected == 0
    assert handle.stops == 0
    assert service.active_handle() is handle
    assert service.provider is adapter  # unchanged final state, not the inference


async def test_minimal_adapter_disconnect_counter_counts_when_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disconnect counters are live: the service and adapter both count."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _MinimalProvider(tmp_path)
    service._provider = adapter

    await service.disconnect()

    assert counts.disconnects == [True]
    assert adapter.disconnected == 1
    assert service.provider is None


async def test_service_capabilities_of_a_minimal_adapter_are_unchanged(
    tmp_path: Path,
) -> None:
    """Attaching a minimal adapter leaves the service capabilities at the default."""
    service = ProviderService(_config(tmp_path))
    service._provider = _MinimalProvider(tmp_path)

    assert service.capabilities == ProviderCapabilities()


class _CountingHandle(ActiveHandle):
    def __init__(self) -> None:
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1


async def test_stop_counter_fires_through_stop_active(tmp_path: Path) -> None:
    """The handle stop counter observes the boundary stop_active() calls."""
    service = ProviderService(_config(tmp_path))
    handle = _CountingHandle()
    service._active_handle = handle

    assert await service.stop_active() is True

    assert handle.stops == 1
    assert service.active_handle() is None  # stop_active clears the handle


async def test_seam_counters_fire_on_a_real_provider_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry lookup, factory resolution and construction are distinct seams."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)

    provider = service.ensure_provider("claude")

    assert counts.ensure == ["claude"]
    assert counts.registry_lookups == ["claude"]
    assert counts.factory_resolutions == ["claude"]
    assert counts.constructions == ["ClaudeProvider"]
    assert isinstance(provider, ClaudeProvider)
    assert service.provider is provider


def test_constructor_counter_fires_on_direct_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The up-front constructor patch observes builds with no factory involved."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)

    provider = ClaudeProvider(tmp_path)  # direct construct, no registry or factory

    assert counts.constructions == ["ClaudeProvider"]
    assert counts.factory_resolutions == []  # resolution stayed at zero
    assert counts.registry_lookups == []
    assert isinstance(provider, ClaudeProvider)


async def test_seam_counters_fire_on_a_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream boundary, run_streaming boundary and registration all count."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _MinimalProvider(tmp_path)
    service._provider = adapter
    request = AgentRequest(prompt="hi", model="", mode="normal", provider="claude")

    events = [event async for event in service.execute_streaming(request)]

    assert len(events) == 1
    assert counts.ensure == ["claude"]  # execute_streaming resolves per turn
    assert counts.streams == [request]
    assert adapter.stream_calls == 1
    assert counts.registrations == [None]  # the adapter registered None itself
    assert service.active_handle() is None
    assert counts.disconnects == []  # no memory changed, so no reconnect


async def test_sdk_server_subprocess_counters_fire_on_real_seams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK, server and subprocess counters observe their real boundaries.

    Each firing is side-effect-free: the SDK client constructor stores options
    and starts no transport (that happens at ``connect()``), the httpx client
    opens no socket until a request (and is closed again here), and the
    subprocess is a trivial Python command (``sys.executable -c pass``).
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)

    sdk_client = ClaudeSDKClient()
    server_client = httpx.AsyncClient()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", stdout=asyncio.subprocess.DEVNULL
    )
    await process.wait()
    await server_client.aclose()

    assert counts.sdk_constructions == ["ClaudeSDKClient"]
    assert counts.server_invocations == ["AsyncClient"]
    assert counts.subprocess_invocations == [sys.executable]
    assert isinstance(sdk_client, ClaudeSDKClient)
    assert isinstance(server_client, httpx.AsyncClient)


class _ToggleProvider(BaseProvider):
    """Overridden flags with read counters, for dispatch observation."""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.can_drain_value = False
        self.cli_connected_value = True
        self.can_drain_reads = 0
        self.cli_connected_reads = 0
        self.drain_calls = 0  # method boundary
        self.drain_bodies = 0  # async generator body start
        self.stream_calls = 0  # run_streaming invocation, at the method boundary
        self.disconnected = 0
        self.events: list[StreamEvent] = []

    @property
    def can_drain(self) -> bool:
        self.can_drain_reads += 1
        return self.can_drain_value

    @property
    def cli_connected(self) -> bool:
        self.cli_connected_reads += 1
        return self.cli_connected_value

    def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        self.drain_calls += 1
        return self._events()

    async def _events(self) -> AsyncGenerator[StreamEvent, None]:
        self.drain_bodies += 1
        for event in self.events:
            yield event

    def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        self.stream_calls += 1
        return self._turn()

    async def _turn(self) -> AsyncGenerator[StreamEvent, None]:
        yield ResultEvent(type="result", result="ok", session_id="s")

    async def disconnect(self) -> None:
        self.disconnected += 1


class _DrainOnlyProvider(BaseProvider):
    """Overrides ``can_drain`` only; inherits the owner-health and drain defaults."""

    @property
    def can_drain(self) -> bool:
        return True

    def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        del request, register_handle
        return self._turn()

    async def _turn(self) -> AsyncGenerator[StreamEvent, None]:
        yield ResultEvent(type="result", result="ok", session_id="s")


# ── third-adapter dispatch ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("can_drain", "cli_connected"),
    [(False, False), (False, True), (True, False), (True, True)],
)
async def test_service_dispatches_the_typed_flags_independently(
    tmp_path: Path, can_drain: bool, cli_connected: bool
) -> None:
    """A real service holds a real third adapter and reads its flags as they are."""
    service = ProviderService(_config(tmp_path))
    adapter = _ToggleProvider(tmp_path)
    adapter.can_drain_value = can_drain
    adapter.cli_connected_value = cli_connected
    handle = _CountingHandle()
    service._active_handle = handle
    service._provider = adapter

    assert service.can_drain is can_drain
    assert service.cli_connected is cli_connected

    assert adapter.can_drain_reads == 1  # one property observation per access
    assert adapter.cli_connected_reads == 1
    assert adapter.stream_calls == 0  # no start/disconnect/handle mutation
    assert adapter.disconnected == 0
    assert service.active_handle() is handle


async def test_service_mixes_inherited_defaults_with_one_override(
    tmp_path: Path,
) -> None:
    """An adapter overriding one member still inherits the other defaults."""
    service = ProviderService(_config(tmp_path))
    service._provider = _DrainOnlyProvider(tmp_path)

    assert service.can_drain is True  # overridden by the adapter
    assert service.cli_connected is True  # inherited owner-health default
    assert [event async for event in service.drain_events()] == []  # inherited empty drain


# ── forwarding ───────────────────────────────────────────────────────────

_USAGE = TokenUsageEvent(type="token_usage", input_tokens=3, output_tokens=5)
_RESULT = ResultEvent(
    type="result",
    result="done",
    session_id="ses_wake",
    effective_model="model-x",
    usage={"input_tokens": "9"},
    origin="task-notification",
)


class _ForwardProvider(BaseProvider):
    """A custom reader yielding two normalized events; counts its work.

    ``drain_calls`` counts the drain method boundary (the invocation itself);
    ``drain_bodies`` counts the async generator body start. ``stream_calls``
    counts the run_streaming method boundary.
    """

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.can_drain_value = False
        self.drain_calls = 0
        self.drain_bodies = 0
        self.stream_calls = 0
        self.disconnected = 0

    @property
    def can_drain(self) -> bool:
        return self.can_drain_value

    def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        del request, register_handle
        self.stream_calls += 1
        return self._turn()

    async def _turn(self) -> AsyncGenerator[StreamEvent, None]:
        yield ResultEvent(type="result", result="ok", session_id="s")

    def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        self.drain_calls += 1
        return self._events()

    async def _events(self) -> AsyncGenerator[StreamEvent, None]:
        self.drain_bodies += 1
        yield _USAGE
        yield _RESULT

    async def disconnect(self) -> None:
        self.disconnected += 1


async def _collect(
    gen: AsyncGenerator[StreamEvent, None],
) -> tuple[list[StreamEvent], Exception | None]:
    events: list[StreamEvent] = []
    try:
        async for event in gen:
            events.append(event)
    except Exception as exc:
        return events, exc
    return events, None


async def test_service_forwards_drain_events_unchanged(tmp_path: Path) -> None:
    """Events keep their identity, order and fields; the iterator runs lazily once."""
    service = ProviderService(_config(tmp_path))
    adapter = _ForwardProvider(tmp_path)
    service._provider = adapter

    gen = service.drain_events()
    assert adapter.drain_calls == 0  # lazy: no invocation before consumption
    assert adapter.drain_bodies == 0
    collected = [event async for event in gen]

    assert len(collected) == 2
    assert collected[0] is _USAGE and collected[1] is _RESULT  # identity and order
    assert collected == [_USAGE, _RESULT]
    assert adapter.drain_calls == 1  # exactly one invocation at the boundary
    assert adapter.drain_bodies == 1  # and exactly one generator body start
    assert adapter.stream_calls == 0  # zero foreground streaming
    assert adapter.disconnected == 0
    assert isinstance(collected[1], ResultEvent)
    assert collected[1].origin == "task-notification"
    assert collected[1].session_id == "ses_wake"
    assert collected[1].usage == {"input_tokens": "9"}

    await service.disconnect()  # the counting override is live
    assert adapter.disconnected == 1


async def test_service_drain_does_not_check_eligibility(tmp_path: Path) -> None:
    """Eligibility is the host's decision: an explicit caller still collects events."""
    service = ProviderService(_config(tmp_path))
    adapter = _ForwardProvider(tmp_path)
    adapter.can_drain_value = False
    service._provider = adapter

    collected = [event async for event in service.drain_events()]

    assert collected == [_USAGE, _RESULT]
    assert adapter.drain_calls == 1
    assert adapter.drain_bodies == 1


# ── failure and cancellation ─────────────────────────────────────────────

_SENTINEL = RuntimeError("sentinel drain failure")


class _FailureProvider(_ForwardProvider):
    async def _events(self) -> AsyncGenerator[StreamEvent, None]:
        self.drain_bodies += 1
        yield _USAGE
        raise _SENTINEL


async def test_service_propagates_a_drain_exception_exactly(tmp_path: Path) -> None:
    """The event yielded before the failure is seen; the exact exception propagates."""
    service = ProviderService(_config(tmp_path))
    adapter = _FailureProvider(tmp_path)
    service._provider = adapter

    collected, error = await _collect(service.drain_events())

    assert collected == [_USAGE]
    assert error is _SENTINEL
    assert adapter.drain_calls == 1
    assert adapter.drain_bodies == 1
    assert adapter.stream_calls == 0
    assert adapter.disconnected == 0


class _BlockingProvider(BaseProvider):
    """A reader that yields one event, then blocks until cancelled."""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.drain_calls = 0
        self.drain_bodies = 0
        self.stream_calls = 0
        self.disconnected = 0
        self.entered_wait = asyncio.Event()
        self.left = asyncio.Event()

    def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        del request, register_handle
        self.stream_calls += 1
        return self._turn()

    async def _turn(self) -> AsyncGenerator[StreamEvent, None]:
        yield ResultEvent(type="result", result="ok", session_id="s")

    def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        self.drain_calls += 1
        return self._blocked()

    async def _blocked(self) -> AsyncGenerator[StreamEvent, None]:
        self.drain_bodies += 1
        try:
            yield _USAGE
            self.entered_wait.set()
            await asyncio.Event().wait()
        finally:
            self.left.set()

    async def disconnect(self) -> None:
        self.disconnected += 1


async def test_service_propagates_cancellation_and_awaits_the_unwind(
    tmp_path: Path,
) -> None:
    """Cancelling the consumer unwinds the adapter's finally before the task ends."""
    service = ProviderService(_config(tmp_path))
    adapter = _BlockingProvider(tmp_path)
    service._provider = adapter
    handle = _CountingHandle()
    service._active_handle = handle

    collected: list[StreamEvent] = []

    async def consume() -> None:
        async for event in service.drain_events():
            collected.append(event)

    task = asyncio.ensure_future(consume())
    await adapter.entered_wait.wait()  # no sleeps: the adapter is blocked at its await
    assert collected == [_USAGE]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()

    assert adapter.left.is_set()  # the adapter's finally ran during the full unwind
    assert adapter.drain_calls == 1  # no replacement reader started
    assert adapter.drain_bodies == 1
    assert adapter.stream_calls == 0  # no foreground turn before completion
    assert adapter.disconnected == 0  # the cancellation did not disconnect
    assert service.active_handle() is handle
    assert handle.stops == 0

    await service.disconnect()  # the counting override is live
    assert adapter.disconnected == 1


# ── built-in inheritance ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("client", "connected", "expected"),
    [
        (None, False, (False, False)),  # unconnected
        (object(), True, (True, True)),  # connected client
        (object(), False, (False, False)),  # flag already down
        (None, True, (False, False)),  # flag up but no client
    ],
)
async def test_service_reports_claude_state_from_client_and_flag(
    tmp_path: Path, client: object, connected: bool, expected: tuple[bool, bool]
) -> None:
    """Claude drives both service flags from its client state; no SDK is called."""
    provider = ClaudeProvider(tmp_path)
    provider._client = cast(ClaudeSDKClient, client)
    provider._connected = connected
    service = ProviderService(_config(tmp_path))
    service._provider = provider

    assert service.can_drain is expected[0]
    assert service.cli_connected is expected[1]


def _attach_live(
    provider: OpencodeProvider, session: str, *, dead: bool = False
) -> None:
    """Make the provider look attached to a live server, in-memory only."""
    provider._client = cast(httpx.AsyncClient, object())
    provider._process = cast(
        asyncio.subprocess.Process, SimpleNamespace(returncode=1 if dead else None)
    )
    provider._session_id = session


async def test_opencode_inherits_owner_health_and_uses_dynamic_eligibility(
    tmp_path: Path,
) -> None:
    """OpenCode tracks no CLI owner (inherited True) while eligibility follows work."""
    service = ProviderService(_config(tmp_path))
    provider = OpencodeProvider(tmp_path)
    service._provider = provider

    # No server yet: ineligible, but no owner is tracked, so health is inherited True.
    assert service.can_drain is False
    assert service.cli_connected is True

    # Live server with a session but no outstanding work: still ineligible.
    _attach_live(provider, "ses_a")
    assert provider.has_live_server is True
    assert service.can_drain is False
    assert service.cli_connected is True

    # Background shell work outstanding for the session: eligible, still healthy.
    provider._background_shells = {"sh"}
    assert service.can_drain is True
    assert service.cli_connected is True

    # Health never tracks server state: a dead process stays healthy, ineligible.
    _attach_live(provider, "ses_a", dead=True)
    assert service.can_drain is False
    assert service.cli_connected is True

    # Missing session with work outstanding: ineligible again.
    provider._background_children = {"ses_child"}
    _attach_live(provider, "")
    assert service.can_drain is False
    assert service.cli_connected is True

    # Child work alone (shells cleared) makes the live session eligible.
    provider._background_shells.clear()
    _attach_live(provider, "ses_b")
    assert service.can_drain is True
    assert service.cli_connected is True


# ── service disconnect ───────────────────────────────────────────────────


async def test_service_disconnect_clears_only_the_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """disconnect() clears the provider instance; handle clearing stays with stops."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _ToggleProvider(tmp_path)
    service._provider = adapter
    handle = _CountingHandle()
    service._active_handle = handle

    await service.disconnect()

    assert counts.disconnects == [True]  # the service boundary ran
    assert adapter.disconnected == 1  # adapter disconnect called exactly once
    assert service.provider is None
    assert service.can_drain is False  # false/false/empty afterwards
    assert service.cli_connected is False
    assert [event async for event in service.drain_events()] == []
    # No reconstruction at any seam, counted independently after the iteration.
    assert counts.ensure == []
    assert counts.streams == []
    assert counts.disconnects == [True]  # the one disconnect above
    assert counts.registrations == []
    assert counts.registry_lookups == []
    assert counts.factory_resolutions == []
    assert counts.constructions == []
    assert service.active_handle() is handle  # seeded handle unchanged
    assert handle.stops == 0  # and not stopped


# ── no instance ──────────────────────────────────────────────────────────


async def test_service_without_instance_keeps_no_instance_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider id: false / false / empty, and every seam stays at zero."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    handle = _CountingHandle()
    service._active_handle = handle

    assert service.can_drain is False
    assert service.cli_connected is False
    assert [event async for event in service.drain_events()] == []

    # Every seam counted at its own boundary, asserted after the iteration —
    # absence is not inferred from the final state.
    assert counts.ensure == []
    assert counts.streams == []
    assert counts.disconnects == []
    assert counts.registrations == []
    assert counts.registry_lookups == []
    assert counts.factory_resolutions == []
    assert counts.constructions == []
    assert handle.stops == 0
    assert service.active_handle() is handle  # a seeded handle is unchanged
    assert service.provider is None
