# コンポーネント間の契約

各コンポーネント(エミュレーター、エッジ、Lambda、Terraform、ビューワー)が並行開発するための共通の約束事。
変更するときは、このファイルを先に直してから実装を合わせる。設計の背景は [構想.md](../構想.md)。

## 共通

| 項目 | 値 |
| --- | --- |
| AWS プロファイル / リージョン | `cloud-camera` / `ap-northeast-1`(`mise.toml` の env で設定済み) |
| リソース名の接頭辞 | `var.project`(既定 `cloud-camera`)。以下 `{p}` と書く |
| カメラ ID | `cam-001` 形式。Terraform の `var.cameras`(list)で定義 |
| ゲートウェイ ID | `gw-001` 形式。IoT Thing 名は `{p}-{gateway_id}` |
| KVS ストリーム名 | `{p}-{camera_id}`。保持期間 1 時間。media type はカメラのコーデック(既定 `video/h265`、Terraform の `camera_codecs` で `video/h264`。[codec.md](codec.md)) |
| 時刻 | すべて UTC。文字列は ISO 8601 の `2026-10-24T10:20:00Z`(区間の SK はミリ秒なし) |
| Lambda | Python 3.13、arm64、zip デプロイ(コンテナイメージは使わない) |

## ディレクトリ

| パス | 中身 | 言語 |
| --- | --- | --- |
| `emulator/` | ONVIF カメラエミュレーター(コンテナ) | Python + MediaMTX + FFmpeg |
| `edge/` | エッジゲートウェイ(コンテナ)。ONVIF、IoT Core、KVS 送信 | Python + GStreamer(kvssink) |
| `lambdas/common/` | 処理済み位置・リース・区間ループの共通処理 | Python |
| `lambdas/video_archiver/` | 動画の退避(1 分ごと) | Python |
| `lambdas/detector/` | 物体検出(1 分ごと、ONNX Runtime) | Python |
| `lambdas/api/` | ビューワー向け HTTP API と自然言語検索 | Python |
| `infra/` | Terraform | HCL |
| `viewer/` | ビューワー | React + TypeScript + Vite |
| `scripts/` | ビルド、デプロイ、検証の補助スクリプト | bash / Python |
| `tools/cost/` | コスト計測(単価表、実測の収集、机上計算、レポート)。リソース名はこのファイルの命名規則から導く | Python |

各 Lambda ディレクトリは `build.sh` を持ち、`build/<name>.zip` を出力する(Terraform はこの zip を参照する)。
依存は `pip install --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.13` 等で arm64 用を入れる。
`lambdas/common/` は各 zip に同梱する(レイヤーにしてもよい)。

## S3

| バケット | 用途 | 備考 |
| --- | --- | --- |
| `{p}-media-{account_id}` | 動画、スプライト、プレイリスト | CloudFront(OAC)経由で配信 |
| `{p}-detections-{account_id}` | 検出結果の Parquet | バージョニング有効、旧バージョンは 1 日後に削除。現行版は映像と同じ保存日数(`media_retention_days`、既定 30 日)で削除 |
| `{p}-athena-{account_id}` | Athena のクエリ結果 | 7 日で削除 |
| `{p}-viewer-{account_id}` | ビューワーの静的ファイル | CloudFront(OAC)経由で配信 |
| `{p}-artifacts-{account_id}` | Lambda の zip(`lambda/<name>.zip`) | Terraform が S3 経由でデプロイするため(50MB 超に対応) |

### メディアのキー(1 分区間ごと)

`{camera_id}/{YYYY}/{MM}/{DD}/{HH}/{mm}/` の下に置く。例: `cam-001/2026/10/24/10/20/`

| ファイル | 中身 |
| --- | --- |
| `clip.mp4` | その 1 分の動画。単体で再生できる fragmented MP4(init + 断片を連結したもの)が第一候補 |
| `index.m3u8` | その 1 分の HLS(fMP4)プレイリスト。`clip.mp4` を `EXT-X-BYTERANGE` で参照する |
| `sprite.jpg` | 1 秒おきのサムネイル 60 枚を 1 枚にまとめたシート。1 コマ 160×90、横 10 × 縦 6、左上から時刻順 |
| `clip.1.mp4`、`clip.2.mp4` … | 区間の途中で映像形式(コーデックプライベートデータ)が変わったときの 2 つ目以降の部分。`clip.mp4` は 1 つ目の部分。`index.m3u8` は部分の頭で `EXT-X-DISCONTINUITY` と `EXT-X-MAP` を切り替える([codec.md](codec.md)) |

