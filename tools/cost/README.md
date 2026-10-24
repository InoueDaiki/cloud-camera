# tools/cost

カメラ 1 台・1 か月(30 日)あたりの総コストと内訳を出すツール。内訳は [構想.md](../../構想.md)「コスト検証の進め方」の項目
(取り込み、一時保持、動画抽出、加工・保存、物体検出、検出結果保存、再生、検索、制御)に揃える。

| サブコマンド | 内容 |
| --- | --- |
| `prices` | AWS Pricing API から東京リージョンの単価表 `prices_ap-northeast-1.json` を作る |
| `desk` | 机上計算。ビットレート、検出頻度、Lambda のメモリと実行時間、視聴時間、質問数などを引数で与える |
| `measure` | 実測。デプロイ済みの環境(`--project`)から期間内の使用量を集めて計算する。`--billing` で Cost Explorer と突き合わせる |
| `billing` | Cost Explorer の実請求額だけを出す |

レポートは Markdown で `tools/cost/reports/`(gitignore 済み)に書き出す。どのモードも、1 台・100 台・1,000 台の表を出す。

## セットアップ

```sh
cd tools/cost
mise exec -- uv sync
mise exec -- uv run pytest          # 単体テスト(AWS には接続しない。実測モードは moto)
```

AWS のプロファイルとリージョンは `mise.toml` の env(`cloud-camera` / `ap-northeast-1`)を使う。`--profile` で上書きできる。

## 使い方

```sh
# 単価表を取り直す(読み取りだけ。数秒〜十数秒)
mise exec -- uv run python -m cost prices
# 同じロジックで US East を取る(公式ページの料金例と照合する用)
mise exec -- uv run python -m cost prices --region us-east-1 --out /tmp/prices_us-east-1.json

# 机上計算(既定値は下の「机上計算の既定値」)
mise exec -- uv run python -m cost desk
mise exec -- uv run python -m cost desk --bitrate-mbps 2 --detect-interval-s 5 --media-retention-days 7
mise exec -- uv run python -m cost desk --bedrock-model-id jp.anthropic.claude-haiku-4-5-20251001-v1:0 --questions-per-month 1000
mise exec -- uv run python -m cost desk --help     # 引数の一覧

# 実測(直近 7 日、Cost Explorer と突き合わせ)
mise exec -- uv run python -m cost measure --project cloud-camera --days 7 --billing
# 期間を指定
mise exec -- uv run python -m cost measure --project cloud-camera --start 2026-11-01T00:00:00Z --end 2026-11-08T00:00:00Z
# 集めた生の値(reports/raw_*.json)から AWS を呼ばずに計算し直す(保存日数を変えて比べるなど)
mise exec -- uv run python -m cost measure --from-raw reports/raw_cloud-camera_....json --media-retention-days 7
```

## 単価表(`prices_ap-northeast-1.json`)

- 取得元は AWS Price List Query API(`pricing.us-east-1.amazonaws.com` の `get_products`)。件ごとに SKU、usagetype、
  公開日(`publication_date`)、段階料金(`tiers`)を持ち、ファイル全体に取得日時(`fetched_at`)がある
- 引き方は `cost/prices.py` の `SPECS`(サービスコード、usagetype、operation / model などの絞り込み)。SKU が 1 つに決まらなければエラーにする
- **無料枠は計算に含めない**。Pricing API の段階の先頭に単価 0 の段階がある(DynamoDB の保存 25 GB、EventBridge Scheduler の 1,400 万回)
  ものは `free_tier` 欄に移し、最初の有料単価を 0 から適用する。API に出ない無料枠(Lambda、CloudFront、Glue)は `free_tiers_manual` に出典付きで載せるだけ
- Pricing API で取れないものは `MANUAL_PRICES` に出典 URL 付きで手入力し、`source: "manual"` で区別する。今は Athena の最低課金の仮定(1 クエリ 10 MB)だけ
- 単位は揃えてある: Bedrock は 100 万トークン、Athena は TB、データ量は GB(= 2^30 バイト、AWS の課金の GB)
- Bedrock の `jp.` 推論プロファイルはリージョン内(Regional)の単価、`global.` は Global の単価で計算する(`prices.bedrock_price_prefix`)

主な単価(東京、2026-10-24 取得):

