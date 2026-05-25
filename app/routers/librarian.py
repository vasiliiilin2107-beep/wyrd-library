import os
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Knowledge
from ..qdrant_store import search_knowledge

router = APIRouter(prefix="/librarian", tags=["librarian"])
log = logging.getLogger(__name__)

KIE_API_KEY = os.environ.get("KIE_API_KEY", "")
KIE_API_URL = os.environ.get("KIE_API_URL", "https://api.kie.ai")
MODEL = os.environ.get("KIE_CHAT_MODEL", "claude-sonnet-4-6")

SYSTEM = """Ты Библиотекарь WYRD — самый начитанный бот в системе НЕЙРОЦЕХ.
Ты отвечаешь ТОЛЬКО из знаний своей Библиотеки. Не выдумываешь. Если в базе нет ответа — говоришь честно.
Отвечай по-русски, чётко и по делу. Ссылайся на источники если знаешь их."""


async def _ask_claude(question: str, context: str, system: Optional[str] = None) -> str:
    if not KIE_API_KEY:
        return "Библиотекарь не настроен: нет KIE_API_KEY."
    prompt = f"Знания из Библиотеки:\n{context}\n\nВопрос: {question}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{KIE_API_URL}/claude/v1/messages",
                headers={"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "max_tokens": 1500,
                    "system": system or SYSTEM,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            return r.json()["content"][0]["text"]
    except Exception as e:
        log.warning(f"[Librarian] Claude call failed: {e}")
        raise HTTPException(503, f"Библиотекарь временно недоступен: {e}")


class AskIn(BaseModel):
    question: str
    category: Optional[str] = None
    namespace: Optional[str] = None
    top_k: int = 5


@router.post("/ask")
async def ask_librarian(body: AskIn, session: AsyncSession = Depends(get_session)):
    if not body.question.strip():
        raise HTTPException(400, "question is empty")

    hits = await search_knowledge(body.question, body.category, limit=body.top_k, namespace=body.namespace)

    if not hits:
        return {
            "answer": "В Библиотеке пока нет знаний по этой теме. Можешь добавить через /request или /knowledge.",
            "sources": [],
            "from_cache": False,
        }

    ids = [h.get("knowledge_id") for h in hits if h.get("knowledge_id")]
    rows: list[Knowledge] = []
    if ids:
        result = await session.execute(select(Knowledge).where(Knowledge.id.in_(ids)))
        rows = result.scalars().all()
    rows_map = {r.id: r for r in rows}

    context_parts = []
    sources = []
    for i, hit in enumerate(hits, 1):
        kid = hit.get("knowledge_id")
        rec = rows_map.get(kid)
        if rec:
            context_parts.append(f"{i}. [Категория: {rec.category}] Q: {rec.question}\nA: {rec.answer[:600]}")
            sources.append({"id": kid, "score": hit["score"], "category": rec.category, "source": rec.source})
        else:
            context_parts.append(f"{i}. {hit.get('question', '?')}")

    context = "\n\n".join(context_parts)
    answer = await _ask_claude(body.question, context)

    return {
        "answer": answer,
        "sources": sources,
        "from_cache": True,
        "knowledge_used": len(sources),
    }
