"""
engine.py – Async SQLAlchemy engine and session factory.
Uses SQLite by default (DATABASE_URL env var overrides to PostgreSQL, etc.).
"""
import os
import pathlib
from contextlib import asynccontextmanager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

_DEFAULT_DB = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "darklands.db"
_DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{_DEFAULT_DB.as_posix()}",
)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    # SQLite-specific: prevent "database is locked" on concurrent writes
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

class Base(DeclarativeBase):
    pass

@asynccontextmanager
async def get_session():
    """Async context manager that yields a session and commits/rolls back."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

async def init_db():
    """Create all tables and perform lightweight SQLite migrations (idempotent)."""
    from database.models import (  # noqa: F401 – needed for metadata
        DiscordTicket,
        DiscordSuggestion,
        DiscordQuestion,
        DiscordBugReport,
        DiscordGangRecruitment,
        DiscordApplication,
        MinecraftStatusHistory,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _migrate_columns(sync_conn):
            try:
                res = sync_conn.execute(text("PRAGMA table_info(discord_bug_reports);")).fetchall()
                existing_cols = {row[1] for row in res}
                cols_to_add = [
                    ("minecraft_ign", "VARCHAR(128)"),
                    ("expected_behavior", "TEXT"),
                    ("evidence", "TEXT"),
                    ("ticket_channel_id", "BIGINT"),
                ]
                for col_name, col_type in cols_to_add:
                    if col_name not in existing_cols:
                        sync_conn.execute(text(f"ALTER TABLE discord_bug_reports ADD COLUMN {col_name} {col_type};"))
            except Exception:
                pass

        await conn.run_sync(_migrate_columns)
