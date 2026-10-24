# ビューワー

React + TypeScript + Vite。UI は [Mantine](https://mantine.dev/)、再生は [hls.js](https://github.com/video-dev/hls.js)(Safari はネイティブの HLS)。

## 画面

- 左: カメラ一覧。オンライン / 配信中 / 処理済み位置の遅れ(S3 への退避がどこまで済んだか)と、配信の開始・停止スイッチ
- 中央: プレイヤー。映像、操作(再生・一時停止、前後の録画へ、再生速度、日付、ライブ)、1 日分のシークバー
- 右(狭い画面では下): 自然言語検索。回答、該当時刻の一覧(クリックでその時刻から再生)、生成された SQL とトークン数

### 再生元の切り替え

プレイヤーは位置を絶対時刻で持ち、要求された時刻から再生元を決める(`src/lib/playbackSource.ts`)。

| 時刻 | 再生元 |
| --- | --- |
| ライブ(現在から 15 秒以内を含む) | KVS WebRTC(`GET /api/cameras/{id}/webrtc`)。つながらなければ `GET /api/cameras/{id}/kvs-hls?mode=LIVE` |
| `video_processed_until` 以後 | `GET /api/cameras/{id}/kvs-hls?mode=ON_DEMAND&from=&to=`(分の頭から現在まで) |
| `video_processed_until` より前 | `GET /api/cameras/{id}/vod.m3u8?from=&to=`(時の境界で 1 時間ずつ) |

窓の終わりまで再生したら次の窓(S3 → KVS → ライブ)を自動で読み込む。今どこから再生しているかは映像の右上に小さく出す。
絶対時刻と動画内の位置の対応は `EXT-X-PROGRAM-DATE-TIME` を使う(なければ窓の始まりを基準にする)。

### シークバー

- 色: 録画(S3)、未退避(KVS)、処理失敗(FAILED)、録画なし、未来(斜線)
- ズーム 1日 / 6時間 / 1時間。ズーム中は再生位置を中心に表示し、左右の矢印で送れる
- ホバー(スマホはドラッグ中)で `sprite.jpg` の該当秒のコマを CSS で切り出して表示(`src/lib/sprite.ts`)
- 録画がない時刻をクリックすると次の録画から再生する。矢印キーで 10 秒(Shift で 1 分)送る

### ライブ(KVS WebRTC と HLS)

ライブは KVS WebRTC(1 秒未満の遅れ)を先に試し、だめなら KVS の HLS(数秒遅れ)に切り替える(`src/player/webrtcLive.ts`、純粋なロジックは `src/lib/webrtc.ts`)。

- API(`GET .../webrtc`)が返す署名付き WSS URL と ICE サーバーで、KVS の JS SDK(`amazon-kinesis-video-streams-webrtc` 2.9.0、版を固定)の
  `SignalingClient` につなぐ。署名は API が済ませているので、SDK には URL を返すだけの `requestSigner` を渡す(ブラウザに AWS の認証情報はない)。
  SDK はブラウザ向けのバンドル(`dist/kvs-webrtc.min.js`)をライブのときだけ読み込む
- コーデック: `RTCRtpReceiver.getCapabilities('video')` に H.265 があり、エッジが H.265 を出せる(API の `codecs`)なら
  `setCodecPreferences` で H.265 → H.264 → 再送の順、なければ H.264 だけ。エッジは並びを見て main(H.265)か sub(H.264)を流す
- HLS に切り替える条件: API が 409(WebRTC を止めている、オフライン、エッジの master が待ち受けていない)、最初の映像が 10 秒以内に出ない、
  ICE が 5 秒以上 failed / disconnected のまま。再生中に切れた・映像が 8 秒止まったときは開き直す(WebRTC をもう一度試し、だめなら HLS)
- 映像の右上の再生元に「ライブ │ WebRTC · H.265」「ライブ │ HLS · H.265」と小さく出す(マウスを乗せると、TURN 経由か、HLS に切り替えた理由)
- 検証用の URL の引数: `?live=hls`(WebRTC を使わない)、`?live=h264`(WebRTC で H.264 だけ)。
  本番のビルドでも有効にしている。理由: デプロイ済みの環境で HLS と WebRTC の遅延を比べる計測(docs/webrtc.md)に使うことと、
  回線が細い・H.265 の WebRTC がうまく動かない端末の逃げ道になること。変わるのはライブの方式の選び方だけで、API の権限や見られる範囲は変わらない
- モック(`dev:mock`)では API が `mock: true` を返し、同じページ内の疑似 master(`src/mocks/rtcMaster.ts`。canvas のテストパターン)につなぐ。
  玄関は WebRTC、駐車場は WebRTC を止めているので HLS になる

### PTZ

シャドウの `reported.ptz.supported` が true のカメラだけ、操作ボタン(コントロール列の十字矢印)とパネル(映像の下)を出す。
**WebRTC の**ライブ再生中だけ操作でき、録画と HLS のライブの再生中はパネルを隠してボタンを無効にする(HLS は数秒遅れて操作感が悪いため。`src/player/PtzPanel.tsx`、ロジックは `src/lib/ptz.ts`)。

- 方向パッドとズーム(+ / −): 押している間 `continuous`(速度 0.5、`timeout_ms` 4 秒を 2.5 秒ごとに送り直す)、
  離したとき・ポインタが外れたとき・タブが非表示になったとき・ウィンドウのフォーカスが外れたとき・パネルが消えるときに `stop`。
  250 ms より短い押下(タップ)は `relative` で一定量(pan / tilt 0.1、zoom 0.1)だけ動かす
- 中央のボタンでホーム、「プリセット」の一覧から移動、「現在位置を保存」で `set_preset`
- キーボード(パネルにフォーカスがあるときだけ): 矢印で向き、+ / − でズーム、Home でホーム
- 命令は 1 つずつ順に送る(前の 202 を待つ)。待っている間の continuous / stop は最後の 1 つだけ送る
- 現在の位置は `GET .../ptz`。操作中(と操作後 6 秒)は 0.7 秒、それ以外は 10 秒ごとに取り直す。
  ミニマップは最大倍率 4 倍(エミュレーターの既定)を仮定した目安
- 操作から映像の変化まで WebRTC で 0.8〜0.9 秒(実 AWS で計測。API → IoT Core → エッジ → ONVIF とエミュレーターの切り出し + WebRTC の遅れ。docs/webrtc.md)なので、「操作はおよそ 1 秒で映像に反映されます」と添える
- タッチ端末(`pointer: coarse`)ではボタンを 44px にする

### H.265

`MediaSource.isTypeSupported`(Safari は `canPlayType`)で `hvc1` / `hev1` を判定し、再生できない見込みなら案内を出す。
再生中にコーデックのエラーが出たときも映像の上に案内を出す。

## 使い方

ツールは mise で入れる(リポジトリ直下で `mise install`)。

```sh
cd viewer
mise exec -- npm ci

# モック(AWS なし)。MSW が API とメディアを返し、動画は公開のテスト HLS(H.264)で代用する
mise exec -- npm run dev:mock

# 実環境。/api と /media を CloudFront にプロキシする
VIEWER_PROXY_TARGET=https://dxxxxxxxx.cloudfront.net mise exec -- npm run dev

# 本番ビルド(dist/ に出力。CloudFront の同一オリジンから配信する前提で、API は相対パス)
mise exec -- npm run build
```

`VIEWER_PROXY_TARGET` は `viewer/.env.local` に書いてもよい(gitignore 済み)。

## 検証

```sh
mise exec -- npm run lint
mise exec -- npm run typecheck
mise exec -- npm test

# スクリーンショット(dev:mock を起動しておく)。ライト/ダーク × デスクトップ/スマホ幅
mise exec -- npx playwright install chromium
mise exec -- node scripts/screenshots.mjs <出力先> http://localhost:5173/

# コーデックの切り替えをまたぐ録画を本物の Chrome(hls.js)で再生できるか(デプロイ済みの環境で。docs/codec.md)
mise exec -- node scripts/codec-play.mjs <CloudFront の URL> <camera_id> <from ISO> <to ISO> [出力先]
```

## 構成

| パス | 中身 |
| --- | --- |
| `src/theme.ts` | デザイントークン(色、余白、角丸、文字サイズ、ライト/ダークの意味色 `--cc-*`)。見た目の値はここだけで決める |
| `src/api/` | API の型とクライアント |
| `src/lib/` | 純粋なロジック(再生元の判定、シークバーの描画計算、スプライトのコマ位置、JST の時刻)と単体テスト |
| `src/player/` | プレイヤー(`usePlayback` が再生元の切り替えと hls.js を扱う)、シークバー、PTZ パネル |
| `src/cameras/` | カメラ一覧 |
| `src/search/` | 自然言語検索 |
| `src/mocks/` | MSW のハンドラーとそれらしいデータ(録画の隙間、FAILED、未退避の区間、検出結果、canvas で描くスプライト、疑似 PTZ の位置。玄関・駐車場は PTZ 対応、倉庫は非対応) |

## 既知の制約

- Safari(ネイティブ HLS)では断片ごとの `PROGRAM-DATE-TIME` を取れないため、録画の隙間をまたぐと表示時刻がずれることがある(`getStartDate()` + 経過秒で近似)
- KVS の HLS で時刻を合わせるには、API が `DisplayFragmentTimestamp=ALWAYS` でセッション URL を発行している必要がある
- 音声は扱わない(常にミュート)
- API(lambdas/api)はゲートウェイの死活(`online`)をまだ返さないため、実環境ではオンライン/オフラインのバッジを出さない(モックは表示確認のため返す)。カメラ名はシャドウの `name` があれば使い、なければ camera_id
