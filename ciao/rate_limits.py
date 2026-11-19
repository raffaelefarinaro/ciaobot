"""Classify the Claude rate-limit status lines that reach transcripts.

The Claude adapter no longer reports per-bucket quota snapshots: Ciaobot keeps no
quota bookkeeping for ACP agents, and the CLI shows its own limits. Old transcripts
still carry the telemetry lines, so the chat-history and websocket filters drop them
with this predicate.
"""

from __future__ import annotations


def is_rate_limit_telemetry(text: str) -> bool:
    """True for a transient rate-limit status line that shouldn't reach chat.

    Drops the allowed / warning / rejected telemetry pings while letting a
    genuine hard "Rate limit exceeded" through (that surfaces as an error, not
    telemetry). Single source of truth for the chat-history and websocket
    filters. The frontend mirror is in ``web/src/lib/rateLimit.ts`` — keep the
    two in sync.
    """
    return text.strip().startswith("Rate limit:")
