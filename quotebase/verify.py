"""Deterministic checks for every quote before it is saved, and again on
demand for the whole database (`quotebase verify-db`)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from .article import GENERIC_BOILERPLATE
from .textmatch import SENT_END, locate, normalize, snap_to_sentences, strip_markdown_markers

MIN_WORDS = 12
MAX_WORDS = 300
MAX_BLOCKS = 3


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def quote_id(source_id: str, url: str, text: str) -> str:
    return "Q" + sha256(f"{source_id}|{url}|{text}")[:12].upper()


def text_checks(text: str, boilerplate: list[str]) -> dict[str, bool]:
    pats = [re.compile(p, re.I) for p in GENERIC_BOILERPLATE + list(boilerplate)]
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    words = len(text.split())
    first = re.sub(r"^\s*[-*]\s+", "", text.lstrip())[:1]
    return {
        "min_words": words >= MIN_WORDS,
        "max_words": words <= MAX_WORDS,
        "no_boilerplate": not any(p.search(l) for l in lines for p in pats),
        "no_heading_lines": not any(l.startswith("#") for l in lines),
        "no_table_symbols": "|" not in text and "\t" not in text,
        "no_urls": not re.search(r"https?://|www\.", text),
        "starts_cleanly": bool(first) and (first.isupper() or first.isdigit() or first in "\"'“‘("),
        "ends_with_sentence": bool(SENT_END.search(text.rstrip())),
        "max_paragraphs": len(re.split(r"\n\s*\n", text.strip())) <= MAX_BLOCKS,
        "no_ellipsis_splice": "[...]" not in text and "(...)" not in text,
    }


def meta_checks(*, source, url: str, title: str | None, published_at: str | None,
                extracted_at: str, section: str, section_ids: set[str]) -> dict[str, bool]:
    now = datetime.now(timezone.utc)
    pub_ok = False
    if published_at:
        try:
            pub = datetime.fromisoformat(published_at)
            pub_ok = datetime(2000, 1, 1, tzinfo=timezone.utc) <= pub <= now + timedelta(days=1)
        except ValueError:
            pub_ok = False
    return {
        "source_id_present": bool(source.id),
        "source_name_present": bool(source.name),
        "url_https": url.startswith("https://"),
        "url_belongs_to_source": source.owns_url(url),
        "title_present": bool(title and title.strip()),
        "publication_date_valid": pub_ok,
        "extraction_date_present": bool(extracted_at),
        "section_known": section in section_ids,
    }


def build_quote(*, article_text: str, candidate: str, source, url: str, title: str | None,
                published_at: str | None, section: str, section_ids: set[str], extracted_at: str) -> tuple[dict | None, dict]:
    """Returns (quote fields or None, report). The report lists every check
    and, on failure, the reasons."""
    report: dict = {"candidate": candidate}
    span = locate(article_text, candidate)
    if span is None:
        report["reasons"] = ["not_found_verbatim_in_article"]
        report["checks"] = {"found_verbatim": False}
        return None, report

    start, end = snap_to_sentences(article_text, *span)
    text = article_text[start:end]
    grew = (end - start) - (span[1] - span[0])

    checks = {"found_verbatim": True}
    plain = normalize(strip_markdown_markers(text))
    checks["span_round_trip"] = any(
        normalize(strip_markdown_markers(v)) in plain for v in (candidate, candidate.strip().strip('"'))
    )
    checks["sentence_snap_small"] = grew <= 400
    checks.update(text_checks(text, source.boilerplate))
    checks.update(meta_checks(source=source, url=url, title=title, published_at=published_at,
                              extracted_at=extracted_at, section=section, section_ids=section_ids))
    reasons = [k for k, v in checks.items() if not v]
    report.update({"checks": checks, "span": [start, end], "snap_added_chars": grew, "reasons": reasons})
    if reasons:
        return None, report
    return {"quote_text": text, "char_start": start, "char_end": end, "quote_sha256": sha256(text)}, report


def recheck_stored(row, article_text: str, source, section_ids: set[str]) -> list[str]:
    """Re-run checks on a stored quote against its stored article text."""
    problems = []
    if article_text is None:
        return ["article_text_missing"]
    if sha256(article_text) != row["article_text_sha256"]:
        problems.append("article_text_changed")
    if article_text[row["char_start"]:row["char_end"]] != row["quote_text"]:
        problems.append("quote_not_at_recorded_offsets")
    if sha256(row["quote_text"]) != row["quote_sha256"]:
        problems.append("quote_hash_mismatch")
    if quote_id(row["source_id"], row["article_url"], row["quote_text"]) != row["quote_id"]:
        problems.append("quote_id_mismatch")
    checks = text_checks(row["quote_text"], source.boilerplate)
    checks.update(meta_checks(source=source, url=row["article_url"], title=row["article_name"],
                              published_at=row["publication_date"], extracted_at=row["extraction_date"],
                              section=row["section"], section_ids=section_ids))
    problems += [k for k, v in checks.items() if not v]
    return problems