- `sprite.jpg` のコマ i(0〜59)は、枠 [区間の開始 + i 秒, + i + 1 秒) に入る **キーフレームの絵**(video_archiver が `clip.mp4` からデコードする。GetImages は使わない)。
  絵の時刻は秒ちょうどではなく、その秒の中のキーフレームの実際の時刻(キーフレームの位相は送信開始時刻で決まり、実測では枠の開始から 0.5〜1 秒後)
- 区間の頭の枠には、その区間に始まる断片のキーフレームが入る(前の区間の断片から続く部分は使わない)。キーフレームが 2 秒おきなら枠は 1 つおきに空く
- 録画がない秒(キーフレームがない枠)のコマは黒にする。録画の有無は DynamoDB の `segments` で区別できる
- 部分が 2 つ以上の区間は、部分ごとにキーフレームをデコードして 1 枚のシートにする

## DynamoDB: `{p}-state`

PK `camera_id`(S)、SK `sk`(S)。TTL 属性 `expires_at`(数値、epoch 秒)。

**処理済み位置**: SK `CURSOR#VIDEO` / `CURSOR#DETECT`

| 属性 | 型 | 例 |
| --- | --- | --- |
| `processed_until` | S | `2026-10-24T10:25:00Z` |
| `lease_until` | S | `2026-10-24T10:27:00Z`。リースは `lease_until < 現在時刻` の条件付き更新で取る |
| `lease_owner` | S | Lambda のリクエスト ID |

カーソルが存在しないときは「現在時刻 − lag」を 1 分境界に切り捨てた時刻 − 5 分 から始める。
`processed_until` より前の区間はすべて確定済み(下の「区間を処理してよい条件」を満たしてから確定する)。

**区間**: SK = 区間の開始時刻 `2026-10-24T10:20:00Z`

動画の退避と物体検出は同じ件を **UpdateItem で自分の属性だけ** 書く(PutItem で上書きしない)。

| 属性 | 書く人 | 型 | 内容 |
| --- | --- | --- | --- |
| `video_status` | video_archiver | S | `DONE` / `EMPTY` / `FAILED` |
| `segments` | video_archiver | L | 録画がある範囲 `[[開始ms, 終了ms], ...]`(epoch ミリ秒の数値ペア)。断片は開始時刻の区間に入るので、最後の終わりは区間の終わりを断片 1 本ぶんまで越えることがある |
| `files` | video_archiver | M | `{"clip": key, "playlist": key, "sprite": key}`。部分が 2 つ以上なら `"clips": [key, ...]`(1 つ目は `clip` と同じ) |
| `hls` | video_archiver | M | `{"init_length": n, "parts": [[offset, length, duration_ms], ...]}`(1 つ目の部分)。API が複数区間のプレイリストを組むのに使う。部分が 2 つ以上なら `"sections": [{"clip": key, "init_length", "parts", "codec", "start_ms"}, ...]`(すべての部分。読む側は `sections` があればそちらを使う) |
| `codecs` | video_archiver | L | 部分ごとの映像形式 `["H265"]`、途中で切り替わった区間は `["H265", "H264"]`(init のサンプルエントリから。不明は `""`) |
| `fragment_count` / `bytes` | video_archiver | N | 実測値 |
| `sprite_frames` / `sprite_error` | video_archiver | N / S | スプライトに入ったコマ数(0〜60)、キーフレームのデコードに失敗した理由(あれば。動画は `DONE` で退避する) |
| `video_duration_ms` / `video_attempts` | video_archiver | N | 実行時間、試行回数 |
| `video_error` / `detect_error` | 各自(共通処理) | S | 最後の失敗の理由。成功したら消える |
| `detect_status` | detector | S | `DONE` / `EMPTY` / `FAILED` |
| `detect_count` / `frame_count` | detector | N | 検出数、推論したフレーム数 |
| `detect_duration_ms` / `detect_attempts` | detector | N | 実行時間、試行回数 |
| `expires_at` | 両方 | N | メディアの保存期限と同じ |

**共通処理のインターフェース**(`lambdas/common/`)

```python
from common import MinuteResult, run_pipeline   # zip の直下に common/ を同梱する

def run_pipeline(kind: Literal["VIDEO", "DETECT"], camera_id: str,
                 process_minute: Callable[[str, datetime, datetime], MinuteResult],
                 *, lag_seconds: int, max_minutes: int, max_attempts: int,
                 context=None, **testing_options) -> dict
```

