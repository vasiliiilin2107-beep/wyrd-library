"""
Бригадир Библиотеки — Старший в поле ветки знаний.
Каждый час собирает статус Библиотеки: читатели, знания, Писатель, Мусорщики.
Пульсирует в HQ + отправляет событие с метриками.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session, SessionLocal
from ..models import Knowledge, Reader
from ..hq_adapter import hq_register_agent, hq_pulse_agent, hq_event
from . import writer, janitor

router = APIRouter(prefix="/foreman", tags=["foreman"])
log = logging.getLogger(__name__)

FOREMAN_NAME = "Бригадир Библиотеки"
FOREMAN_ROLE = "Старший в поле ветки знаний. Мониторит читателей, рост знаний, Писателя и Мусорщиков."
LOOP_INTERVAL_H = 1

_agent_id: Optional[int] = None
_last_report: Optional[dict] = None


async def _register() -> None:
    global _agent_id
    _agent_id = await hq_register_agent(
        name=FOREMAN_NAME,
        role=FOREMAN_ROLE,
        level="foreman",
        branch="наука",
    )
    if _agent_id:
        log.info("[LibForeman] зарегистрирован в HQ agents id=%d", _agent_id)
    else:
        log.warning("[LibForeman] регистрация в HQ не удалась — пульс недоступен")


async def _gather_stats(session: AsyncSession) -> dict:
    now = datetime.utcnow()
    ago_24h = now - timedelta(hours=24)
    ago_48h = now - timedelta(hours=48)

    # Читатели
    readers_enabled = (await session.execute(
        select(func.count()).select_from(Reader).where(Reader.enabled == True)  # noqa: E712
    )).scalar()
    readers_ran_24h = (await session.execute(
        select(func.count()).select_from(Reader)
        .where(Reader.enabled == True)  # noqa: E712
        .where(Reader.last_run > ago_24h)
    )).scalar()
    readers_stale = (await session.execute(
        select(func.count()).select_from(Reader)
        .where(Reader.enabled == True)  # noqa: E712
        .where((Reader.last_run == None) | (Reader.last_run < ago_48h))  # noqa: E711
    )).scalar()

    # Знания
    total_knowledge = (await session.execute(
        select(func.count()).select_from(Knowledge)
    )).scalar()
    new_today = (await session.execute(
        select(func.count()).select_from(Knowledge).where(Knowledge.created_at > ago_24h)
    )).scalar()
    by_namespace = (await session.execute(
        select(Knowledge.namespace, func.count().label("cnt"))
        .group_by(Knowledge.namespace)
    )).all()

    return {
        "readers": {
            "enabled": readers_enabled,
            "ran_24h": readers_ran_24h,
            "stale": readers_stale,
        },
        "knowledge": {
            "total": total_knowledge,
            "new_24h": new_today,
            "by_namespace": {r.namespace: r.cnt for r in by_namespace},
        },
        "writer": {
            "last_run": writer._last_run.isoformat() if writer._last_run else None,
            "last_stats": writer._last_stats,
        },
        "janitor": {
            "last_run": janitor._last_run.isoformat() if janitor._last_run else None,
            "last_stats": janitor._last_stats,
        },
        "checked_at": now.isoformat(),
    }


async def _run_foreman_cycle() -> dict:
    global _last_report
    async with SessionLocal() as session:
        stats = await _gather_stats(session)

    _last_report = stats

    if _agent_id:
        stale = stats["readers"]["stale"]
        status = "active" if stale > 0 else "idle"
        task = f"стагн. читателей: {stale}" if stale > 0 else None
        await hq_pulse_agent(_agent_id, status=status, current_task=task, metrics={
            "readers_enabled": stats["readers"]["enabled"],
            "readers_stale": stale,
            "knowledge_total": stats["knowledge"]["total"],
            "knowledge_new_24h": stats["knowledge"]["new_24h"],
        })

    await hq_event("foreman_report", {"foreman": FOREMAN_NAME, **stats})
    log.info("[LibForeman] отчёт отправлен: знаний=%d новых_24h=%d стагн_читателей=%d",
             stats["knowledge"]["total"], stats["knowledge"]["new_24h"], stats["readers"]["stale"])
    return stats


async def library_foreman_loop() -> None:
    await asyncio.sleep(90)  # старт через 90с после запуска (после регистрации)
    await _register()
    while True:
        try:
            await _run_foreman_cycle()
        except Exception as e:
            log.error("[LibForeman] ошибка: %s", e)
        await asyncio.sleep(LOOP_INTERVAL_H * 3600)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def status(session: AsyncSession = Depends(get_session)):
    stats = await _gather_stats(session)
    return {
        "agent_id": _agent_id,
        "last_report": _last_report,
        "current": stats,
    }
