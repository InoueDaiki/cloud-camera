# lambdas/video_archiver

KVS の映像を 1 分区間ごとに S3 へ退避する Lambda。EventBridge Scheduler から 1 分ごとに `{"camera_id": "cam-001"}` で呼ばれる。
区間ループ、リース、処理済み位置は [lambdas/common](../common/README.md) の `run_pipeline("VIDEO", ...)` に任せ、ここは 1 区間の処理だけを持つ。

| ファイル | 中身 |
| --- | --- |
| `handler.py` | `lambda_handler`(環境変数を読んで `run_pipeline` を呼ぶ) |
| `archiver.py` | 1 区間の処理(断片の一覧、clip.mp4、index.m3u8、sprite.jpg、DynamoDB の属性) |
| `build.sh` | `build/video_archiver.zip`(リポジトリ直下)を作る。Pillow(arm64)と `lambdas/common` を同梱 |
| `testing/` | 実 KVS での検証用の道具(H.265 送信コンテナ、調査スクリプト、ローカル実行) |

## 環境変数

| 名前 | 内容 | 既定 |
| --- | --- | --- |
| `STATE_TABLE` | DynamoDB `{p}-state` | 必須 |
| `MEDIA_BUCKET` | `{p}-media-{account_id}` | 必須 |
| `STREAM_NAME_PREFIX` | KVS ストリーム名の接頭辞(`{p}-`) | 空 |
| `LAG_SECONDS` | 処理対象の終わり(現在 − lag を 1 分に切り捨て)。断片が出そろったかはウォーターマークで判定するので短くてよい | 30 |
| `MAX_MINUTES` | 1 回で処理する区間数の上限 | 5 |
| `MAX_ATTEMPTS` | 1 区間の試行回数の上限 | 3 |
| `MEDIA_RETENTION_DAYS` | `expires_at` の日数(S3 のライフサイクルと揃える) | 30 |
| `MAX_LATE_SECONDS` | ウォーターマークが区間の終わりを越えないとき、区間の終わりから最大何秒待つか(共通処理が読む。エッジのバッファ上限 + 余裕) | 720 |

推奨設定: メモリ 256〜512 MB、タイムアウト 60 秒(下の実測を参照)。

## IAM

- `kinesisvideo:GetDataEndpoint`、`ListFragments`、`GetImages`、`GetHLSStreamingSessionURL`、`GetMP4InitFragment`、`GetMP4MediaFragment`(ストリーム `{p}-*`)
- `s3:PutObject`(メディアバケット)
- `dynamodb:UpdateItem`(状態テーブル)

## 1 区間の処理

