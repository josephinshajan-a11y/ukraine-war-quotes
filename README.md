# Ukraine war quotes

Collects quotes from Reuters, BBC News and other outlets that help explain where Russia's war on Ukraine is going, stores them with full source details, and writes a daily Markdown briefing of the quotes from the last 14 days that are still current.

Start date for collection is 1 October 2026. Earlier periods can be added on request.

## What happens on a daily run

```
sources.yaml ──> discovery ──> topic filter ──> fetch ──> clean text ──> model proposes passages
 (feeds,          (RSS, news     (keyword        (direct,    (trafilatura,      │
  sitemaps,        sitemaps,      rules on        archive,    boilerplate       ▼
  GDELT)           GDELT)         headline/URL)   browser)    rules)       program checks each passage
                                                                            (exact match, whole sentences,
                                                                             no junk, metadata present)
                                                                                    │
                       digests/digest_YYYY-MM-DD.md <── 14-day digest <── SQLite (quotes.db)
                       + .json with every decision        (model marks     ──> data/export/*.jsonl
                                                           superseded         ──> data/vault (Obsidian)
                                                           quotes, writes
                                                           section leads)
```

1. **Discovery.** Every source in `config/sources.yaml` is checked the same way every day: its RSS feeds, its Google News sitemap and a GDELT query for its domain over the last 3 days. The lookback overlaps so a missed day is caught by the next run. URLs are normalised (tracking parameters removed) and filtered by each source's include/exclude patterns (live blogs, video pages and sport are excluded).
2. **Topic filter.** A link is kept if its headline, summary or URL matches a term in `config/topic.yaml` (Ukraine, Kyiv, Kremlin, Zelensky, sanctions, ceasefire and so on) and no exclude rule (football, weather and so on). After download, the headline and body together must contain at least 3 topic terms. These rules decide what is read; the model does not.
3. **Fetch.** Direct request with retries and per-host delays. If the publisher blocks it, the Internet Archive copy is tried, then a headless browser if enabled. How each page was obtained is stored in `articles.fetched_via`. The raw HTML is saved under `data/raw/` by its SHA-256.
4. **Clean text.** trafilatura pulls the article body. Paragraphs, list items and subheadings keep their structure. Captions, bylines, "sign up" prompts, "Reporting by ... Editing by ..." lines and similar are removed by generic and per-source rules.
5. **Passage proposal.** The model reads the article (long ones are split into overlapping parts) and returns passages it thinks matter, each with a section, speaker and a short reason.
6. **Verification.** A passage is saved only if all checks pass. Rejected passages are kept in `rejected_quotes` with the reasons.
7. **Digest and exports.** See below.

## Verification of quotes

The model's copy of a passage is never stored. The program finds the passage in the article text and stores the article's own characters for that span, along with the start and end offsets and a hash of the article text. Checks:

| Check | Fails when |
|---|---|
| `found_verbatim` | The passage is not one contiguous piece of the article. Only typography is forgiven (curly vs straight quotes, dash types, spacing). Changed words, reordering, "..." splices and passages glued from two places all fail. |
| `ends_with_sentence`, `starts_cleanly` | The span does not cover whole sentences. Spans are first widened to sentence boundaries inside the same paragraph. |
| `max_paragraphs` | More than 3 paragraphs. |
| `min_words`, `max_words` | Under 12 or over 300 words. |
| `no_boilerplate`, `no_heading_lines`, `no_table_symbols`, `no_urls` | Captions, bylines, ads, table rows, page numbers, links. |
| `url_belongs_to_source` | The article URL is not on the source's domains. |
| `title_present`, `publication_date_valid`, `source_*_present`, `extraction_date_present`, `section_known` | Any metadata field is missing or invalid. |

`quotebase verify-db` re-runs the checks on every stored quote: it confirms the quote text still sits at its recorded offsets, the article text hash is unchanged, and the quote ID still matches its content. The GitHub workflow runs it after every collection.

## Database

SQLite at `data/quotes.db`. Main table `quotes`:

