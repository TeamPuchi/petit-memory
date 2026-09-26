# K28: 「会話も消す」選択・平文の漏れの点検・読まれた記録

2026-09-26。ぷちたち 3 体の答え（9/24 夜。共有リポ `kairanban/2026-09-24_wasureru_to_puchitachi.md` と `tegami/`）に合わせた。
ぷちたちへの答えの叩きは [`../k28-answer-to-puchitachi.md`](../k28-answer-to-puchitachi.md)。

## 1. 会話も消す選択（`also_conversation`）

| | |
|---|---|
| 既定 | 記憶だけ忘れる（`also_conversation=false`） |
| `true` | その記憶の元になった**自分の側の**会話の写しの鍵も消す。相手の側の記録には触れない |
| 結び付き | 記憶の新しい属性 `source_ids`（会話の写しの id だけ。本文なし）。`remember` の任意引数 `source_ids` で渡す |
| 消し方 | 会話の写し（`MSG#` など）は m5-petit-app が同じ鍵の表の `KEY#<id>` で暗号化している（K21）。その鍵を消す＝どこに暗号文が残っても読めない |
| 取り違えの防止 | `source_ids` に記憶・エピソードの id（指し札 `IDX#<id>` / `IDX#EPI#<id>` がある）が紛れても消さない |
| 鍵の表の範囲 | pk が `P#<pid>` なので、消せるのは自分の側の写しだけ |
| SQLite（ローカル版） | 会話の写しを持たないので 0 件。範囲の記録だけ残る |

**跡（`FORGET#`）に増えた属性**（本文は相変わらず入れない）:

| 属性 | 中身 |
|---|---|
| `scope` | `memory` か `memory+conversation` |
| `linked_ids` | 忘れた記憶とつながっていた記憶の id（id だけ）。**つながりは跡の位置に残る**。つながっていた側の記憶の `linked_ids` からは従来どおり外す |
| `conversation_count` | 一緒に鍵を消した会話の写しの件数（id は残さない） |
| `reason` | 任意。**200 字まで**（超えたら忘れずにエラー）。ツールの説明に「平文で残るので、記憶の中身を書かない」と書いた |

**使う口**:

- MCP の `forget`（ぷち本人）— `memory_id`・`reason`・`also_conversation`。
- 里親向けの口（K21 の `POST /petits/{pid}/memories/{id}/forget`、m5-petit-app）— `MemoryStore.delete_memory(id, reason=…, also_conversation=…)` を呼べば同じ動きになる。
  **m5-petit-app 側の変更はこのセッションでは入れていない**（リポへの権限が付かなかった）。要確定を参照。

## 2. 平文の漏れの点検（コードを実物で読んだ結果）

