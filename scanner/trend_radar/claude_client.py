"""Single-provider Anthropic client wrapper for the Claude-powered Trend
Radar spike. One provider, one model family, no abstraction layer, no
tool calling, no assistant-turn prefilling, no explicit sampling
parameters -- this is deliberately not built to support swapping
providers later.

The API key is read from ANTHROPIC_API_KEY only, and only at call time
(build_client() is never invoked at import time anywhere in this
package). It is never logged, printed, persisted, or included in any
returned/recorded structure -- UsageTotals below tracks call counts and
token counts only.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic
from anthropic.lib._parse._transform import transform_schema  # same schema transform messages.parse() uses
from pydantic import ValidationError

DEFAULT_MODEL = "claude-sonnet-5-5"


class MissingApiKeyError(RuntimeError):
    """Raised when ANTHROPIC_API_KEY is unset. Never includes any key
    value in its message -- there is none available to include."""


# US$ per million tokens: (input, output, cache_read, cache_write_5m).
# Source: claude-api skill model table (cached 2026-10-06).
PRICES_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
FALLBACK_PRICE = (4.00, 20.00, 0.40, 5.00)  # deliberately pessimistic for unknown models
# Web search server tool: US$10 per 1,000 searches, on top of tokens.
WEB_SEARCH_USD = 0.01
# Search results come back as input tokens; budget this many per search
# when estimating a research call's worst case for the spend cap.
EST_TOKENS_PER_SEARCH = 25_000


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def price_for(model: str) -> tuple[float, float, float, float]:
    return PRICES_PER_MTOK.get(model, FALLBACK_PRICE)


@dataclass
class UsageTotals:
    """Accumulates usage across every structured call made in one scan
    run. Deliberately holds nothing but counts/timings -- no prompt or
    response content, so this is always safe to serialize into the
    final result or a log line."""

    model: str
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    web_search_requests: int = 0
    elapsed_seconds: float = 0.0

    def record(self, usage, elapsed: float) -> float:
        """Adds one call's usage; returns that call's cost in US$."""
        before = self.cost_usd
        self.api_calls += 1
        self.input_tokens += int(usage.input_tokens)
        self.output_tokens += int(usage.output_tokens)
        self.cache_read_input_tokens += _int(getattr(usage, "cache_read_input_tokens", 0))
        self.cache_creation_input_tokens += _int(getattr(usage, "cache_creation_input_tokens", 0))
        self.web_search_requests += _int(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0))
        self.elapsed_seconds += elapsed
        return self.cost_usd - before

    @property
    def cost_usd(self) -> float:
        p_in, p_out, p_cr, p_cw = price_for(self.model)
        return (
            self.input_tokens * p_in
            + self.output_tokens * p_out
            + self.cache_read_input_tokens * p_cr
            + self.cache_creation_input_tokens * p_cw
        ) / 1_000_000 + self.web_search_requests * WEB_SEARCH_USD

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "web_search_requests": self.web_search_requests,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "cost_usd": round(self.cost_usd, 4),
        }


class SpendCapReached(RuntimeError):
    """Raised before a call that could push today's Claude spend over the cap."""


class ClaudeOutputError(RuntimeError):
    """Raised when a response was truncated (max_tokens), refused, or
    could not be parsed into the requested schema."""


DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent.parent / "claude_spend.json"
DEFAULT_DAILY_CAP_USD = 5.0


class SpendLedger:
    """Tiny per-day spend ledger (local date -> US$) shared by every paid
    Claude call in this app, so a scheduler bug or a retry loop can never
    burn the monthly credit."""

    def __init__(self, path: Path = DEFAULT_LEDGER_PATH, daily_cap_usd: float | None = None):
        self.path = Path(path)
        cap_env = os.environ.get("MFO_CLAUDE_DAILY_CAP_USD")
        self.daily_cap_usd = daily_cap_usd if daily_cap_usd is not None else float(cap_env or DEFAULT_DAILY_CAP_USD)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def spent_today(self) -> float:
        return float(self._load().get(self._today(), 0.0))

    def check(self, estimated_usd: float) -> None:
        spent = self.spent_today()
        if spent + estimated_usd > self.daily_cap_usd:
            raise SpendCapReached(
                f"daily Claude cap US${self.daily_cap_usd:.2f} would be exceeded "
                f"(spent US${spent:.2f} today, next call could cost up to US${estimated_usd:.2f})"
            )

    def add(self, usd: float) -> None:
        data = self._load()
        today = self._today()
        data[today] = round(float(data.get(today, 0.0)) + usd, 6)
        keep = sorted(data)[-60:]
        self.path.write_text(json.dumps({k: data[k] for k in keep}, indent=2) + "\n", encoding="utf-8")


def estimate_call_cost(model: str, system: str, user_content: str, max_tokens: int) -> float:
    """Upper-bound estimate: ~3 chars/token input, full max_tokens output."""
    p_in, p_out, _, _ = price_for(model)
    input_tokens = (len(system) + len(user_content)) / 3
    return (input_tokens * p_in + max_tokens * p_out) / 1_000_000


