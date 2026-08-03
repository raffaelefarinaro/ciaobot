#!/usr/bin/env python3
"""Deprecated: ``ciao memory`` was removed.

Bounded memory now lives in the fenced ``ciao:memory`` / ``ciao:profile``
regions of the workspace ``CLAUDE.md``. Edit those regions directly.
"""

from __future__ import annotations

import sys


if __name__ == "__main__":
    print(
        "ciao memory was removed. Edit the ciao:memory / ciao:profile "
        "regions in the workspace CLAUDE.md instead.",
        file=sys.stderr,
    )
    raise SystemExit(2)
