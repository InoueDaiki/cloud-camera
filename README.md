# cloud-camera

AWS のマネージドサービスとサーバーレスだけで、クラウドカメラの録画・再生・AI 検索の基盤を組んだときに、**カメラ 1 台・1 か月あたりいくらかかるか**を実測で明らかにする検証プロジェクト。

「エッジは薄く、クラウドはマネージドとサーバーレスだけ」という設計で、自前のサーバー(EC2 やコンテナの常駐)は持たない。

> **注意**: 検証用のコードで、API に認証はない。デプロイすると URL を知っている人は誰でも録画の閲覧や PTZ の操作、課金の発生する検索ができる。本番には使わず、検証が終わったら環境を削除すること。

- 検証の中で決めたこと・変えたことと、その理由: [docs/decisions.md](docs/decisions.md)
- コンポーネント間の約束事(名前、テーブル、API、MQTT): [docs/contracts.md](docs/contracts.md)

## アーキテクチャ

![カメラの映像は、エッジから Kinesis Video Streams、Lambda を経て S3 に保存される。ビューワーは CloudFront 経由で録画と API を使い、ライブは WebRTC でエッジとつなぐ](docs/architecture.svg)

### 構成要素

| 層 | 使うもの | 役割 |
| --- | --- | --- |
| カメラ | ONVIF 対応カメラ(開発では [ONVIF エミュレーター](emulator/README.md)) | H.265 の main と H.264 の sub を RTSP で出す。キーフレーム間隔は 1 秒 |
| エッジ | [エッジのコンテナ](edge/README.md)(arm64 / amd64) | カメラの探索と設定(ONVIF)、録画の中継(kvssink)、ライブの中継(KVS WebRTC)、IoT Core との通信。**再エンコードしない** |
| 取り込み | Kinesis Video Streams | 受け口と直近 1 時間の置き場。長期保存はしない |
| 制御 | IoT Core | 設定(クラシックシャドウ)、死活と単発コマンド(MQTT)、KVS 用の一時認証情報(認証情報プロバイダー) |
| 加工 | Lambda × 2(Python 3.13、arm64、zip) | 1 分ごとに起動し、KVS から S3 へ動画を退避(video_archiver)、物体検出(detector) |
| 状態 | DynamoDB 1 テーブル | 「どこまで処理したか」と 1 分区間ごとの結果 |
| 保存 | S3 | 動画・プレイリスト・スプライト、検出結果の Parquet(どちらも 30 日で削除) |
| 検索 | Glue Data Catalog、Athena、Bedrock | 自然言語の質問 → 検出結果の検索 → 回答 |
| 配信 | CloudFront、API Gateway(HTTP API)+ Lambda | ビューワー、メディア、API を同じドメインで配る |
| ビューワー | [React + Vite + Mantine](viewer/README.md) | ライブ、録画の再生、タイムライン、検索、PTZ |
| IaC | [Terraform](infra/README.md)(aws、awscc プロバイダー) | 上のすべて。エッジの証明書も発行する |

### 映像の流れ

1. エッジがカメラを ONVIF で見つけ、H.265 のプロファイルの RTSP を受ける。どのカメラを流すかは IoT Core のシャドウ(desired)で決まる
2. kvssink がフレームを再エンコードせずに KVS に送る(`rtph265depay ! h265parse ! h265timestamper ! kvssink`)。フラグメントはキーフレームごと(1 秒)で、時刻はゲートウェイの壁時計(プロデューサー時刻)。認証は IoT の証明書から得る一時認証情報
3. 回線が切れても、kvssink のメモリ上のバッファ(約 11 分)に溜め、復旧後に古い順に送り直す。それより長い断は欠落として扱う(ディスクには溜めない)

### 1 分ごとの処理

EventBridge Scheduler がカメラごとに video_archiver と detector を毎分起動する。KVS への到着をきっかけにはしない(エッジの追加作業が要らず、失敗しても次の起動が追いつく)。

