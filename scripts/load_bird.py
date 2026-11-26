"""Reproducible rebuild: BIRD Mini-Dev target databases + the results database.

    python scripts/load_bird.py --zip data/minidev.zip

1. (re)creates the results database schema: sql/01_schema.sql + sql/02_part3_migration.sql
2. streams the official Mini-Dev PostgreSQL dump out of the zip (no 1 GB extract),
   keeps only the objects of the selected databases, and loads them into the target
   database; each BIRD database then moves into its own schema (formula_1, ...)
3. grants llm_reader read-only access to those schemas
4. fills target_database (with a frozen schema prompt), target_table (real row counts
   and sizes), question (the Mini-Dev questions of the four Part 1 databases: 198 entries,
   196 unique, since 137 and 138 appear twice),
   question_table (tables read by each gold query, from its EXPLAIN plan) and
   gold_timing (1 warm-up + 5 timed runs of every gold query)
5. inserts the pipeline configs and the 'playground' experiment used by the web app
"""
import argparse
import csv
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg import sql as pgsql  # noqa: E402

from t2s import config, plan_analysis, targets  # noqa: E402

DUMP_PATH = "minidev/MINIDEV_postgresql/BIRD_dev.sql"
QUESTIONS_PATH = "minidev/MINIDEV/mini_dev_postgresql.json"
TABLES_PATH = "minidev/MINIDEV/dev_tables.json"
DESC_DIR = "minidev/MINIDEV/dev_databases/{db}/database_description/"

DOMAINS = {
    "formula_1": "motorsport",
    "european_football_2": "sports",
    "codebase_community": "Q&A site",
    "financial": "banking",
    "california_schools": "education",
}
# Databases whose questions are loaded (the Part 1 set: 66 + 51 + 49 + 32 = 198).
# california_schools is loaded without questions, as in Part 2.
QUESTION_DBS = ["formula_1", "european_football_2", "codebase_community", "financial"]

CONFIGS = [
    # label, max_fix, max_rewrite
    ("baseline", 2, 0),
    ("plan_guided", 2, 2),
]

HEADER = re.compile(rb"^-- (?:Data for )?Name: ([^;]+); Type: ([^;]+);")
ON_TABLE = re.compile(rb"\bON (?:ONLY )?public\.\"?(\w+)\"?")


def log(*a):
    print(*a, flush=True)


def dsn_dbname(dsn):
    m = re.search(r"dbname=(\S+)", dsn)
    return m.group(1) if m else None


