import json
import re
from datetime import date

from conftest import mock_fetcher

from quotebase import config, db
from quotebase.config import SETTINGS
from quotebase.digest import build_digest
from quotebase.export import export_all
from quotebase.llm import FakeLLM
from quotebase.pipeline import run_daily
from quotebase.verify import recheck_stored


def extract_handler(tool, user):
    assert tool == "record_passages"
    q = []
    if "heavy drone attack" in user:
        q = [
            # straight quotes in the model output; article has curly ones
            {"text": "Ukraine's air force said Russia launched 512 drones and 41 missiles overnight, the largest "
                     "combined attack recorded in the test month.", "section": "war_intensity",
             "speaker": "Ukraine's air force", "kind": "reported_statement", "why": "strike volume"},
            {"text": "\"We are seeing the tempo of strikes rise every week,\" said Test Commander Ivan Example, "
                     "who leads an air defence brigade near the capital.", "section": "war_intensity",
             "speaker": "Ivan Example", "kind": "direct_quote", "why": "trend"},
            {"text": "Energy officials said power was cut to about 1.2 million households in four regions after "
                     "strikes on substations and a thermal power plant.", "section": "energy_infrastructure",
             "speaker": None, "kind": "reporting", "why": "grid damage"},
            # frankenquote: two sentences from different paragraphs glued together
            {"text": "Ukraine's air force said Russia launched 512 drones and 41 missiles overnight. Energy "
                     "officials said power was cut to about 1.2 million households.", "section": "war_intensity",
             "speaker": None, "kind": "reporting", "why": "bad"},
            # one word changed
            {"text": "Air defence units shot down 470 drones and 33 missiles, the force said in a statement on "
                     "Friday morning.", "section": "war_intensity", "speaker": None, "kind": "data", "why": "bad"},
        ]
    elif "peace proposal" in user:
        q = [
            {"text": "The Kremlin said on Monday that it was studying a new peace proposal but that any agreement "
                     "would have to recognise what it called the new territorial realities.",
             "section": "peace_initiatives", "speaker": "Kremlin", "kind": "reported_statement", "why": "position"},
            {"text": "Analysts said the gap between the two sides' positions remained wide and that talks were "
                     "unlikely to produce a deal before the end of the year.", "section": "peace_initiatives",
             "speaker": "Analysts", "kind": "analysis", "why": "outlook"},
        ]
    elif "record drone attack" in user:
        q = [
            {"text": "KYIV, Nov 24 (Test) - Ukraine's air force said on Tuesday that Russia had launched 620 drones "
                     "overnight, beating the record set four days earlier.", "section": "war_intensity",
             "speaker": None, "kind": "reporting", "why": "new record"},
            {"text": "The European Union agreed on a new sanctions package targeting 120 more vessels in Russia's "
                     "shadow fleet, diplomats said.", "section": "sanctions", "speaker": None,
             "kind": "reporting", "why": "sanctions"},
            # boilerplate was cleaned out of the article, so this cannot be found
            {"text": "Reporting by Test Reporter; Editing by Test Editor", "section": "war_intensity",
             "speaker": None, "kind": "reporting", "why": "bad"},
        ]
    return {"relevant": bool(q), "reason": "test", "quotes": q}


def digest_handler(tool, user):
    if tool == "record_summary":
        return {"summary": "Commentators highlight a rising tempo of strikes — both sides report records. "
                           "Drone launches hit new highs. This third sentence must be dropped."}
    ids = re.findall(r"\[(Q[0-9A-F]{12})\] (\S+) \| (\S+)", user)
    hide = []
    if "War Intensity" in user:
        old = [i for i, d, s in ids if s == "BBC" and "512" in user.split(i)[1][:200]]
        new = [i for i, d, s in ids if s == "Reuters" and "620" in user.split(i)[1][:200]]
        if old and new:
            hide.append({"id": old[0], "type": "superseded", "by": new[0], "reason": "new record reported"})
            # invalid: newer quote marked as superseded by an older one -> must be ignored
            hide.append({"id": new[0], "type": "superseded", "by": old[0], "reason": "bad"})
        hide.append({"id": "QDOESNOTEXIST", "type": "duplicate", "by": "Q000", "reason": "bad"})
    return {"hide": hide}


