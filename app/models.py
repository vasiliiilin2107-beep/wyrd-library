from datetime import datetime
from sqlalchemy import Integer, String, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base

# category: content | analytics | finance | tech | world | bot_memory
# ttl_type:  static (never) | fresh (30d) | realtime (1d)
# bot level: L1=0-10, L2=11-30, L3=31-100, L4=101-300, L5=300+


def compute_level(tezis_count: int) -> int:
    if tezis_count <= 10:   return 1
    if tezis_count <= 30:   return 2
    if tezis_count <= 100:  return 3
    if tezis_count <= 300:  return 4
    return 5


class BotProfile(Base):
    __tablename__ = "bot_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    start_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    current_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    level: Mapped[int] = mapped_column(Integer, default=1)
    tezis_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class Knowledge(Base):
    __tablename__ = "knowledge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(500), default="direct")
    category: Mapped[str] = mapped_column(String(50), default="world")
    namespace: Mapped[str] = mapped_column(String(50), default="public")
    ttl_type: Mapped[str] = mapped_column(String(20), default="fresh")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[int] = mapped_column(Integer, default=0)
    qdrant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )
