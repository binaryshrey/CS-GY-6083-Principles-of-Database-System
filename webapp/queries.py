"""Every SQL statement the web app runs, by name. All take bound parameters (%(name)s);
nothing is spliced into SQL text. docs/USE_CASES.md maps each use case to these names."""

# A question is "slow" when its gold query's median runtime is at least this many ms.
SLOW_QUESTION_MS = 10

# ---------- public / accounts ----------

PUBLIC_DATABASES = """
SELECT d.db_name, d.domain, d.size_mb, c.question_count, c.table_count
FROM target_database d JOIN v_database_counts c ON c.db_id = d.db_id
ORDER BY d.db_id"""

USER_BY_EMAIL = "SELECT user_id, email, name, password_hash, role FROM app_user WHERE email = %(email)s"

USER_BY_ID = "SELECT user_id, email, name, role FROM app_user WHERE user_id = %(user_id)s"

# role is fixed to 'analyst': the form has no role field and none is read
INSERT_ANALYST = """
INSERT INTO app_user (email, name, password_hash, role)
VALUES (%(email)s, %(name)s, %(password_hash)s, 'analyst')
RETURNING user_id"""

# ---------- shared lookups ----------

DATABASES_WITH_QUESTIONS = """
SELECT d.db_id, d.db_name FROM target_database d ORDER BY d.db_id"""

CONFIGS = """
SELECT config_id, label, provider, model_name, model_version, effort, prompt_version,
       max_fix_attempts, max_rewrite_attempts, temperature
FROM pipeline_config ORDER BY config_id"""

CONFIG_EXISTS = "SELECT config_id FROM pipeline_config WHERE config_id = %(config_id)s"

DATABASE_EXISTS = "SELECT db_id FROM target_database WHERE db_id = %(db_id)s"

PLAYGROUND_ID = "SELECT experiment_id, is_frozen FROM experiment WHERE name = 'playground'"

RUNNING_FOR_USER = "SELECT count(*) AS n FROM run WHERE user_id = %(user_id)s AND status = 'running'"

# ---------- analyst use cases ----------

MY_RUNS = """
SELECT r.run_id, r.started_at, r.finished_at, r.status, c.label, d.db_name, q.question_id, q.source,
       left(q.question_text, 120) AS question, s.is_correct, s.total_tokens,
       m.median_ms, g.gold_median_ms
FROM run r
JOIN question q        ON q.question_id = r.question_id
JOIN target_database d ON d.db_id = q.db_id
JOIN pipeline_config c ON c.config_id = r.config_id
JOIN v_run_summary s   ON s.run_id = r.run_id
LEFT JOIN v_query_median m ON m.run_id = r.run_id AND m.step_no = s.final_step
LEFT JOIN v_gold_median g  ON g.question_id = q.question_id
WHERE r.user_id = %(user_id)s
ORDER BY r.started_at DESC
LIMIT %(limit)s"""

INSERT_USER_QUESTION = """
INSERT INTO question (question_id, db_id, question_text, evidence_hint, source, created_by)
VALUES (nextval('user_question_seq'), %(db_id)s, %(question_text)s, %(evidence_hint)s, 'user', %(user_id)s)
RETURNING question_id"""

QUESTIONS = """
SELECT q.question_id, d.db_name, q.difficulty, q.question_text,
       g.gold_median_ms,
       (SELECT string_agg(qt.table_name, ', ' ORDER BY qt.table_name)
          FROM question_table qt WHERE qt.question_id = q.question_id) AS tables
FROM question q
JOIN target_database d ON d.db_id = q.db_id
LEFT JOIN v_gold_median g ON g.question_id = q.question_id
WHERE q.source = 'bird'
  AND (%(db_id)s::int IS NULL OR q.db_id = %(db_id)s)
  AND (%(difficulty)s::text IS NULL OR q.difficulty = %(difficulty)s)
  AND (%(table)s::text IS NULL OR EXISTS (SELECT 1 FROM question_table qt
                                          WHERE qt.question_id = q.question_id AND qt.table_name = %(table)s))
  AND (%(text)s::text IS NULL OR q.question_text ILIKE '%%' || %(text)s || '%%')
ORDER BY q.question_id
LIMIT 100"""

QUESTION = """
SELECT q.question_id, q.db_id, d.db_name, q.question_text, q.evidence_hint, q.gold_sql, q.difficulty,
       q.source, q.created_by, g.gold_median_ms
FROM question q
JOIN target_database d ON d.db_id = q.db_id
LEFT JOIN v_gold_median g ON g.question_id = q.question_id
WHERE q.question_id = %(question_id)s"""

