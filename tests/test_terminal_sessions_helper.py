"""Engine helper terminal sessions: a fixed argv, read and typed by the engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.terminal_sessions import (
    TerminalSessionError,
    TerminalSessionManager,
)
from tests.test_terminal_sessions import (
    FakeClock,
    FakeProcess,
    _reason,
    _wait_until,
)


class ArgvFactory:
    """A process factory that records the argv it was asked to run, if any.

    ``"absent"`` in ``argvs`` means the factory was called without an argv.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.argvs: list[object] = []
        self.processes: list[FakeProcess] = []

    def __call__(
        self,
        *,
        cwd: Path,
        cols: int = 80,
        rows: int = 24,
        argv: list[str] | None = None,
    ) -> FakeProcess:
        self.calls += 1
        self.argvs.append(argv if argv is not None else "absent")
        process = FakeProcess(pid=5_000 + self.calls)
        self.processes.append(process)
        return process

    @property
    def process(self) -> FakeProcess:
        assert len(self.processes) == 1, self.processes
        return self.processes[0]


def _manager(factory: ArgvFactory) -> TerminalSessionManager:
    return TerminalSessionManager(clock=FakeClock(), factory=factory)


async def test_create_with_argv_hands_argv_to_the_factory(tmp_path: Path) -> None:
    factory = ArgvFactory()
    manager = _manager(factory)
    try:
        await manager.create(
            tmp_path, "chat", "c1", argv=["/bin/echo", "hi"]
        )
        assert factory.argvs == [["/bin/echo", "hi"]]
    finally:
        await manager.shutdown()


async def test_create_without_argv_never_passes_argv(tmp_path: Path) -> None:
    factory = ArgvFactory()
    manager = _manager(factory)
    try:
        await manager.create(tmp_path, "chat", "c1")
        assert factory.argvs == ["absent"]
    finally:
        await manager.shutdown()


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["/bin/x", ""],
        ["/bin/x\x00"],
        ["/bin/x"] * 17,
        ["/bin/" + "x" * 4097],
    ],
)
async def test_argv_is_refused_before_spawn(tmp_path: Path, argv: list[str]) -> None:
    factory = ArgvFactory()
    manager = _manager(factory)
    try:
        assert (
            await _reason(manager.create, tmp_path, "chat", "c1", argv=argv)
            == "invalid_input"
        )
        assert factory.calls == 0
        assert manager.retained_count() == 0
    finally:
        await manager.shutdown()


async def test_helper_output_returns_the_programs_bytes(tmp_path: Path) -> None:
    factory = ArgvFactory()
    manager = _manager(factory)
    try:
        info = await manager.create(
            tmp_path, "chat", "c1", argv=["/bin/echo", "hi"]
        )
        factory.process.feed(b"hello")
        await _wait_until(
            lambda: manager.helper_output(info.session_id) == b"hello",
            what="the helper's output",
        )
    finally:
        await manager.shutdown()


async def test_helper_calls_refuse_a_user_shell(tmp_path: Path) -> None:
    factory = ArgvFactory()
    manager = _manager(factory)
    try:
        info = await manager.create(tmp_path, "chat", "c1")
        assert (
            await _reason(manager.helper_output, info.session_id) == "invalid_target"
        )
        assert (
            await _reason(manager.helper_input, info.session_id, b"x")
            == "invalid_target"
        )
        assert await _reason(manager.helper_output, "no-such-id") == "not_found"
        assert await _reason(manager.helper_input, "no-such-id", b"x") == "not_found"
    finally:
        await manager.shutdown()


async def test_helper_input_needs_no_controller(tmp_path: Path) -> None:
    factory = ArgvFactory()
    manager = _manager(factory)
    try:
        info = await manager.create(
            tmp_path, "chat", "c1", argv=["/bin/echo", "hi"]
        )
        assert manager.helper_input(info.session_id, b"\r") == 1
        await _wait_until(
            lambda: bytes(factory.process.written) == b"\r",
            what="the helper's input",
        )
    finally:
        await manager.shutdown()


async def test_helper_input_after_exit_is_closed(tmp_path: Path) -> None:
    factory = ArgvFactory()
    manager = _manager(factory)
    try:
        info = await manager.create(
            tmp_path, "chat", "c1", argv=["/bin/echo", "hi"]
        )
        factory.process.finish(0)
        await _wait_until(
            lambda: manager.get(info.session_id).exited,
            what="the helper's exit",
        )
        assert (
            await _reason(manager.helper_input, info.session_id, b"x") == "closed"
        )
    finally:
        await manager.shutdown()
