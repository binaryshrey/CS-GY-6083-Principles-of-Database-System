"""The Text-to-SQL pipeline: write the SQL, fix errors, then (plan-guided configs only)
rewrite it for speed using the EXPLAIN ANALYZE plan.

    initial ──(error)──> fix ──(error)──> fix ...          (max_fix_attempts)
       │ executes
       └──(plan issue or slow)──> rewrite ──> rewrite ...  (max_rewrite_attempts)

A rewrite is kept only if it returns the same multiset of rows as the query it
improves on AND its median runtime is at least SPEEDUP_MIN lower. The pipeline never
looks at gold SQL to make a decision; gold results are only used afterwards to fill
generated_query.matches_gold.

All rows of a run (queries, plans, issues, timings, LLM calls) are written in one
transaction at the end, so a crashed run never leaves half a chain behind.
"""
import re
import threading
import traceback
from dataclasses import dataclass, field

from psycopg.types.json import Jsonb

from . import config, costs, db, llm, plan_analysis, prompts, targets

_gold_cache = {}
_gold_lock = threading.Lock()


@dataclass
class Step:
    step_no: int
    parent_step_no: int | None
    stage: str
    sql_text: str
    explanation: str = ""
    executed_ok: bool = False
    error_message: str | None = None
    timeout: bool = False
    rows: list | None = None
    matches_gold: bool | None = None
    plan: list | None = None
    issues: set = field(default_factory=set)
    trials: list = field(default_factory=list)
    median_ms: float | None = None
    accepted: bool | None = None
    reject_reason: str | None = None


def _gold_rows(tconn, question_id, schema, gold_sql):
    with _gold_lock:
        if question_id in _gold_cache:
            return _gold_cache[question_id]
    _, rows, _ = targets.fetch_rows(tconn, schema, gold_sql)
    with _gold_lock:
        _gold_cache[question_id] = rows
    return rows


def _same_text(a, b):
    squash = lambda s: re.sub(r"\s+", " ", s or "").strip().rstrip(";").lower()
    return squash(a) == squash(b)


def needs_rewrite(step):
    return bool(step.issues) or (step.median_ms or 0) >= config.SLOW_MS


class RunContext:
    def __init__(self, run, tconn):
        self.run = run
        self.tconn = tconn
        self.schema = run["pg_schema"]
        self.system = prompts.system_text(run["db_name"], run["schema_prompt"] or "")
        self.steps: list[Step] = []
        self.calls = []          # (stage, input_tokens, output_tokens, latency_ms)
        self.unsaved_cost = 0.0  # spend of this run not yet in llm_call
        self.gold = None

    # ---- LLM ----
    def _bill(self, stage, tin, tout, lat):
        self.calls.append((stage, tin, tout, lat))
        if self.run["model_name"] in costs.PRICES:
            c = costs.call_cost(self.run["model_name"], tin, tout)
            self.unsaved_cost += c
            costs.add_unsaved(c)

    def ask(self, stage, user_text):
        costs.check(self.run["model_name"])          # hard spending cap
        try:
            r = llm.ask_sql(self.run["model_name"], self.run["effort"], self.system, user_text)
        except llm.LLMError as e:
            if e.usage:
                self._bill(stage, *e.usage)
            raise
        self._bill(stage, r.input_tokens, r.output_tokens, r.latency_ms)
        return r

    # ---- executing a candidate ----
    def evaluate(self, stage, parent, sql_raw, explanation):
        step = Step(step_no=len(self.steps) + 1, parent_step_no=parent.step_no if parent else None,
                    stage=stage, sql_text=(sql_raw or "").strip() or "(empty)", explanation=explanation)
        self.steps.append(step)
        try:
            sql_text = targets.clean_sql(sql_raw)
            step.sql_text = sql_text
            _, rows, _ = targets.fetch_rows(self.tconn, self.schema, sql_text)
            m = targets.measure(self.tconn, self.schema, sql_text)
        except targets.QueryError as e:
            step.error_message = str(e)[:2000]
            step.timeout = e.timeout
            return step
        step.executed_ok = True
        step.rows = rows
        step.plan = m["plan"]
        step.trials = m["trials"]
        step.median_ms = m["median_ms"]
        step.issues = plan_analysis.find_issues(m["plan"])
        if self.gold is not None:
            step.matches_gold = targets.same_set(rows, self.gold)
        return step


