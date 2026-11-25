"""Shared fixtures. All article text below is invented for tests; it does
not come from any publisher and describes no real event."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from quotebase import config, http

REPO = Path(__file__).resolve().parent.parent

BBC_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>BBC News - Europe</title>
<item><title>Test: Ukraine reports heavy drone attack overnight</title>
<link>https://www.bbc.co.uk/news/articles/ctest0001?at_medium=RSS&amp;at_campaign=rss</link>
<pubDate>Fri, 20 Nov 2026 08:00:00 GMT</pubDate><description>Fixture item.</description></item>
<item><title>Test: Kremlin comments on new peace proposal</title>
<link>https://www.bbc.co.uk/news/articles/ctest0002</link>
<pubDate>Mon, 23 Nov 2026 09:30:00 GMT</pubDate></item>
<item><title>Test: Football club wins cup final</title>
<link>https://www.bbc.co.uk/sport/football/123</link>
<pubDate>Mon, 23 Nov 2026 09:30:00 GMT</pubDate></item>
<item><title>Test: Weather warning for Scotland</title>
<link>https://www.bbc.co.uk/news/articles/ctest0003</link>
<pubDate>Mon, 23 Nov 2026 10:30:00 GMT</pubDate></item>
<item><title>Test: Old Ukraine story from September</title>
<link>https://www.bbc.co.uk/news/articles/ctest0004</link>
<pubDate>Mon, 14 Sep 2026 10:30:00 GMT</pubDate></item>
</channel></rss>"""


def article_html(*, headline: str, published: str, paragraphs: list[str], canonical: str) -> str:
    body = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    return f"""<!doctype html><html><head><title>{headline} - Test</title>
<!-- FICTIONAL TEST FIXTURE -->
<link rel="canonical" href="{canonical}">
<meta property="og:title" content="{headline}">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"NewsArticle",
"headline":"{headline}","datePublished":"{published}"}}</script></head>
<body><nav><a href="/">Home</a> | <a href="/news">News</a></nav>
<main><article><h1>{headline}</h1>
{body}
</article></main><footer>Copyright 2026 Test Corp. All rights reserved.</footer></body></html>"""


ART1_PARAS = [
    "Image caption, Firefighters at the scene in a test city",
    "Ukraine's air force said Russia launched 512 drones and 41 missiles overnight, the largest combined attack recorded in the test month.",
    "Air defence units shot down 460 drones and 33 missiles, the force said in a statement on Friday morning.",
    "“We are seeing the tempo of strikes rise every week,” said Test Commander Ivan Example, who leads an air defence brigade near the capital.",
    "Energy officials said power was cut to about 1.2 million households in four regions after strikes on substations and a thermal power plant.",
    "The attack came a day after talks between envoys in a third country ended without agreement on a ceasefire.",
    "Watch: Test video of the aftermath",
    "Sign up for our test newsletter here",
]

ART2_PARAS = [
    "The Kremlin said on Monday that it was studying a new peace proposal but that any agreement would have to recognise what it called the new territorial realities.",
    "Spokesman Test Speaker told reporters that Moscow saw no basis for a ceasefire before a full settlement was agreed.",
    "Ukraine's foreign minister, Test Minister, said Kyiv would not accept any deal that required it to give up land its forces still held.",
    "European officials said the proposal included security guarantees backed by a group of countries willing to station troops after a ceasefire.",
    "Analysts said the gap between the two sides' positions remained wide and that talks were unlikely to produce a deal before the end of the year.",
]

REUTERS_ART_PARAS = [
    "KYIV, Nov 24 (Test) - Ukraine's air force said on Tuesday that Russia had launched 620 drones overnight, beating the record set four days earlier.",
    "The European Union agreed on a new sanctions package targeting 120 more vessels in Russia's shadow fleet, diplomats said.",
    "Russia's defence ministry said its forces had taken control of two villages in the Donetsk region, a claim Ukraine's military denied.",
    "Reporting by Test Reporter; Editing by Test Editor",
    "Our Standards: The Thomson Reuters Trust Principles.",
]