def ensure_database(dsn):
    name = dsn_dbname(dsn)
    with psycopg.connect("dbname=postgres", autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            c.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
            log(f"created database {name}")


def run_psql_file(dsn, path):
    subprocess.run(["psql", dsn, "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)], check=True)


# ---------- dump filtering ----------

def load_dump(zip_path, tables):
    """Stream the dump through a filter into psql, keeping objects of `tables` only."""
    keep_tables = {t.encode() for t in tables}

    def seq_owner(seqname):
        return any(seqname.startswith(t + b"_") for t in keep_tables)

    psql = subprocess.Popen(["psql", config.TARGET_DSN, "-q", "-v", "ON_ERROR_STOP=1"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    kept = skipped = 0
    with zipfile.ZipFile(zip_path) as z, z.open(DUMP_PATH) as dump:
        decision = True        # preamble (SET ...) is kept
        pending = None         # buffered lines of an INDEX section until its table is known
        for line in dump:
            m = HEADER.match(line)
            if m:
                if pending is not None:
                    pending = None
                name, typ = m.group(1).strip(), m.group(2).strip()
                first = name.split()[0]
                if typ in (b"TABLE", b"TABLE DATA", b"CONSTRAINT", b"FK CONSTRAINT", b"DEFAULT"):
                    decision = first in keep_tables
                elif typ.startswith(b"SEQUENCE"):
                    decision = seq_owner(first)
                elif typ == b"INDEX":
                    decision, pending = None, [line]
                    continue
                else:
                    decision = False
                if decision:
                    kept += 1
                else:
                    skipped += 1
            if pending is not None:
                pending.append(line)
                t = ON_TABLE.search(line)
                if t:
                    decision = t.group(1) in keep_tables
                    kept, skipped = (kept + 1, skipped) if decision else (kept, skipped + 1)
                    if decision:
                        psql.stdin.writelines(pending)
                    pending = None
                continue
            if decision and b"OWNER TO" not in line:
                psql.stdin.write(line)
    psql.stdin.close()
    if psql.wait() != 0:
        raise SystemExit("psql failed while loading the dump")
    log(f"dump loaded: {kept} objects kept, {skipped} skipped")


def organize_schemas(dbs_tables):
    """Move each BIRD database's tables from public into a schema named after it."""
    with psycopg.connect(config.TARGET_DSN, autocommit=True) as c:
        for db, tables in dbs_tables.items():
            c.execute(pgsql.SQL("CREATE SCHEMA {}").format(pgsql.Identifier(db)))
            for t in tables:
                c.execute(pgsql.SQL("ALTER TABLE public.{} SET SCHEMA {}").format(
                    pgsql.Identifier(t), pgsql.Identifier(db)))
            c.execute(pgsql.SQL("GRANT USAGE ON SCHEMA {} TO llm_reader").format(pgsql.Identifier(db)))
            c.execute(pgsql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO llm_reader").format(pgsql.Identifier(db)))
        c.execute("ANALYZE")
    log("schemas organized, llm_reader granted SELECT, ANALYZE done")


def drop_target_objects(dbs_tables):
    with psycopg.connect(config.TARGET_DSN, autocommit=True) as c:
        for db, tables in dbs_tables.items():
            c.execute(pgsql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pgsql.Identifier(db)))
            for t in tables:
                c.execute(pgsql.SQL("DROP TABLE IF EXISTS public.{} CASCADE").format(pgsql.Identifier(t)))


# ---------- metadata ----------

def read_descriptions(zip_path, db):
    """BIRD's per-column notes -> {table: {column: note}} (lower-case keys)."""
    out = {}
    prefix = DESC_DIR.format(db=db)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if not info.filename.startswith(prefix) or not info.filename.endswith(".csv"):
                continue
            raw = z.read(info)
            for enc in ("utf-8-sig", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            table = Path(info.filename).stem.lower()
            cols = {}
            for row in csv.DictReader(io.StringIO(text)):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                name = row.get("original_column_name", "").lower()
                if not name:
                    continue
                note = row.get("column_description") or row.get("column_name") or ""
                vd = row.get("value_description", "")
                if vd and len(vd) <= 160:
                    note = f"{note}; {vd}" if note else vd
                note = " ".join(note.split())
                if note and note.lower() != name:
                    cols[name] = note[:240]
            out[table] = cols
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", default=str(ROOT / "data" / "minidev.zip"))
    ap.add_argument("--skip-dump", action="store_true", help="target tables are already loaded")
    ap.add_argument("--skip-gold-timing", action="store_true")
    args = ap.parse_args()

    with zipfile.ZipFile(args.zip) as z:
        all_tables = json.loads(z.read(TABLES_PATH))
        questions = json.loads(z.read(QUESTIONS_PATH))
    by_name = {d["db_id"]: d for d in all_tables}
    dbs_tables = {db: [t.lower() for t in by_name[db]["table_names_original"]] for db in DOMAINS}

    # 1. results database
    ensure_database(config.APP_DSN)
    ensure_database(config.TARGET_DSN)
    run_psql_file(config.APP_DSN, ROOT / "sql" / "01_schema.sql")
    run_psql_file(config.APP_DSN, ROOT / "sql" / "02_part3_migration.sql")
    log("results schema t2s created")

    # 2-3. target database
    if not args.skip_dump:
        drop_target_objects(dbs_tables)
        load_dump(args.zip, [t for ts in dbs_tables.values() for t in ts])
        organize_schemas(dbs_tables)

    # 4. catalog
    tconn = psycopg.connect(config.TARGET_DSN)
    app = psycopg.connect(config.APP_DSN)
    app.execute("SET search_path = t2s")
    db_ids = {}
    for i, (db, tables) in enumerate(dbs_tables.items(), start=1):
        sizes = []
        for t in tables:
            cnt = tconn.execute(pgsql.SQL("SELECT count(*) FROM {}.{}").format(
                pgsql.Identifier(db), pgsql.Identifier(t))).fetchone()[0]
            mb = tconn.execute("SELECT pg_total_relation_size(%s::regclass) / 1048576.0",
                               (f'{db}."{t}"',)).fetchone()[0]
            sizes.append((t, cnt, round(float(mb), 2)))
        prompt = targets.build_schema_prompt(tconn, db, tables, read_descriptions(args.zip, db))
        tconn.rollback()
        app.execute("""INSERT INTO target_database (db_id, db_name, domain, size_mb, pg_schema, schema_prompt)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (i, db, DOMAINS[db], round(sum(s for _, _, s in sizes), 2), db, prompt))
        for t, cnt, mb in sizes:
            app.execute("INSERT INTO target_table (db_id, table_name, row_count, size_mb) VALUES (%s,%s,%s,%s)",
                        (i, t, cnt, mb))
        db_ids[db] = i
        log(f"{db}: {len(tables)} tables, {sum(c for _, c, _ in sizes):,} rows, prompt {len(prompt):,} chars")
    app.execute("SELECT setval('target_database_db_id_seq', (SELECT max(db_id) FROM target_database))")
    app.commit()

    # questions, touched tables, gold timings
    loaded = 0
    bad, seen = [], set()
    for q in questions:
        # Mini-Dev lists questions 137 and 138 twice (identical entries); keep one copy
        if q["db_id"] not in QUESTION_DBS or q["question_id"] in seen:
            continue
        seen.add(q["question_id"])
        db = q["db_id"]
        gold = q["SQL"].strip().rstrip(";")
        app.execute("""INSERT INTO question (question_id, db_id, question_text, evidence_hint, gold_sql, difficulty, source)
                       VALUES (%s,%s,%s,%s,%s,%s,'bird')""",
                    (q["question_id"], db_ids[db], q["question"].strip(), (q["evidence"] or "").strip() or None,
                     gold, q["difficulty"]))
        try:
            if args.skip_gold_timing:
                plan = targets._run(tconn, db, "EXPLAIN (FORMAT JSON) " + gold, lambda c: c.fetchone()[0])
                trials = []
            else:
                m = targets.measure(tconn, db, gold)
                plan, trials = m["plan"], m["trials"]
        except targets.QueryError as e:
            bad.append((q["question_id"], str(e)))
            app.commit()
            continue
        for rel in sorted(plan_analysis.relations(plan)):
            app.execute("""INSERT INTO question_table (question_id, db_id, table_name)
                           SELECT %s, %s, %s WHERE EXISTS
                             (SELECT 1 FROM target_table WHERE db_id = %s AND table_name = %s)""",
                        (q["question_id"], db_ids[db], rel, db_ids[db], rel))
        for trial_no, ms, warm in trials:
            app.execute("INSERT INTO gold_timing (question_id, trial_no, runtime_ms, is_warmup) VALUES (%s,%s,%s,%s)",
                        (q["question_id"], trial_no, round(ms, 3), warm))
        app.commit()
        loaded += 1
        if loaded % 25 == 0:
            log(f"  {loaded} questions loaded")
    log(f"questions loaded: {loaded}; gold queries that failed: {len(bad)}")
    for qid, err in bad:
        log(f"  question {qid}: {err}")

    # 5. configs and the playground experiment
    for label, fixes, rewrites in CONFIGS:
        app.execute("""INSERT INTO pipeline_config (label, provider, model_name, model_version, temperature,
                           prompt_version, max_fix_attempts, max_rewrite_attempts, effort)
                       VALUES (%s, 'anthropic', %s, %s, 0, 'p1', %s, %s, %s)""",
                    (label, config.DEFAULT_MODEL, "5.5", fixes, rewrites, config.DEFAULT_EFFORT))
    app.execute("""INSERT INTO experiment (name, description) VALUES
                   ('playground', 'Single runs launched from the web app (ad-hoc and benchmark questions).')""")
    app.commit()
    app.close()
    tconn.close()
    log("done")


if __name__ == "__main__":
    main()
