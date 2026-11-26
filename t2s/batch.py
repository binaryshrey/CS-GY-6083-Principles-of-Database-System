"""Planning a batch of runs: one 'running' run row per (question, config, repeat) still missing."""
from . import db, pipeline


class BatchError(ValueError):
    pass


def select_questions(conn, dbs, difficulty, ids, limit):
    sql = """SELECT q.question_id FROM question q JOIN target_database d ON d.db_id = q.db_id
             WHERE q.source = 'bird'
               AND (%(dbs)s::text[] IS NULL OR d.db_name = ANY(%(dbs)s))
               AND (%(diff)s::text IS NULL OR q.difficulty = %(diff)s)
               AND (%(ids)s::int[] IS NULL OR q.question_id = ANY(%(ids)s))
             ORDER BY q.question_id"""
    rows = db.query(conn, sql, {"dbs": dbs, "diff": difficulty, "ids": ids})
    qids = [r["question_id"] for r in rows]
    return qids[:limit] if limit else qids


def plan_batch(experiment, configs, dbs=None, difficulty=None, ids=None, limit=None, repeats=1,
               description=None, user_id=None):
    """Create the experiment if needed and insert one 'running' run per missing job.
    Returns (experiment_id, [run_id, ...])."""
    with db.app_conn() as conn:
        exp = db.query_one(conn, "SELECT experiment_id, is_frozen FROM experiment WHERE name = %s", (experiment,))
        if exp is None:
            exp = db.query_one(conn, """INSERT INTO experiment (name, description) VALUES (%s, %s)
                                        RETURNING experiment_id, is_frozen""", (experiment, description))
        if exp["is_frozen"]:
            raise BatchError(f"experiment {experiment} is frozen")
        eid = exp["experiment_id"]
        db.execute(conn, """UPDATE run SET status = 'failed', finished_at = now(),
                                   error_detail = 'interrupted before completion'
                            WHERE experiment_id = %s AND status = 'running'""", (eid,))
        cfgs = db.query(conn, "SELECT config_id, label FROM pipeline_config WHERE label = ANY(%s)", (configs,))
        missing = set(configs) - {c["label"] for c in cfgs}
        if missing:
            raise BatchError(f"unknown config(s): {', '.join(sorted(missing))}")
        qids = select_questions(conn, dbs, difficulty, ids, limit)
        run_ids = []
        for qid in qids:
            for c in cfgs:
                done = db.query_one(conn, """SELECT count(*) AS n FROM run WHERE experiment_id = %s AND config_id = %s
                                             AND question_id = %s AND status <> 'failed'""",
                                    (eid, c["config_id"], qid))["n"]
                for _ in range(max(0, repeats - done)):
                    run_ids.append(pipeline.create_run(conn, eid, c["config_id"], qid, user_id))
    return eid, run_ids
