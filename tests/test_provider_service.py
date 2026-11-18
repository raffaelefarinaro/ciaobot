"""Provider service dispatch and the typed optional provider contract.

Covers the BaseProvider defaults for ``can_drain``, ``cli_connected`` and
``drain_events`` (#3171) and the ProviderService forwarding of those members:
no-instance guards, third-adapter dispatch, event forwarding (identity,
order, laziness, exceptions, cancellation), built-in inheritance and
disconnect behavior. Characterization: all of that behavior is preserved
unchanged; only the service's duck-typed probes are removed.

Also covers the ``current_session_id`` observation (#3175): the concrete
read-only default and the service getter's direct typed forward — no-instance
guard, adapter-verbatim dynamic values, built-in characterization, getter
failure propagation (a getter-raised ``AttributeError`` included, which the
former ``getattr(..., None)`` probe masked) and the inherited read-only
descriptor's assignment break.

The model-catalog contract (#3178) is class-level dispatch only: the concrete
optional ``model_catalog`` classmethod on ``BaseProvider`` and its fresh,
no-work inherited default (empty for either force value), typed dispatch
through ``type[BaseProvider]`` to a third-adapter override, unwrapped failure
and cancellation, and built-in characterization — Claude inherits the empty
default while OpenCode's real override keeps its cache/force behavior with
only its fetch seam faked. This is not a service or host migration: no
service, route or manager consumer is migrated, and no assertion claims it.

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


async def test_minimal_adapter_session_observation_defaults_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inherited session observation is None, side-effect-free, direct and
    through a real service; the sibling defaults and capabilities are unchanged."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _MinimalProvider(tmp_path)

    assert adapter.current_session_id is None  # direct observation
    assert adapter.response_session_id == ""  # unchanged sibling default
    assert adapter.capabilities == ProviderCapabilities()

    service._provider = adapter
    assert service.current_session_id is None  # real-service observation
    assert service.capabilities == ProviderCapabilities()

    assert service.provider is adapter  # unchanged final state
    # No provider was looked up, resolved, built, started or torn down.
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


class _AssigningProvider(BaseProvider):
    """A real adapter whose constructor assigns ``current_session_id``.

    That assignment succeeded before the base defined the member; with the
    inherited read-only property it is the deliberate compatibility-boundary
    break pinned by ``test_assigning_the_inherited_session_property_raises``.
    """

    assignment_reached: list[bool] = []

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.assignment_reached.append(True)
        # The failure under test is the runtime descriptor error; setattr keeps
        # mypy out of an assignment statement the contract now rejects anyway.
        setattr(self, "current_session_id", "")

    def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        del request, register_handle
        return self._turn()

    async def _turn(self) -> AsyncGenerator[StreamEvent, None]:
        yield ResultEvent(type="result", result="ok", session_id="s")


def test_assigning_the_inherited_session_property_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subclass assignment of the inherited read-only session property fails.

    Before the base property existed this instance assignment stored an
    attribute and construction succeeded, so this is a compatibility-boundary
    regression, not preservation evidence. The counter proves the failure is
    the assignment itself, not earlier adapter work.
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    _AssigningProvider.assignment_reached.clear()

    with pytest.raises(AttributeError):
        _AssigningProvider(tmp_path)

    assert _AssigningProvider.assignment_reached == [True]  # the assignment ran
    assert service.provider is None  # construction never returned
    # No stream, SDK, server or subprocess work: every real boundary stayed at zero.
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
    """Overridden flags with read counters, for dispatch observation.

    The counting ``current_session_id`` getter reads its own backing field —
    the supported read-only override shape both built-ins use.
    """

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.can_drain_value = False
        self.cli_connected_value = True
        self.can_drain_reads = 0
        self.cli_connected_reads = 0
        self.current_session_reads = 0
        self._session_id: str | None = None
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

    @property
    def current_session_id(self) -> str | None:
        self.current_session_reads += 1
        return self._session_id

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


async def test_service_forwards_the_adapters_session_values_dynamically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each read observes the adapter exactly once, returning precisely its value.

    None, two ids and a bare "" are forwarded without caching or truthiness
    conversion; no provider work runs after setup.
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _ToggleProvider(tmp_path)
    service._provider = adapter
    handle = _CountingHandle()
    service._active_handle = handle

    for value in (None, "sid-A", "sid-B", "", None):
        reads = adapter.current_session_reads
        adapter._session_id = value
        observed = service.current_session_id
        if value is None:
            assert observed is None
        else:
            assert observed == value and type(observed) is str  # "" stays a string
        assert adapter.current_session_reads == reads + 1  # exactly one getter call

    assert adapter.current_session_reads == 5  # positive getter counts, no caching
    assert adapter.stream_calls == 0  # no start/disconnect/handle mutation
    assert adapter.disconnected == 0
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
    assert service.provider is adapter  # unchanged instance and handle
    assert service.active_handle() is handle
    assert handle.stops == 0


class _SessionPairProvider(BaseProvider):
    """Distinct current and response session values, set independently."""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.current_value: str | None = None
        self.response_value = ""

    @property
    def current_session_id(self) -> str | None:
        return self.current_value

    @property
    def response_session_id(self) -> str:
        return self.response_value

    def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        del request, register_handle
        return self._turn()

    async def _turn(self) -> AsyncGenerator[StreamEvent, None]:
        yield ResultEvent(type="result", result="ok", session_id="s")


async def test_current_session_id_is_independent_of_the_response_fence(
    tmp_path: Path,
) -> None:
    """Changing either session concept leaves the other's return unchanged."""
    service = ProviderService(_config(tmp_path))
    adapter = _SessionPairProvider(tmp_path)
    service._provider = adapter

    adapter.current_value = "cur-A"
    adapter.response_value = "resp-A"
    assert service.current_session_id == "cur-A"
    assert adapter.response_session_id == "resp-A"

    adapter.current_value = "cur-B"
    assert service.current_session_id == "cur-B"
    assert adapter.response_session_id == "resp-A"  # response unchanged

    adapter.current_value = None
    assert service.current_session_id is None
    assert adapter.response_session_id == "resp-A"  # response unchanged

    adapter.response_value = "resp-B"
    assert service.current_session_id is None  # current unchanged
    assert adapter.response_session_id == "resp-B"

    # An adapter without a response override keeps its inherited "" fence
    # while its current session id is nonempty: no policy derives one.
    inheriting = _ToggleProvider(tmp_path)
    inheriting._session_id = "sid-A"
    service._provider = inheriting
    assert service.current_session_id == "sid-A"
    assert inheriting.response_session_id == ""


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


