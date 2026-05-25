import os
import logging
import httpx

HQ_URL = os.environ.get("WYRD_HQ_URL", "http://m29g5q65uc0vw0r5zku6pukb.147.45.212.155.sslip.io")
BRANCH_NAME = "library"
BRANCH_URL = os.environ.get("LIBRARY_URL", "")

log = logging.getLogger(__name__)


async def hq_register() -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{HQ_URL}/branches/register", json={
                "name": BRANCH_NAME,
                "url": BRANCH_URL,
                "version": "0.1.0",
            })
        log.info("[HQ] registered as 'library'")
    except Exception as e:
        log.warning(f"[HQ] register failed (non-fatal): {e}")


async def hq_event(event_type: str, payload: dict = None) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{HQ_URL}/events", json={
                "branch": BRANCH_NAME,
                "type": event_type,
                "payload": payload or {},
            })
    except Exception as e:
        log.warning(f"[HQ] event failed (non-fatal): {e}")
