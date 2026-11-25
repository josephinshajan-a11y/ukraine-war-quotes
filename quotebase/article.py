"""Turning an article page into clean text plus metadata.

The body text keeps the article's block structure: paragraphs are separated
by a blank line, list items keep their "- " prefix, subheadings keep "#".
Quotes are later cut out of this exact string, so whatever is stored here
is what the quote verifier checks against."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import trafilatura
from lxml import html as lhtml

from .discovery import to_utc_iso

GENERIC_BOILERPLATE = [
    r"^advertisement$",
    r"^(read|see) (more|also)\b",
    r"^share (this|on)\b",
    r"^(click|tap) here\b",
    r"^copyright\b",
    r"^©",
    r"^all rights reserved",
    r"^subscribe\b",
    r"^sign up\b",
    r"^follow us\b",
    r"^related:",
    r"^(image|photo|picture)( credit| source)?:",
    r"^\d+\s*$",  # stray page/slide numbers
    r"^\|.*\|$",  # table rows
    r"^[-=_*]{3,}$",
]


@dataclass
class ParsedArticle:
    title: str | None
    published_at: str | None
    published_from: str | None
    author: str | None
    canonical: str | None
    text: str
    word_count: int


def _jsonld_objects(tree) -> list[dict]:
    out = []
    for node in tree.xpath('//script[@type="application/ld+json"]'):
        raw = (node.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                out.append(d)
                if "@graph" in d:
                    stack.append(d["@graph"])
    return out


def _meta(tree, *names) -> str | None:
    for n in names:
        for attr in ("property", "name", "itemprop"):
            vals = tree.xpath(f'//meta[@{attr}="{n}"]/@content')
            if vals and vals[0].strip():
                return vals[0].strip()
    return None


def _published(tree) -> tuple[str | None, str | None]:
    for obj in _jsonld_objects(tree):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x and ("Article" in x or x in ("ReportageNewsArticle", "LiveBlogPosting")) for x in types):
            if obj.get("datePublished"):
                iso = to_utc_iso(obj["datePublished"])
                if iso:
                    return iso, "json-ld"
    m = _meta(tree, "article:published_time", "og:article:published_time", "datePublished",
              "pubdate", "publish-date", "dc.date", "DC.date.issued", "sailthru.date")
    if m and to_utc_iso(m):
        return to_utc_iso(m), "meta"
    times = tree.xpath("//time/@datetime")
    if times and to_utc_iso(times[0]):
        return to_utc_iso(times[0]), "time-tag"
    return None, None


def _title(tree) -> str | None:
    for obj in _jsonld_objects(tree):
        if obj.get("headline") and isinstance(obj["headline"], str):
            return obj["headline"].strip()
    m = _meta(tree, "og:title", "twitter:title")
    if m:
        return m
    h1 = tree.xpath("//h1")
    if h1:
        return " ".join(h1[0].text_content().split())
    t = tree.xpath("//title/text()")
    return t[0].strip() if t else None


def clean_blocks(text: str, extra_patterns: list[str] | None = None, title: str | None = None) -> str:
    pats = [re.compile(p, re.I) for p in GENERIC_BOILERPLATE + list(extra_patterns or [])]
    blocks = []
    for raw in re.split(r"\n\s*\n", text):
        lines = []
        for line in raw.split("\n"):
            s = line.strip()
            if not s:
                continue
            if any(p.search(s) for p in pats):
                continue
            lines.append(re.sub(r"[ \t ]+", " ", s))
        if not lines:
            continue
        block = "\n".join(lines)
        # The headline is stored separately; drop it from the body.
        if title and block.lstrip("# ").strip() == title.strip():
            continue
        blocks.append(block)
    # Drop exact repeated blocks (pull quotes, repeated captions).
    seen, out = set(), []
    for b in blocks:
        if b in seen:
            continue
        seen.add(b)
        out.append(b)
    return "\n\n".join(out)


def parse(html: str, url: str, boilerplate: list[str] | None = None) -> ParsedArticle:
    tree = lhtml.fromstring(html)
    title = _title(tree)
    published, published_from = _published(tree)
    canonical = (tree.xpath('//link[@rel="canonical"]/@href') or [None])[0]

    body = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=False,
        include_images=False,
        include_links=False,
        include_formatting=True,
        favor_precision=True,
        deduplicate=True,
    ) or ""
    if not published:
        meta = trafilatura.extract_metadata(html)
        if meta and meta.date:
            published, published_from = to_utc_iso(meta.date), "trafilatura"
    author = None
    meta_author = _meta(tree, "author", "article:author", "byl")
    if meta_author and not meta_author.startswith("http"):
        author = meta_author

    text = clean_blocks(body, boilerplate, title)
    return ParsedArticle(
        title=title,
        published_at=published,
        published_from=published_from,
        author=author,
        canonical=canonical,
        text=text,
        word_count=len(text.split()),
    )
