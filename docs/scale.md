# カメラ台数を増やしたときの検証

100 台・1,000 台・1 万台にしたとき、どの上限にどの台数で当たるかを、公式ドキュメントの上限と 10 台の実測から見積もった
(2026-10-24、ap-northeast-1、project `cc-tmp-scale`)。全体像は [README.md](../README.md#アーキテクチャ)、契約は [contracts.md](contracts.md)。

## 結論

| 台数 | 詰まるもの | 種類 |
| --- | --- | --- |
| **約 20〜40 台** | **このアカウントの Lambda の同時実行の上限が 10**(既定の 1,000 ではない)。10 台で平均 2.7、最大 6 を使った。ほかの検証環境(`cc-tmp-e2e` など)と共有で、API の Lambda(同期)も同じ枠を使う | **引き上げ申請が必須**(Service Quotas「Concurrent executions」、調整可) |
| **約 85〜190 台** | Lambda の環境変数の合計 4 KB(引き上げ不可)。`CAMERAS`(カメラ → ゲートウェイの JSON)が 1 台あたり 19 バイト増え、約 190 台で `terraform apply` が失敗する。main で追加された `CAMERA_NAMES`(表示名)を全台に付けると 1 台あたり約 20〜27 バイト増え、約 85〜100 台に下がる | **設計変更**(カメラ一覧を DynamoDB か S3 に置く) |
| 約 15〜20 台 / ゲートウェイ | IoT のシャドウ文書 8 KB。1 つのクラシックシャドウに配下の全カメラの desired と reported を入れているので、1 台あたり約 340 バイト(PTZ 対応ならもっと)で 20 台前後が上限 | 設計変更(カメラごとの名前付きシャドウ)か、ゲートウェイを分ける |
| 約 110 ゲートウェイ | api のロールのインラインポリシー 10,240 文字(Thing の ARN を 1 つずつ列挙していた) | **実装済み**(ワイルドカードにした) |
| 約 700 台(障害の後) | 既定の同時実行 1,000。止まっていた分を追い上げると、1 起動が最大 10 区間(物体検出は約 2 分)になり、同時実行が平常の約 6 倍(台数 × 1.4)になる | 引き上げ申請、または追い上げの同時実行を絞る設計 |
| 約 4,000 台 | 既定の同時実行 1,000(平常時。台数 × 0.24) | 引き上げ申請 |
| **約 6,500 台** | **KVS の GetDataEndpoint、アカウントあたり 300 TPS(引き上げ不可)**。プロセス内のキャッシュだけだと、台数が増えるほど同じ実行環境に同じカメラが来なくなり、1 万台で約 460 TPS の見込み | **実装済み**(処理済み位置の件にエンドポイントを保存。1 万台で約 6 TPS) |
| 1 万台 | KVS のストリーム数 10,000(アカウント全体、調整可)。ほかの環境のストリームも数に入る | 引き上げ申請 |
| 1 万台 | `GET /api/cameras` がゲートウェイごとに GetThingShadow を順に呼ぶ(1,000 回 × 20〜30 ms)ので、API Gateway の 30 秒に近づく。Terraform も 1 つの state に約 3 万リソース | 設計変更(ページ分け、状態のキャッシュ、プロビジョニングを Terraform の外へ) |

当たらないもの: EventBridge Scheduler(スケジュール数、起動 TPS)、DynamoDB、S3、IoT Core の接続とメッセージ、API Gateway、Athena、KVS のストリームごとの上限は 1 万台でも余裕がある(下の表)。
**「毎分の境界で全カメラが一斉に起動する」ことは起きなかった**。Scheduler がスケジュールごとに起動の秒を散らしている(実測。下記)。

## 上限と試算の比較表

1 台・1 分あたりの値は 10 台の実測(下の「実測」)。ピークの同時実行は、起動の秒がスケジュールごとにばらばらに決まる(実測)ので、
平均 m に対して m + 3√m(ポアソン近似の 99.9% 点)とした。「障害の後」は全カメラが同時に追い上げる最悪の場合。

| 項目 | 1 台・1 分あたり(実測) | 100 台 | 1,000 台 | 1 万台 | 上限(東京) | 判定 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| Lambda 同時実行(平常の平均 / ピーク) | 動画 2.36 秒 + 検出 11.7 秒 = 0.23 | 23 / 38 | 234 / 281 | 2,340 / 2,490 | 既定 1,000(このアカウントは **10**)、調整可 | このアカウント: 100 台で NG。既定: 1 万台で NG |
| Lambda 同時実行(障害の後の追い上げ) | 1 起動 10 区間 × 2 関数(リースでカメラごとに 1 本まで) | 140 | 1,400 | 14,000 | 同上 | 既定: 1,000 台で NG |
| Lambda のスケーリング速度 | — | — | — | 2,500 環境 | 関数ごとに 10 秒あたり 1,000 環境 | OK(立ち上がりに約 25 秒) |
| Lambda の環境変数(api、動画、検出) | 1 台 19 バイト(`CAMERAS`)+ 表示名を付ければ約 20〜27 バイト(`CAMERA_NAMES`)+ 共通 約 500 バイト | 2.4〜4.7 KB | 19.5〜46 KB | 190〜460 KB | 4 KB(引き上げ不可) | **約 190 台(表示名ありなら約 85〜100 台)で NG** |
| EventBridge Scheduler のスケジュール数 | 2 本 / 台 | 200 | 2,000 | 20,000 | 1,000 万 | OK |
| Scheduler の起動 | 2 回 | 3.3 TPS | 33 TPS | 333 TPS | 1,000 TPS(東京。超えると遅れる) | OK |
| KVS GetDataEndpoint(変更前の見込み) | 0.21 回(10 台。ほぼコールドスタートの分) | 0.1 TPS | 22 TPS | **約 460 TPS** | アカウント 300 TPS、ストリーム 5 TPS(引き上げ不可) | 変更前: 約 6,500 台で NG |
| KVS GetDataEndpoint(変更後) | ストリーム × API ごとに 1 時間に 1 回 | 0.06 TPS | 0.6 TPS | 5.6 TPS(+ エッジ 約 8 TPS) | 同上 | OK |
| KVS DescribeStream(エッジの起動とトークン更新) | — | — | — | 約 4 TPS(一斉再起動で 1 万回) | アカウント 300 TPS | OK(一斉再起動は 30 秒ほどで捌ける) |
| KVS ListFragments(変更前 → 後) | 2 回 → **1 回** | 1.7 TPS | 17 TPS | 167 TPS | アカウントの TPS の記載なし。ストリームごとに断片メタデータ 10,000 点 / 秒(1 回 = MaxResults 1,000 点) | OK |
| KVS GetHLSStreamingSessionURL | 1 回 | — | — | 167 TPS(全体) | ストリームごと 25 TPS | OK(ストリームあたり毎分 1 回 + 視聴) |
| KVS GetMP4MediaFragment | 60 回(1 秒の断片)、8 並列で約 1 秒に集中 | 100 TPS | 1,000 TPS | 10,000 TPS | セッションごと 20 TPS、ストリームごとに断片メディア 500 点 / 秒。アカウントの記載なし | 台数によらない(下の注) |
| KVS ストリーム数 | 1 本 / 台 | 100 | 1,000 | 10,000 | アカウント 10,000(調整可) | **1 万台で NG** |
| KVS PutMedia | 接続 1 本 / ストリーム | — | — | — | ストリームごと 1 接続、5 TPS、12.5 MB/秒 | OK |
| DynamoDB 書き込み | 12.9 WCU | 22 WCU/秒 | 215 WCU/秒 | 2,150 WCU/秒 | テーブル 40,000、新しいオンデマンドのテーブルは 4,000 書き込み/秒まで即時 | OK |
| DynamoDB(パーティション = カメラ) | 12.9 WCU / 分 = 0.2 WCU/秒 | — | — | — | パーティションあたり 1,000 WCU/秒 | OK |
| S3 PUT(メディア、カメラごとの接頭辞) | 3 回 | 5 /秒 | 50 /秒 | 500 /秒 | 接頭辞あたり 3,500 /秒 | OK |
| S3 PUT + GET(検出結果、`detections/dt=…/` を全カメラで共有) | PUT 1 回 + GET 1 回 | 1.7 /秒 | 17 /秒 | 167 /秒ずつ | 接頭辞あたり PUT 3,500 / GET 5,500 | OK |
| IoT Core 接続(10 台 / ゲートウェイ) | — | 10 | 100 | 1,000 | 50 万、CONNECT 3,000 /秒 | OK |
| IoT Core publish(status 30 秒 + シャドウ更新は 4 件扱い) | — | 1.7 /秒 | 17 /秒 | 167 /秒 | 受信 20,000 /秒、シャドウ API 4,000 /秒 | OK |
| IoT 認証情報プロバイダー(kvssink がカメラごとに 1 時間ごと) | — | 0.03 TPS | 0.3 TPS | 2.8 TPS(一斉再起動で 1 万回) | アカウント 50 TPS | OK(一斉再起動は 200 秒かかる) |
| シャドウ文書(1 ゲートウェイ) | 約 340 バイト / カメラ | — | — | — | 8 KB | 約 20 台 / ゲートウェイ |
| api のインラインポリシー | 約 65 文字 / ゲートウェイ | — | — | — | 10,240 文字 | 変更前は約 110 ゲートウェイで NG → 実装済み |
| `GET /api/cameras` | ゲートウェイごとに GetThingShadow を順に | 10 回 | 100 回(約 3 秒) | 1,000 回(約 30 秒) | API Gateway の統合 30 秒 | 1 万台で NG |
| API Gateway / Athena | 視聴と質問の数で決まる(台数に比例しない) | — | — | — | 10,000 rps / 同時 DML 150 | OK |

注:
- GetMP4MediaFragment は動画の退避が 1 区間ごとに新しい HLS セッションを開いて 8 並列で取るので、1 セッションで約 60 回 / 秒になる(ドキュメントの 20 TPS を超える)。
  実測ではエラー 0(`Requests` = `Success`)だった。台数によらない(ストリームとセッションごとの上限)ので、問題になったら `DOWNLOAD_WORKERS` を下げる
- GetDataEndpoint の「変更前」は、実行環境の数(≒ ピークの同時実行 × 1.2)と、1 時間に同じカメラが同じ実行環境に来る確率から出した見込み:
  1,000 台で動画 72%、検出 23%、1 万台で 12%、3% がキャッシュに当たる。10 台では実行環境が 2〜3 個なのでほぼ当たり、20 分で 42 回(うちコールドスタート 2 回で 40 回)だった
- 出典: [KVS クォータ](https://docs.aws.amazon.com/kinesisvideostreams/latest/dg/limits.html)、[Lambda クォータ](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)、
  [Lambda のスケーリング](https://docs.aws.amazon.com/lambda/latest/dg/scaling-behavior.html)、[Lambda の非同期の再試行](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)、
  [Scheduler クォータ](https://docs.aws.amazon.com/scheduler/latest/UserGuide/scheduler-quotas.html)、[Scheduler → Lambda は非同期](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html)、
  [DynamoDB クォータ](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html)、[オンデマンドの初期スループット](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode.html)、
  [IoT Core クォータ](https://docs.aws.amazon.com/general/latest/gr/iot-core.html)、[S3 の性能](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)、
  [Athena クォータ](https://docs.aws.amazon.com/general/latest/gr/athena.html)、[API Gateway クォータ](https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html)、[IAM クォータ](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html)。
  このアカウントの値は `aws lambda get-account-settings`(`ConcurrentExecutions: 10`)と Service Quotas で確認した。
  環境変数の大きさは cc-tmp-scale(10 台)の api の実際の設定から測った(合計 691 バイト、うち `CAMERAS` 191 バイト)。`CAMERA_NAMES` は計測後に main で追加されたもので、1 台あたりの増え方は日本語 2〜5 文字の名前を仮定した見込み

### 上限の主な値(一次情報、2026-10-24 に確認)

| サービス | 上限 | 範囲 | 引き上げ |
| --- | --- | --- | --- |
| KVS | ストリーム 10,000 | アカウント | 可 |
| KVS | GetDataEndpoint / DescribeStream 300 TPS(ストリームごと 5 TPS) | アカウント | 不可 |
| KVS | GetHLSStreamingSessionURL 25 TPS | ストリーム | 不可 |
| KVS | GetHLSMasterPlaylist / GetHLSMediaPlaylist / GetMP4InitFragment 5 TPS、GetMP4MediaFragment 20 TPS | HLS セッション | 不可 |
| KVS | 断片メタデータ 10,000 点 / 秒(ListFragments、GetHLSMediaPlaylist、GetClip、GetImages で共有)、断片メディア 500 点 / 秒 | ストリーム | 不可 |
| KVS | ListFragments、GetClip、GetImages の TPS | — | 記載なし |
| KVS | PutMedia 1 接続、5 TPS、12.5 MB/秒 | ストリーム | 帯域のみ可 |
| Lambda | 同時実行 1,000(新しいアカウントは低いことがある。このアカウントは 10) | リージョン | 可 |
| Lambda | スケーリング 10 秒あたり 1,000 実行環境 | 関数 | 不可 |
| Lambda | 環境変数の合計 4 KB | 関数 | 不可 |
| Lambda | 非同期でスロットリングされたイベントは最長 6 時間、1 秒〜5 分の間隔で再試行 | イベント | 最大保持時間で短縮 |
| Lambda | 予約同時実行は「未予約 − 100」まで | アカウント | — |
| Scheduler | スケジュール 1,000 万、グループ 500 | アカウント | 可 |
| Scheduler | 起動 1,000 TPS(東京。超えると遅れて起動) | アカウント | 可 |
| Scheduler | FlexibleTimeWindow 1〜1,440 分。ウィンドウ内の散らし方は記載なし | — | — |
| DynamoDB | パーティションあたり 3,000 RCU / 1,000 WCU。オンデマンドのテーブルあたり 40,000 RRU / WRU。新しいテーブルは 4,000 書き込み / 12,000 読み込み/秒まで即時 | — | テーブルの上限は可 |
| IoT Core | 接続 50 万、CONNECT 3,000 /秒、publish 受信 20,000 /秒、接続あたり 100 /秒 | アカウント / 接続 | 接続あたりは不可 |
| IoT Core | シャドウ API 4,000 /秒、シャドウあたり 20 /秒、文書 8 KB、ネスト 8 段 | アカウント / シャドウ | 文書サイズは可 |
| IoT Core | AssumeRoleWithCertificate 50 TPS(東京) | アカウント | 可 |
| S3 | 接頭辞あたり PUT 3,500 /秒、GET 5,500 /秒(徐々に拡がる。途中は 503 がありうる) | 接頭辞 | — |
| Athena | 同時 DML 150(東京)、StartQueryExecution 100 /秒 | アカウント | 可 |
| API Gateway | 10,000 rps(バースト 5,000)、統合のタイムアウト 30 秒 | アカウント / API | rps は可 |
| IAM | ロールのインラインポリシー合計 10,240 文字 | ロール | 不可 |

## 実測(10 台、2026-10-24 05:20〜06:52 UTC)

### 構成

- エミュレーター 1 コンテナで 10 台(H.265 960×540、15 fps、キーフレーム 1 秒、`testsrc`)。1280×720 × 10 台は Mac の CPU が足りず
  エンコードが実時間の 0.8 倍しか出なかったので、解像度を下げた(このときエミュレーターは約 5.6 コア)。配信のビットレートは実測 約 0.37 Mbps / 台
- エッジ 1 コンテナ(1 ゲートウェイ = 10 カメラ、シャドウで配信、`CC_SINK=kvs`)。`tools/scale/compose.scale.yaml`
- `scripts/deploy.sh cc-tmp-scale -var-file=...`(`gateways = {"gw-001" = [cam-001 … cam-010]}`)。Lambda の設定は既定(動画 512 MB、検出 1,024 MB、arm64)
- 計測: CloudWatch(Lambda、Scheduler、KVS、DynamoDB)、Lambda のログ(REPORT と `pipeline_done`)、CloudTrail(GetDataEndpoint)、
  30 秒ごとの処理済み位置の遅れとエッジの CPU・メモリ・送信量(`tools/scale/`)

| 区間 | 時刻(UTC) | 内容 |
| --- | --- | --- |
| A | 05:40〜06:00 | 変更前のコード、`rate(1 minute)`、FlexibleTimeWindow OFF(Terraform の定義のまま) |
| S | 06:01〜06:11 | 全スケジュールを `cron(* * * * ? *)` にして毎分 0 秒にそろえようとした |
| F | 06:12〜06:21 | `rate(1 minute)`、FlexibleTimeWindow 1 分 |
| B | 06:23〜06:29 | スケジュールを止めて仕事を溜め、20 本(10 台 × 2 関数)を同じ瞬間に非同期起動(2 回) |
| C | 06:36〜06:52 | 変更後のコード(エンドポイントの保存、物体検出の ListFragments 省略、IAM)、`rate(1 minute)`、OFF |

### 結果

| 項目 | A(変更前) | S(cron) | C(変更後) |
| --- | --- | --- | --- |
| 起動(動画 / 検出) | 200 / 200 | 100 / 100 | 160 / 159 |
| 動画の退避の実行時間(平均 / p99 / 最大) | 2.36 / 2.99 / 4.48 秒 | 2.38 / 4.63 / 6.29 秒 | 2.30 / 2.96 / 4.42 秒 |
| 物体検出の実行時間(平均 / p50 / 最大) | 11.7 / 12.4 / 12.6 秒 | 12.7 / 12.5 / 27.3 秒 | 10.1 / 8.8 / 17.5 秒 |
| コールドスタートの Init(動画 / 検出) | 0.30 / — 秒 | 0.28 / 1.30 秒 | — / 1.06 秒 |
| 最大メモリ(動画 / 検出) | 183 / 533 MB | 181 / 536 MB | 186 / 531 MB |
| 同時実行(ログから 1 秒ごと、平均 / 最大) | 2.68 / 6 | 2.73 / 7 | 2.30 / 5 |
| 同時実行(CloudWatch、アカウント全体の最大) | 6 | 8 | 6 |
| スロットリング、Lambda のエラー、非同期の破棄 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| Scheduler の起動 / エラー / スロットリング | 399 / 0 / 0 | 198 / 0 / 0 | 313 / 0 / 0 |
| ListFragments / 1 台・1 分 | **2.0** | 2.0 | **1.0** |
| GetHLSStreamingSessionURL、Master、Media、Init / 1 台・1 分 | 各 1.0 | 各 1.0 | 各 1.0 |
| GetMP4MediaFragment / 1 台・1 分 | 60.0 | — | 60.0 |
| KVS の API のエラー(`Requests` − `Success`) | 0 | 0 | 0 |
| GetDataEndpoint(CloudTrail) | 42 回 / 20 分(検出 2、動画 40) | — | 20 回 / 22 分(デプロイ直後の全実行環境が新しい状態から。検出 0) |
| DynamoDB 消費 WCU / 1 台・1 分 | 12.9 | 13.4 | 12.9(エンドポイントの保存で増えない) |
| DynamoDB 消費 RCU / 1 台・1 分 | 3.7(うち計測用の BatchGetItem 約 2) | 3.9 | 3.7 |
| DynamoDB のスロットリング | 0 | 0 | 0 |
| 処理済み位置の遅れ: 動画(最小 / 平均 / 最大) | 58 / 117 / 160 秒 | — | 62 / 106 / 170 秒 |
| 処理済み位置の遅れ: 検出 | 81 / 156 / 212 秒 | — | 96 / 144 / 229 秒 |

- 物体検出の実行時間は区間ごとに 8.8 秒か 12.5 秒の 2 つの山があった(実行環境の差と見られる。[detector の README](../lambdas/detector/README.md) にも ±30% のばらつきの記載)。
  1,280×720 の実映像での実測(9.4〜9.6 秒)とほぼ同じ水準
- 物体検出の `pipeline_done` の `stopped` は A で `waiting_upstream` 159 回 / `caught_up` 41 回。ただし **1 起動あたり 1 区間を処理しており、無駄な起動ではない**
  (動画の退避より先に起動した回は、前の起動までに確定した区間を処理してから止まる)。代わりに検出は動画より平均 約 40 秒遅れる
- 遅れ(動画 約 2 分)は lag 45 秒 + 1 分区間の切り捨て + 起動の秒の位置による。設計どおり

### 起動の秒の位置(Scheduler)

`pipeline_done` と直前の `START` を対応させ、カメラごとに起動の時刻を見た(`tools/scale/phase.py`)。

| 設定 | 1 本のスケジュールの起動 | 20 本の散らばり |
| --- | --- | --- |
| `rate(1 minute)`、OFF | 毎分同じ秒(例: cam-001 の動画は毎分 35.3 秒)。間隔は 60.0 秒ちょうど | 0〜59 秒にばらばら(同じ秒に 2 本重なることもある) |
| `cron(* * * * ? *)`、OFF | 毎分同じ秒(例: 44.1 秒)。0 秒ちょうどにはならない | 1〜50 秒にばらばら |
| `rate(1 minute)`、FLEXIBLE 1 分 | 毎分同じ秒。毎回ランダムにはならない | 0〜59 秒にばらばら |

- **3 つの設定とも、Scheduler はスケジュールごとに決まった秒の位置で起動し、全体は分の中に散らばった**。cron でも 0 秒にはそろわなかった。
  秒の位置はスケジュールを更新するたびに決め直される(作成直後と更新直後の 1〜2 回だけ間隔が 40〜55 秒になる)。ドキュメントには記載がない挙動なので、変わりうる前提で扱う
- `StartDate` の秒で位置を決められるかも試した(一時スケジュール 3 本)。指定した秒に合ったのは 2 本で、1 本はずれた。決め手にはならない
- 以上から **FlexibleTimeWindow は入れていない**(効果が見えず、ドキュメント上はウィンドウ内のどこで起動してもよいので、検出と動画の順序や遅れがかえって読みにくくなる)

### 一斉に起動したとき(区間 B)

Scheduler では一斉起動を作れなかったので、スケジュールを止めて 2〜3 分ぶんの仕事を溜め、20 本を同時に非同期で Invoke した。

- アカウントの同時実行が上限の 10 に達し、スロットリング 61 回(動画 41、検出 20)。`AsyncEventAge` の最大は約 35 秒。**破棄 0**、40 本すべてが実行された
  (非同期のスロットリングは Lambda が最長 6 時間、1 秒〜5 分の間隔で再試行する)
- つまり一斉起動は「遅れる」だけで取りこぼしにはならない。ただし同じアカウントの同期呼び出し(ビューワーの API)は、その間 429 になる。
  このとき `cc-tmp-e2e` の API も同じ枠を使っていたので、06:24 と 06:26 の約 40 秒ずつ影響したおそれがある
- 追い上げ(1 起動 3 区間)の物体検出は 26〜44 秒、動画は 7〜9 秒

### エッジ(1 ゲートウェイで 10 カメラ)

| 項目 | 値 |
| --- | --- |
| CPU | 平均 5.5%、最大 12%(Mac の Docker、1 コア = 100%。再エンコードしないので軽い) |
| メモリ | 約 327 MiB(10 カメラ。子プロセスは 1 カメラ 1 つ) |
| 上り帯域 | 3.94 Mbps(配信 10 × 0.37 Mbps に対して約 6% の上乗せ) |
| PutMedia | 接続エラー 0。断片は 1 秒ごと |
| シャドウ | desired 1,243 バイト、reported 2,150 バイト(1 カメラ 193 バイト + ゲートウェイの情報 97 バイト)。PTZ 非対応のカメラで 1 台あたり計 約 340 バイト |

## 設計の弱点と改善

### 実装したもの

1. **KVS のデータエンドポイントを処理済み位置の件に保存する**(`lambdas/common/kvs.py`、`pipeline.py`)。
   リースを取る UpdateItem(`ReturnValues=ALL_NEW`)で読み、取り直したものがあればリースを解放する UpdateItem で書く。DynamoDB の呼び出しは増えない(実測の WCU も同じ)。
   期限はプロセス内のキャッシュと同じ 1 時間。GetDataEndpoint はストリーム × API ごとに 1 時間に 1 回になり、1 万台で約 460 TPS(見込み)→ 約 6 TPS。
   契約に `CURSOR#*` の `kvs_endpoints` を追加した
2. **物体検出のウォーターマークを、要る区間に来たときだけ取る**(`pipeline.py`)。上流(動画の退避)が確定させた区間はウォーターマークを見ないので、
   ListFragments を呼ばない。1 台・1 分あたりの ListFragments は 2 回 → 1 回(実測)。上流が止まって `upstream_max_wait` を過ぎたときだけ呼ぶ
3. **api のロールの IoT シャドウの権限をワイルドカードにした**(`infra/lambda.tf`、`thing/{p}-gw-*`)。ゲートウェイの Thing の ARN を並べていたので、
   約 110 ゲートウェイでインラインポリシーの 10,240 文字に当たっていた

### 提案のみ(大きな設計変更)

| 弱点 | 当たる台数 | 案 |
| --- | --- | --- |
| Lambda の同時実行がアカウントで共有。このアカウントは 10 | 20〜40 台 | まず Service Quotas で引き上げる(1 万台なら平常 2,500、追い上げを考えると 5,000 程度)。本番と検証を別アカウントにする(同時実行も KVS のストリーム数もアカウント単位)。api に予約同時実行を付けて、録画処理の追い上げで API が 429 にならないようにする(未予約を 100 残す必要があるので、上限の引き上げが先) |
| `CAMERAS`、`CAMERA_NAMES` を環境変数で渡している(合計 4 KB) | 約 190 台(表示名ありなら約 85〜100 台) | カメラ → ゲートウェイの対応と表示名を DynamoDB の状態テーブル(例: SK `CAMERA`)か S3 の JSON に置き、api はそこから読む(起動中はキャッシュ)。動画と検出は使っていないので、環境変数を関数ごとに分けて外す |
| 障害の後の追い上げで同時実行が平常の約 6 倍 | 約 700 台(既定 1,000) | 1 起動の区間数(`MAX_MINUTES` 10)を検出だけ小さくする、または下の SQS 案で同時実行の上限を付ける |
| カメラ × 2 本のスケジュール。検出は動画を待つだけ(平均 40 秒遅れる) | 遅れは台数によらない。本数は 1 万台で 2 万本(上限には遠い) | 動画の退避が区間を確定させたら、物体検出を非同期で起動する(スケジュールはカメラ × 1 本、`waiting_upstream` がなくなり、検出の遅れが数秒になる)。検出用のスケジュールは取りこぼしの保険として 5 分おきなどに残す |
| (同上)スケジュールの本数と Terraform の state | 1,000 台〜 | 1 本のスケジュール(1 分ごと)→ 振り分けの Lambda → SQS(カメラごとのメッセージ、`DelaySeconds` で散らす)→ イベントソースマッピングの最大同時実行で上限を付ける。下の比較 |
| ゲートウェイごとに 1 つのクラシックシャドウ(8 KB) | 約 20 台 / ゲートウェイ | カメラごとの名前付きシャドウ(`cam-001` など)に分ける。文書の上限が 1 台ずつになり、更新の競合(シャドウあたり 20 /秒)も分かれる |
| `GET /api/cameras` が全ゲートウェイのシャドウを順に読む | 1,000〜1 万台 | ページ分け(`?gateway=`、`?limit=`)、シャドウの取得を並列に、または IoT のルールで status を DynamoDB に書き、一覧はそこから読む |
| Terraform の 1 つの state に台数 × 3 のリソース | 1,000 台〜 | 共通の基盤(テーブル、バケット、関数)は Terraform、カメラの追加・削除(ストリーム、スケジュール、Thing)はプロビジョニングの API(Lambda)で行う |
| エッジの一斉再起動(停電の復旧など) | 1 万台 | 認証情報プロバイダー 50 TPS、DescribeStream 300 TPS に並ぶので、エッジの起動に数十秒のジッターを入れる |

スケジュールの方式の比較:

| | カメラ × 2 本(今) | カメラ × 1 本 + 動画の後に検出を起動 | 1 本 + 振り分け + SQS |
| --- | --- | --- | --- |
| 本数(1 万台) | 2 万 | 1 万 | 1 |
| 起動の散らばり | Scheduler 任せ(実測でばらける) | 同左 | 振り分けの Lambda が決める(`DelaySeconds` 0〜59) |
| 検出の遅れ | 動画 + 0〜60 秒 | 動画 + 数秒 | 振り分け次第 |
| 同時実行の上限 | なし(アカウントの上限まで) | なし | イベントソースマッピングの最大同時実行で付けられる |
| 振り分けの失敗 | スケジュールごとに独立 | 同左 | 振り分けの Lambda が止まると全台止まる(要監視) |
| 料金(1 万台・月) | Scheduler 約 $1,080 | 約 $540 + Lambda の Invoke | SQS 約 $520(4.3 億メッセージ × 送信・受信・削除、$0.40 / 100 万の仮定)+ 振り分け |
| 変更の大きさ | — | 小(動画の退避に Invoke を 1 つ足す) | 大 |

FlexibleTimeWindow は、Scheduler がすでに起動を散らしている(実測)ので入れなかった。

### 当初の構想の想定との差

当初の構想(削除済みの `構想.md`、コミット `20a15d5`)の「調査結果」の「Lambda の上限」の行は、「全台が毎分の境界で同時に起動すると上限に当たり得るので、FlexibleTimeWindow で散らす」としている。実測では次のようになった。

- FlexibleTimeWindow なしでも、Scheduler はスケジュールごとに起動の秒をずらしていた(`cron` でも 0 秒にそろわない)。一斉起動は起きなかった
- 同時実行の上限は、構想に書いた既定の 1,000 ではなく、このアカウントでは **10** だった(`aws lambda get-account-settings`)。数十台の検証の前に引き上げが要る

## エッジ 1 台あたりのカメラ数の目安

- **上り帯域が先に決まる**: 送信量は配信のビットレートの合計 × 約 1.06(実測)。1 Mbps のカメラなら 10 台で約 11 Mbps、2 Mbps なら約 21 Mbps
- **シャドウが約 20 台で上限**(8 KB。PTZ 対応でプリセットが多いともっと少ない)
- CPU は軽い(Mac で 10 台 5.5%)。Raspberry Pi 4 は未計測だが、再エンコードしないので CPU より帯域とメモリで決まる見込み
- メモリは 10 台で約 330 MiB。回線断の間は kvssink がカメラごとに最大 `CC_KVS_STORAGE_SIZE_MB`(既定 64 MB)を貯めるので、最悪 10 台で +640 MB。
  Raspberry Pi 4(2〜4 GB)なら 10 台程度、8 GB なら 15 台程度
- 目安: **1 ゲートウェイ 10 台(多くても 15 台)**。それより多ければゲートウェイを分けるか、シャドウをカメラごとにする

## tools/cost との整合

- `tools/cost` の机上計算(B 案の既定値)と実測は同じ水準: 動画の退避 3.2 秒(机上)/ 2.3 秒(実測、960×540・0.37 Mbps なので短い)、
  物体検出 9.6 秒 / 10〜12 秒、起動はどちらも 1 台 1 分 2 回、S3 PUT はメディア 3 回 + 検出結果 1 回
- DynamoDB は机上が 1 台 1 分 18 WRU(動画 9 + 検出 9)、実測は 12.9 WCU で、机上の方が多め(安全側)。読み込みは机上 2 RRU、実測 約 1.7 RCU(計測用の読み込みを除く)
- 今回の変更は料金を変えない: GetDataEndpoint と ListFragments は KVS の料金表に項目がない(取り出しは GB 単位)。エンドポイントはリースの解放と同じ書き込みに入れたので、WCU は同じ(実測 12.9)
- 台数の比例: 1 台あたりの単価は台数によらない(S3 の段階料金だけ。机上で 1 台 $24.82、1,000 台で 1 台あたり $24.03)。ここで挙げた上限の対応(上限の引き上げ)に追加の料金はない。
  SQS 案や名前付きシャドウにすると、そのぶん少し変わる(上の比較表)

## 再現の手順

```sh
# 1. デプロイ(10 台、1 ゲートウェイ)
cat > /tmp/scale.tfvars <<'EOF'
gateways = { "gw-001" = ["cam-001", "cam-002", "cam-003", "cam-004", "cam-005", "cam-006", "cam-007", "cam-008", "cam-009", "cam-010"] }
EOF
AUTO_APPROVE=1 scripts/deploy.sh cc-tmp-scale -var-file=/tmp/scale.tfvars
mise exec -- scripts/provision-edge.sh gw-001
# 2. シャドウに 10 台を書き、エミュレーターとエッジを起動(ホストのポートは公開しない)
mise exec -- uv run --no-project --with boto3 python -I tools/scale/set_shadow.py true
docker compose -f tools/scale/compose.scale.yaml up -d --build
# 3. 計測(30 秒ごとの遅れとエッジ)、集計
mise exec -- uv run --no-project --with boto3 python -I tools/scale/sampler.py /tmp/samples.jsonl &
mise exec -- uv run --no-project --with boto3 python -I tools/scale/analyze.py <開始 ISO> <終了 ISO> label
mise exec -- uv run --no-project --with boto3 python -I tools/scale/phase.py <開始> <終了>        # 起動の秒の位置
mise exec -- uv run --no-project --with boto3 python -I tools/scale/trail.py <開始> <終了>        # GetDataEndpoint(CloudTrail、5〜15 分遅れる)
mise exec -- uv run --no-project --with boto3 python -I tools/scale/switch_sched.py sync|flex|rate  # スケジュールの比較
mise exec -- uv run --no-project --with boto3 python -I tools/scale/burst.py disable|fire          # 一斉起動(同じアカウントの API に影響する)
# 4. 後片付け
docker compose -f tools/scale/compose.scale.yaml down
AUTO_APPROVE=1 scripts/destroy.sh cc-tmp-scale -var-file=/tmp/scale.tfvars
mise exec -- uv run --no-project --with boto3 python -I tools/scale/leftovers.py                  # 残骸の確認
rm -rf edge/.certs/gw-001
```

スクリプトは project 名 `cc-tmp-scale` とカメラ 10 台を決め打ちにしている。