_SESSION_FAILURE = RuntimeError("sentinel session read failure")
_SESSION_ATTR_FAILURE = AttributeError("sentinel session attribute failure")


class _FailingSessionProvider(_ToggleProvider):
    @property
    def current_session_id(self) -> str | None:
        self.current_session_reads += 1
        raise _SESSION_FAILURE


async def test_service_propagates_a_session_getter_failure_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A getter failure propagates the exact object once, with no fallback or startup."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _FailingSessionProvider(tmp_path)
    service._provider = adapter
    handle = _CountingHandle()
    service._active_handle = handle

    with pytest.raises(RuntimeError) as excinfo:
        service.current_session_id

    assert excinfo.value is _SESSION_FAILURE  # exact object, no coercion
    assert adapter.current_session_reads == 1  # exactly one getter call
    assert counts.ensure == []  # no fallback read, lookup or construction
    assert counts.streams == []
    assert counts.disconnects == []
    assert counts.registrations == []
    assert counts.registry_lookups == []
    assert counts.factory_resolutions == []
    assert counts.constructions == []
    assert counts.sdk_constructions == []
    assert counts.server_invocations == []
    assert counts.subprocess_invocations == []
    assert service.provider is adapter  # unchanged final state
    assert service.active_handle() is handle
    assert handle.stops == 0


class _BrokenSessionGetterProvider(_ToggleProvider):
    @property
    def current_session_id(self) -> str | None:
        self.current_session_reads += 1
        raise _SESSION_ATTR_FAILURE


