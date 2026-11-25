"""Locating a model-proposed passage inside the stored article text.

The model's copy of a passage is never stored. We find where it sits in the
article and store the article's own characters for that span. Matching only
forgives typography (curly vs straight quotes, dash variants, spacing); any
change of words, order or omission makes the match fail."""

from __future__ import annotations

import re
import unicodedata

TYPO_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
    "…": "...",
}


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Returns (normalized, index_map) where index_map[i] is the position in
    `text` of normalized character i. Whitespace runs collapse to one space."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True  # also strips leading whitespace
    for i, ch in enumerate(text):
        rep = TYPO_MAP.get(ch)
        if rep is None:
            rep = unicodedata.normalize("NFKC", ch)
        for r in rep:
            if r.isspace():
                if prev_space:
                    continue
                out.append(" ")
                idx.append(i)
                prev_space = True
            else:
                out.append(r)
                idx.append(i)
                prev_space = False
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


def strip_markdown_markers(s: str) -> str:
    # The model sometimes drops list dashes or heading hashes at line starts.
    return re.sub(r"(?m)^\s*(?:[-*]|#{1,6})\s+", "", s)


def locate(article: str, candidate: str) -> tuple[int, int] | None:
    """Span (start, end) in `article` matching `candidate`, or None."""
    norm_art, amap = normalize_with_map(article)
    for variant in (candidate, candidate.strip().strip('"').strip()):
        norm_c = normalize(variant)
        if len(norm_c) < 10:
            continue
        pos = norm_art.find(norm_c)
        if pos >= 0:
            return _widen_to_marker(article, amap[pos], amap[pos + len(norm_c) - 1] + 1)

    # Second try: ignore list/heading markers on both sides, then map back.
    plain_art = strip_markdown_markers(article)
    if plain_art != article:
        norm_c = normalize(strip_markdown_markers(candidate))
        # rebuild a map from plain_art back to article
        back = []
        i = j = 0
        while i < len(article) and j < len(plain_art):
            if article[i] == plain_art[j]:
                back.append(i)
                i += 1
                j += 1
            else:
                i += 1
        norm_plain, pmap = normalize_with_map(plain_art)
        pos = norm_plain.find(norm_c) if len(norm_c) >= 10 else -1
        if pos >= 0:
            return _widen_to_marker(article, back[pmap[pos]], back[pmap[pos + len(norm_c) - 1]] + 1)
    return None


def _widen_to_marker(article: str, s: int, e: int) -> tuple[int, int]:
    """Include a list marker that sits right before the span on its line."""
    line_start = article.rfind("\n", 0, s) + 1
    if s > line_start and re.fullmatch(r"\s*[-*]\s+", article[line_start:s]):
        s = line_start
    return s, e


SENT_END = re.compile(r'[.!?]["\'”’)]*$')


def snap_to_sentences(article: str, start: int, end: int, max_grow: int = 400) -> tuple[int, int]:
    """Widen a span so it starts at the beginning of a sentence and ends at
    the end of one. Never crosses a blank line (paragraph break)."""
    # start
    s = start
    floor = max(0, start - max_grow)
    para_start = article.rfind("\n\n", 0, start)
    floor = max(floor, para_start + 2 if para_start >= 0 else 0)
    while s > floor:
        before = article[floor:s]
        if re.search(r'[.!?]["\'”’)]*\s+$', before) or article[s - 1] == "\n":
            break
        s -= 1
    # trim leading spaces
    while s < start and article[s].isspace():
        s += 1
    # end
    e = end
    para_end = article.find("\n\n", end)
    ceiling = min(len(article), end + max_grow, para_end if para_end >= 0 else len(article))
    while e < ceiling and not SENT_END.search(article[s:e]):
        e += 1
    # swallow closing quote marks right after the full stop
    while e < len(article) and article[e] in "\"'”’":
        e += 1
    return s, e
