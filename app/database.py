import fcntl
from pathlib import Path

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    # API and worker may start simultaneously against the same SQLite file.
    # Serialize schema creation/migrations to avoid CREATE TABLE races.
    lock_path = Path(settings.db_path).parent / ".lexiquest-db-init.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        SQLModel.metadata.create_all(engine)
        # Tiny forward migration for existing Cake installations. Existing
        # database rows and downloaded media are preserved.
        with engine.begin() as conn:
            _ensure_column(conn, "phrase", "provider", "TEXT DEFAULT 'source'")
            _ensure_column(conn, "flashcard", "source_word", "TEXT")
            _ensure_column(conn, "flashcard", "dictionary_name", "TEXT")
            _ensure_column(conn, "flashcard", "pronunciation", "TEXT")
            _ensure_column(conn, "flashcard", "source_phrase_snapshot", "TEXT")
            _ensure_column(conn, "flashcard", "target_phrase_snapshot", "TEXT")
            _ensure_column(conn, "flashcard", "clip_start", "FLOAT")
            _ensure_column(conn, "flashcard", "clip_end", "FLOAT")
            _ensure_column(conn, "flashcard", "exported_at", "TIMESTAMP")
            _ensure_column(conn, "mined_cards", "pronunciation", "TEXT")
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_flashcard_source_word ON flashcard(source_word)"))
            # v0.9: normalize the common Kazakhstan country-code typo KZ to
            # the ISO 639-1 language code for Kazakh: KK. This repairs lessons
            # created by older mobile UI versions without touching media.
            conn.execute(text("UPDATE video SET source_lang='kk' WHERE lower(source_lang) IN ('kz','kaz')"))
            conn.execute(text("UPDATE video SET target_lang='kk' WHERE lower(target_lang) IN ('kz','kaz')"))
            conn.execute(text("UPDATE dictionary SET source_lang='kk' WHERE lower(source_lang) IN ('kz','kaz')"))
            conn.execute(text("UPDATE dictionary SET target_lang='kk' WHERE lower(target_lang) IN ('kz','kaz')"))
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def get_session():
    with Session(engine) as session:
        yield session
