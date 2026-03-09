"""Dual async database engines and session factories."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.config import settings

# App database (read-write)
app_engine = create_async_engine(settings.app_db_url, pool_size=5)
app_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)

# Parla! database (read-only)
parla_engine = create_async_engine(
    settings.parla_db_url,
    pool_size=3,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"server_settings": {"default_transaction_read_only": "on"}},
)
parla_session_factory = async_sessionmaker(parla_engine, expire_on_commit=False)


async def get_app_db() -> AsyncGenerator[AsyncSession]:
    async with app_session_factory() as session:
        yield session


async def get_parla_db() -> AsyncGenerator[AsyncSession]:
    async with parla_session_factory() as session:
        yield session
