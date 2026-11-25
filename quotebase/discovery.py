"""Finding candidate articles. Everything here is rule-based: the same
inputs always give the same list of URLs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
from dateutil import parser as dtparser
from lxml import etree

from . import logs
from .config import Source, load_topic
from .http import FetchError, Fetcher

log = logs.get("discovery")

TRACKING_PARAMS = re.compile(r"^(utm_|at_|ref|cmpid|taid|s_cid|ocid|fbclid|gclid|mc_)", re.I)


@dataclass
class Candidate:
    url: str
    title: str | None
    published_at: str | None  # ISO 8601, UTC
    summary: str | None
    via: str


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, urlencode(query), ""))


def to_utc_iso(value) -> str | None:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, date):
            dt = datetime.combine(value, dtime(0, 0))
        else:
            dt = dtparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# relevance rules ----------------------------------------------------------

class TopicFilter:
    def __init__(self, topic: dict | None = None):
        t = topic or load_topic()
        self.head = re.compile(r"\b(" + "|".join(t["headline_terms"]) + r")\b", re.I)
        self.body = re.compile(r"\b(" + "|".join(t["body_terms"]) + r")\b", re.I)
        self.exclude = [re.compile(p, re.I) for p in t.get("headline_exclude", [])]
        self.min_body_hits = int(t.get("min_body_hits", 3))

    def headline_match(self, c: Candidate) -> tuple[bool, str]:
        hay = " ".join(x for x in (c.title, c.summary, c.url.replace("-", " ")) if x)
        for ex in self.exclude:
            if c.title and ex.search(c.title):
                return False, f"excluded by /{ex.pattern}/"
        m = self.head.search(hay)
        return (True, f"matched '{m.group(0)}'") if m else (False, "no topic term in title/summary/url")

    def body_hits(self, text: str) -> int:
        return len(self.body.findall(text))


# feeds and sitemaps ----------------------------------------------------------

def read_feed(fetcher: Fetcher, feed_url: str) -> list[Candidate]:
    res = fetcher.get(feed_url, accept="application/rss+xml, application/atom+xml, application/xml, text/xml")
    parsed = feedparser.parse(res.text)
    out = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                published = datetime(*e[key][:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
                break
        summary = re.sub(r"<[^>]+>", " ", e.get("summary", "") or "").strip() or None
        out.append(Candidate(link, e.get("title"), published, summary, f"feed:{feed_url}"))
    return out


NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


def _xml(text: str):
    return etree.fromstring(text.encode("utf-8") if isinstance(text, str) else text,
                            parser=etree.XMLParser(recover=True, huge_tree=True))


def read_sitemap(fetcher: Fetcher, sitemap_url: str) -> list[Candidate]:
    res = fetcher.get(sitemap_url, accept="application/xml, text/xml")
    root = _xml(res.text)
    if root is None:
        return []
    out = []
    for u in root.iter("{%s}url" % NS["sm"]):
        loc = u.findtext("sm:loc", namespaces=NS)
        if not loc:
            continue
        title = u.findtext("news:news/news:title", namespaces=NS)
        pub = u.findtext("news:news/news:publication_date", namespaces=NS) or u.findtext("sm:lastmod", namespaces=NS)
        out.append(Candidate(loc.strip(), title, to_utc_iso(pub), None, f"sitemap:{sitemap_url}"))
    return out


def read_sitemap_index(fetcher: Fetcher, index_url: str, start: date, end: date) -> list[str]:
    """Child sitemaps whose <lastmod> falls on or after `start`. Children with
    no lastmod are kept if a date in their URL is inside the window, and
    skipped otherwise."""
    res = fetcher.get(index_url, accept="application/xml, text/xml")
    root = _xml(res.text)
    if root is None:
        return []
    keep = []
    for sm in root.iter("{%s}sitemap" % NS["sm"]):
        loc = sm.findtext("sm:loc", namespaces=NS)
        lastmod = to_utc_iso(sm.findtext("sm:lastmod", namespaces=NS))
        if not loc:
            continue
        if lastmod:
            if lastmod[:10] >= start.isoformat():
                keep.append(loc.strip())
            continue
        m = re.search(r"(20\d\d)[-/_]?(\d\d)[-/_]?(\d\d)?", loc)
        if m:
            d = f"{m.group(1)}-{m.group(2)}-{m.group(3) or '01'}"
            if start.isoformat()[:7] <= d[:7] <= end.isoformat()[:7]:
                keep.append(loc.strip())
    return keep


# GDELT -----------------------------------------------------------------------

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_TERMS = "(ukraine OR ukrainian OR kyiv OR russia OR russian OR kremlin OR putin OR zelensky OR zelenskiy)"
GDELT_MAX = 250


def gdelt_window(fetcher: Fetcher, domain: str, start: datetime, end: datetime, depth: int = 0) -> list[Candidate]:
    params = {
        "query": f"{GDELT_TERMS} domain:{domain}",
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(GDELT_MAX),
        "sort": "DateAsc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }
    url = f"{GDELT_URL}?{urlencode(params)}"
    # GDELT asks clients to keep to one request every five seconds.
    res = fetcher.get(url, accept="application/json", min_delay=6.0, retries=5)
    try:
        data = json.loads(res.text) if res.text.strip() else {}
    except json.JSONDecodeError:
        log.warning("GDELT returned non-JSON for %s: %s", domain, res.text[:200])
        return []
    arts = data.get("articles", []) or []
    # A full page means the window may hold more; split it in half.
    if len(arts) >= GDELT_MAX and depth < 6 and end - start > timedelta(minutes=30):
        mid = start + (end - start) / 2
        return gdelt_window(fetcher, domain, start, mid, depth + 1) + gdelt_window(fetcher, domain, mid, end, depth + 1)
    out = []
    for a in arts:
        if a.get("language") and a["language"].lower() not in ("english", "eng"):
            continue
        out.append(Candidate(a["url"], a.get("title"), to_utc_iso(a.get("seendate")), None, "gdelt"))
    return out


def gdelt_range(fetcher: Fetcher, source: Source, start: date, end: date) -> list[Candidate]:
    out = []
    day = start
    while day <= end:
        s = datetime.combine(day, dtime(0, 0), tzinfo=timezone.utc)
        e = s + timedelta(days=1) - timedelta(seconds=1)
        for domain in source.domains:
            try:
                out.extend(gdelt_window(fetcher, domain, s, e))
            except FetchError as err:
                log.warning("GDELT failed for %s %s: %s", domain, day, err.reason)
        day += timedelta(days=1)
    return out


# per-source discovery ------------------------------------------------------------

def discover(fetcher: Fetcher, source: Source, start: date, end: date, *, mode: str) -> tuple[list[Candidate], list[str]]:
    """mode="daily": feeds + news sitemaps + GDELT for the window.
    mode="backfill": sitemap index + GDELT for the window.
    Returns (candidates in window, list of error strings)."""
    found: list[Candidate] = []
    errors: list[str] = []

    def attempt(label, fn, *args):
        try:
            got = fn(*args)
            log.info("%s %s: %d links", source.id, label, len(got))
            return got
        except FetchError as e:
            errors.append(f"{label}: {e.reason}")
            log.warning("%s %s failed: %s", source.id, label, e.reason)
            return []

    if mode == "daily":
        for f in source.feeds:
            found += attempt(f"feed {f}", read_feed, fetcher, f)
        for sm in source.news_sitemaps:
            found += attempt(f"news-sitemap {sm}", read_sitemap, fetcher, sm)
    elif mode == "backfill" and source.sitemap_index:
        children = attempt("sitemap-index", read_sitemap_index, fetcher, source.sitemap_index, start, end)
        for child in children:
            found += attempt(f"sitemap {child}", read_sitemap, fetcher, child)

    # On daily runs GDELT is only used when the source's own feeds and
    # sitemaps gave nothing (blocked, as Reuters often is, or none set up).
    # Backfills always use it, because feeds only list recent items.
    if source.gdelt and (mode == "backfill" or not found):
        found += gdelt_range(fetcher, source, start, end)

    # de-duplicate, keep the first sighting but fill missing fields from later ones
    by_url: dict[str, Candidate] = {}
    for c in found:
        c.url = canonical_url(c.url)
        prev = by_url.get(c.url)
        if prev is None:
            by_url[c.url] = c
        else:
            prev.title = prev.title or c.title
            prev.summary = prev.summary or c.summary
            prev.published_at = prev.published_at or c.published_at

    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
    in_window = []
    for c in by_url.values():
        if not source.accepts_url(c.url):
            continue
        # Items with no date yet are kept; the article page gives the real date.
        if c.published_at and not (lo <= c.published_at < hi):
            continue
        in_window.append(c)
    in_window.sort(key=lambda c: (c.published_at or "", c.url))
    return in_window, errors
