# 全体を通す検証(E2E)

`scripts/e2e/run.sh` が、一時環境(既定 `cc-tmp-e2e`)を作ってからエッジ → KVS → Lambda → S3 / Athena → API → ビューワー(実ブラウザ)まで
順に確かめ、最後に削除して残骸がないことを確認する。各段階の合否は最後に表で出る。

## 前提

- `mise trust && mise install`(terraform、uv、node、Python)。AWS は `cloud-camera` プロファイル(mise の env)
- Docker(エミュレーターとエッジを compose で起動する。ffprobe もエミュレーターのイメージのものを使う)
- Google Chrome(`/Applications/Google Chrome.app`)。Playwright は `viewer/node_modules` のもの(build で `npm ci` される)
- WebKit も試すなら `cd viewer && mise exec -- npx playwright install webkit`
- ホストのポート 8080〜8083、8554、udp 8000/8001/3702 が空いていること(エミュレーター)
- project 名は `cc-tmp-*` を使う。`cloud-camera*` は拒否する

## 使い方

```sh
scripts/e2e/run.sh                      # cc-tmp-e2e で全部(最後に destroy まで)
KEEP=1 scripts/e2e/run.sh               # 最後に destroy しない(失敗の調査用)
SKIP_DEPLOY=1 KEEP=1 scripts/e2e/run.sh # 残した環境にチェックだけ回す(エッジは起動し直す)
DESTROY=1 SKIP_DEPLOY=1 scripts/e2e/run.sh   # 残した環境のチェック後に削除
DUMMY_MISSING=1 scripts/e2e/run.sh      # video_archiver / detector の zip がなければダミーで deploy(該当は SKIP)
BROWSERS=chrome,webkit scripts/e2e/run.sh
scripts/destroy.sh cc-tmp-e2e           # 手で消すとき(AUTO_APPROVE=1 で確認なし)
```

| 環境変数 | 既定 | 内容 |
| --- | --- | --- |
| `KEEP` / `DESTROY` | `0` / | `KEEP=1`(または `DESTROY=0`)で destroy しない |
| `SKIP_BUILD` / `SKIP_DEPLOY` | `0` | ビルド / deploy を飛ばす |
| `DUMMY_MISSING` | `0` | 足りない Lambda の zip を、`handler.lambda_handler` が何もしないダミーで作る(`build/.dummy-<name>` を置き、次回は作り直す) |
| `BROWSERS` | `chrome` | `chrome`(本物の Chrome)/ `webkit`(Playwright の WebKit)/ `chromium`(同梱版)。空で飛ばす |
| `ARCHIVE_TIMEOUT` / `DETECT_TIMEOUT` | `480` | S3 退避・物体検出を待つ秒数 |
| `E2E_OUT` | `$TMPDIR/cc-e2e/<project>-<時刻>` | ログ、結果、スクリーンショットの出力先(リポジトリの外) |
| `HEADED` | `0` | `1` でブラウザを画面に出す |
| `E2E_QUESTION` | `直近10分で人は映った?` | 検索の質問 |
| `E2E_SOURCE` | `file:/media/crosswalk_toronto.mp4` | エミュレーターの映像(`EMULATOR_SOURCE`)。物体検出で人が映るようにサンプル動画をループ再生する。なければ `emulator/scripts/fetch_sample_media.py` で取得する。`testsrc` で時刻入りのテストパターン |

出力先には `results.tsv`(表の元)、`run.log`、段階ごとのログ、`tf.json`(terraform output。証明書と秘密鍵は provision の後に消す)、
`edge-container.log` / `emulator-container.log`、`screenshots/<browser>-<mode>.png` が残る。

## 段階とチェックの意味

