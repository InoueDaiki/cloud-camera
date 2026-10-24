# lambdas/api: ビューワー向け HTTP API と自然言語検索

API Gateway HTTP API(ペイロード 2.0)の後ろに置く 1 本の Lambda。パスでルーティングする。
API の形は [docs/contracts.md](../../docs/contracts.md) の「HTTP API」、背景は [構想.md](../../構想.md) の「再生」「自然言語検索」。

| ファイル | 中身 |
| --- | --- |
| `handler.py` | 入口(`handler.lambda_handler`)とルーティング |
| `app/cameras.py` | `GET /api/cameras`、`PUT /api/cameras/{id}/streaming`(IoT のクラシックシャドウ) |
| `app/timeline.py` | `GET /api/cameras/{id}/timeline`(処理済み位置の前は DynamoDB、後は KVS ListFragments) |
| `app/playlist.py` | `GET /api/cameras/{id}/vod.m3u8`(区間の `hls` から複数区間の HLS を組む) |
| `app/kvs.py` | `GET /api/cameras/{id}/kvs-hls`、ListFragments |
| `app/ptz.py` | `POST /api/cameras/{id}/ptz`(IoT Core の cmd トピックへ publish)、`GET /api/cameras/{id}/ptz`(シャドウ) |
| `app/search.py` / `prompt.py` / `sql_guard.py` / `athena.py` | `POST /api/search`(Bedrock Converse のツール使用 → SQL ガード → Athena) |
| `local_server.py` | ローカル開発用サーバー |
| `scripts/e2e_search.py` | 検索の end-to-end 検証(一時リソースを作って消す) |

## 開発

```sh
cd lambdas/api
mise exec -- uv sync
mise exec -- uv run pytest -q          # 単体テスト(moto とスタブ。AWS には繋がない)
mise exec -- ./build.sh                # ../../build/api.zip を作る(sqlglot だけ同梱、boto3 はランタイムのもの)
```

## 環境変数

| 変数 | 例 | 用途 |
| --- | --- | --- |
| `PROJECT` | `cloud-camera` | 既定値の組み立てに使う |
| `STATE_TABLE` | `cloud-camera-state` | 処理済み位置と区間の件 |
| `MEDIA_BUCKET` | `cloud-camera-media-123456789012` | ローカルサーバーの `/media/*` |
| `STREAM_NAME_PREFIX` | `cloud-camera-` | KVS ストリーム名 = 接頭辞 + カメラ ID |
| `GLUE_DATABASE` / `GLUE_TABLE` | `cloud_camera` / `detections` | 検索対象 |
| `ATHENA_WORKGROUP` | `cloud-camera` | 結果の出力先とスキャン上限はワークグループ側で強制する |
| `BEDROCK_MODEL_ID` | `jp.amazon.nova-2-lite-v1:0` | 未設定ならこの値 |
| `IOT_DATA_ENDPOINT` | `xxxx-ats.iot.ap-northeast-1.amazonaws.com` | `aws iot describe-endpoint --endpoint-type iot:Data-ATS` |
| `IOT_THING_PREFIX` | `cloud-camera-` | Thing 名 = 接頭辞 + ゲートウェイ ID |
| `CAMERAS` | `{"cam-001":"gw-001"}` | カメラ ID → ゲートウェイ ID |
| `MEDIA_URL_PREFIX`(任意) | `/media/` | プレイリストやスプライトの URL の接頭辞 |
| `SEARCH_TIMEOUT_SECONDS`(任意) | `25` | 検索全体の時間の予算(API Gateway の 30 秒より手前で答える) |

Lambda のタイムアウトは 30 秒、メモリは 256 MB で足りる(検索 1 回で 3〜6 秒、実測)。

**IAM(Terraform 担当向け)**: `dynamodb:Query`/`BatchGetItem`(state テーブル)、`iot:GetThingShadow`/`UpdateThingShadow`、`iot:Publish`(`{p}/*/cmd` トピックだけ。PTZ)、
`kinesisvideo:GetDataEndpoint`/`ListFragments`/`GetHLSStreamingSessionURL`、
`athena:StartQueryExecution`/`GetQueryExecution`/`GetQueryResults`/`StopQueryExecution`(ワークグループ)、
`glue:GetDatabase`/`GetTable`/`GetPartitions`、検出結果バケットの `s3:GetObject`/`ListBucket`、
Athena 結果バケットの `s3:GetObject`/`PutObject`/`ListBucket`/`GetBucketLocation`、
`bedrock:InvokeModel`(推論プロファイル `jp.*` と、その先の各リージョンの foundation-model)。

