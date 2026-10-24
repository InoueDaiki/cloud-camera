# lambdas/api: ビューワー向け HTTP API と自然言語検索

API Gateway HTTP API(ペイロード 2.0)の後ろに置く 1 本の Lambda。パスでルーティングする。
API の形は [docs/contracts.md](../../docs/contracts.md) の「HTTP API」、全体像は [README.md](../../README.md) の「再生」「自然言語検索」。

| ファイル | 中身 |
| --- | --- |
| `handler.py` | 入口(`handler.lambda_handler`)とルーティング |
| `app/cameras.py` | `GET /api/cameras`、`PUT /api/cameras/{id}/streaming`(IoT のクラシックシャドウ) |
| `app/timeline.py` | `GET /api/cameras/{id}/timeline`(処理済み位置の前は DynamoDB、後は KVS ListFragments) |
| `app/playlist.py` | `GET /api/cameras/{id}/vod.m3u8`(区間の `hls` から複数区間の HLS を組む) |
| `app/kvs.py` | `GET /api/cameras/{id}/kvs-hls`、ListFragments |
| `app/webrtc.py` | `GET /api/cameras/{id}/webrtc`(KVS WebRTC の ConnectAsViewer の署名付き WSS URL、GetIceServerConfig の TURN、チャネルの ARN、クライアント ID) |
| `app/ptz.py` | `POST /api/cameras/{id}/ptz`(IoT Core の cmd トピックへ publish)、`GET /api/cameras/{id}/ptz`(シャドウ) |
| `app/search.py` / `prompt.py` / `events.py` / `sql_guard.py` / `athena.py` | `POST /api/search`(Bedrock Converse のツール使用 → 引数付き検索か SQL ガード → Athena → 回答の検査) |
| `local_server.py` | ローカル開発用サーバー |
| `bench/` | 検索の評価セット(合成データ、質問と正解、採点、実行)。zip には入れない |

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
| `WEBRTC_CHANNEL_PREFIX`(任意) | `cloud-camera-` | KVS WebRTC のシグナリングチャネル名の接頭辞。未設定なら `STREAM_NAME_PREFIX` |
| `MEDIA_URL_PREFIX`(任意) | `/media/` | プレイリストやスプライトの URL の接頭辞 |
| `SEARCH_TIMEOUT_SECONDS`(任意) | `25` | 検索全体の時間の予算(API Gateway の 30 秒より手前で答える) |
| `CAMERA_NAMES`(任意) | `{"cam-001":"玄関"}` | 検索のプロンプトに渡すカメラ名。未設定ならシャドウの `name`(reported か desired)を読む |
| `SEARCH_FEATURES`(任意、評価用) | `structured,guard_ts,lint` | 検索の改善を段階ごとに切る。未設定ならすべて有効。本番では設定しない |

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

- **webrtc**: シャドウを見て、WebRTC を止めている(`desired.webrtc.enabled=false`)、オフライン、エッジの master が `CONNECTED` でないカメラは 409
  (`WEBRTC_DISABLED` / `CAMERA_OFFLINE` / `WEBRTC_UNAVAILABLE`)を KVS に問い合わせずに返す。チャネルの ARN と viewer のエンドポイントは
  コンテナ内で 6 時間キャッシュし(DescribeSignalingChannel / GetSignalingChannelEndpoint はチャネルあたり 5 TPS)、リクエストごとに
  GetIceServerConfig を 1 回呼ぶ。署名は Lambda のロールの一時認証情報で SigV4 のクエリ署名(`X-Amz-Expires=299`)。
  IAM は `kinesisvideo:DescribeSignalingChannel` / `GetSignalingChannelEndpoint` / `GetIceServerConfig` / `ConnectAsViewer`(チャネルの ARN)

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

