"""Asking the model which passages of an article are worth keeping.

The model only proposes passages. verify.build_quote decides whether each
one is accepted, and the stored text always comes from the article."""

from __future__ import annotations

import os
import re

from .config import load_sections
from .llm import LLM

PROMPT_VERSION = "extract-v1"
# Smaller chunks for providers with low tokens-per-minute limits (Groq free tier: 6000).
CHUNK_CHARS = int(os.environ.get("QUOTEBASE_CHUNK_CHARS", "20000"))

SYSTEM = """You help an analyst who tracks Russia's war against Ukraine. You read one news or think-tank article and pick out passages that the analyst would want in a briefing on where the war is heading.

Keep a passage when it gives one of these:
- a fact or figure about the fighting, the economy, aid, sanctions or talks (territory taken, strike counts, budget numbers, aid amounts, production rates)
- a statement by a named official, commander, negotiator or analyst about intentions, expectations or conditions
- a forecast or assessment of what is likely to happen next
- a decision that changes the situation (a sanctions package adopted, an aid deal signed, a negotiating position stated)

Skip:
- background that any reader of the news already knows, unless it contains a new number
- human-interest detail that says nothing about the course of the war
- passages that only make sense with the sentences around them (a bare "he said" with no named speaker, "this" or "the plan" with no referent)
- captions, bylines, newsletter prompts, related-article links, corrections notes

Copying rules. These are checked by a program, and anything that breaks them is thrown away:
- Copy the passage character for character from the article. Do not fix typos, change punctuation, shorten, or join pieces from different places.
- A passage is one to four whole sentences in a row. It can run over at most two neighbouring paragraphs.
- Do not use "..." to skip text.
- If the speaker is named in the sentence before, include that sentence so the passage stands on its own.

Return every passage that meets the bar. A long article can have many; a weak one can have none. If the article is not about the war at all, set relevant to false and return no passages."""


def _sections_block(sections: list[dict]) -> str:
    return "\n".join(f"- {s['id']}: {s['title']}. {' '.join(s['scope'].split())}" for s in sections)


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    blocks = re.split(r"\n\n", text)
    chunks, cur = [], []
    size = 0
    for b in blocks:
        if cur and size + len(b) + 2 > limit:
            chunks.append("\n\n".join(cur))
            cur = cur[-1:]  # one block of overlap so a passage on the seam is not lost
            size = len(cur[0])
        cur.append(b)
        size += len(b) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def schema(section_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean", "description": "Is the article about Russia's war on Ukraine or its direct consequences?"},
            "reason": {"type": "string", "description": "One sentence."},
            "quotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Exact copy from the article."},
                        "section": {"type": "string", "enum": section_ids},
                        "speaker": {"type": ["string", "null"], "description": "Person or body quoted, null if the passage is the outlet's own reporting."},
                        "speaker_role": {"type": ["string", "null"]},
                        "kind": {"type": "string", "enum": ["direct_quote", "reported_statement", "reporting", "analysis", "data"]},
                        "why": {"type": "string", "description": "What the passage tells the analyst, in a few words."},
                    },
                    "required": ["text", "section", "speaker", "kind", "why"],
                },
            },
        },
        "required": ["relevant", "reason", "quotes"],
    }


def propose(llm: LLM, *, title: str, source_name: str, published_at: str, url: str, text: str) -> dict:
    sections = load_sections()
    ids = [s["id"] for s in sections]
    chunks = chunk_text(text)
    relevant_any = False
    reasons = []
    quotes: list[dict] = []
    seen = set()
    for i, chunk in enumerate(chunks, 1):
        part = f" (part {i} of {len(chunks)})" if len(chunks) > 1 else ""
        user = (
            f"Sections to assign each passage to:\n{_sections_block(sections)}\n\n"
            f"Source: {source_name}\nPublished: {published_at}\nTitle: {title}\nURL: {url}\n\n"
            f"<article{part}>\n{chunk}\n</article>"
        )
        out = llm.json_call(system=SYSTEM, user=user, schema=schema(ids), tool_name="record_passages")
        relevant_any = relevant_any or bool(out.get("relevant"))
        if out.get("reason"):
            reasons.append(out["reason"])
        for q in out.get("quotes") or []:
            key = " ".join((q.get("text") or "").split())
            if key and key not in seen:
                seen.add(key)
                quotes.append(q)
    return {"relevant": relevant_any, "reason": " / ".join(reasons), "quotes": quotes, "chunks": len(chunks)}
