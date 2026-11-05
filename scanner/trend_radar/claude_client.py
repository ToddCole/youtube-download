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

import os
import time
from dataclasses import dataclass

import anthropic

DEFAULT_MODEL = "claude-sonnet-5-5"


class MissingApiKeyError(RuntimeError):
    """Raised when ANTHROPIC_API_KEY is unset. Never includes any key
    value in its message -- there is none available to include."""


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
    elapsed_seconds: float = 0.0

    def record(self, usage, elapsed: float) -> None:
        self.api_calls += 1
        self.input_tokens += int(usage.input_tokens)
        self.output_tokens += int(usage.output_tokens)
        self.elapsed_seconds += elapsed

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


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
):
    """One structured-output call via the native Anthropic SDK's
    Structured Outputs facility (messages.parse + output_format).

    No tools, no tool_choice, no assistant-role prefill message (the
    only message sent is a single user turn), and no explicit
    temperature/top_p/top_k -- every sampling parameter is left at the
    SDK's default. Raises ValueError if the response has no parsed
    structured output (should not happen with output_format set, but is
    checked explicitly rather than letting a None propagate silently)."""
    started = time.monotonic()
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        output_format=output_format,
    )
    elapsed = time.monotonic() - started
    usage.record(response.usage, elapsed)
    if response.parsed_output is None:
        raise ValueError("Claude response did not include a parsed structured output")
    return response.parsed_output
