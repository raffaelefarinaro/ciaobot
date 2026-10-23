"""Authenticated browser endpoints for onboarding progress."""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.onboarding import OnboardingConflictError, OnboardingStateError
from ciao.onboarding_progress import (
    OnboardingFinishRequired,
    OnboardingTargetClosed,
    OnboardingTargetNotFound,
    change_step,
    complete_progress,
    get_progress,
)


def _body(receipt: Any) -> dict[str, Any]:
    return {"schema": receipt.schema, "revision": receipt.revision,
            "installation": {"status": receipt.installation.status, "step": receipt.installation.step},
            "workspaces": {k: {"status": v.status, "step": v.step} for k, v in receipt.workspaces.items()}}


async def _run(request: Request, *, complete: bool = False) -> JSONResponse:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
        allowed = {"expected_revision", "target"} | (set() if complete else {"step"})
        expected = data.get("expected_revision")
        target = data.get("target")
        if (set(data) != allowed or isinstance(expected, bool)
                or not isinstance(expected, int) or expected < 0
                or not isinstance(target, dict)):
            raise ValueError
        config = request.app.state.config
        from ciao.web.routes_api import _workspace_archive_lock
        async with _workspace_archive_lock(request):
            receipt = complete_progress(config, target=target, expected_revision=expected) if complete else change_step(config, target=target, expected_revision=expected, step=data["step"])
        return JSONResponse(_body(receipt), headers={"Cache-Control": "no-store"})
    except (ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid onboarding request.", "code": "invalid_onboarding_request"}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "Onboarding is not initialized.", "code": "onboarding_not_initialized"}, status_code=503)
    except OnboardingTargetNotFound:
        return JSONResponse({"error": "Onboarding target not found.", "code": "onboarding_target_not_found"}, status_code=404)
    except OnboardingConflictError:
        return JSONResponse({"error": "Onboarding revision conflict.", "code": "onboarding_revision_conflict"}, status_code=409)
    except OnboardingTargetClosed:
        return JSONResponse({"error": "Onboarding target is closed.", "code": "onboarding_target_closed"}, status_code=409)
    except OnboardingFinishRequired:
        return JSONResponse({"error": "Finish the onboarding steps first.", "code": "onboarding_finish_required"}, status_code=409)
    except OnboardingStateError:
        return JSONResponse({"error": "Onboarding state is unavailable.", "code": "onboarding_state_unavailable"}, status_code=500)


async def get_onboarding(request: Request) -> JSONResponse:
    try:
        return JSONResponse(_body(get_progress(request.app.state.config)), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return JSONResponse({"error": "Onboarding is not initialized.", "code": "onboarding_not_initialized"}, status_code=503)
    except OnboardingStateError:
        return JSONResponse({"error": "Onboarding state is unavailable.", "code": "onboarding_state_unavailable"}, status_code=500)


async def patch_onboarding(request: Request) -> JSONResponse:
    return await _run(request)


async def complete_onboarding(request: Request) -> JSONResponse:
    return await _run(request, complete=True)
