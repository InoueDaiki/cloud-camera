# lambdas/common

動画の退避(`video_archiver`)と物体検出(`detector`)で共用する処理。
処理済み位置(カーソル)、リース、1 分区間のループ、ウォーターマーク(断片が出そろったかの判定)、KVS の読み出しヘルパー、構造化ログ。
設計は [構想.md](../../構想.md) の「処理状態の管理」、テーブルの形は [docs/contracts.md](../../docs/contracts.md)。

## 同梱のしかた

このディレクトリをそのまま zip の直下に `common/` として入れる(`tests/`、`pyproject.toml`、`.venv/`、`pyav_min/` などは除く)。
依存は boto3 だけで、Lambda のランタイムに入っているものを使う。
`frames.py`(キーフレームのデコード)を使う Lambda(video_archiver、detector)は、PyAV と Pillow も zip に入れる。
PyAV は `pyav_min/build.sh "$STAGE"` で最小構成の FFmpeg ごと入れる(docker でビルドし、`build/.cache/pyav_min/` にキャッシュ。[FRAMES.md](FRAMES.md))。

```sh
# build.sh の例
bash lambdas/common/pyav_min/build.sh "$STAGE"     # av/、lib/(frames.py を使う場合)
rsync -a --exclude tests --exclude .venv --exclude '__pycache__' --exclude pyproject.toml \
  --exclude uv.lock --exclude README.md --exclude FRAMES.md --exclude pyav_min lambdas/common/ "$STAGE/common/"
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
| `max_late_seconds` | ウォーターマークを待つ上限(秒)。省略時は環境変数 `MAX_LATE_SECONDS`、既定 720(下の「ウォーターマーク」) |
| `stream_name` | KVS のストリーム名(省略時は `kvs.stream_name_for(camera_id)`、つまり `STREAM_NAME_PREFIX` + カメラ ID) |
| `list_fragments` | `kvs.list_fragments` の差し替え(テスト用) |
| `use_watermark` | `False` ならウォーターマークを見ない(KVS を使わない単体テスト用。本番では使わない) |
| `upstream_kind` | 上流の種類(物体検出なら `"VIDEO"`)。上流の処理済み位置を越えて処理しない(下の「上流を待つ」) |
| `upstream_max_wait` | 上流を待つ上限(秒、区間の終わりから)。省略時は環境変数 `UPSTREAM_MAX_WAIT_SECONDS`、既定 1800 |

## ウォーターマーク(区間を処理してよいか)

回線が切れている間、エッジ(kvssink)は断片をバッファに貯め、復旧後に **未 ACK の分を古い順に** 送る。
そのため「ある時刻より後の ProducerTimestamp を持つ断片が KVS に届いていれば、それより前の断片はすべて届いている」とみなせる。
共通処理はこれを使い、区間 [s, e) を次のどちらかを満たしたときだけ処理する(確定させる)。

- (a) **ウォーターマーク ≥ e**。ウォーターマーク = 届いている最新の断片の終わり(ProducerTimestamp + `FragmentLengthInMilliseconds`)
- (b) **現在 − e ≥ `MAX_LATE_SECONDS`**(既定 720 秒 = エッジのバッファ上限 10 分 + 余裕)。届いている分で確定する

満たさなければそこで止め、次の起動を待つ(区間の件は作らず、試行回数にも数えない。`pipeline_done` の `stopped` は `waiting`)。
区間は 1 回だけ処理し、再処理はしない。「処理済み位置より前はすべて確定」は変わらない。

**呼び出し**: 処理する区間があるときだけ、起動ごとに ListFragments(プロデューサー時刻、カーソル 〜 現在 + 60 秒)を **1 回** 呼ぶ
(断片 1,000 本ごとに 1 ページ。1 秒の断片なら約 16 分ぶんまで 1 回)。結果は起動の間だけキャッシュし、
`process_minute` の中の `kvs.list_fragments(stream, start, end)` はそこから返る(API を呼ばない)。
追いついていて処理する区間がない起動では呼ばない。
以前(`minute_ready` を使っていたとき)の動画退避は 1 区間あたり ListFragments 2 回(サーバー時刻 1 回 + プロデューサー時刻 1 回)だったので、
通常時(1 起動 = 1 区間)は 2 回 → 1 回に減り、追いつくとき(1 起動 = n 区間)は 2n 回 → 1 回になる。物体検出も一覧はキャッシュから返る。
KVS の料金表にはリクエスト単位の項目がなく(取り出しは GB 単位)、ListFragments の呼び出しによる追加の費用はない見込み。
ウォーターマーク待ちの起動(`waiting`)は ListFragments 1 回と DynamoDB のリース 2 回だけで、Lambda の実行は 1 秒未満。

**限界**:
- `MAX_LATE_SECONDS` より後に届いた断片は処理しない(その区間は確定済み)。エッジのバッファ上限を超えた分はエッジ側でも捨てられている
- 配信停止中、カメラの故障、ゲートウェイのオフライン中は断片が届かないので、処理済み位置は現在より約 `MAX_LATE_SECONDS`(+ lag)遅れる。
  その間の区間はビューワーが KVS(保持 1 時間)から再生する。停止が続けば、`MAX_LATE_SECONDS` 遅れで `EMPTY` が確定していく
- ListFragments に失敗したらウォーターマークは不明(`watermark_error`)として待つ。続けば (b) で進む
- 前提はエッジの送信が時刻順であること(kvssink の動作)。時刻を戻すと、戻った分は確定済みの区間に入って捨てられる

## 上流を待つ(物体検出が動画の退避の clip.mp4 を読む)

物体検出は、動画の退避が S3 に置いた `clip.mp4` からキーフレームをデコードする([FRAMES.md](FRAMES.md))。
そのため物体検出(DETECT)は `upstream_kind="VIDEO"` で動かし、**物体検出の処理済み位置は動画の退避の処理済み位置を越えない**ようにする。

- 起動ごとに 1 回(処理する区間があるときだけ)、`CURSOR#VIDEO` の `processed_until` を強い整合性で読む(GetItem 1 回)
- 区間 [s, e) は、**動画の処理済み位置 ≥ e** のときだけ処理する。満たさなければそこで止める
  (`pipeline_done` の `stopped` は `waiting_upstream`。区間の件は作らず、試行回数にも数えない。ウォーターマーク待ちと同じ扱い)
