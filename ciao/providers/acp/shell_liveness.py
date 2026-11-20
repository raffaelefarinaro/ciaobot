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

from collections.abc import Mapping

# pid -> (ppid, start time), as ``ciao.os_support.processes.process_table`` returns it.
ProcessTable = Mapping[int, tuple[int, str]]

# Shells tracked at once. A call past the cap is never settled by this check; its card stays
# running as it did before this check existed.
MAX_TRACKED = 16
CHECK_INTERVAL_S = 5.0


class ShellLiveness:
    """The background shells of one session, by call id, with the pid and start time each had."""

    def __init__(self) -> None:
        self._tracked: dict[str, tuple[int, str]] = {}

    @property
    def tracking(self) -> bool:
        return bool(self._tracked)

    def track(self, call_id: str, pid: int, table: ProcessTable) -> bool:
        """Record a launched shell's pid and start time. False when it cannot be tracked."""
        if call_id in self._tracked:
            return True
        if len(self._tracked) >= MAX_TRACKED:
            return False
        entry = table.get(pid)
        if entry is None:
            return False
        self._tracked[call_id] = (pid, entry[1])
        return True

    def settled(self, table: ProcessTable) -> list[str]:
        """Call ids whose process is gone or was replaced by a pid reuse. They stop being tracked."""
        if not table or not self._tracked:
            return []
        done: list[str] = []
        for call_id, (pid, started) in list(self._tracked.items()):
            entry = table.get(pid)
            if entry is None or entry[1] != started:
                done.append(call_id)
                del self._tracked[call_id]
        return done
