import asyncio
from unittest.mock import AsyncMock

from datasette.app import Datasette
import pytest

from datasette_agent.schema import ensure_tables


@pytest.mark.asyncio
async def test_ensure_tables_once_per_database(monkeypatch):
    for _ in range(2):
        db = Datasette(memory=True).get_internal_database()
        write = AsyncMock(wraps=db.execute_write_script)
        read = AsyncMock(wraps=db.execute)
        monkeypatch.setattr(db, "execute_write_script", write)
        monkeypatch.setattr(db, "execute", read)

        await ensure_tables(db)
        await ensure_tables(db)

        assert write.await_count == 1
        assert read.await_count == 1
        assert "agent_conversations" in await db.table_names()


@pytest.mark.asyncio
async def test_concurrent_ensure_tables(monkeypatch):
    db = Datasette(memory=True).get_internal_database()
    started = asyncio.Event()
    release = asyncio.Event()
    original_write = db.execute_write_script

    async def delayed_write(sql):
        started.set()
        await release.wait()
        await original_write(sql)

    write = AsyncMock(side_effect=delayed_write)
    monkeypatch.setattr(db, "execute_write_script", write)
    first = asyncio.create_task(ensure_tables(db))
    await started.wait()
    second = asyncio.create_task(ensure_tables(db))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)

    assert write.await_count == 1
    assert "agent_conversations" in await db.table_names()


@pytest.mark.asyncio
async def test_ensure_tables_retries_failed_migration(monkeypatch):
    db = Datasette(memory=True).get_internal_database()
    await db.execute_write(
        "CREATE TABLE agent_questions (id TEXT PRIMARY KEY, "
        "conversation_id TEXT, status TEXT)"
    )
    original_write = db.execute_write
    write = AsyncMock(side_effect=RuntimeError("migration failed"))
    monkeypatch.setattr(db, "execute_write", write)

    with pytest.raises(RuntimeError, match="migration failed"):
        await ensure_tables(db)

    monkeypatch.setattr(db, "execute_write", original_write)
    await ensure_tables(db)
    columns = await db.table_columns("agent_questions")
    assert "html" in columns
    await ensure_tables(db)