- 動画の処理済み位置 ≥ e なら、**自分のウォーターマークは見ない**。動画の退避がウォーターマーク(または `MAX_LATE_SECONDS`)で
  判定して確定させた区間なので、clip.mp4 と同じ断片の集合で検出する(動画が `MAX_LATE_SECONDS` で確定させた区間に、後から届いた断片は
  動画にも検出にも入らない。両者が食い違わない)
- 動画の退避が止まって、区間の終わりから `upstream_max_wait`(既定 1800 秒)を過ぎても処理済み位置が進まないときだけ、
  上流を待たずに自分のウォーターマークで判定して処理する(`minute_upstream_late`)。process_minute は区間の件に `video_status` が
  ないのを見て、予備の経路(GetImages)で取る。1800 秒は、動画の退避のウォーターマーク待ちの上限(720 秒)+ lag より長く、
  KVS の保持期間(1 時間)より十分前(予備の経路で KVS から読める時間を残す)
- 区間の件の `video_status` の扱いは process_minute(detector の `frame_source.clipfile`)が決める:
  `DONE` → clip.mp4、`EMPTY` → 空、`FAILED` → 予備の経路、なし → `NotReady`(上の判定を通ったあとで件がない、というずれへの保険)

書く順序は「動画の退避: S3 → 区間の件(`video_status`)→ `CURSOR#VIDEO`」なので、動画の処理済み位置 ≥ e なら、
その区間の `video_status`(`DONE` なら S3 の clip.mp4 も)は必ずある。例外は、動画の退避のカーソルの初期値(初回起動時の「現在 − lag − 5 分」)
より前の区間で、件が作られない。物体検出のカーソルの方が古いとこの区間で `NotReady` が続き、`upstream_max_wait` を過ぎてから予備の経路で処理される。

遅れ: 物体検出は、動画の退避が区間を確定させた後の起動で処理する。同じ 1 分ごとのスケジュールなら、動画の退避より 0〜1 分遅れる。

## 再送による重複

復旧時、kvssink は ACK を受け取れなかった断片を送り直すので、同じ ProducerTimestamp の断片が KVS に 2 つ入りうる。
`kvs.list_fragments`(プロデューサー時刻)は ProducerTimestamp で重複を除く(大きい方、同じなら断片番号の小さい方を残す)。
clip の連結、`segments`、`fragment_count` はこの一覧から作る。`kvs.get_images` も同じ時刻の画像を 1 枚にする。

### `process_minute(camera_id, start, end) -> MinuteResult`

- `start` / `end` は UTC の aware datetime(`end = start + 1 分`)
- `MinuteResult(status, attributes)`。`status` は `DONE` / `EMPTY`、再試行しても無駄な失敗なら `FAILED`(即確定)
- `attributes` は区間の件に `SET` する属性。float は Decimal に直す。`{kind}_status`、`{kind}_attempts`、`{kind}_duration_ms`、`expires_at`、キーは共通処理が書くので無視される
- 例外を投げると失敗扱い。区間の件に `{kind}_error` を残し、カーソルはその手前で止まる(次の起動で再挑戦)
- `common.NotReady` を投げると「まだ処理できない」扱い。試行回数に数えず(足した 1 を戻す)、カーソルはその手前で止まる。
  断片が出そろったかは共通処理がウォーターマークで判定済みなので、それ以外の「待てば処理できる」理由があるときに使う

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
| `list_fragments(stream, start, end)` | ListFragments(プロデューサー時刻)をページ送りし、**開始時刻が `[start, end)`** の断片を時刻順に返す。同じ ProducerTimestamp の重複は 1 つにする。run_pipeline のキャッシュに収まる範囲は API を呼ばない |
| `effective_durations(fragments, gap_ms=500)` | 断片の実際の長さ。`FragmentLengthInMilliseconds` は最後の 1 フレーム分短い(実測)ので、次の断片の開始までを長さとする |
| `build_segments(fragments, gap_ms=500)` | 断片の隙間が 500ms 以内ならつないだ録画範囲 `[[開始ms, 終了ms], ...]`(長さは `effective_durations`) |
| `dedupe_fragments(fragments)` / `watermark_ms(fragments)` | 同じ ProducerTimestamp の重複を除く / ウォーターマーク(最新の断片の終わり、epoch ミリ秒) |
| `set_fragment_cache` / `clear_fragment_cache` | run_pipeline が使う、起動の間だけの断片一覧のキャッシュ |
| `get_images(stream, start, end, interval_ms=, width=, height=)` | GetImages(プロデューサー時刻)をページ送りして時刻順に返す(同じ時刻は 1 枚)。範囲の始まりの近くに断片がないと `ResourceNotFoundException` になる(実測) |

### 遅れて届く断片(経緯)

送信側(エッジ)が詰まると、断片は時刻順のまま遅れて届く。検証では送信コンテナの CPU 不足で、届くのが 30 秒 → 100 秒と遅れ続け、
lag 30 秒のまま処理した区間は 60 本中 42 本しか入らなかった。以前は `kvs.minute_ready`(最近届いた断片で判定し、
何も届いていなければ「送信が止まった」とみなして進む)で対処していたが、回線断のあとにバッファから遅れて届く断片を取りこぼした。
今はウォーターマークで判定する(`minute_ready` は廃止。呼び出し側を消すまでの互換として、常に `(True, "watermark")` を返す関数だけ残している)。

## ログ

`common.log(event, **fields)` で 1 行 1 JSON を標準出力に出す。共通処理は `minute_done` / `minute_retry` / `minute_not_ready` / `minute_failed` / `minute_waiting`(ウォーターマーク待ち)/
`minute_max_late`(待つ上限を過ぎて確定)/ `watermark_error` / `pipeline_done` / `lease_busy` などを出す。
`pipeline_done` の `stopped` は `caught_up` / `max_minutes` / `time` / `retry` / `not_ready` / `waiting` / `waiting_upstream` / `lease_lost`。
上流を待つときは `minute_waiting_upstream`、待つ上限を過ぎて上流なしで処理するときは `minute_upstream_late` も出る(`pipeline_done` に `upstream_processed_until`、`upstream_late`)。
`pipeline_done` には `watermark`(ISO、ミリ秒付き)と `watermark_fragments`(取った断片の数)、`max_late`(上限で確定した区間)も入る。

## テスト

```sh
cd lambdas/common
mise exec -- uv sync
mise exec -- uv run pytest
```

DynamoDB は moto、KVS は botocore の Stubber で差し替えている(実 AWS には接続しない)。
`tests/test_frames.py` は PyAV が要る(`uv run --with av --with pillow --with numpy pytest`、FRAMES.md)。なければ skip される。
テスト用の H.265 fMP4 を作る `tests/media.py` は、video_archiver と detector のテストからも `common.tests.media` として使う。
