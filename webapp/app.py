"""Efficient Text-to-SQL web application (Flask).

Roles
  analyst     self-registers; asks questions, runs the pipeline on benchmark questions,
              browses questions and databases, sees only their own run history.
  researcher  created by scripts/create_user.py; everything an analyst can do, plus the
              experiment reports (experiments themselves are run with scripts/run_experiment.py).

Every request after login re-reads the user from app_user (role checks never trust the
session alone), every POST carries a CSRF token, all SQL uses bound parameters, and
Jinja autoescaping escapes everything users or the LLM wrote.

    python -m webapp.app          (development server on http://127.0.0.1:5050; PORT to change)
"""
import json
import os
import re
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from flask import (Flask, abort, flash, g, redirect, render_template, request,  # noqa: E402
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

from t2s import config, costs, db, pipeline  # noqa: E402
from webapp import queries as Q  # noqa: E402

app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.SECRET_KEY or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=64 * 1024,
)

executor = ThreadPoolExecutor(max_workers=config.WEB_WORKERS)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,24}$")
MAX_QUESTION = 1000
MAX_EVIDENCE = 1000
MAX_RUNNING_PER_USER = 3
DIFFICULTIES = ("simple", "moderate", "challenging")


# ---------- helpers ----------

def one(statement, **params):
    return db.query_one(g.conn, statement, params)


def rows(statement, **params):
    return db.query(g.conn, statement, params)


def form_text(name, max_len, required=True):
    v = (request.form.get(name) or "").strip()
    if required and not v:
        raise ValueError(f"{name.replace('_', ' ')} is required")
    if len(v) > max_len:
        raise ValueError(f"{name.replace('_', ' ')} is longer than {max_len} characters")
    return v


def form_int(name, lo=None, hi=None, required=True):
    raw = (request.form.get(name) or "").strip()
    if not raw:
        if required:
            raise ValueError(f"{name.replace('_', ' ')} is required")
        return None
    if not re.fullmatch(r"-?\d{1,9}", raw):
        raise ValueError(f"{name.replace('_', ' ')} must be a whole number")
    v = int(raw)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ValueError(f"{name.replace('_', ' ')} must be between {lo} and {hi}")
    return v


def arg_int(name):
    raw = request.args.get(name, "")
    return int(raw) if re.fullmatch(r"\d{1,9}", raw) else None


def submit_run(run_id):
    executor.submit(pipeline.execute_run, run_id)


# ---------- request lifecycle ----------

@app.before_request
def open_connection():
    g.conn = db.connect()
    g.user = None
    uid = session.get("user_id")
    if uid is not None:
        g.user = one(Q.USER_BY_ID, user_id=uid)
        if g.user is None:                     # account deleted: drop the session
            session.clear()
    if request.method == "POST":
        token = session.get("csrf")
        if not token or not secrets.compare_digest(token, request.form.get("csrf", "")):
            abort(400, "invalid or missing form token; reload the page and try again")


@app.teardown_request
def close_connection(exc):
    conn = g.pop("conn", None)
    if conn is not None:
        if exc is None:
            conn.commit()
        else:
            conn.rollback()
        conn.close()


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf"], "user": g.get("user"), "slow_ms": Q.SLOW_QUESTION_MS,
            "budget": config.BUDGET_USD}


@app.template_filter("ms")
def fmt_ms(v):
    return "–" if v is None else f"{float(v):.3f}"


@app.template_filter("pretty_json")
def pretty_json(v):
    return json.dumps(v, indent=1)


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            flash("Please log in first.", "error")
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped


def researcher_required(view):
    @wraps(view)
    @login_required
    def wrapped(*a, **kw):
        if g.user["role"] != "researcher":
            abort(403, "this page is for researchers only")
        return view(*a, **kw)
    return wrapped


def can_view_run(run):
    return (g.user["role"] == "researcher" or run["user_id"] == g.user["user_id"]
            or run["experiment"] != "playground")


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(e):
    return render_template("error.html", code=e.code, message=e.description), e.code


# ---------- public pages ----------

