"""HTTP fetching with retries, per-host delays, robots.txt and fallbacks.

Fallback order for article pages:
  1. direct request to the publisher
  2. Internet Archive copy (web.archive.org/web/<date>id_/<url>)
  3. headless Chromium via Playwright, if installed and enabled
"""

from __future__ import annotations

import os
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import logs
from .config import SETTINGS

log = logs.get("http")
_sleep = time.sleep  # replaced in tests

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
BLOCK_STATUS = {401, 403, 451}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str
    via: str  # "direct", "archive:<snapshot url>", "browser"

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status = status


def _cookie_for(host: str) -> str | None:
    # QUOTEBASE_COOKIES_FT, QUOTEBASE_COOKIES_ECONOMIST, ... keyed by the
    # second-level domain name.
    parts = host.split(".")
    key = parts[-2] if len(parts) >= 2 else host
    return os.environ.get(f"QUOTEBASE_COOKIES_{key.upper()}")


class Fetcher:
    def __init__(self, settings=SETTINGS, client: httpx.Client | None = None):
        self.s = settings
        self.client = client or httpx.Client(
            timeout=settings.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "en-GB,en;q=0.9",
            },
        )
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # basic GET -----------------------------------------------------------

    def _wait_for_host(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.s.host_delay:
                _sleep(self.s.host_delay - gap)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, *, accept: str | None = None, retries: int = 3,
            min_delay: float | None = None) -> FetchResult:
        host = urlsplit(url).netloc
        headers = {"Accept": accept} if accept else {}
        cookie = _cookie_for(host)
        if cookie:
            headers["Cookie"] = cookie
        last_err = None
        for attempt in range(1, retries + 1):
            self._wait_for_host(host)
            if min_delay:
                _sleep(min_delay)
            try:
                r = self.client.get(url, headers=headers)
            except httpx.HTTPError as e:
                last_err = FetchError(url, f"{type(e).__name__}: {e}")
                log.warning("network error %s (attempt %d/%d): %s", url, attempt, retries, e)
            else:
                if r.status_code in RETRY_STATUS:
                    last_err = FetchError(url, f"HTTP {r.status_code}", r.status_code)
                    log.warning("HTTP %s for %s (attempt %d/%d)", r.status_code, url, attempt, retries)
                    retry_after = r.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        _sleep(min(int(retry_after), 60))
                    elif r.status_code == 429:
                        # rate limited with no hint: back off harder than for other errors
                        _sleep(20 * attempt)
                elif r.status_code >= 400:
                    raise FetchError(url, f"HTTP {r.status_code}", r.status_code)
                else:
                    return FetchResult(url, str(r.url), r.status_code, r.text, "direct")
            if attempt < retries:
                _sleep(2 ** attempt + random.random())
        raise last_err or FetchError(url, "unknown error")

    # robots.txt -----------------------------------------------------------

    def allowed(self, url: str) -> bool:
        if not self.s.respect_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                res = self.get(base + "/robots.txt", retries=1)
                rp.parse(res.text.splitlines())
                self._robots[base] = rp
            except FetchError:
                self._robots[base] = None  # no robots.txt reachable: treat as allowed
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.s.user_agent, url)

    # article pages ----------------------------------------------------------

    def fetch_article(self, url: str, published: str | None = None) -> FetchResult:
        errors = []
        if self.allowed(url):
            try:
                res = self.get(url, accept="text/html,application/xhtml+xml")
                if res.ok and not looks_blocked(res.text):
                    return res
                errors.append("direct: blocked or empty page")
            except FetchError as e:
                errors.append(f"direct: {e.reason}")
        else:
            errors.append("direct: disallowed by robots.txt")

        if self.s.use_archive_fallback:
            stamp = (published or "")[:10].replace("-", "") or "2"
            snap = f"https://web.archive.org/web/{stamp}id_/{url}"
            try:
                res = self.get(snap, accept="text/html", retries=2)
                if res.ok and not looks_blocked(res.text):
                    return FetchResult(url, url, res.status, res.text, f"archive:{res.final_url}")
                errors.append("archive: blocked or empty")
            except FetchError as e:
                errors.append(f"archive: {e.reason}")

        if self.s.use_browser_fallback:
            try:
                html = browser_fetch(url, self.s.user_agent)
                if html and not looks_blocked(html):
                    return FetchResult(url, url, 200, html, "browser")
                errors.append("browser: blocked or empty")
            except Exception as e:  # playwright raises its own types
                errors.append(f"browser: {type(e).__name__}: {e}")

        raise FetchError(url, "; ".join(errors))


BLOCK_MARKERS = (
    "captcha-delivery.com",
    "Please enable JS and disable any ad blocker",
    "Access Denied</title>",
    "Are you a robot?",
    "px-captcha",
    "cf-browser-verification",
)


def looks_blocked(html: str) -> bool:
    head = html[:20000]
    return len(html) < 500 or any(m in head for m in BLOCK_MARKERS)


def browser_fetch(url: str, user_agent: str) -> str:
    from playwright.sync_api import sync_playwright  # optional dependency

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=user_agent)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            return page.content()
        finally:
            browser.close()
