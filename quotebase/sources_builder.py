"""Adding a new source from its name and homepage.

Finds RSS/Atom feeds and news sitemaps on the site, writes
config/sources.d/<id>.yaml, and (unless told not to) backfills from
1 October 2026."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import feedparser
import yaml
from lxml import html as lhtml

from . import config, logs
from .config import load_sources
from .discovery import read_sitemap
from .http import FetchError, Fetcher

log = logs.get("sources")

COMMON_FEED_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml",
                     "/news/rss", "/rss/news", "/feeds/all.rss"]
DEFAULT_EXCLUDES = ["/video", "/videos/", "/podcast", "/gallery", "/live/", "/tag/", "/tags/", "/author/",
                    "/category/", "/search", "/events?/", "/jobs?/", r"\.pdf$"]


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _feed_ok(fetcher: Fetcher, url: str) -> int:
    try:
        res = fetcher.get(url, retries=1)
    except FetchError:
        return 0
    parsed = feedparser.parse(res.text)
    return len(parsed.entries)


def inspect_site(fetcher: Fetcher, homepage: str) -> dict:
    found_feeds: dict[str, int] = {}
    news_sitemaps: list[str] = []
    sitemaps: list[str] = []
    notes = []

    try:
        res = fetcher.get(homepage)
        tree = lhtml.fromstring(res.text)
        for href in tree.xpath('//link[@rel="alternate"][contains(@type,"rss") or contains(@type,"atom")]/@href'):
            found_feeds.setdefault(urljoin(res.final_url, href), 0)
        for href in tree.xpath('//a[contains(translate(@href,"RSS","rss"),"rss") or contains(@href,"/feed")]/@href')[:20]:
            found_feeds.setdefault(urljoin(res.final_url, href), 0)
    except FetchError as e:
        notes.append(f"homepage: {e.reason}")

    base = f"{urlsplit(homepage).scheme}://{urlsplit(homepage).netloc}"
    for p in COMMON_FEED_PATHS:
        found_feeds.setdefault(base + p, 0)

    try:
        robots = fetcher.get(base + "/robots.txt", retries=1).text
        sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
    except FetchError as e:
        notes.append(f"robots.txt: {e.reason}")
    for sm in sitemaps:
        if "news" in sm.lower():
            try:
                if read_sitemap(fetcher, sm):
                    news_sitemaps.append(sm)
            except FetchError:
                pass

    feeds = []
    for f in found_feeds:
        n = _feed_ok(fetcher, f)
        if n:
            feeds.append(f)
            log.info("feed %s: %d items", f, n)
    # Prefer feeds whose URL mentions the topic, but keep all working ones.
    feeds.sort(key=lambda u: (not re.search(r"ukrain|russia|europe|world", u, re.I), u))
    return {"feeds": feeds[:8], "news_sitemaps": news_sitemaps,
            "sitemap_index": next((s for s in sitemaps if "index" in s.lower()), sitemaps[0] if sitemaps else None),
            "notes": notes}


def add_source(name: str, homepage: str, kind: str = "media", fetcher: Fetcher | None = None) -> dict:
    fetcher = fetcher or Fetcher()
    if not homepage.startswith("http"):
        homepage = "https://" + homepage
    sid = slugify(name)
    if any(s.id == sid for s in load_sources(include_disabled=True)):
        raise ValueError(f"source id '{sid}' already exists")
    host = urlsplit(homepage).netloc.lower()
    domain = host[4:] if host.startswith("www.") else host

    info = inspect_site(fetcher, homepage)
    doc = {
        "id": sid,
        "name": name,
        "kind": kind,
        "homepage": homepage,
        "domains": [domain],
        "feeds": info["feeds"],
        "news_sitemaps": info["news_sitemaps"],
        "sitemap_index": info["sitemap_index"],
        "url_include": [],
        "url_exclude": DEFAULT_EXCLUDES,
        "boilerplate": [],
        "gdelt": True,
        "enabled": True,
        "notes": "Added by `quotebase add-source`. " + "; ".join(info["notes"]),
    }
    path = config.CONFIG_DIR / "sources.d" / f"{sid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    log.info("wrote %s (%d feeds, %d news sitemaps)", path, len(doc["feeds"]), len(doc["news_sitemaps"]))
    return {"id": sid, "path": str(path), **doc}