| Field | |
|---|---|
| `quote_id` | `Q` + 12 hex chars of sha256(source_id, article_url, quote_text). Same quote, same ID, on any machine. |
| `quote_text` | Exact text from the article |
| `source_id`, `source_name` | e.g. `reuters`, `Reuters` |
| `publication_date` | ISO 8601 UTC, from the page's JSON-LD or meta tags, falling back to the feed |
| `article_name`, `article_url` | Headline and link to the original page |
| `extraction_date` | When the quote was saved |
| `section` | Digest section |
| `speaker`, `speaker_role`, `quote_kind` | Who is quoted and whether it is a direct quote, reported statement, reporting, analysis or data |
| `rationale` | The model's reason for picking the passage |
| `article_id`, `char_start`, `char_end`, `article_text_sha256` | Where the quote sits in the stored article text |
| `model`, `prompt_version`, `verification` | What proposed it and the result of every check |

Other tables: `articles` (every URL ever discovered, with status: `processed`, `skipped_prefilter`, `off_topic`, `not_relevant`, `fetch_failed`, `duplicate`, `out_of_range` and so on), `rejected_quotes`, `runs`, `events`, `digests`, `sources`.

For use with an LLM:

- `data/export/quotes.jsonl` has one quote per line with all fields. It can be pasted into a context window, loaded into a vector store or uploaded as a file.
- `data/export/articles.jsonl` has the cleaned text of every processed article, so a quote can be checked against its source.
- `data/export/schema.json` describes each field.
- `data/vault/` is an Obsidian vault with one note per quote and per article, linked to each other and tagged by section and source.

All model calls (prompt, response, token counts) are logged in `logs/llm/YYYY-MM-DD.jsonl`. Run logs are in `logs/YYYY-MM-DD.jsonl` and in the `runs` and `events` tables.

## The daily digest

`digests/digest_YYYY-MM-DD.md` covers the 14 days ending on the run date; `digests/latest.md` is a copy of the newest one. The title gives the period.

