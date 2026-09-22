# 段0: 保管と計算の境目 — 判定表

fork 元（PetitOnes/m5-petit-memory）からの最終コミットは `1b8bf2e`（2026-07-06）で、TeamPuchi/petit-memory の `main` はこの 1 コミットのみを持ち、fork 後に TeamPuchi 側で追加したコミットは無い（PetitOnes 側 `main` が 2026-07-06 以降に進んでいないかは、当セッションのリポジトリスコープが `TeamPuchi/petit-memory` だけのため**未確認**）。

## この文書の役割

ぷちの長期記憶を、家コンテナ内の SQLite から DynamoDB の単一表 `house`
（`pk = H#<hid>#P#<pid>` / `sk = MEM#<ts>#<id>`・`VEC#<id>`・`EPI#…`・`COACT#…`）へ移す段0として、
`store.py` と `server.py` を 1 行ずつ「**保管**（行の出し入れ）」と「**計算**（類似度・減衰・ブースト・再ランク・探索）」に振り分けた結果を残す。
段0 では MCP ツールの名前・引数・戻り値・振る舞いは一切変えていない。

行番号は fork base `1b8bf2e` 時点の各ファイルのもの。

## 判定の基準

- **保管** = その処理を DynamoDB に載せ替えるとき、SQL/テーブル設計ごと書き換わる処理。行・属性・キーの知識を持つ。
- **計算** = 行がどこから来ても同じ結果になる処理。numpy・埋め込み・スコア式・グラフ探索。
- **混在** = 1 つの関数の中に両方があるもの。段0 では関数を割って、保管側だけを実装クラスへ降ろした。

## store.py（1504 行）