- **処理済み位置とリース**: カメラ・処理ごとに DynamoDB に「この時刻より前はすべて確定」という位置を持ち、そこから 1 分区間ずつ進める。二重起動はリース(条件付き更新)で防ぐ。書く順序は S3 → 区間の件 → 処理済み位置なので、途中で落ちても次の起動がやり直すだけで済む
- **区間を確定させる条件(ウォーターマーク)**: KVS に届いた最新の断片のプロデューサー時刻が区間の終わりを越えたら処理する。kvssink は回線復旧後に古い順に送るので、それより前の断片はすべて届いている。届かないまま区間の終わりから 720 秒過ぎたら、届いた分で確定する。各区間の処理は 1 回だけ
- **video_archiver**: 区間の HLS(fMP4)の断片を KVS から取り出し、連結して `clip.mp4` にする。`index.m3u8` はその中をバイト範囲で指す。キーフレームをデコードして 1 秒 1 コマのスプライト(10 × 6)を作る。ffmpeg 本体は使わず、デコーダだけをビルドした PyAV(LGPL)を同梱する
- **detector**: video_archiver が置いた `clip.mp4` を読み、キーフレーム(1 秒に 1 枚)をデコードして YOLOX-Tiny(Apache-2.0、INT8、入力幅 640)を ONNX Runtime で推論する。結果はその日の Parquet に追記する(該当区間の行を除いてから足し、条件付き PUT で書く)。video_archiver の処理済み位置を越えない
- 1 分区間の途中で映像形式(H.265 / H.264)が切り替わった場合は、部分ごとに `clip.mp4`、`clip.1.mp4` … に分けて保存する

### 再生

ビューワーは見ている時刻によって再生元を切り替える。どこから再生しているかは映像の右上に小さく出る。

| 見ている時刻 | 再生元 | 遅延 |
| --- | --- | --- |
| ライブ | KVS WebRTC(H.265 を受けられないブラウザは sub の H.264)。つながらなければ KVS の HLS(LIVE) | WebRTC 約 0.1〜0.4 秒、HLS 約 3〜5 秒 |
| 直近で、まだ S3 に退避していない区間 | KVS の HLS(ON_DEMAND) | — |
| 退避済みの区間 | S3 の `clip.mp4` を連結したプレイリスト(API が作る `vod.m3u8`)を CloudFront から | — |

- タイムラインは API(`GET /api/cameras/{id}/timeline`)が、処理済み位置より前は DynamoDB、後は KVS の ListFragments から作る。録画あり・なし・処理失敗を色分けし、スプライトでサムネイルを出す
- WebRTC は、API がブラウザ用の署名付き URL と ICE サーバーを発行する(ブラウザに AWS の認証情報は渡さない)。映像はエッジとブラウザの間を直接(P2P)か TURN 経由で流れ、KVS は接続の仲介だけをする
- H.265 の再生はブラウザと端末で対応が分かれる(録画で日本の利用者の約 8〜9 割、WebRTC で約 6〜7 割の見込み。[docs/browser-hevc.md](docs/browser-hevc.md))。カメラごとにビューワーから H.264 に切り替えられる(費用は約 1.15〜1.6 倍。[docs/codec.md](docs/codec.md))

### 自然言語検索

1. ビューワーが質問(例「昨日の夕方に玄関に来た人は?」)を `POST /api/search` に送る
2. api の Lambda が Bedrock(既定は Nova 2 Lite)の Converse をツール付きで呼ぶ。ツールは「条件を引数で受けて、検出をイベント(同じカメラ・ラベルで 60 秒以内に続いたもの)にまとめて返す」ものと、自由な SQL(SELECT だけ、`detections` テーブルだけ、日付の範囲が必須などの検査付き)
3. Athena が Glue のテーブル(日付のパーティションプロジェクション)を通して S3 の Parquet を引く
4. 回答と、該当時刻の一覧(クリックでその時刻から再生)を返す

評価用の質問 62 問と採点の仕組みは [lambdas/api/README.md](lambdas/api/README.md)。

### 制御(IoT Core)

- **シャドウ**: ゲートウェイ(Thing)ごとに 1 つ。desired にカメラの設定(ONVIF の URL、配信の有無、映像形式、WebRTC の有無)、reported にエッジの実際の状態(配信中か、コーデック、解像度、PTZ の位置、WebRTC の状態、`online` と `last_seen`)
- **MQTT**: `{project}/{gateway}/status`(30 秒ごとの死活とバッファなどの計測値、切断時は LWT)、`/cmd`(探索、PTZ などの単発コマンド)、`/discovery`、`/ptz`
- **PTZ**: ビューワー → API → cmd トピック → エッジ → ONVIF の PTZ。WebRTC のライブで、操作から映像の変化まで約 0.8〜0.9 秒。エミュレーターは元映像からの切り出しを動かす疑似 PTZ

## 費用の目安

