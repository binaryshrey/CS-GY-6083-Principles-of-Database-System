-- Efficient Text-to-SQL: Part 3 migration
-- CS-GY 6083 Part 3 | Shreyansh Saurabh (ss21034)
-- Applied on top of the unchanged Part 2 schema (01_schema.sql). Every change is
-- additive except two relaxed NOT NULLs on question, explained below.
-- Run: psql -d text2sql -f 02_part3_migration.sql

SET search_path = t2s;

-- ---------- M1. Application users (web app login) ----------
-- Two roles. 'analyst' accounts self-register; 'researcher' accounts are created
-- only by scripts/create_user.py (trusted setup), never through the web form.
CREATE TABLE app_user (
    user_id       SERIAL PRIMARY KEY,
    email         VARCHAR(254) NOT NULL UNIQUE,
    name          VARCHAR(100) NOT NULL,
    password_hash TEXT         NOT NULL,             -- werkzeug scrypt hash, never plain text
    role          VARCHAR(10)  NOT NULL DEFAULT 'analyst'
                  CHECK (role IN ('analyst','researcher')),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (email = lower(email))
);

-- ---------- M2. Where each target database lives ----------
-- The BIRD tables are loaded into a separate PostgreSQL database; pg_schema is
-- the schema that holds this target database's tables.
ALTER TABLE target_database ADD COLUMN pg_schema VARCHAR(63) NOT NULL DEFAULT 'public';
-- Schema text shown to the LLM (columns, keys, BIRD column notes, example rows).
-- Generated once by the loader and stored, so every run sees the same prompt.
ALTER TABLE target_database ADD COLUMN schema_prompt TEXT;

-- ---------- M3. Questions typed by users ----------
-- Users can ask their own questions in the web app. Those have no gold SQL and
-- no BIRD difficulty, so the two NOT NULLs become conditional on source.
ALTER TABLE question ADD COLUMN source VARCHAR(5) NOT NULL DEFAULT 'bird'
    CHECK (source IN ('bird','user'));
ALTER TABLE question ADD COLUMN created_by INT REFERENCES app_user(user_id) ON DELETE SET NULL;
ALTER TABLE question ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE question ALTER COLUMN gold_sql DROP NOT NULL;
ALTER TABLE question ALTER COLUMN difficulty DROP NOT NULL;
ALTER TABLE question ADD CONSTRAINT question_bird_has_gold
    CHECK (source = 'user' OR (gold_sql IS NOT NULL AND difficulty IS NOT NULL));
-- BIRD ids are < 2000; user questions draw ids from this sequence so they never collide
CREATE SEQUENCE user_question_seq START 1000000 OWNED BY question.question_id;

-- ---------- M4. Who launched a run ----------
-- NULL for runs started by the batch script. ON DELETE SET NULL keeps experiment
-- results when an account is removed.
ALTER TABLE run ADD COLUMN user_id INT REFERENCES app_user(user_id) ON DELETE SET NULL;
ALTER TABLE run ADD COLUMN finished_at TIMESTAMPTZ;
ALTER TABLE run ADD COLUMN error_detail TEXT;          -- pipeline-level failure (API error, ...)

-- ---------- M5. Experiment description ----------
ALTER TABLE experiment ADD COLUMN description TEXT;

-- ---------- M5a. Effort level of a pipeline config ----------
-- Current Claude models reject sampling parameters, so temperature is recorded as 0
-- and not sent; the reasoning-effort level is the setting that is actually sent.
ALTER TABLE pipeline_config ADD COLUMN effort VARCHAR(6) NOT NULL DEFAULT 'medium'
    CHECK (effort IN ('low','medium','high','xhigh','max'));
ALTER TABLE pipeline_config DROP CONSTRAINT pipeline_config_provider_model_name_model_version_temperatu_key;
ALTER TABLE pipeline_config ADD CONSTRAINT pipeline_config_settings_key
    UNIQUE (provider, model_name, model_version, temperature, prompt_version,
            max_fix_attempts, max_rewrite_attempts, effort);

-- ---------- M5b. Rewrite acceptance ----------
-- A rewrite is accepted when it returns the same rows as the query it improves on
-- and its median runtime is at least 10% lower. Accepted rewrites can later be
-- superseded by a better one, so is_final alone cannot tell accepted from rejected.
ALTER TABLE generated_query ADD COLUMN accepted BOOLEAN;
ALTER TABLE generated_query ADD COLUMN reject_reason VARCHAR(16)
    CHECK (reject_reason IN ('error','different_result','not_faster'));
ALTER TABLE generated_query ADD CONSTRAINT gq_accept_only_rewrites
    CHECK ((stage = 'rewrite') = (accepted IS NOT NULL));
