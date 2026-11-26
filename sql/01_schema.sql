-- Efficient Text-to-SQL: results database
-- CS-GY 6083 Part 2 | Shreyansh Saurabh (ss21034)
-- Table definitions derived from the Part 1 ER diagram. PostgreSQL 16.
-- Run: psql -d text2sql -f 01_schema.sql

DROP SCHEMA IF EXISTS t2s CASCADE;
CREATE SCHEMA t2s;
SET search_path = t2s;

-- ---------- Benchmark catalog ----------

CREATE TABLE target_database (
    db_id       SERIAL PRIMARY KEY,
    db_name     VARCHAR(64)  NOT NULL UNIQUE,          -- candidate key
    domain      VARCHAR(64)  NOT NULL,
    size_mb     NUMERIC(10,2) NOT NULL CHECK (size_mb >= 0)
);

-- weak entity: owner target_database, partial key table_name
CREATE TABLE target_table (
    db_id       INT          NOT NULL REFERENCES target_database(db_id) ON DELETE CASCADE,
    table_name  VARCHAR(64)  NOT NULL,
    row_count   BIGINT       NOT NULL CHECK (row_count >= 0),
    size_mb     NUMERIC(10,2) NOT NULL CHECK (size_mb >= 0),
    PRIMARY KEY (db_id, table_name)
);

CREATE TABLE question (
    question_id   INT PRIMARY KEY,                     -- BIRD question_id
    db_id         INT  NOT NULL REFERENCES target_database(db_id) ON DELETE RESTRICT,
    question_text TEXT NOT NULL,
    evidence_hint TEXT,
    gold_sql      TEXT NOT NULL,
    difficulty    VARCHAR(12) NOT NULL CHECK (difficulty IN ('simple','moderate','challenging')),
    UNIQUE (question_id, db_id)                        -- target for the composite FK below
);

-- associative entity for M:N Touches
CREATE TABLE question_table (
    question_id INT NOT NULL,
    db_id       INT NOT NULL,
    table_name  VARCHAR(64) NOT NULL,
    PRIMARY KEY (question_id, db_id, table_name),
    -- both FKs share db_id, so a question can only touch tables of its own database
    FOREIGN KEY (question_id, db_id)  REFERENCES question(question_id, db_id) ON DELETE CASCADE,
    FOREIGN KEY (db_id, table_name)   REFERENCES target_table(db_id, table_name) ON DELETE CASCADE
);

-- weak entity: owner question, partial key trial_no
CREATE TABLE gold_timing (
    question_id INT NOT NULL REFERENCES question(question_id) ON DELETE CASCADE,
    trial_no    SMALLINT NOT NULL CHECK (trial_no >= 1),
    runtime_ms  NUMERIC(12,3) NOT NULL CHECK (runtime_ms >= 0),
    is_warmup   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (question_id, trial_no)
);

-- ---------- Experiment log ----------

CREATE TABLE experiment (
    experiment_id SERIAL PRIMARY KEY,
    name          VARCHAR(100) NOT NULL UNIQUE,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    is_frozen     BOOLEAN      NOT NULL DEFAULT FALSE
);

-- composite attribute model -> provider, model_name, model_version
CREATE TABLE pipeline_config (
    config_id            SERIAL PRIMARY KEY,
    label                VARCHAR(50)  NOT NULL UNIQUE,
    provider             VARCHAR(50)  NOT NULL,
    model_name           VARCHAR(100) NOT NULL,
    model_version        VARCHAR(50)  NOT NULL,
    temperature          NUMERIC(3,2) NOT NULL CHECK (temperature BETWEEN 0 AND 2),
    prompt_version       VARCHAR(20)  NOT NULL,
    max_fix_attempts     SMALLINT     NOT NULL CHECK (max_fix_attempts >= 0),
    max_rewrite_attempts SMALLINT     NOT NULL CHECK (max_rewrite_attempts >= 0),
    UNIQUE (provider, model_name, model_version, temperature, prompt_version,
            max_fix_attempts, max_rewrite_attempts)   -- no duplicate configs under new labels
);

