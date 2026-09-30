"""``python -m ciao``: the same entry point as the ``ciao`` console script."""

from __future__ import annotations

import sys

from ciao.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))