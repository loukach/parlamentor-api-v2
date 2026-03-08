"""Seed script: create one test investigation with stage rows."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import app_engine, app_session_factory

STAGES = ["research", "analysis", "editorial", "visualization", "drafting", "qa"]


async def seed():
    async with app_session_factory() as session:
        session: AsyncSession

        # Check if seed data already exists
        result = await session.execute(
            text("SELECT id FROM investigations WHERE slug = 'habitacao-teste'")
        )
        if result.scalar():
            print("Seed data already exists -- skipping")
            return

        # Create test investigation
        inv_result = await session.execute(
            text("""
                INSERT INTO investigations (topic, slug, status, current_stage)
                VALUES (:topic, :slug, 'active', 'research')
                RETURNING id
            """),
            {
                "topic": "Que solucoes propoe cada partido para a crise habitacional?",
                "slug": "habitacao-teste",
            },
        )
        inv_id = inv_result.scalar()

        # Create stage rows
        for i, stage in enumerate(STAGES):
            status = "active" if i == 0 else "pending"
            await session.execute(
                text("""
                    INSERT INTO stages (investigation_id, stage, status)
                    VALUES (:inv_id, :stage, :status)
                """),
                {"inv_id": inv_id, "stage": stage, "status": status},
            )

        await session.commit()
        print(f"Seeded investigation {inv_id} with {len(STAGES)} stages")


async def main():
    try:
        await seed()
    finally:
        await app_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
