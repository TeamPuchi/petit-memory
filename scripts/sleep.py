"""Cron script: run memory sleep (consolidation/decay/forgetting)."""

import asyncio
import json

from memory_mcp.config import MemoryConfig
from memory_mcp.sleep import SleepEngine
from memory_mcp.store import MemoryStore


async def main():
    store = MemoryStore(MemoryConfig.from_env())
    await store.connect()
    try:
        engine = SleepEngine(store)
        stats = await engine.run(dry_run=False)
        print(json.dumps({
            "merged": len(stats.merged),
            "decayed": len(stats.decayed),
            "forgotten": len(stats.forgotten),
            "protected": stats.protected,
        }))
    finally:
        await store.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
