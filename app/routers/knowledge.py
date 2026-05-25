from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Knowledge
from ..qdrant_store import search_knowledge, store_knowledge

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

TTL_DAYS = {"static": None, "fresh": 30, "realtime": 1}


class KnowledgeIn(BaseModel):
    question: str
    answer: str
    source: str = "direct"
    category: str = "world"
    ttl_type: str = "fresh"


class RateIn(BaseModel):
    rating: int  # +1 or -1


@router.post("", status_code=201)
async def create_knowledge(body: KnowledgeIn, session: AsyncSession = Depends(get_session)):
    days = TTL_DAYS.get(body.ttl_type)
    expires = datetime.utcnow() + timedelta(days=days) if days else None
    rec = Knowledge(
        question=body.question, answer=body.answer, source=body.source,
        category=body.category, ttl_type=body.ttl_type, expires_at=expires,
    )
    session.add(rec)
    await session.flush()
    qdrant_id = await store_knowledge(rec.id, body.question, body.answer, body.category)
    if qdrant_id:
        rec.qdrant_id = qdrant_id
    await session.commit()
    return {"id": rec.id, "category": rec.category, "ttl_type": rec.ttl_type}


@router.get("/search")
async def search(q: str, category: Optional[str] = None, limit: int = 5):
    results = await search_knowledge(q, category, limit=limit)
    return {"results": results, "count": len(results)}


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)):
    total = (await session.execute(select(func.count()).select_from(Knowledge))).scalar()
    rows = (await session.execute(
        select(Knowledge.category, func.count().label("cnt"))
        .group_by(Knowledge.category)
        .order_by(desc("cnt"))
    )).all()
    return {
        "total": total,
        "by_category": [{"category": r.category, "count": r.cnt} for r in rows],
    }


@router.get("")
async def list_knowledge(
    category: Optional[str] = None,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Knowledge).order_by(desc(Knowledge.request_count)).limit(limit)
    if category:
        stmt = stmt.where(Knowledge.category == category)
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [
        {
            "id": r.id,
            "question": r.question[:120],
            "category": r.category,
            "source": r.source,
            "request_count": r.request_count,
            "rating": r.rating,
            "ttl_type": r.ttl_type,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        }
        for r in rows
    ]}


@router.patch("/{kid}/rate")
async def rate_knowledge(kid: int, body: RateIn, session: AsyncSession = Depends(get_session)):
    if body.rating not in (1, -1):
        raise HTTPException(400, "rating must be 1 or -1")
    rec = await session.get(Knowledge, kid)
    if not rec:
        raise HTTPException(404, "not found")
    rec.rating += body.rating
    await session.commit()
    return {"id": kid, "rating": rec.rating}


@router.delete("/{kid}")
async def delete_knowledge(kid: int, session: AsyncSession = Depends(get_session)):
    rec = await session.get(Knowledge, kid)
    if not rec:
        raise HTTPException(404, "not found")
    await session.delete(rec)
    await session.commit()
    return {"deleted": kid}
