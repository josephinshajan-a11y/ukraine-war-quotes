"""SQLite storage. The database is the source of truth; JSONL and the
Obsidian vault in data/ are exports of it."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    homepage    TEXT NOT NULL,
    domains     TEXT NOT NULL,
    first_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           TEXT NOT NULL REFERENCES sources(id),
    url                 TEXT NOT NULL UNIQUE,
    canonical_url       TEXT,
    title               TEXT,
    published_at        TEXT,
    published_at_from   TEXT,
    author              TEXT,
    summary             TEXT,
    discovered_at       TEXT NOT NULL,
    discovered_via      TEXT NOT NULL,
    status              TEXT NOT NULL,
    status_detail       TEXT,
    fetch_attempts      INTEGER NOT NULL DEFAULT 0,
    fetched_at          TEXT,
    fetched_via         TEXT,
    http_status         INTEGER,
    html_sha256         TEXT,
    text                TEXT,
    text_sha256         TEXT,
    word_count          INTEGER,
    body_hits           INTEGER,
    llm_relevant        INTEGER,
    llm_reason          TEXT,
    processed_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS ix_articles_pub ON articles(published_at);
CREATE INDEX IF NOT EXISTS ix_articles_canon ON articles(canonical_url);

CREATE TABLE IF NOT EXISTS quotes (
    quote_id        TEXT PRIMARY KEY,
    quote_text      TEXT NOT NULL,
    source_id       TEXT NOT NULL REFERENCES sources(id),
    source_name     TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    article_name    TEXT NOT NULL,
    article_url     TEXT NOT NULL,
    extraction_date TEXT NOT NULL,
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    quote_sha256    TEXT NOT NULL,
    article_text_sha256 TEXT NOT NULL,
    section         TEXT NOT NULL,
    speaker         TEXT,
    speaker_role    TEXT,
    quote_kind      TEXT,
    rationale       TEXT,
    model           TEXT,
    prompt_version  TEXT,
    verification    TEXT NOT NULL,
    UNIQUE(article_id, quote_sha256)
);
CREATE INDEX IF NOT EXISTS ix_quotes_pub ON quotes(publication_date);
CREATE INDEX IF NOT EXISTS ix_quotes_section ON quotes(section);

CREATE TABLE IF NOT EXISTS rejected_quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    candidate_text  TEXT NOT NULL,
    reasons         TEXT NOT NULL,
    model           TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    params      TEXT,
    stats       TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    event   TEXT NOT NULL,
    data    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS digests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    period_start  TEXT NOT NULL,
    period_end    TEXT NOT NULL,
    path          TEXT NOT NULL,
    decisions     TEXT NOT NULL,
    model         TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_source(conn, source) -> None:
    conn.execute(
        "INSERT INTO sources(id, name, kind, homepage, domains, first_seen) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind, "
        "homepage=excluded.homepage, domains=excluded.domains",
        (source.id, source.name, source.kind, source.homepage, json.dumps(source.domains), now_iso()),
    )
    conn.commit()


def log_event(conn, run_id: int | None, level: str, event: str, data: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events(run_id, ts, level, event, data) VALUES (?,?,?,?,?)",
        (run_id, now_iso(), level, event, json.dumps(data, ensure_ascii=False, default=str) if data else None),
    )
    conn.commit()


def start_run(conn, kind: str, params: dict) -> int:
    cur = conn.execute(
        "INSERT INTO runs(kind, started_at, status, params) VALUES (?,?,?,?)",
        (kind, now_iso(), "running", json.dumps(params, default=str)),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str, stats: dict, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, stats=?, error=? WHERE id=?",
        (now_iso(), status, json.dumps(stats, default=str), error, run_id),
    )
    conn.commit()


def insert_discovered(conn, *, source_id, url, title, published_at, summary, via, status, detail=None) -> bool:
    """Returns True if the URL is new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO articles(source_id, url, title, published_at, published_at_from, summary, "
        "discovered_at, discovered_via, status, status_detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source_id, url, title, published_at, "discovery" if published_at else None, summary,
         now_iso(), via, status, detail),
    )
    conn.commit()
    return cur.rowcount == 1


def set_article(conn, article_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE articles SET {cols} WHERE id=?", (*fields.values(), article_id))
    conn.commit()


def pending_articles(conn, max_attempts: int, source_ids: list[str] | None = None,
                     since: str | None = None, until: str | None = None) -> list[sqlite3.Row]:
    q = ("SELECT * FROM articles WHERE (status='pending' OR (status='fetch_failed' AND fetch_attempts < ?) "
         "OR status='extract_failed')")
    args: list = [max_attempts]
    if source_ids:
        q += f" AND source_id IN ({','.join('?' * len(source_ids))})"
        args += source_ids
    if since:
        q += " AND (published_at IS NULL OR published_at >= ?)"
        args.append(since)
    if until:
        q += " AND (published_at IS NULL OR published_at < ?)"
        args.append(until)
    q += " ORDER BY published_at, id"
    return conn.execute(q, args).fetchall()


def quotes_between(conn, start: str, end_exclusive: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM quotes WHERE publication_date >= ? AND publication_date < ? "
        "ORDER BY publication_date, quote_id",
        (start, end_exclusive),
    ).fetchall()