## ローカル開発用サーバー

ハンドラーをそのまま HTTP で動かす。実 AWS(`cloud-camera` プロファイル)に向けて使う。

```sh
cd lambdas/api
export STATE_TABLE=cloud-camera-state MEDIA_BUCKET=cloud-camera-media-<account> \
  GLUE_DATABASE=cloud_camera ATHENA_WORKGROUP=cloud-camera \
  IOT_DATA_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text) \
  CAMERAS='{"cam-001":"gw-001"}'
mise exec -- uv run python local_server.py --port 8787
```

- `/api/*` は Lambda と同じ処理
- `/media/*` は `MEDIA_BUCKET` の同じキーを S3 から返す(Range 対応)。vod.m3u8 のセグメント URI は `/media/...` なので、
  CloudFront がなくても hls.js で再生を試せる
- Vite からは `server.proxy` で `/api` と `/media` を `http://127.0.0.1:8787` に向ける

## エラーの形

すべて `{"error": {"code": "...", "message": "..."}}`。主なコード:
`BAD_REQUEST`(400)、`RANGE_TOO_LARGE`(400)、`NOT_FOUND` / `CAMERA_NOT_FOUND` / `NO_RECORDING` / `NO_FRAGMENTS`(404)、
`METHOD_NOT_ALLOWED`(405)、`BEDROCK_*`(429/502)、`INTERNAL`(500)。

## 各 API の補足

- **timeline**: `from`/`to` は ISO 8601(`Z` かオフセット付き)か epoch ミリ秒。省略時は直近 1 時間、最大 48 時間。
  DynamoDB は必要な属性だけ(`video_status`、`segments`、`files.sprite`)を取り、ページ送りする。
  分をまたいでつながっている `segments` は 1 つの範囲にまとめる(隙間 1 秒まで)。KVS の断片は隙間 500 ms までつなげる。
  `segments` のない `DONE` は 1 分まるごと録画ありとみなす
- **vod.m3u8**: `from`/`to` 必須、最大 3 時間。範囲にかかる 1 分区間のうち `video_status=DONE` で `hls` があるものを連結する。
  区間ごとに `EXT-X-DISCONTINUITY`(先頭以外)、`EXT-X-MAP`(`BYTERANGE` で init 部分)、`EXT-X-PROGRAM-DATE-TIME`
  (`segments` の最初の開始、なければ区間の開始)。`EXT-X-VERSION:7`、`TARGETDURATION` は断片の最大長の切り上げ
- **kvs-hls**: `FRAGMENTED_MP4`、`PRODUCER_TIMESTAMP`、`DisplayFragmentTimestamp=ALWAYS`(PDT 付き)、有効期限 1 時間。
  呼ぶたびに新しいセッション URL を発行する(共有するとスロットリングされ得るため)。ON_DEMAND は最大 24 時間

- **ptz**(POST): 本文は MQTT の cmd の `op` 以降と同じ形(docs/contracts.md の「PTZ」)。値の範囲を検証し(範囲外は 400。
  continuous の速度と absolute / relative の pan・tilt は -1〜1(relative の pan・tilt は -2〜2)、absolute の zoom は 0〜1、
  `timeout_ms` は 100〜60000、既定 10000)、知らない項目は捨てる。`request_id`(UUID)を付けて `{p}/{gw}/cmd` に QoS 1 で
  publish し、エッジの処理を待たずに `202 {"request_id"}` を返す。処理の結果はエッジが `{p}/{gw}/ptz` とシャドウに書く
- **ptz**(GET): シャドウの `reported.cameras.{id}.ptz` をそのまま返す。なければ `{"supported": false}`

## 自然言語検索

