"""Model resolution for the post-archive memory pass, and the deterministic-
rejection classifiers the schedule attention check shares with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ciao.config import CiaoConfig


def resolve_insights_model(
    config: CiaoConfig, workspace: str | None, provider: str, *, source_model: str = ""
) -> str:
    """Pick the model for the memory pass and the one-shots that share it.

    ``provider`` is the chat's actual provider. Its Session insights model
    (Settings → Models → that provider's card, ``provider_insights_models``)
    wins; Automatic uses the source chat's model. Calls without a recorded
    source model (for example history imports) use the workspace/provider default.
    """
    override = (config.provider_insights_models or {}).get(provider, "")
    if override:
        return override
    if source_model:
        return source_model
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
    schedule attention check so the two callers classify 400s the
    same way.
    """
    text = str(exc).lower()
    return "too long" in text or "context window" in text or "context_length_exceeded" in text


# Provider messages that name the model as unusable for this call. opencode
# reports this one on a one-shot call (#1066) whose model is served only from
# opencode's own client. Only the text is known here; that the restriction is
# permanent, or that a different config could not lift it, is not something
# this module can establish.
_MODEL_REFUSAL_MARKERS = ("free tier can only be used from within opencode",)


def is_model_refused(exc: Exception) -> bool:
    """True when the provider's message names the model, not the request.

    Distinct from :func:`is_context_overflow` in what the operator has to do
    about it: an overflow is a payload that must be trimmed, while this is a
    model that refused a one-shot call and is worked around by picking another
    in Settings → Models. A refusal recurs on the same configuration, so it is
    not worth a traceback per dispatch — but the classification is a text
    match, not a claim that the model can never serve any call.

    Matched on message text for the same reason as the overflow check: the
    provider returns a plain error result rather than a typed rejection. The
    two are not mutually exclusive for an arbitrary string; callers order the
    checks so an overflow (a payload to fix) wins over a refusal.

    .. note:: the returned match is a heuristic over the message, so an
       unrelated error quoting the same text would classify as a refusal. The
       cost of that is a missing traceback for one run, which the job-history
       error row and the upstream message in ``run.error`` still carry.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _MODEL_REFUSAL_MARKERS)
