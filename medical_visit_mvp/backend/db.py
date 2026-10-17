"""Database location, engines and migrations.

Paths (relative to the project folder medical_visit_mvp/):
- data/database/visits.sqlite      visits, users, audit trail, assistant records
- data/database/pseudonyms.sqlite  placeholder -> original identifier mappings (kept apart)
- data/database/backups/           automatic copy taken before every schema migration
- data/runtime/tmp_audio/          temporary audio (never in the database; see backend/retention.py)

Databases created before this layout (data/runtime/*.sqlite) are copied to data/database/ on
first start with SQLite's online-backup API. The old file is renamed, never deleted.
"""
import logging
import sqlite3
import time
from pathlib import Path
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATABASE_DIR = DATA / "database"
BACKUP_DIR = DATABASE_DIR / "backups"
RUNTIME = DATA / "runtime"
TMP_AUDIO = RUNTIME / "tmp_audio"
DB_PATH = DATABASE_DIR / "visits.sqlite"
PSEUDONYM_PATH = DATABASE_DIR / "pseudonyms.sqlite"
LEGACY = {DB_PATH: RUNTIME / "visits.sqlite", PSEUDONYM_PATH: RUNTIME / "pseudonyms.sqlite"}
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def engine(path: Path, immediate: bool, foreign_keys: bool = True):
    """SQLite engine whose transactions are opened explicitly (BEGIN IMMEDIATE for writers)."""
    eng = create_engine(f"sqlite:///{path}", connect_args={"timeout": 10})

    @event.listens_for(eng, "connect")
    def _connect(dbapi_conn, _):
        dbapi_conn.isolation_level = None  # let the begin event control transactions
        if foreign_keys:
            dbapi_conn.execute("PRAGMA foreign_keys = ON")

    @event.listens_for(eng, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return eng


def copy_database(src: Path, dst: Path):
    """Consistent copy of a live SQLite file (online-backup API). Accepts str or Path."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:  # sqlite3's context manager only commits; close explicitly so Windows releases the files
        a.backup(b)
        ok = b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        a.close()
        b.close()
    if not ok:
        raise RuntimeError(f"Integrity check failed for copy {dst}")


def relocate_legacy(path: Path):
    """Move a pre-data/database file to its new location without deleting anything."""
    legacy = LEGACY.get(Path(path))
    if legacy and legacy.exists() and not Path(path).exists():
        copy_database(legacy, Path(path))
        legacy.rename(legacy.with_name(legacy.name + f".moved-to-data-database-{int(time.time())}"))
        LOG.warning("Database moved: %s -> %s (old file renamed, not deleted)", legacy.name, path)


def _config():
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(_config()).get_current_head()


def current_revision(path: Path):
    eng = engine(path, immediate=False, foreign_keys=False)
    try:
        with eng.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        eng.dispose()


def migrate(path: Path) -> list[str]:
    """Bring the database at `path` to the latest schema. Returns the revisions applied.

    - A pre-migration database (tables but no alembic_version) is stamped at the baseline 0001.
    - Before upgrading a non-empty database, a backup copy is written to data/database/backups/.
    - Runs with foreign-key enforcement off (SQLite table rebuilds need it) inside one
      transaction, then fails loudly if any foreign-key violation remains.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    relocate_legacy(path)
    cfg = _config()
    head = head_revision()
    eng = engine(path, immediate=True, foreign_keys=False)
    try:
        with eng.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            current = MigrationContext.configure(conn).get_current_revision()
        if current is None and "users" in tables:
            with eng.begin() as conn:
                cfg.attributes["connection"] = conn
                command.stamp(cfg, "0001")
            current = "0001"
        if current == head:
            return []
        if tables:
            backup = path.parent / "backups" / f"{path.stem}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-before-{head}.sqlite"
            copy_database(path, backup)
            LOG.warning("Schema migration %s -> %s; backup written to %s", current, head, backup.name)
        with eng.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            problems = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"Foreign-key violations after migration: {problems[:5]}")
        script = ScriptDirectory.from_config(cfg)
        return [r.revision for r in script.iterate_revisions(head, current)][::-1]
    finally:
        eng.dispose()


def table_counts(path: Path) -> dict:
    eng = engine(path, immediate=False)
    try:
        with eng.connect() as conn:
            names = inspect(conn).get_table_names()
            return {n: conn.execute(text(f'SELECT COUNT(*) FROM "{n}"')).scalar() for n in names}
    finally:
        eng.dispose()