| 項目 | 単価 | 参考: US East |
| --- | ---: | ---: |
| KVS 取り込み | $0.010965 / GB | $0.0085 |
| KVS 保存 | $0.025 / GB-月 | $0.023 |
| KVS 取り出し(HLS、GetClip) | $0.01536 / GB | $0.0119 |
| KVS GetImages(1080p 以下) | $10 / 100 万枚 | $10 |
| Lambda arm64 / x86_64 | $0.0000133334 / $0.0000166667 GB 秒 | 同じ |
| Lambda リクエスト | $0.20 / 100 万 | 同じ |
| S3 Standard 保存(最初の 50 TB) | $0.025 / GB-月 | $0.023 |
| S3 PUT / GET | $0.0047 / $0.00037 / 1,000 件 | $0.005 / $0.0004 |
| S3 → CloudFront | $0 | $0 |
| CloudFront 日本(最初の 10 TB)/ HTTPS リクエスト | $0.114 / GB、$0.012 / 1 万件 | — |
| DynamoDB オンデマンド 書き込み / 読み込み | $0.715 / $0.1425 / 100 万 | $0.625 / $0.125 |
| IoT Core 接続 / メッセージ / シャドウ | $0.096 / 100 万分、$1.20 / 100 万、$1.50 / 100 万 | $0.08、$1.00、$1.25 |
| Athena | $5 / TB | $5 |
| Glue Data Catalog リクエスト | $1 / 100 万 | $1 |
| EventBridge Scheduler | $1.25 / 100 万 | $1.00 |
| CloudWatch Logs 取り込み(Lambda、vended) | $0.76 / GB | $0.50 |
| Bedrock Nova 2 Lite(jp)入力 / 出力 | $0.396 / $3.311 / 100 万トークン | — |
| Bedrock Claude Haiku 4.5(jp)入力 / 出力 | $1.10 / $5.50 / 100 万トークン | — |

公式の料金ページは US East の値しか載せていないので、同じ取得ロジックで us-east-1 を取り、ページの値と一致することを確かめた
(KVS $0.0085 / $0.023 / $0.0119、Lambda $0.0000166667 / $0.0000133334 / 追加 /tmp $0.0000000309、S3 GET $0.0004 / 1,000、
IoT 接続 $0.08 / 100 万分、Athena $5 / TB、Scheduler $1.00 / 100 万。構想.md「料金の取得状況」の表と同じ)。

## 計算の決まり

- 期間の合計を 30 日あたりに換算する(`model.to_month`)。実測は台数(KVS ストリームの数)で割って 1 台あたりにする
- 保存量は保存期間の**定常状態**で計算する: 1 日に増える量 × 保存日数(`model.steady_state_gb_month`)。
  - メディア: 保存日数は video_archiver の `MEDIA_RETENTION_DAYS`(= infra の `media_retention_days`)。`--media-retention-days` で変えられる
  - 検出結果: 現行版には期限がないので、`--detections-retention-days`(省略時はメディアと同じ)で区切る。旧バージョンは 1 日で消えるので、収集時点の量をそのまま使う
  - KVS: 1 時間あたりの取り込み量 × 保持時間
- 明細ごとに「台数に比例」か「台数によらない」(質問の数で決まる Bedrock のトークン、Athena の最低課金、Glue のリクエスト)を持つ。
  100 台・1,000 台は前者だけ台数倍する。段階料金(S3 の 50 TB 超など)は、同じ単価キーの量をアカウント全体で合計してから掛ける
- 明細の出所は `実測`(メトリクスや一覧から直接)、`推定`(取れないので根拠を付けて置いた値)、`机上` の 3 つ。レポートの「根拠」列に式を書く

### 実測で集めるもの

