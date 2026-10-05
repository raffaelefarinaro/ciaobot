"""Model resolution for the post-archive memory pass, and the context-overflow
classifier the schedule attention check shares with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ciao.config import CiaoConfig


def resolve_insights_model(
    config: CiaoConfig, workspace: str | None, provider: str
) -> str:
    """Pick the model for the memory pass and the one-shots that share it.

    ``provider`` is the chat's actual provider. Its Session insights model
    (Settings → Models → that provider's card, ``provider_insights_models``)
    wins; Automatic falls through to the provider's default chat model.
    """
    override = (config.provider_insights_models or {}).get(provider, "")
    if override:
        return override
    return config.default_model_for_workspace(workspace, provider)


# The insights model is operator-chosen and may be a slow local/cloud GGUF:
# measured end-to-end calls on such a backend run 214-253s, so the old flat
# 120s budget turned tail latency into a guaranteed TimeoutError and the job
# failed ~79% of the time. Generous on purpose.
_DEFAULT_TIMEOUT_S = 600.0


def is_context_overflow(exc: Exception) -> bool:
    """True for a deterministic oversized-input rejection.

    These fail identically on retry, so re-sending only burns another slow
    call plus the retry wait. Matched on message text because the providers
    surface it as a plain 400 rather than a typed error. Reused by the
    schedule attention classifier so the two callers classify 400s the
    same way.
    """
    text = str(exc).lower()
    return "too long" in text or "context window" in text or "context_length_exceeded" in text
