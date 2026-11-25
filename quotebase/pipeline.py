"""Daily run and backfill: discover -> filter -> fetch -> parse -> extract -> verify -> store."""

from __future__ import annotations

import gzip
import json
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import config, db, logs
from .article import parse
from .config import SETTINGS, Source, load_sections, load_sources
from .discovery import TopicFilter, canonical_url, discover
from .extract import PROMPT_VERSION, propose
from .http import FetchError, Fetcher
from .llm import LLM, LLMError
from .verify import build_quote, quote_id, sha256

log = logs.get("pipeline")


def save_raw(html: str) -> str:
    digest = sha256(html)
    path = config.RAW_DIR / digest[:2] / f"{digest}.html.gz"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(html)
    return digest


def collect(conn, fetcher: Fetcher, sources: list[Source], start: date, end: date, mode: str,
            run_id: int, topic: TopicFilter, stats: Counter) -> None:
    for src in sources:
        db.upsert_source(conn, src)
        cands, errors = discover(fetcher, src, start, end, mode=mode)
        for e in errors:
            db.log_event(conn, run_id, "WARNING", "discovery_error", {"source": src.id, "error": e})
        new = 0
        for c in cands:
            ok, why = topic.headline_match(c)
            created = db.insert_discovered(
                conn, source_id=src.id, url=c.url, title=c.title, published_at=c.published_at,
                summary=c.summary, via=c.via, status="pending" if ok else "skipped_prefilter", detail=why,
            )
            if created:
                new += 1
                stats["discovered"] += 1
                stats["prefilter_pass" if ok else "prefilter_skip"] += 1
        db.log_event(conn, run_id, "INFO", "discovered", {"source": src.id, "candidates": len(cands), "new": new})
        log.info("%s: %d candidates in window, %d new", src.id, len(cands), new)