CREATE TABLE run (
    run_id        BIGSERIAL PRIMARY KEY,
    experiment_id INT    NOT NULL REFERENCES experiment(experiment_id) ON DELETE RESTRICT,
    config_id     INT    NOT NULL REFERENCES pipeline_config(config_id) ON DELETE RESTRICT,
    question_id   INT    NOT NULL REFERENCES question(question_id)   ON DELETE RESTRICT,
    repeat_no     SMALLINT NOT NULL CHECK (repeat_no >= 1),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        VARCHAR(10) NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','done','failed','timeout')),
    UNIQUE (experiment_id, config_id, question_id, repeat_no)
);

-- weak entity: owner run, partial key step_no; parent_step_no stores recursive Revises
CREATE TABLE generated_query (
    run_id         BIGINT   NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    step_no        SMALLINT NOT NULL CHECK (step_no >= 1),
    parent_step_no SMALLINT,
    stage          VARCHAR(8) NOT NULL CHECK (stage IN ('initial','fix','rewrite')),
    sql_text       TEXT     NOT NULL,
    executed_ok    BOOLEAN  NOT NULL,
    error_message  TEXT,
    matches_gold   BOOLEAN,                            -- NULL when not executed
    is_final       BOOLEAN  NOT NULL DEFAULT FALSE,
    PRIMARY KEY (run_id, step_no),
    FOREIGN KEY (run_id, parent_step_no) REFERENCES generated_query(run_id, step_no) ON DELETE CASCADE,
    UNIQUE (run_id, parent_step_no),                   -- at most one revision per query
    CHECK (parent_step_no < step_no),                  -- no cycles
    CHECK ((stage = 'initial') = (parent_step_no IS NULL)),
    CHECK (NOT executed_ok OR error_message IS NULL),
    CHECK (matches_gold IS NOT TRUE OR executed_ok),
    CHECK (NOT is_final OR executed_ok)
);
-- at most one final query per run
CREATE UNIQUE INDEX one_final_per_run ON generated_query(run_id) WHERE is_final;

-- weak entity 1:1 with generated_query; composite est_cost -> startup_cost, total_cost
CREATE TABLE query_plan (
    run_id       BIGINT   NOT NULL,
    step_no      SMALLINT NOT NULL,
    plan_json    JSONB    NOT NULL,
    startup_cost NUMERIC(14,2) NOT NULL CHECK (startup_cost >= 0),
    total_cost   NUMERIC(14,2) NOT NULL,
    est_rows     BIGINT   NOT NULL CHECK (est_rows >= 0),
    PRIMARY KEY (run_id, step_no),
    FOREIGN KEY (run_id, step_no) REFERENCES generated_query(run_id, step_no) ON DELETE CASCADE,
    CHECK (startup_cost <= total_cost)
);

-- multi-valued attribute plan_issues
CREATE TABLE plan_issue (
    run_id  BIGINT   NOT NULL,
    step_no SMALLINT NOT NULL,
    issue   VARCHAR(30) NOT NULL CHECK (issue IN
            ('correlated_subplan','large_seq_scan','nested_loop_large','sort_spill','hash_spill')),
    PRIMARY KEY (run_id, step_no, issue),
    FOREIGN KEY (run_id, step_no) REFERENCES query_plan(run_id, step_no) ON DELETE CASCADE
);

-- weak entity: owner generated_query, partial key trial_no
CREATE TABLE timing_trial (
    run_id     BIGINT   NOT NULL,
    step_no    SMALLINT NOT NULL,
    trial_no   SMALLINT NOT NULL CHECK (trial_no >= 1),
    runtime_ms NUMERIC(12,3) NOT NULL CHECK (runtime_ms >= 0),
    is_warmup  BOOLEAN  NOT NULL DEFAULT FALSE,
    PRIMARY KEY (run_id, step_no, trial_no),
    FOREIGN KEY (run_id, step_no) REFERENCES generated_query(run_id, step_no) ON DELETE CASCADE
);

