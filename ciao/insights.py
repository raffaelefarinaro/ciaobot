"""Model resolution for the post-archive memory pass, and the context-overflow
classifier the schedule attention check shares with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ciao.config import CiaoConfig


def resolve_insights_model(
    config: CiaoConfig, workspace: str | None = None, provider: str | None = None
) -> str:
    """Pick the model for the post-archive memory pass.

    When the operator has not set an explicit override (Settings → Models →
    Session insights = Automatic), use the workspace/provider default model.
    Scripts without either context fall back to ``config.insights_model``.

    ``provider``, when given, is the chat's actual provider; it is passed
    through to ``default_model_for_workspace`` so an opencode chat in a
    Claude-default workspace resolves that provider's own default model
    instead of a Claude tier alias.
    """
    if config.insights_model_override:
        return config.insights_model_override
    if workspace is not None or provider is not None:
        return config.default_model_for_workspace(workspace, provider)
    return config.insights_model


# The insights model is operator-chosen and may be a slow local/cloud GGUF:
# measured end-to-end calls on such a backend run 214-253s, so the old flat
# 120s budget turned tail latency into a guaranteed TimeoutError and the job
# failed ~79% of the time. Generous on purpose.
_DEFAULT_TIMEOUT_S = 600.0


def _resolve_insights_call(
    config, model: str, *, provider: str = "claude"
) -> tuple[str, str, str | None]:
    """Resolve an insights model to (effective_model, provider, note).

    The requested model is used as-is; ``note`` is always ``None`` now that no
    model is substituted, and stays in the shape for the callers that log it.
    """
    # Routine settings qualify runtime-provider overrides so a global choice
    # is not accidentally sent through Claude (the default one-shot provider).
    for routed_provider in ("opencode",):
        prefix = f"{routed_provider}:"
        if model.startswith(prefix):
            return model[len(prefix):] or "sonnet", routed_provider, None

    return model, provider, None


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