def process_article(conn, row, src: Source, fetcher: Fetcher, llm: LLM, topic: TopicFilter,
                    window_start: str, run_id: int, stats: Counter) -> None:
    aid = row["id"]
    attempts = row["fetch_attempts"] + (0 if row["status"] == "extract_failed" else 1)

    if row["status"] != "extract_failed" or not row["text"]:
        try:
            res = fetcher.fetch_article(row["url"], row["published_at"])
        except FetchError as e:
            db.set_article(conn, aid, status="fetch_failed", status_detail=e.reason, fetch_attempts=attempts,
                           http_status=e.status)
            db.log_event(conn, run_id, "WARNING", "fetch_failed", {"url": row["url"], "reason": e.reason})
            stats["fetch_failed"] += 1
            return
        html_sha = save_raw(res.text)
        try:
            art = parse(res.text, row["url"], src.boilerplate)
        except Exception as e:
            db.set_article(conn, aid, status="parse_failed", status_detail=str(e), fetch_attempts=attempts)
            stats["parse_failed"] += 1
            return
        published = art.published_at or row["published_at"]
        db.set_article(
            conn, aid,
            fetch_attempts=attempts, fetched_at=db.now_iso(), fetched_via=res.via, http_status=res.status,
            html_sha256=html_sha, text=art.text, text_sha256=sha256(art.text), word_count=art.word_count,
            title=art.title or row["title"], author=art.author,
            canonical_url=canonical_url(art.canonical) if art.canonical else row["url"],
            published_at=published,
            published_at_from=art.published_from or row["published_at_from"],
        )
        stats["fetched"] += 1
        row = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()

    text = row["text"] or ""
    if not text.strip() or row["word_count"] < 60:
        db.set_article(conn, aid, status="no_text", status_detail=f"{row['word_count']} words after cleaning")
        stats["no_text"] += 1
        return
    if not row["published_at"]:
        db.set_article(conn, aid, status="no_date", status_detail="no publication date on page or in feed")
        stats["no_date"] += 1
        return
    if row["published_at"] < window_start:
        db.set_article(conn, aid, status="out_of_range", status_detail=f"published {row['published_at']}")
        stats["out_of_range"] += 1
        return
    dup = conn.execute(
        "SELECT id FROM articles WHERE canonical_url=? AND id<>? AND status='processed'",
        (row["canonical_url"], aid),
    ).fetchone()
    if dup:
        db.set_article(conn, aid, status="duplicate", status_detail=f"same canonical URL as article {dup['id']}")
        stats["duplicate"] += 1
        return
    hits = topic.body_hits(f"{row['title'] or ''}\n{text}")
    db.set_article(conn, aid, body_hits=hits)
    if hits < topic.min_body_hits:
        db.set_article(conn, aid, status="off_topic", status_detail=f"{hits} topic terms in body")
        stats["off_topic"] += 1
        return

    try:
        proposal = propose(llm, title=row["title"] or "", source_name=src.name,
                           published_at=row["published_at"], url=row["url"], text=text)
    except LLMError as e:
        db.set_article(conn, aid, status="extract_failed", status_detail=str(e))
        db.log_event(conn, run_id, "ERROR", "extract_failed", {"url": row["url"], "error": str(e)})
        stats["extract_failed"] += 1
        return

    db.set_article(conn, aid, llm_relevant=int(proposal["relevant"]), llm_reason=proposal["reason"])
    if not proposal["relevant"]:
        db.set_article(conn, aid, status="not_relevant", processed_at=db.now_iso())
        stats["not_relevant"] += 1
        return

    section_ids = {s["id"] for s in load_sections()}
    extracted_at = db.now_iso()
    saved = rejected = 0
    for q in proposal["quotes"]:
        fields, report = build_quote(
            article_text=text, candidate=q.get("text") or "", source=src, url=row["url"], title=row["title"],
            published_at=row["published_at"], section=q.get("section", ""), section_ids=section_ids,
            extracted_at=extracted_at,
        )
        if fields is None:
            conn.execute(
                "INSERT INTO rejected_quotes(article_id, candidate_text, reasons, model, created_at) VALUES (?,?,?,?,?)",
                (aid, q.get("text") or "", json.dumps(report, ensure_ascii=False), llm.model, extracted_at),
            )
            rejected += 1
            continue
        qid = quote_id(src.id, row["url"], fields["quote_text"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO quotes(quote_id, quote_text, source_id, source_name, publication_date, "
            "article_name, article_url, extraction_date, article_id, char_start, char_end, quote_sha256, "
            "article_text_sha256, section, speaker, speaker_role, quote_kind, rationale, model, prompt_version, "
            "verification) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, fields["quote_text"], src.id, src.name, row["published_at"], row["title"], row["url"],
             extracted_at, aid, fields["char_start"], fields["char_end"], fields["quote_sha256"],
             row["text_sha256"], q["section"], q.get("speaker"), q.get("speaker_role"), q.get("kind"),
             q.get("why"), llm.model, PROMPT_VERSION, json.dumps(report["checks"])),
        )
        saved += cur.rowcount
    conn.commit()
    db.set_article(conn, aid, status="processed", processed_at=db.now_iso(),
                   status_detail=f"{saved} quotes saved, {rejected} rejected, {proposal['chunks']} chunk(s)")
    stats["processed"] += 1
    stats["quotes_saved"] += saved
    stats["quotes_rejected"] += rejected
    log.info("%s: %d quotes saved, %d rejected (%s)", row["url"], saved, rejected, src.id)


def process_pending(conn, fetcher: Fetcher, llm: LLM, sources: list[Source], window_start: date,
                    window_end: date | None, run_id: int, stats: Counter, limit: int | None = None) -> None:
    by_id = {s.id: s for s in sources}
    topic = TopicFilter()
    until = (window_end + timedelta(days=1)).isoformat() if window_end else None
    rows = db.pending_articles(conn, SETTINGS.max_fetch_attempts, list(by_id), window_start.isoformat(), until)
    if limit:
        rows = rows[:limit]
    log.info("%d articles to process", len(rows))
    for row in rows:
        try:
            process_article(conn, row, by_id[row["source_id"]], fetcher, llm, topic,
                            window_start.isoformat(), run_id, stats)
        except Exception as e:  # one bad article must not stop the run
            db.set_article(conn, row["id"], status="extract_failed", status_detail=f"{type(e).__name__}: {e}")
            db.log_event(conn, run_id, "ERROR", "article_error",
                         {"url": row["url"], "error": str(e), "trace": traceback.format_exc()[-2000:]})
            log.exception("failed on %s", row["url"])
            stats["errors"] += 1


def _run(kind: str, params: dict, body) -> dict:
    config.ensure_dirs()
    conn = db.connect()
    run_id = db.start_run(conn, kind, params)
    logs.set_run_id(run_id)
    stats: Counter = Counter()
    try:
        body(conn, run_id, stats)
    except Exception as e:
        db.finish_run(conn, run_id, "failed", dict(stats), f"{type(e).__name__}: {e}")
        log.exception("%s run %d failed", kind, run_id)
        raise
    status = "ok" if not stats.get("errors") else "ok_with_errors"
    db.finish_run(conn, run_id, status, dict(stats))
    log.info("%s run %d finished: %s", kind, run_id, dict(stats))
    logs.set_run_id(None)
    return {"run_id": run_id, "status": status, **stats}


def run_daily(today: date | None = None, llm: LLM | None = None, fetcher: Fetcher | None = None,
              source_ids: list[str] | None = None, limit: int | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    start = max(config.DEFAULT_START, today - timedelta(days=SETTINGS.daily_lookback_days))
    sources = [s for s in load_sources() if not source_ids or s.id in source_ids]
    from .llm import default

    def body(conn, run_id, stats):
        f = fetcher or Fetcher()
        m = llm or default("extract")
        collect(conn, f, sources, start, today, "daily", run_id, TopicFilter(), stats)
        # Process everything still pending since the collection start date,
        # which also retries earlier failures.
        process_pending(conn, f, m, sources, config.DEFAULT_START, None, run_id, stats, limit)

    return _run("daily", {"date": today.isoformat(), "from": start.isoformat(),
                          "sources": [s.id for s in sources]}, body)


def run_backfill(start: date, end: date, source_ids: list[str] | None = None, llm: LLM | None = None,
                 fetcher: Fetcher | None = None, limit: int | None = None) -> dict:
    if end < start:
        raise ValueError("end date is before start date")
    sources = [s for s in load_sources() if not source_ids or s.id in source_ids]
    if source_ids and len(sources) != len(set(source_ids)):
        missing = set(source_ids) - {s.id for s in sources}
        raise ValueError(f"unknown or disabled sources: {sorted(missing)}")
    from .llm import default

    def body(conn, run_id, stats):
        f = fetcher or Fetcher()
        m = llm or default("extract")
        collect(conn, f, sources, start, end, "backfill", run_id, TopicFilter(), stats)
        process_pending(conn, f, m, sources, start, end, run_id, stats, limit)

    return _run("backfill", {"from": start.isoformat(), "to": end.isoformat(),
                             "sources": [s.id for s in sources]}, body)


def run_process(llm: LLM | None = None, fetcher: Fetcher | None = None, limit: int | None = None) -> dict:
    sources = load_sources()
    from .llm import default

    def body(conn, run_id, stats):
        process_pending(conn, fetcher or Fetcher(), llm or default("extract"), sources,
                        date(2000, 1, 1), None, run_id, stats, limit)

    return _run("process", {"limit": limit}, body)
