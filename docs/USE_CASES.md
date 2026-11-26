# Use cases and the queries they run

Query names refer to `webapp/queries.py` (web app) and `t2s/*.py` (pipeline). Every
statement uses bound parameters (`%(name)s`). "Server check" lists what is enforced on
the server, independent of what the page shows.

Users: **analyst** (self-registers) and **researcher** (created only by
`scripts/create_user.py`). Both must log in for everything except the public home page.

---

## Public (not logged in)

### U1. Home page: purpose and available databases — `GET /`
`PUBLIC_DATABASES`: target databases with their derived counts from Part 2's view.
```sql
SELECT d.db_name, d.domain, d.size_mb, c.question_count, c.table_count
FROM target_database d JOIN v_database_counts c ON c.db_id = d.db_id ORDER BY d.db_id
```
Logged-in users are redirected to their home page.

### U2. Register (analyst only) — `GET/POST /register`
`INSERT_ANALYST`: `INSERT INTO app_user (email, name, password_hash, role) VALUES (..., 'analyst')`.
Server check: role is a constant in the SQL (a `role` form field is ignored); email lower-cased
and format-checked; `UNIQUE(email)` violation becomes "account already exists"; password 8–200
chars, confirmed, stored as a scrypt hash.

### U3. Login — `GET/POST /login`
`USER_BY_EMAIL`, then `check_password_hash`. On success the session is cleared and
`user_id` stored; redirect to `/home`. On failure the same message ("wrong email or password")
is shown whether or not the email exists.

## Every logged-in request
`USER_BY_ID` re-reads the user and role from `app_user`; a deleted account loses its session.
POSTs without the session's CSRF token are rejected (400).

### U4. Home page by role — `GET /home`
`MY_RUNS` (limit 5) for both roles; analysts also get `DATABASES_WITH_QUESTIONS`, `CONFIGS`
(quick ask form); researchers get `EXPERIMENTS`. Errors from the previous action are shown
as flash messages.

### U5. Logout — `POST /logout`
Session cleared; "goodbye" page.

---

## Analyst use cases (researchers can do all of these too)

### U6. Ask a question in English — `GET/POST /ask`
1. Validate: database exists (`DATABASE_EXISTS`), config exists (`CONFIG_EXISTS`), question
   1–1000 chars, evidence ≤ 1000 chars, fewer than 3 runs in progress (`RUNNING_FOR_USER`),
   playground experiment exists and is not frozen (`PLAYGROUND_ID`).
2. `INSERT_USER_QUESTION`: `INSERT INTO question (... source) VALUES (nextval('user_question_seq'), ..., 'user', user_id)`.
3. `pipeline.create_run`: next free `repeat_no`, status `running`, `user_id` = caller.
   ```sql
   INSERT INTO run (experiment_id, config_id, question_id, repeat_no, status, user_id)
   SELECT %(e)s, %(c)s, %(q)s, COALESCE(max(repeat_no), 0) + 1, 'running', %(u)s
   FROM run WHERE experiment_id = %(e)s AND config_id = %(c)s AND question_id = %(q)s
   RETURNING run_id
   ```
   The Part 2 trigger `run_frozen_check` rejects the insert if the experiment is frozen.
4. The pipeline (P1 below) runs in a background thread; the run page refreshes until done.

### U7. Browse benchmark questions — `GET /questions`
`QUESTIONS` with optional filters (database, difficulty, table read by the gold query,
text contains). Filters are bound parameters; `NULL` means "no filter":
```sql
... WHERE q.source = 'bird'
  AND (%(db_id)s::int IS NULL OR q.db_id = %(db_id)s)
  AND (%(table)s::text IS NULL OR EXISTS (SELECT 1 FROM question_table qt
        WHERE qt.question_id = q.question_id AND qt.table_name = %(table)s))
  AND (%(text)s::text IS NULL OR q.question_text ILIKE '%%' || %(text)s || '%%')
```

### U8. View a question and run the pipeline on it — `GET /questions/<id>`, `POST /questions/<id>/run`
`QUESTION`, `QUESTION_TABLES`, `GOLD_TRIALS` (gold timing trials, warm-up marked),
`QUESTION_RUNS`. Runs shown: batch experiments for everyone, playground runs only to their
owner (researchers see all). Server check: user-written questions are visible only to their
author and researchers; only `source='bird'` questions can be launched here. Launch = steps
3–4 of U6.

### U9. View one run — `GET /runs/<id>`
`RUN` (run + question + config + `v_run_summary` + `v_run_rves`), `RUN_STEPS` (the attempt
chain with median, cost, plan JSON and issues), `RUN_CALLS`. Server check
(`can_view_run`): researcher, or the run's owner, or a run outside the playground; else 404.