`process_minute(camera_id, start, end)` は 1 分区間を処理し、`MinuteResult(status, attributes)` を返す(`attributes` は区間の件に UpdateItem する属性)。
`status` は `DONE` / `EMPTY`、再試行しても無駄なら `FAILED`。`context` には Lambda のコンテキストを渡す(残り時間での打ち切りとリースに使う)。
例外を投げたら失敗扱いで、構想.md の「1回の起動での流れ」どおりに再試行・`FAILED` 確定する。
`common.NotReady` を投げたら「まだ処理できない」扱い(試行回数に数えず、カーソルはその手前で止まる)。

**区間を処理してよい条件(ウォーターマーク)**: 共通処理が判定し、process_minute は呼ばれた区間の断片が出そろっている前提でよい。
区間 [s, e) は (a) ストリームのウォーターマーク(届いている最新の断片の ProducerTimestamp + 長さ)≥ e、または
(b) 現在 − e ≥ `MAX_LATE_SECONDS`(環境変数、既定 720 秒 = エッジのバッファ上限 10 分 + 余裕)のどちらかで処理する。
満たさなければ止めて次の起動を待つ(試行回数に数えない)。前提はエッジ(kvssink)が断片を時刻順に送り、回線断のあとは未 ACK の分を古い順に送り直すこと。
区間は 1 回だけ処理し、再処理はしない(「処理済み位置より前はすべて確定」は維持)。構想.md の「1回の起動での流れ」との差は、2. と 3. の間にこの判定が入ること。
- 限界: `MAX_LATE_SECONDS` より後に届いた断片は捨てる(エッジ側でも捨てられている量)。配信停止中やオフライン中は、処理済み位置が現在より約 `MAX_LATE_SECONDS` 遅れる(その範囲はビューワーが KVS から再生する)
- ListFragments は、処理する区間がある起動ごとに 1 回(カーソル 〜 現在)。結果を起動の間キャッシュし、`common.kvs.list_fragments` はそこから返す
- 再送による重複: 同じ ProducerTimestamp の断片が KVS に 2 つ入りうる。`common.kvs.list_fragments` が ProducerTimestamp で重複を除き、clip、`segments`、`fragment_count`、GetImages の時刻はその結果に揃える
- `common.kvs.minute_ready` は廃止(互換のため常に `(True, "watermark")` を返す。「何も届いていない = 送信が止まった」とみなして進むため、回線断のあとの断片を取りこぼした)
**物体検出は動画の退避を待つ(上流)**: 物体検出は動画の退避が置いた `clip.mp4` を読むので、`run_pipeline(..., upstream_kind="VIDEO")` で動かす。
- 区間 [s, e) は `CURSOR#VIDEO` の `processed_until` ≥ e のときだけ処理する(**物体検出の処理済み位置は動画の退避の処理済み位置を越えない**)。
  満たさなければ止めて次の起動を待つ(`stopped = waiting_upstream`、試行回数に数えない)。このとき物体検出は自分のウォーターマークを見ない(動画の退避が判定済み)
- 区間の件の `video_status` で分岐する: `DONE` → `clip.mp4`(`hls.sections` があれば部分ごとの clip)からキーフレームをデコード、`EMPTY` → 検出なし(`detect_status = EMPTY`)、
  `FAILED` → 予備の経路(KVS の GetImages)、件がない → `NotReady`
- 動画の退避が区間の終わりから `UPSTREAM_MAX_WAIT_SECONDS`(環境変数、既定 1800 秒)を過ぎても進まないときだけ、上流を待たずに
  自分のウォーターマークで判定し、予備の経路で処理する(KVS の保持 1 時間の内に)
- 物体検出は動画の退避より 0〜1 分遅れて確定する

`{kind}_attempts` は処理の前に 1 足す(タイムアウトで落ちても数える)。失敗の理由は `{kind}_error`(S、`video_error` / `detect_error`)に残し、成功したら消す。
詳細は [lambdas/common/README.md](../lambdas/common/README.md)。
Scheduler からの入力は `{"camera_id": "cam-001"}`。カメラごとに 1 本のスケジュールを作る。

## 検出結果: Parquet と Glue

- キー: `s3://{p}-detections-{account_id}/detections/dt=2026-10-24/cam-001.parquet`
- Glue データベース `{p_underscored}`(例 `cloud_camera`)、テーブル `detections`。パーティション `dt`(string)はパーティションプロジェクションで定義(Terraform で作る)
- 列(1 行 = 1 検出):

