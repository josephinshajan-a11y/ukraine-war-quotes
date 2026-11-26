"""Model access. Calls go through one method that returns JSON matching a
schema. Every request and response is appended to logs/llm/YYYY-MM-DD.jsonl
so any quote or digest decision can be traced back to the exact exchange."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Protocol

import httpx

from . import logs
from . import config
from .config import SETTINGS

log = logs.get("llm")


class LLMError(Exception):
    pass


class QuotaExhausted(LLMError):
    """Every configured model has used up its daily allowance."""


DAILY_LIMIT_MARKERS = ("per day", "(tpd)", "(rpd)", "daily")


class LLM(Protocol):
    model: str

    def json_call(self, *, system: str, user: str, schema: dict, tool_name: str, max_tokens: int = 8000) -> dict: ...


def _trace(model: str, tool: str, system: str, user: str, result: dict | None, error: str | None, usage=None) -> str:
    now = datetime.now(timezone.utc)
    call_id = hashlib.sha256(f"{now.isoformat()}{user[:2000]}".encode()).hexdigest()[:16]
    (config.LOG_DIR / "llm").mkdir(parents=True, exist_ok=True)
    with (config.LOG_DIR / "llm" / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "call_id": call_id, "ts": now.isoformat(timespec="seconds"), "model": model, "tool": tool,
            "system": system, "user": user, "result": result, "error": error, "usage": usage,
        }, ensure_ascii=False) + "\n")
    return call_id


class AnthropicLLM:
    def __init__(self, model: str, api_key: str | None = None):
        import anthropic

        key = api_key or SETTINGS.anthropic_api_key
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.Anthropic(api_key=key, max_retries=4)
        self.model = model

    def json_call(self, *, system: str, user: str, schema: dict, tool_name: str, max_tokens: int = 8000) -> dict:
        import anthropic

        tool = {"name": tool_name, "description": "Return the result in this structure.", "input_schema": schema}
        last = None
        for attempt in range(3):
            kwargs = dict(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user}],
            )
            # Temperature 0 by default. Set QUOTEBASE_TEMPERATURE="" for models
            # that do not accept the parameter.
            temp = os.environ.get("QUOTEBASE_TEMPERATURE", "0")
            if temp != "":
                kwargs["temperature"] = float(temp)
            try:
                resp = self.client.messages.create(**kwargs)
            except anthropic.APIError as e:
                last = str(e)
                log.warning("model call failed (attempt %d): %s", attempt + 1, e)
                time.sleep(5 * (attempt + 1))
                continue
            for block in resp.content:
                if block.type == "tool_use" and block.name == tool_name:
                    usage = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
                    _trace(self.model, tool_name, system, user, block.input, None, usage)
                    if resp.stop_reason == "max_tokens":
                        raise LLMError("response cut off at max_tokens")
                    return block.input
            last = f"no tool_use block (stop_reason={resp.stop_reason})"
        _trace(self.model, tool_name, system, user, None, last)
        raise LLMError(last or "model call failed")


class FakeLLM:
    """For tests and dry runs: `handler(tool_name, user_prompt) -> dict`."""

    def __init__(self, handler: Callable[[str, str], dict], model: str = "fake"):
        self.handler = handler
        self.model = model

    def json_call(self, *, system: str, user: str, schema: dict, tool_name: str, max_tokens: int = 8000) -> dict:
        result = self.handler(tool_name, user)
        _trace(self.model, tool_name, system, user, result, None)
        return result


def _plain_schema(node):
    """Some OpenAI-compatible servers (Gemini among them) reject union types
    like ["string", "null"]. Keep the first non-null type."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, list):
                out[k] = next((t for t in v if t != "null"), "string")
            else:
                out[k] = _plain_schema(v)
        return out
    if isinstance(node, list):
        return [_plain_schema(x) for x in node]
    return node


def _salvage(generation):
    """Arguments from a function call the server generated but rejected."""
    if not generation:
        return None
    try:
        data = json.loads(generation)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict) and "arguments" in data:
        args = data["arguments"]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None
        return args if isinstance(args, dict) else None
    return data if isinstance(data, dict) and ("quotes" in data or "hide" in data or "summary" in data) else None