ALTER TABLE generated_query ADD CONSTRAINT gq_reason_iff_rejected
    CHECK ((accepted IS FALSE) = (reject_reason IS NOT NULL));

-- ---------- M6. Indexes (the Part 2 P2 set, plus ones the web app needs) ----------
CREATE INDEX run_question_idx       ON run (question_id);
CREATE INDEX run_exp_config_idx     ON run (experiment_id, config_id);
CREATE INDEX run_user_idx           ON run (user_id, started_at DESC);
CREATE INDEX question_db_idx        ON question (db_id);
CREATE INDEX question_table_tab_idx ON question_table (db_id, table_name);
CREATE INDEX plan_issue_issue_idx   ON plan_issue (issue);
CREATE INDEX llm_call_stage_idx     ON llm_call (stage);
CREATE INDEX gq_stage_parent_idx    ON generated_query (run_id, parent_step_no) WHERE stage = 'rewrite';
CREATE INDEX idx_sugg_table         ON index_suggestion (db_id, table_name);
CREATE INDEX query_plan_json_gin    ON query_plan USING gin (plan_json jsonb_path_ops);

-- ---------- M7. Derived views for the reports ----------

-- R-VES reward of each finished benchmark run (BIRD Mini-Dev definition).
-- tau = gold median / predicted median; reward 1.25 / 1 / 0.75 / 0.5 / 0.25 by tau band,
-- 0 when the final query is wrong. R-VES of a set of runs = 100 * avg(reward).
CREATE VIEW v_run_rves AS
SELECT s.run_id, s.experiment_id, s.config_id, s.question_id, s.is_correct,
       m.median_ms, g.gold_median_ms,
       g.gold_median_ms / NULLIF(m.median_ms, 0) AS tau,
       CASE WHEN NOT s.is_correct OR m.median_ms IS NULL OR g.gold_median_ms IS NULL THEN 0
            WHEN g.gold_median_ms / NULLIF(m.median_ms, 0) >= 2    THEN 1.25
            WHEN g.gold_median_ms / NULLIF(m.median_ms, 0) >= 1    THEN 1.00
            WHEN g.gold_median_ms / NULLIF(m.median_ms, 0) >= 0.5  THEN 0.75
            WHEN g.gold_median_ms / NULLIF(m.median_ms, 0) >= 0.25 THEN 0.50
            ELSE 0.25 END AS reward
FROM v_run_summary s
JOIN question q ON q.question_id = s.question_id AND q.source = 'bird'
LEFT JOIN v_query_median m ON m.run_id = s.run_id AND m.step_no = s.final_step
LEFT JOIN v_gold_median  g ON g.question_id = s.question_id
WHERE s.status IN ('done','failed','timeout');

-- Outcome of every rewrite attempt, measured against its BASE query: the query it had
-- to beat. parent_step_no (Revises) can point at a rejected attempt, since a retry
-- revises the failed attempt; the base is the latest earlier step that executed and is
-- either a non-rewrite step or an accepted rewrite.
-- 'broke' = accepted by the pipeline (same rows as the base query, faster) but the
-- result no longer matches gold; possible only through set-vs-multiset edge cases.
CREATE VIEW v_rewrite_outcome AS
SELECT c.run_id, c.step_no, c.parent_step_no, b.step_no AS base_step_no,
       r.experiment_id, r.config_id,
       c.executed_ok, c.accepted, c.reject_reason, c.matches_gold,
       b.matches_gold AS base_matches_gold,
       mc.median_ms AS median_ms, mb.median_ms AS base_median_ms,
       CASE WHEN c.accepted AND b.matches_gold AND c.matches_gold IS NOT TRUE THEN 'broke'
            WHEN c.accepted THEN 'accepted'
            ELSE COALESCE(c.reject_reason, 'error') END AS outcome
FROM generated_query c
JOIN run r ON r.run_id = c.run_id
JOIN LATERAL (SELECT s.step_no, s.matches_gold FROM generated_query s
              WHERE s.run_id = c.run_id AND s.step_no < c.step_no AND s.executed_ok
                AND (s.stage <> 'rewrite' OR s.accepted)
              ORDER BY s.step_no DESC LIMIT 1) b ON true
LEFT JOIN v_query_median mc ON mc.run_id = c.run_id AND mc.step_no = c.step_no
LEFT JOIN v_query_median mb ON mb.run_id = c.run_id AND mb.step_no = b.step_no
WHERE c.stage = 'rewrite';

-- ---------- M8. Read-only role ----------
-- 01_schema.sql creates llm_reader (NOLOGIN, read-only, 30 s timeout); the loader
-- grants it USAGE + SELECT on the BIRD schemas in the target database only. The app
-- switches to it with SET LOCAL ROLE inside a READ ONLY transaction for every
-- candidate query, so LLM-written SQL can neither write nor see the results database.