| 列 | 型 | 内容 |
| --- | --- | --- |
| `camera_id` | string | |
| `ts` | timestamp(ミリ秒、UTC) | フレームの時刻(ProducerTimestamp)。`clip.mp4` のキーフレームの実際の時刻で、1 秒の枠に最大 1 枚(秒ちょうどではない。`sprite.jpg` の同じ枠のコマと同じフレーム)。予備の経路(GetImages)で取った区間だけは秒ちょうど |
| `label` | string | COCO のクラス名(英語、例 `person`) |
| `confidence` | float | 0〜1 |
| `x` / `y` / `w` / `h` | float | 0〜1 に正規化したバウンディングボックス(左上原点) |
| `model` | string | 例 `yolox_nano` |

## IoT Core(MQTT)

- Thing `{p}-{gateway_id}`、証明書は Terraform で発行。KVS への認証は IoT の認証情報プロバイダー(ロールエイリアス `{p}-kvs-role-alias`)を使う
- **クラシックシャドウ**でカメラ設定を配る:

```json
{"state": {"desired": {"cameras": {"cam-001": {
  "onvif_url": "http://emulator:8080/onvif/device_service",
  "username": "admin", "password": "admin",
  "profile_token": null,
  "prefer_codec": null,
  "codec": null,
  "streaming": true
}}}}}
```

`reported` はエッジが実際の状態(`streaming`、`rtsp_url`、`codec`(`H265` / `H264`)、`codec_error`、`resolution`、`error`)を返す。`profile_token` が null なら `prefer_codec`(`H265` / `H264`。null ならエッジの `CC_PREFER_CODEC`、既定 `H265`)のプロファイルを自動選択し、なければもう一方のコーデックを使う(再エンコードしない)。
desired の `codec`(`H265` / `H264`、ビューワーの「映像形式」)は、ONVIF のカメラなら **カメラのエンコーダーをその形式に切り替える**:
対象のプロファイル(`profile_token`、なければ最大解像度)の Encoding を Media2 の SetVideoEncoderConfiguration で変え、
パイプラインを組み直す(配信は数秒途切れる)。変えられなければその形式のプロファイルを選び、それもなければもう一方のまま送り、
理由を `reported.codec_error` に書く。`codec` があれば `prefer_codec` より優先する。
desired に `rtsp_url`(と `codec`)を書くと ONVIF を使わずにその URL を直接使う(検証用。このときの `codec` は RTSP の形式の申告)。
シャドウは null を書くとキーを消すので、`error` などが null のときはキーがない(読む側は「ない = null」として扱う)。

`reported` の最上位には次も入れる(ビューワーのオンライン表示用):

| キー | 内容 |
| --- | --- |
| `online` | bool。接続時と status 送信(30 秒ごと)で `true`、正常終了時に `false` |
| `last_seen` | ISO 8601 UTC。30 秒ごとに更新。異常終了は API 側で `now - last_seen > 90 秒` ならオフライン扱い |
| `gateway` | `{"gateway_id", "sink"}`(`sink` は `kvs` / `fake` / `file`) |

MQTT のクライアント ID は Thing 名。LWT のトピックがポリシーで許可されていないと、IoT Core は CONNECT 自体を拒否する。

- トピック(`{gw}` はゲートウェイ ID):

| トピック | 向き | 内容 |
| --- | --- | --- |
| `{p}/{gw}/status` | エッジ → クラウド | 30 秒ごとの死活。`{"ts", "online": true, "gateway_id", "cameras": {"cam-001": {"streaming", "uptime_s", "bytes_sent", "restarts", "gop_s", "persist_latency_ms", "error", "buffer", "last_exit"}}}`(`buffer` は下記)。LWT(異常切断と正常終了の両方)で `{"online": false, "gateway_id"}` |
| `{p}/{gw}/discovery` | エッジ → クラウド | WS-Discovery の結果 `{"ts", "gateway_id", "request_id"?, "devices": [{"urn", "types", "scopes", "xaddrs", "from"}]}`。`probe` の結果は `{"probe": {"onvif_url", "selected" or "error"}}` |
| `{p}/{gw}/cmd` | クラウド → エッジ | 単発コマンド。`{"action": "discover"}`、`{"action": "probe", "onvif_url", "username", "password"}`、`{"action": "restart", "camera_id"?}`、`{"action": "status"}`、`{"action": "reload"}`(シャドウを取り直す)。`request_id` を付けると応答に入る |

status の `cameras.*.buffer`(回線断のバッファ。`sink=kvs` のときだけ、それ以外は null。値は kvssink のキーフレームごとの計測値):

