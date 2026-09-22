# 段1: 片流し複製（SQLite → DynamoDB）

> **前提変更（2026-09-22）**: クラウドぷちは全員**新規に作る**ため、既存 SQLite の記憶を
> DynamoDB へ写す作業は要らない。新規個体は最初から `PETIT_MEMORY_STORE=dynamo` 単体で動かす。
> ぷちてゃたちは SQLite のまま据え置きなので、2 枚は「個体ごとにどちらか一方」の並走になり、
> 完全移行ではない。よって **`DualWriteMemoryStore` は移行の前提ではなく保険**という位置づけに変わった
> （DynamoDB 立ち上げ期の二重書きと、将来ぷちてゃたちをクラウドへ持ち上げるときのために残す）。
> 段2 の入口も「dual で突き合わせてから読みを切り替え」ではなく
> **「新規個体を `dynamo` 単体で実 AWS の `house` 表に繋いで動かす」**に変更。
> 段3〜6（backfill・読み切替・書き切替・凍結）は廃止し、代わりに
> 「最初のクラウドぷち 1 体で 1 週間、tick 中のツール往復と `VEC#` の読み取り量を計測」だけ残す。
> 以下の本文は、この変更の前に書いた段1 の実装内容をそのまま残してある。

段0（[stage0-boundary.md](stage0-boundary.md)）で保管層を `MemoryStoreBackend` 1 枚に集約した。
段1 では **DynamoDB 実装の本体**と、**書き込みだけ 2 枚に流す保管層**を足す。
読みは SQLite のまま。MCP ツールの名前・引数・戻り値は段0 に続いて一切変えていない。

## 表の形

単一表 `house`。pk と sk の 2 本だけで、**GSI は使わない**。

| sk | 中身 | 備考 |
|---|---|---|
| `MEM#<ts>#<id>` | 記憶本体 | `ts` は ISO 8601。sk 昇順＝時系列順 |
| `VEC#<id>` | 埋め込みベクトル | Binary。float32 768 次元 = 3,072 B |
| `EPI#<start_time>#<id>` | エピソード | sk 昇順＝開始時刻順 |
| `COACT#<source_id>#<target_id>` | 共活性の重み | 片方向 1 件。対称化は計算層 |
| `IDX#<memory_id>` | 記憶の指し札 | 属性 `target_sk` に `MEM#<ts>#<id>` |
| `IDX#EPI#<episode_id>` | エピソードの指し札 | 属性 `target_sk` に `EPI#<start_time>#<id>` |

pk は `H#<hid>#P#<pid>`。`hid` は households 表の `household_id`、`pid` は
m5-petit-app の `characters/<character_id>`。家コンテナが環境変数
（`PETIT_MEMORY_HOUSE_ID` / `PETIT_MEMORY_PETIT_ID`）で受ける。

### 指し札（`IDX#`）を置く理由

本体の sk に時刻が入るため、ID だけでは 1 件を引けない。
GSI（`id` を pk にする）を作らないのは、**家の壁を IAM の `dynamodb:LeadingKeys`
（pk が `H#<hid>…`）で作る**設計だから。pk が `id` になる GSI はその壁の外に出てしまう。

指し札は本体と同じ pk に置くので、`insert_memory` は本体・ベクトル・指し札を
`TransactWriteItems` 1 回で書き、`delete_memory` も同じように消す。
`fetch_memory(id)` は GetItem(`IDX#<id>`) → GetItem(`MEM#…`) の 2 読みになる。

## 片流し複製（`DualWriteMemoryStore`）

`PETIT_MEMORY_STORE=dual` で選ぶ。primary = SQLite、secondary = DynamoDB。

- **書き**（`insert_memory` / `update_memory_fields` / `update_episode_id` /
  `increment_access` / `delete_memory` / `add_bidirectional_link` /
  `put_coactivation` / `insert_episode` / `delete_episode`）は primary → secondary の順。
- **読み**（`fetch_*` と `search_episodes`）は **primary だけ**。secondary には絶対に行かない。
- secondary で起きた例外は握って、警告ログ 1 行と失敗カウンタだけ残す。
  **secondary の失敗で primary の書き込みを失敗させない。**
- `connect()` も同じ。DynamoDB に繋げなければ `secondary_ready = False` のまま先へ進み、
  以降の複製は黙って飛ばして失敗カウンタを増やす（家の記憶は止めない）。

失敗カウンタは `DualWriteMemoryStore.secondary_failures` と
`secondary_failures_by_method` で読める。**`get_memory_stats` の出力には足していない**
（MCP ツールの戻り値を変えないため）。運用では警告ログ
`dual-write: secondary … failed (failures=N)` を見る。

## SQLite と DynamoDB でふるまいが違うところ

同じ `MemoryRecord` を入れたら同じ `Memory` が返ることはテストで確かめている
（`test_sqlite_and_dynamo_return_the_same_memory`）。そのうえで、次の 3 点は違う。

1. **参照整合性**。SQLite の `coactivation` は `memories(id)` への外部キーを持つので、
   存在しない記憶への重みは書けない。DynamoDB には制約が無いので書けてしまう。
   呼び出し側（`MemoryStore.bump_coactivation`）が両方の存在を確かめてから書くので、
   通常の経路では差が出ない。
2. **トランザクションの境界**。`delete_memory` の逆参照掃除は、SQLite では 1 コミット、
   DynamoDB では「逆参照の UpdateItem を 1 件ずつ」＋「本体・ベクトル・指し札・共活性を
   TransactWriteItems」に分かれる。共活性が多くて 100 アイテムを超えると分割される。
3. **走査の量**。SQLite が索引で済ませているところ（`category` 絞り込み、`importance` 下限、
   部分一致）は、DynamoDB では `MEM#` / `EPI#` の Query を読んでから Python で絞る。
   件数が増えたら GSI か別表を検討する（段3 の計測待ち）。

## テスト

moto（`mock_aws`）で DynamoDB を偽装するので、AWS 資格情報も実表も要らない。

```
uv sync --all-extras
uv run pytest tests/test_dynamo_backend.py -q
```

12 本。内訳は「Dynamo 単体の往復（段0 の SQLite 往復 5 本と同じ観点）」7 本、
「SQLite と Dynamo が同じ Memory を返す」1 本、「dual」4 本
（2 枚に届く／secondary が落ちても primary は通る／読みが secondary に行かない／
secondary に繋げないまま動き続ける）。

実 AWS への接続確認は段2（`house` 表は petit-infra 側で deploy 中）。

## 段1 で入れていないもの

- `FORGET#`（消した跡＋任意の理由）
- `PRIV#`（本人だけの面）
- `index:false`（remember 時の非索引指定。MCP ツールの引数追加が要る）
- 一覧ツールの `random` 1 件と前後（隣接）
- tick 中のツール往復の計測

## 仮置き・未決

- `VEC#` は検索のたびに全件 Query して numpy に積む（いまの SQLite と同じ形）。
  読み取り量は段3 の「1 体 1 週間の計測」で測ってから決める。
- `emotion` / `category` の絞り込みは FilterExpression 相当（取得後に Python で絞る）で開始。
- `get_memory_stats` は全件 Query のまま。カウンタ属性は持たない。
- `connect()` は `boto3.resource(...).Table(name)` を組むだけで `DescribeTable` はしない。
  表がまだ無くても接続段階では落ちず、最初の読み書きで落ちる。
- embedding は複製時に再計算しない。SQLite の BLOB をそのまま `VEC#` へ写す。