@app.route("/")
def index():
    if g.user:
        return redirect(url_for("home"))
    return render_template("index.html", databases=rows(Q.PUBLIC_DATABASES))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        try:
            name = form_text("name", 100)
            email = form_text("email", 254).lower()
            password = request.form.get("password", "")
            if not EMAIL_RE.match(email):
                raise ValueError("enter a valid email address")
            if len(password) < 8 or len(password) > 200:
                raise ValueError("password must be 8 to 200 characters")
            if password != request.form.get("password2", ""):
                raise ValueError("passwords do not match")
            row = one(Q.INSERT_ANALYST, email=email, name=name, password_hash=generate_password_hash(password))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("register.html", form=request.form), 400
        except psycopg.errors.UniqueViolation:
            g.conn.rollback()
            flash("An account with that email already exists.", "error")
            return render_template("register.html", form=request.form), 400
        g.conn.commit()
        session.clear()
        session["user_id"] = row["user_id"]
        flash("Welcome! Your analyst account is ready.", "ok")
        return redirect(url_for("home"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:254]
        password = request.form.get("password", "")[:200]
        u = one(Q.USER_BY_EMAIL, email=email)
        if u is None or not check_password_hash(u["password_hash"], password):
            # same message whether the email exists or not
            flash("Login failed: wrong email or password.", "error")
            return render_template("login.html", email=email), 401
        session.clear()
        session["user_id"] = u["user_id"]
        return redirect(url_for("home"))
    return render_template("login.html", email="")


@app.route("/logout", methods=["POST"])
def logout():
    name = g.user["name"] if g.user else None
    session.clear()
    return render_template("goodbye.html", name=name)


# ---------- home ----------

@app.route("/home")
@login_required
def home():
    recent = rows(Q.MY_RUNS, user_id=g.user["user_id"], limit=5)
    if g.user["role"] == "researcher":
        return render_template("home_researcher.html", recent=recent, experiments=rows(Q.EXPERIMENTS),
                               spent=costs.spend())
    return render_template("home_analyst.html", recent=recent,
                           databases=rows(Q.DATABASES_WITH_QUESTIONS), configs=rows(Q.CONFIGS))


# ---------- analyst use cases ----------

def _start_playground_run(question_id, config_id):
    """Shared by 'ask' and 'run benchmark question'. Returns run_id or raises ValueError."""
    if one(Q.CONFIG_EXISTS, config_id=config_id) is None:
        raise ValueError("unknown pipeline config")
    left = costs.remaining()
    if left is not None and left <= 0.05:
        raise ValueError("the project's API budget is used up; no new runs can start")
    if one(Q.RUNNING_FOR_USER, user_id=g.user["user_id"])["n"] >= MAX_RUNNING_PER_USER:
        raise ValueError(f"you already have {MAX_RUNNING_PER_USER} runs in progress; wait for one to finish")
    pg = one(Q.PLAYGROUND_ID)
    if pg is None or pg["is_frozen"]:
        raise ValueError("the playground experiment is not accepting runs")
    return pipeline.create_run(g.conn, pg["experiment_id"], config_id, question_id, g.user["user_id"])


@app.route("/ask", methods=["GET", "POST"])
@login_required
def ask():
    databases, configs = rows(Q.DATABASES_WITH_QUESTIONS), rows(Q.CONFIGS)
    if request.method == "POST":
        try:
            db_id = form_int("db_id", 1)
            if one(Q.DATABASE_EXISTS, db_id=db_id) is None:
                raise ValueError("unknown database")
            question = form_text("question", MAX_QUESTION)
            evidence = form_text("evidence", MAX_EVIDENCE, required=False) or None
            config_id = form_int("config_id", 1)
            qid = one(Q.INSERT_USER_QUESTION, db_id=db_id, question_text=question,
                      evidence_hint=evidence, user_id=g.user["user_id"])["question_id"]
            run_id = _start_playground_run(qid, config_id)
        except ValueError as e:
            g.conn.rollback()
            flash(str(e), "error")
            return render_template("ask.html", databases=databases, configs=configs, form=request.form), 400
        g.conn.commit()
        submit_run(run_id)
        return redirect(url_for("run_detail", run_id=run_id))
    return render_template("ask.html", databases=databases, configs=configs, form={})


@app.route("/questions")
@login_required
def questions():
    f = {
        "db_id": arg_int("db_id"),
        "difficulty": request.args.get("difficulty") if request.args.get("difficulty") in DIFFICULTIES else None,
        "table": (request.args.get("table") or "").strip()[:64] or None,
        "text": (request.args.get("text") or "").strip()[:200] or None,
    }
    return render_template("questions.html", questions=rows(Q.QUESTIONS, **f), f=f,
                           databases=rows(Q.DATABASES_WITH_QUESTIONS), difficulties=DIFFICULTIES)


@app.route("/questions/<int:question_id>")
@login_required
def question_detail(question_id):
    q = one(Q.QUESTION, question_id=question_id)
    # user-written questions are private to their author (and researchers)
    if q is None or (q["source"] == "user" and g.user["role"] != "researcher"
                     and q["created_by"] != g.user["user_id"]):
        abort(404, "question not found")
    return render_template(
        "question.html", q=q, tables=rows(Q.QUESTION_TABLES, question_id=question_id),
        trials=rows(Q.GOLD_TRIALS, question_id=question_id), configs=rows(Q.CONFIGS),
        runs=rows(Q.QUESTION_RUNS, question_id=question_id, user_id=g.user["user_id"],
                  is_researcher=g.user["role"] == "researcher"))


@app.route("/questions/<int:question_id>/run", methods=["POST"])
@login_required
def question_run(question_id):
    q = one(Q.QUESTION, question_id=question_id)
    if q is None or q["source"] != "bird":
        abort(404, "question not found")
    try:
        run_id = _start_playground_run(question_id, form_int("config_id", 1))
    except ValueError as e:
        g.conn.rollback()
        flash(str(e), "error")
        return redirect(url_for("question_detail", question_id=question_id))
    g.conn.commit()
    submit_run(run_id)
    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/runs")
@login_required
def my_runs():
    return render_template("runs.html", runs=rows(Q.MY_RUNS, user_id=g.user["user_id"], limit=200))


@app.route("/runs/<int:run_id>")
@login_required
def run_detail(run_id):
    run = one(Q.RUN, run_id=run_id)
    if run is None or not can_view_run(run):
        abort(404, "run not found")
    steps = rows(Q.RUN_STEPS, run_id=run_id)
    return render_template("run.html", run=run, steps=steps, calls=rows(Q.RUN_CALLS, run_id=run_id))


# ---------- researcher use cases ----------

@app.route("/experiments")
@researcher_required
def experiments():
    return render_template("experiments.html", experiments=rows(Q.EXPERIMENTS))


@app.route("/experiments/<int:experiment_id>")
@researcher_required
def experiment_detail(experiment_id):
    e = one(Q.EXPERIMENT, experiment_id=experiment_id)
    if e is None:
        abort(404, "experiment not found")
    p = {"experiment_id": experiment_id}
    status = request.args.get("status") if request.args.get("status") in ("running", "done", "failed", "timeout") else None
    return render_template(
        "experiment.html", e=e,
        summary=rows(Q.REPORT_SUMMARY, **p),
        slowfast=rows(Q.REPORT_SLOW_FAST, slow_ms=Q.SLOW_QUESTION_MS, **p),
        rewrites=rows(Q.REPORT_REWRITES, **p),
        effect=rows(Q.REPORT_REWRITE_EFFECT, **p),
        speedups=rows(Q.REPORT_SPEEDUPS, limit=10, **p),
        runs=rows(Q.EXPERIMENT_RUNS, status=status, **p),
        progress=next((x for x in rows(Q.EXPERIMENTS) if x["experiment_id"] == experiment_id), None))


def mark_orphaned_runs():
    """Runs left 'running' by a previous web server process can never finish."""
    with db.app_conn() as conn:
        db.execute(conn, """UPDATE run SET status = 'failed', finished_at = now(),
                                   error_detail = 'web server restarted before the run finished'
                            WHERE status = 'running' AND user_id IS NOT NULL""")


if __name__ == "__main__":
    mark_orphaned_runs()
    # 5000 is taken by the AirPlay receiver on macOS
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")), debug=False, threaded=True)