| キー | 内容 |
| --- | --- |
| `unsent_s` / `unsent_bytes` | まだ KVS に送っていない映像の長さ(秒)とバイト数。回線断の間は増え続け、復旧後に 0 に戻る |
| `buffered_s` / `buffered_bytes` | バッファ全体(送ったが永続化の ACK がまだのぶんを含む) |
| `store_used_bytes` | kvssink のメモリ(コンテンツストア)の使用量 |
| `limit_s` / `limit_bytes` | バッファの上限(既定 660 秒 / 160MB)。どちらかに達すると古い方から捨てる |
| `persisted_lag_s` | 最後に永続化された断片のプロデューサー時刻から今までの秒数(クラウドから見た遅れ。平常時 2 秒前後) |
| `dropped_frames` | バッファがあふれて送らずに捨てたフレームの累計(パイプラインの再起動をまたいで加算) |
| `lost_s` | 回線断がバッファの上限を超えて失った映像の秒数の累計(最後に永続化された時刻からバッファの最古までの差。再起動をまたいで加算) |
| `kvs_errors` | KVS への送信エラーの累計(回線断など。これではパイプラインを再起動しない) |
| `sessions` / `stale_events` / `error_acks` | PutMedia の接続回数、ACK が途絶えて接続を張り直そうとした回数、エラー ACK の数(= KVS が受け付けなかった断片の数)(今の子プロセスの中での累計) |

`last_exit` は直近のパイプライン再起動の理由 `{"code", "ts", "reason"}`(なければ null)。

**回線断のときのエッジの約束**: 回線断(KVS・IoT Core に届かない)の間も、エッジはパイプラインを止めずに映像を kvssink のメモリに溜め、
復旧後に元のプロデューサー時刻のまま送り直す。既定で 660 秒(10 分 + 再接続の余裕、`limit_s`)まで吸収し、それより長い断は古い方から欠落する。
- 送り直しは古い順(プロデューサー時刻の昇順)。遮断試験(1・5・10・15 分、5 分 → 2 分 → 5 分)で順序の逆転 0、重複 0
- 断片 1 本の遅れ(サーバー時刻 − プロデューサー時刻)の最大は「断の長さ(上限 660 秒)+ 十数秒」。実測 10 分の断で 613 秒、15 分の断で 659 秒。
  クラウドの `MAX_LATE_SECONDS`(720 秒)はこれを前提にしている。エッジの上限を伸ばすときは両方を合わせる
- 溜まったぶんは実時間の約 4.5 倍の速さで追いつく(10 分の断 → 復旧から 133 秒で送り終わる)
- 1 分を超える断で、断片 1 本(1 秒)を KVS がエラー ACK で受け付けないことがある(SDK の再送の挙動。status の `error_acks`)
- 断の最中に IoT の認証情報の取り直しが重なると kvssink が止まりバッファが消える。ロールエイリアスの `credential_duration` を 43200 秒にして起きにくくする

実測と設定の理由は [edge/README.md](../edge/README.md) の「回線断とバッファ」。

## HTTP API(API Gateway HTTP API、パス接頭辞 `/api`)

ビューワーと同じ CloudFront ドメインから `/api/*` で呼ぶ(CORS 不要)。メディアは `/media/*`(メディアバケットのキーに対応)。

| メソッドとパス | 内容 |
| --- | --- |
| `GET /api/cameras` | カメラ一覧と状態(シャドウの reported、処理済み位置) |
| `PUT /api/cameras/{id}/streaming` | `{"streaming": bool}` でシャドウの desired を更新 |
| `PUT /api/cameras/{id}/codec` | `{"codec": "H265" \| "H264"}`(`H.265`、`HEVC` なども受ける)でシャドウの desired の `codec` を更新。`{"camera_id", "codec", "version"}` |
| `GET /api/cameras/{id}/timeline?from=&to=` | 録画範囲。レスポンス下記 |
| `GET /api/cameras/{id}/vod.m3u8?from=&to=` | S3 退避済み区間を連結した HLS プレイリスト(区間ごとに `EXT-X-DISCONTINUITY`、`EXT-X-MAP`、`EXT-X-PROGRAM-DATE-TIME`)。セグメントの URI は `/media/...`。区間の途中で映像形式が変わった区間は部分(`hls.sections`)ごとに同じ 3 つを入れる。メディアプレイリストなので `CODECS` 属性はなく、プレーヤーは各 `EXT-X-MAP` の init から形式を読む(H.265 と H.264 が混ざってよい) |
| `GET /api/cameras/{id}/kvs-hls?mode=LIVE\|ON_DEMAND&from=&to=` | KVS の HLS セッション URL(`{"url", "expires_at"}`)。fMP4 を指定 |
| `POST /api/search` | `{"question": "..."}` → `{"answer": "...", "hits": [{"camera_id", "ts", "label", "confidence"}], "sql": "...", "usage": {...}}` |

