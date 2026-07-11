"""Alembic environment — targets SQLModel metadata, URL from MELD7T_DB_URL (spec §22)."""
from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from app import models  # noqa: F401  (registers all tables on SQLModel.metadata)
from app.config import settings

config = context.config
config.set_main_option("sqlalchemy.url", settings.db_url)
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(url=settings.db_url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine

    engine = create_engine(settings.db_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