| 段階 | 確かめること | 失敗したら疑うところ |
| --- | --- | --- |
| `build` | `lambdas/detector/fetch_models.sh`(モデル。用意済みなら何もしない)→ `scripts/build-all.sh`(Lambda の zip と viewer/dist)。足りない zip はダミー(`DUMMY_MISSING=1`) | uv / npm、arm64 の wheel、モデルの取得 |
| `deploy` | `scripts/deploy.sh <project>`(`AUTO_APPROVE=1`)。terraform output を `tf.json` に保存 | Terraform、権限、名前の衝突 |
| `provision` | `scripts/provision-edge.sh gw-001` で `edge/.certs/gw-001/` に証明書と config.json | output の名前 |
| `edge-up` | `docker compose -p <project> up`(エミュレーター + エッジ、`CC_SINK=kvs CC_USE_SHADOW=true CC_PROJECT=<project>`) | ポートの衝突、イメージのビルド |
| `shadow` | シャドウの `desired.cameras.cam-001`(ONVIF は `http://emulator:8080/onvif/device_service`、`streaming: true`)を書く | IoT のエンドポイント |
| `kvs` | ListFragments(直近 2 分)で断片が 5 本以上、最新の ProducerTimestamp と壁時計の差が 20 秒以内、未来の時刻がない | エッジのログ(`edge-container.log`)、IoT の認証情報プロバイダー、ロールエイリアスの権限、時計 |
| `cameras` | `GET /api/cameras` で `online=true`、`reported.streaming=true`(codec / resolution / rtsp_url も表示) | MQTT 接続、ポリシー、シャドウの更新 |
| `api-kvs` | timeline(直近 10 分)に `source=kvs` の範囲がある。kvs-hls の LIVE / ON_DEMAND の URL を発行し、マスタープレイリストの `CODECS` と ffprobe で hevc を確認 | API の KVS 権限、断片がない(`NO_FRAGMENTS`) |
| `archive` | DynamoDB の区間の件に `video_status=DONE`(断片 55 本以上の 1 分まるごとの区間)、`files` の clip.mp4 / index.m3u8 を CloudFront の `/media/` から ffprobe で hevc、sprite.jpg が 1600×540 | Scheduler、video_archiver のログ(`/aws/lambda/<p>-video-archiver`)、lag |
| `detect` | `detect_status=DONE` で `detect_count > 0` の区間があり、`detections/dt=<日>/cam-001.parquet` があり、Athena で label ごとの件数が引ける(テストパターンでは検出 0 件で失敗する) | detector のログ、Glue のパーティションプロジェクション |
| `api-s3` | timeline に `source=s3` の範囲(と s3 → kvs の継ぎ目の秒数)、vod.m3u8 に EXT-X-MAP / PROGRAM-DATE-TIME / BYTERANGE があり ffprobe で hevc | API の DynamoDB の読み方、CloudFront の `/media/*` |
| `search` | `POST /api/search` が回答を返す。detector が本物なら SQL が実行されていること | Bedrock のモデルアクセス、Athena |
| `browser-<b>-live` / `-kvs` / `-s3` | CloudFront の URL を実ブラウザで開き、ライブ、直近の録画(処理済み位置より後、KVS ON_DEMAND)、録画(S3 の vod.m3u8)のそれぞれで、映像右上の再生元の表示が切り替わり、4 秒で currentTime が 1.5 秒以上進み、`videoWidth > 0`、`video.error` もエラー表示もないこと。スクリーンショットを撮る | 下の「ブラウザと H.265」 |
| `stop` | `PUT /api/cameras/cam-001/streaming {"streaming": false}` で `reported.streaming=false` になる | エッジのシャドウ差分の適用 |
| `edge-down` | compose を止める(ログを保存) | |
| `destroy` | `scripts/destroy.sh <project>`(`AUTO_APPROVE=1`) | バケットの中身、CloudFront の削除待ち |
| `residue` | タグ `Project=<project>`、S3、ロググループ、KVS、IoT(Thing、ポリシー、ロールエイリアス、deploy 時に控えた証明書 ID)、スケジュールグループ、DynamoDB、Glue、Athena、IAM ロール、CloudFront に残りがない(最大 3 分待つ) | 削除の失敗。手で消す |

どこかで失敗しても後続は続け(前提が欠けたものは FAIL になる)、`KEEP=1` でなければ最後に destroy する。
ビルドに失敗したときは何も作っていないので、そこで終わる。

## ブラウザと H.265

ブラウザの検証は `scripts/e2e/browser.mjs`(単独でも動く)。

```sh
mise exec -- node scripts/e2e/browser.mjs --url https://dxxxx.cloudfront.net --out /tmp/shots --browser chrome --modes live,kvs,s3 --s3-at <epoch ms>
```

- シークバーを「1時間」表示にし、`aria-valuemin` / `aria-valuemax` から狙った時刻の位置をクリックする(KVS は処理済み位置 + 5 秒か 90 秒前の遅い方、S3 は退避済み区間の頭 + 20 秒)
- 再生の方式は `video.currentSrc` で見分ける(`blob:` なら hls.js(MSE)、HLS の URL ならネイティブ)
- 最初に `MediaSource.isTypeSupported` / `canPlayType` の結果を `ENV` 行に出す

### 確かめた結果(2026-10-24、macOS 26.6、Apple Silicon、`cc-tmp-e2e`)

映像はエミュレーターの H.265(libx265、1280×720、15fps、キーフレーム 1 秒)をエッジが再エンコードせずに KVS へ送ったもの。

| ブラウザ | 方式 | ライブ(KVS LIVE) | 直近の録画(KVS ON_DEMAND) | 録画(S3 の vod.m3u8) |
| --- | --- | --- | --- | --- |
| Google Chrome 155(`channel: 'chrome'`、ヘッドレス) | hls.js(MSE) | 再生できる | 再生できる(※) | 再生できる |
| Playwright WebKit 27.2(Safari 相当) | ネイティブ HLS | 再生できる | 再生できる | 再生できる |

- Chrome: `MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L93.90"')` が true(hev1 も true)。macOS のハードウェアデコーダーを使う。
  ビューワーは UA で Safari 以外を hls.js にするので、Chrome は hls.js + MSE で再生する
