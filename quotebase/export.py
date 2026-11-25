"""Exports for use outside SQLite.

data/export/quotes.jsonl    one quote per line, all metadata; drop into any LLM context or vector store
data/export/articles.jsonl  processed articles with their cleaned text
data/export/schema.json     field descriptions
data/vault/                 Obsidian vault: one note per quote, one per article, linked
"""

from __future__ import annotations

import json
import re
import shutil

from . import config, db

QUOTE_FIELDS = {
    "quote_id": "Stable ID: 'Q' + first 12 hex chars of sha256(source_id|article_url|quote_text).",
    "quote_text": "Exact text from the article, as stored in articles.text[char_start:char_end].",
    "source_id": "Short source key from config/sources.yaml.",
    "source_name": "Outlet name.",
    "publication_date": "Article publication time, ISO 8601 UTC.",
    "article_name": "Article headline.",
    "article_url": "Link to the original article.",
    "extraction_date": "When the quote was saved, ISO 8601 UTC.",
    "section": "Digest section id (config/sections.yaml).",
    "speaker": "Person or body quoted, if any.",
    "speaker_role": "Their role, if given.",
    "quote_kind": "direct_quote | reported_statement | reporting | analysis | data",
    "rationale": "Why the model proposed the passage.",
    "char_start": "Start offset in the stored article text.",
    "char_end": "End offset in the stored article text.",
    "quote_sha256": "sha256 of quote_text.",
    "article_text_sha256": "sha256 of the stored article text the offsets refer to.",
    "model": "Model that proposed the passage.",
    "prompt_version": "Extraction prompt version.",
    "verification": "Result of every deterministic check (all true for stored quotes).",
}


def _slug(s: str, n: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "untitled"


def _yaml_str(s) -> str:
    return json.dumps("" if s is None else str(s), ensure_ascii=False)


def export_all() -> dict:
    config.ensure_dirs()
    conn = db.connect()
    quotes = conn.execute("SELECT * FROM quotes ORDER BY publication_date, quote_id").fetchall()
    articles = conn.execute(
        "SELECT id, source_id, url, canonical_url, title, published_at, author, fetched_at, fetched_via, "
        "text, text_sha256, word_count, status, status_detail FROM articles WHERE status='processed' "
        "ORDER BY published_at, id"
    ).fetchall()

    with (config.EXPORT_DIR / "quotes.jsonl").open("w", encoding="utf-8") as f:
        for q in quotes:
            d = dict(q)
            d["verification"] = json.loads(d["verification"])
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    with (config.EXPORT_DIR / "articles.jsonl").open("w", encoding="utf-8") as f:
        for a in articles:
            f.write(json.dumps(dict(a), ensure_ascii=False) + "\n")
    (config.EXPORT_DIR / "schema.json").write_text(json.dumps({"quote": QUOTE_FIELDS}, indent=2), encoding="utf-8")

    # Obsidian vault (rebuilt from scratch each time)
    vault = config.VAULT_DIR
    if vault.exists():
        shutil.rmtree(vault)
    (vault / "quotes").mkdir(parents=True)
    (vault / "articles").mkdir(parents=True)
    art_note = {}
    for a in articles:
        name = f"{(a['published_at'] or '')[:10]} {a['source_id']} {_slug(a['title'] or 'untitled')}"
        art_note[a["id"]] = name
    by_article: dict[int, list] = {}
    for q in quotes:
        by_article.setdefault(q["article_id"], []).append(q)
        note = [
            "---",
            f"quote_id: {q['quote_id']}",
            f"source_id: {q['source_id']}",
            f"source_name: {_yaml_str(q['source_name'])}",
            f"publication_date: {q['publication_date']}",
            f"article_name: {_yaml_str(q['article_name'])}",
            f"article_url: {q['article_url']}",
            f"extraction_date: {q['extraction_date']}",
            f"section: {q['section']}",
            f"speaker: {_yaml_str(q['speaker'])}",
            f"tags: [quote, {q['section']}, {q['source_id']}]",
            "---",
            "",
            "\n".join("> " + l if l else ">" for l in q["quote_text"].split("\n")),
            "",
            f"Article: [[{art_note.get(q['article_id'], q['article_id'])}]]",
            "",
        ]
        (vault / "quotes" / f"{q['quote_id']}.md").write_text("\n".join(note), encoding="utf-8")
    for a in articles:
        qs = by_article.get(a["id"], [])
        note = [
            "---",
            f"source_id: {a['source_id']}",
            f"title: {_yaml_str(a['title'])}",
            f"url: {a['url']}",
            f"published_at: {a['published_at']}",
            f"fetched_at: {a['fetched_at']}",
            f"text_sha256: {a['text_sha256']}",
            "tags: [article]",
            "---",
            "",
            f"# {a['title']}",
            "",
            "## Quotes",
            "",
            *[f"- [[{q['quote_id']}]] ({q['section']})" for q in qs],
            "",
            "## Text",
            "",
            a["text"] or "",
            "",
        ]
        (vault / "articles" / f"{art_note[a['id']]}.md").write_text("\n".join(note), encoding="utf-8")
    return {"quotes": len(quotes), "articles": len(articles)}
