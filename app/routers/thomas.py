import os
import re
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Knowledge
from ..qdrant_store import search_knowledge, store_knowledge
from .librarian import _ask_claude

router = APIRouter(prefix="/thomas", tags=["thomas"])
log = logging.getLogger(__name__)

THOMAS_TOKEN = os.environ.get("THOMAS_TOKEN", "")
NAMESPACE = "thomas"

THOMAS_SYSTEM = """Ты Библиотекарь WYRD — отвечаешь ТОЛЬКО из личной библиотеки Томаса.
Это изолированное пространство: только данные Томаса, только его факты и история.
Не выдумывай. Если в базе нет ответа — честно скажи. Отвечай по-русски."""

TTL_DAYS = {"static": None, "fresh": 30, "realtime": 1}

# --- Карантин памяти ---
_MIN_Q = 10    # минимум символов в question
_MIN_A = 5     # минимум символов в answer
_MAX_FIELD = 2000  # максимум символов в одном поле

_INJECTION_RE = re.compile(
    r"ignore\s+(previous|all|above|prior)\s+instructions?"
    r"|you\s+are\s+now\b"
    r"|pretend\s+(you\s+are|to\s+be)"
    r"|act\s+as\s+(a\s+|an\s+)?\w+"
    r"|forget\s+(everything|all|your)"
    r"|new\s+(role|persona|instructions?|context)"
    r"|system\s+prompt"
    r"|<\s*(system|instruction|prompt)\s*>"
    r"|roleplay"
    r"|jailbreak"
    r"|\[INST\]"
    r"|<\|[\w\s]+\|>"          # <|im_start|> и подобные
    r"|\{\{.{0,60}\}\}"        # {{template injection}}
    r"|игнорируй\s+инструкции"
    r"|ты\s+теперь\s+\w+"
    r"|притворись\s+(что\s+)?ты",
    re.IGNORECASE,
)


def _quarantine(question: str, answer: str) -> None:
    """Блокирует инъекции и мусор. Бросает HTTPException при нарушении."""
    q, a = question.strip(), answer.strip()
    if len(q) < _MIN_Q:
        raise HTTPException(400, f"question слишком короткий (минимум {_MIN_Q} символов)")
    if len(a) < _MIN_A:
        raise HTTPException(400, f"answer пустой или слишком короткий (минимум {_MIN_A} символов)")
    if len(q) > _MAX_FIELD:
        raise HTTPException(400, f"question превышает лимит {_MAX_FIELD} символов")
    if len(a) > _MAX_FIELD:
        raise HTTPException(400, f"answer превышает лимит {_MAX_FIELD} символов")
    for field_name, val in (("question", q), ("answer", a)):
        m = _INJECTION_RE.search(val)
        if m:
            log.warning("[Thomas quarantine] инъекция в %s: %r", field_name, m.group())
            raise HTTPException(422, f"Заблокировано: подозрительный паттерн в {field_name}")


def _require_token(x_bot_token: str = Header(...)):
    if not THOMAS_TOKEN:
        raise HTTPException(500, "THOMAS_TOKEN not configured on server")
    if x_bot_token != THOMAS_TOKEN:
        raise HTTPException(403, "Forbidden: invalid token")


class RememberIn(BaseModel):
    question: str
    answer: str
    source: str  # обязателен — без метки источника не пишем
    ttl_type: str = "static"


class AskIn(BaseModel):
    question: str
    top_k: int = 5


@router.post("/remember", status_code=201, dependencies=[Depends(_require_token)])
async def thomas_remember(body: RememberIn, session: AsyncSession = Depends(get_session)):
    """Записать факт в личное пространство Томаса. Требует X-Bot-Token."""
    if not body.source.strip():
        raise HTTPException(400, "source is required")

    _quarantine(body.question, body.answer)

    days = TTL_DAYS.get(body.ttl_type)
    expires = datetime.utcnow() + timedelta(days=days) if days else None

    rec = Knowledge(
        question=body.question,
        answer=body.answer,
        source=body.source,
        category="thomas_memory",
        namespace=NAMESPACE,
        ttl_type=body.ttl_type,
        expires_at=expires,
    )
    session.add(rec)
    await session.flush()

    qdrant_id = await store_knowledge(rec.id, body.question, body.answer, "thomas_memory", namespace=NAMESPACE)
    if qdrant_id:
        rec.qdrant_id = qdrant_id
    await session.commit()

    log.info(f"[Thomas] remembered: id={rec.id} source={body.source}")
    return {"id": rec.id, "namespace": NAMESPACE, "source": body.source}


@router.get("/recall")
async def thomas_recall(q: str, limit: int = 5):
    """Семантический поиск только в личном пространстве Томаса. Открыт для чтения."""
    results = await search_knowledge(q, category=None, limit=limit, namespace=NAMESPACE)
    return {"namespace": NAMESPACE, "results": results, "count": len(results)}


@router.post("/ask")
async def thomas_ask(body: AskIn, session: AsyncSession = Depends(get_session)):
    """RAG только из личного пространства Томаса. Открыт для чтения."""
    if not body.question.strip():
        raise HTTPException(400, "question is empty")

    hits = await search_knowledge(body.question, category=None, limit=body.top_k, namespace=NAMESPACE)

    if not hits:
        return {
            "answer": "В личной библиотеке Томаса пока нет данных по этой теме.",
            "sources": [],
            "namespace": NAMESPACE,
            "knowledge_used": 0,
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
            context_parts.append(f"{i}. [source: {rec.source}] Q: {rec.question}\nA: {rec.answer[:600]}")
            sources.append({"id": kid, "score": hit["score"], "source": rec.source})
        else:
            context_parts.append(f"{i}. {hit.get('question', '?')}")

    context = "\n\n".join(context_parts)
    answer = await _ask_claude(body.question, context, system=THOMAS_SYSTEM)

    return {
        "answer": answer,
        "sources": sources,
        "namespace": NAMESPACE,
        "knowledge_used": len(sources),
    }
