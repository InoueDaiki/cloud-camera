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
| KVS ストリーム名 | `{p}-{camera_id}`。保持期間 1 時間 |
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

各 Lambda ディレクトリは `build.sh` を持ち、`build/<name>.zip` を出力する(Terraform はこの zip を参照する)。
依存は `pip install --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.13` 等で arm64 用を入れる。
`lambdas/common/` は各 zip に同梱する(レイヤーにしてもよい)。

## S3

| バケット | 用途 | 備考 |
| --- | --- | --- |
| `{p}-media-{account_id}` | 動画、スプライト、プレイリスト | CloudFront(OAC)経由で配信 |
| `{p}-detections-{account_id}` | 検出結果の Parquet | バージョニング有効、旧バージョンは 1 日後に削除 |
| `{p}-athena-{account_id}` | Athena のクエリ結果 | 7 日で削除 |
| `{p}-viewer-{account_id}` | ビューワーの静的ファイル | CloudFront(OAC)経由で配信 |

### メディアのキー(1 分区間ごと)

`{camera_id}/{YYYY}/{MM}/{DD}/{HH}/{mm}/` の下に置く。例: `cam-001/2026/10/24/10/20/`

| ファイル | 中身 |
| --- | --- |
| `clip.mp4` | その 1 分の動画。単体で再生できる fragmented MP4(init + 断片を連結したもの)が第一候補 |
| `index.m3u8` | その 1 分の HLS(fMP4)プレイリスト。`clip.mp4` を `EXT-X-BYTERANGE` で参照する |
| `sprite.jpg` | 1 秒おきのサムネイル 60 枚を 1 枚にまとめたシート。1 コマ 160×90、横 10 × 縦 6、左上から時刻順 |

録画がない秒のコマは黒(または前のコマの複製)にする。どちらにしたかは DynamoDB の `segments` で区別できる。

## DynamoDB: `{p}-state`

PK `camera_id`(S)、SK `sk`(S)。TTL 属性 `expires_at`(数値、epoch 秒)。

**処理済み位置**: SK `CURSOR#VIDEO` / `CURSOR#DETECT`

| 属性 | 型 | 例 |
| --- | --- | --- |
| `processed_until` | S | `2026-10-24T10:25:00Z` |
| `lease_until` | S | `2026-10-24T10:27:00Z`。リースは `lease_until < 現在時刻` の条件付き更新で取る |
| `lease_owner` | S | Lambda のリクエスト ID |

カーソルが存在しないときは「現在時刻 − lag」を 1 分境界に切り捨てた時刻 − 5 分 から始める。

**区間**: SK = 区間の開始時刻 `2026-10-24T10:20:00Z`

動画の退避と物体検出は同じ件を **UpdateItem で自分の属性だけ** 書く(PutItem で上書きしない)。

| 属性 | 書く人 | 型 | 内容 |
| --- | --- | --- | --- |
| `video_status` | video_archiver | S | `DONE` / `EMPTY` / `FAILED` |
| `segments` | video_archiver | L | 録画がある範囲 `[[開始ms, 終了ms], ...]`(epoch ミリ秒の数値ペア) |
| `files` | video_archiver | M | `{"clip": key, "playlist": key, "sprite": key}` |
| `hls` | video_archiver | M | `{"init_length": n, "parts": [[offset, length, duration_ms], ...]}`。API が複数区間のプレイリストを組むのに使う |
| `fragment_count` / `bytes` | video_archiver | N | 実測値 |
| `video_duration_ms` / `video_attempts` | video_archiver | N | 実行時間、試行回数 |
| `detect_status` | detector | S | `DONE` / `EMPTY` / `FAILED` |
| `detect_count` / `frame_count` | detector | N | 検出数、推論したフレーム数 |
| `detect_duration_ms` / `detect_attempts` | detector | N | 実行時間、試行回数 |
| `expires_at` | 両方 | N | メディアの保存期限と同じ |

**共通処理のインターフェース**(`lambdas/common/`)

```python
def run_pipeline(kind: Literal["VIDEO", "DETECT"], camera_id: str,
                 process_minute: Callable[[str, datetime, datetime], MinuteResult],
                 *, lag_seconds: int, max_minutes: int, max_attempts: int) -> dict
```

