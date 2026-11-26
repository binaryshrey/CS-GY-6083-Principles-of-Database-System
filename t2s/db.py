"""Connections to the results database. Every query in the project goes through
psycopg with bound parameters; no SQL is built by string formatting of user input."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config


def connect(autocommit=False):
    conn = psycopg.connect(config.APP_DSN, row_factory=dict_row, autocommit=autocommit)
    with conn.cursor() as cur:
        cur.execute("SET search_path = t2s")
    if not autocommit:
        conn.commit()
    return conn


@contextmanager
def app_conn():
    """One connection per unit of work; commit on success, roll back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
