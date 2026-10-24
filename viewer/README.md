# ビューワー

React + TypeScript + Vite。UI は [Mantine](https://mantine.dev/)、再生は [hls.js](https://github.com/video-dev/hls.js)(Safari はネイティブの HLS)。

## 画面

- 左: カメラ一覧。オンライン / 配信中 / 処理済み位置の遅れ(S3 への退避がどこまで済んだか)と、配信の開始・停止スイッチ
- 中央: プレイヤー。映像、操作(再生・一時停止、前後の録画へ、再生速度、日付、ライブ)、1 日分のシークバー
- 右(狭い画面では下): 自然言語検索。回答、該当時刻の一覧(クリックでその時刻から再生)、生成された SQL とトークン数

### 再生元の切り替え(構想.md「再生」節)

プレイヤーは位置を絶対時刻で持ち、要求された時刻から再生元を決める(`src/lib/playbackSource.ts`)。

| 時刻 | 再生元 |
| --- | --- |
| ライブ(現在から 15 秒以内を含む) | `GET /api/cameras/{id}/kvs-hls?mode=LIVE` |
| `video_processed_until` 以後 | `GET /api/cameras/{id}/kvs-hls?mode=ON_DEMAND&from=&to=`(分の頭から現在まで) |
| `video_processed_until` より前 | `GET /api/cameras/{id}/vod.m3u8?from=&to=`(時の境界で 1 時間ずつ) |

窓の終わりまで再生したら次の窓(S3 → KVS → ライブ)を自動で読み込む。今どこから再生しているかは映像の右上に小さく出す。
絶対時刻と動画内の位置の対応は `EXT-X-PROGRAM-DATE-TIME` を使う(なければ窓の始まりを基準にする)。

### シークバー

- 色: 録画(S3)、未退避(KVS)、処理失敗(FAILED)、録画なし、未来(斜線)
- ズーム 1日 / 6時間 / 1時間。ズーム中は再生位置を中心に表示し、左右の矢印で送れる
- ホバー(スマホはドラッグ中)で `sprite.jpg` の該当秒のコマを CSS で切り出して表示(`src/lib/sprite.ts`)
- 録画がない時刻をクリックすると次の録画から再生する。矢印キーで 10 秒(Shift で 1 分)送る

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
```

## 構成

| パス | 中身 |
| --- | --- |
| `src/theme.ts` | デザイントークン(色、余白、角丸、文字サイズ、ライト/ダークの意味色 `--cc-*`)。見た目の値はここだけで決める |
| `src/api/` | API の型とクライアント |
| `src/lib/` | 純粋なロジック(再生元の判定、シークバーの描画計算、スプライトのコマ位置、JST の時刻)と単体テスト |
| `src/player/` | プレイヤー(`usePlayback` が再生元の切り替えと hls.js を扱う)、シークバー |
| `src/cameras/` | カメラ一覧 |
| `src/search/` | 自然言語検索 |
| `src/mocks/` | MSW のハンドラーとそれらしいデータ(録画の隙間、FAILED、未退避の区間、検出結果、canvas で描くスプライト) |

## 既知の制約

- Safari(ネイティブ HLS)では断片ごとの `PROGRAM-DATE-TIME` を取れないため、録画の隙間をまたぐと表示時刻がずれることがある(`getStartDate()` + 経過秒で近似)
- KVS の HLS で時刻を合わせるには、API が `DisplayFragmentTimestamp=ALWAYS` でセッション URL を発行している必要がある
- 音声は扱わない(常にミュート)