QUESTION_TABLES = """
SELECT qt.table_name, t.row_count, t.size_mb
FROM question_table qt JOIN target_table t ON t.db_id = qt.db_id AND t.table_name = qt.table_name
WHERE qt.question_id = %(question_id)s ORDER BY qt.table_name"""

GOLD_TRIALS = """
SELECT trial_no, runtime_ms, is_warmup FROM gold_timing WHERE question_id = %(question_id)s ORDER BY trial_no"""

# runs of one question the viewer may see: batch experiments for everyone,
# playground runs only for their owner (researchers see all)
QUESTION_RUNS = """
SELECT r.run_id, e.name AS experiment, c.label, r.status, r.started_at, s.is_correct,
       m.median_ms, s.total_tokens
FROM run r
JOIN experiment e      ON e.experiment_id = r.experiment_id
JOIN pipeline_config c ON c.config_id = r.config_id
JOIN v_run_summary s   ON s.run_id = r.run_id
LEFT JOIN v_query_median m ON m.run_id = r.run_id AND m.step_no = s.final_step
WHERE r.question_id = %(question_id)s
  AND (%(is_researcher)s OR e.name <> 'playground' OR r.user_id = %(user_id)s)
ORDER BY r.started_at DESC
LIMIT 50"""

RUN = """
SELECT r.run_id, r.experiment_id, e.name AS experiment, r.config_id, r.question_id, r.repeat_no,
       r.started_at, r.finished_at, r.status, r.user_id, r.error_detail,
       c.label, c.model_name, c.effort, c.max_fix_attempts, c.max_rewrite_attempts,
       q.question_text, q.evidence_hint, q.gold_sql, q.difficulty, q.source, d.db_name,
       s.is_correct, s.final_step, s.total_tokens, g.gold_median_ms, v.reward, v.tau
FROM run r
JOIN experiment e      ON e.experiment_id = r.experiment_id
JOIN pipeline_config c ON c.config_id = r.config_id
JOIN question q        ON q.question_id = r.question_id
JOIN target_database d ON d.db_id = q.db_id
JOIN v_run_summary s   ON s.run_id = r.run_id
LEFT JOIN v_gold_median g ON g.question_id = r.question_id
LEFT JOIN v_run_rves v    ON v.run_id = r.run_id
WHERE r.run_id = %(run_id)s"""

RUN_STEPS = """
SELECT g.step_no, g.parent_step_no, g.stage, g.sql_text, g.executed_ok, g.error_message, g.matches_gold,
       g.is_final, g.accepted, g.reject_reason, m.median_ms, m.trials,
       p.startup_cost, p.total_cost, p.est_rows, p.plan_json,
       (SELECT string_agg(i.issue, ', ' ORDER BY i.issue) FROM plan_issue i
         WHERE i.run_id = g.run_id AND i.step_no = g.step_no) AS issues
FROM generated_query g
LEFT JOIN v_query_median m ON m.run_id = g.run_id AND m.step_no = g.step_no
LEFT JOIN query_plan p     ON p.run_id = g.run_id AND p.step_no = g.step_no
WHERE g.run_id = %(run_id)s
ORDER BY g.step_no"""

RUN_CALLS = """
SELECT call_no, stage, input_tokens, output_tokens, latency_ms
FROM llm_call WHERE run_id = %(run_id)s ORDER BY call_no"""




# ---------- researcher use cases ----------

EXPERIMENTS = """
SELECT e.experiment_id, e.name, e.description, e.started_at, e.is_frozen,
       count(r.run_id) AS runs,
       count(*) FILTER (WHERE r.status = 'running') AS running,
       count(*) FILTER (WHERE r.status = 'done')    AS done,
       count(*) FILTER (WHERE r.status IN ('failed','timeout')) AS failed
FROM experiment e LEFT JOIN run r ON r.experiment_id = e.experiment_id
GROUP BY e.experiment_id ORDER BY e.experiment_id"""

EXPERIMENT = "SELECT experiment_id, name, description, started_at, is_frozen FROM experiment WHERE experiment_id = %(experiment_id)s"




