"""Settings read from the environment (or a .env file in the project root)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Results database (schema t2s) and the database holding the BIRD tables.
APP_DSN = os.environ.get("APP_DSN", "dbname=text2sql")
TARGET_DSN = os.environ.get("TARGET_DSN", "dbname=bird")

SECRET_KEY = os.environ.get("SECRET_KEY", "")

# LLM-written SQL limits
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "30000"))
MAX_RESULT_ROWS = int(os.environ.get("MAX_RESULT_ROWS", "200000"))

# Timing protocol: warm-up runs (recorded, excluded from the median) + timed runs.
# The timed boundary is PostgreSQL's own "Execution Time" from EXPLAIN ANALYZE,
# so it excludes network transfer, parsing in Python and LLM latency.
WARMUP_TRIALS = int(os.environ.get("WARMUP_TRIALS", "1"))
TIMED_TRIALS = int(os.environ.get("TIMED_TRIALS", "5"))

# Rewrite stage: a query is a rewrite candidate if its plan shows an issue or its
# median is at least SLOW_MS; a rewrite is kept only if it is SPEEDUP_MIN faster.
SLOW_MS = float(os.environ.get("SLOW_MS", "50"))
SPEEDUP_MIN = float(os.environ.get("SPEEDUP_MIN", "0.10"))

# Plan-issue thresholds (rows actually read / loops actually executed)
LARGE_SCAN_ROWS = int(os.environ.get("LARGE_SCAN_ROWS", "100000"))
NESTED_LOOP_LOOPS = int(os.environ.get("NESTED_LOOP_LOOPS", "10000"))

DEFAULT_MODEL = os.environ.get("T2S_MODEL", "claude-haiku-5-5")
DEFAULT_EFFORT = os.environ.get("T2S_EFFORT", "medium")

# Hard cap on Claude API spend in USD across all runs (0 = no cap); see t2s/costs.py
BUDGET_USD = float(os.environ.get("T2S_BUDGET_USD", "3"))

# Background workers for runs launched from the web app
WEB_WORKERS = int(os.environ.get("WEB_WORKERS", "2"))
