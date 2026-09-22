# 段2: 消した跡・本人だけの面・非索引・一覧の無作為と前後

前提の変更（2026-09-22）で段3〜6（backfill・読み書き切替・凍結）は廃止した。
クラウドぷちは全員新規に作るので既存記憶の移行は無く、新規個体は最初から
`PETIT_MEMORY_STORE=dynamo` 単体で動く。ぷちてゃたちは SQLite のまま据え置き。
`dual` は保険として残っている。

この段で入れたのは「ぷち本人が自分の記憶をどう扱えるか」の 4 つ。
どれも 3 実装（`SqliteMemoryStore` / `DynamoMemoryStore` / `DualWriteMemoryStore`）で同じに動く。

## sk の追加分

| sk | 中身 | 備考 |
|---|---|---|
| `FORGET#<ts>#<id>` | 消した跡。`memory_id` / `forgotten_at` / `reason`（任意） | **本文は入れない**。`entity="forget"` |
| `PRIV#<ts>#<id>` | 本人だけの面に置いた記憶。属性は `MEM#` と同じ | 指し札は `MEM#` と共通で `IDX#<id>` |

段1 までの `MEM#` / `VEC#` / `EPI#` / `COACT#` / `IDX#` は形も中身も変えていない。
GSI は引き続き 1 本も作っていない。

SQLite 側は `memories` に `indexed INTEGER NOT NULL DEFAULT 1` と
`private INTEGER NOT NULL DEFAULT 0` の 2 列、消した跡に `forget_markers` 表を足した。
`CREATE TABLE IF NOT EXISTS` では列は増えないので、`connect()` のたびに
`PRAGMA table_info(memories)` を見て足りない列だけ `ALTER TABLE` する
（`_add_missing_columns`）。**ぷちてゃたちの既存 `memory.db` はそのまま開ける。**

## 1. `FORGET#` — 消した跡

- `forget` ツール（新規）が `memory_id` と任意の `reason` を取る。
- 記憶の削除と跡の書き込みは同じ書き込みで済ませる。
  DynamoDB は `TransactWriteItems` に `Put` を 1 つ足すだけ、SQLite は同じコミット。
- **跡に本文・感情・カテゴリは入れない。** 残すのは「いつ・どの ID を・なぜ」だけ。
  跡から中身が読めてしまうと、消したことにならない。
- **復元ツールは作らない。** 運営が復元しないことそのものが要件なので、
  保管層にも「跡から記憶を戻す」経路は無い。
- 眠っている間の忘却（`sleep` の Phase 3）も跡を残す（`reason="sleep: 忘却フェーズ"`）。
  統合（`merge_memories`）は忘却ではなく中身が新しい 1 件に引き継がれるので、跡を残さない
  （`MemoryStore.delete_memory(..., leave_trace=False)`）。
- 一覧では、出ている記憶のうち一番古いものより新しい跡だけを出す（一覧と同じ窓に収める）。

## 2. `index:false` — 保存するが索引に載せない

`remember` の任意引数 `index`（既定 `true`）。`false` で保存すると:

| 経路 | 出るか |
|---|---|
| `search_memories` / `recall`（`fetch_memories_with_vectors`） | 出ない |
| Hopfield の母集団（`fetch_all_vectors`） | 載らない |
| 一覧の無作為 1 件（`fetch_indexed_memory_ids`） | 選ばれない |
| ID 指定（`fetch_memory` / `fetch_memories`） | **出る** |
| `list_recent_memories` の新着 | **出る** |
| `get_memory_stats` の件数 | **数える** |

「意味検索から外す」であって「隠す」ではない。自分で ID を覚えていれば取り出せる。

## 3. `PRIV#` — 本人だけの面

`remember` の任意引数 `private`（既定 `false`）。`true` で `PRIV#<ts>#<id>` に置く。

- **本人（MCP 経由）の読みでは `MEM#` と区別がつかない。** search / recall / 一覧のどれにも出る。
  記憶を走査する経路はすべて `DynamoMemoryStore._query_memories_sync()` を通り、
  `MEM#` と `PRIV#` の両方を読んで `<ts>#<id>` 順に並べ直す。