0. 断片が出そろったか(ウォーターマーク)は `run_pipeline` が判定してから呼ぶ([common の README](../common/README.md#ウォーターマーク区間を処理してよいか))
1. **ListFragments**(プロデューサー時刻)で断片を取る(`run_pipeline` が起動ごとに 1 回取った結果のキャッシュから返る。同じ ProducerTimestamp の再送は 1 つにする)。断片がなければ `EMPTY`(`segments: []`、`fragment_count: 0`、`bytes: 0`)。S3 には何も書かない
2. **clip.mp4**: HLS ON_DEMAND(fMP4)セッションを開き、init と、1. で選んだ断片をそのまま連結する
3. **sprite.jpg**: GetImages(1 秒間隔、160×90、JPEG)→ Pillow で 10×6 のシート
4. **index.m3u8**: clip.mp4 を `EXT-X-BYTERANGE` で参照するプレイリスト
5. S3 に clip → sprite → プレイリストの順で PUT。その後 `run_pipeline` が区間の件 → カーソルの順に書く

### 遅れて届く断片

検証中、送信コンテナが CPU 不足で詰まり、断片が届くまでの遅れが 30 秒 → 100 秒と伸び続けた。
lag 30 秒で処理した区間(03:23)は 60 本中 42 本しか入らず、残りの 18 本は後から KVS に届いたがカーソルが進んでいたので S3 に入らなかった。
また回線断のあと、エッジのバッファから遅れて届いた断片も、「最近何も届いていない = 送信が止まった」とみなして先に `EMPTY` で確定していたため取りこぼした。
今は共通処理のウォーターマーク(届いている最新の断片の終わりが区間の終わりを越えたか、`MAX_LATE_SECONDS` を過ぎたか)で判定する。
回線断の間は処理済み位置が止まり、復旧後にバッファの断片が古い順に届くにつれて追いつく(下の「回線断の試験」)。

### 区間をまたぐ断片の扱い

**断片の開始時刻(ProducerTimestamp)が属する区間に割り当てる。** 断片は分割しない(キーフレームの境界でしか切れないため)。

- 例: 10:20:59.979 に始まる 1 秒の断片は 10:20 の区間の clip.mp4 に入り、中身は 10:21:00.979 まで続く
- そのため `segments` の最後の終わりが区間の終わりを少し越えることがある(最大で断片 1 本ぶん)。次の区間の `segments` は、その区間に始まる最初の断片から始まる。つなげて見れば重複も欠けもない
- キーフレーム間隔が 60 秒を割り切っていても、プロデューサー時刻の秒以下の位相は送信開始時刻で決まるので、断片の境界は分の境界に揃わない(実測では毎秒 .979 に断片が始まった)

### segments と断片の長さ

`FragmentLengthInMilliseconds` は最後の 1 フレーム分短い(15fps・1 秒ごとの断片で 933ms。実測)。
そのまま使うと断片ごとに 67ms の隙間ができるので、長さは「次の断片の開始 − この断片の開始」(隙間が 500ms 以内のとき)、
続く断片がなければ「長さ + 隙間の中央値(最大 250ms)」で補正する(`common.kvs.effective_durations`)。
`segments` はこの長さでつなぎ、隙間が 500ms を超えたら分ける。`hls.parts` の `duration_ms` も同じ値。

### clip.mp4 と index.m3u8

- `clip.mp4` = HLS の init(`ftyp` + `moov`、約 800 バイト)+ 断片(`moof` + `mdat`)を時刻順に連結した fragmented MP4。単体で再生できる
- 時間軸は「その区間の最初の断片の開始 = 0」。録画の切れ目は時間軸上の空白として残る(KVS の HLS 断片の `tfdt` が実時刻の差を保っている。実測)
- `index.m3u8`(HLS v7、VOD):
  - `#EXT-X-MAP:URI="clip.mp4",BYTERANGE="<init の長さ>@0"`
  - 断片ごとに `#EXTINF` と `#EXT-X-BYTERANGE:<長さ>@<オフセット>`
  - 先頭と、録画の切れ目(500ms 超)の後に `#EXT-X-PROGRAM-DATE-TIME`。切れ目には `#EXT-X-DISCONTINUITY`
- DynamoDB の `hls` に `init_length` と `parts: [[offset, length, duration_ms], ...]` を書くので、API はプレイリストを読まずに複数区間の vod.m3u8 を組める

### 方式の比較(実測、H.265 1280×720 15fps 約 0.6 Mbps)

| 方式 | 結果 | 採否 |
| --- | --- | --- |
| **HLS ON_DEMAND(fMP4)の init + 断片を連結** | init は `ftyp`+`moov`、断片は `moof`+`mdat`。連結した clip.mp4 は ffprobe で 900 フレーム(60 秒)、デコードエラーなし。BYTERANGE のプレイリストも ffmpeg の HLS デマクサーで 900 フレーム読める。1 分あたり約 0.8〜1 秒(並列 8 本で取得、Lambda 上) | **採用** |
| GetClip | H.265 で動く。1 回の呼び出しで約 0.7 秒。ただし出力は **非 fragmented の MP4**(`ftyp` + `moov` + `mdat`、`moof` なし) | 不採用。HLS(fMP4)のセグメントにできず、BYTERANGE で参照するには MP4 → fMP4 の再多重化が要る |
| GetMediaForFragmentList | MKV が返る | 不採用。fMP4 への再多重化(ffmpeg か自前の実装)が要る |
| ffmpeg で再多重化 / 再エンコード | — | 不要。Lambda に ffmpeg を載せずに済んだ |

HLS 方式の注意点:
- 1 区間あたり KVS への HTTP リクエストは「セッション URL 発行 + マスター + メディアプレイリスト + init + 断片の数」(1 秒の断片なら約 64 回)。KVS の料金は HLS の取り出し量(GB)で、リクエスト数ではない
- HLS セッション URL の発行はストリームあたり 25 TPS が上限。1 区間 1 回なので問題にならない
- セッションのプレイリストに ListFragments に出てこない断片が入っていても、断片番号で絞り込む(前後の区間の断片は入れない)

### sprite.jpg

- GetImages は **H.265 で動いた**(JPEG、160×90 を指定)。1 ページ 25 枚程度で返るのでページ送りする。区間 [start, end) を指定すると終わりの時刻も含む 61 枚が返る(61 枚目は捨てる)
- コマ `i`(0〜59)= 区間の開始 + i 秒の画像。左上から右へ、10 枚で折り返す。画像がない秒は **黒**(`segments` と突き合わせれば録画なしと分かる)
- GetImages は、範囲の始まりの近くに断片がないと `ResourceNotFoundException`(No fragments found)になる(区間の途中で録画が始まった場合など。実測)。そのときは録画がある範囲ごとに「開始を秒に切り上げた時刻 〜 終わり」で取り直す
- スプライトが作れなくても動画は退避する(`sprite_error` に理由を残して `DONE`)

### 回線断の試験(2026-10-24、実 KVS)

`testing/kvssink` のコンテナ(テストパターン、H.265 600 kbps、`storage-size=512 buffer-duration=900`)を専用の Docker ネットワークに置き、
`docker network disconnect` で回線を切った(DNS も引けなくなる)。ハンドラーはローカルから毎分 1 回実行。

**試験 1: 4 分の遮断、`MAX_LATE_SECONDS=720`**(03:58:41 切断 → 04:02:41 復旧)

- 遮断中: ウォーターマークは 03:58:40.745 で止まり、03:58 の区間で `minute_waiting`(処理済み位置は 03:58 で止まった。区間の件は作らない)
- 復旧後: バッファの 287 本(プロデューサー時刻 03:58:41〜04:03:27)が 04:02:41〜04:03:38 の約 1 分で古い順に届いた。届いた順でプロデューサー時刻が戻ることはなかった
- 復旧後最初の起動(04:03:15)で 03:58〜04:00 が、次の起動で 04:01〜04:02 が `DONE`。各区間 60 本(03:58 だけ 59 本)、ffprobe で 900 フレーム(03:58 は 885)、デコードエラーなし、スプライト 60 コマ
- 切断の瞬間に送っていた 1 本(03:58:40.8 から 1 秒)は KVS に入らなかった(`segments` に 1 秒の切れ目)。同じ ProducerTimestamp の重複断片は 0 本

**試験 2: 5 分の遮断、`MAX_LATE_SECONDS=150`**(04:08:40 切断 → 04:13:41 復旧。上限を超えたときの挙動)

- 遮断中: 区間の終わりから 150 秒過ぎた区間を順に `minute_max_late` で確定(04:07 は届いていた 53 本で `DONE`、04:08〜04:10 は `EMPTY`)
- 復旧後に届いた 04:09:11〜04:10:59 の断片は、その区間が確定済みなので処理しなかった(限界どおり)。04:07:53〜04:09:10 は KVS に届かなかった(エッジ側で失われた)
- このとき送信コンテナは CPU 100% で、復旧後のバッファの送信が実時間の約 0.4 倍しか進まず、遅れが 150 秒を超え続けたため 04:11 以降も届いた分(04:11 は 13 本)か `EMPTY` で確定した。
  既定の 720 秒なら、遅れが 12 分に収まる限り待って埋まる。エッジのバッファと送信速度は別途(エッジ側の担当)

## DynamoDB に書く属性(区間の件)

| 属性 | 内容 |
| --- | --- |
| `video_status` | `DONE` / `EMPTY` / `FAILED`(共通処理が書く) |
| `segments` | `[[開始ms, 終了ms], ...]` |
| `files` | `{"clip", "playlist", "sprite"}` の S3 キー |
| `hls` | `{"init_length", "parts": [[offset, length, duration_ms], ...]}` |
| `fragment_count` / `bytes` | clip に入れた断片の数、clip.mp4 のバイト数 |
| `sprite_frames` | スプライトに実際に入ったコマ数(0〜60) |
| `sprite_error` | スプライトの画像が取れなかった理由(あれば) |
| `video_duration_ms` / `video_attempts` / `video_error` / `expires_at` | 共通処理が書く |

## ログ(1 行 1 JSON)

区間ごとに `minute_metrics` を出す。CloudWatch Logs Insights の例:

```
fields @timestamp, total_ms, clip_ms, images_ms, s3_ms, clip_bytes, fragment_count, max_rss_mb
| filter event = "minute_metrics" and status = "DONE"
| stats avg(total_ms), max(total_ms), avg(clip_bytes), max(max_rss_mb)
```

| フィールド | 内容 |
| --- | --- |
| `list_ms` / `clip_ms` / `images_ms` / `sprite_ms` / `s3_ms` / `total_ms` | 各段階の所要時間 |
| `clip_bytes` / `kvs_bytes` / `sprite_bytes` | clip.mp4、KVS 上の断片の合計、スプライトのバイト数 |
| `fragment_count` / `listed_fragments` / `images` / `sprite_frames` | 件数 |
| `recorded_ms` | `segments` の合計 |
| `max_rss_mb` | プロセスの最大常駐メモリ |

共通処理の `minute_done` / `minute_retry` / `minute_failed` / `pipeline_done` も出る。Lambda の REPORT 行と合わせて見る。

## 実測(2026-10-24、arm64、H.265 1280×720 15fps、キーフレーム 1 秒、約 0.6 Mbps)

一時 Lambda(`cc-tmp-video-archiver`)を毎分 1 回呼び、1 回 = 1 区間(録画 60 秒)を処理したときの値。ウォームスタートの 2 回の平均。

| メモリ | Duration(REPORT) | 内訳(clip / images / sprite / S3) | Max Memory Used | GB 秒 / 区間 |
| --- | --- | --- | --- | --- |
| 256 MB | 約 3.1 秒 | 1.66 / 0.59 / 0.11 / 0.36 秒 | 130 MB | 約 0.79 |
| 512 MB | 約 1.9 秒 | 0.82 / 0.61 / 0.03 / 0.26 秒 | 132〜138 MB | 約 0.95 |
| 1024 MB | 約 1.4 秒 | 0.47 / 0.53 / 0.02 / 0.22 秒 | 134 MB | 約 1.37 |

- コールドスタート: Init 約 0.3 秒。最初の区間は sprite(Pillow の読み込み)と clip が遅く、+1〜2 秒
- 1 区間のサイズ: clip.mp4 約 4.5〜4.6 MB(KVS 上の断片の合計より約 28 KB 小さい)、sprite.jpg 約 80 KB、index.m3u8 約 3.5 KB
- 録画なし(EMPTY)の区間は ListFragments だけで 50ms 前後
- 断片の到着: サーバー時刻 − プロデューサー時刻は 0.1〜0.3 秒。ListFragments には、断片の開始から 0.8〜2 秒(断片の終わりから 0〜1 秒)で見えた。良い回線なら `LAG_SECONDS` は 10 秒程度まで縮められそう(既定は余裕を見て 30 秒)
- メモリは 256 MB で足りる(使用は 130 MB 前後で、大半は Python + boto3 + Pillow)。費用は 256 MB が最小(1 台・1 か月で約 34,000 GB 秒)。ビットレートが高いカメラでは clip をメモリに持つ分(1 Mbps で約 7.5 MB)だけ増える。余裕を見るなら 512 MB

## テスト

```sh
cd lambdas/video_archiver
mise exec -- uv sync
mise exec -- uv run pytest
```

S3 と DynamoDB は moto、KVS はスタブ(実 AWS には接続しない)。

## 実 KVS での検証手順

```sh
# 1. H.265 を KVS に送るコンテナ(テストパターン + 時刻の焼き込み、kvssink)
docker build -t cc-tmp-video-kvssink lambdas/video_archiver/testing/kvssink
aws kinesisvideo create-stream --stream-name cc-tmp-video-cam-001 --data-retention-in-hours 1 --media-type video/h265
aws configure export-credentials --format env-no-export > creds.env && echo AWS_DEFAULT_REGION=ap-northeast-1 >> creds.env
docker run -d --name cc-tmp-video-sender --env-file creds.env -e STREAM_NAME=cc-tmp-video-cam-001 cc-tmp-video-kvssink

# 2. 各 API の挙動を見る(ListFragments、GetImages、HLS、GetClip)
mise exec -- uv run python testing/probe.py cc-tmp-video-cam-001 /tmp/probe 2

# 3. ハンドラーをローカルから実行(一時のテーブルとバケットを用意しておく)
STATE_TABLE=cc-tmp-video-state MEDIA_BUCKET=cc-tmp-video-media-<account> STREAM_NAME_PREFIX=cc-tmp-video- \
  mise exec -- uv run python testing/run_local.py cam-001

# 4. 出力の確認(コンテナに ffmpeg / ffprobe が入っている)
docker run --rm -v "$PWD/out:/d" --entrypoint ffprobe cc-tmp-video-kvssink -v error -count_frames \
  -show_entries stream=codec_name,nb_read_frames /d/clip.mp4
docker run --rm -v "$PWD/out:/d" --entrypoint ffprobe cc-tmp-video-kvssink -v error -count_frames \
  -protocol_whitelist file -show_entries stream=nb_read_frames /d/index.m3u8
```

終わったら `cc-tmp-video-` のストリーム、テーブル、バケット、コンテナを必ず削除する。
