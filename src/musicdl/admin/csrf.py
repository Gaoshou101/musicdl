from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import hmac


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit guard scoped to one path prefix.

    The middleware is mounted on the whole application, so it must never touch
    routes that carry their own authentication, such as the WeCom callback.
    """

    def __init__(self, app, auth=None, prefix="/admin"):
        super().__init__(app); self.auth = auth; self.prefix = prefix.rstrip("/") or "/admin"

    def _guarded(self, path: str) -> bool:
        return path == self.prefix or path.startswith(self.prefix + "/")

    async def dispatch(self, request, call_next):
        path = request.url.path
        if (self._guarded(path) and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and path != f"{self.prefix}/login"):
            token = request.headers.get("x-csrf-token")
            cookie = request.cookies.get("csrf_token")
            session = request.cookies.get("admin_session")
            valid = self.auth.valid_csrf(session, token) if self.auth else False
            if not valid or not cookie or not hmac.compare_digest(token, cookie):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        return await call_next(request)