async def test_service_propagates_an_attribute_error_from_a_session_getter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A getter-raised AttributeError is an invalid contract, not an absent member.

    The former duck-typed service read (``getattr(..., None)``) returned its
    default here, masking broken getters; direct typed access deliberately
    propagates the error. Valid unknown state is None, not an exception.
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _BrokenSessionGetterProvider(tmp_path)
    service._provider = adapter
    handle = _CountingHandle()
    service._active_handle = handle

    with pytest.raises(AttributeError) as excinfo:
        service.current_session_id

    assert excinfo.value is _SESSION_ATTR_FAILURE  # exact object, no masking
    assert adapter.current_session_reads == 1  # exactly one getter call
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
    assert service.provider is adapter  # unchanged final state
    assert service.active_handle() is handle
    assert handle.stops == 0


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
    assert provider.is_connected is True
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


async def test_builtin_session_ids_are_observed_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude forwards its stored id verbatim; OpenCode maps its "" to None.

    Both are attached to a real service without any connection. The counters
    are installed after construction, so setup is never mistaken for
    forbidden accessor work; the getters and their unknown representations
    are used exactly as the built-ins define them.
    """
    provider = ClaudeProvider(tmp_path)
    opencode = OpencodeProvider(tmp_path)
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    handle = _CountingHandle()
    service._active_handle = handle

    service._provider = provider
    assert service.current_session_id is None  # Claude's initial unknown
    provider._session_id = "sid-1"
    assert service.current_session_id == "sid-1"
    provider._session_id = "sid-2"
    assert service.current_session_id == "sid-2"
    provider._session_id = None  # Claude's existing unknown representation
    assert service.current_session_id is None

    service._provider = opencode
    assert service.current_session_id is None  # "" internally: OpenCode's unknown
    opencode._session_id = "sid-1"
    assert service.current_session_id == "sid-1"
    opencode._session_id = "sid-2"
    assert service.current_session_id == "sid-2"
    opencode._session_id = ""  # OpenCode's existing unknown representation
    assert service.current_session_id is None

    # Exact zero read work: no lookup, resolution, construction or teardown.
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
    assert handle.stops == 0
    assert service.provider is opencode


# ── service disconnect ───────────────────────────────────────────────────


async def test_service_disconnect_clears_only_the_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """disconnect() clears the provider instance; handle clearing stays with stops.

    A seeded session id is observed before disconnect and reads None after,
    with no retained last-known value and no provider reconstruction.
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    adapter = _ToggleProvider(tmp_path)
    adapter._session_id = "sid-live"
    service._provider = adapter
    handle = _CountingHandle()
    service._active_handle = handle

    assert service.current_session_id == "sid-live"  # observed before disconnect

    await service.disconnect()

    assert counts.disconnects == [True]  # the service boundary ran
    assert adapter.disconnected == 1  # adapter disconnect called exactly once
    assert service.provider is None
    assert service.can_drain is False  # false/false/empty afterwards
    assert service.cli_connected is False
    assert service.current_session_id is None  # no retained last-known id
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


