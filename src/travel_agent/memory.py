from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def user_namespace(user_id: str, kind: str) -> tuple[str, str]:
    """Store namespace for one user's data of a given kind (memories, itineraries, ...).

    Store namespace labels can't contain "."; user ids are emails, so the dots are percent-encoded.
    """
    return (user_id.replace(".", "%2E"), kind)


@asynccontextmanager
async def open_memory(
    database_url: str,
) -> AsyncIterator[tuple[AsyncPostgresSaver, AsyncPostgresStore]]:
    """Postgres-backed checkpointer (thread memory) and store (long-term memory) on one pool."""
    async with AsyncConnectionPool(
        database_url,
        open=False,
        # Settings required by the LangGraph Postgres saver/store
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    ) as pool:
        checkpointer = AsyncPostgresSaver(pool)
        store = AsyncPostgresStore(pool)
        await checkpointer.setup()
        await store.setup()
        yield checkpointer, store
