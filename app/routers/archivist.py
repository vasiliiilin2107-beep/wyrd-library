"""
Архивариус — инструмент Библиотеки.
Знает весь фонд. Принимает запросы: "есть такое?" → ЕСТЬ / НЕТ / ИЩЕМ.
Если нет → сам добавляет тему в oneshot-читателя нужной категории.
Прямая запись в Библиотеку (namespace=moz) — без Карантина, это чистые внутренние знания.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import ArchivistCheck, Knowledge, Reader
from ..qdrant_store import search_knowledge, store_knowledge

router = APIRouter(prefix="/archivist", tags=["archivist"])
log = logging.getLogger(__name__)

FOUND_THRESHOLD = 0.75   # выше — знание есть
TTL_DAYS = {"static": None, "fresh": 30, "realtime": 1}


# ── Схемы ───────────────────────────────────────────────────────────────────

class CheckIn(BaseModel):
    topic: str
    category: str = "world"
    namespace: str = "public"

class MemorizeIn(BaseModel):
    question: str
    answer: str
    source: str = "moz_session"
    category: str = "wyrd_build"
    namespace: str = "moz"
    ttl_type: str = "static"   # знания Моза не протухают


# ── /archivist/check ────────────────────────────────────────────────────────

@router.post("/check")
async def check(body: CheckIn, session: AsyncSession = Depends(get_session)):
    """
    Проверяет фонд. ЕСТЬ → возвращает топ hits.
    НЕТ → находит подходящего oneshot-читателя → добавляет тему → статус ИЩЕМ.
    """
    if not body.topic.strip():
        raise HTTPException(400, "topic is empty")

    hits = await search_knowledge(body.topic, body.category, limit=3, namespace=body.namespace)
    top_score = hits[0]["score"] if hits else 0.0

    if top_score >= FOUND_THRESHOLD:
        # Знание есть — логируем и возвращаем
        await _log_check(session, body.topic, body.category, body.namespace,
                         status="found", score=top_score)
        return {
            "status": "found",
            "score": top_score,
            "hits": hits,
        }

    # Знания нет — найти oneshot-читателя этой категории и добавить тему
    reader_name = await _assign_to_reader(session, body.topic, body.category)
    await _log_check(session, body.topic, body.category, body.namespace,
                     status="searching", score=top_score, reader=reader_name)

    return {
        "status": "searching",
        "score": top_score,
        "assigned_to": reader_name,
        "message": f"Тема добавлена читателю '{reader_name}' для поиска",
    }


# ── /archivist/memorize ─────────────────────────────────────────────────────

@router.post("/memorize", status_code=201)
async def memorize(body: MemorizeIn, session: AsyncSession = Depends(get_session)):
    """
    Прямая запись внутренних знаний в Библиотеку (namespace=moz по умолчанию).
    Без Карантина — данные уже чистые (сессии Моза, решения Шефа, ключевые моменты).
    """
    days = TTL_DAYS.get(body.ttl_type)
    expires = datetime.utcnow() + timedelta(days=days) if days else None

    rec = Knowledge(
        question=body.question,
        answer=body.answer,
        source=body.source,
        category=body.category,
        namespace=body.namespace,
        ttl_type=body.ttl_type,
        expires_at=expires,
    )
    session.add(rec)
    await session.flush()

    qdrant_id = await store_knowledge(
        rec.id, body.question, body.answer, body.category, namespace=body.namespace
    )
    if qdrant_id:
        rec.qdrant_id = qdrant_id

    await session.commit()
    log.info("[Archivist] memorized: ns=%s cat=%s id=%d", body.namespace, body.category, rec.id)
    return {"id": rec.id, "namespace": body.namespace, "category": body.category}


# ── /archivist/fund ─────────────────────────────────────────────────────────

@router.get("/fund")
async def fund(session: AsyncSession = Depends(get_session)):
    """Статистика фонда: кол-во знаний по категориям и namespace."""
    total = (await session.execute(select(func.count()).select_from(Knowledge))).scalar()

    by_cat = (await session.execute(
        select(Knowledge.category, func.count().label("cnt"))
        .group_by(Knowledge.category)
        .order_by(desc("cnt"))
    )).all()

    by_ns = (await session.execute(
        select(Knowledge.namespace, func.count().label("cnt"))
        .group_by(Knowledge.namespace)
        .order_by(desc("cnt"))
    )).all()

    last_added = (await session.execute(
        select(Knowledge.created_at).order_by(desc(Knowledge.created_at)).limit(1)
    )).scalar()

    readers_total = (await session.execute(select(func.count()).select_from(Reader))).scalar()
    readers_active = (await session.execute(
        select(func.count()).select_from(Reader).where(Reader.enabled == True)
    )).scalar()

    return {
        "total_knowledge": total,
        "last_added": last_added.isoformat() if last_added else None,
        "by_category": [{"category": r.category, "count": r.cnt} for r in by_cat],
        "by_namespace": [{"namespace": r.namespace, "count": r.cnt} for r in by_ns],
        "readers": {"total": readers_total, "active": readers_active},
    }


# ── /archivist/log ──────────────────────────────────────────────────────────

@router.get("/log")
async def log_checks(
    limit: int = 50,
    status: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Лог проверок Архивариуса."""
    stmt = select(ArchivistCheck).order_by(desc(ArchivistCheck.created_at)).limit(limit)
    if status:
        stmt = stmt.where(ArchivistCheck.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return {"checks": [_fmt_check(r) for r in rows]}


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _assign_to_reader(session: AsyncSession, topic: str, category: str) -> str:
    """Находит oneshot-читателя нужной категории → добавляет тему.
    Если нет oneshot → добавляет в первый доступный stable читатель категории как одноразовую тему.
    """
    # Ищем oneshot читателя этой категории
    oneshot = (await session.execute(
        select(Reader)
        .where(Reader.category == category)
        .where(Reader.reader_type == "oneshot")
        .where(Reader.enabled == True)
        .limit(1)
    )).scalar_one_or_none()

    if oneshot:
        topics = json.loads(oneshot.topics)
        if topic not in topics:
            topics.append(topic)
            oneshot.topics = json.dumps(topics, ensure_ascii=False)
            await session.commit()
        return oneshot.name

    # Нет oneshot — создаём новый oneshot-читатель для этой категории
    name = f"archivist_{category}_oneshot"
    existing = (await session.execute(
        select(Reader).where(Reader.name == name)
    )).scalar_one_or_none()

    if existing:
        topics = json.loads(existing.topics)
        if topic not in topics:
            topics.append(topic)
            existing.topics = json.dumps(topics, ensure_ascii=False)
            existing.enabled = True
        await session.commit()
        return name

    new_reader = Reader(
        name=name,
        topics=json.dumps([topic], ensure_ascii=False),
        category=category,
        interval_hours=2,
        reader_type="oneshot",
    )
    session.add(new_reader)
    await session.commit()
    log.info("[Archivist] создан oneshot-читатель: %s", name)
    return name


async def _log_check(
    session: AsyncSession,
    topic: str,
    category: str,
    namespace: str,
    status: str,
    score: float,
    reader: Optional[str] = None,
):
    check = ArchivistCheck(
        topic=topic,
        category=category,
        namespace=namespace,
        status=status,
        score=round(score, 3),
        reader_assigned=reader,
    )
    session.add(check)
    await session.commit()


def _fmt_check(r: ArchivistCheck) -> dict:
    return {
        "id": r.id,
        "topic": r.topic[:100],
        "category": r.category,
        "namespace": r.namespace,
        "status": r.status,
        "score": r.score,
        "reader_assigned": r.reader_assigned,
        "created_at": r.created_at.isoformat(),
    }
