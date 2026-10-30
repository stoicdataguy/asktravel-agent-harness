"""Deleting a chat in Open WebUI deletes the agent's copy of it.

Open WebUI keeps its tables in our database (schema `open_webui`, see docker-compose.yml) but has
no delete webhook. A trigger on its `chat` table queues every deleted chat, with its owner's
email, in `chat_deletions`; `sync_chat_deletions` (run every few seconds by the API) deletes the
matching checkpointed thread and chat-index entry and marks the row done. Archived chats are not
deleted in Open WebUI, so they're kept here too. Saved itineraries stay with their trip.
"""

import asyncio
import logging
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from psycopg_pool import AsyncConnectionPool

from travel_agent.tools.conversations import chats_namespace

logger = logging.getLogger(__name__)

# Separate statements: the pool prepares every statement, and a prepared one can't hold several
INSTALL_SQL = [
    """create table if not exists chat_deletions (
      id bigserial primary key,
      user_email text not null,
      chat_id text not null,
      deleted_at timestamptz not null default now(),
      processed_at timestamptz
    )""",
    """create or replace function queue_chat_deletion() returns trigger language plpgsql as $$
    begin
      insert into public.chat_deletions (user_email, chat_id)
      select lower(u.email), old.id from {schema}."user" u where u.id = old.user_id;
      return old;
    end $$""",
    "drop trigger if exists queue_chat_deletion on {schema}.chat",
    """create trigger queue_chat_deletion after delete on {schema}.chat
      for each row execute function queue_chat_deletion()""",
]


@dataclass
class Deletion:
    id: int
    user_email: str
    chat_id: str

    @property
    def thread_id(self) -> str:
        return f"{self.user_email}:{self.chat_id}"


async def install_trigger(pool: AsyncConnectionPool, schema: str) -> bool:
    """Create the queue and trigger once Open WebUI's tables exist; returns whether they are."""
    async with pool.connection() as conn:
        cur = await conn.execute("select to_regclass(%s)", (f"{schema}.chat",))
        if (await cur.fetchone())["to_regclass"] is None:
            return False
        for statement in INSTALL_SQL:
            await conn.execute(statement.format(schema=schema))
    return True


async def forget_chats(
    deletions: list[Deletion], checkpointer: BaseCheckpointSaver, store: BaseStore
) -> None:
    """Delete the agent's thread and chat-index entry for each deleted chat."""
    for deletion in deletions:
        await checkpointer.adelete_thread(deletion.thread_id)
        await store.adelete(chats_namespace(deletion.user_email), deletion.thread_id)


async def sync_chat_deletions(
    pool: AsyncConnectionPool, checkpointer: BaseCheckpointSaver, store: BaseStore
) -> int:
    """Process queued deletions; returns how many."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id, user_email, chat_id from chat_deletions where processed_at is null "
            "order by id limit 500"
        )
        deletions = [Deletion(**row) for row in await cur.fetchall()]
    if not deletions:
        return 0
    await forget_chats(deletions, checkpointer, store)
    async with pool.connection() as conn:
        await conn.execute(
            "update chat_deletions set processed_at = now() where id = any(%s)",
            ([d.id for d in deletions],),
        )
    logger.info("forgot %d chats deleted in Open WebUI", len(deletions))
    return len(deletions)


async def run_chat_deletion_sync(
    pool: AsyncConnectionPool,
    checkpointer: BaseCheckpointSaver,
    store: BaseStore,
    schema: str,
    interval: float,
) -> None:
    """Background loop: install the trigger when Open WebUI's tables appear, then drain the queue.

    Errors are logged and retried next round, so a database hiccup never stops the loop.
    """
    installed = False
    while True:
        try:
            installed = installed or await install_trigger(pool, schema)
            if installed:
                await sync_chat_deletions(pool, checkpointer, store)
        except Exception:
            logger.exception("chat deletion sync failed; retrying")
        await asyncio.sleep(interval)
