import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Knowledge
from ..qdrant_store import store_knowledge

router = APIRouter(prefix="/memory-backup", tags=["memory-backup"])


class BackupIn(BaseModel):
    bot_name: str
    memory: dict
    snapshot_type: str = "daily"  # daily | manual | emergency


@router.post("", status_code=201)
async def create_backup(body: BackupIn, session: AsyncSession = Depends(get_session)):
    date_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    question = f"memory_backup:{body.bot_name}:{body.snapshot_type}:{date_str}"
    answer = json.dumps(body.memory, ensure_ascii=False)

    rec = Knowledge(
        question=question,
        answer=answer,
        source=f"bot:{body.bot_name}",
        category="bot_memory",
        ttl_type="static",
        expires_at=None,
    )
    session.add(rec)
    await session.flush()
    await store_knowledge(rec.id, question, answer[:300], "bot_memory")
    await session.commit()
    return {"id": rec.id, "bot": body.bot_name, "snapshot_type": body.snapshot_type}


@router.get("/{bot_name}/latest")
async def get_latest_backup(bot_name: str, session: AsyncSession = Depends(get_session)):
    stmt = (
        select(Knowledge)
        .where(Knowledge.category == "bot_memory")
        .where(Knowledge.source == f"bot:{bot_name}")
        .order_by(desc(Knowledge.created_at))
        .limit(1)
    )
    rec = (await session.execute(stmt)).scalars().first()
    if not rec:
        raise HTTPException(404, f"No backup found for bot '{bot_name}'")
    try:
        memory = json.loads(rec.answer)
    except Exception:
        memory = rec.answer
    return {
        "id": rec.id,
        "bot": bot_name,
        "created_at": rec.created_at.isoformat(),
        "memory": memory,
    }


@router.get("/{bot_name}/history")
async def backup_history(bot_name: str, limit: int = 10, session: AsyncSession = Depends(get_session)):
    stmt = (
        select(Knowledge.id, Knowledge.question, Knowledge.created_at)
        .where(Knowledge.category == "bot_memory")
        .where(Knowledge.source == f"bot:{bot_name}")
        .order_by(desc(Knowledge.created_at))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return {"bot": bot_name, "backups": [
        {"id": r.id, "label": r.question, "created_at": r.created_at.isoformat()}
        for r in rows
    ]}
