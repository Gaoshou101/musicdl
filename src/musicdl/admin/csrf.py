from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import hmac

from .forms import form_fields


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit guard scoped to one path prefix.

    The middleware is mounted on the whole application, so it must never touch
    routes that carry their own authentication, such as the WeCom callback.

    The token may arrive as the ``x-csrf-token`` header or, for the portal's own
    HTML forms, as the ``csrf_token`` form field. A form post cannot set a
    header, and dropping the guard for those routes would leave the credential
    change reachable from any page on the internet.
    """

    def __init__(self, app, auth=None, prefix="/admin", exempt=()):
        super().__init__(app); self.auth = auth; self.prefix = prefix.rstrip("/") or "/admin"
        # The login routes run before a session exists, so there is no token to
        # double-submit yet; every other route is guarded on both transports.
        self.exempt = frozenset(exempt) | {f"{self.prefix}/login", f"{self.prefix}/login-form"}

    def _guarded(self, path: str) -> bool:
        return path == self.prefix or path.startswith(self.prefix + "/")

    async def _form_token(self, request):
        """The token from an HTML form body, or None for any other request."""
        value = (await form_fields(request)).get("csrf_token")
        return value if isinstance(value, str) else None

    async def dispatch(self, request, call_next):
        path = request.url.path
        if (self._guarded(path) and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and path not in self.exempt):
            token = request.headers.get("x-csrf-token") or await self._form_token(request)
            cookie = request.cookies.get("csrf_token")
            session = request.cookies.get("admin_session")
            valid = self.auth.valid_csrf(session, token) if self.auth else False
            if not valid or not cookie or not hmac.compare_digest(token, cookie):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        return await call_next(request)