| 行 | 関数 / 定義 | 判定 | 理由 |
|---|---|---|---|
| 54–108 | `_DDL` | 保管 | テーブル・索引定義そのもの。Dynamo では単一表の pk/sk 設計に置き換わる |
| 114–123 | `EMOTION_BOOST_MAP` | 計算 | 感情→ブースト値の定数表。保管先に依存しない |
| 126–142 | `calculate_time_decay` | 計算 | 半減期 30 日の指数減衰。入力は timestamp 文字列だけ |
| 145–146 | `calculate_emotion_boost` | 計算 | 定数表の参照 |
| 149–151 | `calculate_importance_boost` | 計算 | 1–5 の clamp と線形変換 |
| 154–167 | `calculate_final_score` | 計算 | 距離・減衰・ブーストの重み付き合成 |
| 175–214 | `_parse_linked_ids` / `_parse_sensory_data` / `_parse_camera_position` / `_parse_tags` / `_parse_links` | 保管 | SQLite の TEXT 表現（カンマ連結・JSON 文字列）を解くデコーダ。Dynamo では属性型が変わるので実装ごとに持つ |
| 217–242 | `_row_to_memory` | 保管 | `sqlite3.Row` → `Memory`。行の形を知っている |
| 245–260 | `_row_to_episode` | 保管 | 同上（episodes 表） |
| 271–280 | `MemoryStore.__init__` | 混在 | 接続設定（保管）と WorkingMemory/Association/Consolidation/Hopfield/Embedding/BM25（計算）の同居 |
| 284–302 | `connect` | 保管 | `sqlite3.connect` と DDL 実行。PRAGMA も SQLite 固有 |
| 304–309 | `disconnect` | 保管 | 接続の後始末 |
| 311–314 | `_ensure_connected` | 保管 | 生の `sqlite3.Connection` を返す。**SQLite 固有の漏れ**（後述） |
| 318–322 | `_encode_document` / `_encode_query` | 計算 | e5 モデルでの埋め込み生成 |
| 326–331 | `_get_coactivation` | 保管 | `coactivation` 表の SELECT |
| 335–340 | `_fetch_memory_by_id` | 保管 | `memories` の 1 行取得 + coactivation の結合 |
| 342–353 | `_fetch_memories_by_ids_sync` | 保管 | `IN (...)` の複数行取得 |
| 357–423 | `save` | 混在 | 370–372 の id/timestamp/clamp と 387–391 の正規化・読み・埋め込みは計算、393–420 の INSERT 2 本は保管、421–422 の BM25 dirty と working memory は計算 |
| 427–487 | `_vector_search` | 混在 | 438–439 クエリ正規化と埋め込み＝計算、442–465 の WHERE 組み立てと行＋ベクトル取得＝保管、470–487 の cosine・並べ替え・距離変換＝計算 |
| 491–509 | `search` | 計算 | `_vector_search` の薄いラッパ |
| 513–597 | `search_with_scoring` | 計算 | 減衰・感情・重要度の合成と BM25 / 読み一致の再ランク。567 の `get_all()` だけが保管呼び出し |
| 601–627 | `recall` | 計算 | スコア済み結果と Hopfield スコアの混合 |
| 631–650 | `list_recent` | 保管 | `ORDER BY timestamp DESC LIMIT`。Dynamo では `sk` 前置辞 `MEM#` の降順 Query |
| 654–680 | `get_stats` | 混在 | 658–664 の SELECT / MIN / MAX ＝保管、666–680 のカテゴリ・感情の数え上げ＝計算 |
| 684–700 | `get_by_id` / `get_by_ids` | 保管 | 取得のみ |
| 704–716 | `get_all` | 保管 | 全件 SELECT |
| 720–769 | `delete_memory` | 保管 | 存在確認・`LIKE` での逆参照探索・`linked_ids`/`links` の書き戻し・DELETE。CASCADE 前提も含めて保管側の事情。768 の BM25 dirty のみ計算 |
| 773–832 | `merge_memories` | 計算 | リンクの収集・重複排除の手順。実際の出し入れは `save`/`update_memory_fields`/`delete_memory` に委譲 |
| 836–849 | `update_access` | 保管 | `access_count = access_count + 1` の原子更新。Dynamo では `ADD` |
| 853–866 | `update_episode_id` | 保管 | 1 属性の更新と rowcount 判定 |
| 870–895 | `update_memory_fields` | 保管 | 列ホワイトリストと `SET` 句の組み立て。列名は schema の契約 |
| 899–913 | `record_activation` | 混在 | 回数 +1 と clamp＝計算、書き込みは `update_memory_fields` へ委譲 |
| 917–951 | `bump_coactivation` | 混在 | 932/941 の clamp と左右対称化＝計算、936–947 の SELECT + UPSERT＝保管 |
| 955–976 | `maybe_add_related_link` | 混在 | 閾値 0.6 の判定＝計算、weight 読み出し＝保管 |
| 980–1043 | `save_with_auto_link` | 混在 | `save` と同じ切り分け。989–991 の類似検索と閾値＝計算 |
| 1047–1061 | `_add_bidirectional_link` | 保管 | 双方向の `linked_ids` 追記 |
| 1065–1089 | `get_linked_memories` | 計算 | 幅優先の探索手順。取得は `get_by_id` 経由 |
| 1093–1109 | `recall_with_chain` | 計算 | 想起結果と連想の合成 |
| 1113–1139 | `add_causal_link` | 混在 | 重複判定と JSON 組み立て＝計算、書き込みは `update_memory_fields` |
| 1143–1180 | `get_causal_chain` | 計算 | リンク種別を辿る探索 |
| 1184–1213 | `search_important_memories` | 保管 | 条件付き SELECT + ORDER BY + LIMIT |
| 1217–1218 | `get_working_memory` | 計算 | インメモリのバッファを返すだけ |
| 1222–1292 | `save_episode` / `get_episode_by_id` / `search_episodes` / `list_all_episodes` / `delete_episode` | 保管 | episodes 表の CRUD。`search_episodes` の `LIKE` も保管側の都合 |
| 1296–1395 | `recall_divergent` | 計算 | 連想拡散・ワークスペース選択・発散度。保管は `get_by_ids` と活性の書き戻しのみ |
| 1397–1407 | `get_association_diagnostics` | 計算 | `recall_divergent` の呼び直し |
| 1409–1421 | `consolidate_memories` | 計算 | ConsolidationEngine への委譲 |
| 1425–1445 | `hopfield_load` | 混在 | 1429–1436 の embeddings + normalized_content の結合取得＝保管、1441–1445 の Hopfield への格納＝計算 |
| 1447–1471 | `hopfield_recall` | 計算 | 正規化・埋め込み・retrieve |
| 1475–1504 | `_build_divergent_diagnostics` | 計算 | 集計のみ |

## server.py（1392 行）

