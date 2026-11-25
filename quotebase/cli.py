"""Command line entry point: `quotebase <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone

from . import config, db, logs


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _need_key():
    if not config.SETTINGS.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env or export it.")


def cmd_init(a):
    config.ensure_dirs()
    conn = db.connect()
    for s in config.load_sources():
        db.upsert_source(conn, s)
    print(f"database ready at {config.DB_PATH}")


def cmd_daily(a):
    _need_key()
    from .digest import build_digest
    from .export import export_all
    from .pipeline import run_daily

    res = run_daily(a.date, source_ids=a.source, limit=a.limit)
    print(json.dumps(res, indent=2))
    if not a.no_digest:
        print("digest:", build_digest(a.date))
    print("export:", export_all())


def cmd_backfill(a):
    _need_key()
    from .export import export_all
    from .pipeline import run_backfill

    end = a.to or datetime.now(timezone.utc).date()
    res = run_backfill(a.from_, end, a.source, limit=a.limit)
    print(json.dumps(res, indent=2))
    print("export:", export_all())


def cmd_process(a):
    _need_key()
    from .pipeline import run_process

    print(json.dumps(run_process(limit=a.limit), indent=2))


def cmd_digest(a):
    _need_key()
    from .digest import build_digest

    print(build_digest(a.date))


def cmd_export(a):
    from .export import export_all

    print(export_all())


def cmd_add_source(a):
    from .export import export_all
    from .pipeline import run_backfill
    from .sources_builder import add_source

    info = add_source(a.name, a.url, kind=a.kind)
    print(f"added source '{info['id']}' -> {info['path']}")
    print(f"  feeds: {info['feeds'] or 'none found'}")
    print(f"  news sitemaps: {info['news_sitemaps'] or 'none found'}")
    if not info["feeds"] and not info["news_sitemaps"]:
        print("  no feeds found; daily discovery for this source will use GDELT only")
    if not a.no_backfill:
        _need_key()
        end = datetime.now(timezone.utc).date()
        print(json.dumps(run_backfill(a.since, end, [info["id"]]), indent=2))
        print("export:", export_all())


def cmd_verify_db(a):
    from .verify import recheck_stored

    conn = db.connect()
    section_ids = {s["id"] for s in config.load_sections()}
    sources = {s.id: s for s in config.load_sources(include_disabled=True)}
    rows = conn.execute("SELECT q.*, a.text AS article_text FROM quotes q JOIN articles a ON a.id=q.article_id").fetchall()
    bad = 0
    for r in rows:
        problems = recheck_stored(r, r["article_text"], sources[r["source_id"]], section_ids)
        if problems:
            bad += 1
            print(f"{r['quote_id']}: {', '.join(problems)}")
    print(f"{len(rows)} quotes checked, {bad} with problems")
    sys.exit(1 if bad else 0)


def cmd_check_sources(a):
    from .discovery import read_feed, read_sitemap, read_sitemap_index
    from .http import FetchError, Fetcher

    f = Fetcher()
    failures = 0
    for s in config.load_sources(include_disabled=a.all):
        checks = [(u, read_feed) for u in s.feeds] + [(u, read_sitemap) for u in s.news_sitemaps]
        if s.sitemap_index:
            checks.append((s.sitemap_index, lambda f, u: read_sitemap_index(f, u, config.DEFAULT_START, date.today())))
        for url, fn in checks:
            try:
                n = len(fn(f, url))
                status = "ok" if n else "EMPTY"
            except FetchError as e:
                n, status = 0, f"FAIL {e.reason}"
            failures += status != "ok"
            print(f"{s.id:18} {status:6} {n:5}  {url}")
    sys.exit(1 if failures else 0)


def cmd_stats(a):
    conn = db.connect()
    print("articles by status:")
    for r in conn.execute("SELECT source_id, status, COUNT(*) n FROM articles GROUP BY 1,2 ORDER BY 1,2"):
        print(f"  {r['source_id']:18} {r['status']:18} {r['n']}")
    print("quotes by source:")
    for r in conn.execute("SELECT source_id, COUNT(*) n, MIN(publication_date) a, MAX(publication_date) b "
                          "FROM quotes GROUP BY 1"):
        print(f"  {r['source_id']:18} {r['n']:6}  {r['a'][:10]} .. {r['b'][:10]}")
    print("last runs:")
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5"):
        print(f"  #{r['id']} {r['kind']:9} {r['started_at']} {r['status']} {r['stats']}")


def main(argv=None):
    logs.setup()
    p = argparse.ArgumentParser(prog="quotebase", description="Media quotes on Russia's war on Ukraine.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database").set_defaults(fn=cmd_init)

    d = sub.add_parser("daily", help="discover, extract, write digest and exports")
    d.add_argument("--date", type=_date, help="run as if today were this date (YYYY-MM-DD)")
    d.add_argument("--source", action="append", help="limit to a source id (repeatable)")
    d.add_argument("--limit", type=int, help="process at most N articles")
    d.add_argument("--no-digest", action="store_true")
    d.set_defaults(fn=cmd_daily)

    b = sub.add_parser("backfill", help="collect quotes for a past period")
    b.add_argument("--from", dest="from_", type=_date, default=config.DEFAULT_START)
    b.add_argument("--to", type=_date)
    b.add_argument("--source", action="append")
    b.add_argument("--limit", type=int)
    b.set_defaults(fn=cmd_backfill)

    pr = sub.add_parser("process", help="retry pending and failed articles")
    pr.add_argument("--limit", type=int)
    pr.set_defaults(fn=cmd_process)

    g = sub.add_parser("digest", help="write the 14-day Markdown digest")
    g.add_argument("--date", type=_date)
    g.set_defaults(fn=cmd_digest)

    sub.add_parser("export", help="write JSONL and Obsidian exports").set_defaults(fn=cmd_export)

    s = sub.add_parser("add-source", help="add a media or think-tank source and backfill it")
    s.add_argument("--name", required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--kind", default="media", choices=["media", "think-tank"])
    s.add_argument("--since", type=_date, default=config.DEFAULT_START)
    s.add_argument("--no-backfill", action="store_true")
    s.set_defaults(fn=cmd_add_source)

    sub.add_parser("verify-db", help="re-check every stored quote").set_defaults(fn=cmd_verify_db)

    c = sub.add_parser("check-sources", help="test every feed and sitemap")
    c.add_argument("--all", action="store_true", help="include disabled sources")
    c.set_defaults(fn=cmd_check_sources)

    sub.add_parser("stats", help="counts by source and status").set_defaults(fn=cmd_stats)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
