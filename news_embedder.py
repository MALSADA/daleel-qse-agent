#!/usr/bin/env python3
"""
Embedding pipeline: sentence-transformers → ChromaDB.
Model: intfloat/multilingual-e5-base (Arabic + English, 768 dims, 512 token limit).

e5 models require specific prefixes for best retrieval quality:
  - Passages stored in the index: "passage: {text}"
  - Queries at search time:       "query: {text}"
"""

import sys
import threading
from pathlib import Path

from typing import Optional

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

from news_db import get_unembedded_articles, mark_embedded

CHROMA_DIR = Path(__file__).parent / "chroma_db"
COLLECTION_NAME = "qse_news"
MODEL_NAME = "intfloat/multilingual-e5-base"
BATCH_SIZE = 16  # e5-base is larger; smaller batches avoid OOM on CPU

_model: Optional[SentenceTransformer] = None
_model_lock = threading.Lock()
_client = None
_collection = None
_collection_lock = threading.Lock()


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                print(f"[embedder] Loading model {MODEL_NAME} (CPU)...", file=sys.stderr)
                # Force CPU so Ollama keeps exclusive GPU access during analysis
                _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def _get_collection():
    global _client, _collection
    if _collection is None:
        with _collection_lock:
            if _collection is None:
                CHROMA_DIR.mkdir(exist_ok=True)
                _client = chromadb.PersistentClient(
                    path=str(CHROMA_DIR),
                    settings=Settings(anonymized_telemetry=False),
                )
                _collection = _client.get_or_create_collection(
                    name=COLLECTION_NAME,
                    metadata={"hnsw:space": "cosine"},
                )
    return _collection


def embed_pending(batch_size: int = BATCH_SIZE) -> int:
    """Embed all articles marked embedded=0. Returns count processed."""
    articles = get_unembedded_articles(limit=500)
    if not articles:
        print("[embedder] No new articles to embed.", file=sys.stderr)
        return 0

    model = _get_model()
    collection = _get_collection()
    total = 0

    for i in range(0, len(articles), batch_size):
        batch = articles[i : i + batch_size]
        # e5 models require "passage: " prefix for indexed documents
        texts = [f"passage: {a['title']} {a['body']}"[:512] for a in batch]
        ids = [str(a["id"]) for a in batch]
        metadatas = [
            {
                "source": a["source"],
                "language": a["language"],
                "article_id": a["id"],
            }
            for a in batch
        ]

        embeddings = model.encode(texts, show_progress_bar=False).tolist()

        # ChromaDB upsert (safe to re-run)
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )

        mark_embedded([a["id"] for a in batch])
        total += len(batch)
        print(f"[embedder] Embedded {total}/{len(articles)}", file=sys.stderr)

    return total


def query(text: str, n_results: int = 10, where: Optional[dict] = None) -> list[dict]:
    """
    Semantic search over embedded articles.
    Returns list of {article_id, document, distance, source, language}.
    """
    model = _get_model()
    collection = _get_collection()

    # e5 models require "query: " prefix for search queries
    embedding = model.encode([f"query: {text}"], show_progress_bar=False).tolist()[0]

    kwargs: dict = {"query_embeddings": [embedding], "n_results": n_results}
    if where:
        kwargs["where"] = where

    try:
        results = collection.query(**kwargs)
    except Exception as e:
        print(f"[embedder] query error: {e}", file=sys.stderr)
        return []

    items = []
    for idx in range(len(results["ids"][0])):
        items.append(
            {
                "article_id": int(results["ids"][0][idx]),
                "document": results["documents"][0][idx],
                "distance": results["distances"][0][idx],
                "source": results["metadatas"][0][idx].get("source"),
                "language": results["metadatas"][0][idx].get("language"),
            }
        )
    return items


def delete_embeddings(article_ids: list[int]):
    """Remove embeddings for the given article IDs from ChromaDB."""
    if not article_ids:
        return
    collection = _get_collection()
    str_ids = [str(i) for i in article_ids]
    try:
        collection.delete(ids=str_ids)
        print(f"[embedder] Deleted {len(str_ids)} embeddings from ChromaDB.", file=sys.stderr)
    except Exception as e:
        print(f"[embedder] Warning: could not delete embeddings: {e}", file=sys.stderr)


def collection_count() -> int:
    return _get_collection().count()


if __name__ == "__main__":
    count = embed_pending()
    print(f"Embedded {count} articles. Collection size: {collection_count()}")
