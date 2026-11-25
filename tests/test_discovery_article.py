from datetime import date

from conftest import ART1_PARAS, article_html, mock_fetcher

from quotebase import config
from quotebase.article import parse
from quotebase.discovery import Candidate, TopicFilter, canonical_url, discover, read_feed


def test_canonical_url_strips_tracking():
    u = canonical_url("https://WWW.bbc.co.uk/news/articles/abc/?at_medium=RSS&at_campaign=rss&page=2#top")
    assert u == "https://www.bbc.co.uk/news/articles/abc?page=2"


def test_topic_filter():
    t = TopicFilter()
    assert t.headline_match(Candidate("https://x/a", "Zelensky meets envoys", None, None, ""))[0]
    assert not t.headline_match(Candidate("https://x/a", "Bank of England holds rates", None, None, ""))[0]
    assert not t.headline_match(Candidate("https://x/a", "Ukraine football team qualifies", None, None, ""))[0]


def test_read_feed(home):
    items = read_feed(mock_fetcher(), "https://feeds.bbci.co.uk/news/world/europe/rss.xml")
    assert len(items) == 5
    assert items[0].published_at == "2026-11-20T08:00:00+00:00"


def test_discover_applies_url_rules_and_window(home):
    bbc = [s for s in config.load_sources() if s.id == "bbc"][0]
    cands, errors = discover(mock_fetcher(), bbc, date(2026, 11, 18), date(2026, 11, 25), mode="daily")
    urls = [c.url for c in cands]
    assert "https://www.bbc.co.uk/news/articles/ctest0001" in urls  # tracking params gone
    assert not any("/sport/" in u for u in urls)  # excluded by url_exclude
    assert not any("ctest0004" in u for u in urls)  # September, outside window
    assert errors == []


def test_parse_article_metadata_and_cleaning():
    html = article_html(headline="Test headline", published="2026-11-20T08:00:00+00:00",
                        paragraphs=ART1_PARAS, canonical="https://www.bbc.com/news/articles/ctest0001")
    bbc = [s for s in config.load_sources() if s.id == "bbc"][0]
    a = parse(html, "https://www.bbc.co.uk/news/articles/ctest0001", bbc.boilerplate)
    assert a.title == "Test headline"
    assert a.published_at == "2026-11-20T08:00:00+00:00"
    assert a.canonical == "https://www.bbc.com/news/articles/ctest0001"
    assert "Image caption" not in a.text
    assert "Sign up" not in a.text
    assert "Watch:" not in a.text
    assert "512 drones and 41 missiles" in a.text
    assert "\n\n" in a.text  # paragraph structure kept
