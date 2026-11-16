"""The account the engine runs as.

``user_key`` is a short, stable string that tells local accounts apart in a
path shared between them (per-user lock directories under the system temp
root). POSIX uses the numeric uid, as the call sites did before this module
existed.
"""

from __future__ import annotations

import os

def user_key() -> str:
    """The numeric uid; see the module docstring."""
    return str(os.getuid())
