import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .auth import internal_token_middleware
from .database import engine, Base
from .qdrant_store import init_qdrant, close_qdrant
from .hq_adapter import hq_register, hq_event
from .routers import knowledge, request, memory_backup, bots, librarian, thomas, readers, archivist, writer, janitor, foreman

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

START_TIME = datetime.utcnow()
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS namespace VARCHAR(50) DEFAULT 'public'"
        ))
        await conn.execute(text(
            "ALTER TABLE readers ADD COLUMN IF NOT EXISTS reader_type VARCHAR(20) DEFAULT 'stable'"
        ))
        await conn.execute(text(
            "ALTER TABLE readers ADD COLUMN IF NOT EXISTS last_queries TEXT DEFAULT '[]'"
        ))
        await conn.execute(text(
            "ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS synthesized BOOLEAN DEFAULT FALSE"
        ))
        await conn.execute(text(
            "UPDATE readers SET interval_hours = 4 WHERE reader_type = 'stable' AND interval_hours >= 12"
        ))
    await init_qdrant()
    await hq_register()
    await hq_event("startup", {"service": "library", "version": "0.3.0"})
    asyncio.create_task(readers.reader_scheduler_loop())
    asyncio.create_task(writer.writer_loop())
    asyncio.create_task(janitor.janitor_loop())
    asyncio.create_task(foreman.library_foreman_loop())
    yield
    await close_qdrant()


app = FastAPI(title="WYRD Library", version="0.2.0", lifespan=lifespan)
app.middleware("http")(internal_token_middleware)

app.include_router(knowledge.router)
app.include_router(request.router)
app.include_router(memory_backup.router)
app.include_router(bots.router)
app.include_router(librarian.router)
app.include_router(thomas.router)
app.include_router(readers.router)
app.include_router(archivist.router)
app.include_router(writer.router)
app.include_router(janitor.router)
app.include_router(foreman.router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def root():
    p = STATIC_DIR / "library.html"
    if p.exists():
        return FileResponse(str(p))
    return {"message": "WYRD Library v0.2.0"}


@app.get("/health")
def health():
    uptime = (datetime.utcnow() - START_TIME).seconds
    return {
        "status": "ok",
        "service": "wyrd-library",
        "version": "0.2.0",
        "uptime_seconds": uptime,
        "timestamp": datetime.utcnow().isoformat(),
    }
