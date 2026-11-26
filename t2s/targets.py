"""Running SQL against the BIRD target database.

Every candidate query (LLM-written or gold) runs inside a READ ONLY transaction,
as the llm_reader role, with a statement timeout, and is always rolled back.
Queries are sent with the extended protocol, which rejects multiple statements,
so "SELECT 1; DROP TABLE x" fails before anything runs.
"""
import math
import statistics
import threading
from collections import Counter
from decimal import Decimal

import psycopg
from psycopg import sql as pgsql

from . import config

MAX_SQL_CHARS = 20000

# Only one query is timed at a time in this process, so parallel runs (LLM calls
# overlap freely) do not inflate each other's measured runtimes.
_timing_lock = threading.Lock()


class QueryError(Exception):
    def __init__(self, message, timeout=False):
        super().__init__(message)
        self.timeout = timeout


def connect():
    return psycopg.connect(config.TARGET_DSN)


def clean_sql(text):
    """Strip whitespace, code fences and trailing semicolons; reject empty/oversized input."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("sql"):
            s = s[3:]
    s = s.strip().rstrip(";").strip()
    if not s:
        raise QueryError("empty SQL")
    if len(s) > MAX_SQL_CHARS:
        raise QueryError(f"SQL longer than {MAX_SQL_CHARS} characters")
    return s


def _run(conn, schema, sql_text, consume, timeout_ms=None):
    """Execute one statement read-only as llm_reader and pass the cursor to consume()."""
    timeout_ms = timeout_ms or config.STATEMENT_TIMEOUT_MS
    # params=[] forces the extended query protocol (single statement only);
    # literal % signs must then be doubled for psycopg's placeholder parser.
    body = sql_text.replace("%", "%%")
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            cur.execute("SET LOCAL ROLE llm_reader")
            cur.execute(pgsql.SQL("SET LOCAL statement_timeout = {}").format(pgsql.Literal(int(timeout_ms))))
            cur.execute(pgsql.SQL("SET LOCAL search_path = {}").format(pgsql.Identifier(schema)))
            cur.execute(body, [])
            return consume(cur)
    except psycopg.errors.QueryCanceled as e:
        raise QueryError(f"statement timeout after {timeout_ms} ms", timeout=True) from e
    except psycopg.Error as e:
        msg = (e.diag.message_primary if e.diag and e.diag.message_primary else str(e)).strip()
        if e.diag and e.diag.message_hint:
            msg += f" (hint: {e.diag.message_hint})"
        raise QueryError(msg) from e
    finally:
        conn.rollback()


def fetch_rows(conn, schema, sql_text):
    """Return (column names, rows, truncated)."""
    def consume(cur):
        cols = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchmany(config.MAX_RESULT_ROWS + 1)
        return cols, rows[: config.MAX_RESULT_ROWS], len(rows) > config.MAX_RESULT_ROWS
    return _run(conn, schema, sql_text, consume)


def explain_analyze(conn, schema, sql_text):
    """EXPLAIN ANALYZE (TIMING OFF keeps per-node overhead out of the measurement)."""
    def consume(cur):
        return cur.fetchone()[0]
    return _run(conn, schema, "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + sql_text, consume)


def measure(conn, schema, sql_text, warmups=None, timed=None):
    """Run the query warmups+timed times under EXPLAIN ANALYZE.

    Returns {"plan": plan_json of the last run, "trials": [(trial_no, ms, is_warmup)],
    "median_ms": median of the timed runs}.
    """
    warmups = config.WARMUP_TRIALS if warmups is None else warmups
    timed = config.TIMED_TRIALS if timed is None else timed
    trials, plan = [], None
    with _timing_lock:
        for i in range(1, warmups + timed + 1):
            plan = explain_analyze(conn, schema, sql_text)
            trials.append((i, float(plan[0]["Execution Time"]), i <= warmups))
    return {
        "plan": plan,
        "trials": trials,
        "median_ms": statistics.median(ms for _, ms, w in trials if not w),
    }


# ---------- result comparison ----------

def _norm(v):
    if isinstance(v, (float, Decimal)):
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return str(f)
        return round(f, 6)
    if isinstance(v, (list, tuple)):
        return tuple(_norm(x) for x in v)
    return v


def normalize(rows):
    return [tuple(_norm(v) for v in r) for r in rows]


def same_set(a, b):
    """BIRD's execution-accuracy rule: the two results are equal as sets of rows."""
    return set(normalize(a)) == set(normalize(b))


def same_multiset(a, b):
    """Stricter check used to accept rewrites: same rows with the same multiplicities."""
    return Counter(normalize(a)) == Counter(normalize(b))


# ---------- schema description used in prompts ----------

def _fmt_val(v, width=40):
    s = repr(v) if not isinstance(v, str) else v
    s = s.replace("\n", " ")
    return s if len(s) <= width else s[: width - 3] + "..."


def build_schema_prompt(conn, schema, tables, descriptions=None, sample_rows=3):
    """Text description of the given tables: columns, types, keys, BIRD column notes,
    and a few example rows. Generated once by the loader and stored, so every run of
    an experiment sees exactly the same schema text."""
    descriptions = descriptions or {}
    out = []
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(
                """SELECT c.column_name, c.data_type
                   FROM information_schema.columns c
                   WHERE c.table_schema = %s AND c.table_name = %s
                   ORDER BY c.ordinal_position""", (schema, t))
            cols = cur.fetchall()
            cur.execute(
                """SELECT kcu.column_name
                   FROM information_schema.table_constraints tc
                   JOIN information_schema.key_column_usage kcu
                     ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
                   WHERE tc.table_schema = %s AND tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'""",
                (schema, t))
            pk = [r[0] for r in cur.fetchall()]
            cur.execute(
                """SELECT a.attname, cf.relname, af.attname
                   FROM pg_constraint con
                   JOIN pg_class c   ON c.oid = con.conrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   JOIN pg_class cf  ON cf.oid = con.confrelid
                   JOIN LATERAL unnest(con.conkey, con.confkey) AS k(src, dst) ON true
                   JOIN pg_attribute a  ON a.attrelid = con.conrelid AND a.attnum = k.src
                   JOIN pg_attribute af ON af.attrelid = con.confrelid AND af.attnum = k.dst
                   WHERE con.contype = 'f' AND n.nspname = %s AND c.relname = %s""", (schema, t))
            fks = cur.fetchall()
            cur.execute(pgsql.SQL("SELECT * FROM {}.{} LIMIT {}").format(
                pgsql.Identifier(schema), pgsql.Identifier(t), pgsql.Literal(sample_rows)))
            samples = cur.fetchall()
            cur.execute("SELECT reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = %s AND c.relname = %s", (schema, t))
            est = cur.fetchone()[0]

            notes = descriptions.get(t, {})
            out.append(f"TABLE {t}  (~{max(est, 0)} rows)")
            for name, dtype in cols:
                line = f"  {name} {dtype}"
                if name in pk:
                    line += " PRIMARY KEY" if len(pk) == 1 else " (part of PRIMARY KEY)"
                note = notes.get(name.lower())
                if note:
                    line += f"  -- {note}"
                out.append(line)
            for col, rt, rc in fks:
                out.append(f"  FOREIGN KEY ({col}) REFERENCES {rt}({rc})")
            if samples:
                out.append("  example rows:")
                for r in samples:
                    out.append("    (" + ", ".join(_fmt_val(v) for v in r) + ")")
            out.append("")
    return "\n".join(out)
