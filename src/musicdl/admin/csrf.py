from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import hmac


class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, auth=None):
        super().__init__(app); self.auth = auth
    async def dispatch(self, request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/admin/login":
            token = request.headers.get("x-csrf-token")
            cookie = request.cookies.get("csrf_token")
            session = request.cookies.get("admin_session")
            valid = self.auth.valid_csrf(session, token) if self.auth else False
            if not valid or not cookie or not hmac.compare_digest(token, cookie):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        return await call_next(request)
