#!/usr/bin/env python3
"""Thin wrapper around the in-package adversarial-review engine.

The engine lives in ``ciao.critique`` and uses the Claude Agent SDK with
per-model routing (OpenRouter / Ollama / Anthropic). This wrapper keeps the
``$SKILL_DIR/scripts/review.py`` interface the skill documents.
"""

from __future__ import annotations

import sys

from ciao.critique import main

if __name__ == "__main__":
    sys.exit(main())
