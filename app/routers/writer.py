"""
Писатель — синтез сырых знаний Библиотеки в осмысленные выводы.
Каждые 6ч берёт несинтезированные Q&A записи по категориям,
прогоняет через LLM → сохраняет как namespace=synthesis.
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session, SessionLocal
from ..models import Knowledge
from ..qdrant_store import store_knowledge

router = APIRouter(prefix="/writer", tags=["writer"])
log = logging.getLogger(__name__)

KIE_API_KEY = os.environ.get("KIE_API_KEY", "")
KIE_API_URL = os.environ.get("KIE_API_URL", "https://polza.ai/api/v1")
MODEL = os.environ.get("KIE_CHAT_MODEL", "deepseek/deepseek-v4-flash")

WRITER_INTERVAL_H = 6
BATCH_SIZE = 15      # макс записей на категорию за один прогон
MIN_BATCH = 3        # меньше — не синтезируем, мало данных

_last_run: Optional[datetime] = None
_last_stats: dict = {}


SYSTEM = """Ты Писатель Библиотеки WYRD — аналитик, который превращает сырые факты в выводы.
Тебе дают набор вопросов и ответов по одной теме. Твоя задача:
1. Найди связи, закономерности, противоречия.
2. Напиши один связный аналитический вывод (5-10 предложений).
3. Не повторяй очевидное. Извлеки НЕТРИВИАЛЬНОЕ.
Пиши по-русски, чётко, без вводных фраз типа "Конечно" или "Итак"."""


async def _llm_synthesize(category: str, items: list[dict]) -> Optional[str]:
    if not KIE_API_KEY:
        log.warning("[Writer] нет KIE_API_KEY — синтез пропущен")
        return None

    parts = [f"Q: {it['question']}\nA: {it['answer'][:400]}" for it in items]
    user_msg = f"Категория: {category}\nЗаписей: {len(items)}\n\n" + "\n\n---\n\n".join(parts)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{KIE_API_URL}/chat/completions",
                headers={"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("[Writer] LLM failed for %s: %s", category, e)
        return None


async def _run_writer_cycle() -> dict:
    global _last_run, _last_stats
    synthesized_total = 0
    skipped = []
    errors = []

    async with SessionLocal() as session:
        # Категории с несинтезированными записями
        rows = (await session.execute(
            select(Knowledge.category, func.count().label("cnt"))
            .where(Knowledge.synthesized == False)  # noqa: E712
            .where(Knowledge.namespace != "synthesis")
            .group_by(Knowledge.category)
        )).all()

        for row in rows:
            category, cnt = row.category, row.cnt
            if cnt < MIN_BATCH:
                skipped.append({"category": category, "count": cnt, "reason": f"< {MIN_BATCH}"})
                continue

            items_rows = (await session.execute(
                select(Knowledge)
                .where(Knowledge.category == category)
                .where(Knowledge.synthesized == False)  # noqa: E712
                .where(Knowledge.namespace != "synthesis")
                .order_by(Knowledge.created_at.asc())
                .limit(BATCH_SIZE)
            )).scalars().all()

            if not items_rows:
                continue

            items = [{"question": r.question, "answer": r.answer} for r in items_rows]
            synthesis = await _llm_synthesize(category, items)

            if not synthesis:
                errors.append(category)
                continue

            # Сохраняем синтез
            syn_question = f"[Синтез] {category} — {len(items)} записей от {datetime.utcnow().strftime('%Y-%m-%d')}"
            rec = Knowledge(
                question=syn_question,
                answer=synthesis,
                source="writer_synthesis",
                category=category,
                namespace="synthesis",
                ttl_type="static",
            )
            session.add(rec)
            await session.flush()

            qdrant_id = await store_knowledge(rec.id, syn_question, synthesis, category, namespace="synthesis")
            if qdrant_id:
                rec.qdrant_id = qdrant_id

            # Помечаем оригиналы как синтезированные
            for orig in items_rows:
                orig.synthesized = True

            await session.commit()
            synthesized_total += len(items_rows)
            log.info("[Writer] %s: синтезировано %d записей → id=%d", category, len(items_rows), rec.id)

    _last_run = datetime.utcnow()
    _last_stats = {
        "synthesized_records": synthesized_total,
        "categories_skipped": skipped,
        "categories_error": errors,
        "ran_at": _last_run.isoformat(),
    }
    return _last_stats


async def writer_loop():
    await asyncio.sleep(30)  # старт через 30с после запуска
    while True:
        try:
            stats = await _run_writer_cycle()
            log.info("[Writer] цикл завершён: %s", json.dumps(stats, ensure_ascii=False))
        except Exception as e:
            log.error("[Writer] ошибка цикла: %s", e)
        await asyncio.sleep(WRITER_INTERVAL_H * 3600)


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/run")
async def run_now():
    """Ручной запуск синтеза."""
    stats = await _run_writer_cycle()
    return {"status": "done", **stats}


@router.get("/status")
async def status(session: AsyncSession = Depends(get_session)):
    """Статус Писателя: последний прогон + ожидает синтеза."""
    pending = (await session.execute(
        select(Knowledge.category, func.count().label("cnt"))
        .where(Knowledge.synthesized == False)  # noqa: E712
        .where(Knowledge.namespace != "synthesis")
        .group_by(Knowledge.category)
    )).all()

    synthesis_total = (await session.execute(
        select(func.count()).select_from(Knowledge)
        .where(Knowledge.namespace == "synthesis")
    )).scalar()

    return {
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_stats": _last_stats,
        "pending_by_category": [{"category": r.category, "count": r.cnt} for r in pending],
        "synthesis_records_total": synthesis_total,
        "next_run_in_hours": WRITER_INTERVAL_H,
    }