- WebKit: `canPlayType` は `hvc1` だけ `probably`、`hev1` は空。KVS の HLS も S3 の clip.mp4 も `hvc1`(`hvcC`)なので再生できる
- KVS のマスタープレイリストの `CODECS` は `hvc1.1.6.L93.90`。ffprobe でも hevc / hvc1
- (※) 当初、ビューワーで KVS ON_DEMAND を開始位置付きで開くと Chrome で映像が出なかった(ローディングのまま)。
  マルチバリアントのプレイリストで `startLoad(-1)` した直後に `startLoad(pos)` をやり直すと、最初の断片の読み込みが中断されたまま
  SourceBuffer が作られずに止まる(hls.js 単体でも再現。単純に `loadSource` するだけなら再生できる)。読み込み開始後はシークで位置を合わせるよう直した
- 本物の Safari は未確認(`safaridriver --enable` に管理者権限が要る)。WebKit で動くので見込みは高いが、iOS を含め実機で確かめたい
- Firefox は H.265 非対応なので対象外(ビューワーが案内を出す)

## 所要時間(目安)

`BROWSERS=chrome,webkit`(全 Lambda が本物、サンプル動画)で全体 11 分。`DUMMY_MISSING=1` でも同じくらい(待ち時間はほぼ退避と CloudFront)。

| 段階 | 秒 |
| --- | --- |
| build(依存はキャッシュ済み) | 20〜30 |
| deploy(新規。CloudFront が大半) | 約 190 |
| provision + edge-up(イメージはキャッシュ済み) | 約 15 |
| kvs / cameras / api-kvs | 各 5〜15 |
| archive(最初の 1 分まるごとの区間が DONE になるまで) | 約 160 |
| detect(archive の後なので待たずに済むことが多い) | 2〜60 |
| browser(2 ブラウザ × 3 モード) | 約 40 |
| stop | 3 |
| destroy(CloudFront の無効化と削除が大半) | 約 210 |

エッジのイメージを初めてビルドするときは kvssink のビルドで 10 分以上かかる。

## よくある失敗と対処

| 症状 | 原因と対処 |
| --- | --- |
| build で `uv: command not found` | mise の外で呼んだ。`run.sh` は自分で `mise exec` し直す。`scripts/deploy.sh` も build を mise で実行するよう直した |
| `kvs` が「最新の断片が N 秒前」で失敗 | エッジが送っていない。`edge-container.log` を見る。シャドウの desired が反映されていない、IoT の認証情報プロバイダーの失敗、RTSP に届かない など |
| `kvs` で Producer と壁時計の差が大きい | Docker の VM の時計がずれている。Docker Desktop を再起動する |
| `cameras` で online が false / null | MQTT に繋がっていない(証明書、ポリシー、エンドポイント)。`CC_PROJECT` が project と違うと Thing 名がずれる(`run.sh` は合わせて渡す) |
| `archive` がタイムアウト | Scheduler が止まっている、video_archiver の失敗(`/aws/lambda/<p>-video-archiver`)。カーソルは「現在 − lag」から 5 分前で始まり、最初の区間は録画の途中から始まるので、断片 55 本以上の区間ができるまで 2〜3 分かかる |
| `browser-*-kvs` が「時間帯が短すぎる」 | 配信開始直後で、処理済み位置より後の KVS の範囲が 20 秒もない。`archive` の後に回しているので通常は起きない |
| `browser-chrome-*` で 0x0 のまま | Chrome が H.265 を再生できない(Chromium 同梱版、古い Chrome、ハードウェアデコーダーのない環境)。`ENV` 行の `isTypeSupported` を見る |
| `destroy` が `BucketNotEmpty` | スケジュールを消す前に起動していた video_archiver が、空にした後のバケットに書いた。`destroy.sh` は 1 回やり直すようにした。それでも残れば `scripts/destroy.sh <project>` をもう一度 |
| destroy を途中で失敗した後に再実行すると `BucketNotEmpty` | 途中の destroy で output が state から消え、空にするバケットが分からなかった。`destroy.sh` は state のバケットから名前を読むよう直した |
| ポート 8080 などが使用中 | ほかのエミュレーター(別のワークツリーなど)を止める。`docker ps` |
| `residue` で何か残る | タグ検索は削除直後に少し残ることがあるので 3 分待つ。残り続けるものは名前で手で消す(`cloud-camera-*` には触らない) |

## 補足

- エッジ側の compose は `-p <project>` で起動するので、ほかの compose(`cloud-camera` など)と混ざらない
- `stop` の後、シャドウの desired は `streaming: false` のまま残る(destroy で Thing ごと消える)
- 出力先の `tf.json` からは provision の後に証明書と秘密鍵を消す。`edge/.certs/gw-001/` は destroy の後に消す(その project のものだけ)
- 検索は「直近10分で人は映った?」に hits 10 件で答えたが、回答文の数値(「1秒間に約8.4人」など)は検出数を人数のように書くことがある(lambdas/api の README のとおり、モデルの癖)
