# K20: 暗号シュレッダー — 「忘れる」をバックアップからも戻せない形にする

2026-09-24 なぎ承認。ぷちたち（3体）の要望「消したものは戻らなくていい／バックアップや PITR からも戻さないで」を、
**約束ではなく仕組みで**守る。澪の最初の記憶より前に入れる（記憶 0 件なので移行は無い）。

## 何が変わるか

段2 の `forget` は `FORGET#` の跡を残して本文を消すが、house 表は PITR 35日（と運用上のバックアップ）があるので、
戻そうと思えば戻せた。K20 では本文を **1 件ごとのデータ鍵（DEK）** で暗号化し、DEK を**別の表**に置く。
忘れるときは DEK を消す。本体の表をいつの時点に戻しても、その DEK はもう無い。

```
house 表（PITR 35日）                     鍵の表 petit-<env>-memory-keys（PITR 無効・バックアップ対象外）
  MEM#<ts>#<id>  sealed = AES-GCM(DEK)      P#<pid> / KEY#<id>   wrapped = KMS(DEK, ctx={pid})
  VEC#<id>       vector_sealed              ↑ forget で DeleteItem（本体より先）
  FORGET#…       平文の跡（本文なし）
```

## 暗号化するもの・しないもの

| 置き場 | 暗号化（`sealed` にまとめる） | 平文のまま |
|---|---|---|
| `MEM#` / `PRIV#` | `content`・`normalized_content`・`reading`・`sensory_data`（説明文・縮小画像）・`links`（note に本文が混ざりうる） | `id`・`timestamp`・`emotion`・`importance`・`category`・`tags`・`indexed`・`private`・`access_count` など・`linked_ids`（id だけ）・`camera_position`・`episode_id` |
| `VEC#` | ベクトル（本文をある程度復元できる） | `memory_id` |
| `EPI#`（K21 で追加） | `title`・`summary`・`participants`・`location_context` | `id`・`start_time`・`end_time`・`memory_ids`・`emotion`・`importance` |
| `FORGET#` / `IDX#` / `COACT#` | — | そのまま |

- 暗号化した行には平文で `enc_v = 1` が付く（射影だけの読みで「鍵が要る行か」を判定するため）。
- AAD は `"<pid>|<memory id>|mem"` / `"…|vec"`。暗号文を別の記憶・別のぷちに貼り替えても開かない。
- `EPI#` は K21（2026-09-24）で対象に入れた（本文から中身が分かるものは全部対象、の原則）。AAD は `"<pid>|<episode id>|epi"`、
  鍵は `KEY#<episode id>`（episode id は UUID なので記憶と衝突しない）。`delete_episode` は鍵を先に消す。

## 読み・検索

- 復号は `DynamoMemoryStore` の中、つまり**ぷちコンテナのプロセスの中だけ・その場だけ**。
- 平文の DEK はプロセスのメモリの LRU（既定 2 万件）にだけ置く。ディスクに平文を書かない。
- ベクトル検索・想起（Hopfield）は、母集団を読んだあとコンテナ内で復号してから計算する（数千件規模なので問題ない）。
- **鍵の無い暗号文は、どの読みでも「無いもの」**: ID 指定・一覧・新着・重要・意味検索・無作為・前後・ベクトル・facets。
  忘れた記憶を PITR で戻しても、指し札ごと戻っても、出てこない。

## 忘れる

`delete_memory` の順番:

1. 鍵の表から DEK を消す（`CryptoShredder.shred`）。**ここで読めなくなる**。
2. 他の記憶の `linked_ids` / `links` から外す（暗号化された `links` は開いて再暗号化）。
3. `MEM#`・`VEC#`・`IDX#`・`COACT#` を消し、`FORGET#` の跡を書く（1 トランザクション）。

途中で落ちても「読めない」側に倒れる。

## 切り替え（環境変数）

| 変数 | 例 | |
|---|---|---|
| `PETIT_MEMORY_KEYS_TABLE` | `petit-v0-memory-keys` | 鍵の表 |
| `PETIT_MEMORY_KMS_KEY_ID` | `alias/petit-v0-memory` | DEK を包む CMK |

- 両方あれば暗号化、両方無ければ従来どおり平文。片方だけは起動時に `ValueError`。
- **ローカル版（SQLite・既存 3 体）には一切効かない。** `PETIT_MEMORY_STORE=sqlite` の経路は触っていない。
- 暗号化を入れる前の平文の行も、暗号化を有効にした環境から普通に読める（混在可）。
- 逆に、暗号化を切った環境からは暗号化された記憶は見えない（鍵を持たない＝読めない）。

## 他のリポから使う（K21: m5-petit-app の `MSG#` など）

部品は `memory_mcp.crypto_shred`（house 表の形に依存しない）。

```python
from memory_mcp.crypto_shred import CryptoShredder, seal, open_sealed, seal_json, open_json

shredder = CryptoShredder.from_env(pid="mio")   # PETIT_MEMORY_KEYS_TABLE / PETIT_MEMORY_KMS_KEY_ID
# 書く
dek = shredder.new_key(msg_id)                   # 鍵の表に KEY#<msg_id> を置く（本体より先）
item["sealed"] = seal_json(dek, {"text": text}, shredder.aad(msg_id, "msg"))
# 読む
dek = shredder.key_for(msg_id)                   # None なら忘れた項目 → 無いものとして扱う
body = open_json(dek, item["sealed"], shredder.aad(msg_id, "msg"))
# 忘れる
shredder.shred(msg_id)                           # 本体の削除より先
```

- 鍵の表の sk は `KEY#<item id>`。item id は**ぷちの中で一意**にする（記憶と会話で衝突しない id：UUID など）。
- 多言語で読むときの暗号文の形: `0x01 || nonce(12) || AES-256-GCM(ciphertext||tag)`、AAD は UTF-8。
- 依存: `boto3` と `cryptography`（`pip install -e ".[dynamo]"`）。

## テスト

`tests/test_crypto_shred.py`（moto の DynamoDB・KMS）:

- house 表を全部読んでも、本文・読み・説明文・リンクの note・ベクトルの bytes が 1 つも出てこない。メタデータは平文。
- `forget` で鍵の表から `KEY#<id>` が消え、`FORGET#` の跡は残る。
- **復元の証明**: 3 件書く → 表を丸ごと退避（＝PITR の戻り先）→ 1 件忘れる → 退避した行を全部書き戻す
  （暗号文・指し札も物理的に戻る）→ 同じプロセスでも**キャッシュの無い別プロセス**でも、11 通りの読みのどれにも出てこない。
- 暗号化コンテキストの pid が違えば KMS が DEK を解かない。
- 暗号文の貼り替え（別 id の AAD）では開かない。

加えて段2 の振る舞いテスト（`tests/test_stage2_forget_priv.py`）を `sqlite` / `dynamo` / `dynamo_encrypted` の 3 通りで回している。

## 権限（petit-infra 側）

- CMK `alias/petit-<env>-memory` の鍵ポリシーは、`GenerateDataKey` / `Decrypt` / `Encrypt` を
  「ぷちコンテナが使うロール」かつ「暗号化コンテキストに `pid` がある」ときだけ許す。
  運営の管理者は鍵の管理はできても**使えない**（Decrypt の許可が無い）。
- すべての `Decrypt` は CloudTrail に残る。
- 運営のサポート用の復号は、Q-81(d)（里親の同意スイッチ＋期限＋記録）の条件で、KMS の grant を一時的に付ける
  「枠」だけを鍵ポリシーに置いた。手順は petit-infra README §9。
