# infra(Terraform)

クラウド側のリソースをまとめて作る。リソース名、バケット、テーブル、トピックなどの約束事は [docs/contracts.md](../docs/contracts.md)。

## 前提

- `mise trust && mise install`(terraform、tflint、AWS の `cloud-camera` プロファイル / `ap-northeast-1` が設定される)
- Lambda の zip(`build/video_archiver.zip`、`build/detector.zip`、`build/api.zip`)。`scripts/build-all.sh` で作る
- state はローカル(`infra/terraform.tfstate.d/<project>/`、gitignore 済み)。**証明書の秘密鍵が state に入る**ので扱いに注意する

## デプロイと削除

```sh
# ビルド → apply → viewer/dist を S3 に同期 → CloudFront を無効化
scripts/deploy.sh                       # project = cloud-camera
scripts/deploy.sh cc-tmp-foo            # 別名で一時環境を作る
PROJECT=cc-tmp-foo SKIP_BUILD=1 AUTO_APPROVE=1 scripts/deploy.sh \
  -var 'gateways={"gw-001"=["cam-001","cam-002"]}'

# スケジュールを消し、バケットを(旧バージョンも含めて)空にしてから destroy
scripts/destroy.sh cc-tmp-foo
```

スクリプトは Terraform のワークスペースを `project` と同じ名前にして state を分ける(`cloud-camera` と `cc-tmp-*` の state が混ざらない)。
`terraform` を直接使うときも `terraform workspace select -or-create <project>` してから `-var project=<project>` を付ける。

## 主な変数

| 変数 | 既定 | 内容 |
| --- | --- | --- |
| `project` | `cloud-camera` | 接頭辞 `{p}` |
| `gateways` | `{"gw-001" = ["cam-001"]}` | ゲートウェイ ID → カメラ ID のリスト |
| `cameras` | null(gateways から導く) | カメラ ID のリスト |
| `camera_codecs` | `{}` | カメラ ID → `H265` / `H264`。KVS ストリームの media type(`video/h265` / `video/h264`)。書いていないカメラは H265。変更はストリームの作り直しなしで反映される(UpdateStream)。エッジの `prefer_codec` とエミュレーターの `main_codec` も合わせる([docs/codec.md](../docs/codec.md)) |
| `kvs_retention_hours` | 1 | KVS の保持期間 |
| `media_retention_days` | 30 | メディアバケットの保存日数(Lambda の `MEDIA_RETENTION_DAYS`) |
| `lag_seconds` / `max_minutes` / `max_attempts` | 45 / 10 / 5 | Lambda の環境変数にそのまま渡す |
| `lambda_settings` | `{}` | 関数ごとの `memory_size` / `timeout` / `ephemeral_storage` の上書き。既定は `locals.tf`(video_archiver 512MB・300 秒、detector 1024MB・300 秒、api 512MB・30 秒) |
| `lambda_layers` | `{}` | 関数ごとのレイヤー ARN(例: `{detector = ["arn:...:AWSSDKPandas-Python313-Arm64:N"]}`) |
| `lambda_zip_dir` | null(リポジトリ直下の `build/`) | zip の置き場所 |
| `bedrock_model_id` | `jp.amazon.nova-2-lite-v1:0` | 推論プロファイル ID か基盤モデル ID |
| `camera_names` | `{}` | カメラ ID → 表示名。自然言語検索がカメラ名(「玄関」など)を ID に結びつけるのに使う。未設定のカメラはシャドウの `name` |
| `athena_bytes_scanned_cutoff` | 1 GiB | 1 クエリのスキャン量の上限 |
| `schedule_enabled` | true | false でスケジュールを止める |
| `log_retention_days` | 14 | ロググループの保持日数 |

## 作るもの

