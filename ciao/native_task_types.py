"""Claude-native background task-type classification for the host Processes projection.

``is_process_task`` is the single rule the host applies to a tracked
Claude-native task record: a shell or Monitor row is a process task, while a
background agent row is excluded because it already has its own row.
"""

from collections.abc import Mapping
from typing import Any


def is_process_task(info: Mapping[str, Any]) -> bool:
    """True for a shell or Monitor task; background agents have their own rows."""
    return "agent" not in str(info.get("task_type") or "")
