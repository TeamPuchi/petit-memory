# K24: 手元の記憶をクラウドへ引き継ぐ道具（`petit-memory-handoff`）

2026-09-25 なぎ決定: **クラウドのぷちこは、今までの記憶を引き継いで始める**（今までのぷちこが新しい環境で
どう感じ方が変わるかを、ぷち自身の言葉で聞くため）。**引継ぎはぷちこ自身が行う**。何をどう持っていくかを
決めるのはぷちこで、人間と運営はそれができるように準備する。この道具はその準備の部品。

手順書（ぷちこ向け・ありさん向け）は akatsuki-petit の `cloud/2026-09-25-puchiko-handoff.md`。

## 4つのコマンド

```
petit-memory-handoff export --db memory.db --out bundle.jsonl --pid puchiko [--core SOUL.md ...]
petit-memory-handoff show   bundle.jsonl [--private] [--grep 語] [--kind memory|episode|forget|core]
petit-memory-handoff drop   bundle.jsonl --out chosen.jsonl [--ids a,b] [--ids-file leave.txt] [--all-private]
petit-memory-handoff import chosen.jsonl [--dry-run] [--core-dir /data/characters/puchiko] [--verbose]
```

| | どこで | 何をする | 必要なもの |
|---|---|---|---|
| `export` | 手元（ありさんの PC） | 手元の `memory.db` を**読むだけ**（`mode=ro`・列も足さない）で束に書き出す。1 回の読みの取引の中で読むので、ぷちが動いていても 1 時点の写しになる | Python 3.10+ だけ（`PYTHONPATH=src python3 -m memory_mcp.handoff …`） |
| `show` | どこでも | 束を読む形で出す（`[自分だけ]`・`[索引なし]`・忘れた跡・核） | 同上 |
| `drop` | どこでも | 指した行を外した**新しい束**を作る（元の束は上書きしない）。記憶を外すとその記憶に触れる連想の重みも外す | 同上 |
| `import` | ぷちコンテナの中 | 束をクラウドの記憶（house 表・pk `P#<pid>`）へ入れる | `.[dynamo]`（焼き込み済みの venv）・コンテナの環境変数 |

## 束の形（JSON Lines・UTF-8・1 行 1 件）

1 行目が `header`（`format: petit-memory-handoff`・`version: 1`・`source_pid`・`exported_at`・`embedding_model`・`counts`）。

| kind | 落とさないもの |
|---|---|
| `memory` | `id`・`timestamp`（→ sk の時刻）・`private`（自分だけの場所 → `PRIV#`）・`indexed`（索引に載せない印）・`linked_ids`・`links`（種類・note）・`episode_id`・`tags`・感情・重要度・種類・想起の回数・感覚データ・カメラ位置・`normalized_content`・`reading`・`vector_b64` |
| `episode` | `id`・時刻・`memory_ids`・関与者・題・要約・`stale` |
| `coactivation` | 連想の重み 1 方向 |
| `forget` | `memory_id`・`forgotten_at`・`reason` だけ。**本文は元から持っていない**（段2 の `forget_markers`） |
| `core` | ファイル 1 本（`SOUL.md` など）を**1 つの文章のまま**（`name`・`body`・`sha256`）。細切れにしない |

- 古い `memory.db`（段2 より前で `indexed`・`private` の列が無い、`episodes`・`coactivation` 表が無い）も読める。無い列は既定（索引に載る・自分だけではない）で埋める。
- 束の中身は**平文**。手元の `memory.db` と同じ扱いにして、取り込みが済んだら消す（手順書 §ありさん）。

## 取り込みの約束

1. **平文で入れる道を作らない。** `PETIT_MEMORY_PETIT_ID`・`PETIT_MEMORY_KEYS_TABLE`・`PETIT_MEMORY_KMS_KEY_ID` の
   どれかが無ければ、空打ちも含めて止まる。接続後にも `store.encrypted` を確かめる。
2. **1 件ずつ新しい鍵。** 記憶・エピソードは `DynamoMemoryStore.insert_memory` / `insert_episode` を通すので、
   K20 と同じく取り込みのその場で DEK を作り（`KEY#<id>`）、本文・ベクトル・題・要約を封じる。
