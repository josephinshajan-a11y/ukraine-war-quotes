from quotebase.config import Source
from quotebase.textmatch import locate, snap_to_sentences
from quotebase.verify import build_quote, quote_id, text_checks

ARTICLE = (
    "Ukraine’s air force said Russia launched 512 drones overnight. It was the largest attack this month.\n\n"
    "“We are seeing the tempo of strikes rise every week,” said Test Commander Ivan Example.\n\n"
    "Officials said power was cut to about 1.2 million households – mostly in the east.\n\n"
    "- First list item about air defence stocks running low in several regions.\n"
    "- Second list item about repair crews."
)

SRC = Source(id="bbc", name="BBC News", homepage="https://www.bbc.co.uk", domains=["bbc.co.uk", "bbc.com"],
             boilerplate=[r"^Image caption"])
SECTIONS = {"war_intensity", "resources"}


def test_locate_forgives_typography_only():
    span = locate(ARTICLE, "\"We are seeing the tempo of strikes rise every week,\" said Test Commander Ivan Example.")
    assert span is not None
    assert ARTICLE[span[0]:span[1]].startswith("“We are")


def test_locate_rejects_changed_words():
    assert locate(ARTICLE, "Ukraine's air force said Russia fired 512 drones overnight.") is None


def test_locate_rejects_frankenquote():
    joined = "Ukraine's air force said Russia launched 512 drones overnight. Officials said power was cut"
    assert locate(ARTICLE, joined) is None


def test_locate_rejects_ellipsis_splice():
    assert locate(ARTICLE, "Ukraine's air force said ... 512 drones overnight.") is None


def test_locate_list_without_marker():
    span = locate(ARTICLE, "First list item about air defence stocks running low in several regions.")
    assert span is not None
    assert ARTICLE[span[0]:span[1]].startswith("- First")


def test_snap_extends_to_sentence_bounds():
    s = ARTICLE.index("Russia launched")
    e = ARTICLE.index(" overnight")
    a, b = snap_to_sentences(ARTICLE, s, e)
    assert ARTICLE[a:b] == "Ukraine’s air force said Russia launched 512 drones overnight."


def _build(candidate, url="https://www.bbc.co.uk/news/articles/x1", section="war_intensity",
           published="2026-11-20T08:00:00+00:00"):
    return build_quote(article_text=ARTICLE, candidate=candidate, source=SRC, url=url, title="Test title",
                       published_at=published, section=section, section_ids=SECTIONS,
                       extracted_at="2026-11-25T06:00:00+00:00")


def test_build_quote_accepts_and_stores_article_characters():
    fields, report = _build("Ukraine's air force said Russia launched 512 drones overnight. "
                            "It was the largest attack this month.")
    assert fields, report
    assert fields["quote_text"] == ARTICLE[fields["char_start"]:fields["char_end"]]
    assert "’" in fields["quote_text"]  # article's curly apostrophe kept, not the model's straight one


def test_build_quote_rejects_short_and_bad_metadata():
    fields, report = _build("It was the largest attack this month.")
    assert fields is None and "min_words" in report["reasons"]
    fields, report = _build("Ukraine's air force said Russia launched 512 drones overnight. It was the largest attack this month.",
                            url="https://example.com/fake")
    assert fields is None and "url_belongs_to_source" in report["reasons"]
    fields, report = _build("Ukraine's air force said Russia launched 512 drones overnight. It was the largest attack this month.",
                            section="made_up")
    assert fields is None and "section_known" in report["reasons"]
    fields, report = _build("Ukraine's air force said Russia launched 512 drones overnight. It was the largest attack this month.",
                            published=None)
    assert fields is None and "publication_date_valid" in report["reasons"]


def test_text_checks_flag_junk():
    c = text_checks("Reporting by Jane Doe; Editing by John Roe and several other people today.", [r"^Reporting by"])
    assert not c["no_boilerplate"]
    c = text_checks("| col | col | and other table text that goes on for a few words.", [])
    assert not c["no_table_symbols"]


def test_quote_id_stable():
    assert quote_id("bbc", "u", "t") == quote_id("bbc", "u", "t")
    assert quote_id("bbc", "u", "t") != quote_id("bbc", "u", "t2")
