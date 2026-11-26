"""Trusted setup: create an account of any role (researcher accounts can only be made here).

    python scripts/create_user.py --email ss21034@nyu.edu --name "Shreyansh Saurabh" --role researcher
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.security import generate_password_hash  # noqa: E402

from t2s import db  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--role", choices=["analyst", "researcher"], default="researcher")
    ap.add_argument("--password", help="omit to be prompted")
    a = ap.parse_args()
    pw = a.password or getpass.getpass("password: ")
    if len(pw) < 8:
        raise SystemExit("password must be at least 8 characters")
    with db.app_conn() as conn:
        row = db.query_one(conn, """
            INSERT INTO app_user (email, name, password_hash, role) VALUES (%s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, password_hash = EXCLUDED.password_hash,
                                              role = EXCLUDED.role
            RETURNING user_id""", (a.email.strip().lower(), a.name.strip(), generate_password_hash(pw), a.role))
    print(f"user {row['user_id']} ({a.role}) ready")


if __name__ == "__main__":
    main()
