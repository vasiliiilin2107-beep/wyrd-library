import os
import logging
from urllib.parse import quote
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Knowledge
from ..qdrant_store import search_knowledge

router = APIRouter(prefix="/request", tags=["request"])
log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.90

WYRD_QUARANTINE_URL = os.environ.get(
    "WYRD_QUARANTINE_URL",
    "http://ktup27quru59l1m4wfes69ow.147.45.212.155.sslip.io",
)
WYRD_INTERNAL_TOKEN = os.environ.get("WYRD_INTERNAL_TOKEN", "")


def _huginn_headers() -> dict:
    if WYRD_INTERNAL_TOKEN:
        return {"x-wyrd-token": WYRD_INTERNAL_TOKEN}
    return {}


class RequestIn(BaseModel):
    question: str
    category: str = "world"
    namespace: str = "public"
    ttl_type: str = "fresh"
    cache_only: bool = False  # если True — только кэш, Хугин не летит


@router.post("")
async def handle_request(body: RequestIn, session: AsyncSession = Depends(get_session)):
    # 1. Поиск в кэше
    hits = await search_knowledge(body.question, body.category, limit=1, namespace=body.namespace)
    if hits and hits[0]["score"] >= SIMILARITY_THRESHOLD:
        kid = hits[0].get("knowledge_id")
        if kid:
            rec = await session.get(Knowledge, kid)
            if rec and (rec.expires_at is None or rec.expires_at > datetime.utcnow()):
                rec.request_count += 1
                await session.commit()
                return {
                    "source": "cache",
                    "knowledge_id": kid,
                    "answer": rec.answer,
                    "score": hits[0]["score"],
                }

    # 2. Cache miss
    if body.cache_only:
        return {"source": "not_found", "answer": None}

    # 3. Заряжаем Хугина — всё внешнее только через Scout → Карантин → Библиотека
    log.info("[Request] cache miss → Huginn: %.80s", body.question)
    search_url = f"https://html.duckduckgo.com/html/?q={quote(body.question)}"

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                f"{WYRD_QUARANTINE_URL}/huginn/scout",
                json={"url": search_url, "task": body.question, "category": body.category},
                headers=_huginn_headers(),
            )
    except Exception as e:
        log.warning("[Request] Huginn unreachable: %s", e)
        return {"source": "error", "answer": None, "note": f"Huginn unreachable: {e}"}

    if resp.status_code == 403:
        log.error("[Request] Huginn 403 — add library token to quarantine WYRD_ALLOWED_TOKENS")
        return {"source": "error", "answer": None, "note": "auth error"}

    if resp.status_code != 200:
        log.warning("[Request] Huginn HTTP %s", resp.status_code)
        return {"source": "error", "answer": None, "note": f"Huginn HTTP {resp.status_code}"}

    data = resp.json()
    status = data.get("status")
    result = data.get("result")

    if status in ("library_hit", "clean") and result:
        return {"source": "huginn", "answer": result, "library_written": data.get("library_written", False)}

    if status in ("threat", "quarantine_offline", "blocked", "fetch_error"):
        log.warning("[Request] Huginn blocked content: status=%s threats=%s", status, data.get("threats"))
        return {"source": "blocked", "answer": None, "status": status}

    return {"source": "not_found", "answer": None}