REUTERS_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://www.reuters.com/world/europe/test-record-drone-attack-2026-11-24/</loc>
<news:news><news:publication><news:name>Reuters</news:name><news:language>eng</news:language></news:publication>
<news:publication_date>2026-11-24T07:10:00Z</news:publication_date>
<news:title>Test: Russia launches record drone attack on Ukraine</news:title></news:news></url>
<url><loc>https://www.reuters.com/markets/test-stocks-close-higher-2026-11-24/</loc>
<news:news><news:publication><news:name>Reuters</news:name><news:language>eng</news:language></news:publication>
<news:publication_date>2026-11-24T21:00:00Z</news:publication_date>
<news:title>Test: Stocks close higher on tech rally</news:title></news:news></url>
</urlset>"""


def routes() -> dict[str, tuple[int, str]]:
    return {
        "https://feeds.bbci.co.uk/news/world/europe/rss.xml": (200, BBC_FEED),
        "https://www.bbc.co.uk/news/articles/ctest0001": (200, article_html(
            headline="Test: Ukraine reports heavy drone attack overnight",
            published="2026-11-20T08:00:00+00:00", paragraphs=ART1_PARAS,
            canonical="https://www.bbc.com/news/articles/ctest0001")),
        "https://www.bbc.co.uk/news/articles/ctest0002": (200, article_html(
            headline="Test: Kremlin comments on new peace proposal",
            published="2026-11-23T09:30:00+00:00", paragraphs=ART2_PARAS,
            canonical="https://www.bbc.com/news/articles/ctest0002")),
        "https://www.reuters.com/arc/outboundfeeds/news-sitemap/?outputType=xml": (200, REUTERS_SITEMAP),
        # Reuters blocks the direct request; the archive copy is served instead.
        "https://www.reuters.com/world/europe/test-record-drone-attack-2026-11-24": (403, "Forbidden"),
        "https://web.archive.org/web/20261124id_/https://www.reuters.com/world/europe/test-record-drone-attack-2026-11-24":
            (200, article_html(headline="Test: Russia launches record drone attack on Ukraine",
                               published="2026-11-24T07:10:00Z", paragraphs=REUTERS_ART_PARAS,
                               canonical="https://www.reuters.com/world/europe/test-record-drone-attack-2026-11-24/")),
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated QUOTEBASE home with only the bbc and reuters sources, GDELT off."""
    cfg = tmp_path / "config"
    shutil.copytree(REPO / "config", cfg)
    for p in (cfg / "sources.d").glob("*.yaml"):
        p.unlink()
    import yaml

    doc = yaml.safe_load((cfg / "sources.yaml").read_text())
    keep = []
    for s in doc["sources"]:
        if s["id"] in ("bbc", "reuters"):
            s["gdelt"] = False
            if s["id"] == "bbc":
                s["feeds"] = ["https://feeds.bbci.co.uk/news/world/europe/rss.xml"]
                s["news_sitemaps"] = []
            keep.append(s)
    doc["sources"] = keep
    (cfg / "sources.yaml").write_text(yaml.safe_dump(doc))

    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "quotes.db")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "data" / "export")
    monkeypatch.setattr(config, "VAULT_DIR", tmp_path / "data" / "vault")
    monkeypatch.setattr(config, "DIGEST_DIR", tmp_path / "digests")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(http, "_sleep", lambda s: None)
    config.ensure_dirs()
    return tmp_path


def mock_fetcher(extra: dict | None = None):
    table = {**routes(), **(extra or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key in (url, url.rstrip("/")):
            if key in table:
                code, body = table[key]
                return httpx.Response(code, text=body)
        return httpx.Response(404, text="not found")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return http.Fetcher(client=client)