1. Quotes from the period are loaded and grouped by the section given at extraction time (`config/sections.yaml`: War Intensity, Peace Initiatives, International Support, Sanctions, Conflicting Parties' Resources, Energy and Infrastructure, Escalation Risks, Domestic Politics).
2. Identical texts (the same wire copy run by two outlets) are reduced to the earliest one by a rule.
3. For each section the model gets the quotes in date order, plus later quotes from other sections for reference, and lists the quotes to hide: **superseded** (a later quote gives a newer state of the same fact, e.g. an updated strike count, or a planned meeting that has since happened) or **duplicate**. The program rejects any decision that points to an unknown quote or says an old quote replaces a newer one. Anything the model does not mark stays in.
4. The model writes a one or two sentence lead for each section from the quotes left. It is labelled as model-written.
5. Quote text, source, date, speaker, article title and link are copied from the database by ID. After the file is written, the program checks that every quote appears in it character for character; if not, the digest is not written.

The `.json` file next to each digest lists which quotes were shown, which were hidden and why, and the summaries.

## Setup

Python 3.11 or later.

```bash
git clone <this repo>
cd ukraine-war-quotes
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dashboard,dev]"
cp .env.example .env          # add ANTHROPIC_API_KEY
quotebase init
quotebase check-sources       # confirms every feed and sitemap answers
```

First full load (1 October 2026 to today):

```bash
quotebase backfill --from 2026-10-01
quotebase digest
```

## Commands

| Command | |
|---|---|
| `quotebase daily` | Discover, extract, verify, write the digest, export. `--date` to run as of a past date, `--source` to limit, `--no-digest`. |
| `quotebase backfill --from 2026-08-01 --to 2026-09-30` | Collect quotes for any past period and add them to the database. `--source bbc` to limit. |
| `quotebase add-source --name "Carnegie Endowment" --url https://carnegieendowment.org --kind think-tank` | Finds the site's RSS/Atom feeds and news sitemaps, writes `config/sources.d/<id>.yaml`, then backfills from 1 October 2026. From then on the source is part of the daily run. `--no-backfill`, `--since`. |
| `quotebase digest` | Rebuild the digest only. |
| `quotebase process` | Retry articles left pending or failed. |
| `quotebase verify-db` | Re-check every stored quote. Exits non-zero on any problem. |
| `quotebase export` | Rewrite JSONL and the Obsidian vault. |
| `quotebase check-sources` | Test every feed and sitemap. |
| `quotebase stats` | Counts by source and status, last runs. |

## Dashboard

```bash
streamlit run dashboard/app.py
```

Tabs:

- **Digests**: read and download any day's digest and its decisions file.
- **Quotes**: filter by date, source, section and text, download CSV or JSONL, open any quote in the context of its article with its check results.
- **Articles**: every discovered URL and what happened to it, plus rejected passages and why.
- **Runs and logs**: run history, events per run, log files, every model call.
- **Sources**: configured sources and quote counts.
- **Actions**: run a backfill for a chosen period, add a source, run the daily job, re-verify the database.

## Scheduling

`.github/workflows/daily.yml` runs `quotebase daily` at 05:17 UTC, runs the tests and `verify-db`, then commits the database, exports, digest and logs back to the repository, so every change to the data has a commit. Add `ANTHROPIC_API_KEY` under Settings > Secrets > Actions. The same workflow can be started by hand for a backfill or to add a source.

To run on your own machine instead:

```
17 5 * * * cd /path/to/ukraine-war-quotes && .venv/bin/quotebase daily >> logs/cron.out 2>&1
```

## Sources

| Source | Discovery | Notes |
|---|---|---|
| Reuters | news sitemap, sitemap index, GDELT | Reuters blocks many automated requests. When that happens the Internet Archive copy is used, and the browser fallback can be switched on. The stored link is always reuters.com. Runs from GitHub's servers are blocked more often than runs from a home connection. |
| BBC News | 6 RSS feeds, 2 news sitemaps, sitemap index, GDELT | Live pages are excluded because their text keeps changing. bbc.com and bbc.co.uk copies of the same story are merged by canonical URL. |
| The Guardian, The Kyiv Independent, Al Jazeera | RSS, GDELT | Optional extras. |
| Institute for the Study of War | RSS, GDELT | Think tank. Its long daily assessments are split into parts for the model. |
| FT, The Economist, Bloomberg | configured, off | Paywalled. Set `QUOTEBASE_COOKIES_FT` (and so on) to the Cookie header from a logged-in browser and set `enabled: true`. |

GDELT is what makes backfills possible for sources whose feeds only show recent items. It indexes most stories from these outlets, but not every one, so the sitemap index is also read where a source has one.

## Rule-based vs model-based

| Step | How |
|---|---|
| Which sources are checked, how often, which URLs | Rules (config files, cron) |
| Which articles are read | Rules (topic terms, URL patterns, date window) |
| Which passages are worth keeping, and their section | Model |
| Whether a passage is accepted | Rules (all checks above) |
| Which quotes are superseded | Model, with rule checks on its answer |
| Quote text and metadata in the digest | Copied from the database by ID, then checked |
| Section leads | Model, labelled as such |

Model calls use temperature 0 and a fixed output schema. Prompts are versioned (`prompt_version` on each quote).

## Tests

```bash
pytest
```

The tests run offline. HTTP is replaced by a mock that serves invented feeds and articles, and the model by a stub. They cover: matching with typographic differences, rejection of changed words, glued passages and "..." splices, boilerplate removal, metadata checks, the archive fallback when the publisher returns 403, a failed fetch being retried the next day, a full daily run, tamper detection in `verify-db`, the digest hiding a superseded quote, the digest ignoring invalid model decisions, and the exports.

## Layout

```
config/            sources.yaml, sources.d/ (added sources), topic.yaml, sections.yaml
quotebase/
  discovery.py     feeds, sitemaps, GDELT, topic filter
  http.py          fetching, retries, robots.txt, archive and browser fallbacks
  article.py       text and metadata extraction
  extract.py       model prompt for passages
  textmatch.py     exact span matching
  verify.py        checks
  pipeline.py      daily run, backfill
  digest.py        14-day digest
  sources_builder.py  add-source
  export.py        JSONL and Obsidian
  db.py, llm.py, logs.py, config.py, cli.py
dashboard/app.py   Streamlit dashboard
digests/           daily Markdown files and decision logs
data/              quotes.db, export/, vault/, raw/
logs/              run logs and model call logs
tests/
```

## Known limits

- Coverage depends on what the feeds, sitemaps and GDELT list. An article missing from all three will be missed. `quotebase stats` and the Articles tab show what was found and what was skipped, so gaps can be checked by hand.
- If Reuters blocks the direct request and the Internet Archive has no copy yet, the article is retried on later runs, up to 3 attempts in all, and then left as `fetch_failed`.
- Whether a passage is "material" is a model judgement. The `rejected_quotes` table and the model call log make it possible to review those judgements.