async def test_service_without_instance_reports_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider instance: the session observation is None on repeated reads,
    the service stays unattached and every seam stays demonstrably at zero."""
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    handle = _CountingHandle()
    service._active_handle = handle

    assert service.current_session_id is None
    assert service.current_session_id is None  # repeated read: still None

    # Every seam counted at its own boundary, not inferred from the value.
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
    assert handle.stops == 0
    assert service.active_handle() is handle  # a seeded handle is unchanged
    assert service.provider is None


# ── model catalog contract ───────────────────────────────────────────────


async def test_minimal_adapter_model_catalog_default_is_fresh(
    tmp_path: Path,
) -> None:
    """The inherited catalog default returns a fresh empty list per call.

    Calls go through the base-typed class without constructing the adapter:
    an empty result means no dynamic catalog supplied by this operation, not
    that the provider has no models. Two distinct roots and both force values
    share the same behavior; mutating one result never leaks into the others.
    """
    adapter_class: type[BaseProvider] = _MinimalProvider
    root_one = tmp_path / "catalog-one"
    root_two = tmp_path / "catalog-two"
    root_one.mkdir()
    root_two.mkdir()

    first: list[dict[str, Any]] = await adapter_class.model_catalog(root_one)
    second: list[dict[str, Any]] = await adapter_class.model_catalog(
        root_two, force=False
    )
    third: list[dict[str, Any]] = await adapter_class.model_catalog(
        root_one, force=True
    )

    assert first == []
    assert second == []
    assert third == []
    assert first is not second
    assert first is not third
    assert second is not third

    # A caller mutating its result does not corrupt any other call's result,
    # and the unchanged class defaults are still in place.
    first.append({"model": "sentinel"})
    assert second == []
    assert third == []
    assert adapter_class.capabilities == ProviderCapabilities()


async def test_default_model_catalog_does_no_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inherited catalog operation is a no-start class operation.

    Both force values answer [] with no adapter construction, no service
    streaming/disconnect/registration, no registry or factory resolution, no
    SDK client, server connection or subprocess, and no handle stop. This
    pins the inherited operation only, not any overriding adapter.
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    _patch_constructor(monkeypatch, _MinimalProvider, counts)
    handle = _CountingHandle()
    service._active_handle = handle
    adapter_class: type[BaseProvider] = _MinimalProvider
    root_one = tmp_path / "catalog-quiet-one"
    root_two = tmp_path / "catalog-quiet-two"
    root_one.mkdir()
    root_two.mkdir()

    assert await adapter_class.model_catalog(root_one, force=False) == []
    assert await adapter_class.model_catalog(root_two, force=True) == []

    # Every real seam counted independently and observed at zero.
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
    assert handle.stops == 0
    assert service.active_handle() is handle  # the seeded handle, untouched
    assert service.provider is None  # no instance was ever attached


_CATALOG_ROWS: list[dict[str, Any]] = [
    {"model": "prov/text", "label": "Text", "variants": ["fast"], "images": False},
    {"model": "prov/vision", "label": "Vision", "variants": [], "images": True},
    # All three image answers are pinned: explicit False (text-only), explicit
    # True, and absent (unknown). The extension field is preserved unchanged
    # through the shared dispatch.
    {"model": "prov/unknown", "label": "Unknown", "variants": [], "extra": "preserved"},
]


class _CatalogProvider(_MinimalProvider):
    """A third real adapter whose class-level catalog override is pinned."""

    catalog_calls: list[tuple[type[BaseProvider], Path, bool]] = []

    @classmethod
    async def model_catalog(
        cls, workspace_root: Path, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Exact shared signature; records the dispatch and returns fixed rows."""
        _CatalogProvider.catalog_calls.append((cls, workspace_root, force))
        return _CATALOG_ROWS


async def test_third_adapter_model_catalog_dispatches_through_base_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typed class dispatch reaches an arbitrary adapter override exactly.

    The call site holds ``type[BaseProvider]`` — no fake service or production
    wrapper bypasses it. The override receives the exact subclass, root object
    and force value exactly once per call, and the returned rows pass through
    with identity and content unchanged. This proves the shared dispatch
    contract only, not a registry-backed production consumer migration.
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    _patch_constructor(monkeypatch, _CatalogProvider, counts)
    _CatalogProvider.catalog_calls.clear()
    root_one = tmp_path / "catalog-override-one"
    root_two = tmp_path / "catalog-override-two"
    root_one.mkdir()
    root_two.mkdir()
    adapter_class: type[BaseProvider] = _CatalogProvider

    first: list[dict[str, Any]] = await adapter_class.model_catalog(root_one)
    forced: list[dict[str, Any]] = await adapter_class.model_catalog(
        root_two, force=True
    )

    assert first is _CATALOG_ROWS
    assert forced is _CATALOG_ROWS
    # Content is pinned against independently constructed literals, not only
    # the constant's identity, so a corrupted or redefined shared constant
    # cannot hide behind a passing identity check.
    expected_rows: list[dict[str, Any]] = [
        {"model": "prov/text", "label": "Text", "variants": ["fast"], "images": False},
        {"model": "prov/vision", "label": "Vision", "variants": [], "images": True},
        {"model": "prov/unknown", "label": "Unknown", "variants": [], "extra": "preserved"},
    ]
    assert first == expected_rows
    assert forced == expected_rows
    assert _CatalogProvider.catalog_calls == [
        (_CatalogProvider, root_one, False),
        (_CatalogProvider, root_two, True),
    ]
    # Every seam observed independently and at zero: no construction, stream,
    # service access, registry, factory, SDK, HTTP or subprocess work happens
    # behind the shared dispatch.
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