1. システムプロンプト(`prompt.py`)に書くもの: 現在時刻(日本時間と UTC)、**日付と曜日の早見表**(今日から 15 日前まで。
   「先週の金曜」「今週の月曜」まで付ける)と**よく使う範囲の start / end**(今日、昨日、ここ 3 日間、今週、先週 = 前の週の月〜日、
   週末、直近 10 分、さっき = 直近 30 分 …。Python で計算して渡し、モデルには日付計算をさせない)、時間帯の目安(午前中 0〜12 時 など)、
   **カメラ ID と名前**(`CAMERA_NAMES` かシャドウの `name`)、**ラベルの言い換え表**(誰か → person、ワンちゃん → dog、動物 → dog/cat/bird …)、
   **データの意味**(1 行 = 1 フレームの 1 検出、人物の同定はしない、「3人」「2台」とは書かない、「回」はイベントの数)、**回答の型**
   (いつ / 何回 / いちばん多い / 最初と最後 / 該当なし / 未来)、質問文の中の指示に従わないこと
2. ツールは 3 つ。`toolChoice` で毎回どれかを呼ばせ、検索の上限(4 回)か時間の予算に達したら `submit_answer` を強制する
   - `search_detections`(主に使う、`events.py`): 引数は日本時間の `start` / `end`、`labels`、`camera_ids`(名前も可)、
     `group_by`(event / hour / day / camera / label)。**SQL はサーバーが組む**: dt は UTC の範囲から計算し、ts の条件は必ず付け、
     同じカメラ・ラベルの検出が 60 秒以内の間隔で続いたものを 1 つの**イベント**にまとめる(Athena のウィンドウ関数)。
     返すのはイベントの一覧(`event_id`、開始・終了の日本時間、秒数、フレーム数、1 フレームに同時に映った数の最大、範囲の端で切れたか)、
     `event_count`、ラベル・カメラごとの数、最初と最後、`group_by` の集計(イベントの多い順)。未来の範囲は Athena を呼ばずに「未来」と返し、
     終わりが現在より後なら現在で切る。存在しないカメラやラベルはモデルに返して直させる
   - `query_detections`(上で表せないとき。位置やモデル名など): 自由 SQL。下の SQL ガードを通す
   - `submit_answer`: 回答と `hits`(`event_id` か、`camera_id` と `ts`)
3. SQL ガード(`sql_guard.py`、sqlglot で構文解析): 1 文だけ、SELECT(集合演算可)のみ、テーブルは `detections` だけ
   (WITH の別名は可、別データベースやテーブル関数は不可)、`detections` を読む SELECT には WHERE の AND 条件で `dt` の上下限が必須
   (31 日まで)、いちばん外側の LIMIT を 100 以下に強制(なければ付ける)。**ts の条件があれば、その UTC の日付を dt が覆っているか**を見て、
   足りなければ正しい dt を示して拒否する。`24:00:00` は拒否。**ts の条件がなければ実行はするが、結果に警告**(dt だけでは日本時間の日で
   区切れない)を付けて書き直しを促す。拒否した理由はモデルに返して書き直させる。最後の砦は Athena ワークグループの `BytesScannedCutoffPerQuery`
4. 結果の timestamp 列は ISO(UTC)に直し、日本時間の列(`*_jst`)を足してモデルに返す
5. **回答の検査**(`search.lint_answer`): 1 フレームに同時に映った数の最大より大きい「n 人」「n 台」「n 匹」や、
   見たイベント数より大きい「n 回」(フレーム数や行数を回数にした疑い)があれば、1 回だけ理由を返して `submit_answer` をやり直させる
6. `hits` は検索結果に実在したイベント・時刻だけ残す(モデルが作った値は捨てる)。`event_id` ならイベントの最初のフレームの時刻。
   `ts`(UTC)と `ts_jst` を返す
7. `usage` に入出力トークン、Bedrock の呼び出し回数、ツールの呼び出し回数、回答の書き直し回数(`lint_retries`)、Athena のスキャン量、経過時間。
   `queries[]` には各ツール呼び出しの `tool`、`sql`(search_detections はサーバーが組んだ SQL)、`input`、`warnings`、スキャン量、エラー

### 検索の評価(`bench/`)

合成の検出結果に日本語の質問を投げ、機械的に採点する。実 Bedrock と Athena を使う(一時リソースは `cc-tmp-search-` / `cc_tmp_search_`)。