# Headline numbers per config: execution accuracy, R-VES, runtime vs gold, cost.
REPORT_SUMMARY = """
SELECT c.label,
       count(*)                                                       AS runs,
       round(100.0 * avg(v.is_correct::int), 1)                       AS ex_percent,
       round(100.0 * avg(v.reward), 2)                                AS rves,
       round(avg(v.median_ms / NULLIF(v.gold_median_ms, 0)) FILTER (WHERE v.is_correct)::numeric, 3) AS avg_ratio_vs_gold,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY v.median_ms) FILTER (WHERE v.is_correct))::numeric, 3) AS median_ms_correct,
       round(avg(s.total_tokens))                                     AS avg_tokens,
       round(avg((SELECT count(*) FROM llm_call l WHERE l.run_id = v.run_id)), 2) AS avg_calls,
       round(avg((SELECT sum(l.latency_ms) FROM llm_call l WHERE l.run_id = v.run_id)) / 1000.0, 1) AS avg_llm_seconds
FROM v_run_rves v
JOIN v_run_summary s   ON s.run_id = v.run_id
JOIN pipeline_config c ON c.config_id = v.config_id
WHERE v.experiment_id = %(experiment_id)s
GROUP BY c.label ORDER BY c.label"""

# Same numbers split by slow vs fast questions (gold median >= SLOW_QUESTION_MS)
REPORT_SLOW_FAST = """
SELECT c.label,
       CASE WHEN v.gold_median_ms >= %(slow_ms)s THEN 'slow' ELSE 'fast' END AS bucket,
       count(*) AS runs,
       round(100.0 * avg(v.is_correct::int), 1) AS ex_percent,
       round(100.0 * avg(v.reward), 2)          AS rves,
       round(avg(v.median_ms / NULLIF(v.gold_median_ms, 0)) FILTER (WHERE v.is_correct)::numeric, 3) AS avg_ratio_vs_gold
FROM v_run_rves v JOIN pipeline_config c ON c.config_id = v.config_id
WHERE v.experiment_id = %(experiment_id)s
GROUP BY 1, 2 ORDER BY 1, 2 DESC"""

REPORT_REWRITES = """
SELECT c.label, o.outcome, count(*) AS n
FROM v_rewrite_outcome o JOIN pipeline_config c ON c.config_id = o.config_id
WHERE o.experiment_id = %(experiment_id)s
GROUP BY 1, 2 ORDER BY 1, 3 DESC"""

# plan issues of the first executable query vs the final query, per config
REPORT_ISSUES = """
WITH first_ok AS (
    SELECT DISTINCT ON (g.run_id) g.run_id, g.step_no
    FROM generated_query g JOIN run r ON r.run_id = g.run_id
    WHERE r.experiment_id = %(experiment_id)s AND g.executed_ok AND g.stage <> 'rewrite'
    ORDER BY g.run_id, g.step_no DESC)
SELECT c.label, i.issue,
       count(*) FILTER (WHERE i.step_no = f.step_no) AS before_rewrite,
       count(*) FILTER (WHERE i.step_no = s.final_step) AS in_final
FROM plan_issue i
JOIN run r ON r.run_id = i.run_id
JOIN pipeline_config c ON c.config_id = r.config_id
JOIN first_ok f ON f.run_id = i.run_id
JOIN v_run_summary s ON s.run_id = i.run_id
WHERE r.experiment_id = %(experiment_id)s
GROUP BY 1, 2 ORDER BY 1, 2"""

# Best accepted rewrites: speedup = base median / rewritten median
REPORT_SPEEDUPS = """
SELECT o.run_id, r.question_id, c.label, round((o.base_median_ms / NULLIF(o.median_ms, 0))::numeric, 2) AS speedup,
       round(o.base_median_ms::numeric, 3) AS before_ms, round(o.median_ms::numeric, 3) AS after_ms,
       o.matches_gold, b.sql_text AS before_sql, g.sql_text AS after_sql,
       (SELECT string_agg(issue, ', ') FROM plan_issue pi WHERE pi.run_id = o.run_id AND pi.step_no = o.base_step_no) AS issues_before
FROM v_rewrite_outcome o
JOIN run r ON r.run_id = o.run_id
JOIN pipeline_config c ON c.config_id = r.config_id
JOIN generated_query g ON g.run_id = o.run_id AND g.step_no = o.step_no
JOIN generated_query b ON b.run_id = o.run_id AND b.step_no = o.base_step_no
WHERE o.experiment_id = %(experiment_id)s AND o.accepted
ORDER BY speedup DESC NULLS LAST
LIMIT %(limit)s"""