ビューワーが cameras の `reported` / `desired` から読む項目: `streaming`、`codec`、`codec_error`、`resolution`、`error`(`reported`)、`streaming`、`codec`(`desired`)、`name`(どちらか。なければ camera_id を表示)。
`desired.codec` と `reported.codec` が違う間は「切り替え中」と表示する。
cameras の各項目の `online` はゲートウェイの死活(bool か null)。シャドウの `reported.online` が true でも `last_seen` が 90 秒より古ければ false、シャドウに情報がなければ null(ビューワーは「不明」として表示しない)。

timeline のレスポンス:

```json
{
  "camera_id": "cam-001",
  "from": "...", "to": "...",
  "video_processed_until": "2026-10-24T10:25:00Z",
  "detect_processed_until": "2026-10-24T10:24:00Z",
  "recorded": [{"start": "...", "end": "...", "source": "s3" | "kvs"}],
  "failed": [{"start": "...", "end": "..."}],
  "minutes": [{"start": "...", "sprite": "/media/cam-001/.../sprite.jpg"}]
}
```

補足(実装で決めたこと。上の形に項目を足しただけで、既存の項目は変えていない):

- 時刻の引数は ISO 8601(`Z` かオフセット付き)か epoch ミリ秒。範囲の上限は timeline 48 時間(省略時は直近 1 時間)、vod.m3u8 3 時間、kvs-hls の ON_DEMAND 24 時間
- エラーはすべて `{"error": {"code": "...", "message": "..."}}`(例: `BAD_REQUEST`、`RANGE_TOO_LARGE`、`CAMERA_NOT_FOUND`、`NO_RECORDING`、`NO_FRAGMENTS`)
- `GET /api/cameras` → `{"cameras": [{"camera_id", "gateway_id", "stream_name", "reported", "desired"(password は伏せる), "reported_at", "video_processed_until", "detect_processed_until"}]}`
- `PUT .../streaming` → `{"camera_id", "streaming", "version"}`
- timeline に `now` を追加。`minutes[]` に `codecs`(区間の件の `codecs`。あれば)を追加
- kvs-hls に `mode` を追加
- search: `hits[]` に `ts_jst` を追加。`sql` は最後に実行した SQL(なければ null)。`queries`(実行した SQL ごとのスキャン量・エラー)を追加。
  `usage` は `{"input_tokens", "output_tokens", "total_tokens", "model_id", "bedrock_calls", "tool_calls", "athena_bytes_scanned", "elapsed_ms", ...}`
- search(検索の改善で追加。既存の項目は変えていない): `queries[]` に `tool`(`search_detections` / `query_detections`)、
  `input`(search_detections の引数)、`warnings`(SQL ガードの警告)を追加。`usage` に `lint_retries`(回答の書き直し回数)を追加。
  `hits[].ts` は search_detections の場合、イベントの最初のフレームの時刻

## ONVIF エミュレーター

- コンテナ 1 つで複数台を模擬できる(環境変数か設定ファイルでカメラ数を指定)
- ONVIF: WS-Discovery(UDP 3702)、Device、Media(Profile S)、Media2(H.265 は Media2 で返す)、PTZ(下の「PTZ」節)。認証は WS-Security UsernameToken(Digest)
- 既定のデバイスサービス URL: `http://<host>:8080/onvif/device_service`、既定の認証情報 `admin` / `admin`
  - カメラ n 台目は **ポートで分ける**: `http://<host>:{8080 + n - 1}/onvif/device_service`(cam1 = 8080、cam2 = 8081 …)。既定 2 台、カメラ名(ONVIF の name スコープと Hostname)は `cam-001` 形式
  - 認証なしで呼べるのは GetSystemDateAndTime、GetCapabilities、GetServices、GetServiceCapabilities、GetHostname、GetWsdlUrl、GetEndpointReference。誤った認証は SOAP Fault `env:Sender` / `ter:NotAuthorized`(HTTP 400)。PasswordText も受け付ける