class _FailingCatalogProvider(_MinimalProvider):
    """A real subclass whose catalog override raises test-controlled errors."""

    pending_failures: list[BaseException] = []
    raised: list[BaseException] = []

    @classmethod
    async def model_catalog(
        cls, workspace_root: Path, *, force: bool = False
    ) -> list[dict[str, Any]]:
        del workspace_root, force
        failure = cls.pending_failures.pop(0)
        cls.raised.append(failure)
        raise failure


async def test_model_catalog_override_failures_are_not_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override's ordinary exception and cancellation propagate exactly.

    Raised once each, through the base-typed class: no retries, no fallback
    list, no construction or process work. The base adds no fail-open
    translation; host retry policy is separate and untested here.
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    _patch_constructor(monkeypatch, _FailingCatalogProvider, counts)
    sentinel = RuntimeError("catalog failed")
    cancellation = asyncio.CancelledError("catalog cancelled")
    _FailingCatalogProvider.pending_failures[:] = [sentinel, cancellation]
    _FailingCatalogProvider.raised.clear()
    adapter_class: type[BaseProvider] = _FailingCatalogProvider
    root = tmp_path / "catalog-failure-root"
    root.mkdir()

    with pytest.raises(RuntimeError) as failure_info:
        await adapter_class.model_catalog(root)
    assert failure_info.value is sentinel

    with pytest.raises(asyncio.CancelledError) as cancellation_info:
        await adapter_class.model_catalog(root)
    assert cancellation_info.value is cancellation

    # Exactly one raise per call, in order, and no fallback results.
    assert _FailingCatalogProvider.raised == [sentinel, cancellation]
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


