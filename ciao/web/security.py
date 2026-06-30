"""HTTP security headers for the PWA server."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline browser security headers to all HTTP responses."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "; ".join(
                [
                    "default-src 'self'",
                    "base-uri 'self'",
                    "object-src 'none'",
                    "frame-ancestors 'none'",
                    "form-action 'self'",
                    "script-src 'self'",
                    # Vue uses dynamic style attributes for measured UI layout.
                    # Keep script-src strict; relax style-src only.
                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
                    "img-src 'self' data: blob:",
                    "media-src 'self' blob:",
                    "font-src 'self' data: https://fonts.gstatic.com",
                    "connect-src 'self' ws: wss: https://fonts.googleapis.com https://fonts.gstatic.com",
                ]
            ),
        )
        return response
