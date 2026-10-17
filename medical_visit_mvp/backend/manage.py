"""Administration command line (run from the medical_visit_mvp folder with the project venv).

  python -m backend.manage create-admin --username <name> --display "<full name>"
        First administrator. The password is read twice from a hidden prompt (never a command-line
        argument, so it is not stored in shell history). Refused once an admin exists.
  python -m backend.manage migrate        Apply schema migrations (a backup is written first).
  python -m backend.manage info           Database path, schema revision and row count per table.
  python -m backend.manage verify-audit   Recompute the audit hash chain.
  python -m backend.manage purge          Apply the temporary-audio / pseudonym retention policy now.
Every command is itself audited where it changes data.
"""
import argparse
import getpass
import sys
from backend import db, security


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m backend.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin")
    admin.add_argument("--username", required=True)
    admin.add_argument("--display", required=True)
    for name in ("migrate", "info", "verify-audit", "purge"):
        sub.add_parser(name)
    parser.add_argument("--db", default=str(db.DB_PATH), help="database file (default: data/database/visits.sqlite)")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        applied = db.migrate(args.db)
        print("applied:", ", ".join(applied) if applied else "nothing (already at head)", "| head:", db.head_revision())
        return 0
    if args.command == "info":
        from pathlib import Path
        path = Path(args.db).resolve()
        if not path.exists():
            print(f"{path} does not exist yet (it is created on first start).")
            return 1
        print("database:", path)
        print("schema revision:", db.current_revision(path), "(head:", db.head_revision() + ")")
        for table, count in sorted(db.table_counts(path).items()):
            print(f"  {table:28} {count}")
        return 0

    from backend.repository import Conflict, PseudonymStore, Repository
    repo = Repository(args.db)
    try:
        if args.command == "create-admin":
            print(f"Password policy: at least {security.PASSWORD_MIN_LENGTH} characters, not containing the username.")
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Repeat password: "):
                print("Passwords do not match.", file=sys.stderr)
                return 1
            try:
                repo.bootstrap_admin(args.username, args.display, password)
            except (Conflict, ValueError) as exc:
                print(f"Refused: {exc}", file=sys.stderr)
                return 1
            print(f"Administrator '{args.username.strip().lower()}' created. Log in through the app to add doctors and patients.")
        elif args.command == "verify-audit":
            from backend import audit
            with repo._tx(False) as s:
                result = audit.verify(s)
            print(result)
            return 0 if result["ok"] else 2
        elif args.command == "purge":
            from backend import retention
            store = PseudonymStore()
            try:
                print(retention.purge(repo, store, db.TMP_AUDIO))
            finally:
                store.dispose()
        return 0
    finally:
        repo.dispose()


if __name__ == "__main__":
    sys.exit(main())
