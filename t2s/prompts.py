"""Prompt text, version p1. The version string is stored in pipeline_config.prompt_version;
change it whenever this file changes so old and new runs are never mixed."""

PROMPT_VERSION = "p1"

SYSTEM_RULES = """You write SQL for a PostgreSQL 14 database. Answer each request with exactly one read-only SELECT statement (WITH ... SELECT is fine).

Rules:
- Use only the tables and columns listed in the schema below.
- All table and column names are lowercase in this database; write them unquoted, except reserved words such as "order", which must be double-quoted.
- Return exactly the columns the question asks for, in the order it asks, and nothing extra (no ids or helper columns unless asked).
- When an evidence note is given, it defines terms in the question; follow it literally.
- Integer division truncates in PostgreSQL: cast to REAL or NUMERIC before dividing when a ratio or percentage is wanted.
- Do not end the statement with a semicolon.

Database schema:
"""


def system_text(db_name, schema_prompt):
    return SYSTEM_RULES + f"-- database: {db_name}\n" + schema_prompt


def _question_block(question, evidence):
    s = f"Question: {question}\n"
    if evidence:
        s += f"Evidence: {evidence}\n"
    return s


def initial(question, evidence):
    return _question_block(question, evidence) + "\nWrite the SQL query that answers the question."


def fix(question, evidence, bad_sql, error):
    return (_question_block(question, evidence)
            + f"\nThis query failed:\n{bad_sql}\n\nPostgreSQL error:\n{error}\n\n"
              "Write a corrected query that answers the question.")


def rewrite(question, evidence, sql, plan_text, median_ms, issues, feedback=None):
    issue_text = ", ".join(sorted(issues)) if issues else "none flagged, but the query is slow"
    s = (_question_block(question, evidence)
         + f"\nThis query answers the question:\n{sql}\n\n"
           f"Its measured median runtime is {median_ms:.3f} ms. Plan issues detected: {issue_text}.\n"
           f"EXPLAIN ANALYZE output:\n{plan_text}\n\n"
           "Rewrite the query so that it returns exactly the same rows (same columns in the same order, "
           "same duplicates) but runs faster. Use the plan to find the cost: for example replace a "
           "correlated subquery with a join or a single aggregation, filter before joining, avoid reading "
           "a large table more than once, or drop an unnecessary DISTINCT/ORDER BY only if the result "
           "rows are unchanged. You cannot create indexes or change the data. If you see no faster "
           "equivalent, return the query unchanged.")
    if feedback:
        s += f"\n\nYour previous attempt was rejected ({feedback['reason']}):\n{feedback['sql']}\n"
        if feedback.get("detail"):
            s += f"{feedback['detail']}\n"
        s += "Try a different rewrite."
    return s
