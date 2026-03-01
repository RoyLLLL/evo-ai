"""
A2A Database Tables Initialization Script

Creates the necessary database tables for A2A SDK:
- a2a_tasks - Task storage table
- a2a_push_configs - Push notification config table

Run this script to initialize the database tables before using the A2A routes.
"""

import asyncio
import logging
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from src.config.settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def init_a2a_tables():
    """Initialize A2A database tables."""

    # Create async engine
    async_connection_string = settings.POSTGRES_CONNECTION_STRING.replace(
        "postgresql://", "postgresql+asyncpg://"
    )

    logger.info(f"Connecting to database: {async_connection_string.split('@')[1]}")

    async_engine = create_async_engine(
        async_connection_string,
        echo=True,  # Show SQL statements
        pool_pre_ping=True,
    )

    try:
        # Import the model creation functions
        from a2a.server.models import create_task_model, create_push_notification_config_model

        # Create tasks table model
        logger.info("Creating tasks table model...")
        TaskModel = create_task_model(async_engine, "a2a_tasks")

        # Create push configs table model
        logger.info("Creating push notification configs table model...")
        PushConfigModel = create_push_notification_config_model(
            async_engine, "a2a_push_configs"
        )

        # Create all tables
        logger.info("Creating database tables...")
        async with async_engine.begin() as conn:
            await conn.run_sync(TaskModel.metadata.create_all)
            await conn.run_sync(PushConfigModel.metadata.create_all)

        logger.info("\n🎉 All A2A database tables initialized successfully!")

        # Verify tables were created
        async with async_engine.connect() as conn:
            result = await conn.execute(text("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name LIKE 'a2a_%'
                ORDER BY table_name
            """))
            tables = result.fetchall()

            if tables:
                logger.info("\nCreated tables:")
                for table in tables:
                    logger.info(f"  ✅ {table[0]}")
            else:
                logger.warning("\n⚠️ No A2A tables found in database")

    except Exception as e:
        logger.error(f"❌ Error initializing tables: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise

    finally:
        await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(init_a2a_tables())
