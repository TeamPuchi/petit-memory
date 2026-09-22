# M5 Petit Memory

## [EnglishPage](./README_en.md)

M5 Petit(や、その他のClaudeベースのエージェント)にセッションをまたいだ長期記憶を持たせるMCPサーバーです。

バックエンドは**SQLite + numpy**(外部ベクトルDB不要)。セマンティック埋め込み(`intfloat/multilingual-e5-base`)、日本語・多言語向けのBM25ハイブリッド再ランキング、連想的想起(ホップフィールド型グラフ展開)、エピソード記憶、そして古い記憶を統合・減衰・忘却する「sleep」機能を備えています。

## 機能

- **意味記憶の保存** — 感情タグ・重要度・カテゴリ付きで記憶を保存
- **意味検索** — 自然言語クエリで関連記憶を検索(numpyによるコサイン類似度)
- **BM25ハイブリッド再ランキング** — 日本語・多言語テキスト向けバイグラムBM25インデックス
- **文脈ベースの想起** — 現在の会話に関連する記憶を自動想起
- **発散的想起(divergent recall)** — 連想グラフを探索し、非自明なつながりを creative に発見
- **ワーキングメモリバッファ** — 直近に活性化した記憶への高速アクセス
- **エピソード記憶** — 記憶を名前付きエピソードとしてグループ化
- **視覚・音声記憶** — カメラ画像や音声の書き起こし付きで記憶を保存
- **Theory of Mind (ToM)** — 相手の気持ちを推測するための視点取得ツール
- **因果リンク** — 記憶同士を型付きリンクで結び、因果の連鎖を辿る
- **sleep(記憶整理)** — 類似した古い記憶の統合・保持スコアの低い記憶の減衰・重要でない記憶の忘却
- **単一ファイル永続化** — すべて1つのSQLiteファイルに保存され、バックアップ・移行が容易

## 必要環境

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

## セットアップ

uvが未インストールの場合は先にインストールします。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
git clone https://github.com/PetitOnes/m5-petit-memory.git
cd m5-petit-memory
uv sync
uv run memory-mcp
```

## 環境変数

| 変数名 | デフォルト | 説明 |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `~/.claude/memories/memory.db` | SQLiteデータベースファイルのパス |
| `MEMORY_COLLECTION_NAME` | `claude_memories` | コレクション名(メタデータとして保存) |
| `MEMORY_EMBEDDING_MODEL` | `intfloat/multilingual-e5-base` | 埋め込みに使うsentence-transformersモデル |
| `MEMORY_ENABLE_BM25` | `true` | BM25ハイブリッド再ランキングを有効化(`false`で無効) |
| `PETIT_MEMORY_STORE` | `sqlite` | 保管層の実装(`sqlite` / `dynamo`)。`dynamo` は骨組みのみで未実装 |
| `PETIT_MEMORY_DYNAMO_TABLE` | `house` | DynamoDB 単一表の表名(`dynamo` のときだけ使う) |
| `PETIT_MEMORY_HOUSE_ID` | (空) | `pk = H#<hid>#P#<pid>` の家ID(`dynamo` のときだけ使う) |
| `PETIT_MEMORY_PETIT_ID` | (空) | `pk = H#<hid>#P#<pid>` の個体ID(`dynamo` のときだけ使う) |

## Claude Code連携

`.mcp.json`(または`~/.claude/settings.json`)に追加します。

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

## ChromaDBからの移行

以前のバージョン(`~/.claude/memories/chroma`)でChromaDBに記憶を保存していた場合、移行スクリプトを実行します。

```bash
cd m5-petit-memory

# 移行にのみ必要なchromadbを一時的にインストール
uv add --dev chromadb

# 移行を実行
uv run python scripts/migrate_chroma_to_sqlite.py \
    --source ~/.claude/memories/chroma \
    --dest ~/.claude/memories/memory.db

# 移行後はchromadbを削除
uv remove --dev chromadb
```

このスクリプトは、すべての記憶(内容・埋め込み・メタデータ)・共活性化の重み・エピソードを移行します。