`tools/cost` の机上計算で、東京リージョン、1 Mbps、毎秒の物体検出、月 2 時間の視聴と 100 件の質問を仮定すると、**1 台・月 約 $25**。主な内訳は S3 の保存、KVS の取り込みと HLS の取り出し、物体検出の Lambda。実際の環境で長時間動かした実測はこれから。詳しくは [tools/cost/README.md](tools/cost/README.md)。

台数を増やしたときに当たる上限と、その対応は [docs/scale.md](docs/scale.md)。

## ディレクトリ

| パス | 中身 |
| --- | --- |
| [emulator/](emulator/README.md) | ONVIF カメラのエミュレーター(WS-Discovery、Device、Media、Media2、PTZ、RTSP。映像はテストパターン・動画・ブラウザのカメラ・外部ストリームから選べる。操作画面 `http://localhost:8090/`) |
| [edge/](edge/README.md) | エッジのコンテナ(エージェント、kvssink、WebRTC master)と Raspberry Pi への配置手順 |
| [lambdas/common/](lambdas/common/README.md) | 処理済み位置・リース・ウォーターマークの共通処理、キーフレームのデコード |
| [lambdas/video_archiver/](lambdas/video_archiver/README.md) | 動画の退避 |
| [lambdas/detector/](lambdas/detector/README.md) | 物体検出(モデルの選定とベンチマークを含む) |
| [lambdas/api/](lambdas/api/README.md) | ビューワー向け API と自然言語検索 |
| [infra/](infra/README.md) | Terraform |
| [viewer/](viewer/README.md) | ビューワー |
| [tools/cost/](tools/cost/README.md) | 単価表、机上計算、実測の収集 |
| [tools/codec/](docs/codec.md) | H.264 / H.265 の画質とビットレートの測定(VMAF)、実 KVS での比較。結果は `tools/codec/results/` |
| [tools/scale/](docs/scale.md) | 台数を増やしたときの計測(10 カメラのエッジ、遅れの記録、起動の分析)。結果は `tools/scale/results/` |
| [tools/browser-check/](docs/browser-hevc.md) | ブラウザの H.265 対応の確認ページと、Playwright での一括確認 |
| [tools/emulator-webcam-check/](emulator/README.md#操作画面とブラウザのカメラ) | エミュレーターにブラウザのカメラを WHIP で送り込めることを Playwright(偽のカメラ)で確認 |
| [scripts/](scripts/) | ビルド、デプロイ、削除、テスト、E2E(運用の手順。計測・調査の道具は `tools/`、コンポーネント固有の道具は各コンポーネントの `tools/`・`scripts/`・`bench/`) |
| [docs/](docs/) | 決定事項、契約、調査と計測の記録(WebRTC、ブラウザ、コーデック、台数、E2E) |

## セットアップ

```sh
mise trust && mise install
```

terraform、Python、Node.js、uv などのバージョンは `mise.toml` で管理する。AWS CLI のプロファイル `cloud-camera`、リージョン `ap-northeast-1` も `mise.toml` で設定される。ビルドには Docker が要る(Lambda に入れる最小構成の FFmpeg と、エッジのイメージ)。

## よく使うコマンド

| やること | コマンド |
| --- | --- |
| 全コンポーネントの単体テストと静的検査 | `scripts/test-all.sh` |
| ビルド(Lambda の zip とビューワー) | `scripts/build-all.sh` |
| デプロイ / 削除 | `scripts/deploy.sh [PROJECT]` / `scripts/destroy.sh [PROJECT]` |
| エッジの証明書と設定を書き出す | `PROJECT=<project> scripts/provision-edge.sh` |
| エミュレーターとエッジをローカルで起動 | `docker compose up --build`(詳細は [edge/README.md](edge/README.md)) |
| コストの机上計算 | `cd tools/cost && mise exec -- uv run python -m cost desk` |
| 全体を通す検証(E2E。一時環境を作って消す) | `scripts/e2e/run.sh [cc-tmp-*]`(詳細は [docs/e2e.md](docs/e2e.md)) |

検証用の環境は `cc-tmp-` で始まる名前で作り、終わったら必ず削除する。このアカウントは Lambda の同時実行の上限が 10 なので、環境は 1 つずつ動かす。

## ライセンス

Copyright (c) 2026 InoueDaiki. All rights reserved.

ライセンスは設定していません。`emulator/tests/wsdl/` の ONVIF のスキーマなど、第三者のファイルはそれぞれの著作権表示に従います。
