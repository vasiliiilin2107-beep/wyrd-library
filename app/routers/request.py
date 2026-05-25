from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Knowledge
from ..qdrant_store import search_knowledge, store_knowledge
from ..courier import fetch
import logging

router = APIRouter(prefix="/request", tags=["request"])
log = logging.getLogger(__name__)

TTL_DAYS = {"static": None, "fresh": 30, "realtime": 1}
SIMILARITY_THRESHOLD = 0.90


class RequestIn(BaseModel):
    question: str
    category: str = "world"
    namespace: str = "public"
    ttl_type: str = "fresh"


@router.post("")
async def handle_request(body: RequestIn, session: AsyncSession = Depends(get_session)):
    # 1. Semantic search — if close enough, return from cache
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

    # 2. Send courier to the internet
    log.info(f"[Request] cache miss, sending courier for: {body.question[:80]}")
    raw = await fetch(body.question)
    if not raw:
        return {"source": "not_found", "answer": None, "note": "courier returned empty"}

    # 3. Save to library
    days = TTL_DAYS.get(body.ttl_type)
    expires = datetime.utcnow() + timedelta(days=days) if days else None

    rec = Knowledge(
        question=body.question,
        answer=raw,
        source="courier:duckduckgo",
        category=body.category,
        namespace=body.namespace,
        ttl_type=body.ttl_type,
        expires_at=expires,
        request_count=1,
    )
    session.add(rec)
    await session.flush()

    qdrant_id = await store_knowledge(rec.id, body.question, raw, body.category, namespace=body.namespace)
    if qdrant_id:
        rec.qdrant_id = qdrant_id
    await session.commit()

    return {"source": "courier", "knowledge_id": rec.id, "answer": raw}