- Media2: `GetProfiles` は **`Type=["All"]`(または `VideoEncoder`)を指定したときだけ** Configurations を返す(ONVIF の仕様どおり)。main(token `Profile_Main`)の `Configurations.VideoEncoder.Encoding` は既定 `"H265"`、カメラごとの設定(`main_codec`)で `"H264"`。sub(`Profile_Sub`)は常に `"H264"`。`SetVideoEncoderConfiguration` でビットレート、GovLength、フレームレート、Quality を変えられ、配信が数秒途切れて再起動する。
  main は Media2 の Encoding(`H265` / `H264`)も変えられる(実行中の切り替え。sub は H.264 固定、解像度は固定)。`GetVideoEncoderConfigurationOptions` は main に両方の Options を返す
- Media(ver10): Encoding に H265 がないため、H.265 の `Profile_Main` は VideoEncoderConfiguration を持たない(GetStreamUri は使える)。main を H.264 にしたカメラは main も持つ
- RTSP: `rtsp://<host>:8554/cam{n}/main`(既定 H.265。`main_codec` で H.264)、`.../sub`(H.264、低解像度)。キーフレーム間隔 1 秒(設定可)、VBR。RTSP の認証は既定でなし(`EMULATOR_RTSP_AUTH=true` でカメラの認証情報が必要)
- スナップショット: `http://<host>:{ONVIF ポート}/onvif/snapshot.jpg`(1 秒ごとに更新、認証なし)
- RTSP はフレームごとに一定間隔で送る(1 秒ごとのまとめ送りにしない)
- 映像ソース: 時刻を焼き込んだテストパターン(既定)か、ループ再生する動画ファイル。焼き込む時刻はフレーム生成時の UTC(ミリ秒)
- Docker のブリッジネットワークでは、同じネットワークのコンテナからだけ WS-Discovery のマルチキャストが届く(Docker for Mac のホストからは届かない)

## エッジ

- `docker compose`(リポジトリ直下の `compose.yaml`)でエミュレーターとエッジを一緒に起動して検証できる
- 証明書は `edge/.certs/`(gitignore 済み)をマウント。`scripts/` に Terraform の出力から証明書と設定を書き出すスクリプトを置く
- 再エンコードしない: `rtspsrc ! rtph265depay ! h265parse ! video/x-h265,stream-format=hvc1,alignment=au ! h265timestamper ! kvssink`
  - H.264 のカメラは `rtph264depay ! h264parse ! video/x-h264,stream-format=avc,alignment=au ! h264timestamper ! kvssink`(コーデックは ONVIF の Encoding、または desired の `codec`)。
    kvssink は caps から codec-id(`V_MPEG4/ISO/AVC` / `V_MPEGH/ISO/HEVC`)と content-type(`video/h264` / `video/h265`)を決める
  - kvssink は NAL 変換をしないため、コーデックプライベートデータは caps の `codec_data` で渡す(`stream-format=hvc1` が必須)
  - RTP から取り出したフレームには DTS がなく、kvssink は「VPS/SPS/PPS を含み DTS が無効」なキーフレームを捨てる。`h265timestamper` で DTS を付ける
- フラグメントはキーフレームごと(エミュレーターのキーフレーム間隔 1 秒 → 1 秒ごと)。ProducerTimestamp はゲートウェイの壁時計(NTP 前提)で、最初のフレームを受け取った時刻 + PTS の差分
- `FragmentLengthInMilliseconds` は「最後のフレームの開始 − 最初のフレームの開始」で、1 フレーム分(15fps なら 67ms)短い。`segments` をつなぐ許容値(500ms)には収まる

## PTZ(IoT Core 経由の制御とエミュレーターの疑似 PTZ)

ビューワー → API → IoT Core(cmd トピック)→ エッジ → ONVIF PTZ → エミュレーター、の順に流れる。
エミュレーターは光学 PTZ の代わりに、元映像から切り出す範囲(クロップ)を動かして疑似 PTZ にする(デジタル PTZ)。

### 座標(ONVIF の Generic 空間に合わせる)

| 値 | 範囲 | 意味 |
| --- | --- | --- |
| `pan` | -1〜1 | 切り出し窓の水平位置。-1 が左端、1 が右端 |
| `tilt` | -1〜1 | 切り出し窓の垂直位置。-1 が下端、1 が上端(ONVIF の向きに合わせる) |
| `zoom` | 0〜1 | 0 が全体(等倍)、1 が最大倍率(既定 4 倍、エミュレーターの設定) |

速度(ContinuousMove)は pan/tilt/zoom とも -1〜1。等倍のときは pan/tilt を動かしても見た目は変わらない。

### エミュレーター(ONVIF PTZ サービス)

