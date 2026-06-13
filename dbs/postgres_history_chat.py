from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from configs.config_service import get_settings

_async_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_validated_db_url() -> str:
    db_url = get_settings().session_database_url.strip()
    if not db_url:
        raise RuntimeError("SESSION_DATABASE_URL is not configured")

    if db_url.startswith("postgres://"):
        raise RuntimeError("SESSION_DATABASE_URL must use the postgresql+asyncpg:// scheme")

    if db_url.startswith("postgresql://"):
        raise RuntimeError("SESSION_DATABASE_URL must use the postgresql+asyncpg:// scheme")

    if db_url.startswith("postgresql+") and not db_url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("SESSION_DATABASE_URL must use the postgresql+asyncpg:// scheme")

    if not db_url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("SESSION_DATABASE_URL must use the postgresql+asyncpg:// scheme")

    return db_url


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _async_engine, _session_factory
    if _session_factory is None:
        _async_engine = create_async_engine(
            _get_validated_db_url(),
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        _session_factory = async_sessionmaker(
            bind=_async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _session_factory


async def get_db_session() -> AsyncSession:
    """
    Create an async SQLAlchemy session for the ADK PostgreSQL store.

    Returns:
        AsyncSession: SQLAlchemy async session
    """
    return get_session_factory()()


async def close_db_session(session: AsyncSession) -> None:
    await session.close()


async def dispose_db_engine() -> None:
    global _async_engine, _session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
    _async_engine = None
    _session_factory = None
