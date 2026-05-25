import os
from fastapi import Request
from fastapi.responses import JSONResponse

INTERNAL_TOKEN = os.getenv("WYRD_INTERNAL_TOKEN", "")

_OPEN_PREFIXES = ("/health", "/static")


async def internal_token_middleware(request: Request, call_next):
    path = request.url.path
    if not INTERNAL_TOKEN:
        return await call_next(request)
    for prefix in _OPEN_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return await call_next(request)
    token = request.headers.get("x-wyrd-token", "")
    if token != INTERNAL_TOKEN:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)