- サービス URL `http://<host>:<port>/onvif/ptz_service`。GetServices / GetCapabilities に載せ、各プロファイル(main と sub)に同じ PTZConfiguration を付ける(main と sub は同じ切り出し窓)
- 操作: GetNodes、GetNode、GetConfigurations、GetConfiguration、GetConfigurationOptions、GetStatus、ContinuousMove(Timeout 対応)、RelativeMove、AbsoluteMove、Stop、GetPresets、SetPreset、RemovePreset、GotoPreset、GotoHomePosition、SetHomePosition
- 動きは速度上限付きで滑らかに(瞬間移動しない)。GetStatus の MoveStatus は `MOVING` / `IDLE`
- 元映像は出力より高い解像度で作り(または読み)、切り出してから出力解像度に拡大縮小する。PTZ の操作で配信(RTSP)を途切れさせない。解像度とコーデックのパラメータは変えない(KVS の GetClip などはコーデック情報が変わるとエラーになるため)
- 実装で決めた細部(エミュレーター):
  - 倍率 = 1 + zoom × (最大倍率 − 1)(線形)。窓の大きさは元映像の 1/倍率、窓の左端 = (元映像の幅 − 窓の幅) × (pan + 1) / 2、上端 = (元映像の高さ − 窓の高さ) × (1 − tilt) / 2
  - 速度 1(最大)の速さは既定で pan / tilt 0.5 単位/秒、zoom 0.5 単位/秒(カメラごとに設定可)。AbsoluteMove / RelativeMove / GotoPreset / GotoHomePosition の Speed は 0〜1(最大に対する割合、省略で 1)
  - 範囲外の位置・速度・差分は Fault にせず丸める。ContinuousMove で端に着いたら止まる(`IDLE`)。ContinuousMove で省略した軸は変えない
  - ContinuousMove の Timeout は 0.1〜600 秒に丸める。省略時は DefaultPTZTimeout(10 秒)
  - トークンはノード `PTZNode_1`、構成 `PTZConfig_1`。プリセットは最大 32 個、トークンは空いている最小の番号(`"1"`、`"2"` …)、名前の省略時は `Preset {token}`。同じ名前は `ter:PresetExist`、ないトークンは `ter:NoToken`。状態(位置・プリセット・ホーム)はメモリ上だけ
  - PTZ を無効にしたカメラ(`EMULATOR_CAM{n}_PTZ=false`)は GetServices / GetCapabilities に載せず、プロファイルに PTZConfiguration を付けない。PTZ の操作は `ter:ActionNotSupported` / `ter:PTZNotSupported`
  - 焼き込みの 4 行目に現在の pan / tilt / zoom(と倍率)を出す。時刻とカメラ名は切り出しのあとに描くので常に見える
  - 操作から RTSP の映像が変わるまで約 0.25 秒(受信側のデコードを含む。Mac の Docker で計測)

### MQTT

`{p}/{gw}/cmd` に次を送る(既存の cmd に `ptz` を追加):

```json
{"action": "ptz", "camera_id": "cam-001", "request_id": "uuid",
 "op": "continuous" | "stop" | "absolute" | "relative" | "goto_preset" | "set_preset" | "home",
 "pan": 0.5, "tilt": 0, "zoom": 0, "timeout_ms": 10000, "preset_token": "1", "preset_name": "玄関"}
```

- `continuous` は速度、`absolute` は位置、`relative` は差分(pan / tilt は -2〜2、zoom は -1〜1)。`timeout_ms` は continuous の自動停止(既定 10000、100〜60000。stop を取りこぼしても動き続けない)
- エッジは結果を `{p}/{gw}/ptz` に送る: `{"ts", "gateway_id", "camera_id", "request_id", "ok", "error"?, "position": {"pan", "tilt", "zoom"}, "move_status", "preset_token"?}`(`preset_token` は set_preset のとき)
- エッジはシャドウの `reported.cameras.{camera_id}.ptz` に `{"supported": bool, "pan", "tilt", "zoom", "presets": [{"token", "name"}], "move_status", "error", "updated_at"}` を書く。動いている間は 1 秒に 1 回まで、止まったら必ず 1 回。起動時(設定が変わったとき)にも 1 回
  - `error` は準備(ONVIF の接続)に失敗したときだけ(このとき `supported: false`)。値のないキーは null で書く(シャドウからキーが消える)

### HTTP API

| メソッドとパス | 内容 |
| --- | --- |
| `POST /api/cameras/{id}/ptz` | 本文は MQTT の `op` 以降と同じ形。cmd トピックに QoS 1 で publish し、`202 {"request_id"}` を返す |
| `GET /api/cameras/{id}/ptz` | シャドウの `reported.cameras.{id}.ptz` を返す。なければ `{"supported": false}` |