-- weak entity: owner run, partial key call_no; composite token_usage
CREATE TABLE llm_call (
    run_id        BIGINT   NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    call_no       SMALLINT NOT NULL CHECK (call_no >= 1),
    stage         VARCHAR(8) NOT NULL CHECK (stage IN ('initial','fix','rewrite','index')),
    input_tokens  INT NOT NULL CHECK (input_tokens >= 0),
    output_tokens INT NOT NULL CHECK (output_tokens >= 0),
    latency_ms    INT NOT NULL CHECK (latency_ms >= 0),
    PRIMARY KEY (run_id, call_no)
);

-- stretch goal: index suggestions
CREATE TABLE index_suggestion (
    suggestion_id    BIGSERIAL PRIMARY KEY,
    run_id           BIGINT   NOT NULL,
    step_no          SMALLINT NOT NULL,
    db_id            INT      NOT NULL,
    table_name       VARCHAR(64) NOT NULL,
    create_sql       TEXT     NOT NULL,
    size_mb          NUMERIC(10,2) CHECK (size_mb >= 0),
    measured_speedup NUMERIC(8,3)  CHECK (measured_speedup > 0),
    FOREIGN KEY (run_id, step_no)     REFERENCES generated_query(run_id, step_no) ON DELETE CASCADE,
    FOREIGN KEY (db_id, table_name)   REFERENCES target_table(db_id, table_name)  ON DELETE CASCADE,
    UNIQUE (run_id, step_no, create_sql)
);

-- ---------- Trigger: frozen experiments cannot get new runs ----------

CREATE FUNCTION reject_frozen_experiment() RETURNS trigger AS $$
BEGIN
    IF (SELECT is_frozen FROM experiment WHERE experiment_id = NEW.experiment_id) THEN
        RAISE EXCEPTION 'experiment % is frozen; no new runs allowed', NEW.experiment_id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER run_frozen_check BEFORE INSERT OR UPDATE OF experiment_id ON run
    FOR EACH ROW EXECUTE FUNCTION reject_frozen_experiment();

-- ---------- Derived attributes as views ----------

-- median_ms of each generated query (non-warm-up trials)
CREATE VIEW v_query_median AS
SELECT run_id, step_no,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY runtime_ms) AS median_ms,
       count(*) AS trials
FROM timing_trial WHERE NOT is_warmup
GROUP BY run_id, step_no;

-- gold_median_ms of each question
CREATE VIEW v_gold_median AS
SELECT question_id,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY runtime_ms) AS gold_median_ms
FROM gold_timing WHERE NOT is_warmup
GROUP BY question_id;

-- is_correct and total_tokens of each run
CREATE VIEW v_run_summary AS
SELECT r.run_id, r.experiment_id, r.config_id, r.question_id, r.repeat_no, r.status,
       COALESCE(f.matches_gold, FALSE)           AS is_correct,
       f.step_no                                  AS final_step,
       (SELECT COALESCE(sum(c.input_tokens + c.output_tokens), 0)
          FROM llm_call c WHERE c.run_id = r.run_id) AS total_tokens
FROM run r
LEFT JOIN generated_query f ON f.run_id = r.run_id AND f.is_final;

-- question_count and table_count of each target database
CREATE VIEW v_database_counts AS
SELECT d.db_id, d.db_name,
       (SELECT count(*) FROM question q     WHERE q.db_id = d.db_id) AS question_count,
       (SELECT count(*) FROM target_table t WHERE t.db_id = d.db_id) AS table_count
FROM target_database d;

-- ---------- Roles: LLM-written SQL runs read-only ----------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'llm_reader') THEN
        CREATE ROLE llm_reader NOLOGIN;
    END IF;
END $$;
ALTER ROLE llm_reader SET statement_timeout = '30s';
ALTER ROLE llm_reader SET default_transaction_read_only = on;