| 項目 | 取り方 | 出所 |
| --- | --- | --- |
| 区間の件(`fragment_count`、`bytes`、`video_duration_ms`、`detect_duration_ms`、`frame_count`、`detect_count`、状態) | DynamoDB `{p}-state` を Query(カメラごと、SK が期間内) | 実測 |
| KVS 取り込み、取り出し、GetImages の枚数 | CloudWatch `AWS/KinesisVideo` の `PutMedia.IncomingBytes`、`GetMP4MediaFragment.OutgoingBytes`、`GetClip.OutgoingBytes`、`GetMedia.OutgoingBytes`、`GetImages.LowRes/HighRes.ImagesCount` | 実測 |
| HLS 取り出しの内訳 | 動画の退避 = 区間の件の `bytes` の合計、残りを視聴に回す | 実測から按分 |
| GetImages の内訳 | 検出 = `frame_count` + DONE の区間数(区間の終わりの 1 枚。実測の枚数が上限)、残りをスプライトに回す。B 案(既定)ではどちらもほぼ 0(検出が予備の経路を使った区間だけ) | 推定 |
| Lambda | `Invocations`、`Duration`(合計)、メモリ・エフェメラルストレージ・アーキテクチャは関数の設定 | 実測 |
| CloudWatch Logs | `AWS/Logs IncomingBytes`(ロググループごと) | 実測 |
| S3 メディア | 期間内の時の接頭辞を ListObjectsV2(大きさと数。数 = PUT 回数とみなす) | 実測 / PUT は推定 |
| S3 検出結果 | 期間内の day ファイルを HeadObject、旧バージョンを ListObjectVersions。PUT / GET は DONE の区間数 | 実測 / 推定 |
| CloudFront | メディアバケットをオリジンに持つディストリビューションの `BytesDownloaded`、`Requests`(us-east-1) | 実測(ビューワーの静的ファイルと /api を含む) |
| S3 GET(オリジン取得) | CloudFront のリクエスト − API の数(キャッシュミス率 1 の上限) | 推定 |
| API Gateway | `AWS/ApiGateway Count`(`{p}-api`) | 実測 |
| DynamoDB | `ConsumedRead/WriteCapacityUnits`(テーブル全体)、保存は DescribeTable の平均サイズ × 1,440 件 / 日 × 保存日数 | 実測 / 保存は推定 |
| Bedrock、Athena(質問ごと) | api Lambda のログの `search {...}` 行の `usage`(トークン、`tool_calls`、`elapsed_ms`) | 実測 |
| Athena スキャン量 | ワークグループ `{p}` の `ProcessedBytes`(SEARCH で合計)。なければログの `athena_bytes_scanned` | 実測 |
| Glue Data Catalog | Athena のクエリ数 × 3 回 | 推定(CloudTrail を見ないと数えられない) |
| IoT Core | `PublishIn/Out.Success`、`RulesExecuted`(アカウント全体)。接続分とシャドウ更新はゲートウェイ数 × 期間 | 実測 / 推定 |
| EventBridge Scheduler | Lambda の起動回数(再試行なしの設定) | 推定 |

取れなかったもの(権限やリソースがない)は警告としてレポートの注記に出し、残りで計算する。

## 実環境での実行手順(メインセッション向け)

1. 本番名(`cloud-camera`)でデプロイし、カメラ 1 台で連続して動かす(1〜2 週間が目標。最低でも 2 日: 旧バージョンの削除と日次メトリクスが反映される)
2. 単価表を取り直すかを決める(`python -m cost prices`。差分は `git diff tools/cost/prices_ap-northeast-1.json` で見える)
3. 実測を集めて計算する:
   ```sh
   cd tools/cost
   mise exec -- uv run python -m cost measure --project cloud-camera --start <稼働開始の翌日 00:00Z> --end <今日 00:00Z> --billing
   ```
   期間は「カメラが連続で動いていた区間」にする(止めていた時間があると 1 台あたりの値が下がる)。生の値は `reports/raw_*.json` に残る
4. 同じ前提で机上計算し、実測との差を見る(ビットレートは実測の `bytes` から、実行時間は区間の件の平均から入れる):
   ```sh
   mise exec -- uv run python -m cost desk --bitrate-mbps <実測> --archiver-duration-s <実測> --detector-sec-per-frame <実測> ...
   ```
5. Cost Explorer の値は約 24 時間遅れる。突き合わせは期間の終わりから 1 日以上たってから行う

読み取り API しか呼ばない(Pricing、CloudWatch、DynamoDB Query、S3 の一覧と HeadObject、Lambda の設定、Logs の FilterLogEvents、
KVS の ListStreams、IoT の ListThings、API Gateway の GetApis、CloudFront の ListDistributions、Cost Explorer)。

## Cost Explorer の突き合わせ

`--billing` は期間(UTC の日単位)のサービス別・使用タイプ別の実請求額を出し、計算した額(実測台数 × 期間)と比べる。
タグ `Project`(infra の `default_tags`)で絞れればその値を、絞れなければアカウント全体の値を使う。このアカウントはプロジェクト専用なので、アカウント全体の値で突き合わせてよい(タグは有効化しない方針。docs/decisions.md)。ただし `cc-tmp-*` の一時環境を同時に動かしている期間は、その費用も混ざる。

2026-10-24 時点、このアカウントはリンクアカウントで、`ce list-cost-allocation-tags` が拒否され、`Project` タグでの絞り込みも 0 件。
タグで比べるには下の「infra への要望」の 1 が要る。

## infra への要望(担当外なので報告だけ)

