"""Daily Markdown digest for the last 14 days.

What the model decides: which quotes are obsolete (superseded by a later
quote, or repeating one) and the 1-2 sentence summary under each section.
What it does not touch: the quote text and metadata, which are copied from
the database by ID. After writing the file we check that every quote shown
appears in it character for character."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config, db, logs
from .config import DIGEST_WINDOW_DAYS, load_sections
from .llm import LLM, LLMError
from .textmatch import normalize

log = logs.get("digest")

# Lower these for providers with small per-request token limits.
BATCH = int(os.environ.get("QUOTEBASE_DIGEST_BATCH", "100"))
REFERENCE_SAME = int(os.environ.get("QUOTEBASE_DIGEST_REF_SAME", "40"))
REFERENCE_OTHER = int(os.environ.get("QUOTEBASE_DIGEST_REF_OTHER", "30"))
SUMMARY_MAX_QUOTES = int(os.environ.get("QUOTEBASE_DIGEST_SUMMARY_QUOTES", "60"))

REVIEW_SYSTEM = """You maintain a rolling two-week briefing of press quotes on Russia's war against Ukraine. For one section of the briefing you get a list of quotes in date order. Your job is to find the quotes that should no longer be shown.

A quote is "superseded" when a later quote in the lists below reports a newer state of the same specific fact. Examples: an earlier casualty or strike count replaced by an updated count; "talks are expected on Tuesday" after a later quote reports how the talks went; "the EU is preparing a sanctions package" after a later quote says the package was adopted or blocked; a forecast about an event that has since happened.

A quote is a "duplicate" when another quote in the list says the same thing with no extra information (often the same wire story run by two outlets). Mark the weaker or less complete one, and point to the one to keep.

Do not mark a quote just because it is older, because it disagrees with another quote, or because you think it is wrong. Opinions, assessments and forecasts stay unless a later quote shows events have overtaken them. When unsure, keep the quote.

Only quotes in the TO REVIEW list can be marked. Quotes in the REFERENCE lists are there so you can see later facts; never mark them."""

SUMMARY_SYSTEM = """You write the one or two sentence lead for a section of a briefing on Russia's war against Ukraine. The lead says what the quotes below, taken together, indicate about the trend or expectations in this area over the period.

Rules:
- One or two sentences, under 60 words in total.
- Say only what the quotes support. Name the direction (intensifying, stalling, growing, weakening, uncertain) when the quotes show one.
- Plain language. No hype, no hedging filler, no phrases like "it is worth noting" or "in conclusion".
- Do not mention quote IDs.

Example of the register wanted: "Commentators highlight facts indicating that the war in Ukraine is intensifying, with record drone launches on both sides and new Russian gains near Pokrovsk.\""""


def _fmt_quote_for_model(r) -> str:
    spk = f" | speaker: {r['speaker']}" if r["speaker"] else ""
    return f"[{r['quote_id']}] {r['publication_date'][:10]} | {r['source_name']}{spk}\n{r['quote_text']}"


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "hide": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ["superseded", "duplicate"]},
                    "by": {"type": "string", "description": "ID of the later or better quote."},
                    "reason": {"type": "string", "description": "One short sentence naming the fact that changed."},
                },
                "required": ["id", "type", "by", "reason"],
            },
        }
    },
    "required": ["hide"],
}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def exact_duplicates(rows) -> tuple[list, list[dict]]:
    """Same text (after typographic normalisation) from more than one article:
    keep the earliest. This step is rule-based."""
    seen: dict[str, str] = {}
    keep, hidden = [], []
    for r in sorted(rows, key=lambda r: (r["publication_date"], r["quote_id"])):
        key = normalize(r["quote_text"]).lower()
        if key in seen:
            hidden.append({"id": r["quote_id"], "type": "duplicate", "by": seen[key],
                           "reason": "identical text already shown", "decided_by": "rule"})
        else:
            seen[key] = r["quote_id"]
            keep.append(r)
    return keep, hidden


