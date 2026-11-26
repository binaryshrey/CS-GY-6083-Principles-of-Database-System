"""Run the pipeline over benchmark questions for one or more configs.

    python scripts/run_experiment.py --experiment main-v1 --configs baseline,plan_guided
    python scripts/run_experiment.py --experiment pilot --limit 10 --dbs formula_1

Resumable: a (question, config, repeat) that already has a finished run in the
experiment is skipped, so an interrupted batch can simply be started again.
Runs left in 'running' by a crash are marked 'failed' before starting.
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from t2s import batch, db, pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--description")
    ap.add_argument("--configs", default="baseline,plan_guided")
    ap.add_argument("--dbs", help="comma-separated database names")
    ap.add_argument("--difficulty", choices=["simple", "moderate", "challenging"])
    ap.add_argument("--question-ids", help="comma-separated BIRD question ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4, help="parallel runs (LLM calls overlap; timing is serialized)")
    ap.add_argument("--freeze", action="store_true", help="freeze the experiment when the batch ends")
    a = ap.parse_args()

    try:
        eid, run_ids = batch.plan_batch(
        a.experiment, a.configs.split(","),
        dbs=a.dbs.split(",") if a.dbs else None, difficulty=a.difficulty,
        ids=[int(x) for x in a.question_ids.split(",")] if a.question_ids else None,
        limit=a.limit, repeats=a.repeats, description=a.description)
    except batch.BatchError as e:
        raise SystemExit(str(e))
    print(f"experiment {a.experiment} (id {eid}): {len(run_ids)} runs to execute", flush=True)

    t0, counts = time.time(), {}
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(pipeline.execute_run, rid): rid for rid in run_ids}
        for i, f in enumerate(as_completed(futures), start=1):
            try:
                status = f.result()
            except Exception as e:                 # execute_run already stores most failures
                status = f"error: {e}"
            counts[status] = counts.get(status, 0) + 1
            print(f"[{i}/{len(run_ids)}] run {futures[f]}: {status}  ({time.time() - t0:.0f}s)", flush=True)
    print("finished:", counts)

    if a.freeze:
        with db.app_conn() as conn:
            db.execute(conn, "UPDATE experiment SET is_frozen = TRUE WHERE experiment_id = %s", (eid,))
        print("experiment frozen")


if __name__ == "__main__":
    main()