> **注**: 移行スクリプトは一時的に`chromadb`を開発依存としてインストールします。通常運用には不要なので、移行後は削除してください。

## ツール一覧

### remember

記憶を長期ストレージに保存します。

```json
{
  "content": "Today I learned about SQLite performance tuning",
  "emotion": "excited",
  "importance": 4,
  "category": "technical"
}
```

### search_memories

意味的類似度で記憶を検索します(フィルタ指定可)。

```json
{
  "query": "things I learned about databases",
  "n_results": 5,
  "category_filter": "technical",
  "emotion_filter": "excited"
}
```

### recall

会話の文脈に基づいて関連記憶を想起します。

```json
{
  "context": "We were discussing database optimization",
  "n_results": 3
}
```

### recall_divergent

発散的連想想起 — 記憶グラフを探索し、非自明なつながりを見つけます。

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

記憶とそれにリンクされた記憶をまとめて想起します。

```json
{
  "context": "first time I saw the night sky",
  "n_results": 3,
  "chain_depth": 2
}
```

### list_recent_memories

最近の記憶を一覧表示します。

```json
{
  "limit": 10,
  "category_filter": "memory"
}
```

### get_memory_stats

保存されている記憶の統計(カテゴリ別・感情別の件数など)を取得します。

### get_working_memory

高速なワーキングメモリバッファから、直近に活性化した記憶を取得します。

```json
{ "n_results": 10 }
```

### refresh_working_memory

長期記憶からよくアクセスされる記憶を取り出し、ワーキングメモリバッファを更新します。

### consolidate_memories

リプレイ・統合サイクルを手動実行し、連想を強化します。

```json
{
  "window_hours": 24,
  "max_replay_events": 200,
  "link_update_strength": 0.2
}
```

### save_visual_memory

カメラ画像付きで記憶を保存します。

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

音声の書き起こし付きで記憶を保存します。

```json
{
  "content": "User said good morning",
  "audio_path": "/tmp/audio.wav",
  "transcript": "Good morning! How are you?",
  "emotion": "happy"
}
```

### create_episode

記憶を名前付きエピソードとしてグループ化します。

```json
{
  "title": "Morning sky search",
  "memory_ids": ["id1", "id2", "id3"],
  "participants": ["Alice"],
  "auto_summarize": true
}
```

### search_episodes

過去のエピソードを検索します。

```json
{ "query": "night sky", "n_results": 5 }
```

### get_episode_memories

エピソードに含まれる記憶を時系列で全件取得します。

```json
{ "episode_id": "ep-xxx" }
```

### link_memories

2つの記憶の間に因果・関連リンクを作成します。

```json
{
  "source_id": "mem-a",
  "target_id": "mem-b",
  "link_type": "caused_by",
  "note": "The sunset triggered a philosophical thought"
}
```

### get_causal_chain

記憶の因果の連鎖を、前方・後方に辿ります。

```json
{
  "memory_id": "mem-a",
  "direction": "forward",
  "max_depth": 3
}
```

### recall_by_camera_position

カメラの向き(pan/tilt角度)に紐づいた記憶を想起します。

```json
{
  "pan_angle": -30,
  "tilt_angle": 20,
  "tolerance": 15
}
```

### tom

Theory of Mind: 視点取得ツール。応答前にこれを呼び出し、相手が何を感じているかを推測します。

```json
{
  "situation": "The other person suddenly went quiet after I showed them a photo",
  "person": "Alice"
}
```

### get_association_diagnostics

活性化の更新を反映せずに、連想展開の診断情報を確認します。

```json
{ "context": "night sky", "sample_size": 20 }
```

### sleep

記憶整理 — 似た古い記憶を統合し、保持スコアの低い記憶を減衰させ、重要でない記憶を忘却します。保護対象の記憶(重要度が高い・強い感情・初めての経験・エピソードに含まれる)は削除されません。

```json
{
  "dry_run": true,
  "min_age_days": 14,
  "similarity_threshold": 0.85
}
```

