import logging
import os

import httpx

log = logging.getLogger(__name__)

KIE_API_URL = os.environ.get("KIE_API_URL", "https://polza.ai/api/v1")
KIE_API_KEY = os.environ.get("KIE_API_KEY", "")
SEARCH_MODEL = "perplexity/sonar"


async def search(query: str) -> dict:
    """Поиск через Perplexity Sonar — реальный интернет через polza.ai."""
    if not KIE_API_KEY:
        log.warning("[Courier] KIE_API_KEY не задан")
        return {"answer": "", "sources": []}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{KIE_API_URL}/chat/completions",
                headers={"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": SEARCH_MODEL,
                    "max_tokens": 1200,
                    "messages": [{"role": "user", "content": query}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            answer = data["choices"][0]["message"]["content"].strip()
            citations = data.get("citations", [])
            log.info("[Courier] got %d chars, %d citations for: %s", len(answer), len(citations), query[:60])
            return {"answer": answer, "sources": citations}
    except Exception as e:
        log.warning("[Courier] search error '%s': %s", query[:60], e)
        return {"answer": "", "sources": []}