| 行 | 箇所 | 判定 | 理由 |
|---|---|---|---|
| 37–635 | `list_tools()` のツール定義 | どちらでもない | MCP の入出力契約。段0 では 1 文字も変えていない |
| 637–1337 | `call_tool()` の各 `case` | 計算 | 引数の取り出しと、`MemoryStore` の戻り値をテキストに整形する処理。SQL は 1 つも無い |
| 1343–1348 | `connect_memory()` | 保管 | `MemoryConfig.from_env()` から `MemoryStore` を作って `connect()` する唯一の場所。**実装の選択はここに集約する** |
| 1359–1362 | `disconnect_memory()` | 保管 | 後始末 |

## sleep.py（保管が漏れていた箇所）

| 行 | 箇所 | 判定 | 対応 |
|---|---|---|---|
| 193–207 | `SleepEngine._phase_merge` が `self._store._ensure_connected()` から生の接続を取り、`embeddings` 表を直接 SELECT している | 保管 | 段0 で `MemoryStore.get_vectors(ids)`（保管層の `fetch_vectors`）に置き換えた。cosine による貪欲グルーピング（216–236）は計算のまま `sleep.py` に残す |
| 28–59 / 62–70 / 160–176 | `calculate_retention_score` / `_is_protected` / `_should_forget` | 計算 | 保持スコアと保護判定。保管先に依存しない |

## 段0 で切った形

```
MemoryStore（store.py・名前も公開メソッドも据え置き）
  └─ 計算: 埋め込み・cosine・減衰・感情/重要度ブースト・BM25 再ランク・Hopfield・連想拡散・探索
  └─ self._backend: MemoryStoreBackend  ← 保管はすべてここ越し
         ├─ SqliteMemoryStore（sqlite_backend.py）既存 SQL をそのまま移設
         └─ DynamoMemoryStore（dynamo_backend.py）署名のみ・NotImplementedError
```

- 抽象は `store_backend.py` の `MemoryStoreBackend`（`typing.Protocol`, `runtime_checkable`）。
  メソッドは「行を取る・入れる・消す・一覧する」だけで、スコアも距離も返さない。
- 選択は環境変数 1 つ: `PETIT_MEMORY_STORE=sqlite|dynamo`（既定 `sqlite`）。
  解決は `MemoryConfig.from_env()` → `create_backend(config)`。不正な値は `ValueError`。
- **`MemoryStore` という名前は既存の呼び出し側（`server.py`・`episode.py`・`sensory.py`・`conftest.py`）が使っているため、
  抽象側の名前を `MemoryStoreBackend` にした。**
  指示は「`MemoryStore` の抽象を切る」だったが、
  公開クラス名を Protocol に差し替えると `MemoryStore(config)` を呼んでいる既存テストが全滅するため、
  「抽象 = `MemoryStoreBackend`、実装 = `SqliteMemoryStore` / `DynamoMemoryStore`」とした。実装側の名前は指示どおり。

### 抽象のメソッド一覧（すべて保管）

| メソッド | 対応する既存 SQL | Dynamo での想定 |
|---|---|---|
| `connect` / `disconnect` | 接続と DDL | クライアント生成のみ（表は IaC 側で作る） |
| `fetch_memory` / `fetch_memories` / `fetch_all_memories` | `SELECT * FROM memories …` | `MEM#` の GetItem / BatchGetItem / Query |
| `fetch_memories_with_vectors` | `memories JOIN embeddings` ＋ 絞り込み | `MEM#` Query ＋ `VEC#` の引き当て |
| `fetch_recent_memories` | `ORDER BY timestamp DESC LIMIT` | `sk` 降順 Query（`MEM#<ts>#<id>` の時刻順を利用） |
| `fetch_important_memories` | `importance >= ? AND access_count >= ?` | Query ＋ FilterExpression |
| `fetch_memory_facets` | `SELECT emotion, category, timestamp` ＋ MIN/MAX | Query の射影（段1 以降で集計属性を検討） |
| `insert_memory` | `INSERT INTO memories` ＋ `INSERT INTO embeddings` | `MEM#` と `VEC#` の TransactWrite |
| `update_memory_fields` | `UPDATE memories SET …` | UpdateItem |
| `update_episode_id` | `UPDATE memories SET episode_id` | UpdateItem |
| `increment_access` | `access_count = access_count + 1` | `ADD access_count :one` |
| `delete_memory` | DELETE ＋ 逆参照の書き戻し | `MEM#`/`VEC#`/`COACT#` の削除と逆参照更新 |
| `add_bidirectional_link` | 双方の `linked_ids` 追記 | 2 件の UpdateItem |
| `fetch_vectors` / `fetch_all_vectors` | `SELECT … FROM embeddings` | `VEC#` の Query |
| `fetch_coactivation` / `fetch_coactivation_weight` / `put_coactivation` | `coactivation` 表 | `COACT#<target>` |
| `insert_episode` / `fetch_episode` / `search_episodes` / `fetch_all_episodes` / `delete_episode` | episodes 表 | `EPI#<id>` |

