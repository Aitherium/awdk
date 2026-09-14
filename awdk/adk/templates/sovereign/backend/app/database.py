"""Database setup — async SQLite."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from .config import settings
from .models import Base

_log = logging.getLogger(__name__)

_db_url = f"sqlite+aiosqlite:///{settings.db_path}"
engine = create_async_engine(_db_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