def test_full_pipeline(home, monkeypatch):
    monkeypatch.setattr(SETTINGS, "daily_lookback_days", 7)
    fetcher = mock_fetcher()
    res = run_daily(date(2026, 11, 25), llm=FakeLLM(extract_handler, "fake-extract"), fetcher=fetcher)
    assert res["status"] == "ok"
    assert res["quotes_saved"] == 7
    assert res["quotes_rejected"] == 3

    conn = db.connect()
    statuses = dict(conn.execute("SELECT url, status FROM articles").fetchall())
    assert statuses["https://www.bbc.co.uk/news/articles/ctest0003"] == "skipped_prefilter"  # weather
    assert statuses["https://www.reuters.com/markets/test-stocks-close-higher-2026-11-24"] == "skipped_prefilter"

    reuters = conn.execute("SELECT * FROM articles WHERE source_id='reuters' AND status='processed'").fetchone()
    assert reuters["fetched_via"].startswith("archive:")
    assert reuters["url"].startswith("https://www.reuters.com/")
    assert "Reporting by" not in reuters["text"]

    # every stored quote is the article's own text at the stored offsets
    sections = {s["id"] for s in config.load_sections()}
    sources = {s.id: s for s in config.load_sources()}
    rows = conn.execute("SELECT q.*, a.text AS article_text FROM quotes q JOIN articles a ON a.id=q.article_id").fetchall()
    for r in rows:
        assert recheck_stored(r, r["article_text"], sources[r["source_id"]], sections) == []
        for col in ("quote_id", "quote_text", "source_id", "source_name", "publication_date", "article_name",
                    "article_url", "extraction_date"):
            assert r[col], col
    commander = [r for r in rows if "tempo of strikes" in r["quote_text"]][0]
    assert commander["quote_text"].startswith("“")  # curly quote from the article, not the model's

    rejected = conn.execute("SELECT reasons FROM rejected_quotes").fetchall()
    assert all("not_found_verbatim_in_article" in json.loads(r[0])["reasons"] for r in rejected)

    # tamper with a quote and make sure the re-check catches it
    conn.execute("UPDATE quotes SET quote_text = quote_text || ' extra' WHERE quote_id=?", (commander["quote_id"],))
    conn.commit()
    r = conn.execute("SELECT q.*, a.text AS article_text FROM quotes q JOIN articles a ON a.id=q.article_id "
                     "WHERE quote_id=?", (commander["quote_id"],)).fetchone()
    assert "quote_not_at_recorded_offsets" in recheck_stored(r, r["article_text"], sources["bbc"], sections)
    conn.execute("UPDATE quotes SET quote_text = substr(quote_text, 1, length(quote_text) - 6) WHERE quote_id=?",
                 (commander["quote_id"],))
    conn.commit()

    # digest
    path = build_digest(date(2026, 11, 25), llm=FakeLLM(digest_handler, "fake-digest"))
    md = path.read_text()
    side = json.loads(path.with_suffix(".json").read_text())
    assert md.startswith("# Russia's war on Ukraine: media quotes, 12 November 2026 to 25 November 2026")
    assert "512 drones and 41 missiles" not in md  # superseded by the 620 record
    assert "620 drones" in md
    assert "“We are seeing the tempo of strikes rise every week,”" in md
    assert "This third sentence" not in md
    assert "—" not in md.split("## Contents")[1].split("**Summary")[1].split("\n")[0]
    assert len([h for h in side["hidden"] if h["type"] == "superseded"]) == 1
    assert side["verbatim_check"] == "passed"
    assert "## Sanctions and Economic Pressure on Russia" in md
    assert "No quotes in this period." in md  # some sections are empty in the fixture
    for r in rows:
        if "512 drones" in r["quote_text"]:
            continue
        assert r["article_url"] in md

    out = export_all()
    assert out["quotes"] == 7
    lines = (config.EXPORT_DIR / "quotes.jsonl").read_text().splitlines()
    assert len(lines) == 7 and json.loads(lines[0])["quote_id"].startswith("Q")
    assert len(list((config.VAULT_DIR / "quotes").glob("*.md"))) == 7

    # a second run finds nothing new and does not duplicate anything
    res2 = run_daily(date(2026, 11, 25), llm=FakeLLM(extract_handler), fetcher=fetcher)
    assert res2.get("quotes_saved", 0) == 0
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 7


def test_network_failure_is_recorded_and_retried(home, monkeypatch):
    monkeypatch.setattr(SETTINGS, "daily_lookback_days", 7)
    broken = mock_fetcher({"https://www.bbc.co.uk/news/articles/ctest0002": (503, "unavailable")})
    run_daily(date(2026, 11, 25), llm=FakeLLM(extract_handler), fetcher=broken)
    conn = db.connect()
    row = conn.execute("SELECT * FROM articles WHERE url LIKE '%ctest0002'").fetchone()
    # direct and archive copies both failed
    assert row["status"] == "fetch_failed" and row["fetch_attempts"] == 1
    events = conn.execute("SELECT * FROM events WHERE event='fetch_failed'").fetchall()
    assert events
    # next day the page is back
    run_daily(date(2026, 11, 26), llm=FakeLLM(extract_handler), fetcher=mock_fetcher())
    row = conn.execute("SELECT * FROM articles WHERE url LIKE '%ctest0002'").fetchone()
    assert row["status"] == "processed"
