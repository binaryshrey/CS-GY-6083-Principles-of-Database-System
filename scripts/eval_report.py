"""Write the evaluation tables for one experiment as Markdown (for the final report).

    python scripts/eval_report.py --experiment main-v1 > docs/evaluation_results.md

Uses the same SQL as the web app's experiment page (webapp/queries.py), plus the
environment details the report must record (hardware, versions, corpus, settings).
"""
import argparse
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from t2s import config, db  # noqa: E402
from webapp import queries as Q  # noqa: E402


def table(rows, cols=None):
    if not rows:
        return "_no rows_\n"
    cols = cols or list(rows[0].keys())
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("" if r[c] is None else str(r[c]).replace("|", "\\|").replace("\n", " ")
                                     for c in cols) + " |")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--examples", type=int, default=5)
    a = ap.parse_args()

    conn = db.connect()
    e = db.query_one(conn, "SELECT * FROM experiment WHERE name = %s", (a.experiment,))
    if e is None:
        raise SystemExit(f"no experiment {a.experiment}")
    p = {"experiment_id": e["experiment_id"]}
    pg = db.query_one(conn, "SELECT version() AS v")["v"]
    try:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    except OSError:
        chip = platform.processor()

    print(f"# Evaluation results: experiment `{e['name']}`\n")
    print(f"_{e['description'] or ''}_  \nStarted {e['started_at']:%Y-%m-%d %H:%M}, frozen: {e['is_frozen']}\n")

    print("## Setup\n")
    corpus = db.query(conn, """SELECT d.db_name, c.question_count AS questions, c.table_count AS tables, d.size_mb
                               FROM target_database d JOIN v_database_counts c USING (db_id) ORDER BY d.db_id""")
    print(f"- Hardware: {chip}, {platform.system()} {platform.release()}")
    print(f"- PostgreSQL: {pg.split(',')[0]}; Python {platform.python_version()}; anthropic SDK {anthropic.__version__}")
    print(f"- Timing: {config.WARMUP_TRIALS} warm-up + {config.TIMED_TRIALS} timed runs per query; median of timed runs; "
          "boundary = PostgreSQL `Execution Time` from `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)` "
          "(excludes network, client and LLM time); one query timed at a time")
    print(f"- Rewrite trigger: plan issue or median >= {config.SLOW_MS} ms; accepted if same multiset of rows and "
          f">= {config.SPEEDUP_MIN:.0%} faster than its base query")
    print(f"- Slow question: gold median >= {Q.SLOW_QUESTION_MS} ms\n")
    print(table(corpus))
    print(table(db.query(conn, Q.CONFIGS, {}),
                ["label", "model_name", "effort", "prompt_version", "max_fix_attempts", "max_rewrite_attempts"]))

    print("## Accuracy, speed and cost per config\n")
    print(table(db.query(conn, Q.REPORT_SUMMARY, p)))
    print("## Slow vs fast questions\n")
    print(table(db.query(conn, Q.REPORT_SLOW_FAST, {**p, "slow_ms": Q.SLOW_QUESTION_MS})))
    print("## Head to head on the same questions\n")
    print(table(db.query(conn, Q.REPORT_HEAD_TO_HEAD, p)))
    print("## Effect of the rewrite stage (same run, before vs after)\n")
    print(table(db.query(conn, Q.REPORT_REWRITE_EFFECT, p)))
    print("## Rewrite outcomes\n")
    print(table(db.query(conn, Q.REPORT_REWRITES, p)))
    print("## Plan issues before rewriting vs in the final query\n")
    print(table(db.query(conn, Q.REPORT_ISSUES, p)))
    print("## By database\n")
    print(table(db.query(conn, Q.REPORT_BY_DB, p)))
    print("## LLM calls by stage\n")
    print(table(db.query(conn, """
        SELECT c.label, l.stage, count(*) AS calls, round(avg(l.input_tokens)) AS avg_in,
               round(avg(l.output_tokens)) AS avg_out, round(avg(l.latency_ms)) AS avg_latency_ms
        FROM llm_call l JOIN run r ON r.run_id = l.run_id JOIN pipeline_config c ON c.config_id = r.config_id
        WHERE r.experiment_id = %(experiment_id)s GROUP BY 1, 2 ORDER BY 1, 2""", p)))
    print("## Run status\n")
    print(table(db.query(conn, """SELECT c.label, r.status, count(*) AS runs FROM run r
                                  JOIN pipeline_config c ON c.config_id = r.config_id
                                  WHERE r.experiment_id = %(experiment_id)s GROUP BY 1, 2 ORDER BY 1, 2""", p)))

    print("## Largest accepted speedups (before / after)\n")
    for s in db.query(conn, Q.REPORT_SPEEDUPS, {**p, "limit": a.examples}):
        print(f"### Run {s['run_id']} (question {s['question_id']}, {s['label']}): {s['speedup']}x, "
              f"{s['before_ms']} -> {s['after_ms']} ms, issues before: {s['issues_before'] or 'none'}, "
              f"matches gold: {s['matches_gold']}\n")
        print(f"Before:\n```sql\n{s['before_sql']}\n```\nAfter:\n```sql\n{s['after_sql']}\n```\n")


if __name__ == "__main__":
    main()
