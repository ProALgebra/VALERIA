"""Content-Security-Policy for the HTML the backend serves (the SPA shell).

Defense in depth against XSS in agent output: scripts only from our own origin, no inline
scripts, no plugins, no framing. Angular and Koobiq add `<style>` elements and `style`
attributes at runtime, hence `'unsafe-inline'` for styles only. The production build disables
`inlineCritical`, so the shell has no inline `onload` handler.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    # 'unsafe-eval': the Datetime pipe of Koobiq's filter bar (audit, requests, statistics)
    # formats periods through DateFormatter (durationLong, rangeShortDate, rangeShortDateTime),
    # and @koobiq/date-formatter compiles messageformat templates with `new Function`. Our own
    # code does not call it. Inline and injected scripts stay blocked: never 'unsafe-inline'.
    "script-src 'self' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class ContentSecurityPolicyMiddleware:
    """Adds the CSP to every `text/html` response, except opt-in API docs pages (CDN scripts)."""

    def __init__(self, app: ASGIApp, exempt_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        async def send_with_csp(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if content_type.lower().startswith(b"text/html"):
                    csp = CONTENT_SECURITY_POLICY.encode("latin-1")
                    headers.append((b"content-security-policy", csp))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_csp)