## 割れなかった / 割らなかったところ

1. **`_ensure_connected()` が生の `sqlite3.Connection` を返す。**
   `tests/test_sleep.py:127` と `:288` が、保存済み記憶の `timestamp` を古くするために直接 SQL を投げている。
   ここを抽象に載せると「任意の SQL を実行する」メソッドを抽象に生やすことになり、Dynamo 実装が書けない。
   段0 では `MemoryStore._ensure_connected()` を **SQLite 実装専用の逃げ道**として残し、
   `SqliteMemoryStore` 以外が入っているときは `RuntimeError` にした。
   `sleep.py` の本番経路は `get_vectors()` に付け替え済みなので、残った利用者はテストだけ。
   段1 以降でテスト側にヘルパ（保存時刻を差し込める `save` か、テスト用の `set_timestamp`）を用意して消す。

2. **`delete_memory` の逆参照掃除を分割しなかった。**
   `linked_ids LIKE '%id%'` と `links LIKE '%id%'` で逆参照を探す部分は、
   「どの行が自分を指しているか」を保管側しか知らないため、探索と書き戻しを 1 つの保管メソッドに残した。
   Dynamo では逆引きの持ち方（GSI か `COACT#`/`LINK#` の双方向書き込みか）が別途決めになるので、
   実装ごとに中身が変わる前提でよい。

3. **`get_stats` の集計。**
   件数・カテゴリ別・感情別は SQL 側の `GROUP BY` に寄せられるが、
   現行は全行を取ってから Python で数えている。振る舞いを変えないため、
   保管側は「emotion / category / timestamp の一覧と MIN/MAX」を返すところまでにして、
   数え上げは `MemoryStore` に残した。Dynamo では全件 Query になるので、段2 以降でカウンタ属性を検討する。

4. **`search_episodes` の `LIKE` 検索。**
   タイトルと要約の部分一致。SQL 固有だが件数が少ない前提の実装なので、
   「検索語と件数を渡して Episode を返す」形のまま保管側に置いた。Dynamo では Query + FilterExpression になる。

5. **`save` と `save_with_auto_link` の INSERT を 1 つの `insert_memory` に統合した。**
   従来 `save_with_auto_link` は `sensory_data` と `links` に空文字 `""` を、
   `save` は `to_metadata()` の `"[]"` を書いていた。統合後は両方 `"[]"` になる。
   読み出し側（`_parse_sensory_data` / `_parse_links`）はどちらも空タプルに解くため、
   API から見える振る舞いは変わらない。生の列値だけが揃う。

## 決まっているが今回は入れないもの（次の PR の範囲）

段0 のスコープ外。設計としては決まっているが、このブランチには一切含まれていない。

- **`FORGET#`** — 消した跡を残す前置辞。任意で理由を添える。運営は復元しない（本人だけの操作として扱う）。
- **`index:false`** — `remember` 時に本人が指定する「検索に載せない」指定。保存はするが意味検索の対象から外す。
- **`PRIV#`** — 本人だけの面。運営・共有の経路からは読めない区画。
- **一覧ツールの `random` 1 件と前後（隣接）** — `sk` の時刻順を使って、無作為の 1 件とその前後の記憶を返す。
- **tick 中のツール往復の計測** — 1 tick あたり何回ツールを呼んだか、どこで時間を使ったかの記録。

## 段1 の入口

段1 は「片流し複製」。書き込み経路（`insert_memory` / `update_*` / `delete_memory` / `put_coactivation`）を
SQLite と DynamoDB の両方へ流し、読みは SQLite のまま。
段0 で保管メソッドが 1 か所に揃ったので、段1 は `MemoryStoreBackend` を 2 つ束ねる
デコレータ実装（例 `DualWriteMemoryStore`）を足すだけで済む想定。
