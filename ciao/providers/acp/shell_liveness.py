"""Background shell liveness: settle a shell call once its process has gone.

A background shell's launch acknowledgement names a pid, and nothing on the wire says when that
process ends (OpenCode sends no terminal meta). The session records the pid with its start time
when the acknowledgement is seen, then checks the process table every few seconds. A pid that
has vanished, or whose start time changed (the pid was reused), has ended. Nothing here signals
a process.

The process table is passed in, so the tests drive it with a fake. An empty table means ``ps``
could not be read (and on Windows it always is): nothing is settled then, so it is a no-op there.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

# pid -> (ppid, start time), as ``ciao.os_support.processes.process_table`` returns it.
ProcessTable = Mapping[int, tuple[int, str]]

# Shells tracked at once. A call past the cap is never settled by this check; its card stays
# running as it did before this check existed.
MAX_TRACKED = 16
CHECK_INTERVAL_S = 5.0
# Call ids remembered after they settle, so a replayed acknowledgement never tracks a shell twice.
_MAX_SETTLED_REMEMBERED = 256


@dataclass(slots=True)
class TrackedShell:
    """One background shell: its pid and start time, plus what the end notice needs."""

    pid: int
    started: str
    command: str
    output_path: str
    launched_at: float


class ShellLiveness:
    """The background shells of one session, by call id."""

    def __init__(self) -> None:
        self._tracked: dict[str, TrackedShell] = {}
        self._settled: dict[str, None] = {}

    @property
    def tracking(self) -> bool:
        return bool(self._tracked)

    @property
    def polling(self) -> bool:
        """True while a shell with a known pid is tracked: only those need the process table."""
        return any(shell.pid for shell in self._tracked.values())

    def track_unpolled(self, call_id: str, *, command: str = "", output_path: str = "") -> bool:
        """Track a background shell the provider reports no pid for: it ends only by the provider's own settle."""
        if call_id in self._tracked:
            return True
        if call_id in self._settled or len(self._tracked) >= MAX_TRACKED:
            return False
        self._tracked[call_id] = TrackedShell(
            pid=0, started="", command=command, output_path=output_path, launched_at=time.monotonic(),
        )
        return True

    def track(
        self,
        call_id: str,
        pid: int,
        table: ProcessTable,
        *,
        command: str = "",
        output_path: str = "",
    ) -> bool:
        """Record a launched shell's pid and start time. False when it cannot be tracked."""
        if call_id in self._tracked:
            return True
        if call_id in self._settled or len(self._tracked) >= MAX_TRACKED:
            return False
        entry = table.get(pid)
        if entry is None:
            return False
        self._tracked[call_id] = TrackedShell(
            pid=pid,
            started=entry[1],
            command=command,
            output_path=output_path,
            launched_at=time.monotonic(),
        )
        return True

    def clear(self) -> None:
        """Stop tracking every shell (the session was stopped or closed); none is settled by it."""
        self._tracked.clear()

    def release(self, call_id: str) -> TrackedShell | None:
        """End tracking of *call_id* because the provider settled it. The shell, or None if not tracked.

        A settle from the provider's own events (Claude's ``terminal_exit``, a normalizer
        result) and one from the process poll both go through here, so a shell is ended once.
        """
        shell = self._tracked.pop(call_id, None)
        if shell is not None:
            self._remember_settled(call_id)
        return shell

    def settled(self, table: ProcessTable) -> list[tuple[str, TrackedShell]]:
        """Shells whose process is gone or was replaced by a pid reuse. They stop being tracked."""
        if not table or not self._tracked:
            return []
        done: list[tuple[str, TrackedShell]] = []
        for call_id, shell in list(self._tracked.items()):
            if not shell.pid:
                continue  # no pid to check: the provider's own settle ends it
            entry = table.get(shell.pid)
            if entry is None or entry[1] != shell.started:
                done.append((call_id, shell))
                self.release(call_id)
        return done

    def _remember_settled(self, call_id: str) -> None:
        self._settled[call_id] = None
        while len(self._settled) > _MAX_SETTLED_REMEMBERED:
            del self._settled[next(iter(self._settled))]