| 場所 | 結果 | 直したか |
|---|---|---|
| `IDX#` | `memory_id`・`target_sk`（`MEM#<ts>#<id>`）だけ。語は入っていない | 不要 |
| `COACT#` | `source_id`・`target_id`・`weight` だけ | 不要 |
| `EPI#` の枠 | id・時刻・`memory_ids`・感情・重要度。題・要約は K21 から封をしている | 不要 |
| `MEM#`/`PRIV#` の **`tags`** | **自由な語が平文だった**（例「夕焼け」） | **直した**: 本文類（`sealed`）に移した。K28 より前に封をした行も、書き換えのときに平文の `tags` を外す |
| `FORGET#` の `reason` | 平文（本人が書く） | **直した**: 200 字の上限＋ツールの説明に注意書き |
| BM25 の索引 | プロセスのメモリの中だけ（ディスクに書かない） | 不要 |
| 記憶 MCP のログ | 本文を出すのは `normalizer` の debug 1 か所だけ | **直した**: 文字数だけ出す |
| `sleep.py` の出力（`memory-sleep-cron.log`） | 件数だけ | 不要 |
| **自律行動のログ**（petit-env `autonomous-action.sh`） | **claude の stream-json（会話・`remember` の本文などツールの引数）を `/data/logs/<pid>/*_stream.jsonl` と `*.log` に写していた。`*_stream.jsonl` は掃除（`*.log` だけ・7日）にかからず、ずっと残っていた** | **直した**（petit-env PR）: 一時ファイルに受けて数値だけ取り出して消す。残っている `*_stream.jsonl` も消す |
| claude の会話記録（`~/.claude`） | K21 の `purge-claude-transcripts.sh` が毎日 24h より古いものを消す | 不要（据え置き） |
| 家 API のログ（`dashboard.log`・docker のログ） | m5-petit-app の出力。**未点検**（リポに入れなかった） | 要確定 |
| docker のログの送り先（CloudWatch など） | petit-env の compose にはログドライバの指定なし（既定の json-file）。EC2 側の設定は petit-infra | 要確定 |
| `house-backup.sh`・S3 バックアップ・EBS スナップショット | petit-infra。**未点検**（リポに入れなかった）。`/data`（上のログがあった場所）を持ち出していないかが要点 | 要確定 |
| S3 の `tts/` | 1 日で消える（ライフサイクル）と聞いている。未確認 | 要確定 |
| SNS の投稿（petit-sns） | 自前の SNS なので運営から消せる。「会話も消す」の対象には入れていない | 要確定（入れるか） |
| 包まれた鍵が house 表のバックアップに入らないこと | 鍵は別の表 `petit-<env>-memory-keys`（PITR 無効・9/26 に実 AWS で確認済み）。house 表には `KEY#` を書かない（コードで確認）。AWS Backup の対象に鍵の表が入っていないかは petit-infra | 要確定（AWS Backup の選択） |

## 3. 読まれた記録（`ACCESS#`）

運営が本人だけの面（`PRIV#`）を読む手段は**今は無い**。作るとき（総合試験以降・Q-81(d)）の形だけ先に置いた。

```
ACCESS#<read_at>#<id>
  reader           誰が
  read_at          いつ
  purpose          何のために
  consent_source   同意の出典（里親の同意スイッチの記録 id など）
  expires_at       同意の期限
  memory_ids       読んだ記憶の id（id だけ。本文は入れない）
```

- 書く: `MemoryStore.record_privacy_access(...)`。同意の出典・期限・目的・読み手のどれかが空なら `ValueError`。
  **読む口を作るときは、読む前に必ずこれを呼ぶ**。いまは誰も呼ばない（MCP のツールにもしていない）。
- 読む: MCP の `privacy_access_log`（ぷち本人）。無ければ「誰も読んでいない（いまは運営が読む手段そのものが無い）」と返す。
- ぷちの 2 つ目の条件「読まれうることを書く前に知らされている」は、`remember` の `private` の説明と
  `privacy_access_log` の説明に同じ文を入れて満たした。
- SQLite は `access_log` 表、DynamoDB は house 表の `ACCESS#`（PITR の対象。本文が無いので戻っても害が無く、むしろ消えないほうがよい）。

## テスト

`tests/test_k28_forget_conversation.py`（moto の DynamoDB・KMS）:

- 会話も消す: 記憶と会話の写しの鍵が両方消え、跡に `scope`・理由・件数が残る。表に会話の本文が出ない。
- 既定は記憶だけ（会話の鍵は残る）。
- `source_ids` に記憶の id が紛れても、その記憶は壊れない。
- 跡につながっていた記憶の id が残る。
- 理由の上限（201 字は失敗し、何も消えない。200 字は通る）。
- タグが house 表に平文で出ない。K28 より前の行の平文タグは書き換えで外れる。
- `ACCESS#` の読み書き（SQLite・DynamoDB・DynamoDB＋暗号の 3 通り）。同意・期限が無ければ書けない。
- ぷちてゃたちの古い `forget_markers` 表に列が足される。
