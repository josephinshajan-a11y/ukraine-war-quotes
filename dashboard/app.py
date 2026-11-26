"""Dashboard: `streamlit run dashboard/app.py`"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quotebase import config, db  # noqa: E402

st.set_page_config(page_title="Ukraine war quotes", page_icon="📰", layout="wide",
                   initial_sidebar_state="collapsed")

# One fixed colour per section, in config order. Taken from a palette checked
# for colour-blind separation; colour always comes with the section name.
SECTION_COLORS = ["#338ced", "#e76624", "#00a888", "#d28e05", "#e24d8b", "#0aac23", "#9887ef", "#eb6263"]
SECTIONS = config.load_sections()
SECTION_TITLE = {s["id"]: s["title"] for s in SECTIONS}
SECTION_COLOR = {s["id"]: SECTION_COLORS[i % len(SECTION_COLORS)] for i, s in enumerate(SECTIONS)}

STATUS_STYLE = {
    "ok": ("#0aac23", "Completed"),
    "ok_with_errors": ("#d28e05", "Completed with errors"),
    "running": ("#338ced", "Running"),
    "failed": ("#eb6263", "Failed"),
}

st.markdown(
    """
<style>
  #MainMenu, header [data-testid="stToolbar"], footer {visibility: hidden;}
  .block-container {padding-top: 3.5rem; max-width: 1280px;}
  h1, h2, h3 {letter-spacing: -0.01em;}
  .qb-head {display:flex; justify-content:space-between; align-items:flex-end; gap:1rem; margin-bottom:.4rem;}
  .qb-title {font-size:1.9rem; font-weight:700; line-height:1.2;}
  .qb-sub {color:#9c9c9c; font-size:.95rem; margin-top:.2rem;}
  .qb-pill {display:inline-flex; align-items:center; gap:.45rem; padding:.3rem .75rem; border-radius:999px;
            background:#1f1f1f; border:1px solid #2c2c2c; font-size:.85rem; color:#d6d6d6; white-space:nowrap;}
  .qb-dot {width:.55rem; height:.55rem; border-radius:50%; display:inline-block;}
  .qb-kpis {display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:.8rem; margin:1.2rem 0 1.4rem;}
  .qb-kpi {background:#1b1b1b; border:1px solid #2a2a2a; border-radius:12px; padding:.9rem 1rem;}
  .qb-kpi .v {font-size:1.7rem; font-weight:700; line-height:1.1;}
  .qb-kpi .l {color:#9c9c9c; font-size:.8rem; margin-top:.25rem;}
  .qb-section {margin:1.6rem 0 .6rem; padding-left:.8rem; border-left:4px solid var(--c);}
  .qb-section .t {font-size:1.25rem; font-weight:650;}
  .qb-section .n {color:#9c9c9c; font-size:.85rem; margin-left:.4rem; font-weight:400;}
  .qb-summary {background:#1b1b1b; border:1px solid #2a2a2a; border-radius:10px; padding:.75rem .9rem;
               margin:.5rem 0 .9rem; color:#d6d6d6; font-size:.95rem;}
  .qb-summary b {color:#9c9c9c; font-weight:600; font-size:.75rem; text-transform:uppercase; letter-spacing:.06em;
                 display:block; margin-bottom:.2rem;}
  .qb-card {background:#181818; border:1px solid #262626; border-radius:12px; padding:.95rem 1.1rem; margin-bottom:.7rem;}
  .qb-card:hover {border-color:#3a3a3a;}
  .qb-quote {font-size:1rem; line-height:1.55; color:#efefef; white-space:pre-wrap;}
  .qb-meta {display:flex; flex-wrap:wrap; gap:.35rem .9rem; margin-top:.65rem; color:#9c9c9c; font-size:.82rem; align-items:center;}
  .qb-meta a {color:#7fb4f5; text-decoration:none;}
  .qb-meta a:hover {text-decoration:underline;}
  .qb-chip {display:inline-flex; align-items:center; gap:.35rem; padding:.1rem .55rem; border-radius:999px;
            background:#222; border:1px solid #2f2f2f; color:#d6d6d6; font-size:.75rem;}
  .qb-id {font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.75rem; color:#7a7a7a;}
  .qb-empty {color:#7a7a7a; font-style:italic; margin:.3rem 0 1rem;}
  .qb-hidden {color:#9c9c9c; font-size:.85rem;}
  mark.qb-hl {background:#33507a; color:#fff; padding:0 .1rem; border-radius:3px;}
  .qb-context {background:#181818; border:1px solid #262626; border-radius:12px; padding:1rem 1.1rem;
               line-height:1.6; color:#bdbdbd; white-space:pre-wrap;}
  @media (max-width: 900px) {.qb-kpis {grid-template-columns:repeat(2, minmax(0,1fr));}}
  @media (max-width: 640px) {
    .block-container {padding-left: .9rem; padding-right: .9rem; padding-top: 3rem;}
    .qb-head {flex-direction: column; align-items: flex-start; gap: .6rem;}
    .qb-title {font-size: 1.4rem;}
    .qb-sub {font-size: .85rem;}
    .qb-pill {white-space: normal; font-size: .78rem;}
    .qb-kpis {gap: .5rem; margin: .9rem 0 1rem;}
    .qb-kpi {padding: .65rem .75rem;}
    .qb-kpi .v {font-size: 1.25rem;}
    .qb-kpi .l {font-size: .72rem;}
    .qb-kpi:last-child {grid-column: 1 / -1;}
    .qb-section .t {font-size: 1.05rem;}
    .qb-summary {font-size: .88rem;}
    .qb-card {padding: .75rem .8rem;}
    .qb-quote {font-size: .93rem;}
    .qb-meta {font-size: .75rem;}
    [data-baseweb="tab-list"] {overflow-x: auto; flex-wrap: nowrap;}
    [data-baseweb="tab-list"] button {flex-shrink: 0;}
  }
</style>
""",
    unsafe_allow_html=True,
)


def q(sql: str, args=()) -> pd.DataFrame:
    # A fresh connection per query: Streamlit runs each session in its own
    # thread, and a sqlite connection may only be used by the thread that made it.
    c = db.connect()
    try:
        return pd.read_sql_query(sql, c, params=args)
    finally:
        c.close()


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def chip(section: str) -> str:
    color = SECTION_COLOR.get(section, "#7a7a7a")
    return (f'<span class="qb-chip"><span class="qb-dot" style="background:{color}"></span>'
            f'{esc(SECTION_TITLE.get(section, section))}</span>')


def present(v) -> bool:
    return v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip() != ""


def quote_card(r, show_section: bool = False) -> str:
    meta = [f"<span><b>{esc(r['source_name'])}</b></span>", f"<span>{esc(str(r['publication_date'])[:10])}</span>"]
    if present(r.get("speaker")):
        meta.append(f"<span>{esc(r['speaker'])}</span>")
    if show_section:
        meta.append(chip(r["section"]))
    meta.append(f'<a href="{esc(r["article_url"])}" target="_blank">{esc(r["article_name"])} ↗</a>')
    meta.append(f'<span class="qb-id">{esc(r["quote_id"])}</span>')
    return (f'<div class="qb-card"><div class="qb-quote">{esc(r["quote_text"])}</div>'
            f'<div class="qb-meta">{"".join(meta)}</div></div>')


def section_header(sid: str, count: int) -> str:
    return (f'<div class="qb-section" style="--c:{SECTION_COLOR.get(sid, "#7a7a7a")}">'
            f'<span class="t">{esc(SECTION_TITLE.get(sid, sid))}</span>'
            f'<span class="n">{count} {"quote" if count == 1 else "quotes"}</span></div>')


def run_cli(args: list[str]) -> None:
    cmd = [sys.executable, "-m", "quotebase", *args]
    st.code(" ".join(["quotebase", *args]), language="bash")
    box = st.empty()
    lines: list[str] = []
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        lines.append(line.rstrip())
        box.code("\n".join(lines[-40:]))
    proc.wait()
    (st.success if proc.returncode == 0 else st.error)(f"Finished with exit code {proc.returncode}")
    st.cache_resource.clear()


# header and headline numbers ---------------------------------------------------

today = datetime.now(timezone.utc).date()
window_start = (today - timedelta(days=config.DIGEST_WINDOW_DAYS - 1)).isoformat()
k = q("SELECT (SELECT COUNT(*) FROM quotes) total, "
      "(SELECT COUNT(*) FROM quotes WHERE publication_date >= ?) recent, "
      "(SELECT COUNT(*) FROM articles WHERE status='processed') arts, "
      "(SELECT COUNT(DISTINCT source_id) FROM quotes) srcs, "
      "(SELECT MAX(publication_date) FROM quotes) newest", (window_start,)).iloc[0]
last_run = q("SELECT kind, started_at, status FROM runs ORDER BY id DESC LIMIT 1")
if last_run.empty:
    pill = '<span class="qb-pill"><span class="qb-dot" style="background:#7a7a7a"></span>No runs yet</span>'
else:
    lr = last_run.iloc[0]
    color, label = STATUS_STYLE.get(lr["status"], ("#7a7a7a", lr["status"]))
    pill = (f'<span class="qb-pill"><span class="qb-dot" style="background:{color}"></span>'
            f'Last {esc(lr["kind"])} run: {esc(label)} · {esc(str(lr["started_at"])[:16].replace("T", " "))} UTC</span>')

st.markdown(
    f"""<div class="qb-head"><div><div class="qb-title">Russia's war on Ukraine: media quotes</div>
<div class="qb-sub">Verified quotes from Reuters, BBC News and other outlets. Rolling {config.DIGEST_WINDOW_DAYS}-day
briefing: {window_start} to {today.isoformat()}.</div></div>{pill}</div>
<div class="qb-kpis">
 <div class="qb-kpi"><div class="v">{int(k['total']):,}</div><div class="l">Quotes in database</div></div>
 <div class="qb-kpi"><div class="v">{int(k['recent']):,}</div><div class="l">Quotes in the last {config.DIGEST_WINDOW_DAYS} days</div></div>
 <div class="qb-kpi"><div class="v">{int(k['arts']):,}</div><div class="l">Articles processed</div></div>
 <div class="qb-kpi"><div class="v">{int(k['srcs'])}</div><div class="l">Sources with quotes</div></div>
 <div class="qb-kpi"><div class="v">{esc((k['newest'] or '–')[:10])}</div><div class="l">Newest quote</div></div>
</div>""",
    unsafe_allow_html=True,
)

# The Actions tab starts jobs and uses the model API, so it only shows where
# QUOTEBASE_ADMIN=1 is set (your own machine). A public deployment is read-only.
ADMIN = os.environ.get("QUOTEBASE_ADMIN") == "1"
tab_names = ["Briefing", "Quotes", "Articles", "Runs and logs", "Sources"] + (["Actions"] if ADMIN else [])
tab_brief, tab_quotes, tab_articles, tab_runs, tab_sources, *rest = st.tabs(tab_names)
tab_actions = rest[0] if rest else None

# briefing -------------------------------------------------------------------------

with tab_brief:
    files = sorted(config.DIGEST_DIR.glob("digest_*.md"), reverse=True)
    if not files:
        st.info("No briefing yet. It is written at the end of each daily run, or run `quotebase digest`.")
    else:
        c1, c2, c3 = st.columns([2, 1, 1])
        pick = c1.selectbox("Briefing date", files, format_func=lambda p: p.stem.replace("digest_", ""))
        md = pick.read_text(encoding="utf-8")
        side_path = pick.with_suffix(".json")
        c2.download_button("Download .md", md, file_name=pick.name, width="stretch")
        if side_path.exists():
            c3.download_button("Download decisions .json", side_path.read_text(), file_name=side_path.name,
                               width="stretch")
        if not side_path.exists():
            st.markdown(md)
        else:
            side = json.loads(side_path.read_text())
            shown_ids = [i for ids in side["shown"].values() for i in ids]
            rows = {}
            if shown_ids:
                df = q(f"SELECT * FROM quotes WHERE quote_id IN ({','.join('?' * len(shown_ids))})", shown_ids)
                rows = {r["quote_id"]: r for r in df.to_dict("records")}
            st.markdown(f"#### {esc(md.splitlines()[0].lstrip('# '))}")
            st.caption("Quote text and source details are copied unchanged from the database. "
                       "The summary under each heading is written by a language model.")
            for s in SECTIONS:
                ids = side["shown"].get(s["id"], [])
                st.markdown(section_header(s["id"], len(ids)), unsafe_allow_html=True)
                if not ids:
                    st.markdown('<div class="qb-empty">No quotes in this period.</div>', unsafe_allow_html=True)
                    continue
                summary = side.get("summaries", {}).get(s["id"])
                if summary:
                    st.markdown(f'<div class="qb-summary"><b>Summary · model-written</b>{esc(summary)}</div>',
                                unsafe_allow_html=True)
                cards = sorted((rows[i] for i in ids if i in rows), key=lambda r: r["publication_date"], reverse=True)
                st.markdown("".join(quote_card(r) for r in cards), unsafe_allow_html=True)
            hidden = side.get("hidden", [])
            if hidden:
                with st.expander(f"{len(hidden)} quotes left out as superseded or repeated"):
                    st.dataframe(pd.DataFrame(hidden), width="stretch", hide_index=True)

# quotes -------------------------------------------------------------------------------

with tab_quotes:
    sources = q("SELECT DISTINCT source_id FROM quotes ORDER BY 1")["source_id"].tolist()
    f1, f2, f3, f4 = st.columns([1.2, 1.2, 1.6, 1.6])
    period = f1.date_input("Published between", (today - timedelta(days=30), today))
    f_src = f2.multiselect("Source", sources, placeholder="All sources")
    f_sec = f3.multiselect("Section", [s["id"] for s in SECTIONS], format_func=lambda i: SECTION_TITLE[i],
                           placeholder="All sections")
    f_text = f4.text_input("Search text or speaker", placeholder="e.g. ceasefire, Zelensky")
    d_from, d_to = (period if isinstance(period, tuple) and len(period) == 2 else (period, period)) \
        if period else (today - timedelta(days=30), today)

    sql = ("SELECT quote_id, publication_date, source_name, source_id, section, speaker, quote_text, "
           "article_name, article_url, extraction_date FROM quotes WHERE publication_date >= ? AND publication_date < ?")
    args: list = [d_from.isoformat(), (d_to + timedelta(days=1)).isoformat()]
    if f_src:
        sql += f" AND source_id IN ({','.join('?' * len(f_src))})"
        args += f_src
    if f_sec:
        sql += f" AND section IN ({','.join('?' * len(f_sec))})"
        args += f_sec
    if f_text:
        sql += " AND (quote_text LIKE ? OR speaker LIKE ?)"
        args += [f"%{f_text}%", f"%{f_text}%"]
    df = q(sql + " ORDER BY publication_date DESC, quote_id", args)

    if not df.empty:
        daily = df.assign(day=pd.to_datetime(df["publication_date"].str[:10]),
                          section_title=df["section"].map(SECTION_TITLE).fillna(df["section"]))
        daily = daily.groupby(["day", "section_title"]).size().reset_index(name="quotes")
        daily["date"] = daily["day"].dt.strftime("%d %b")
        day_order = sorted(daily["day"].unique())
        day_labels = [pd.Timestamp(d).strftime("%d %b") for d in day_order]
        present_titles = set(daily["section_title"])
        order = [s["title"] for s in SECTIONS if s["title"] in present_titles]
        colors = [SECTION_COLORS[i % len(SECTION_COLORS)] for i, s in enumerate(SECTIONS) if s["title"] in present_titles]
        chart = (
            alt.Chart(daily, title=alt.Title("Quotes per day by section", anchor="start", fontSize=14))
            .mark_bar(stroke="#0e1117", strokeWidth=2, width={"band": 0.6})
            .encode(
                x=alt.X("date:O", title=None, sort=day_labels,
                        axis=alt.Axis(grid=False, labelColor="#9c9c9c", labelAngle=0, ticks=False)),
                y=alt.Y("quotes:Q", title=None, axis=alt.Axis(tickMinStep=1, gridColor="#262626",
                                                             labelColor="#9c9c9c", domain=False)),
                color=alt.Color("section_title:N", title=None, sort=order,
                                scale=alt.Scale(domain=order, range=colors),
                                legend=alt.Legend(orient="bottom", columns=3, labelColor="#d6d6d6",
                                                  labelLimit=320, symbolType="circle", columnPadding=24)),
                order=alt.Order("section_title:N"),
                tooltip=[alt.Tooltip("day:T", title="Date", format="%d %b %Y"),
                         alt.Tooltip("section_title:N", title="Section"),
                         alt.Tooltip("quotes:Q", title="Quotes")],
            )
            .properties(height=220)
            .configure_view(strokeWidth=0)
            .configure(background="transparent")
        )
        st.altair_chart(chart, width="stretch", theme=None)

    v1, v2, v3 = st.columns([2, 1, 1])
    v1.markdown(f"**{len(df)}** quotes")
    view = v2.segmented_control("View", ["Cards", "Table"], default="Cards", label_visibility="collapsed")
    v3.download_button("Download CSV", df.to_csv(index=False), file_name="quotes.csv", width="stretch")

    if df.empty:
        st.markdown('<div class="qb-empty">No quotes match these filters.</div>', unsafe_allow_html=True)
    elif view == "Table":
        st.dataframe(df.drop(columns=["source_id"]), width="stretch", hide_index=True,
                     column_config={"article_url": st.column_config.LinkColumn("article_url", display_text="open"),
                                    "quote_text": st.column_config.TextColumn("quote_text", width="large")})
    else:
        page_size = 30
        pages = max(1, (len(df) + page_size - 1) // page_size)
        page = st.number_input("Page", 1, pages, 1) if pages > 1 else 1
        chunk = df.iloc[(page - 1) * page_size: page * page_size]
        st.markdown("".join(quote_card(r, show_section=True) for r in chunk.to_dict("records")),
                    unsafe_allow_html=True)

    st.markdown("##### Check a quote against its source")
    qid = st.text_input("Quote ID", placeholder="Paste an ID such as Q570FEF465B29", label_visibility="collapsed")
    if qid:
        row = q("SELECT q.*, a.text AS article_text, a.fetched_via, a.fetched_at FROM quotes q "
                "JOIN articles a ON a.id=q.article_id WHERE quote_id=?", (qid.strip(),))
        if row.empty:
            st.warning("No quote with that ID.")
        else:
            r = row.iloc[0]
            checks = json.loads(r["verification"])
            passed = sum(1 for v in checks.values() if v)
            st.markdown(quote_card(r.to_dict(), show_section=True), unsafe_allow_html=True)
            c1, c2 = st.columns([2, 1])
            text = r["article_text"]
            s, e = int(r["char_start"]), int(r["char_end"])
            c1.markdown("**In the article** (characters "
                        f"{s:,}–{e:,}, fetched {esc(str(r['fetched_at'])[:10])} via {esc(r['fetched_via'])})")
            c1.markdown(f'<div class="qb-context">…{esc(text[max(0, s - 500):s])}<mark class="qb-hl">'
                        f'{esc(text[s:e])}</mark>{esc(text[e:e + 500])}…</div>', unsafe_allow_html=True)
            c2.markdown(f"**Checks passed: {passed} of {len(checks)}**")
            c2.dataframe(pd.DataFrame({"check": list(checks), "passed": ["yes" if v else "NO" for v in checks.values()]}),
                         hide_index=True, width="stretch", height=420)

# articles -------------------------------------------------------------------------------

with tab_articles:
    statuses = q("SELECT status, COUNT(*) n FROM articles GROUP BY 1 ORDER BY 2 DESC")
    if statuses.empty:
        st.info("No articles yet.")
    else:
        status_chart = (
            alt.Chart(statuses, title=alt.Title("What happened to each discovered link", anchor="start", fontSize=14))
            .mark_bar(color="#338ced", cornerRadiusEnd=4, height=16)
            .encode(x=alt.X("n:Q", title=None, axis=alt.Axis(gridColor="#262626", labelColor="#9c9c9c", domain=False)),
                    y=alt.Y("status:N", sort="-x", title=None, axis=alt.Axis(labelColor="#d6d6d6")),
                    tooltip=[alt.Tooltip("status:N", title="Status"), alt.Tooltip("n:Q", title="Articles")])
            .properties(height=max(120, 30 * len(statuses)))
            .configure_view(strokeWidth=0).configure(background="transparent")
        )
        st.altair_chart(status_chart, width="stretch", theme=None)
        pick = st.multiselect("Status", statuses["status"].tolist(),
                              default=[s for s in statuses["status"] if s != "skipped_prefilter"])
        if pick:
            arts = q(f"SELECT published_at, source_id, title, status, status_detail, fetched_via, word_count, url "
                     f"FROM articles WHERE status IN ({','.join('?' * len(pick))}) ORDER BY published_at DESC LIMIT 2000",
                     pick)
            st.dataframe(arts, width="stretch", hide_index=True,
                         column_config={"url": st.column_config.LinkColumn("url", display_text="open")})
        with st.expander("Rejected quote candidates (last 500)"):
            rej = q("SELECT r.created_at, a.source_id, r.candidate_text, r.reasons, a.url FROM rejected_quotes r "
                    "JOIN articles a ON a.id=r.article_id ORDER BY r.id DESC LIMIT 500")
            if rej.empty:
                st.caption("None so far.")
            else:
                rej["reasons"] = rej["reasons"].map(lambda s: ", ".join(json.loads(s).get("reasons", [])))
                st.dataframe(rej, width="stretch", hide_index=True)

# runs and logs ----------------------------------------------------------------------------

with tab_runs:
    runs = q("SELECT id, kind, started_at, finished_at, status, params, stats, error FROM runs ORDER BY id DESC LIMIT 200")
    if runs.empty:
        st.info("No runs yet.")
    else:
        runs["status"] = runs["status"].map(lambda s: STATUS_STYLE.get(s, (None, s))[1])
        st.dataframe(runs, width="stretch", hide_index=True)
        rid = st.selectbox("Events for run", runs["id"].tolist())
        ev = q("SELECT ts, level, event, data FROM events WHERE run_id=? ORDER BY id", (int(rid),))
        st.dataframe(ev, width="stretch", hide_index=True)
    log_files = sorted(config.LOG_DIR.glob("*.jsonl"), reverse=True)
    if log_files:
        st.markdown("##### Log files")
        c1, c2 = st.columns([1, 2])
        lf = c1.selectbox("Day", log_files, format_func=lambda p: p.stem)
        level = c2.multiselect("Level", ["DEBUG", "INFO", "WARNING", "ERROR"], default=["WARNING", "ERROR"])
        rows = [json.loads(line) for line in lf.read_text(encoding="utf-8").splitlines() if line.strip()]
        st.dataframe(pd.DataFrame([r for r in rows if r["level"] in level][-2000:]),
                     width="stretch", hide_index=True)
    llm_files = sorted((config.LOG_DIR / "llm").glob("*.jsonl"), reverse=True)
    if llm_files:
        with st.expander("Model calls (prompt and response for every call)"):
            lf = st.selectbox("Model call log", llm_files, format_func=lambda p: p.stem)
            calls = [json.loads(line) for line in lf.read_text(encoding="utf-8").splitlines() if line.strip()]
            st.caption(f"{len(calls)} calls")
            if calls:
                i = st.number_input("Call #", 0, len(calls) - 1, len(calls) - 1)
                st.json(calls[int(i)], expanded=False)

# sources ----------------------------------------------------------------------------------

with tab_sources:
    per = q("SELECT source_id, COUNT(*) quotes, MAX(publication_date) last FROM quotes GROUP BY 1")
    per = {r["source_id"]: r for r in per.to_dict("records")}
    rows = []
    for s in config.load_sources(include_disabled=True):
        p = per.get(s.id, {})
        rows.append({"source": s.name, "kind": s.kind, "enabled": s.enabled, "feeds": len(s.feeds),
                     "news sitemaps": len(s.news_sitemaps), "GDELT": s.gdelt, "quotes": p.get("quotes", 0),
                     "newest quote": (p.get("last") or "")[:10], "notes": " ".join(s.notes.split())})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

# actions ------------------------------------------------------------------------------------

if tab_actions is not None:
    with tab_actions:
        a1, a2 = st.columns(2)
        with a1.container(border=True):
            st.markdown("**Collect quotes for a past period**")
            b_from = st.date_input("From", date(2026, 8, 1), key="bf")
            b_to = st.date_input("To", date(2026, 9, 30), key="bt")
            b_src = st.multiselect("Sources", [s.id for s in config.load_sources()], placeholder="All sources")
            if st.button("Run backfill", type="primary"):
                args = ["backfill", "--from", b_from.isoformat(), "--to", b_to.isoformat()]
                for s in b_src:
                    args += ["--source", s]
                run_cli(args)
        with a2.container(border=True):
            st.markdown("**Add a media or think-tank source**")
            n_name = st.text_input("Name", placeholder="Carnegie Endowment")
            n_url = st.text_input("Homepage", placeholder="https://carnegieendowment.org")
            n_kind = st.selectbox("Kind", ["media", "think-tank"])
            if st.button("Add and collect from 1 Oct 2026", type="primary") and n_name and n_url:
                run_cli(["add-source", "--name", n_name, "--url", n_url, "--kind", n_kind])
        with st.container(border=True):
            st.markdown("**Maintenance**")
            c1, c2, c3, c4 = st.columns(4)
            if c1.button("Daily run now", width="stretch"):
                run_cli(["daily"])
            if c2.button("Rebuild briefing", width="stretch"):
                run_cli(["digest"])
            if c3.button("Re-verify all quotes", width="stretch"):
                run_cli(["verify-db"])
            if c4.button("Check feeds", width="stretch"):
                run_cli(["check-sources"])
