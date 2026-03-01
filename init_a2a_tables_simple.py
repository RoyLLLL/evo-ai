"""
A2A Database Tables Initialization Script (Simplified)

This script initializes the A2A database tables by creating store instances.
The tables will be automatically created when the stores are instantiated.
"""

import asyncio
import logging
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from a2a.server.tasks import DatabaseTaskStore, DatabasePushNotificationConfigStore

from src.config.settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def init_a2a_tables():
    """Initialize A2A database tables."""

    # Create async engine
    async_connection_string = settings.POSTGRES_CONNECTION_STRING.replace(
        "postgresql://", "postgresql+asyncpg://"
    )

    logger.info(f"Connecting to database...")
    logger.info(f"Database: {async_connection_string.split('@')[1]}")

    async_engine = create_async_engine(
        async_connection_string,
        echo=False,  # Set to True to see SQL statements
        pool_pre_ping=True,
    )

    try:
        logger.info("\n📋 Creating A2A database tables...")

        # Create tasks table by instantiating DatabaseTaskStore
        logger.info("  - Creating a2a_tasks table...")
        task_store = DatabaseTaskStore(
            engine=async_engine,
            create_table=True,
            table_name="a2a_tasks",
        )
        logger.info("    ✅ a2a_tasks table ready")

        # Create push configs table by instantiating DatabasePushNotificationConfigStore
        logger.info("  - Creating a2a_push_configs table...")
        push_config_store = DatabasePushNotificationConfigStore(
            engine=async_engine,
            create_table=True,
            table_name="a2a_push_configs",
            encryption_key=getattr(settings, 'A2A_ENCRYPTION_KEY', None),
        )
        logger.info("    ✅ a2a_push_configs table ready")

        # Verify tables were created
        logger.info("\n🔍 Verifying tables...")
        async with async_engine.connect() as conn:
            result = await conn.execute(text("""
                SELECT table_name,
                       (SELECT COUNT(*)
                        FROM information_schema.columns
                        WHERE table_name = t.table_name
                        AND table_schema = 'public') as column_count
                FROM information_schema.tables t
                WHERE table_schema = 'public'
                AND table_name LIKE 'a2a_%'
                ORDER BY table_name
            """))
            tables = result.fetchall()

            if tables:
                logger.info("\n✅ A2A tables in database:")
                for table_name, col_count in tables:
                    logger.info(f"   • {table_name} ({col_count} columns)")
            else:
                logger.warning("\n⚠️  No A2A tables found in database")
                logger.warning("   Tables may be created on first use")

        logger.info("\n🎉 A2A database initialization complete!")
        logger.info("\n💡 Note: Tables are created automatically when first accessed.")
        logger.info("   You can now start using the A2A routes.")

    except Exception as e:
        logger.error(f"\n❌ Error initializing tables: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise

    finally:
        await async_engine.dispose()
        logger.info("\n🔌 Database connection closed")


if __name__ == "__main__":
    print("=" * 70)
    print("A2A Database Tables Initialization")
    print("=" * 70)
    asyncio.run(init_a2a_tables())
