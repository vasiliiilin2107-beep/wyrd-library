import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

_raw_url = os.environ["DATABASE_URL"]
DATABASE_URL = (
    _raw_url
    .replace("postgres://", "postgresql+asyncpg://", 1)
    .replace("postgresql://", "postgresql+asyncpg://", 1)
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
