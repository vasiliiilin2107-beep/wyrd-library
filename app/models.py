from datetime import datetime
from sqlalchemy import Integer, String, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base

# category: content | analytics | finance | tech | world
# ttl_type:  static (never) | fresh (30d) | realtime (1d)


class Knowledge(Base):
    __tablename__ = "knowledge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(500), default="direct")
    category: Mapped[str] = mapped_column(String(50), default="world")
    ttl_type: Mapped[str] = mapped_column(String(20), default="fresh")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[int] = mapped_column(Integer, default=0)
    qdrant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )
