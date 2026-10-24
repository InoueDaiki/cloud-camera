# lambdas/common

動画の退避(`video_archiver`)と物体検出(`detector`)で共用する処理。
処理済み位置(カーソル)、リース、1 分区間のループ、KVS の読み出しヘルパー、構造化ログ。
設計は [構想.md](../../構想.md) の「処理状態の管理」、テーブルの形は [docs/contracts.md](../../docs/contracts.md)。

## 同梱のしかた

このディレクトリをそのまま zip の直下に `common/` として入れる(`tests/`、`pyproject.toml`、`.venv/` などは除く)。
依存は boto3 だけで、Lambda のランタイムに入っているものを使う。

```sh
# build.sh の例
rsync -a --exclude tests --exclude .venv --exclude '__pycache__' --exclude pyproject.toml \
  --exclude uv.lock --exclude README.md lambdas/common/ "$STAGE/common/"
```

## 使い方

```python
from common import MinuteResult, run_pipeline
from common import kvs

def process_minute(camera_id, start, end) -> MinuteResult:
    stream = kvs.stream_name_for(camera_id)          # STREAM_NAME_PREFIX + camera_id
    frags = kvs.list_fragments(stream, start, end)   # 開始時刻が [start, end) の断片
    if not frags:
        return MinuteResult("EMPTY", {"detect_count": 0})
    images = kvs.get_images(stream, start, end, interval_ms=1000, width=640, height=360)
    ...
    return MinuteResult("DONE", {"detect_count": n, "frame_count": len(images)})

def lambda_handler(event, context):
    return run_pipeline("DETECT", event["camera_id"], process_minute,
                        lag_seconds=60, max_minutes=5, max_attempts=3, context=context)
```

`context` は必ず渡す(残り時間で打ち切る、リースの長さを決める、`lease_owner` にリクエスト ID を入れる)。

### `run_pipeline` の引数

| 引数 | 内容 |
| --- | --- |
| `kind` | `"VIDEO"` / `"DETECT"`。カーソルの SK(`CURSOR#{kind}`)と、区間の件に書く属性の接頭辞(`video_` / `detect_`)が決まる |
| `lag_seconds` | 遅れて届く断片を待つ秒数。処理対象の終わりは「現在 − lag」を 1 分境界に切り捨てた時刻 |
| `max_minutes` | 1 回の起動で処理する区間数の上限(KVS の保持期間を過ぎた区間は数えない) |
| `max_attempts` | 1 区間の試行回数の上限。達したら `FAILED` で確定して先へ進む |
| `context` | Lambda のコンテキスト(省略可。ローカル実行用) |
| `table` | DynamoDB の Table リソース(省略時は環境変数 `STATE_TABLE`) |
| `now` | 現在時刻を返す関数(テスト用) |
| `retention_days` | `expires_at` の日数(省略時は環境変数 `MEDIA_RETENTION_DAYS`、既定 30) |
| `kvs_retention_seconds` / `expiry_margin_seconds` | KVS の保持期間(既定 3600)と、期限が「近い」とみなす余裕(既定 300) |
| `min_remaining_ms` | 残り時間がこれ未満(またはそれまでで最も長かった区間の 1.5 倍未満)なら次の区間に進まない(既定 15000) |

### `process_minute(camera_id, start, end) -> MinuteResult`

- `start` / `end` は UTC の aware datetime(`end = start + 1 分`)
- `MinuteResult(status, attributes)`。`status` は `DONE` / `EMPTY`、再試行しても無駄な失敗なら `FAILED`(即確定)
- `attributes` は区間の件に `SET` する属性。float は Decimal に直す。`{kind}_status`、`{kind}_attempts`、`{kind}_duration_ms`、`expires_at`、キーは共通処理が書くので無視される
- 例外を投げると失敗扱い。区間の件に `{kind}_error` を残し、カーソルはその手前で止まる(次の起動で再挑戦)
- `common.NotReady` を投げると「まだ処理できない」扱い。試行回数に数えず(足した 1 を戻す)、カーソルはその手前で止まる。
  断片が出そろっていないとき(下の `kvs.minute_ready`)に使う

### 区間の件に共通処理が書く属性

| 属性 | タイミング |
| --- | --- |
| `{kind}_attempts` | 処理の **前** に 1 足す。タイムアウトやメモリ不足で落ちても回数が残り、上限を超えたら処理せずに `FAILED` にする |
| `{kind}_status` | 確定したとき |
| `{kind}_duration_ms` | process_minute の実行時間 |
| `{kind}_error` | 失敗したとき(理由の文字列)。成功したら消す |
| `expires_at` | 区間の開始 + `retention_days` 日(epoch 秒) |

### FAILED になる条件

| 条件 | `{kind}_error` の先頭 |
| --- | --- |
| 区間の終わりが KVS の保持期間(1 時間)より前 — 処理せずに確定 | `expired:` |
| 試行回数が上限に達して失敗 | `max_attempts:` |
| 区間の始まりが保持期限まで 5 分以内で失敗 | `near_expiry:` |
| process_minute が `FAILED` を返した | (process_minute 次第) |

## KVS ヘルパー(`common.kvs`)

| 関数 | 内容 |
| --- | --- |
| `stream_name_for(camera_id)` | `STREAM_NAME_PREFIX` + カメラ ID |
| `get_data_endpoint(stream, api)` | GetDataEndpoint をプロセス内で 1 時間キャッシュ |
| `archived_media_client(stream, api)` | データエンドポイント向けの `kinesis-video-archived-media` クライアント(キャッシュ) |
| `list_fragments(stream, start, end)` | ListFragments(プロデューサー時刻)をページ送りし、**開始時刻が `[start, end)`** の断片を時刻順に返す |
| `effective_durations(fragments, gap_ms=500)` | 断片の実際の長さ。`FragmentLengthInMilliseconds` は最後の 1 フレーム分短い(実測)ので、次の断片の開始までを長さとする |
| `build_segments(fragments, gap_ms=500)` | 断片の隙間が 500ms 以内ならつないだ録画範囲 `[[開始ms, 終了ms], ...]`(長さは `effective_durations`) |
| `get_images(stream, start, end, interval_ms=, width=, height=)` | GetImages(プロデューサー時刻)をページ送りして時刻順に返す。範囲の始まりの近くに断片がないと `ResourceNotFoundException` になる(実測) |
| `minute_ready(stream, end, now, quiet_seconds=30, max_wait_seconds=600)` | 区間の断片が出そろったか。最近 quiet 秒に届いた断片に end 以降のものがある / 何も届いていない / max_wait を過ぎた、なら出そろったとみなす。`(bool, 理由)` |

### 遅れて届く断片(minute_ready を使う理由)

送信側(エッジ)が詰まると、断片は時刻順のまま遅れて届く。検証では送信コンテナの CPU 不足で、届くのが 30 秒 → 100 秒と遅れ続け、
lag 30 秒のまま処理した区間は 60 本中 42 本しか入らなかった(残りは後から届いたが、カーソルが進んでいたので取りこぼした)。
`process_minute` の最初で `kvs.minute_ready` を呼び、出そろっていなければ `NotReady` を投げると、送信が追いつくまで待てる。

```python
ready, why = kvs.minute_ready(stream, end, utcnow(), quiet_seconds=lag_seconds)
if not ready:
    raise NotReady(why)
```

送信が止まっている(回線断など)場合は「何も届いていない」で出そろったとみなすので、回線断のあとにエッジのバッファから遅れて届いた断片は取りこぼす。

## ログ

`common.log(event, **fields)` で 1 行 1 JSON を標準出力に出す。共通処理は `minute_done` / `minute_retry` / `minute_not_ready` / `minute_failed` / `pipeline_done` / `lease_busy` などを出す。
`pipeline_done` の `stopped` は `caught_up` / `max_minutes` / `time` / `retry` / `not_ready` / `lease_lost`。

## テスト

```sh
cd lambdas/common
mise exec -- uv sync
mise exec -- uv run pytest
```

DynamoDB は moto、KVS は botocore の Stubber で差し替えている(実 AWS には接続しない)。
