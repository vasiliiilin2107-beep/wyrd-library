import logging
import httpx

DDGO_URL = "https://api.duckduckgo.com/"
log = logging.getLogger(__name__)


async def fetch(query: str) -> str:
    """Тупой курьер — не AI, просто скачивает сырьё из DuckDuckGo Instant Answer."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(DDGO_URL, params={
                "q": query, "format": "json", "no_html": "1", "skip_disambig": "1"
            })
            data = r.json()

            abstract = data.get("AbstractText", "")
            if abstract:
                return abstract

            topics = data.get("RelatedTopics", [])
            for t in topics:
                if isinstance(t, dict) and t.get("Text"):
                    return t["Text"]

        return ""
    except Exception as e:
        log.warning(f"[Courier] fetch error for '{query}': {e}")
        return ""
