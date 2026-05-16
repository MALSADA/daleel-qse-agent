#!/usr/bin/env python3
"""SQLite schema and CRUD for the news RAG system."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "news.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                url          TEXT UNIQUE NOT NULL,
                url_hash     TEXT UNIQUE NOT NULL,
                content_hash TEXT,
                title        TEXT,
                body         TEXT,
                source       TEXT NOT NULL,
                published_at TEXT,
                scraped_at   TEXT NOT NULL,
                language     TEXT DEFAULT 'en',
                category     TEXT DEFAULT 'general',
                entities     TEXT DEFAULT '[]',
                embedded     INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS scrape_runs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at     TEXT NOT NULL,
                completed_at   TEXT,
                total_articles INTEGER DEFAULT 0,
                new_articles   INTEGER DEFAULT 0,
                errors         TEXT DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS recommendations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT NOT NULL,
                stock_symbol        TEXT NOT NULL,
                stock_name          TEXT,
                recommendation      TEXT NOT NULL,
                sentiment_score     REAL,
                price_direction     TEXT,
                price_prediction_pct REAL,
                justification       TEXT,
                cited_article_ids   TEXT DEFAULT '[]',
                run_date            TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_articles_source   ON articles(source);
            CREATE INDEX IF NOT EXISTS idx_articles_embedded ON articles(embedded);
            CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
            CREATE INDEX IF NOT EXISTS idx_recs_date   ON recommendations(run_date);
            CREATE INDEX IF NOT EXISTS idx_recs_symbol ON recommendations(stock_symbol);
        """)


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def content_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}{body}".encode()).hexdigest()[:16]


def article_exists(url: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE url_hash = ?", (url_hash(url),)
        ).fetchone()
        return row is not None


def insert_article(article: dict):
    """Insert article if not duplicate. Returns new row id or None if skipped."""
    url = article["url"]
    if article_exists(url):
        return None
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO articles
               (url, url_hash, content_hash, title, body, source,
                published_at, scraped_at, language, category, entities)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                url,
                url_hash(url),
                content_hash(article.get("title", ""), article.get("body", "")),
                article.get("title", ""),
                article.get("body", ""),
                article["source"],
                article.get("published_at"),
                now,
                article.get("language", "en"),
                article.get("category", "general"),
                json.dumps(article.get("entities", [])),
            ),
        )
        return cur.lastrowid


def get_unembedded_articles(limit: int = 500) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, body, source, language FROM articles "
            "WHERE embedded = 0 ORDER BY scraped_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_embedded(article_ids: list[int]):
    with get_conn() as conn:
        conn.executemany(
            "UPDATE articles SET embedded = 1 WHERE id = ?",
            [(i,) for i in article_ids],
        )


def get_articles_by_ids(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, title, body, source, published_at, url FROM articles WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return [dict(r) for r in rows]


def save_recommendation(rec: dict):
    now = datetime.now().isoformat(timespec="seconds")
    run_date = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO recommendations
               (created_at, stock_symbol, stock_name, recommendation, sentiment_score,
                price_direction, price_prediction_pct, justification, cited_article_ids, run_date)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                now,
                rec["symbol"],
                rec.get("name", ""),
                rec["recommendation"],
                rec.get("sentiment_score"),
                rec.get("price_direction"),
                rec.get("price_prediction_pct"),
                rec.get("justification", ""),
                json.dumps(rec.get("cited_article_ids", [])),
                run_date,
            ),
        )


def get_today_recommendations() -> list[dict]:
    run_date = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM recommendations WHERE run_date = ? ORDER BY recommendation, sentiment_score DESC",
            (run_date,),
        ).fetchall()
        return [dict(r) for r in rows]


def start_scrape_run() -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scrape_runs (started_at) VALUES (?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        return cur.lastrowid


def finish_scrape_run(run_id: int, total: int, new: int, errors: list[str]):
    with get_conn() as conn:
        conn.execute(
            """UPDATE scrape_runs
               SET completed_at = ?, total_articles = ?, new_articles = ?, errors = ?
               WHERE id = ?""",
            (
                datetime.now().isoformat(timespec="seconds"),
                total,
                new,
                json.dumps(errors),
                run_id,
            ),
        )
