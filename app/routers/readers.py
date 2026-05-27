"""
Отдел читателей. Агенты-подписчики с темами и расписанием.
Каждый читатель по расписанию сдаёт темы в /request → Хугин → Карантин → Библиотека.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session, engine
from ..models import Reader

router = APIRouter(prefix="/readers", tags=["readers"])
log = logging.getLogger(__name__)

WYRD_QUARANTINE_URL = os.environ.get(
    "WYRD_QUARANTINE_URL",
    "http://ktup27quru59l1m4wfes69ow.147.45.212.155.sslip.io",
)
WYRD_INTERNAL_TOKEN = os.environ.get("WYRD_INTERNAL_TOKEN", "")


def _huginn_headers() -> dict:
    return {"x-wyrd-token": WYRD_INTERNAL_TOKEN} if WYRD_INTERNAL_TOKEN else {}


class ReaderIn(BaseModel):
    name: str
    topics: list[str]
    category: str = "world"
    interval_hours: int = 24


class ReaderPatch(BaseModel):
    topics: list[str] | None = None
    interval_hours: int | None = None
    enabled: bool | None = None


# ── CRUD ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_reader(body: ReaderIn, session: AsyncSession = Depends(get_session)):
    if not body.topics:
        raise HTTPException(400, "topics cannot be empty")
    reader = Reader(
        name=body.name,
        topics=json.dumps(body.topics, ensure_ascii=False),
        category=body.category,
        interval_hours=body.interval_hours,
    )
    session.add(reader)
    await session.commit()
    await session.refresh(reader)
    return _fmt(reader)


@router.get("")
async def list_readers(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Reader).order_by(Reader.id))).scalars().all()
    return {"readers": [_fmt(r) for r in rows]}


@router.patch("/{rid}")
async def patch_reader(rid: int, body: ReaderPatch, session: AsyncSession = Depends(get_session)):
    reader = await session.get(Reader, rid)
    if not reader:
        raise HTTPException(404, "Reader not found")
    if body.topics is not None:
        reader.topics = json.dumps(body.topics, ensure_ascii=False)
    if body.interval_hours is not None:
        reader.interval_hours = body.interval_hours
    if body.enabled is not None:
        reader.enabled = body.enabled
    await session.commit()
    return _fmt(reader)


@router.delete("/{rid}", status_code=204)
async def delete_reader(rid: int, session: AsyncSession = Depends(get_session)):
    reader = await session.get(Reader, rid)
    if not reader:
        raise HTTPException(404, "Reader not found")
    await session.delete(reader)
    await session.commit()


@router.post("/{rid}/run")
async def run_reader_now(rid: int, session: AsyncSession = Depends(get_session)):
    """Запустить читателя вручную немедленно."""
    reader = await session.get(Reader, rid)
    if not reader:
        raise HTTPException(404, "Reader not found")
    results = await _run_reader(reader)
    reader.last_run = datetime.utcnow()
    reader.runs += 1
    await session.commit()
    return {"reader": reader.name, "results": results}


# ── Планировщик ─────────────────────────────────────────────────────────────

async def reader_scheduler_loop():
    """Фоновый цикл: каждые 5 минут проверяет кто из читателей должен работать."""
    await asyncio.sleep(30)  # дать время сервису подняться
    while True:
        try:
            async with AsyncSession(engine) as session:
                rows = (await session.execute(
                    select(Reader).where(Reader.enabled == True)
                )).scalars().all()

                for reader in rows:
                    due = (
                        reader.last_run is None or
                        reader.last_run + timedelta(hours=reader.interval_hours) <= datetime.utcnow()
                    )
                    if due:
                        log.info("[Readers] %s пора читать (%d тем)", reader.name, len(json.loads(reader.topics)))
                        results = await _run_reader(reader)
                        reader.last_run = datetime.utcnow()
                        reader.runs += 1
                        await session.commit()
                        log.info("[Readers] %s готово: %s", reader.name, results)
        except Exception as e:
            log.error("[Readers] scheduler error: %s", e)
        await asyncio.sleep(300)  # следующая проверка через 5 минут


async def _run_reader(reader: Reader) -> dict:
    """Отправляет темы читателя в Хугин. Возвращает краткий отчёт."""
    topics = json.loads(reader.topics)
    report = {}
    async with httpx.AsyncClient(timeout=45) as client:
        for topic in topics:
            search_url = f"https://html.duckduckgo.com/html/?q={quote(topic)}"
            try:
                resp = await client.post(
                    f"{WYRD_QUARANTINE_URL}/huginn/scout",
                    json={"url": search_url, "task": topic, "category": reader.category},
                    headers=_huginn_headers(),
                )
                data = resp.json() if resp.status_code == 200 else {}
                report[topic[:40]] = data.get("status", f"http_{resp.status_code}")
            except Exception as e:
                report[topic[:40]] = f"error: {e}"
    return report


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt(r: Reader) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "topics": json.loads(r.topics),
        "category": r.category,
        "interval_hours": r.interval_hours,
        "last_run": r.last_run.isoformat() if r.last_run else None,
        "runs": r.runs,
        "enabled": r.enabled,
    }