1. **(不要と決定)コスト配分タグ `Project` の有効化**: 支払いアカウント(管理アカウント)の Billing で `Project` をユーザー定義のコスト配分タグとして有効にする。リンクアカウントからはできない。有効化の後に発生した費用からしか効かない
2. **タグが付かない費用**: IoT Core(Thing はタグ非対応、メッセージと接続の費用はリソースに紐付かない)、EventBridge Scheduler のスケジュール
   (`aws_scheduler_schedule` はタグ非対応。スケジュールグループには付けられる)、Bedrock のオンデマンド呼び出し。Bedrock は
   **アプリケーション推論プロファイル**(`aws_bedrock_inference_profile`、`jp.` のプロファイルをコピー元にしてタグを付ける)を作って api から使えば、タグで分けられる
3. **S3 のリクエストメトリクス**: 今は PUT / GET を一覧と区間数から推定している。メディアと検出結果のバケットに `aws_s3_bucket_metric`(バケット全体のフィルタ)を
   付ければ `AllRequests` / `PutRequests` / `GetRequests` / `BytesDownloaded` が取れる(CloudWatch の有料メトリクス)
4. **検出結果バケットの現行版の期限**: day ファイルに期限がないので保存量が増え続ける。保存期間を決めて `expiration` を足すか、変数にする
5. **CloudFront の配信量の内訳**: ビューワーの静的ファイルと /media が同じディストリビューション。分けて測るなら標準ログ(S3 か CloudWatch Logs)を有効にする

## 机上計算の既定値と結果(2026-10-24 の単価)

既定値は `cost/desk.py` の `DeskParams`。**B 案**(スプライトも物体検出も clip.mp4 のキーフレームを Lambda 内でデコードし、GetImages を使わない。
[lambdas/common/FRAMES.md](../../lambdas/common/FRAMES.md))の実 KVS での実測に合わせている: video_archiver 512 MB・1 区間 3.2 秒・スプライト 250 KB、
detector 1,024 MB(infra の既定)・1.5 秒 + 60 枚 × 0.135 秒(yolox_tiny INT8 @640)・S3 の clip.mp4 を 1 分 1 回 GET。
視聴 2 時間 / 台・月、質問 100 件 / 月(入力 8,000、出力 600 トークン、Nova 2 Lite jp)は仮定。

| 項目 | 1 台 | 100 台(1 台あたり) | 1,000 台(1 台あたり) |
| --- | ---: | ---: | ---: |
| 取り込み | $3.31 | $3.31 | $3.31 |
| 一時保持 | $0.01 | $0.01 | $0.01 |
| 動画抽出 | $5.67 | $5.67 | $5.67 |
| 加工・保存 | $8.72 | $8.72 | $8.46 |
| 物体検出 | $5.67 | $5.67 | $5.67 |
| 検出結果保存 | $0.61 | $0.61 | $0.60 |
| 再生 | $0.06 | $0.06 | $0.06 |
| 検索 | $0.53 | $0.0087 | $0.0039 |
| 制御 | $0.24 | $0.24 | $0.24 |
| **合計** | **$24.82** | **$24.30** | **$24.03** |

- 以前の方式(GetImages をスプライトと検出で 1 分 61 枚ずつ、detector 3,008 MB・2 秒 + 60 枚 × 0.25 秒の仮定)では合計 $100.19 だった。
  GetImages(月 $26.35 × 2)がなくなり、detector の Lambda は $28.76(仮定)→ $5.53(実測)になった。
  同じ detector の設定(1,024 MB・実測の推論時間)で GetImages だけ戻すと $77.24(`--sprite-images-per-minute 61 --archiver-duration-s 1.9 --sprite-kb 80
  --detector-frame-source getimages --detector-overhead-s 2.0`)。B 案はこれより 1 台・月 約 $52 安い(FRAMES.md の見積もりと同じ)
- 大きいのは **加工・保存**(S3 の動画の保存)、**動画抽出**(KVS の HLS 取り出し $4.6 + video_archiver の Lambda $0.92)、**物体検出**(detector の Lambda $5.53)
- 検出を 5 秒おきにすると合計 $20.99、検出なしで $18.53(`--detect-interval-s 5` / `0`)
- 構想.md の試算例(1 Mbps、US East $0.0085 で取り込み約 $2.75)は、東京の $0.010965 で **$3.31**(GB = 2^30 バイト、301.7 GB)。
  構想.md と同じく 10^9 バイト = 1 GB として数えると 324 GB × $0.010965 = $3.55。保持 1 時間分の保存は $0.0105、HLS で 1 時間視聴は $0.0064
