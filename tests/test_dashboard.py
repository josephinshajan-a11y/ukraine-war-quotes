"""Runs the dashboard script against a populated test database and checks
that every tab renders without an exception."""

from datetime import date
from pathlib import Path

import pytest
from conftest import mock_fetcher
from test_end_to_end import digest_handler, extract_handler

from quotebase.config import SETTINGS
from quotebase.digest import build_digest
from quotebase.export import export_all
from quotebase.llm import FakeLLM
from quotebase.pipeline import run_daily

APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")


def test_dashboard_renders(home, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    monkeypatch.setattr(SETTINGS, "daily_lookback_days", 7)
    run_daily(date(2026, 11, 25), llm=FakeLLM(extract_handler), fetcher=mock_fetcher())
    build_digest(date(2026, 11, 25), llm=FakeLLM(digest_handler))
    export_all()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    assert any("War Intensity" in m.value for m in at.markdown)
