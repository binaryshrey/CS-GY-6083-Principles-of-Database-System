"""Claude client used by the pipeline. One function: ask for a SQL query.

Responses are constrained to JSON {"sql": ..., "explanation": ...} with structured
outputs, so no code-fence parsing is needed. The system prompt (rules + schema of
the target database) is identical for every question of a database and is marked
for prompt caching.
"""
import json
import time
from dataclasses import dataclass

import anthropic

_client = None

# Server-side refusal fallback is supported on these models (Claude API only)
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "One PostgreSQL SELECT statement, no trailing semicolon."},
        "explanation": {"type": "string", "description": "One or two sentences on the approach or the change."},
    },
    "required": ["sql", "explanation"],
    "additionalProperties": False,
}


class LLMError(Exception):
    """usage = (input_tokens, output_tokens, latency_ms) when the call was billed."""
    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage


@dataclass
class LLMResult:
    sql: str
    explanation: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model: str


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()   # credentials from ANTHROPIC_API_KEY / ant auth profile
    return _client


def ask_sql(model, effort, system_text, user_text):
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SQL_SCHEMA}},
    )
    if model in FALLBACK_MODELS:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"

    t0 = time.perf_counter()
    try:
        resp = client().beta.messages.create(**kwargs)
    except anthropic.AuthenticationError as e:
        raise LLMError("Anthropic API authentication failed - set ANTHROPIC_API_KEY") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Anthropic API rate limit hit; retry later") from e
    except anthropic.BadRequestError as e:
        raise LLMError(f"Anthropic API rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("could not reach the Anthropic API") from e
    latency_ms = int((time.perf_counter() - t0) * 1000)

    u = resp.usage
    in_tok = (u.input_tokens or 0) + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
    out_tok = u.output_tokens or 0

    billed = (in_tok, out_tok, latency_ms)
    if resp.stop_reason == "refusal":
        raise LLMError("model declined the request", billed)
    if resp.stop_reason == "max_tokens":
        raise LLMError("model response hit max_tokens", billed)
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMError("model returned no text block", billed)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("model returned invalid JSON", billed) from e

    return LLMResult(sql=data.get("sql", ""), explanation=data.get("explanation", ""),
                     input_tokens=in_tok, output_tokens=out_tok, latency_ms=latency_ms,
                     model=resp.model)
