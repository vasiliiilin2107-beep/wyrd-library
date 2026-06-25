"""
Отдел читателей (Хугины). Агенты-подписчики с темами и расписанием.

reader_type=stable  : тема постоянная, каждый прогон LLM генерирует новый угол.
reader_type=oneshot : очередь тем, тема удаляется после прогона. Когда пусто → disabled.

Цепочка: Reader → Карантин /huginn/scout → Библиотека
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session, engine
from ..models import Reader, Knowledge
from ..qdrant_store import store_knowledge
from ..courier import search as courier_search

router = APIRouter(prefix="/readers", tags=["readers"])
log = logging.getLogger(__name__)

WYRD_QUARANTINE_URL = os.environ.get(
    "WYRD_QUARANTINE_URL",
    "http://ktup27quru59l1m4wfes69ow.147.45.212.155.sslip.io",
)
WYRD_INTERNAL_TOKEN = os.environ.get("WYRD_INTERNAL_TOKEN", "")
KIE_API_KEY = os.environ.get("KIE_API_KEY", "")
KIE_API_URL = os.environ.get("KIE_API_URL", "https://polza.ai/api/v1")
KIE_MODEL = os.environ.get("KIE_CHAT_MODEL", "deepseek/deepseek-v4-flash")

# Скилл "Умный читатель" — профессиональный исследователь
SMART_READER_SKILL = """Ты профессиональный исследователь. Твоя задача — сформулировать один точный поисковый запрос.

Правила:
- Запрос должен раскрывать новый угол темы, которого ещё не было в истории поиска
- Ищи свежее: события, исследования, практики 2025-2026 года
- Запрос должен быть конкретным — не "что такое X", а "как X влияет на Y в контексте Z"
- Формулируй как задаёт вопрос эксперт, а не новичок
- Только сам запрос в ответе, без объяснений