# Effect of the rewrite stage inside the same run: the query before rewriting (base)
# vs the final query, for runs where at least one rewrite was attempted. Unlike the
# config-vs-config comparison, this is free of differences in the initial SQL.
REPORT_REWRITE_EFFECT = """
WITH base AS (
    SELECT DISTINCT ON (g.run_id) g.run_id, g.step_no, g.matches_gold
    FROM generated_query g JOIN run r ON r.run_id = g.run_id
    WHERE r.experiment_id = %(experiment_id)s AND g.executed_ok AND g.stage <> 'rewrite'
    ORDER BY g.run_id, g.step_no DESC)
SELECT c.label,
       count(*)                                          AS runs_with_rewrite_attempt,
       count(*) FILTER (WHERE f.step_no <> b.step_no)    AS final_is_rewrite,
       count(*) FILTER (WHERE b.matches_gold)            AS correct_before,
       count(*) FILTER (WHERE f.matches_gold)            AS correct_after,
       round(exp(avg(ln(GREATEST(mb.median_ms, 0.001) / GREATEST(mf.median_ms, 0.001)))
                 FILTER (WHERE f.step_no <> b.step_no))::numeric, 2) AS geomean_speedup_rewritten,
       round(sum(mb.median_ms)::numeric, 1)              AS total_ms_before,
       round(sum(mf.median_ms)::numeric, 1)              AS total_ms_after
FROM base b
JOIN run r ON r.run_id = b.run_id
JOIN pipeline_config c ON c.config_id = r.config_id
JOIN generated_query f ON f.run_id = b.run_id AND f.is_final
JOIN v_query_median mb ON mb.run_id = b.run_id AND mb.step_no = b.step_no
JOIN v_query_median mf ON mf.run_id = f.run_id AND mf.step_no = f.step_no
WHERE EXISTS (SELECT 1 FROM generated_query x WHERE x.run_id = b.run_id AND x.stage = 'rewrite')
GROUP BY c.label ORDER BY c.label"""

# Same question, two configs: who produced the faster correct final query
REPORT_HEAD_TO_HEAD = """
WITH f AS (
    SELECT v.question_id, c.label, bool_or(v.is_correct) AS correct,
           min(v.median_ms) FILTER (WHERE v.is_correct) AS ms
    FROM v_run_rves v JOIN pipeline_config c ON c.config_id = v.config_id
    WHERE v.experiment_id = %(experiment_id)s GROUP BY 1, 2)
SELECT a.label AS config_a, b.label AS config_b,
       count(*) FILTER (WHERE a.correct AND b.correct) AS both_correct,
       count(*) FILTER (WHERE a.correct AND b.correct AND a.ms < b.ms * 0.9) AS a_faster,
       count(*) FILTER (WHERE a.correct AND b.correct AND b.ms < a.ms * 0.9) AS b_faster,
       count(*) FILTER (WHERE a.correct AND NOT b.correct) AS only_a_correct,
       count(*) FILTER (WHERE b.correct AND NOT a.correct) AS only_b_correct
FROM f a JOIN f b ON b.question_id = a.question_id AND a.label < b.label
GROUP BY 1, 2"""

REPORT_BY_DB = """
SELECT d.db_name, c.label, count(*) AS runs,
       round(100.0 * avg(v.is_correct::int), 1) AS ex_percent,
       round(100.0 * avg(v.reward), 2) AS rves
FROM v_run_rves v
JOIN pipeline_config c ON c.config_id = v.config_id
JOIN question q ON q.question_id = v.question_id
JOIN target_database d ON d.db_id = q.db_id
WHERE v.experiment_id = %(experiment_id)s
GROUP BY 1, 2 ORDER BY 1, 2"""

EXPERIMENT_RUNS = """
SELECT r.run_id, r.question_id, c.label, r.status, s.is_correct, m.median_ms, s.total_tokens,
       left(q.question_text, 90) AS question
FROM run r
JOIN pipeline_config c ON c.config_id = r.config_id
JOIN question q        ON q.question_id = r.question_id
JOIN v_run_summary s   ON s.run_id = r.run_id
LEFT JOIN v_query_median m ON m.run_id = r.run_id AND m.step_no = s.final_step
WHERE r.experiment_id = %(experiment_id)s
  AND (%(status)s::text IS NULL OR r.status = %(status)s)
ORDER BY r.run_id DESC
LIMIT 200"""