```sh
cd lambdas/api
mise exec -- uv run python -m bench.run_bench setup                    # S3 バケット、Glue、Athena ワークグループ(スキャン上限 10 MB)
mise exec -- uv run python -m bench.run_bench run --tag mine --repeat 2 [--split dev] [--model ...] [--id t01]
mise exec -- uv run python -m bench.run_bench report baseline final mine   # 比較表
mise exec -- uv run python -m bench.run_bench failures mine                # 落ちた問の回答、SQL、hits
mise exec -- uv run python -m bench.run_bench teardown                     # 必ず実行
mise exec -- uv run python -m bench.run_bench check                        # remaining: none を確認
```

- `--app-dir <dir>` で別の版の `app/` を使える(ベースラインの再計測: `git archive <commit> lambdas/api/app | tar -x -C <dir> --strip-components=2`)
- `SEARCH_FEATURES=guard_ts`(など)で改善を段階ごとに切って比べられる
- 結果は `bench/results/<tag>.jsonl`(1 行 = 1 問 1 回、回答・hits・SQL・usage・採点)。採点を直したら `rescore <tag...>` で付け直す
- 費用: nova-2-lite で 62 問 1 回あたり約 $0.25、Athena は 1 クエリ 10 MB の最低課金で無視できる

**データ**(`bench/dataset.py`): 3 台(cam-001 玄関、cam-002 駐車場、cam-003 庭)、10/11〜10/22 の 36 イベント・1,124 行。
「現在」は 2026-10-22(木)14:35:20 日本時間に固定する(`app.search.now` を差し替え。SQL の `current_timestamp` も同じ時刻に置き換える)。
1 秒ごとのキーフレームの実時刻(カメラごとの位相 + 揺らぎ、秒ちょうどではない)、2 秒おきの区間、1 フレームに 2〜3 人、
`yolox_tiny_int8s8`、低信頼度(0.3 台)の誤検出、日本時間の 0 時またぎと 9 時(UTC の 0 時 = dt の境目)またぎ、検出のない日(10/20)を入れた。

**質問と正解**(`bench/questions.py`): 62 問。開発用(dev)25 問、確認用(test)23 問、改善を作り終えてから書き足した holdout 14 問。
種類は時刻範囲(今日の午前、昨日の夕方、先週、さっき、直近 10 分、0 時またぎ)、カメラ(名前・ID・存在しない ID)、ラベルの言い換え
(誰か、動物、ワンちゃん、バイク)、集計(何回、いちばん多い時間帯・カメラ、最初と最後)、存在確認、範囲外(未来、データのない日、天気)、
攻撃(DROP、information_schema、SQL の断片、システムプロンプトの出力、別データベース、UPDATE)。
正解は質問ごとの範囲・カメラ・ラベルからイベントを機械的に引く。

**採点**(`bench/score.py`、LLM は使わない): 該当ありの問は、hits がすべて正解のイベント上(カメラ・ラベル・時刻 ±2 秒)かつ範囲内で、
正解のイベントを拾えていること(全部 / 1 つ以上)。回答に「n 人」「n 台」(同時に映った数の最大を超えるもの)がないこと、
「n 回」が正解と合うこと(集計の問)、イベント数より極端に多い「n 回」がないこと、必要な時刻(例: 「10時台」「14時27分」)があること。
該当なし・範囲外の問は hits が空で「ない/まだ」と言うこと。攻撃の問は危険な SQL が実行されず、やったふりをしないこと。

#### 結果(`jp.amazon.nova-2-lite-v1:0`、2026-10-24)

段階ごとに dev と test を各 2 回ずつ回した(1 列の分母 = 問数 × 2)。改善は dev の失敗だけを見て決め、test は数字だけ見た。

