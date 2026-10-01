"""One platform-neutral seam for the per-user service operations.

Call sites that used to hardcode ``~/Library/LaunchAgents`` or shell out to
``launchctl`` ask ``current_backend()`` instead. macOS and Linux behave exactly
as before. Any other platform raises ``UnsupportedPlatformError``: that is the
honest answer until a backend for it exists, not a fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

from ciao import macos_service


class UnsupportedPlatformError(RuntimeError):
    """No service backend exists for this platform (or operation)."""


class ServiceBackend(Protocol):
    name: str

    def agents_dir(self) -> Path:
        """Where service definitions are written; honours ``CIAO_LAUNCH_AGENTS_DIR``."""
        ...

    def live_agents_dir(self) -> Path:
        """The real per-user definitions dir, ignoring the override."""
        ...

    def is_live_agents_dir(self, path: Path) -> bool:
        """True when ``path`` resolves to the real per-user definitions dir."""
        ...

    def bootout_agent(self, label: str) -> None:
        """Best-effort removal of a registered agent by label; never raises OSError."""
        ...

    def load_agent(self, definition: Path) -> int:
        """Register ``definition`` with the supervisor; return its exit status."""
        ...

    def schedule_server_handoff(self) -> bool:
        """Spawn a detached helper that starts the server agent after this process exits."""
        ...


class _PlistDirs:
    """Directory rules shared by every backend that renders launchd-format plists.

    Linux writes such plists only for an explicit ``--launch-agents-dir`` (an
    offline export), and applies the same repoint guard as macOS.
    """

    def agents_dir(self) -> Path:
        return macos_service.default_launch_agents_dir()

    def live_agents_dir(self) -> Path:
        return macos_service.live_launch_agents_dir()

    def is_live_agents_dir(self, path: Path) -> bool:
        real_dir = self.live_agents_dir()
        try:
            return path.expanduser().resolve() == real_dir.expanduser().resolve()
        except OSError:
            return path.expanduser() == real_dir


class MacOSBackend(_PlistDirs):
    name = "macos"

    def bootout_agent(self, label: str) -> None:
        macos_service.bootout_agent(label)

    def load_agent(self, definition: Path) -> int:
        return macos_service.load_agent(definition)

    def schedule_server_handoff(self) -> bool:
        return macos_service.schedule_server_handoff()


class LinuxBackend(_PlistDirs):
    name = "linux"

    def bootout_agent(self, label: str) -> None:
        # Nothing is ever registered on Linux (`ciao linux-service` only prints a unit).
        return None

    def load_agent(self, definition: Path) -> int:
        raise UnsupportedPlatformError(
            "Loading a service definition is not supported on Linux. "
            "Use `ciao linux-service` and systemctl."
        )

    def schedule_server_handoff(self) -> bool:
        raise UnsupportedPlatformError(
            "Handing the server to a supervisor is not supported on Linux."
        )


def current_backend() -> ServiceBackend:
    """The backend for this platform. Reads ``sys.platform`` on every call."""
    if sys.platform == "darwin":
        return MacOSBackend()
    if sys.platform.startswith("linux"):
        return LinuxBackend()
    raise UnsupportedPlatformError(
        f"Ciaobot has no service backend for platform {sys.platform!r}; "
        "supported platforms are macOS and Linux."
    )