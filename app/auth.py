import base64
import hmac

from .config import settings


class BasicAuthMiddleware:
    """Tiny pure-ASGI Basic Auth middleware.

    Pure ASGI is used intentionally so large uploads and ranged video responses
    are not wrapped by BaseHTTPMiddleware.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        # Static shell is not sensitive and must be fetchable by the browser's
        # PWA manifest/service-worker machinery, which may omit Basic Auth.
        if (
            path == "/api/health"
            or path.startswith("/assets/")
            or path == "/favicon.ico"
            # HTML5 <video> issues independent Range requests and may omit the
            # browser's Basic-Auth header. The compose file binds Cake only to
            # 127.0.0.1 by default, so media streaming is reachable through the
            # user's SSH tunnel but not from the public Internet.
            or (path.startswith("/api/videos/") and path.endswith("/stream"))
        ):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw = headers.get(b"authorization", b"").decode("latin-1")
        ok = False
        if raw.startswith("Basic "):
            try:
                decoded = base64.b64decode(raw[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                ok = hmac.compare_digest(username, settings.app_user) and hmac.compare_digest(
                    password, settings.app_password
                )
            except Exception:
                ok = False

        if ok:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        body = b"Authentication required"
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Basic realm="LexiQuest Cake"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
