"""The process's own file-descriptor budget, raised at server startup.

The engine holds one descriptor per accepted connection. A service manager
(launchd, systemd) starts it with a low soft ``RLIMIT_NOFILE`` unless the
service definition says otherwise: macOS's default is 256, a budget a
long-running PWA session with a handful of tabs can exhaust, after which
``accept()`` fails with ``Errno 24`` and the engine answers no endpoint at
all. Raising the soft limit in-process makes ``ciao run``, ``ciao supervise``
and every service manager start the engine with the same budget, independent
of whether the unit file was refreshed by an upgrade.

POSIX clamps the request to the hard limit: the kernel refuses a soft limit
above it, and the hard limit is the ceiling a process cannot pass without
privilege. A failure is swallowed — the engine must start even on a host that
forbids the call. Windows is a no-op: the launchd/systemd limit does not apply
there, and the CRT descriptor table is already large, so the function exists
only to keep call sites free of a platform branch.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# Comfortably above the low service-manager default (256 on macOS) and well
# under the hard limit most hosts allow, so a long PWA session with several
# tabs, websockets and short-lived polls never reaches it. Named, not an env
# var: the value is a product decision, not per-install configuration.
SERVER_NOFILE_TARGET = 4096

if sys.platform == "win32":

    def raise_file_descriptor_limit(target: int = SERVER_NOFILE_TARGET) -> None:
        """No-op on Windows; see the module docstring."""
        return

else:
    import resource

    def raise_file_descriptor_limit(target: int = SERVER_NOFILE_TARGET) -> None:
        """Raise the soft ``RLIMIT_NOFILE`` to *target*, clamped to the hard limit.

        Best-effort: a ``getrlimit``/``setrlimit`` failure is logged and
        swallowed so the caller keeps starting. An already-sufficient soft
        limit (including infinity) is left untouched.
        """
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        except (OSError, ValueError):
            logger.warning("Could not read the file-descriptor limit", exc_info=True)
            return
        desired = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft == resource.RLIM_INFINITY or soft >= desired:
            logger.debug(
                "File-descriptor soft limit already %s (target %s)", soft, desired
            )
            return
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
        except (OSError, ValueError):
            logger.warning(
                "Could not raise the file-descriptor soft limit from %s to %s",
                soft,
                desired,
                exc_info=True,
            )
            return
        logger.info(
            "Raised the file-descriptor soft limit from %s to %s", soft, desired
        )
