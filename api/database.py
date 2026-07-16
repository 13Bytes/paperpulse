"""Database engine and session helpers."""

from collections.abc import Iterator
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.settings import load_app_settings


class Base(DeclarativeBase):
    pass


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or load_app_settings().database_url
    is_memory = url == "sqlite:///:memory:"
    if url.startswith("sqlite:///") and not is_memory:
        db_path = Path(url.removeprefix("sqlite:///"))
        if not db_path.is_absolute():
            db_path = load_app_settings().project_dir / db_path
            url = f"sqlite:///{db_path.as_posix()}"
        db_path.parent.mkdir(parents=True, exist_ok=True)
    engine_kwargs = {
        "connect_args": {"check_same_thread": False, "timeout": 30}
        if url.startswith("sqlite")
        else {},
    }
    if is_memory:
        engine_kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **engine_kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def create_schema(target_engine: Engine | None = None) -> None:
    from api import db_models  # noqa: F401, PLC0415

    Base.metadata.create_all(target_engine or engine)


def schema_revisions(target_engine: Engine | None = None) -> tuple[str | None, str]:
    """Return the database revision and the migration head expected by this checkout."""
    target = target_engine or engine
    config_path = load_app_settings().project_dir / "alembic.ini"
    config = Config(str(config_path))
    script = ScriptDirectory.from_config(config)
    with target.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return current, script.get_current_head()


def assert_schema_current(target_engine: Engine | None = None) -> None:
    current, expected = schema_revisions(target_engine)
    if current != expected:
        raise RuntimeError(
            "Database schema is not current "
            f"(database={current or 'unversioned'}, expected={expected}). "
            "Run `alembic upgrade head` before starting Paperpulse."
        )
