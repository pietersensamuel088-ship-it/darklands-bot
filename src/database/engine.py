"""
engine.py – Async SQLAlchemy engine and session factory.
Uses SQLite by default (DATABASE_URL env var overrides to PostgreSQL, etc.).
"""
import os
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///data/darklands.db",
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
    """Create all tables (idempotent)."""
    from database.models import (  # noqa: F401 – needed for metadata
        DiscordTicket,
        DiscordSuggestion,
        DiscordQuestion,
        DiscordBugReport,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