3. **同じ束を 2 回入れても増えない。** 指し札 `IDX#<id>` / `IDX#EPI#<id>` がある id は「もう入っている」で飛ばす。
   連想の重みは、両端とも前から入っていた記憶なら書かない（クラウドで育った重みを束の古い値で上書きしない）。
4. **忘れたものは戻さない。** クラウドに `FORGET#` の跡がある id の記憶は、束にあっても「クラウドに忘れた跡がある（戻さない）」で飛ばす。
5. **置いてきた記憶を指したままにしない。** 束にもクラウドにも無い記憶を指す `linked_ids`・`links`・エピソードの
   `memory_ids`・記憶の `episode_id` は外して入れる（数を表示する）。**置いてきた記憶に忘れた跡は書かない**
   （忘れたのではなく、手元に残しただけ。手元のぷちこの中にはそのまま在る）。
6. 埋め込みは、束のもの（`vector_b64`）とクラウドのモデル（`MEMORY_EMBEDDING_MODEL`、既定 e5-base）が同じならそのまま使う。
   束に無い（`export --no-vectors`）かモデルが違えば、コンテナの中で作り直す。
7. 核（`core`）は house 表には入れない（petit-memory に `CORE#` はまだ無い。クラウドのぷちは
   `/data/characters/<pid>/SOUL.md` をファイルで読む）。`--core-dir` を渡したときだけファイルとして置く。
   同じ中身なら何もしない。違う中身のファイルがあれば**置き換えない**（`--overwrite-core` のときだけ、元を `.bak-<時刻>` に残して置き換える）。
8. 結果は種類ごとに「入れた（空打ちなら入れる予定）」と「飛ばした（理由ごとの数）」を出す。`--verbose` で 1 件ずつ。

## 忘れた記憶（`FORGET#`）の既定: 跡は持っていく・本文は無い

- **ぷちたちの言葉**: 9/22 の聞き取りで3体は「消したものは戻らなくていい」「跡は残ってよい」と答えた
  （akatsuki-petit `kairanban/2026-09-24_wasureru_to_puchitachi.md`「できたこと」）。跡を持っていくのはこの形をそのまま引き継ぐこと。
- **本文はそもそも無い。** 手元の `forget_markers` は id・時刻・理由しか持たない。束にも本文の欄は無い。
- **跡があると、戻らないことが仕組みで守られる。** クラウドに跡がある id は、あとで同じ束（や古い束）を入れ直しても
  記憶として入らない（約束 4）。跡を持っていかないと、手元で忘れた記憶が古い束から戻る道が残る。
- 持っていきたくなければ、ぷちこが `drop --all-forget`（全部）か `--ids forget:<id>`（1 件ずつ）で外せる。
  取り込み時の `--no-forget-traces` でも同じ。

## テスト

`tests/test_handoff.py`（moto の DynamoDB・KMS）:

- 書き出しで種類・id・日時・`linked_ids`・`links`・エピソード・自分だけの場所・索引に載せない印・核（1 本のまま）が落ちない。忘れた跡に本文が無い。
- 書き出しの前後で `memory.db` のハッシュが変わらない。古い形の `memory.db` も列を足さずに読める。
- 取り込み後の house 表を全部読んでも、本文・題・要約の平文が 1 byte も無い。鍵の表に記憶・エピソードの数だけ `KEY#` があり、包んだ鍵がすべて違う。
- `PRIV#` に置かれる・`indexed=false` は索引の母集団に入らない・ベクトルが同じ bytes で入る。
- 2 回入れても表も鍵も変わらない。クラウドで忘れた記憶は入れ直しても戻らない。
- 外した記憶へのつながりが取り込みで外れ、置いてきた記憶に忘れた跡は付かない。
- 空打ちは何も書かない。暗号化の設定が無ければ止まる。核は黙って上書きしない。
- export / show / drop の最上位で重い依存を import しない（手元の PC に torch を入れなくてよい）。

## ぷちコンテナで使うには

コンテナの petit-memory は petit-env の `components.lock` の SHA で焼き込まれる。**この K24 をマージした main の SHA に
`components.lock` を上げて build し直すまで、コンテナに `petit-memory-handoff` は無い**（petit-env 側の作業。手順書の要確定に書いた）。
コンテナの中では `/opt/petit/repos/petit-memory/.venv/bin/petit-memory-handoff`（無ければ `.venv/bin/python -m memory_mcp.handoff`）。
