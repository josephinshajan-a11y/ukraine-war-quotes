"""Paths, settings and source definitions."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("QUOTEBASE_HOME", Path(__file__).resolve().parent.parent))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.environ.get("QUOTEBASE_DB", DATA_DIR / "quotes.db"))
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "export"
VAULT_DIR = DATA_DIR / "vault"
DIGEST_DIR = ROOT / "digests"
LOG_DIR = ROOT / "logs"

# Collection starts here unless a backfill asks for an earlier period.
DEFAULT_START = date(2026, 10, 1)
DIGEST_WINDOW_DAYS = 14


@dataclass
class Settings:
    # "anthropic" or "openai" (any OpenAI-compatible API: Gemini, Groq, Ollama, OpenAI)
    llm_provider: str = os.environ.get("QUOTEBASE_LLM_PROVIDER", "anthropic").lower()
    anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY")
    llm_base_url: str = os.environ.get("QUOTEBASE_LLM_BASE_URL",
                                       "https://generativelanguage.googleapis.com/v1beta/openai/")
    llm_api_key: str | None = os.environ.get("QUOTEBASE_LLM_API_KEY")
    # Seconds to wait between model calls; free tiers allow only a few requests per minute.
    llm_min_interval: float = float(os.environ.get("QUOTEBASE_LLM_MIN_INTERVAL", "0") or 0)
    extract_model: str = os.environ.get("QUOTEBASE_EXTRACT_MODEL", "claude-sonnet-5-5")
    digest_model: str = os.environ.get("QUOTEBASE_DIGEST_MODEL", "claude-opus-5-5")
    user_agent: str = os.environ.get(
        "QUOTEBASE_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36 quotebase/1.0",
    )
    request_timeout: float = float(os.environ.get("QUOTEBASE_TIMEOUT", "30"))
    host_delay: float = float(os.environ.get("QUOTEBASE_HOST_DELAY", "1.5"))
    max_fetch_attempts: int = int(os.environ.get("QUOTEBASE_MAX_FETCH_ATTEMPTS", "3"))
    respect_robots: bool = os.environ.get("QUOTEBASE_RESPECT_ROBOTS", "1") == "1"
    use_archive_fallback: bool = os.environ.get("QUOTEBASE_ARCHIVE_FALLBACK", "1") == "1"
    use_browser_fallback: bool = os.environ.get("QUOTEBASE_BROWSER_FALLBACK", "0") == "1"
    # Daily runs look back this many days so a missed day is picked up next time.
    daily_lookback_days: int = int(os.environ.get("QUOTEBASE_DAILY_LOOKBACK", "3"))


SETTINGS = Settings()


@dataclass
class Source:
    id: str
    name: str
    homepage: str
    domains: list[str]
    feeds: list[str] = field(default_factory=list)
    news_sitemaps: list[str] = field(default_factory=list)
    sitemap_index: str | None = None
    url_include: list[str] = field(default_factory=list)
    url_exclude: list[str] = field(default_factory=list)
    boilerplate: list[str] = field(default_factory=list)
    gdelt: bool = True
    # URL prefixes to look up in the Internet Archive's capture index (for
    # sites that block their own sitemaps, such as Reuters).
    wayback_prefixes: list[str] = field(default_factory=list)
    enabled: bool = True
    kind: str = "media"
    notes: str = ""

    def owns_url(self, url: str) -> bool:
        host = re.sub(r"^https?://", "", url).split("/")[0].lower().split(":")[0]
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def accepts_url(self, url: str) -> bool:
        if not self.owns_url(url):
            return False
        if any(re.search(p, url) for p in self.url_exclude):
            return False
        if self.url_include and not any(re.search(p, url) for p in self.url_include):
            return False
        return True


def _source_from_dict(d: dict) -> Source:
    known = Source.__dataclass_fields__.keys()
    return Source(**{k: v for k, v in d.items() if k in known})


def load_sources(include_disabled: bool = False) -> list[Source]:
    docs = []
    main = CONFIG_DIR / "sources.yaml"
    if main.exists():
        docs.extend(yaml.safe_load(main.read_text())["sources"])
    extra_dir = CONFIG_DIR / "sources.d"
    if extra_dir.exists():
        for p in sorted(extra_dir.glob("*.yaml")):
            docs.append(yaml.safe_load(p.read_text()))
    sources = [_source_from_dict(d) for d in docs]
    seen = set()
    for s in sources:
        if s.id in seen:
            raise ValueError(f"duplicate source id {s.id}")
        seen.add(s.id)
    return [s for s in sources if include_disabled or s.enabled]


def get_source(source_id: str) -> Source:
    for s in load_sources(include_disabled=True):
        if s.id == source_id:
            return s
    raise KeyError(source_id)


def load_topic() -> dict:
    return yaml.safe_load((CONFIG_DIR / "topic.yaml").read_text())


def load_sections() -> list[dict]:
    return yaml.safe_load((CONFIG_DIR / "sections.yaml").read_text())["sections"]


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, EXPORT_DIR, VAULT_DIR, DIGEST_DIR, LOG_DIR, LOG_DIR / "llm"):
        d.mkdir(parents=True, exist_ok=True)
