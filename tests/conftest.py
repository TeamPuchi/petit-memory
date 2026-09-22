"""Pytest fixtures for Memory MCP tests."""

from pathlib import Path

import pytest
import pytest_asyncio

from memory_mcp.config import MemoryConfig
from memory_mcp.sqlite_backend import SqliteMemoryStore
from memory_mcp.store import MemoryStore


@pytest.fixture
def temp_db_path(tmp_path: Path) -> str:
    """Create a temporary SQLite database path."""
    return str(tmp_path / "test_memory.db")


@pytest.fixture
def memory_config(temp_db_path: str) -> MemoryConfig:
    """Create test memory config."""
    return MemoryConfig(
        db_path=temp_db_path,
        collection_name="test_memories",
    )


@pytest_asyncio.fixture
async def memory_store(memory_config: MemoryConfig) -> MemoryStore:
    """Create and connect a memory store."""
    store = MemoryStore(memory_config)
    await store.connect()
    yield store
    await store.disconnect()


@pytest.fixture
def set_memory_timestamp():
    """保存済み記憶の timestamp を差し替えるテスト用ヘルパ.

    段0 までは MemoryStore._ensure_connected() で生の接続を取っていたが、
    「任意の SQL を実行する口」を本体に残さないために、保管層の実装を
    知っているのはこのヘルパだけにした。
    """

    async def _set(
        store: MemoryStore,
        timestamp: str,
        *,
        memory_id: str | None = None,
        content: str | None = None,
    ) -> None:
        backend = store.backend
        if not isinstance(backend, SqliteMemoryStore):
            raise NotImplementedError(
                f"set_memory_timestamp supports SqliteMemoryStore only, got {type(backend).__name__}"
            )
        db = backend.connection
        if memory_id is not None:
            db.execute("UPDATE memories SET timestamp = ? WHERE id = ?", (timestamp, memory_id))
        elif content is not None:
            db.execute("UPDATE memories SET timestamp = ? WHERE content = ?", (timestamp, content))
        else:
            db.execute("UPDATE memories SET timestamp = ?", (timestamp,))
        db.commit()

    return _set
