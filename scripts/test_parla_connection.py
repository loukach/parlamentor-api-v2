"""Test Parla! DB connection and run simple query."""

import asyncio

from sqlalchemy import text

from api.db import parla_engine, parla_session_factory


async def test_parla():
    async with parla_session_factory() as session:
        result = await session.execute(text("SELECT COUNT(*) as count FROM iniciativas"))
        count = result.scalar()
        print(f"Parla! DB connected — {count} initiatives found")


async def main():
    try:
        await test_parla()
    finally:
        await parla_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