def review_section(llm: LLM, section: dict, rows: list, others: list) -> list[dict]:
    """Returns validated hide decisions for `rows` (already date-sorted)."""
    by_id = {r["quote_id"]: r for r in rows}
    other_ref = sorted(others, key=lambda r: r["publication_date"], reverse=True)[:REFERENCE_OTHER]
    all_known = {**by_id, **{r["quote_id"]: r for r in other_ref}}
    decisions: list[dict] = []
    newest = rows[-REFERENCE_SAME:]
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        batch_ids = {r["quote_id"] for r in batch}
        ref_same = [r for r in newest if r["quote_id"] not in batch_ids]
        user = (
            f"Section: {section['title']}\nScope: {' '.join(section['scope'].split())}\n\n"
            "TO REVIEW:\n\n" + "\n\n".join(_fmt_quote_for_model(r) for r in batch)
        )
        if ref_same:
            user += "\n\nREFERENCE (later quotes in this section):\n\n" + "\n\n".join(_fmt_quote_for_model(r) for r in ref_same)
        if other_ref:
            user += "\n\nREFERENCE (recent quotes from other sections):\n\n" + "\n\n".join(_fmt_quote_for_model(r) for r in other_ref)
        out = llm.json_call(system=REVIEW_SYSTEM, user=user, schema=REVIEW_SCHEMA, tool_name="record_review")
        for d in out.get("hide") or []:
            qid, by = d.get("id"), d.get("by")
            if qid not in batch_ids:
                log.warning("review returned unknown or reference id %s; ignored", qid)
                continue
            if by not in all_known or by == qid:
                log.warning("review for %s points to unknown id %s; kept", qid, by)
                continue
            if d.get("type") == "superseded" and all_known[by]["publication_date"] < by_id[qid]["publication_date"]:
                log.warning("%s marked superseded by older quote %s; kept", qid, by)
                continue
            decisions.append({"id": qid, "type": d["type"], "by": by, "reason": d.get("reason", ""),
                              "decided_by": llm.model})
    # A quote hidden as a duplicate of a quote that is itself hidden would
    # leave nothing; in that case keep it.
    hidden = {d["id"] for d in decisions}
    return [d for d in decisions if not (d["type"] == "duplicate" and d["by"] in hidden and d["by"] in by_id)]


def _clean_summary(s: str) -> str:
    s = re.sub(r"\s*\u2014\s*", ", ", s)
    s = " ".join(s.split())
    sentences = re.split(r"(?<=[.!?])\s+", s)
    return " ".join(sentences[:2]).strip()


def summarise(llm: LLM, section: dict, shown: list) -> str:
    recent = sorted(shown, key=lambda r: r["publication_date"], reverse=True)[:SUMMARY_MAX_QUOTES]
    user = f"Section: {section['title']}\n\nQuotes:\n\n" + "\n\n".join(_fmt_quote_for_model(r) for r in recent)
    out = llm.json_call(system=SUMMARY_SYSTEM, user=user, schema=SUMMARY_SCHEMA, tool_name="record_summary",
                        max_tokens=1500)
    return _clean_summary(out.get("summary", ""))


def _blockquote(text: str) -> str:
    return "\n".join(("> " + line) if line.strip() else ">" for line in text.split("\n"))


def _md_link_text(s: str) -> str:
    return s.replace("[", "\\[").replace("]", "\\]")


