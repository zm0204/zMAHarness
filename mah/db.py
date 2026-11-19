"""Additive SQLite storage for the offline Grant Ledger."""
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cards (
        card_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, repo_root TEXT NOT NULL,
        task_key TEXT NOT NULL, trigger_ids TEXT NOT NULL,
        prepare_mode TEXT NOT NULL CHECK (prepare_mode IN ('card', 'prep_then_card', 'explicit')),
        content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'EXPIRED'))
    )""",
    """CREATE TABLE IF NOT EXISTS grants (
        grant_id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(card_id),
        repo_root TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'standard' CHECK (kind = 'standard'),
        status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'EXHAUSTED', 'EXPIRED', 'CLOSED', 'SUPERSEDED')),
        approved_at TEXT NOT NULL,
        approved_via TEXT NOT NULL CHECK (approved_via IN ('codex_prompt', 'manual_cli', 'windows_gui')),
        approval_evidence TEXT, expires_at TEXT NOT NULL,
        allowed_templates TEXT NOT NULL, allowed_targets TEXT NOT NULL,
        max_dispatches INTEGER NOT NULL CHECK (max_dispatches >= 1),
        used_dispatches INTEGER NOT NULL DEFAULT 0 CHECK (used_dispatches BETWEEN 0 AND max_dispatches),
        max_concurrency INTEGER NOT NULL DEFAULT 1 CHECK (max_concurrency = 1),
        superseded_by TEXT, dispatch_scope_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, grant_id TEXT REFERENCES grants(grant_id),
        parent_run_id TEXT, trigger_id TEXT, template TEXT NOT NULL, adapter TEXT NOT NULL,
        model_requested TEXT NOT NULL, effort_requested TEXT,
        model_observed TEXT, effort_observed TEXT, state TEXT NOT NULL, state_reason TEXT,
        repo_root TEXT NOT NULL, base_commit TEXT,
        workspace_kind TEXT NOT NULL CHECK (
            workspace_kind IN ('read_only', 'worktree', 'disposable_worktree', 'gui_fixed')
        ),
        workspace_path TEXT NOT NULL, task_sha256 TEXT NOT NULL, result_sha256 TEXT,
        conversation_id TEXT, pid INTEGER, cli_version TEXT, created_at TEXT NOT NULL,
        launched_at TEXT, finished_at TEXT, exit_code INTEGER,
        dispatch_consumed INTEGER NOT NULL DEFAULT 1 CHECK (dispatch_consumed IN (0, 1)),
        acceptance_status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (acceptance_status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'MERGED', 'DISCARDED')),
        acceptance_reason TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS run_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
        ts TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('mah', 'controller', 'user', 'adapter')),
        detail_json TEXT
    )""",
)


def connect(database):
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def connect_read_only(database):
    """Open an existing Ledger with SQLite mode=ro: it is never created, migrated or written."""
    db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


@contextmanager
def transaction(database, *, foreign_keys=True):
    with closing(connect(database)) as db, db:
        if not foreign_keys:
            db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN IMMEDIATE")
        yield db


def backup(database):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = database.with_name(f"{database.name}.v1.bak.{stamp}")
    with NamedTemporaryFile(dir=database.parent, prefix=destination.name + ".", delete=False) as file:
        temporary = Path(file.name)
    try:
        # The migration connection holds the write reservation. A separate reader can
        # copy committed pages (including WAL data) without backing up its own transaction.
        with closing(connect(database)) as source, closing(connect(temporary)) as target:
            source.backup(target)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(database):
    existed = database.exists()
    # Rebuilding the grants CHECK must preserve the runs foreign-key references.
    with transaction(database, foreign_keys=False) as db:
        tables = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        if "schema_meta" in tables:
            version = db.execute(
                "SELECT value FROM schema_meta WHERE key = 'ledger_schema_version'"
            ).fetchone()
            if version is not None:
                if version["value"] not in ("1", "2", "3") or not {"cards", "grants", "runs", "run_events"} <= tables:
                    raise ValueError("Ledger schema is unsupported or incomplete")
                if version["value"] != "3":
                    backup(database)
                    if version["value"] == "1":
                        db.execute("ALTER TABLE grants ADD COLUMN dispatch_scope_json TEXT")
                    grant_schema = next(statement for statement in SCHEMA if "CREATE TABLE IF NOT EXISTS grants (" in statement)
                    db.execute(grant_schema.replace("IF NOT EXISTS grants (", "grants_next (", 1))
                    db.execute("INSERT INTO grants_next SELECT * FROM grants")
                    db.execute("DROP TABLE grants")
                    db.execute("ALTER TABLE grants_next RENAME TO grants")
                    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise ValueError("Ledger migration would break existing foreign keys")
                    db.execute("UPDATE schema_meta SET value = '3' WHERE key = 'ledger_schema_version'")
                return
        if tables & {"cards", "grants", "runs", "run_events"}:
            raise ValueError("Existing Ledger tables are missing a migration version")
        if existed or tables:
            backup(database)
        for statement in SCHEMA:
            db.execute(statement)
        db.execute("INSERT INTO schema_meta (key, value) VALUES ('ledger_schema_version', '3')")
