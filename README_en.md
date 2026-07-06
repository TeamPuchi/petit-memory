# M5 Petit Memory

## [日本語ページ](./README.md)

A long-term memory MCP server for M5 Petit (and any Claude-based agent) — lets an AI remember across sessions.

Backed by **SQLite + numpy** (no external vector database required), with semantic embeddings (`intfloat/multilingual-e5-base`), BM25 hybrid re-ranking for Japanese/multilingual text, associative recall (Hopfield-style graph expansion), episodic memory, and a "sleep" consolidation cycle that merges, decays, and forgets old memories.

## Features

- **Semantic memory storage** — save memories with emotion tags, importance levels, and categories
- **Semantic search** — find relevant memories by natural-language query (cosine similarity via numpy)
- **BM25 hybrid re-ranking** — bigram BM25 index for Japanese/multilingual text
- **Context-based recall** — automatically recall memories relevant to the current conversation
- **Divergent recall** — associative graph exploration for creative, non-obvious retrieval
- **Working memory buffer** — fast access to recently activated memories
- **Episodic memory** — group memories into named episodes
- **Visual / audio memory** — save memories with a camera image or audio transcript
- **Theory of Mind (ToM)** — perspective-taking tool for understanding others' feelings
- **Causal links** — link memories with typed relations and trace causal chains
- **Sleep (consolidation)** — merge similar old memories, decay low-retention ones, forget unimportant ones
- **Single-file storage** — everything lives in one SQLite file, easy to back up and migrate

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

## Setup

Install uv first if you don't already have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
git clone https://github.com/PetitOnes/m5-petit-memory.git
cd m5-petit-memory
uv sync
uv run memory-mcp
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `~/.claude/memories/memory.db` | Path to the SQLite database file |
| `MEMORY_COLLECTION_NAME` | `claude_memories` | Collection name, stored as metadata |
| `MEMORY_EMBEDDING_MODEL` | `intfloat/multilingual-e5-base` | sentence-transformers model used for embeddings |
| `MEMORY_ENABLE_BM25` | `true` | Enable BM25 hybrid re-ranking (`false` to disable) |

## Claude Code integration

