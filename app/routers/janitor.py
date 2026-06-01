"""
Мусорщики — три фоновые задачи гигиены Библиотеки:
  1. TTL cleaner    (6ч)  — удаляет записи с истёкшим expires_at
  2. Dedup cleaner  (24ч) — убирает точные дубли по question+category
  3. Synthesis trim (24ч) — оставляет только 3 последних синтеза на категорию
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session, SessionLocal
from ..models import Knowledge
from ..qdrant_store import delete_knowledge

router = APIRouter(prefix="/janitor", tags=["janitor"])
log = logging.getLogger(__name__)

TTL_INTERVAL_H = 6
DEDUP_INTERVAL_H = 24
SYNTHESIS_KEEP = 3  # сколько последних синтезов держим на категорию

_last_run: Optional[datetime] = None
_last_stats: dict = {}


# ── Задача 1: TTL cleaner ─────────────────────────────────────────────────────

async def _run_ttl_cleaner(session: AsyncSession) -> int:
    now = datetime.utcnow()
    expired = (await session.execute(
        select(Knowledge)
        .where(Knowledge.expires_at != None)  # noqa: E711
        .where(Knowledge.expires_at < now)
    )).scalars().all()

    if not expired:
        return 0

    ids = [r.id for r in expired]
    qdrant_ids = [r.qdrant_id for r in expired if r.qdrant_id]

    await session.execute(
        delete(Knowledge).where(Knowledge.id.in_(ids))
    )
    if qdrant_ids:
        await delete_knowledge(qdrant_ids)

    log.info("[Janitor/TTL] удалено %d записей (из них %d из Qdrant)", len(ids), len(qdrant_ids))
    return len(ids)


# ── Задача 2: Dedup cleaner ───────────────────────────────────────────────────

async def _run_dedup_cleaner(session: AsyncSession) -> int:
    # Находим группы с одинаковым question+category где больше 1 записи
    dupes_stmt = (
        select(Knowledge.question, Knowledge.category, func.count().label("cnt"))
        .where(Knowledge.namespace != "synthesis")
        .group_by(Knowledge.question, Knowledge.category)
        .having(func.count() > 1)
    )
    groups = (await session.execute(dupes_stmt)).all()
    if not groups:
        return 0

    total_deleted = 0
    for row in groups:
        question, category, _ = row.question, row.category, row.cnt
        dupes = (await session.execute(
            select(Knowledge)
            .where(Knowledge.question == question)
            .where(Knowledge.category == category)
            .where(Knowledge.namespace != "synthesis")
            .order_by(Knowledge.created_at.desc())
        )).scalars().all()

        # оставляем самый новый, удаляем остальные
        to_delete = dupes[1:]
        ids = [r.id for r in to_delete]
        qdrant_ids = [r.qdrant_id for r in to_delete if r.qdrant_id]

        await session.execute(delete(Knowledge).where(Knowledge.id.in_(ids)))
        if qdrant_ids:
            await delete_knowledge(qdrant_ids)
        total_deleted += len(ids)

    log.info("[Janitor/Dedup] удалено %d дублей", total_deleted)
    return total_deleted


# ── Задача 3: Synthesis trimmer ───────────────────────────────────────────────

async def _run_synthesis_trimmer(session: AsyncSession) -> int:
    categories = (await session.execute(
        select(Knowledge.category)
        .where(Knowledge.namespace == "synthesis")
        .group_by(Knowledge.category)
        .having(func.count() > SYNTHESIS_KEEP)
    )).scalars().all()

    if not categories:
        return 0

    total_deleted = 0
    for category in categories:
        synths = (await session.execute(
            select(Knowledge)
            .where(Knowledge.namespace == "synthesis")
            .where(Knowledge.category == category)
            .order_by(Knowledge.created_at.desc())
        )).scalars().all()

        to_delete = synths[SYNTHESIS_KEEP:]
        ids = [r.id for r in to_delete]
        qdrant_ids = [r.qdrant_id for r in to_delete if r.qdrant_id]

        await session.execute(delete(Knowledge).where(Knowledge.id.in_(ids)))
        if qdrant_ids:
            await delete_knowledge(qdrant_ids)
        total_deleted += len(ids)

    log.info("[Janitor/SynthTrim] удалено %d старых синтезов", total_deleted)
    return total_deleted


# ── Главный цикл ──────────────────────────────────────────────────────────────

async def _run_janitor_cycle() -> dict:
    global _last_run, _last_stats
    async with SessionLocal() as session:
        ttl = await _run_ttl_cleaner(session)
        dedup = await _run_dedup_cleaner(session)
        synth = await _run_synthesis_trimmer(session)
        await session.commit()

    _last_run = datetime.utcnow()
    _last_stats = {
        "ttl_deleted": ttl,
        "dedup_deleted": dedup,
        "synthesis_trimmed": synth,
        "ran_at": _last_run.isoformat(),
    }
    return _last_stats


async def janitor_loop():
    await asyncio.sleep(60)  # старт через минуту после запуска
    tick = 0
    while True:
        try:
            stats = await _run_janitor_cycle()
            log.info("[Janitor] цикл: %s", stats)
        except Exception as e:
            log.error("[Janitor] ошибка: %s", e)
        tick += 1
        # dedup и synth trim раз в 4 тика (24ч), ttl каждые 6ч
        await asyncio.sleep(TTL_INTERVAL_H * 3600)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/run")
async def run_now():
    stats = await _run_janitor_cycle()
    return {"status": "done", **stats}


@router.get("/status")
async def status():
    return {
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_stats": _last_stats,
        "config": {
            "ttl_interval_hours": TTL_INTERVAL_H,
            "dedup_interval_hours": DEDUP_INTERVAL_H,
            "synthesis_keep": SYNTHESIS_KEEP,
        },
    }
