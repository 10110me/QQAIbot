import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from osa.infrastructure.schema import Base

config = context.config
if os.environ.get("OSA_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["OSA_DATABASE_URL"].replace("%", "%%"))
# Read only the database selector from .env; migrations do not require bot credentials.
elif os.path.exists(".env"):
    from dotenv import dotenv_values
    url = dotenv_values(".env").get("OSA_DATABASE_URL")
    if url:
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))


def sync_migrations(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = async_engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(sync_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
