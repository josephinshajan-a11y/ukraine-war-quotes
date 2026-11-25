"""Dashboard: `streamlit run dashboard/app.py`"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quotebase import config, db  # noqa: E402

st.set_page_config(page_title="Ukraine war quotes", layout="wide")


@st.cache_resource
def conn():
    return db.connect()


def q(sql: str, args=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn(), params=args)


def run_cli(args: list[str]) -> None:
    cmd = [sys.executable, "-m", "quotebase", *args]
    st.code(" ".join(["quotebase", *args]))
    box = st.empty()
    lines: list[str] = []
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        lines.append(line.rstrip())
        box.code("\n".join(lines[-40:]))
    proc.wait()
    (st.success if proc.returncode == 0 else st.error)(f"exit code {proc.returncode}")
    st.cache_resource.clear()


# sidebar ---------------------------------------------------------------------

counts = q("SELECT (SELECT COUNT(*) FROM quotes) quotes, (SELECT COUNT(*) FROM articles WHERE status='processed') "
           "articles, (SELECT MAX(publication_date) FROM quotes) latest, "
           "(SELECT started_at || ' ' || status FROM runs ORDER BY id DESC LIMIT 1) last_run").iloc[0]
st.sidebar.title("Ukraine war quotes")
st.sidebar.metric("Quotes", int(counts["quotes"]))
st.sidebar.metric("Articles with quotes processed", int(counts["articles"]))
st.sidebar.caption(f"Newest quote: {(counts['latest'] or '-')[:10]}")
st.sidebar.caption(f"Last run: {counts['last_run'] or '-'}")
st.sidebar.caption(f"Database: {config.DB_PATH}")

tab_digest, tab_quotes, tab_articles, tab_runs, tab_sources, tab_actions = st.tabs(
    ["Digests", "Quotes", "Articles", "Runs and logs", "Sources", "Actions"])

# digests -----------------------------------------------------------------------

with tab_digest:
    files = sorted(config.DIGEST_DIR.glob("digest_*.md"), reverse=True)
    if not files:
        st.info("No digest yet. Run `quotebase daily` or use the Actions tab.")
    else:
        pick = st.selectbox("Digest", files, format_func=lambda p: p.stem.replace("digest_", ""))
        md = pick.read_text(encoding="utf-8")
        side = pick.with_suffix(".json")
        c1, c2 = st.columns(2)
        c1.download_button("Download .md", md, file_name=pick.name)
        if side.exists():
            c2.download_button("Download decisions .json", side.read_text(), file_name=side.name)
        st.markdown(md)
        if side.exists():
            with st.expander("Hidden quotes and reasons"):
                hidden = json.loads(side.read_text()).get("hidden", [])
                st.dataframe(pd.DataFrame(hidden), use_container_width=True)

# quotes --------------------------------------------------------------------------

with tab_quotes:
    sources = q("SELECT DISTINCT source_id FROM quotes ORDER BY 1")["source_id"].tolist()
    sections = [s["id"] for s in config.load_sections()]
    c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
    d_from = c1.date_input("From", date.today() - timedelta(days=14))
    d_to = c1.date_input("To", date.today())
    f_src = c2.multiselect("Source", sources, default=sources)
    f_sec = c3.multiselect("Section", sections, default=sections)
    f_text = c4.text_input("Text contains")
    sql = ("SELECT quote_id, publication_date, source_name, section, speaker, quote_text, article_name, article_url, "
           "extraction_date, source_id FROM quotes WHERE publication_date >= ? AND publication_date < ?")
    args: list = [d_from.isoformat(), (d_to + timedelta(days=1)).isoformat()]
    if f_src:
        sql += f" AND source_id IN ({','.join('?' * len(f_src))})"
        args += f_src
    if f_sec:
        sql += f" AND section IN ({','.join('?' * len(f_sec))})"
        args += f_sec
    if f_text:
        sql += " AND quote_text LIKE ?"
        args.append(f"%{f_text}%")
    df = q(sql + " ORDER BY publication_date DESC", args)
    st.caption(f"{len(df)} quotes")
    st.dataframe(df, use_container_width=True, hide_index=True,
                 column_config={"article_url": st.column_config.LinkColumn("article_url")})
    st.download_button("Download as CSV", df.to_csv(index=False), file_name="quotes.csv")
    jsonl = config.EXPORT_DIR / "quotes.jsonl"
    if jsonl.exists():
        st.download_button("Download full export (JSONL)", jsonl.read_text(encoding="utf-8"), file_name="quotes.jsonl")

    qid = st.text_input("Inspect a quote ID")
    if qid:
        row = q("SELECT q.*, a.text AS article_text, a.fetched_via, a.fetched_at FROM quotes q "
                "JOIN articles a ON a.id=q.article_id WHERE quote_id=?", (qid.strip(),))
        if row.empty:
            st.warning("not found")
        else:
            r = row.iloc[0]
            st.markdown(f"**{r['article_name']}** ({r['source_name']}, {r['publication_date'][:10]})  \n{r['article_url']}")
            st.markdown("Quote in context (offsets "
                        f"{r['char_start']}-{r['char_end']}, fetched via {r['fetched_via']}):")
            text = r["article_text"]
            s, e = int(r["char_start"]), int(r["char_end"])
            st.markdown(text[max(0, s - 400):s] + " **" + text[s:e] + "** " + text[e:e + 400])
            st.json(json.loads(r["verification"]))

# articles -----------------------------------------------------------------------------

with tab_articles:
    statuses = q("SELECT status, COUNT(*) n FROM articles GROUP BY 1 ORDER BY 2 DESC")
    st.dataframe(statuses, hide_index=True)
    pick = st.multiselect("Status", statuses["status"].tolist(), default=[s for s in statuses["status"] if s != "skipped_prefilter"])
    if pick:
        arts = q(f"SELECT id, source_id, published_at, title, status, status_detail, fetched_via, word_count, url "
                 f"FROM articles WHERE status IN ({','.join('?' * len(pick))}) ORDER BY published_at DESC LIMIT 2000", pick)
        st.dataframe(arts, use_container_width=True, hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("url")})
    with st.expander("Rejected quote candidates (last 500)"):
        rej = q("SELECT r.created_at, a.source_id, r.candidate_text, r.reasons, a.url FROM rejected_quotes r "
                "JOIN articles a ON a.id=r.article_id ORDER BY r.id DESC LIMIT 500")
        if not rej.empty:
            rej["reasons"] = rej["reasons"].map(lambda s: ", ".join(json.loads(s).get("reasons", [])))
        st.dataframe(rej, use_container_width=True, hide_index=True)

# runs and logs ---------------------------------------------------------------------

with tab_runs:
    runs = q("SELECT id, kind, started_at, finished_at, status, params, stats, error FROM runs ORDER BY id DESC LIMIT 200")
    st.dataframe(runs, use_container_width=True, hide_index=True)
    if not runs.empty:
        rid = st.selectbox("Events for run", runs["id"].tolist())
        ev = q("SELECT ts, level, event, data FROM events WHERE run_id=? ORDER BY id", (int(rid),))
        st.dataframe(ev, use_container_width=True, hide_index=True)
    log_files = sorted(config.LOG_DIR.glob("*.jsonl"), reverse=True)
    if log_files:
        lf = st.selectbox("Log file", log_files, format_func=lambda p: p.name)
        level = st.multiselect("Level", ["DEBUG", "INFO", "WARNING", "ERROR"], default=["WARNING", "ERROR"])
        rows = [json.loads(l) for l in lf.read_text(encoding="utf-8").splitlines() if l.strip()]
        st.dataframe(pd.DataFrame([r for r in rows if r["level"] in level][-2000:]), use_container_width=True, hide_index=True)
    llm_files = sorted((config.LOG_DIR / "llm").glob("*.jsonl"), reverse=True)
    if llm_files:
        with st.expander("Model calls"):
            lf = st.selectbox("Model call log", llm_files, format_func=lambda p: p.name)
            calls = [json.loads(l) for l in lf.read_text(encoding="utf-8").splitlines() if l.strip()]
            st.caption(f"{len(calls)} calls")
            i = st.number_input("Call #", 0, max(0, len(calls) - 1), max(0, len(calls) - 1))
            if calls:
                st.json(calls[int(i)], expanded=False)

# sources ----------------------------------------------------------------------------------

with tab_sources:
    rows = []
    for s in config.load_sources(include_disabled=True):
        rows.append({"id": s.id, "name": s.name, "kind": s.kind, "enabled": s.enabled, "feeds": len(s.feeds),
                     "news_sitemaps": len(s.news_sitemaps), "gdelt": s.gdelt, "notes": s.notes.strip()})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    per = q("SELECT source_id, COUNT(*) quotes, MIN(publication_date) first, MAX(publication_date) last "
            "FROM quotes GROUP BY 1")
    st.dataframe(per, hide_index=True)

# actions ------------------------------------------------------------------------------------

with tab_actions:
    st.subheader("Collect quotes for a past period")
    c1, c2, c3 = st.columns(3)
    b_from = c1.date_input("From ", date(2026, 8, 1))
    b_to = c2.date_input("To ", date(2026, 9, 30))
    b_src = c3.multiselect("Sources ", [s.id for s in config.load_sources()])
    if st.button("Run backfill"):
        args = ["backfill", "--from", b_from.isoformat(), "--to", b_to.isoformat()]
        for s in b_src:
            args += ["--source", s]
        run_cli(args)

    st.subheader("Add a source")
    c1, c2, c3 = st.columns(3)
    n_name = c1.text_input("Name", placeholder="Carnegie Endowment")
    n_url = c2.text_input("Homepage", placeholder="https://carnegieendowment.org")
    n_kind = c3.selectbox("Kind", ["media", "think-tank"])
    if st.button("Add and backfill from 1 Oct 2026") and n_name and n_url:
        run_cli(["add-source", "--name", n_name, "--url", n_url, "--kind", n_kind])

    st.subheader("Other")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Daily run now"):
        run_cli(["daily"])
    if c2.button("Rebuild today's digest"):
        run_cli(["digest"])
    if c3.button("Re-verify all quotes"):
        run_cli(["verify-db"])
    if c4.button("Check feeds"):
        run_cli(["check-sources"])