`process_minute(camera_id, start, end)` は 1 分区間を処理し、`MinuteResult(status, attributes)` を返す(`attributes` は区間の件に UpdateItem する属性)。
例外を投げたら失敗扱いで、構想.md の「1回の起動での流れ」どおりに再試行・`FAILED` 確定する。
Scheduler からの入力は `{"camera_id": "cam-001"}`。カメラごとに 1 本のスケジュールを作る。

## 検出結果: Parquet と Glue

- キー: `s3://{p}-detections-{account_id}/detections/dt=2026-10-24/cam-001.parquet`
- Glue データベース `{p_underscored}`(例 `cloud_camera`)、テーブル `detections`。パーティション `dt`(string)はパーティションプロジェクションで定義(Terraform で作る)
- 列(1 行 = 1 検出):

| 列 | 型 | 内容 |
| --- | --- | --- |
| `camera_id` | string | |
| `ts` | timestamp(ミリ秒、UTC) | フレームの時刻(ProducerTimestamp) |
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
  "streaming": true
}}}}}
```

`reported` はエッジが実際の状態(`streaming`、`rtsp_url`、`codec`、`resolution`、`error`)を返す。`profile_token` が null なら H.265 のプロファイルを自動選択する。

- トピック(`{gw}` はゲートウェイ ID):

| トピック | 向き | 内容 |
| --- | --- | --- |
| `{p}/{gw}/status` | エッジ → クラウド | 30 秒ごとの死活。`{"ts", "cameras": {"cam-001": {"streaming", "uptime_s", "bytes_sent"}}}`。LWT で `{"online": false}` |
| `{p}/{gw}/discovery` | エッジ → クラウド | WS-Discovery の結果 |
| `{p}/{gw}/cmd` | クラウド → エッジ | `{"action": "discover"}` などの単発コマンド |

## HTTP API(API Gateway HTTP API、パス接頭辞 `/api`)

ビューワーと同じ CloudFront ドメインから `/api/*` で呼ぶ(CORS 不要)。メディアは `/media/*`(メディアバケットのキーに対応)。

| メソッドとパス | 内容 |
| --- | --- |
| `GET /api/cameras` | カメラ一覧と状態(シャドウの reported、処理済み位置) |
| `PUT /api/cameras/{id}/streaming` | `{"streaming": bool}` でシャドウの desired を更新 |
| `GET /api/cameras/{id}/timeline?from=&to=` | 録画範囲。レスポンス下記 |
| `GET /api/cameras/{id}/vod.m3u8?from=&to=` | S3 退避済み区間を連結した HLS プレイリスト(区間ごとに `EXT-X-DISCONTINUITY`、`EXT-X-MAP`、`EXT-X-PROGRAM-DATE-TIME`)。セグメントの URI は `/media/...` |
| `GET /api/cameras/{id}/kvs-hls?mode=LIVE\|ON_DEMAND&from=&to=` | KVS の HLS セッション URL(`{"url", "expires_at"}`)。fMP4 を指定 |
| `POST /api/search` | `{"question": "..."}` → `{"answer": "...", "hits": [{"camera_id", "ts", "label", "confidence"}], "sql": "...", "usage": {...}}` |

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

## ONVIF エミュレーター

- コンテナ 1 つで複数台を模擬できる(環境変数か設定ファイルでカメラ数を指定)
- ONVIF: WS-Discovery(UDP 3702)、Device、Media(Profile S)、Media2(H.265 は Media2 で返す)。認証は WS-Security UsernameToken(Digest)
- 既定のデバイスサービス URL: `http://<host>:8080/onvif/device_service`、既定の認証情報 `admin` / `admin`
- RTSP: `rtsp://<host>:8554/cam{n}/main`(H.265)、`.../sub`(H.264、低解像度)。キーフレーム間隔 1 秒(設定可)、VBR
- 映像ソース: 時刻を焼き込んだテストパターン(既定)か、ループ再生する動画ファイル

## エッジ

- `docker compose`(リポジトリ直下の `compose.yaml`)でエミュレーターとエッジを一緒に起動して検証できる
- 証明書は `edge/.certs/`(gitignore 済み)をマウント。`scripts/` に Terraform の出力から証明書と設定を書き出すスクリプトを置く
- 再エンコードしない: `rtspsrc ! rtph265depay ! h265parse ! kvssink`
