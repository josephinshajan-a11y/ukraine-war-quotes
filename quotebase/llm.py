"""Model access. Calls go through one method that returns JSON matching a
schema. Every request and response is appended to logs/llm/YYYY-MM-DD.jsonl
so any quote or digest decision can be traced back to the exact exchange."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Callable, Protocol

from . import logs
from . import config
from .config import SETTINGS

log = logs.get("llm")


class LLMError(Exception):
    pass


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


def default(kind: str) -> LLM:
    model = SETTINGS.extract_model if kind == "extract" else SETTINGS.digest_model
    return AnthropicLLM(model)
