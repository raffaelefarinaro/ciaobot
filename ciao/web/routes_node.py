"""Package status, changelog and update web API routes (``/api/package/*``).

They are the PWA's window onto the package release installed here. The engine
never updates itself through a route: the Ciaobot for Mac app owns that on
macOS, and the installer owns it on Linux (see ``docs/LINUX.md``).
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.config import RESTART_EXIT_CODE
from ciao.package_version import (
    package_changelog,
    package_status,
    update_package,
)


async def package_status_endpoint(request: Request) -> JSONResponse:
    """Return installed package version and best-effort update status."""
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    if callable(fetcher):
        return JSONResponse(await asyncio.to_thread(fetcher))
    return JSONResponse(await asyncio.to_thread(package_status))


async def package_changelog_endpoint(request: Request) -> JSONResponse:
    """Return the commits between the installed and latest release for the update modal."""
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    status = await asyncio.to_thread(fetcher if callable(fetcher) else package_status)
    current = str(status.get("current_version") or "")
    latest = str(status.get("latest_version") or "")
    changelog = await asyncio.to_thread(
        package_changelog,
        current_version=current,
        latest_version=latest,
    )
    return JSONResponse(
        {
            "current_version": current,
            "latest_version": latest,
            "update_available": bool(status.get("update_available")),
            **changelog,
        }
    )


async def package_update_endpoint(request: Request) -> JSONResponse:
    """Return update guidance; packaged app updates are owned by Ciaobot.app."""
    res = await asyncio.to_thread(update_package)
    if res.get("ok"):
        async def _do_restart():
            await asyncio.sleep(2)
            fn = getattr(request.app.state, "request_restart", None)
            if callable(fn):
                fn(RESTART_EXIT_CODE)
            else:
                from ciao.signals import RestartRequested
                raise RestartRequested(RESTART_EXIT_CODE)

        asyncio.create_task(_do_restart())
        return JSONResponse(res)
    else:
        status_code = 400 if res.get("mode") in {"bundled_app", "editable", "installer", "unknown"} else 500
        return JSONResponse(res, status_code=status_code)