async def test_claude_inherits_empty_model_catalog_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude answers the catalog contract through the inherited no-work default.

    Both force values return fresh empty lists at temporary roots. Empty is
    not evidence that Claude has no models. The operation is inherited, not a
    Claude override; Claude's declared capabilities are unchanged.
    """
    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    root_one = tmp_path / "catalog-claude-one"
    root_two = tmp_path / "catalog-claude-two"
    root_one.mkdir()
    root_two.mkdir()
    adapter_class: type[BaseProvider] = ClaudeProvider

    first: list[dict[str, Any]] = await adapter_class.model_catalog(root_one)
    forced: list[dict[str, Any]] = await adapter_class.model_catalog(
        root_two, force=True
    )

    assert first == []
    assert forced == []
    assert first is not forced  # fresh lists, not a shared or cached result
    first.append({"model": "sentinel"})
    assert forced == []

    # Capabilities are unchanged by the shared contract addition.
    assert ClaudeProvider.capabilities == ProviderCapabilities(
        resume=True,
        fork=True,
        images=True,
        stop=True,
        permissions=True,
        structured_questions=True,
        thinking_levels=True,
        usage=True,
        quota=True,
        subagents=True,
        background_subagents=True,
        subagent_messages=True,
        session_history=True,
        schedule_unattended=True,
    )
    # The default is inherited, not a Claude override: the descriptor found on
    # the class through the MRO is BaseProvider's own.
    base_descriptor: Any = inspect.getattr_static(BaseProvider, "model_catalog")
    assert inspect.getattr_static(ClaudeProvider, "model_catalog") is base_descriptor

    # No constructor, SDK, HTTP or subprocess work on the inherited path.
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


async def test_opencode_catalog_override_satisfies_shared_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenCode's existing override satisfies the shared typed contract.

    The real classmethod runs end to end with only ``_fetch_model_catalog``
    faked, so descriptor dispatch, per-root caching and keyword-only force
    invalidation all stay real. Precedence:
    ``tests/test_opencode_provider.py::test_model_catalog_is_cached_between_calls``.
    No assertion claims that real OpenCode discovery is side-effect free.
    """
    import ciao.providers.opencode as mod

    service = ProviderService(_config(tmp_path))
    counts = _count_provider_seams(monkeypatch, service)
    root = tmp_path / "catalog-opencode-unique"
    root.mkdir()
    catalog_class: type[BaseProvider] = OpencodeProvider
    fetch_calls: list[tuple[str, Path]] = []
    fetch_payloads: list[list[dict[str, Any]]] = []

    async def counting_fetch(
        cls: type[OpencodeProvider], workspace_root: Path
    ) -> list[dict[str, Any]]:
        fetch_calls.append((cls.__name__, workspace_root))
        generation = len(fetch_payloads) + 1
        rows: list[dict[str, Any]] = [
            {
                "model": f"acme/gen-{generation}",
                "label": f"Gen {generation}",
                "variants": ["fast"],
            },
            {
                "model": f"acme/gen-{generation}-vision",
                "label": f"Vision {generation}",
                "variants": [],
                "images": True,
                "extension": "kept",
            },
        ]
        fetch_payloads.append(rows)
        return rows

    monkeypatch.setattr(
        OpencodeProvider, "_fetch_model_catalog", classmethod(counting_fetch)
    )
    # The override is the adapter's own, and it answers through a base-typed
    # call site below.
    assert "model_catalog" in vars(OpencodeProvider)

    try:
        first: list[dict[str, Any]] = await catalog_class.model_catalog(root)
        second: list[dict[str, Any]] = await catalog_class.model_catalog(root)
        forced: list[dict[str, Any]] = await catalog_class.model_catalog(
            root, force=True
        )

        assert fetch_calls == [
            ("OpencodeProvider", root),
            ("OpencodeProvider", root),
        ]
        assert len(fetch_payloads) == 2  # the cache served the second call

        # Every result is compared against expected generation-labelled values
        # written out as literals here — independent of the fake's own
        # construction, so the two sides cannot share an object or drift
        # together and mask a caching bug.
        expected_first: list[dict[str, Any]] = [
            {
                "model": "acme/gen-1",
                "label": "Gen 1",
                "variants": ["fast"],
            },
            {
                "model": "acme/gen-1-vision",
                "label": "Vision 1",
                "variants": [],
                "images": True,
                "extension": "kept",
            },
        ]
        expected_forced: list[dict[str, Any]] = [
            {
                "model": "acme/gen-2",
                "label": "Gen 2",
                "variants": ["fast"],
            },
            {
                "model": "acme/gen-2-vision",
                "label": "Vision 2",
                "variants": [],
                "images": True,
                "extension": "kept",
            },
        ]

        # First call: exact generation-1 rows with the extension field intact.
        assert first == expected_first
        assert first is not fetch_payloads[0]
        assert first[0] is not fetch_payloads[0][0]
        assert first[1] is not fetch_payloads[0][1]
        assert first[1]["images"] is True
        assert first[1]["extension"] == "kept"

        # The uncached second call is served from cache: generation-1 values
        # again, a fresh object against the prior result and the payload —
        # top level and every row — with no second fetch.
        assert second == expected_first
        assert second is not first
        assert second is not fetch_payloads[0]
        assert second[0] is not first[0]
        assert second[1] is not first[1]
        assert second[0] is not fetch_payloads[0][0]
        assert second[1] is not fetch_payloads[0][1]

        # The forced call refetches generation 2, not the cached generation 1.
        assert forced == expected_forced
        assert forced != expected_first
        assert forced is not fetch_payloads[1]
        assert forced[0] is not fetch_payloads[1][0]
        assert forced[1] is not fetch_payloads[1][1]

        # The fetch seam is faked, so no real construction, streaming, HTTP
        # connection or subprocess happened behind the catalog read.
        assert counts.constructions == []
        assert counts.streams == []
        assert counts.server_invocations == []
        assert counts.subprocess_invocations == []
    finally:
        # Only this unique root's cache entry is removed; other roots and the
        # production cache code are untouched.
        with mod._MODEL_CACHE_GUARD:
            mod._MODEL_CACHE.pop(str(root), None)