def run_pipeline(ctx: RunContext):
    run = ctx.run
    q, ev = run["question_text"], run["evidence_hint"]

    if run["source"] == "bird":
        ctx.gold = _gold_rows(ctx.tconn, run["question_id"], ctx.schema, run["gold_sql"])

    # 1. write the SQL
    r = ctx.ask("initial", prompts.initial(q, ev))
    cur = ctx.evaluate("initial", None, r.sql, r.explanation)

    # 2. fix errors
    fixes = 0
    while not cur.executed_ok and fixes < run["max_fix_attempts"]:
        r = ctx.ask("fix", prompts.fix(q, ev, cur.sql_text, cur.error_message))
        cur = ctx.evaluate("fix", cur, r.sql, r.explanation)
        fixes += 1
    if not cur.executed_ok:
        return None
    best = cur

    # 3. plan-guided rewrite
    attempts, tail, feedback = 0, best, None
    while attempts < run["max_rewrite_attempts"] and needs_rewrite(best):
        r = ctx.ask("rewrite", prompts.rewrite(
            q, ev, best.sql_text, plan_analysis.render(best.plan), best.median_ms, best.issues, feedback))
        if _same_text(r.sql, best.sql_text) or (feedback and _same_text(r.sql, feedback["sql"])):
            break                                   # model sees no faster equivalent
        cand = ctx.evaluate("rewrite", tail, r.sql, r.explanation)
        attempts += 1
        tail = cand
        if not cand.executed_ok:
            cand.accepted, cand.reject_reason = False, "error"
            feedback = {"reason": "it failed", "sql": cand.sql_text, "detail": cand.error_message}
        elif not targets.same_multiset(cand.rows, best.rows):
            cand.accepted, cand.reject_reason = False, "different_result"
            feedback = {"reason": "it returned different rows", "sql": cand.sql_text,
                        "detail": f"The original returns {len(best.rows)} rows, the attempt {len(cand.rows)}."}
        elif cand.median_ms > best.median_ms * (1 - config.SPEEDUP_MIN):
            cand.accepted, cand.reject_reason = False, "not_faster"
            feedback = {"reason": "it was not faster", "sql": cand.sql_text,
                        "detail": f"Median {cand.median_ms:.3f} ms vs {best.median_ms:.3f} ms."}
        else:
            cand.accepted = True
            best, feedback = cand, None
    return best


# ---------- persistence ----------

def create_run(conn, experiment_id, config_id, question_id, user_id=None):
    """Insert a 'running' run row with the next free repeat_no; returns run_id."""
    row = db.query_one(conn, """
        INSERT INTO run (experiment_id, config_id, question_id, repeat_no, status, user_id)
        SELECT %(e)s, %(c)s, %(q)s, COALESCE(max(repeat_no), 0) + 1, 'running', %(u)s
        FROM run WHERE experiment_id = %(e)s AND config_id = %(c)s AND question_id = %(q)s
        RETURNING run_id""", {"e": experiment_id, "c": config_id, "q": question_id, "u": user_id})
    return row["run_id"]


def _load_run(conn, run_id):
    return db.query_one(conn, """
        SELECT r.run_id, r.question_id, q.question_text, q.evidence_hint, q.gold_sql, q.source,
               d.db_id, d.db_name, d.pg_schema, d.schema_prompt,
               c.model_name, c.effort, c.max_fix_attempts, c.max_rewrite_attempts
        FROM run r
        JOIN question q        ON q.question_id = r.question_id
        JOIN target_database d ON d.db_id = q.db_id
        JOIN pipeline_config c ON c.config_id = r.config_id
        WHERE r.run_id = %s""", (run_id,))