class OpenAICompatLLM:
    """Chat Completions API with a forced function call. Works with the
    Gemini API (free tier), Groq, a local Ollama server, or OpenAI itself."""

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None,
                 client: httpx.Client | None = None, min_interval: float | None = None):
        # A comma-separated list: when one model hits its daily limit the next one is used.
        self.models = [m.strip() for m in model.split(",") if m.strip()]
        self.index = 0
        self.model = self.models[0]
        self.base_url = (base_url or SETTINGS.llm_base_url).rstrip("/")
        key = api_key if api_key is not None else SETTINGS.llm_api_key
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.client = client or httpx.Client(timeout=300, headers=headers)
        self.min_interval = SETTINGS.llm_min_interval if min_interval is None else min_interval
        self._last = 0.0

    def _pace(self):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def json_call(self, *, system: str, user: str, schema: dict, tool_name: str, max_tokens: int = 8000) -> dict:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": [{"type": "function", "function": {
                "name": tool_name, "description": "Return the result in this structure.",
                "parameters": _plain_schema(schema) if "googleapis.com" in self.base_url else schema}}],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        }
        temp = os.environ.get("QUOTEBASE_TEMPERATURE", "0")
        if temp != "":
            body["temperature"] = float(temp)
        cap = os.environ.get("QUOTEBASE_LLM_MAX_TOKENS")
        if cap:
            # Some providers count max_tokens against the per-minute token limit.
            body["max_tokens"] = min(max_tokens, int(cap))
        effort = os.environ.get("QUOTEBASE_REASONING_EFFORT")
        if effort:
            body["reasoning_effort"] = effort
        last = None
        format_retry = False
        for attempt in range(8):
            self._pace()
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 429 and any(m in r.text.lower() for m in DAILY_LIMIT_MARKERS):
                if self.index + 1 < len(self.models):
                    self.index += 1
                    log.warning("daily limit reached on %s, switching to %s", self.model, self.models[self.index])
                    self.model = self.models[self.index]
                    body["model"] = self.model
                    continue
                _trace(self.model, tool_name, system, user, None, r.text[:500])
                raise QuotaExhausted(f"daily limit reached on all models ({', '.join(self.models)})")
            if r.status_code == 429 or r.status_code >= 500:
                # free-tier quota or overload: wait and try again
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                log.warning("model call got %s, waiting (attempt %d)", r.status_code, attempt + 1)
                time.sleep(min(30 * (attempt + 1), 180))
                continue
            if r.status_code == 400 and "reasoning" in r.text.lower() and "reasoning_effort" in body:
                body.pop("reasoning_effort")
                continue
            if r.status_code == 400:
                try:
                    err = r.json().get("error", {})
                except ValueError:
                    err = {}
                if err.get("code") == "tool_use_failed":
                    salvaged = _salvage(err.get("failed_generation"))
                    if salvaged is not None:
                        _trace(self.model, tool_name, system, user, salvaged, "salvaged from tool_use_failed")
                        return salvaged
                if err.get("code") in ("tool_use_failed", "output_parse_failed") and not format_retry:
                    format_retry = True
                    log.warning("model reply was not in the required format, asking again")
                    continue
            if r.status_code >= 400:
                last = f"HTTP {r.status_code}: {r.text[:500]}"
                break
            data = r.json()
            try:
                msg = data["choices"][0]["message"]
                call = next(c for c in msg.get("tool_calls") or [] if c["function"]["name"] == tool_name)
                result = json.loads(call["function"]["arguments"])
            except (KeyError, IndexError, StopIteration, json.JSONDecodeError, TypeError) as e:
                # some servers put the JSON in the message text instead
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    try:
                        result = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        result = None
                else:
                    result = None
                if result is None:
                    last = f"no usable function call in response ({type(e).__name__})"
                    continue
            _trace(self.model, tool_name, system, user, result, None, data.get("usage"))
            return result
        _trace(self.model, tool_name, system, user, None, last)
        raise LLMError(last or "model call failed")


def default(kind: str) -> LLM:
    model = SETTINGS.extract_model if kind == "extract" else SETTINGS.digest_model
    if SETTINGS.llm_provider == "openai":
        return OpenAICompatLLM(model)
    return AnthropicLLM(model)