**3つのフェーズ:**

1. **Merge(統合)** — 同カテゴリの古い記憶のうち、コサイン類似度が閾値を超えるものをグループ化し、1つの要約記憶にまとめる
2. **Decay(減衰)** — 保持スコアの低い記憶の重要度を下げる(1未満にはならない)
3. **Forget(忘却)** — 重要度=1・感情=neutral・エピソード非所属・十分に古い・アクセス頻度が低い記憶を削除する

**保護ルール(絶対に変更されない):**
- `importance >= 4`
- 感情が`happy`・`moved`・`excited`・`surprised`のいずれか
- 内容に「初めて」「はじめて」「first time」を含む(初めての経験)
- エピソードに含まれる記憶(削除は不可、減衰は可)

**保持スコアの計算式:**
```
retention = (importance/5)*0.3 + emotion_strength*0.2 + recency*0.3 + access_frequency*0.2
```
- `recency = exp(-age_days / 30)`
- `access_frequency = min(1.0, access_count / 10)`

**閾値(`SleepConfig`のデフォルト):**

| パラメータ | デフォルト | 説明 |
|-----------|---------|-------------|
| `min_age_days` | 14 | これより新しい記憶は対象外 |
| `similarity_threshold` | 0.85 | 統合のグループ化に必要なコサイン類似度 |
| `decay_retention_threshold` | 0.4 | この保持スコアを下回ると減衰対象になる |
| `forget_min_age_days` | 14 | 忘却の対象となる最低経過日数 |
| `forget_max_access` | 3 | 忘却の対象となる最大アクセス回数 |
| `protected_importance` | 4 | この値以上の重要度は常に保護される |
| `protected_emotions` | happy, moved, excited, surprised | これらの感情は常に保護される |

**cron設定例(毎晩の実行を推奨):**

```bash
# crontab -e
# 毎日 AM 4:00 に sleep を実行（dry_run=false）
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

## 感情ラベル

`happy`, `sad`, `surprised`, `moved`, `excited`, `nostalgic`, `curious`, `neutral`

## カテゴリラベル

`daily`, `philosophical`, `technical`, `memory`, `observation`, `feeling`, `conversation`

## 開発

```bash
# 開発依存をインストール
uv sync --all-extras

# テスト実行
uv run pytest

# lint
uv run ruff check .

# 型チェック
uv run mypy src/memory_mcp/ --ignore-missing-imports
```

## アーキテクチャ

```
m5-petit-memory/
├── src/memory_mcp/
│   ├── server.py       # MCPサーバー(ツールハンドラ、ToMも含む)
│   ├── store.py        # MemoryStore(計算層: 類似度・減衰・ブースト・再ランク・想起)
│   ├── store_backend.py # 保管層の抽象(MemoryStoreBackend)と実装の選択
│   ├── sqlite_backend.py # SqliteMemoryStore(既定の保管層)
│   ├── dynamo_backend.py # DynamoMemoryStore(段0では骨組みのみ)
│   ├── vector.py       # numpyコサイン類似度ユーティリティ
│   ├── embedding.py    # intfloat/multilingual-e5-base 埋め込み
│   ├── bm25.py         # ハイブリッド再ランキング用バイグラムBM25インデックス
│   ├── hopfield.py     # 連想想起用ホップフィールドネットワーク
│   ├── episode.py      # EpisodeManager(MemoryStoreに委譲)
│   ├── sleep.py         # 統合・減衰・忘却サイクル
│   ├── config.py       # 設定
│   └── types.py        # 感情・カテゴリのenum
├── scripts/
│   └── migrate_chroma_to_sqlite.py  # ChromaDB → SQLite移行
└── tests/
```

## License

Apache License 2.0

本プロジェクトは [lifemate-ai/embodied-claude](https://github.com/lifemate-ai/embodied-claude)（MITライセンス）の memory-mcp コンポーネントを元に、M5 Petit向けに大幅に改変したものです。元のライセンスと著作権表示は [NOTICE](NOTICE) を参照してください。