def _save(conn, run, ctx, final, status, error_detail):
    rid = run["run_id"]
    with conn.cursor() as cur:
        for s in ctx.steps:
            cur.execute("""
                INSERT INTO generated_query (run_id, step_no, parent_step_no, stage, sql_text, executed_ok,
                    error_message, matches_gold, is_final, accepted, reject_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (rid, s.step_no, s.parent_step_no, s.stage, s.sql_text, s.executed_ok,
                 None if s.executed_ok else (s.error_message or "error"),
                 s.matches_gold if s.executed_ok else None,
                 final is not None and s.step_no == final.step_no,
                 s.accepted, s.reject_reason))
            if s.plan is not None:
                cf = plan_analysis.cost_fields(s.plan)
                cur.execute("""INSERT INTO query_plan (run_id, step_no, plan_json, startup_cost, total_cost, est_rows)
                               VALUES (%s,%s,%s,%s,%s,%s)""",
                            (rid, s.step_no, Jsonb(s.plan), cf["startup_cost"], max(cf["total_cost"], cf["startup_cost"]),
                             max(cf["est_rows"], 0)))
                for issue in sorted(s.issues):
                    cur.execute("INSERT INTO plan_issue (run_id, step_no, issue) VALUES (%s,%s,%s)",
                                (rid, s.step_no, issue))
            for trial_no, ms, warm in s.trials:
                cur.execute("""INSERT INTO timing_trial (run_id, step_no, trial_no, runtime_ms, is_warmup)
                               VALUES (%s,%s,%s,%s,%s)""", (rid, s.step_no, trial_no, round(ms, 3), warm))
        for i, (stage, tin, tout, lat) in enumerate(ctx.calls, start=1):
            cur.execute("""INSERT INTO llm_call (run_id, call_no, stage, input_tokens, output_tokens, latency_ms)
                           VALUES (%s,%s,%s,%s,%s,%s)""", (rid, i, stage, tin, tout, lat))
        # a user question records which tables its final query touched
        if run["source"] == "user" and final is not None:
            for rel in sorted(plan_analysis.relations(final.plan)):
                cur.execute("""INSERT INTO question_table (question_id, db_id, table_name)
                               SELECT %s, %s, table_name FROM target_table
                               WHERE db_id = %s AND table_name = %s ON CONFLICT DO NOTHING""",
                            (run["question_id"], run["db_id"], run["db_id"], rel))
        cur.execute("UPDATE run SET status = %s, finished_at = now(), error_detail = %s WHERE run_id = %s",
                    (status, error_detail, rid))


def execute_run(run_id):
    """Run the pipeline for an existing 'running' run row and store everything."""
    with db.app_conn() as conn:
        run = _load_run(conn, run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")

    tconn = targets.connect()
    ctx = RunContext(run, tconn)
    final, error_detail = None, None
    try:
        final = run_pipeline(ctx)
    except costs.BudgetExceeded as e:
        error_detail = str(e)
    except (llm.LLMError, targets.QueryError) as e:
        error_detail = f"{type(e).__name__}: {e}"
    except Exception as e:                       # keep the run row consistent, log the rest
        error_detail = f"internal error: {e}\n{traceback.format_exc()[-1500:]}"
    finally:
        tconn.close()

    if final is not None:
        status = "done"
    elif ctx.steps and ctx.steps[-1].timeout:
        status = "timeout"
    else:
        status = "failed"
    if final is None and error_detail is None and ctx.steps:
        error_detail = f"no query executed successfully after {len(ctx.steps)} attempt(s)"

    try:
        with db.app_conn() as conn:
            _save(conn, run, ctx, final, status, error_detail)
    finally:
        costs.release(ctx.unsaved_cost)
    return status