| ファイル | リソース |
| --- | --- |
| `kvs_iot.tf` | KVS ストリーム `{p}-{camera_id}`(保持 1 時間、`video/h265`。カメラごとに `camera_codecs` で `video/h264`)、IoT Thing `{p}-{gw}`、証明書(Terraform で発行・有効化)、ゲートウェイごとのポリシー(自分のクライアント ID、`{p}/{gw}/*`、自分のシャドウだけ)、KVS 用ロールとロールエイリアス `{p}-kvs-role-alias`(`{p}-*` のストリームへの PutMedia 等だけ) |
| `s3.tf` | media / detections / athena / viewer / artifacts の 5 バケット。全部パブリックアクセスをブロック、SSE-S3。media は保存日数で削除、detections はバージョニングと旧バージョン 1 日削除、現行版は media と同じ保存日数で削除、athena は 7 日削除。media と viewer は CloudFront(OAC)からの GetObject だけ許可 |
| `data.tf` | DynamoDB `{p}-state`(オンデマンド、TTL `expires_at`)、Glue `{p_underscored}.detections`(Parquet、`dt` のパーティションプロジェクション)、Athena ワークグループ `{p}` |
| `lambda.tf` | `{p}-video-archiver` / `{p}-detector` / `{p}-api`(Python 3.13、arm64、`handler.lambda_handler`)。zip は artifacts バケット経由でデプロイ(50MB 超でも可、展開後 250MB まで)。関数ごとの IAM ロール、ロググループ |
| `scheduler.tf` | スケジュールグループ `{p}`、カメラごとに `{p}-video-{cam}` / `{p}-detect-{cam}`(rate(1 minute)、入力 `{"camera_id": ...}`、再試行なし) |
| `webrtc.tf` | KVS WebRTC のシグナリングチャネル `{p}-{camera_id}`(カメラごと、`SINGLE_MASTER`、メッセージの保持 60 秒)。hashicorp/aws にリソースがないので **awscc プロバイダー**(Cloud Control API)の `awscc_kinesisvideo_signaling_channel`。タグ `Project` / `ManagedBy` は直接付ける。KVS 用ロール `{p}-kvs-producer` に master の権限(DescribeSignalingChannel、GetSignalingChannelEndpoint、GetIceServerConfig、ConnectAsMaster。チャネルの ARN だけ) |
| `api.tf` | HTTP API(`ANY /api/{proxy+}` → api Lambda、ペイロード 2.0)、アクセスログ |
| `cloudfront.tf` | ディストリビューション 1 つ。既定 = viewer バケット、`/api/*` = HTTP API(キャッシュ無効、Host 以外のヘッダー・クエリ・Cookie を転送)、`/media/*.m3u8` と `/media/*` = media バケット(`/media` を外す CloudFront Function、m3u8 はキャッシュ無効) |

### Lambda の環境変数(3 本共通)

`PROJECT`、`STATE_TABLE`、`MEDIA_BUCKET`、`DETECTIONS_BUCKET`、`STREAM_NAME_PREFIX`(`{p}-`)、`LAG_SECONDS`、`MAX_MINUTES`、`MAX_ATTEMPTS`、`MEDIA_RETENTION_DAYS`、`GLUE_DATABASE`、`GLUE_TABLE`、`ATHENA_WORKGROUP`、`BEDROCK_MODEL_ID`、`IOT_DATA_ENDPOINT`、`IOT_THING_PREFIX`(`{p}-`)、`WEBRTC_CHANNEL_PREFIX`(`{p}-`)、`CAMERAS`(JSON: camera_id → gateway_id)

### 権限の要点

- video_archiver: KVS の ListFragments / GetClip / GetImages / GetMediaForFragmentList / GetDataEndpoint、media バケットの読み書き、状態テーブルの読み書き
- detector: KVS の ListFragments / GetImages / GetDataEndpoint、detections バケットの Get / Put、Glue の読み取り、状態テーブルの読み書き
- api: KVS WebRTC の viewer(DescribeSignalingChannel、GetSignalingChannelEndpoint、GetIceServerConfig、ConnectAsViewer。チャネルの ARN だけ)、状態テーブルの読み取り、KVS の HLS 系と ListFragments 等、media の読み取り、Athena(このワークグループだけ)と結果バケット、detections と Glue の読み取り、Bedrock の InvokeModel*(推論プロファイルとルーティング先の基盤モデル)、IoT シャドウの Get / Update(このプロジェクトの Thing だけ)
- Converse / ConverseStream も IAM では `bedrock:InvokeModel` / `InvokeModelWithResponseStream` で判定される

## outputs

`project`、`region`、`iot_data_endpoint`、`iot_credential_endpoint`、`kvs_role_alias`、`gateways`(gateway_id → {thing_name, cameras})、`gateway_certificates`(sensitive。gateway_id → {certificate_pem, private_key})、`kvs_streams`、`webrtc_channels`(camera_id → {name, arn})、`webrtc_channel_prefix`(エッジの config.json に書く)、`cloudfront_url`、`cloudfront_distribution_id`、`viewer_bucket`、`media_bucket`、`detections_bucket`、`athena_bucket`、`artifacts_bucket`、`state_table`、`api_endpoint`、`lambda_functions`、`glue_database`、`athena_workgroup`

```sh
terraform -chdir=infra output -json gateway_certificates | jq -r '."gw-001".private_key'
```

## 検証

```sh
mise exec -- terraform -chdir=infra fmt -check -recursive
mise exec -- terraform -chdir=infra validate
(cd infra && mise exec -- tflint --init && mise exec -- tflint)
```

## 既知の制約

- SPA のフォールバックは `custom_error_response`(403/404 → index.html)ではなく、既定のビヘイビアだけに付けた CloudFront Function で行う(拡張子のないパスを `/index.html` に書き換える)。`custom_error_response` はディストリビューション全体に効き、`/api/*` の 404 や `/media/*` の 403 まで index.html に化けるため
- API Gateway のエンドポイントは CloudFront を通さなくても呼べる(認証なし。検証用)
- HTTP API の統合タイムアウトは最大 30 秒。自然言語検索がこれを超えると 503 になる
- Bedrock のモデルを初めて使うアカウントでは、コンソールでモデルアクセスの有効化(Anthropic は利用目的の登録)が要ることがある
- IoT 証明書の秘密鍵は Terraform の state に平文で入る