def resolve_model(explicit: str | None = None) -> str:
    """Explicit --model wins, then MFO_TREND_MODEL, then the default.
    Pure string resolution -- never touches the API key or network."""
    return explicit or os.environ.get("MFO_TREND_MODEL") or DEFAULT_MODEL


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise MissingApiKeyError(
            "ANTHROPIC_API_KEY is not set. This command reads the key only "
            "from that environment variable and will not proceed without "
            "it -- set it before running claude-scan."
        )
    return key


def build_client() -> anthropic.Anthropic:
    """Constructs a client at call time only. Callers must not construct
    this at import time or module load time -- see claude_scan.py, which
    only calls this inside run_claude_scan(), never at module scope."""
    return anthropic.Anthropic(api_key=_api_key())


def call_structured(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    user_content: str,
    output_format: type,
    max_tokens: int,
    usage: UsageTotals,
    ledger: SpendLedger | None = None,
    effort: str | None = None,
):
    """One structured-output call, streamed (so large max_tokens can't hit
    the SDK's non-streaming timeout guard).

    The JSON schema goes in output_config.format and the response text is
    validated *after* the stream completes, so a truncated or refused
    response is reported as such (ClaudeOutputError with the stop_reason)
    instead of surfacing as an opaque JSON parse error.

    No tools, no prefill, no sampling parameters. Thinking is the model's
    default (adaptive); depth is steered with output_config.effort.
    """
    if ledger is not None:
        ledger.check(estimate_call_cost(model, system, user_content, max_tokens))

    output_config: dict = {
        "format": {"type": "json_schema", "schema": transform_schema(output_format.model_json_schema())},
    }
    effort = effort or os.environ.get("MFO_TREND_EFFORT")
    if effort:
        output_config["effort"] = effort

    started = time.monotonic()
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        output_config=output_config,
    ) as stream:
        final = stream.get_final_message()
    elapsed = time.monotonic() - started
    cost = usage.record(final.usage, elapsed)
    if ledger is not None:
        ledger.add(cost)

    stop_reason = getattr(final, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise ClaudeOutputError(f"response truncated at max_tokens={max_tokens}")
    if stop_reason == "refusal":
        details = getattr(final, "stop_details", None)
        category = getattr(details, "category", None)
        raise ClaudeOutputError(f"model refused the request (category: {category})")

    text = "".join(block.text for block in final.content if getattr(block, "type", None) == "text")
    if not text.strip():
        raise ClaudeOutputError(f"response had no text output (stop_reason={stop_reason})")
    try:
        return output_format.model_validate_json(text)
    except ValidationError as exc:
        raise ClaudeOutputError(f"response did not match the {output_format.__name__} schema: {exc.errors()[:3]}") from exc


AU_LOCATION = {"type": "approximate", "country": "AU", "city": "Sydney", "region": "New South Wales",
               "timezone": "Australia/Sydney"}
MAX_PAUSE_RESUMES = 3


def call_web_research(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    user_content: str,
    max_tokens: int,
    max_searches: int,
    usage: UsageTotals,
    ledger: SpendLedger | None = None,
) -> tuple[str, list[dict]]:
    """One plain-text call with the web search server tool (capped at
    max_searches, searches localised to Australia). Returns the model's
    notes and every result URL it saw. Kept separate from
    call_structured so structured output and web search never share a
    request.

    A long server-side search loop can stop with stop_reason "pause_turn";
    the paused assistant turn is sent back unchanged (no extra user
    message) so the server resumes it, at most MAX_PAUSE_RESUMES times.
    """
    if ledger is not None:
        p_in, _p_out, _cr, _cw = price_for(model)
        search_worst = max_searches * (WEB_SEARCH_USD + EST_TOKENS_PER_SEARCH * p_in / 1_000_000)
        ledger.check(estimate_call_cost(model, system, user_content, max_tokens) + search_worst)

    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches,
              "user_location": AU_LOCATION}]
    messages: list[dict] = [{"role": "user", "content": user_content}]
    blocks: list = []
    for _attempt in range(MAX_PAUSE_RESUMES + 1):
        started = time.monotonic()
        with client.messages.stream(model=model, max_tokens=max_tokens, system=system,
                                    messages=messages, tools=tools) as stream:
            final = stream.get_final_message()
        cost = usage.record(final.usage, time.monotonic() - started)
        if ledger is not None:
            ledger.add(cost)
        blocks.extend(final.content)
        stop_reason = getattr(final, "stop_reason", None)
        if stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": final.content}]
    if stop_reason == "refusal":
        raise ClaudeOutputError("model refused the research request")

    text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text").strip()
    sources: dict[str, dict] = {}
    for b in blocks:
        if getattr(b, "type", None) == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
            for r in b.content:
                url = getattr(r, "url", None)
                if url and url not in sources:
                    sources[url] = {"url": url, "title": getattr(r, "title", None),
                                    "page_age": getattr(r, "page_age", None)}
    if stop_reason == "max_tokens":
        text += "\n\n[research notes truncated]"
    return text, list(sources.values())