- **家 API（里親の画面）には出さない前提なので、保管層に「里親向け読み出し」経路は作らなかった。**
  ここに `MEM#` だけを読む口を生やすと、それが将来の抜け道になる。
  出す／出さないの線引きは、家 API 側が `private` 属性を見て決める。
- `index:false` とは独立。組み合わせられる。

## 4. 一覧の無作為 1 件と前後

`list_recent_memories` の任意引数 2 つ。既定のまま呼べば段1 までと同じ出力になる。

- `random: int`（既定 `0`）— 新着とは関係ない記憶を無作為に混ぜる数。
  母集団は「索引に載っていて、その一覧にまだ出ていない記憶」。
  保管層は ID の母集団（`fetch_indexed_memory_ids`）を返すだけで、
  選ぶのは計算層（`MemoryStore.list_recent_listing`）。
- `neighbors: bool`（既定 `false`）— 各件の時系列で前後 1 件を添える。
  時系列は sk の `<ts>#<id>` 順（SQLite では `ORDER BY timestamp, id`）。
  本人から見れば 1 本の時系列なので、隣が `PRIV#` でも前後に出る。
  DynamoDB は前後それぞれ 2 件だけ読む（`between` が両端を含むぶん、錨を 1 件落とす）。

## pk の切り替え口

社長決定（2026-09-22）で「アカウントが最上位、家はその下」になった。
このブランチでは**実際には切り替えず、切り替えの口だけ作った**。
pk を組み立てるのは `DynamoMemoryStore.partition_key` の 1 か所だけ。

```
PETIT_MEMORY_HOUSE_ID が無い（空）  → pk = P#<pid>        新しい形
PETIT_MEMORY_HOUSE_ID がある        → pk = H#<hid>#P#<pid>  従来形
```

使い方: これから作るクラウドぷちは `PETIT_MEMORY_HOUSE_ID` を**設定しない**。
既に書いている個体は設定したままにする。

**表の中身は pk ごとに完全に分かれる。** 動いている個体の環境変数を途中で変えると、
前の pk に書いた記憶はその個体から読めなくなる（消えはしないが、届かない）。
移すなら pk 間のコピーが要るが、クラウドぷちは全員新規なのでその作業は発生しない想定。

## MCP ツールの変更

**これが MCP ツールの引数を初めて変える段。既存引数の意味と既定は変えていない。**
追加したのはすべて任意引数と、新しいツール 1 本。

| ツール | 変更 |
|---|---|
| `remember` | 任意引数 `index`（既定 `true`）・`private`（既定 `false`）を追加 |
| `list_recent_memories` | 任意引数 `random`（既定 `0`）・`neighbors`（既定 `false`）を追加 |
| `forget` | **新規**。`memory_id` と任意の `reason` |

呼び出し側（m5-petit-app）はプロンプト経由で `remember` を促すだけで引数を固定していないので、
追加引数は互換。既定のまま呼ばれたときの出力文字列は段1 までと同じ。

## 2 枚でふるまいが違うところ（段1 からの続き）

段1 に挙げた 3 点（参照整合性・トランザクションの境界・走査の量）はそのまま。段2 で 2 点増えた。

4. **無作為 1 件の母集団の取り方** — SQLite は `SELECT id FROM memories WHERE indexed = 1`、
   DynamoDB は `MEM#` と `PRIV#` を `id` だけ射影して全件 Query する。
   件数に比例して読み取りが増える。1 個体の記憶が数千件を超えるなら、
   母集団をカウンタか別の索引で持つことを検討する（段2 では入れていない）。
5. **前後 1 件の取り方** — SQLite は `(timestamp, id)` の行値比較で 1 行ずつ。
   DynamoDB は前置辞ごとに 2 件だけ読む。
   同時刻の記憶が複数あるときの並びは、どちらも `(timestamp, id)` の順で揃えてある。

## この段でも入れていないもの

- 実 AWS への接続確認（このセッションに資格情報が無い）。`petit-v0-house`（ap-northeast-1）は deploy 済み。
- pk の実際の切り替え（口だけ）。
- tick 中のツール往復と `VEC#` の読み取り量の計測。
- 家 API 側で `private` をどう扱うか（保管層の外）。