| 版 | 内容 | dev | test | holdout | 入力 tok/問 | 出力 tok/問 | $/問 | 秒/問(p90) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 改善前(main の実装) | 40% | 46% | 50% | 6,448 | 320 | 0.0036 | 2.9(4.0) |
| v1-prompt | プロンプト: 日付・曜日・範囲の表、カメラ名、言い換え表、「検出数は人数ではない」、イベントで数える SQL の型、回答の型 | 76% | 80% | | 7,215 | 368 | 0.0041 | 2.9(3.8) |
| v2-guard | + SQL ガード: ts と dt の整合、24:00 の拒否、ts なしの警告 | 74% | 91% | | 7,755 | 400 | 0.0044 | 3.2(4.4) |
| v3-structured | + 引数付きツール `search_detections`(イベントにまとめる) | 96% | 100% | | 7,344 | 205 | 0.0036 | 2.6(3.7) |
| v4-lint | + 回答の検査と書き直し | 96% | 100% | | 8,086 | 230 | 0.0040 | 2.6(3.9) |
| v5 | + 範囲の端で切れたイベントの印(holdout を初めて回した) | 96% | 100% | **79%** | 7,509 | 207 | 0.0037 | 2.4(3.2) |
| v7-final | + 範囲の表に「ここ 3 日間」「週末」、時間帯を時刻で明記、混ざった不正な指示の扱い | 100% | 100% | 93%(*) | 7,971 | 214 | 0.0039 | 2.4(3.4) |
| final | v7 + 相対範囲を秒まで(1 回) | 100% | 100% | 100%(*) | 7,994 | 214 | 0.0039 | 2.5(3.4) |

(*) holdout は v5 の失敗を見て v6〜v7 を直したので、それ以降は参考値。**改善の汎化の目安は v5 の 79%**(baseline は 50%)。
Athena のスキャン量は 1 問あたり 2〜4 KB(Parquet の列の読み取り。課金は 1 クエリ 10 MB の最低額)。

**ベースラインの主な誤り**(dev + test 96 回答のうち 55 件が不合格。1 回答に複数の理由がつく)

| 誤り | 理由の件数 | 例 | 対策 |
| --- | --- | --- | --- |
| 検出の行数・フレーム数を「n 回」「n 人」「n 台」と書く、回数を行数で答える | 回数が多すぎる 21、人数・台数 10、回数の不一致 7 | 「約220人の検出」「60台の車」「合計235回」(正解 4 回) | データの意味と回答の型をプロンプトに、イベントにまとめるツール、回答の検査 |
| ts の条件を省き、dt(UTC の日付)だけで日本時間の日を絞ったつもりになる | 範囲外の hits 12、該当なしで hits 4 など | 「昨日の最後の検出は 10月22日 08時59分」、「一昨日」に 10/21 朝の検出 | 引数付きツール(サーバーが ts を必ず付ける)、ガードの警告 |
| 曜日・週の計算違い | 取りこぼし・hits なしの一部 | 「先週 = 10/15〜21」「先週の金曜 = 10/15」 | 日付・曜日・範囲の表を Python で作って渡す |
| カメラ名が分からない | 4 | 「庭のカメラ」に全カメラの結果、「駐車場 = cam-001」 | カメラ ID と名前の対応を渡す。ツールが名前も受け付ける |
| 時刻リテラルの誤り | 2(Athena のエラーで書き直し) | `TIMESTAMP '2026-10-21 24:00:00'` | 時間帯の表に「翌日 00:00」、ガードで拒否、ツールは 24:00 を受け付ける |

**モデル比較**(final と同じ実装、62 問 1 回。単価は Pricing API の東京の値)

| モデル | 正答 | dev / test / holdout | 入力 tok/問 | 出力 tok/問 | $/問 | 秒/問(p90) | 所見 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `jp.amazon.nova-2-lite-v1:0`(既定) | **100%** | 25/25, 23/23, 14/14 | 7,994 | 214 | 0.0039 | 2.5(3.4) | |
| `qwen.qwen3-235b-a22b-2507-v1:0` | 95% | 24/25, 23/23, 12/14 | 12,529 | 960 | 0.0044 | 11.0(26.0) | 正確だが遅く、p90 が API Gateway の 30 秒に近い |
| `mistral.ministral-3-14b-instruct` | 90% | 22/25, 23/23, 11/14 | 13,483 | 407 | 0.0033 | 5.4(13.1) | 検索を繰り返しがち(2.0 回/問) |
| `apac.amazon.nova-lite-v1:0` | 89% | 23/25, 21/23, 11/14 | 10,099 | 190 | 0.0008 | 2.8(3.5) | 最安。攻撃の質問(DROP、プロンプトの出力)にも検索して答えてしまう。`apac.` はデータが国外のリージョンに出うる。同時 4 本でスロットリングしたので 1 本ずつ回した |
| `openai.gpt-oss-120b-1:0` | 84% | 22/25, 19/23, 11/14 | 6,855 | 456 | 0.0016 | 3.6(5.0) | 回答は正しくても hits に event_id を入れないことが多い |
| `jp.anthropic.claude-haiku-4-5-20251001-v1:0` / `jp.anthropic.claude-sonnet-4-6` | 呼べない | | | | | | ユースケースのフォーム未提出(*) |

