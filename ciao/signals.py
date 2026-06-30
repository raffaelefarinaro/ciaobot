"""Signals for process lifecycle."""


class RestartRequested(Exception):
    """Raised to exit the server for a supervisor-driven restart."""