### U10. Run history — `GET /runs`
`MY_RUNS` with `WHERE r.user_id = %(user_id)s`: only the caller's own runs, with config,
status, correctness, final median, gold median and tokens.

---

## Researcher use cases (`researcher_required`: role re-checked from the database, 403 otherwise)

### U11. Experiments — `GET /experiments`
`EXPERIMENTS`: run counts by status per experiment. Experiments are created, run and frozen
with `scripts/run_experiment.py` (P3), so the web pages are read-only.

### U12. Experiment report — `GET /experiments/<id>`
| Query | Shows |
|---|---|
| `REPORT_SUMMARY` | per config: runs, EX %, R-VES, avg runtime ÷ gold (correct runs), median ms, avg tokens, LLM calls, LLM seconds |
| `REPORT_SLOW_FAST` | the same split by slow (gold ≥ 10 ms) vs fast questions |
| `REPORT_REWRITE_EFFECT` | the rewrite stage inside the same run: query before vs final query (correct, runtime, speedup) |
| `REPORT_REWRITES` | rewrite outcomes from `v_rewrite_outcome`: accepted, different_result, not_faster, error, broke |
| `REPORT_SPEEDUPS` | largest accepted speedups with before/after SQL |
| `EXPERIMENT_RUNS` | latest 200 runs, filterable by status |

R-VES (`v_run_rves`): τ = gold median ÷ final median; reward 1.25 (τ ≥ 2), 1 (≥ 1),
0.75 (≥ 0.5), 0.5 (≥ 0.25), 0.25 (< 0.25), 0 if the final result is wrong; R-VES = 100 × mean reward.

---

## P1. The pipeline (one run) — `t2s/pipeline.py`

1. `_load_run`: run + question + target database (schema name, stored schema prompt) + config.
2. Gold result (benchmark questions only, cached per process): `targets.fetch_rows(gold_sql)`.
3. **Initial**: one Claude call → SQL. **Fix**: while the query fails and attempts remain,
   send the SQL and PostgreSQL's error back.
4. **Execute** each candidate (`t2s/targets.py`), always in this wrapper and then rolled back:
   ```sql
   BEGIN READ ONLY;
   SET LOCAL ROLE llm_reader;
   SET LOCAL statement_timeout = 30000;
   SET LOCAL search_path = <database schema>;
   <candidate SQL>                      -- extended protocol: exactly one statement
   ROLLBACK;
   ```
   then 1 warm-up + 5 timed runs of `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) <candidate>`;
   the median of PostgreSQL's `Execution Time` is the runtime. The last plan is stored and
   scanned for issues.
5. **Rewrite** (configs with `max_rewrite_attempts > 0`), when the plan has an issue or the
   median is ≥ 50 ms: send the question, the current best SQL, its compact plan, median and
   issues. A candidate is **accepted** only if it returns the same multiset of rows as the
   current best and its median is ≥ 10 % lower; otherwise it is rejected (`error`,
   `different_result`, `not_faster`) and the next attempt gets that feedback. The pipeline
   never looks at gold to decide.
6. `matches_gold` for each executed step = set equality with the gold result (BIRD's rule).
7. `_save`, one transaction: `INSERT INTO generated_query` (in step order, so the Revises FK
   `(run_id, parent_step_no)` is satisfied; exactly one `is_final`, enforced by the Part 2
   partial unique index), `query_plan`, `plan_issue`, `timing_trial`, `llm_call`, for user
   questions `question_table` (tables read by the final plan), then
   `UPDATE run SET status, finished_at, error_detail`.

## P2. Rebuild — `scripts/load_bird.py`
`01_schema.sql`, `02_part3_migration.sql`; filtered dump load; `ALTER TABLE public.t SET SCHEMA db`;
`GRANT USAGE/SELECT … TO llm_reader`; `INSERT` into `target_database`, `target_table`
(`count(*)`, `pg_total_relation_size`), `question`, `question_table` (relations in the gold
plan), `gold_timing` (same timing protocol as P1), `pipeline_config`, `experiment`.

## P3. Batch experiments — `scripts/run_experiment.py`
`batch.plan_batch`: creates the experiment if needed (`INSERT INTO experiment`), marks runs left
`running` by a crash as failed, selects the questions, and inserts one `running` run per missing
(question, config, repeat) with `pipeline.create_run`; each then goes through P1 (4 in parallel,
timing serialized). `--freeze` ends with `UPDATE experiment SET is_frozen = TRUE`, after which the
Part 2 trigger rejects new runs.
