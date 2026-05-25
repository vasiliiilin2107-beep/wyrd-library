import os
import logging
import asyncio
from typing import Optional

QDRANT_URL = os.environ.get("QDRANT_URL", "http://10.0.1.1:6333")
COLLECTION = "wyrd_library"
VECTOR_SIZE = 384  # BAAI/bge-small-en-v1.5

log = logging.getLogger(__name__)
_client = None
_embedder = None


def _load_embedder():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name="BAAI/bge-small-en-v1.5")


def _embed_sync(text: str, embedder) -> list:
    return list(embedder.embed([text]))[0].tolist()


async def init_qdrant() -> None:
    global _client, _embedder
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import Distance, VectorParams

        _client = AsyncQdrantClient(url=QDRANT_URL)
        collections = await _client.get_collections()
        names = [c.name for c in collections.collections]
        if COLLECTION not in names:
            await _client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            log.info(f"[Qdrant] created collection {COLLECTION}")

        loop = asyncio.get_running_loop()
        _embedder = await loop.run_in_executor(None, _load_embedder)
        log.info(f"[Qdrant] ready: {QDRANT_URL}, collection={COLLECTION}")
    except Exception as e:
        log.warning(f"[Qdrant] init failed (search disabled): {e}")
        _client = None
        _embedder = None


async def close_qdrant() -> None:
    global _client
    if _client:
        await _client.close()
        _client = None


async def store_knowledge(knowledge_id: int, question: str, answer: str, category: str) -> Optional[int]:
    if _client is None or _embedder is None:
        return None
    try:
        from qdrant_client.models import PointStruct

        text = question + " " + answer[:300]
        loop = asyncio.get_running_loop()
        vector = await loop.run_in_executor(None, _embed_sync, text, _embedder)

        await _client.upsert(
            collection_name=COLLECTION,
            points=[PointStruct(
                id=knowledge_id,
                vector=vector,
                payload={"knowledge_id": knowledge_id, "category": category, "question": question},
            )],
        )
        return knowledge_id
    except Exception as e:
        log.warning(f"[Qdrant] store error: {e}")
        return None


async def search_knowledge(query: str, category: Optional[str] = None, limit: int = 5) -> list[dict]:
    if _client is None or _embedder is None:
        return []
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        loop = asyncio.get_running_loop()
        vector = await loop.run_in_executor(None, _embed_sync, query, _embedder)

        query_filter = None
        if category:
            query_filter = Filter(
                must=[FieldCondition(key="category", match=MatchValue(value=category))]
            )

        response = await _client.query_points(
            collection_name=COLLECTION,
            query=vector,
            query_filter=query_filter,
            limit=limit,
        )
        return [{"score": round(p.score, 3), **p.payload} for p in response.points]
    except Exception as e:
        log.warning(f"[Qdrant] search error: {e}")
        return []
