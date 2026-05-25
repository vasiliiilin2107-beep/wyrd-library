import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import engine, Base
from .qdrant_store import init_qdrant, close_qdrant
from .hq_adapter import hq_register, hq_event
from .routers import knowledge, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

START_TIME = datetime.utcnow()
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await init_qdrant()
    await hq_register()
    await hq_event("startup", {"service": "library", "version": "0.1.0"})
    yield
    await close_qdrant()


app = FastAPI(title="WYRD Library", version="0.1.0", lifespan=lifespan)

app.include_router(knowledge.router)
app.include_router(request.router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def root():
    p = STATIC_DIR / "library.html"
    if p.exists():
        return FileResponse(str(p))
    return {"message": "WYRD Library v0.1.0"}


@app.get("/health")
def health():
    uptime = (datetime.utcnow() - START_TIME).seconds
    return {
        "status": "ok",
        "service": "wyrd-library",
        "version": "0.1.0",
        "uptime_seconds": uptime,
        "timestamp": datetime.utcnow().isoformat(),
    }
