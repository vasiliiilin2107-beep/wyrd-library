import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import BotProfile, compute_level

router = APIRouter(prefix="/bots", tags=["bots"])

LEVEL_NAMES = {1: "Новичок", 2: "Ученик", 3: "Знаток", 4: "Эксперт", 5: "Мастер"}
LEVEL_THRESHOLDS = {1: 10, 2: 30, 3: 100, 4: 300, 5: None}


def _profile_out(p: BotProfile) -> dict:
    level = p.level
    nxt = LEVEL_THRESHOLDS.get(level)
    return {
        "name": p.name,
        "level": level,
        "level_name": LEVEL_NAMES.get(level, "?"),
        "tezis_count": p.tezis_count,
        "next_level_at": nxt,
        "progress_pct": round(p.tezis_count / nxt * 100) if nxt else 100,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


class BotCreateIn(BaseModel):
    name: str
    start_snapshot: dict = {}


class SnapshotUpdateIn(BaseModel):
    snapshot: dict
    tezis_count: Optional[int] = None  # если None — считаем из snapshot["facts"]


@router.post("", status_code=201)
async def create_bot(body: BotCreateIn, session: AsyncSession = Depends(get_session)):
    existing = (await session.execute(select(BotProfile).where(BotProfile.name == body.name))).scalars().first()
    if existing:
        raise HTTPException(409, f"Bot '{body.name}' already exists")
    snap_str = json.dumps(body.start_snapshot, ensure_ascii=False)
    tezis = len(body.start_snapshot.get("facts", []))
    p = BotProfile(
        name=body.name,
        start_snapshot=snap_str,
        current_snapshot=snap_str,
        tezis_count=tezis,
        level=compute_level(tezis),
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return _profile_out(p)


@router.get("")
async def list_bots(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(BotProfile).order_by(BotProfile.level.desc()))).scalars().all()
    return {"bots": [_profile_out(r) for r in rows]}


@router.get("/{name}")
async def get_bot(name: str, session: AsyncSession = Depends(get_session)):
    p = (await session.execute(select(BotProfile).where(BotProfile.name == name))).scalars().first()
    if not p:
        raise HTTPException(404, f"Bot '{name}' not found")
    return _profile_out(p)


@router.put("/{name}/snapshot")
async def update_snapshot(name: str, body: SnapshotUpdateIn, session: AsyncSession = Depends(get_session)):
    p = (await session.execute(select(BotProfile).where(BotProfile.name == name))).scalars().first()
    if not p:
        raise HTTPException(404, f"Bot '{name}' not found")

    old_level = p.level
    old_count = p.tezis_count

    new_snap_str = json.dumps(body.snapshot, ensure_ascii=False)
    tezis = body.tezis_count if body.tezis_count is not None else len(body.snapshot.get("facts", []))

    p.current_snapshot = new_snap_str
    p.tezis_count = tezis
    p.level = compute_level(tezis)
    await session.commit()
    await session.refresh(p)

    leveled_up = p.level > old_level
    return {
        **_profile_out(p),
        "delta_tezis": tezis - old_count,
        "leveled_up": leveled_up,
    }


@router.get("/{name}/growth")
async def bot_growth(name: str, session: AsyncSession = Depends(get_session)):
    p = (await session.execute(select(BotProfile).where(BotProfile.name == name))).scalars().first()
    if not p:
        raise HTTPException(404, f"Bot '{name}' not found")

    try:
        start = json.loads(p.start_snapshot)
        current = json.loads(p.current_snapshot)
    except Exception:
        start, current = {}, {}

    start_facts = set(start.get("facts", []) if isinstance(start.get("facts"), list) else [])
    cur_facts_list = current.get("facts", []) if isinstance(current.get("facts"), list) else []
    cur_facts = set(f.get("fact", f) if isinstance(f, dict) else f for f in cur_facts_list)
    start_facts_set = set(f.get("fact", f) if isinstance(f, dict) else f for f in start.get("facts", []))

    new_facts = [f for f in cur_facts if f not in start_facts_set]

    return {
        "name": name,
        "level": p.level,
        "level_name": LEVEL_NAMES.get(p.level, "?"),
        "start_tezis": len(start_facts_set),
        "current_tezis": p.tezis_count,
        "new_facts_count": len(new_facts),
        "new_facts_sample": new_facts[:10],
        "days_since_creation": (datetime.utcnow() - p.created_at).days,
    }
