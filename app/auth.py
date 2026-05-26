import os
from fastapi import Request
from fastapi.responses import JSONResponse

# Comma-separated list of allowed tokens — one per service
_raw = os.getenv("WYRD_ALLOWED_TOKENS", "") or os.getenv("WYRD_INTERNAL_TOKEN", "")
ALLOWED_TOKENS = {t.strip() for t in _raw.split(",") if t.strip()}

_OPEN_PREFIXES = ("/health", "/static")


async def internal_token_middleware(request: Request, call_next):
    path = request.url.path
    if not ALLOWED_TOKENS:
        return await call_next(request)
    for prefix in _OPEN_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return await call_next(request)
    token = request.headers.get("x-wyrd-token", "")
    if token not in ALLOWED_TOKENS:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)