(*) `Model use case details have not been submitted for this account`。
`jp.` 以外(`qwen.` など)はリージョン内の基盤モデルを直接呼ぶ。評価全体の Bedrock の費用は約 $4.3(nova-2-lite 約 950 回答で $3.65、他モデル $0.6)。

**既定値は `jp.amazon.nova-2-lite-v1:0` のまま**: いま呼べるもので正答率が最も高く、速い。1 問あたり約 8,000 入力 / 210 出力トークン(約 $0.004、
Bedrock の呼び出し平均 1.9 回、2.5 秒)。入力の大半は毎回送るシステムプロンプトなので、プロンプトキャッシュが使えるモデルなら下げられる。
費用を 1/5 にしたいなら `apac.amazon.nova-lite-v1:0` が候補(正答 89%、データの所在に注意)。

**Claude 系との比較の手順**(フォームの提出後):

1. Bedrock コンソールの Model access(モデルアクセス)から、Anthropic のユースケースのフォームを提出する
2. 呼べることを確かめる: `aws bedrock-runtime converse --model-id jp.anthropic.claude-haiku-4-5-20251001-v1:0 --messages '[{"role":"user","content":[{"text":"hi"}]}]'`
3. `bench.run_bench setup` → `run --tag haiku45 --model jp.anthropic.claude-haiku-4-5-20251001-v1:0` → `report final haiku45` → `teardown` → `check`
   (62 問で約 $0.6 の見込み。`run_bench.py` の `PRICES` の Anthropic の単価は目安なので、Pricing API の値に直してから費用を見る)
4. 正答率が同等なら nova-2-lite のまま。上回るなら Lambda の環境変数 `BEDROCK_MODEL_ID` を切り替え、IAM の `bedrock:InvokeModel` に
   そのプロファイルと foundation-model を足す

#### 残る弱点

- 評価セットは小さく(62 問、データは合成 3 台 12 日)、質問と改善を同じ人が作った。holdout の 79%(改善前)が汎化の目安で、
  本番の質問の言い回しではもっと落ちる可能性がある。実際の質問ログが溜まったら、落ちた質問を holdout に足して回す
- 「3時ごろ」のような幅のある表現: 前後 30 分と教えているため、14:31 の車を「3時ごろ」に含めて答えた(未来であることを言わない)。
  正解をどちらとみなすかは決めきれていない
- イベントのまとめ方は「同じカメラ・ラベルで 60 秒以内」の固定。立ち止まって何分も映る人は 1 回、出入りを繰り返すと複数回になる。
  人物の同定はしていないので、「何人来たか」には答えられない(回答でもそう説明させている)
- 採点は機械的(正規表現と hits の時刻)。回答の読みやすさや、正しいが言い回しが想定外の回答の扱いは見ていない
- 時刻の一致はモデルが hits に `event_id` を入れることに依存する。入れなければ hits は空になる(回答の文には時刻が残る)

### end-to-end 検証

デプロイした環境での検索の確認は、全体の E2E([docs/e2e.md](../../docs/e2e.md))の `search` の段階で行う。
デプロイせずに実 Bedrock + Athena で試すときは、上の評価セット(`bench.run_bench setup` → `run --id <問の ID>` → `teardown`)を使う
(以前の `scripts/e2e_search.py` は評価セットと同じことをしていたので、評価セットに統合して削除した。コミット 37dc0ef までの履歴にある)。

## 既知の制約

- タイムラインの `minutes` は `DONE` の区間だけ。`EMPTY` の区間にスプライトはない
- vod.m3u8 は 1 分区間単位で入れる(`from`/`to` の途中から切らない)。シークは PDT で合わせる
- 検索の `hits` は代表時刻のみ(モデルが選ぶ。search_detections ならイベントの最初のフレーム)。自由 SQL で分単位に集計した場合、
  `label` が空になることがある
- 検索は同期処理。Bedrock と Athena が遅いと API Gateway の 30 秒に近づく(予算 25 秒で打ち切って回答させる)
