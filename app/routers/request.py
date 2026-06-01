import os
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Knowledge
from ..qdrant_store import search_knowledge, store_knowledge
from ..courier import search as courier_search

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

    # 3. Cache miss → Perplexity Sonar (реальный поиск в интернете)
    log.info("[Request] cache miss → Perplexity: %.80s", body.question)

    result = await courier_search(body.question)
    if not result["answer"] or len(result["answer"]) < 20:
        return {"source": "not_found", "answer": None}

    source = result["sources"][0] if result["sources"] else "perplexity/sonar"
    days = {"static": None, "fresh": 30, "realtime": 1}.get(body.ttl_type)
    rec = Knowledge(
        question=body.question,
        answer=result["answer"],
        source=source,
        category=body.category,
        namespace=body.namespace,
        ttl_type=body.ttl_type,
        expires_at=datetime.utcnow() + timedelta(days=days) if days else None,
    )
    session.add(rec)
    await session.commit()
    await session.refresh(rec)

    qdrant_id = await store_knowledge(rec.id, body.question, result["answer"], body.category, namespace=body.namespace)
    if qdrant_id:
        rec.qdrant_id = qdrant_id
        await session.commit()

    return {"source": "perplexity", "answer": result["answer"], "library_written": True, "knowledge_id": rec.id}
