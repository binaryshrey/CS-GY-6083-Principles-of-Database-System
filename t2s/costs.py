"""Hard spending cap on Claude API calls.

Spend = tokens logged in llm_call priced per model, plus calls made in this process
whose run is not saved yet. Before every call the pipeline checks that spend plus the
worst case for one more call (max_tokens of output) stays under T2S_BUDGET_USD.
Input tokens are priced at 1.25x the input rate, the cache-write rate, so the estimate
errs high (cache reads actually cost 0.1x). T2S_BUDGET_USD=0 disables the cap.

Limit: two processes running at once (web app + batch script) each know only their own
unsaved calls, so the overshoot is bounded by the calls in flight, not zero.
"""
import threading

from . import config, db

# USD per million tokens (input, output); Anthropic list prices, prompts <= 100K tokens
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
INPUT_FACTOR = 1.25
MAX_OUTPUT_TOKENS = 16000          # matches max_tokens in llm.py
MAX_INPUT_TOKENS = 20000           # generous bound for one prompt (schema + plan)

_lock = threading.Lock()
_unsaved = 0.0


class BudgetExceeded(Exception):
    pass


def call_cost(model, input_tokens, output_tokens):
    p_in, p_out = PRICES[model]
    return (input_tokens * p_in * INPUT_FACTOR + output_tokens * p_out) / 1e6


def logged_spend():
    with db.app_conn() as conn:
        rows = db.query(conn, """
            SELECT c.model_name, sum(l.input_tokens) AS tin, sum(l.output_tokens) AS tout
            FROM llm_call l JOIN run r ON r.run_id = l.run_id
            JOIN pipeline_config c ON c.config_id = r.config_id
            GROUP BY c.model_name""")
    total = 0.0
    for r in rows:
        if r["model_name"] in PRICES:
            total += call_cost(r["model_name"], r["tin"], r["tout"])
    return total


def spend():
    return logged_spend() + _unsaved


def remaining():
    return None if config.BUDGET_USD <= 0 else config.BUDGET_USD - spend()


def check(model):
    """Raise BudgetExceeded unless one more worst-case call fits under the cap."""
    if config.BUDGET_USD <= 0:
        return
    if model not in PRICES:
        raise BudgetExceeded(f"no price known for {model}; add it to t2s/costs.py or set T2S_BUDGET_USD=0")
    worst = call_cost(model, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS)
    with _lock:
        spent = logged_spend() + _unsaved
    if spent + worst > config.BUDGET_USD:
        raise BudgetExceeded(f"budget cap reached: ${spent:.2f} spent of ${config.BUDGET_USD:.2f} "
                             "(raise T2S_BUDGET_USD in .env to continue)")


def add_unsaved(amount):
    global _unsaved
    with _lock:
        _unsaved += amount


def release(amount):
    """Called once a run's llm_call rows are committed (they now count in logged_spend)."""
    global _unsaved
    with _lock:
        _unsaved = max(0.0, _unsaved - amount)