def _long_date(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


def render(start: date, end: date, sections: list[dict], shown: dict[str, list], summaries: dict[str, str],
           hidden_count: int, generated_at: datetime) -> str:
    total = sum(len(v) for v in shown.values())
    sources = sorted({r["source_name"] for v in shown.values() for r in v})
    out = [
        f"# Russia's war on Ukraine: media quotes, {_long_date(start)} to {_long_date(end)}",
        "",
        f"Period: {start.isoformat()} to {end.isoformat()} (last {DIGEST_WINDOW_DAYS} days). "
        f"Generated {generated_at:%Y-%m-%d %H:%M} UTC.",
        f"{total} quotes from {len(sources)} sources ({', '.join(sources) or 'none'}). "
        f"{hidden_count} older {'quote' if hidden_count == 1 else 'quotes'} left out as superseded or repeated; "
        "see the JSON file with the same name for the reasons.",
        "",
        "Quote text and source details are copied unchanged from the database. "
        "The short summary under each heading is written by a language model.",
        "",
        "## Contents",
        "",
    ]
    for s in sections:
        anchor = re.sub(r"[^a-z0-9 -]", "", s["title"].lower()).replace(" ", "-")
        out.append(f"- [{s['title']}](#{anchor}) ({len(shown.get(s['id'], []))})")
    out.append("")
    for s in sections:
        rows = shown.get(s["id"], [])
        out += [f"## {s['title']}", ""]
        if not rows:
            out += ["No quotes in this period.", ""]
            continue
        if summaries.get(s["id"]):
            out += [f"**Summary (model-written):** {summaries[s['id']]}", ""]
        for r in sorted(rows, key=lambda r: (r["publication_date"], r["quote_id"]), reverse=True):
            meta = [f"Source: {r['source_name']}", f"Published: {r['publication_date'][:10]}"]
            if r["speaker"]:
                meta.append(f"Speaker: {r['speaker']}")
            meta.append(f"Article: [{_md_link_text(r['article_name'])}]({r['article_url']})")
            meta.append(f"ID: `{r['quote_id']}`")
            out += [_blockquote(r["quote_text"]), "", " | ".join(meta), ""]
    return "\n".join(out).rstrip() + "\n"


def verbatim_check(md: str, shown: dict[str, list]) -> list[str]:
    bad = []
    for rows in shown.values():
        for r in rows:
            if _blockquote(r["quote_text"]) not in md or r["article_url"] not in md:
                bad.append(r["quote_id"])
    return bad


def build_digest(today: date | None = None, llm: LLM | None = None, out_dir: Path | None = None) -> Path:
    config.ensure_dirs()
    today = today or datetime.now(timezone.utc).date()
    start = today - timedelta(days=DIGEST_WINDOW_DAYS - 1)
    conn = db.connect()
    run_id = db.start_run(conn, "digest", {"date": today.isoformat()})
    logs.set_run_id(run_id)
    sections = load_sections()
    try:
        rows = db.quotes_between(conn, start.isoformat(), (today + timedelta(days=1)).isoformat())
        rows, hidden = exact_duplicates(rows)
        by_section: dict[str, list] = {s["id"]: [] for s in sections}
        for r in rows:
            by_section.setdefault(r["section"], []).append(r)

        if llm is None and rows:
            from .llm import default
            llm = default("digest")

        shown: dict[str, list] = {}
        summaries: dict[str, str] = {}
        errors = []
        for s in sections:
            sec_rows = by_section.get(s["id"], [])
            if not sec_rows:
                continue
            others = [r for sid, rs in by_section.items() if sid != s["id"] for r in rs]
            try:
                decisions = review_section(llm, s, sec_rows, others)
            except LLMError as e:
                # Without the review we still publish, showing everything.
                errors.append(f"{s['id']} review: {e}")
                decisions = []
            hidden += decisions
            hide_ids = {d["id"] for d in decisions}
            shown[s["id"]] = [r for r in sec_rows if r["quote_id"] not in hide_ids]
            try:
                summaries[s["id"]] = summarise(llm, s, shown[s["id"]]) if shown[s["id"]] else ""
            except LLMError as e:
                errors.append(f"{s['id']} summary: {e}")
                summaries[s["id"]] = ""

        generated = datetime.now(timezone.utc)
        md = render(start, today, sections, shown, summaries, len(hidden), generated)
        bad = verbatim_check(md, shown)
        if bad:
            raise RuntimeError(f"verbatim check failed for {bad}")

        out_dir = out_dir or config.DIGEST_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"digest_{today.isoformat()}.md"
        path.write_text(md, encoding="utf-8")
        (out_dir / "latest.md").write_text(md, encoding="utf-8")
        decisions_doc = {
            "period_start": start.isoformat(),
            "period_end": today.isoformat(),
            "generated_at": generated.isoformat(timespec="seconds"),
            "model": getattr(llm, "model", None),
            "shown": {sid: [r["quote_id"] for r in rs] for sid, rs in shown.items()},
            "hidden": hidden,
            "summaries": summaries,
            "verbatim_check": "passed",
            "errors": errors,
        }
        path.with_suffix(".json").write_text(json.dumps(decisions_doc, indent=2, ensure_ascii=False), encoding="utf-8")
        conn.execute(
            "INSERT INTO digests(created_at, period_start, period_end, path, decisions, model) VALUES (?,?,?,?,?,?)",
            (db.now_iso(), start.isoformat(), today.isoformat(), str(path), json.dumps(decisions_doc),
             getattr(llm, "model", None)),
        )
        conn.commit()
        stats = {"quotes_shown": sum(len(v) for v in shown.values()), "hidden": len(hidden), "errors": len(errors)}
        db.finish_run(conn, run_id, "ok" if not errors else "ok_with_errors", stats)
        log.info("digest written to %s (%s)", path, stats)
        return path
    except Exception as e:
        db.finish_run(conn, run_id, "failed", {}, f"{type(e).__name__}: {e}")
        raise
    finally:
        logs.set_run_id(None)