Примеры хороших запросов по теме "Стратегическое мышление":
- "cognitive biases strategic planning enterprise 2026"
- "military decision making under uncertainty lessons business"
- "second order thinking failures startups real cases"
"""

MAX_LAST_QUERIES = 20


def _huginn_headers() -> dict:
    return {"x-wyrd-token": WYRD_INTERNAL_TOKEN} if WYRD_INTERNAL_TOKEN else {}


class ReaderIn(BaseModel):
    name: str
    topics: list[str]
    category: str = "world"
    interval_hours: int = 2
    reader_type: str = "stable"  # stable | oneshot


class ReaderPatch(BaseModel):
    topics: list[str] | None = None
    category: str | None = None
    interval_hours: int | None = None
    enabled: bool | None = None
    reader_type: str | None = None


# ── CRUD ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_reader(body: ReaderIn, session: AsyncSession = Depends(get_session)):
    if not body.topics:
        raise HTTPException(400, "topics cannot be empty")
    if body.reader_type not in ("stable", "oneshot"):
        raise HTTPException(400, "reader_type must be stable or oneshot")
    reader = Reader(
        name=body.name,
        topics=json.dumps(body.topics, ensure_ascii=False),
        category=body.category,
        interval_hours=body.interval_hours,
        reader_type=body.reader_type,
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
    if body.category is not None:
        reader.category = body.category
    if body.interval_hours is not None:
        reader.interval_hours = body.interval_hours
    if body.enabled is not None:
        reader.enabled = body.enabled
    if body.reader_type is not None:
        reader.reader_type = body.reader_type
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
    reader = await session.get(Reader, rid)
    if not reader:
        raise HTTPException(404, "Reader not found")
    results = await _run_reader(reader, session)
    reader.last_run = datetime.utcnow()
    reader.runs += 1
    await session.commit()
    return {"reader": reader.name, "type": reader.reader_type, "results": results}


# ── Планировщик ─────────────────────────────────────────────────────────────

async def reader_scheduler_loop():
    await asyncio.sleep(30)
    while True:
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                rows = (await session.execute(
                    select(Reader).where(Reader.enabled == True)
                )).scalars().all()

                for reader in rows:
                    due = (
                        reader.last_run is None or
                        reader.last_run + timedelta(hours=reader.interval_hours) <= datetime.utcnow()
                    )
                    if due:
                        log.info("[Readers] %s (%s) пора читать", reader.name, reader.reader_type)
                        results = await _run_reader(reader, session)
                        reader.last_run = datetime.utcnow()
                        reader.runs += 1
                        await session.commit()
                        log.info("[Readers] %s готово: %s", reader.name, results)
        except Exception as e:
            log.error("[Readers] scheduler error: %s", e)
        await asyncio.sleep(300)


# ── Ядро: запуск читателя ───────────────────────────────────────────────────

async def _run_reader(reader: Reader, session: AsyncSession) -> dict:
    if reader.reader_type == "oneshot":
        return await _run_oneshot(reader, session)
    return await _run_stable(reader, session)


async def _run_stable(reader: Reader, session: AsyncSession) -> dict:
    """Стабильный читатель: LLM генерирует новый угол → Perplexity ищет → сохраняем."""
    topics = json.loads(reader.topics)
    last_queries = json.loads(reader.last_queries or "[]")
    report = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for topic in topics:
            query = await _generate_fresh_query(topic, reader.category, last_queries, client)
            log.info("[Readers] %s | тема='%s' | запрос='%s'", reader.name, topic[:40], query[:60])

            result = await courier_search(query)
            if result["answer"] and len(result["answer"]) > 30:
                source = result["sources"][0] if result["sources"] else "perplexity/sonar"
                rec = Knowledge(
                    question=query,
                    answer=result["answer"],
                    source=source,
                    category=reader.category,
                    namespace="public",
                    ttl_type="fresh",
                    expires_at=datetime.utcnow() + timedelta(days=30),
                )
                session.add(rec)
                await session.flush()
                qdrant_id = await store_knowledge(rec.id, query, result["answer"], reader.category)
                if qdrant_id:
                    rec.qdrant_id = qdrant_id
                report[topic[:40]] = {"query": query[:60], "status": "saved", "chars": len(result["answer"])}
            else:
                report[topic[:40]] = {"query": query[:60], "status": "empty"}

            last_queries.append(query)
            if len(last_queries) > MAX_LAST_QUERIES:
                last_queries = last_queries[-MAX_LAST_QUERIES:]

    reader.last_queries = json.dumps(last_queries, ensure_ascii=False)
    return report


async def _run_oneshot(reader: Reader, session: AsyncSession) -> dict:
    """Одноразовый читатель: берёт первую тему → Perplexity ищет → сохраняем → удаляем тему."""
    topics = json.loads(reader.topics)
    if not topics:
        reader.enabled = False
        log.info("[Readers] %s очередь пуста → выключен", reader.name)
        return {"status": "queue_empty"}

    topic = topics[0]
    log.info("[Readers] %s (oneshot) | тема='%s'", reader.name, topic[:60])

    result = await courier_search(topic)
    if result["answer"] and len(result["answer"]) > 30:
        source = result["sources"][0] if result["sources"] else "perplexity/sonar"
        rec = Knowledge(
            question=topic,
            answer=result["answer"],
            source=source,
            category=reader.category,
            namespace="public",
            ttl_type="fresh",
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(rec)
        await session.flush()
        qdrant_id = await store_knowledge(rec.id, topic, result["answer"], reader.category)
        if qdrant_id:
            rec.qdrant_id = qdrant_id
        status = "saved"
    else:
        status = "empty"

    topics.pop(0)
    reader.topics = json.dumps(topics, ensure_ascii=False)
    if not topics:
        reader.enabled = False
        log.info("[Readers] %s очередь исчерпана → выключен", reader.name)

    return {topic[:40]: {"status": status}}


# ── LLM: генерация свежего угла поиска ─────────────────────────────────────

async def _generate_fresh_query(
    topic: str,
    category: str,
    last_queries: list[str],
    client: httpx.AsyncClient,
) -> str:
    """Умный читатель: LLM придумывает новый угол поиска по теме."""
    if not KIE_API_KEY:
        # Fallback: тема + текущий месяц
        return f"{topic} {datetime.utcnow().strftime('%B %Y')}"

    history = ""
    if last_queries:
        recent = last_queries[-10:]
        history = f"\nУже искали (не повторять):\n" + "\n".join(f"- {q}" for q in recent)

    user_msg = f"Тема: {topic}\nКатегория: {category}{history}\n\nСформулируй один новый поисковый запрос:"

    try:
        resp = await client.post(
            f"{KIE_API_URL}/chat/completions",
            headers={"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": KIE_MODEL,
                "max_tokens": 80,
                "messages": [
                    {"role": "system", "content": SMART_READER_SKILL},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=20,
        )
        resp.raise_for_status()
        query = resp.json()["choices"][0]["message"]["content"].strip()
        # Убираем кавычки если LLM обернул
        query = query.strip('"\'')
        return query if query else f"{topic} {datetime.utcnow().strftime('%B %Y')}"
    except Exception as e:
        log.warning("[Readers] LLM query gen failed: %s — fallback", e)
        return f"{topic} {datetime.utcnow().strftime('%B %Y')}"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt(r: Reader) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "topics": json.loads(r.topics),
        "category": r.category,
        "interval_hours": r.interval_hours,
        "reader_type": r.reader_type,
        "last_queries_count": len(json.loads(r.last_queries or "[]")),
        "last_run": r.last_run.isoformat() if r.last_run else None,
        "runs": r.runs,
        "enabled": r.enabled,
    }
