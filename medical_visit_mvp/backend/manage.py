"""Administration command line (run from the medical_visit_mvp folder with the project venv).

  python -m backend.manage create-admin --username <name> --display "<full name>"
        First administrator. The password is read twice from a hidden prompt (never a command-line
        argument, so it is not stored in shell history). Refused once an admin exists.
  python -m backend.manage migrate        Apply schema migrations (a backup is written first).
  python -m backend.manage info           Database path, schema revision and row count per table.
  python -m backend.manage verify-audit   Recompute the audit hash chain.
  python -m backend.manage purge          Apply the temporary-audio / pseudonym retention policy now.
  python -m backend.manage backup [--out DIR]
        Consistent copy of both databases (contents stay encrypted) into DIR (default data/database/backups),
        followed by an integrity check and a test decryption. Schedule it daily (deployment/README.md).
  python -m backend.manage verify-backup FILE
        Restore test: opens a backup copy, checks integrity, decrypts sample rows and verifies the audit chain.
  python -m backend.manage key-info       Where the data encryption key is kept and how it is protected.
  python -m backend.manage export-key --out FILE
        Escrow copy of the data encryption key, for offline storage (e.g. a safe). Without the key, encrypted
        data and backups cannot be read.
  python -m backend.manage reset-mfa --username <name>
        Reset a user's authenticator when only one administrator exists (needs access to this server).
  python -m backend.manage seal-plaintext
        Encrypt (whole file, AES-256-GCM) every database copy under data/ that still holds unencrypted content:
        backups taken before encryption at rest and the renamed legacy files in data/runtime/.
  python -m backend.manage unseal FILE.enc --out FILE
        Decrypt a sealed file again (e.g. to restore an old backup).
Every command is itself audited where it changes data.
"""
import argparse
import getpass
import os
import sqlite3
import sys
import time
from pathlib import Path
from backend import crypto, db, security


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m backend.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin")
    admin.add_argument("--username", required=True)
    admin.add_argument("--display", required=True)
    for name in ("migrate", "info", "verify-audit", "purge", "key-info"):
        sub.add_parser(name)
    sub.add_parser("backup").add_argument("--out", default=str(db.BACKUP_DIR))
    sub.add_parser("verify-backup").add_argument("file")
    sub.add_parser("export-key").add_argument("--out", required=True)
    sub.add_parser("reset-mfa").add_argument("--username", required=True)
    sub.add_parser("seal-plaintext")
    unseal = sub.add_parser("unseal")
    unseal.add_argument("file")
    unseal.add_argument("--out", required=True)
    parser.add_argument("--db", default=str(db.DB_PATH), help="database file (default: data/database/visits.sqlite)")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        applied = db.migrate(args.db)
        print("applied:", ", ".join(applied) if applied else "nothing (already at head)", "| head:", db.head_revision())
        return 0
    if args.command == "seal-plaintext":
        found = plaintext_copies(db.DATA)
        for path in found:
            print("sealed:", crypto.seal_file(path))
        print(f"{len(found)} file(s) sealed." if found else "No unencrypted database copies found.")
        return 0
    if args.command == "unseal":
        out = Path(args.out)
        if out.exists():
            print(f"Refused: {out} already exists.", file=sys.stderr)
            return 1
        out.write_bytes(crypto.unseal_bytes(Path(args.file)))
        print(f"Decrypted to {out}. It holds unencrypted data: delete it when you are done.")
        return 0
    if args.command == "key-info":
        print("data encryption key:", crypto.key_protection())
        print("audit HMAC key:", "AUDIT_HMAC_KEY environment variable" if os.getenv("AUDIT_HMAC_KEY") else
              "derived from the data encryption key (HKDF)")
        return 0
    if args.command == "export-key":
        out = Path(args.out)
        if out.exists():
            print(f"Refused: {out} already exists.", file=sys.stderr)
            return 1
        out.write_text(crypto.export_key() + "\n", encoding="ascii")
        print(f"Key written to {out}. Store it offline (e.g. a safe), then delete this file from the computer.")
        print("To use it on another computer, set DATA_ENCRYPTION_KEY to its contents.")
        return 0
    if args.command == "backup":
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out = Path(args.out)
        written = []
        for src in (Path(args.db), Path(args.db).with_name("pseudonyms.sqlite")):
            if src.exists():
                dst = out / f"{src.stem}-{stamp}-backup.sqlite"
                db.copy_database(src, dst)
                written.append(dst)
        for dst in written:
            print("backup:", dst, "|", verify_backup(dst))
        return 0
    if args.command == "verify-backup":
        result = verify_backup(Path(args.file))
        print(result)
        return 0 if result.startswith("ok") else 2
    if args.command == "info":
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
        elif args.command == "reset-mfa":
            repo.reset_mfa_cli(args.username.strip().lower())
            print(f"MFA reset for '{args.username}'. The user sets up an authenticator again at the next login.")
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


def plaintext_copies(root: Path) -> list[Path]:
    """SQLite files under `root` (except the live databases) that hold any unencrypted name, text or mapping."""
    live = {db.DB_PATH.resolve(), db.PSEUDONYM_PATH.resolve()}
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix == ".enc" or path.resolve() in live:
            continue
        if path.read_bytes()[:16] != b"SQLite format 3\x00":
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            values = []
            for table, column in (("users", "display"), ("transcript_segments", "text"), ("summary_versions", "content"),
                                  ("mappings", "original")):
                if table in tables:
                    values += [v for (v,) in con.execute(f"SELECT {column} FROM {table}") if v is not None]
        finally:
            con.close()
        if any(not crypto.is_encrypted(v) for v in values):
            found.append(path)
    return found


def verify_backup(path: Path) -> str:
    """Integrity check, test decryption of every encrypted value in one sample row per table, and audit-chain check."""
    if not path.exists():
        return f"missing: {path}"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return "FAILED: integrity check"
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checked = 0
        for table, column in (("users", "display"), ("transcript_segments", "text"), ("summary_versions", "content"),
                              ("mappings", "original")):
            if table in tables:
                for (value,) in con.execute(f"SELECT {column} FROM {table} LIMIT 20"):
                    crypto.decrypt(value)
                    checked += 1
    except crypto.CryptoError as exc:
        return f"FAILED: {exc}"
    finally:
        con.close()
    if "audit_events" in tables:
        from backend import audit
        from sqlalchemy.orm import Session
        eng = db.engine(path, immediate=False)
        try:
            with Session(eng) as s:
                chain = audit.verify(s)
        finally:
            eng.dispose()
        if not chain["ok"]:
            return f"FAILED: audit chain ({chain['problem']} at {chain['first_bad_seq']})"
        return f"ok: integrity, {checked} values decrypted, audit chain of {chain['checked']} events verified"
    return f"ok: integrity, {checked} values decrypted"


if __name__ == "__main__":
    sys.exit(main())
