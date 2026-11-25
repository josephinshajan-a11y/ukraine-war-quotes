
import pytest
import yaml
from conftest import mock_fetcher

from quotebase import config
from quotebase.digest import _clean_summary, review_section
from quotebase.extract import chunk_text
from quotebase.llm import FakeLLM
from quotebase.sources_builder import add_source

HOMEPAGE = """<html><head>
<link rel="alternate" type="application/rss+xml" href="/feeds/ukraine.xml">
</head><body><a href="/about">About</a></body></html>"""
FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Test analysis of Russian offensive</title><link>https://thinktank.example.org/research/2026/11/a</link>
<pubDate>Tue, 24 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://thinktank.example.org/sitemap_index.xml\n"


def test_add_source_writes_config(home):
    f = mock_fetcher({
        "https://thinktank.example.org": (200, HOMEPAGE),
        "https://thinktank.example.org/feeds/ukraine.xml": (200, FEED),
        "https://thinktank.example.org/robots.txt": (200, ROBOTS),
    })
    info = add_source("Test Think Tank", "https://thinktank.example.org", kind="think-tank", fetcher=f)
    assert info["id"] == "test-think-tank"
    doc = yaml.safe_load((config.CONFIG_DIR / "sources.d" / "test-think-tank.yaml").read_text())
    assert doc["feeds"] == ["https://thinktank.example.org/feeds/ukraine.xml"]
    assert doc["domains"] == ["thinktank.example.org"]
    assert doc["sitemap_index"] == "https://thinktank.example.org/sitemap_index.xml"
    assert "test-think-tank" in [s.id for s in config.load_sources()]
    with pytest.raises(ValueError):
        add_source("Test Think Tank", "https://thinktank.example.org", fetcher=f)


def _row(qid, day, text, source="BBC News"):
    return {"quote_id": qid, "publication_date": f"2026-11-{day:02d}T10:00:00+00:00", "source_name": source,
            "speaker": None, "quote_text": text}


def test_review_rejects_bad_decisions(home):
    rows = [_row("QA", 15, "Old count of 100."), _row("QB", 20, "New count of 200."), _row("QC", 21, "Other.")]

    def handler(tool, user):
        return {"hide": [
            {"id": "QA", "type": "superseded", "by": "QB", "reason": "updated"},    # valid
            {"id": "QC", "type": "superseded", "by": "QA", "reason": "older"},      # by an older quote: ignored
            {"id": "QX", "type": "duplicate", "by": "QB", "reason": "unknown"},     # unknown id: ignored
            {"id": "QB", "type": "duplicate", "by": "QZ", "reason": "unknown by"},  # unknown target: ignored
        ]}

    out = review_section(FakeLLM(handler), {"title": "T", "scope": "s"}, rows, [])
    assert [(d["id"], d["by"]) for d in out] == [("QA", "QB")]


def test_summary_is_cut_to_two_sentences():
    s = _clean_summary("One thing happened — really.  Two. Three.")
    assert s == "One thing happened, really. Two."


def test_chunking_keeps_all_text_with_overlap():
    blocks = [f"Paragraph {i} " + "word " * 300 for i in range(40)]
    text = "\n\n".join(blocks)
    chunks = chunk_text(text, limit=10000)
    assert len(chunks) > 1
    assert all(len(c) <= 10000 + 2000 for c in chunks)
    for b in blocks:
        assert any(b in c for c in chunks)