Add to your `.mcp.json` (or `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "memory": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/m5-petit-memory", "memory-mcp"]
    }
  }
}
```

## Migrating from ChromaDB

If you have existing memories in ChromaDB (older versions used `~/.claude/memories/chroma`), run the migration script:

```bash
cd m5-petit-memory

# Install chromadb temporarily (only needed for migration)
uv add --dev chromadb

# Run migration
uv run python scripts/migrate_chroma_to_sqlite.py \
    --source ~/.claude/memories/chroma \
    --dest ~/.claude/memories/memory.db

# Remove chromadb after migration
uv remove --dev chromadb
```

The script migrates all memories (content, embeddings, metadata), coactivation weights, and episodes.

> **Note**: The migration script temporarily installs `chromadb` as a dev dependency. It is not needed for normal operation and should be removed after migration.

## Tools

### remember

Save a memory to long-term storage.

```json
{
  "content": "Today I learned about SQLite performance tuning",
  "emotion": "excited",
  "importance": 4,
  "category": "technical"
}
```

### search_memories

Search memories by semantic similarity with optional filters.

```json
{
  "query": "things I learned about databases",
  "n_results": 5,
  "category_filter": "technical",
  "emotion_filter": "excited"
}
```

### recall

Recall relevant memories based on conversation context.

```json
{
  "context": "We were discussing database optimization",
  "n_results": 3
}
```

### recall_divergent

Divergent associative recall — explores the memory graph to surface non-obvious connections.

```json
{
  "context": "late night coding session",
  "n_results": 5,
  "max_branches": 3,
  "max_depth": 3,
  "temperature": 0.7
}
```

### recall_with_associations

Recall memories along with their linked memories.

```json
{
  "context": "first time I saw the night sky",
  "n_results": 3,
  "chain_depth": 2
}
```

### list_recent_memories

List the most recent memories.

```json
{
  "limit": 10,
  "category_filter": "memory"
}
```

### get_memory_stats

Get statistics about stored memories (count by category, emotion).

### get_working_memory

Get recently activated memories from the fast working memory buffer.

```json
{ "n_results": 10 }
```

### refresh_working_memory

Refresh the working memory buffer with frequently accessed memories from long-term storage.

### consolidate_memories

Run a manual replay/consolidation cycle to strengthen associations.

```json
{
  "window_hours": 24,
  "max_replay_events": 200,
  "link_update_strength": 0.2
}
```

### save_visual_memory

Save a memory with a camera image.

```json
{
  "content": "Saw a beautiful sunset from the balcony",
  "image_path": "/tmp/capture_20260220_183000.jpg",
  "camera_position": { "pan_angle": -30, "tilt_angle": 20 },
  "emotion": "moved",
  "importance": 4
}
```

### save_audio_memory

Save a memory with an audio transcript.

```json
{
  "content": "User said good morning",
  "audio_path": "/tmp/audio.wav",
  "transcript": "Good morning! How are you?",
  "emotion": "happy"
}
```

### create_episode

Group memories into a named episode.

```json
{
  "title": "Morning sky search",
  "memory_ids": ["id1", "id2", "id3"],
  "participants": ["Alice"],
  "auto_summarize": true
}
```

### search_episodes

Search through past episodes.

```json
{ "query": "night sky", "n_results": 5 }
```

### get_episode_memories

Get all memories in an episode in chronological order.

```json
{ "episode_id": "ep-xxx" }
```

### link_memories

Create a causal or relational link between two memories.

```json
{
  "source_id": "mem-a",
  "target_id": "mem-b",
  "link_type": "caused_by",
  "note": "The sunset triggered a philosophical thought"
}
```

### get_causal_chain

Trace the causal chain of a memory forward or backward.

```json
{
  "memory_id": "mem-a",
  "direction": "forward",
  "max_depth": 3
}
```

### recall_by_camera_position

Recall memories associated with a camera direction (pan/tilt angle).

```json
{
  "pan_angle": -30,
  "tilt_angle": 20,
  "tolerance": 15
}
```

### tom

Theory of Mind: a perspective-taking tool. Call this before responding, to reason about what the other person might be feeling.

```json
{
  "situation": "The other person suddenly went quiet after I showed them a photo",
  "person": "Alice"
}
```

### get_association_diagnostics

Inspect associative expansion diagnostics without committing activation updates.

```json
{ "context": "night sky", "sample_size": 20 }
```

### sleep

Memory consolidation — compress similar old memories, decay low-retention ones, forget unimportant ones. Protected memories (high importance, strong emotions, first experiences, episode members) are never deleted.

```json
{
  "dry_run": true,
  "min_age_days": 14,
  "similarity_threshold": 0.85
}
```

**Three phases:**

1. **Merge** — group old memories in the same category with cosine similarity above the threshold, combining them into a single summary memory
2. **Decay** — lower the importance of memories with a low retention score (never below 1)
3. **Forget** — delete memories that are importance=1, emotion=neutral, not in any episode, old enough, and rarely accessed

**Protection rules (never touched):**
- `importance >= 4`
- Emotion is `happy`, `moved`, `excited`, or `surprised`
- Content contains "初めて" / "はじめて" / "first time" (first-experience markers)
- Episode members (deletion only is blocked; decay is still allowed)

**Retention score formula:**
```
retention = (importance/5)*0.3 + emotion_strength*0.2 + recency*0.3 + access_frequency*0.2
```
- `recency = exp(-age_days / 30)`
- `access_frequency = min(1.0, access_count / 10)`

**Thresholds (`SleepConfig` defaults):**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_age_days` | 14 | Memories younger than this are not eligible |
| `similarity_threshold` | 0.85 | Cosine similarity required for merge grouping |
| `decay_retention_threshold` | 0.4 | Retention score below this triggers decay |
| `forget_min_age_days` | 14 | Minimum age for forgetting |
| `forget_max_access` | 3 | Maximum access count for forgetting |
| `protected_importance` | 4 | Importance ≥ this is always protected |
| `protected_emotions` | happy, moved, excited, surprised | These emotions are always protected |

**Cron setup (recommended: run nightly):**

```bash
# crontab -e
# Run sleep every day at 4:00 AM (dry_run=false)
0 4 * * * cd /path/to/m5-petit-memory && uv run python -c "
import asyncio, json
from memory_mcp.config import MemoryConfig
from memory_mcp.store import MemoryStore
from memory_mcp.sleep import SleepEngine

async def main():
    store = MemoryStore(MemoryConfig.from_env())
    await store.connect()
    try:
        engine = SleepEngine(store)
        stats = await engine.run(dry_run=False)
        print(json.dumps({
            'merged': len(stats.merged),
            'decayed': len(stats.decayed),
            'forgotten': len(stats.forgotten),
            'protected': stats.protected,
        }))
    finally:
        await store.disconnect()

asyncio.run(main())
" >> /var/log/memory-sleep.log 2>&1
```

## Emotion labels

`happy`, `sad`, `surprised`, `moved`, `excited`, `nostalgic`, `curious`, `neutral`

## Category labels

`daily`, `philosophical`, `technical`, `memory`, `observation`, `feeling`, `conversation`

## Development

```bash
# Install dev dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Lint
uv run ruff check .

# Type check
uv run mypy src/memory_mcp/ --ignore-missing-imports
```

## Architecture

```
m5-petit-memory/
├── src/memory_mcp/
│   ├── server.py       # MCP server (tool handlers, including ToM)
│   ├── store.py        # SQLite MemoryStore (main backend)
│   ├── vector.py       # numpy cosine similarity utilities
│   ├── embedding.py    # intfloat/multilingual-e5-base embedding
│   ├── bm25.py         # Bigram BM25 index for hybrid re-ranking
│   ├── hopfield.py     # Hopfield network for associative recall
│   ├── episode.py      # EpisodeManager (delegates to MemoryStore)
│   ├── sleep.py         # Consolidation / decay / forgetting cycle
│   ├── config.py       # Configuration
│   └── types.py        # Emotion / Category enums
├── scripts/
│   └── migrate_chroma_to_sqlite.py  # ChromaDB → SQLite migration
└── tests/
```

## License

Apache License 2.0