1. システムプロンプトに、テーブルのスキーマ、COCO の 80 ラベル、現在時刻(日本時間と UTC)、日本時間の日 → `dt` の早見表、
   「ユーザーの時刻表現は日本時間」を書く。ts の条件は `TIMESTAMP '2026-10-24 06:00:00 Asia/Tokyo'` で書かせる
   (モデルに UTC の換算をさせると間違えやすかったため)
2. ツールは `query_detections`(SQL を受けて Athena で実行)と `submit_answer`(回答と hits)。`toolChoice` で毎回どちらかを呼ばせ、
   検索の上限(4 回)か時間の予算に達したら `submit_answer` を強制する
3. SQL ガード(`sql_guard.py`、sqlglot で構文解析): 1 文だけ、SELECT(集合演算可)のみ、テーブルは `detections` だけ
   (WITH の別名は可、別データベースやテーブル関数は不可)、`detections` を読む SELECT には WHERE の AND 条件で `dt` の上下限が必須
   (31 日まで)、いちばん外側の LIMIT を 100 以下に強制(なければ付ける)。拒否した理由はモデルに返して書き直させる。
   最後の砦は Athena ワークグループの `BytesScannedCutoffPerQuery`
4. 結果の timestamp 列は ISO(UTC)に直し、日本時間の列(`*_jst`)を足してモデルに返す
5. `hits` は検索結果に実在した時刻だけ残す(モデルが作った時刻は捨てる)。`ts`(UTC)と `ts_jst` を返す
6. `usage` に入出力トークン、Bedrock の呼び出し回数、ツールの呼び出し回数、Athena のスキャン量、経過時間

### モデルの選び方

東京で使える推論プロファイル(`aws bedrock list-inference-profiles`)のうち、データが国内に留まる `jp.` を候補にした。
2026-10-24 時点の検証結果:

| モデル | 呼べたか | 単価(入力 / 出力、100 万トークン) | 検索の品質(ダミーデータ 4 問) |
| --- | --- | --- | --- |
| `jp.amazon.nova-2-lite-v1:0` | 呼べる | $0.396 / $3.311(Pricing API の東京の値) | 3 問正解。集計の質問で「今日」の ts 条件を省き、検出数を人数と書くことがある |
| `jp.anthropic.claude-haiku-4-5-20251001-v1:0` | 未申請で不可(*) | $1 / $5 前後(リージョン内プロファイルは割増あり、未確認) | 未検証 |
| `jp.anthropic.claude-sonnet-4-6` | 未申請で不可(*) | $3 / $15 前後(同上) | 未検証 |
| `jp.anthropic.claude-haiku-5-5` | このアカウントでは提供なし | | |

(*) `Model use case details have not been submitted for this account`。Anthropic のユースケースのフォームを
Bedrock コンソールから提出すると使えるようになる。

**既定値は `jp.amazon.nova-2-lite-v1:0`**: いま呼べるもののうち最安で、1 問あたり約 6,000 入力 / 300 出力トークン(約 $0.003)。
ユースケースの申請後は、集計の質問の正確さを上げたいなら `jp.anthropic.claude-haiku-4-5-20251001-v1:0` に切り替えて
`scripts/e2e_search.py run --model ...` で比べる(1 問約 $0.008 の見込み)。

### end-to-end 検証

```sh
cd lambdas/api
mise exec -- uv run python scripts/e2e_search.py setup      # cc-tmp-api-<account> バケット、cc_tmp_api_e2e、cc-tmp-api-wg
mise exec -- uv run python scripts/e2e_search.py run [--model ...] [--question ...]
mise exec -- uv run python scripts/e2e_search.py teardown   # 必ず実行
mise exec -- uv run python scripts/e2e_search.py check      # remaining: none を確認
```

## 既知の制約

- タイムラインの `minutes` は `DONE` の区間だけ。`EMPTY` の区間にスプライトはない
- vod.m3u8 は 1 分区間単位で入れる(`from`/`to` の途中から切らない)。シークは PDT で合わせる
- 検索の `hits` は代表時刻のみ(モデルが選ぶ)。分単位に集計した場合、`label` が空になることがある
- 検索は同期処理。Bedrock と Athena が遅いと API Gateway の 30 秒に近づく(予算 25 秒で打ち切って回答させる)
