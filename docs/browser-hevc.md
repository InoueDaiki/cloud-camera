# ブラウザの H.265(HEVC)対応状況

確認日: 2026-10-24。録画もライブも可能な限り H.265 で配信する方針([decisions.md](decisions.md))のもとで、
ブラウザで H.265 を再生できないユーザーがどのくらいいるかを、一次情報・公開シェア・実機の確認から見積もった。
WebRTC の詳細は [webrtc.md](webrtc.md) の「1. H.265 の対応状況」も参照。

再生経路は 2 つ。

| 経路 | 仕組み | このドキュメントでの呼び方 |
| --- | --- | --- |
| 録画(S3)と KVS の HLS | fMP4 の HLS。hls.js(MSE)で再生。Safari はネイティブ HLS。コーデック文字列は `hvc1.1.6.L93.90` など | **録画** |
| ライブ(KVS WebRTC) | WebRTC で H.265 を受信。受けられなければカメラの sub(H.264)に切り替える | **WebRTC** |

## 結論

- **録画(MSE / ネイティブ HLS)で H.265 を再生できるユーザーは、全体の約 85〜96%(世界)、77〜93%(日本)。**
  - スマホはほぼ全員(96〜100%)が再生できる。iOS は全ブラウザ、Android は OS のデコーダーを使う。
  - 再生できないのは主に **Windows のデスクトップ**。
    - **Windows の Edge** は Microsoft Store の「HEVC Video Extensions」が要る。
    - Chrome もハードウェアデコーダーが要るので、古い PC や GPU のない仮想デスクトップでは再生できない。
    - Linux は環境しだい。
  - 日本の数字が低いのは、デスクトップ比率と Edge の比率が高いため。
- **WebRTC で H.265 を受けられるユーザーは、約 69〜82%(世界)、64〜75%(日本)。**
  - 受けられないのは Edge(既定で H.265 を出さない)、Firefox(非対応)、ハードウェアデコーダーのない Chrome、iOS 17 以前。
  - ライブは今の設計どおり **H.264(sub)を既定にし、H.265 は検出できたときだけ** にするのが妥当。
- **B2B の想定(社内 PC は Windows + Chrome / Edge、スマホは iPhone / Android)は Edge の比率で大きく変わる。**
  - 社内 PC の録画は 37〜81%。Chrome を指定できれば 80〜95%。
  - 社内 PC の WebRTC は 30〜63%。
  - スマホは録画 98〜100%、WebRTC 81〜91%。
  - Edge が標準の会社では、録画も H.265 のままだと見られない人がかなり出る。
    対策は「Chrome を案内する」「HEVC 拡張を社内配布する」「H.264 の代替を用意する」のどれか(「6. 対応しないユーザーへの選択肢」)。
- 実機(Mac、Apple M5、macOS 26.6.2)の結果:
  - Chrome 155、WebKit 27.2(Safari 相当)、Firefox 157 は H.265 の fMP4(Main / Main10)を MSE で実際に再生できた。
  - WebRTC で H.265 の受信能力を出したのは Chrome と WebKit だけで、Firefox は出さなかった。
  - Playwright 同梱の Chromium は H.265 をまったく再生できない。テストで使うときは注意する。

## 1. 対応表(一次情報)

### 1.1 録画(MSE / `<video>`)

| ブラウザ / OS | H.265 再生 | 条件 | 出典 |
| --- | --- | --- | --- |
| Chrome / Windows | ○(107〜) | **ハードウェアデコーダーが必須**。Windows 8 以降。ソフトウェアデコーダーは持たない(公式ビルドに FFmpeg の HEVC は入っていない) | [chromestatus 5186511939567616](https://chromestatus.com/feature/5186511939567616)、[MDN Video codecs](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/Video_codecs)、[caniuse hevc](https://caniuse.com/hevc) |
| Chrome / macOS | ○(107〜) | macOS 11 以降は**全機種**。GPU にデコーダーがなければ VideoToolbox がソフトウェアで再生する | 同上、[StaZhu/enable-chromium-hevc-hardware-decoding](https://github.com/StaZhu/enable-chromium-hevc-hardware-decoding) |
| Chrome / Android | ○(107〜) | Android 5.0 以降は**全機種**(OS の MediaCodec。ソフトウェアデコーダーもある) | chromestatus、MDN、caniuse |
| Chrome / ChromeOS | ○(107〜) | ハードウェアデコーダー(VA-API)が必須 | chromestatus、caniuse の note 5 |
| Chrome / Linux | △(108〜) | VA-API で使えるハードウェアデコーダーが必須。実際にはドライバーの設定しだい | StaZhu リポジトリ、caniuse の note 5 |
| Edge / Windows | △ | ハードウェアデコーダーに加えて、**Microsoft Store の「HEVC Video Extensions」が必須**。Windows 10 1709 以降。拡張のライセンスが無効だと、Chrome で見られる同じ動画でも Edge では見られない | [Microsoft Learn: Fix Edge video playback problems](https://learn.microsoft.com/en-us/troubleshoot/microsoft-edge/development/video-playback-issues)、caniuse の note 4、MDN |
| Edge / macOS・Android | ○ | Chrome と同じ実装 | caniuse の note 4、StaZhu リポジトリ |
| Safari / macOS | ○ | macOS 10.13 High Sierra 以降。Safari 13 以降は全面的に対応(11〜12.1 は部分対応)。ネイティブ HLS も MSE も可 | [Apple: HEIF/HEVC](https://support.apple.com/en-us/116944)、caniuse |
| Safari / iOS・iPadOS | ○ | iOS 11 以降。ネイティブ HLS で再生できる。iPhone の MSE は ManagedMediaSource(Safari 17.1 以降)だけ | Apple、caniuse、[WebKit: Safari 17.1](https://webkit.org/blog/14735/webkit-features-in-safari-17-1/) |
| iOS の Chrome / Edge / Firefox など | ○(Safari と同じ) | App Store 審査ガイドライン 2.5.6 で WebKit が必須。EU と日本では別エンジンも申請できるが、主要ブラウザの iOS 版は 2026-10 時点でも WebKit と見てよい(未確認) | [App Store Review Guidelines 2.5.6](https://developer.apple.com/app-store/review/guidelines/) |
| Firefox / Windows | ○(134〜) | ハードウェアデコーダーがあれば再生できる。なければ有料の拡張を入れてソフトウェアで再生 | [Firefox 134 release notes](https://www.firefox.com/en-US/firefox/134.0/releasenotes/)、MDN |
| Firefox / macOS | ○(136〜) | ハードウェアでもソフトウェアでも再生できる | [Firefox 136](https://www.firefox.com/en-US/firefox/136.0/releasenotes/)、MDN |
| Firefox / Linux | △(137〜) | ハードウェアでもソフトウェア(システムの ffmpeg)でも再生できる。ディストリビューションの ffmpeg に HEVC が入っているかしだい | [Firefox 137](https://www.firefox.com/en-US/firefox/137.0/releasenotes/)、MDN |
| Firefox / Android | △(137〜) | ハードウェアのみ(MDN による。リリースノートでは未確認) | MDN |
| Samsung Internet | ○(21〜) | | caniuse |
| Opera / Brave / Vivaldi | Chrome と同じ(推定) | MDN は「Chromium 系は Chrome と同じ」とする。Windows の Opera は Media Foundation を使うので Edge と同じ制約かもしれない(フォーラムの情報で、未確認) | MDN、[Opera forum](https://forums.opera.com/post/394621) |

caniuse の世界の利用率は、完全対応 19.99% と部分対応 74.95% を合わせて **94.94%** になる([hevc.json](https://raw.githubusercontent.com/Fyrd/caniuse/main/features-json/hevc.json))。
「部分対応」には「ハードウェアがあれば」(Chrome / Windows)と「拡張を入れれば」(Edge)が含まれる。
そのため、これは**上限**と見る。

#### ハードウェアデコーダーが載った世代(Chrome / Windows で効く)

| ベンダー | 8bit Main の完全ハードウェア | Main10 | 出典 |
| --- | --- | --- | --- |
| Intel | 第 6 世代 Core(Skylake、2015)から。Chromium の目安は HD 4400 / HD 515 以降 | 第 7 世代(Kaby Lake、2016)。Skylake はハイブリッド | [Wikipedia: Intel Quick Sync Video](https://en.wikipedia.org/wiki/Intel_Quick_Sync_Video)、[WWDC17 503](https://developer.apple.com/videos/play/wwdc2017/503/)、StaZhu |
| NVIDIA | GM206(GTX 950 / 960、2015)から。Pascal(GTX 10 系、2016)で全プロファイル。Maxwell 1 世代(GTX 750 Ti など)はハイブリッド | GM206 から | [Wikipedia: Nvidia PureVideo](https://en.wikipedia.org/wiki/Nvidia_PureVideo) |
| AMD | UVD 6(Carrizo APU、Fiji、2015)から | Polaris(RX 400、2016)から | [Wikipedia: Unified Video Decoder](https://en.wikipedia.org/wiki/Unified_Video_Decoder) |
| Apple | iOS は A9(iPhone 6s、2015)から。Mac は第 6 世代 Intel と Apple Silicon 全機種。どちらもソフトウェアでも再生できる | A9 以降、Mac は第 7 世代 Intel 以降と Apple Silicon | [WWDC17 503](https://developer.apple.com/videos/play/wwdc2017/503/) |
| Qualcomm | Snapdragon 805(2013)が最初とされる(ベンダーの発表) | — | [発表記事](https://www.edge-ai-vision.com/2013/11/qualcomm-technologies-announces-next-generation-qualcomm-snapdragon-805-ultra-hd-processor/) |

**Windows 11 の要件を満たす PC(第 8 世代 Intel 以降、AMD Zen+ 以降)は、ほぼすべて HEVC のハードウェアデコーダーを持つ**(推論)。
Windows 10 のサポートは 2025-10 に終わった。それでも StatCounter(2026-09)では、世界の Windows の 27.8% が Windows 10 のまま。

ハードウェアデコーダーがない、または使えない典型例:

- 2015 年より前の PC
- 仮想デスクトップ(VDI、AVD、Citrix など。GPU がない)や仮想マシン
- GPU ドライバーが古くてブロックリストに入っている PC
- リモートデスクトップ越しのブラウザ

### 1.2 WebRTC

| ブラウザ / OS | H.265 受信 | 条件 | 出典 |
| --- | --- | --- | --- |
| Chrome(Windows / macOS / Android) | ○(136〜) | **ハードウェアデコーダーがあるときだけ**(WebRTC にはソフトウェアのフォールバックがない)。Intent to Ship 時点のハードウェア対応率は Windows 75%、macOS 99%、Android 86% | [chromestatus 5153479456456704](https://chromestatus.com/feature/5153479456456704)、[blink-dev](https://groups.google.com/a/chromium.org/g/blink-dev/c/3h8lL8a377c/m/_vnatJetAQAJ) |
| Chrome(Linux / ChromeOS) | △ | ハードウェアデコーダーしだい(未確認) | |
| Edge | × | Chromium ベースだが、既定で H.265 を SDP に出さない(Microsoft Q&A のモデレーターの回答。公式情報は未確認) | [Microsoft Q&A](https://learn.microsoft.com/en-ca/answers/questions/5880331/h-265-(hevc)-not-published-sent-via-webrtc-chrome) |
| Safari(macOS / iOS / iPadOS) | ○(18〜) | RFC 7798 のパケット化は Safari 18.0 から | [Safari 18.0](https://webkit.org/blog/15865/webkit-features-in-safari-18-0/)、[WebKit 273829@main](https://github.com/WebKit/WebKit/commit/89d1bc2adb) |
| iOS の Chrome / Edge など | ○(Safari と同じ) | WebKit を使う(ガイドライン 2.5.6) | |
| Firefox(全 OS) | × | 未対応(Bug 2001016、2014236)。実機の Firefox 157 も `RTCRtpReceiver.getCapabilities` に H.265 を出さない | [Bug 2001016](https://bugzilla.mozilla.org/show_bug.cgi?id=2001016) |
| Samsung Internet / Opera など | 不明 | Chromium のバージョンとベンダーの設定しだい(未確認) | |

H.264(Constrained Baseline)は WebRTC の必須コーデックなので、どのブラウザでも受信できる([RFC 7742](https://www.rfc-editor.org/rfc/rfc7742))。

## 2. 割合の概算

### 2.1 使ったシェア(StatCounter、2026-09)

| 項目 | 世界 | 日本 |
| --- | --- | --- |
| デスクトップ / モバイル / タブレット | 39.3 / 59.0 / 1.8 | 51.8 / 45.8 / 2.4 |
| デスクトップのブラウザ | Chrome 65.1、Edge 14.6、Safari 8.1、Firefox 6.1、Opera 2.4、Brave 1.1 | Chrome 56.5、Edge 28.0、Firefox 5.5、Safari 5.4、Brave 1.5 |
| デスクトップの OS | Windows 76.4、macOS 16.8、Linux 4.5、ChromeOS 2.3 | Windows 78.8、macOS 15.1、Linux 5.4、ChromeOS 0.8 |
| モバイルの OS | Android 69.2、iOS 30.8 | iOS 60.5、Android 39.5 |
| Windows のバージョン(デスクトップ) | Windows 11 71.4、Windows 10 27.8 | — |

出典は `https://gs.statcounter.com/{platform-market-share/desktop-mobile-tablet | browser-market-share/desktop | os-market-share/desktop | os-market-share/mobile | windows-version-market-share/desktop}/{worldwide|japan}`。
macOS の数字は StatCounter の「OS X」と「macOS」を足したもの。

### 2.2 仮定

StatCounter は「ブラウザ × OS」の組み合わせを出さないので、次のように割り振った。

- Edge はすべて Windows とする。
- Firefox のデスクトップ利用のうち 4.5 ポイントを Windows とする。
- Safari はすべて Mac とする。
- Windows の残りは Chrome 系(Chrome、Opera、Brave など)とする。
- タブレットはモバイルに含めた。
- 割合は**ページビューのシェア**で、ユーザー数ではない。

| 区分 | 録画で再生できる割合 | WebRTC で受けられる割合 | 根拠 |
| --- | --- | --- | --- |
| Windows の Chrome 系 | 75〜95% | 75〜90% | 下限は Chrome の I2S の Windows ハードウェア対応率 75%(2025 年)。その後の Windows 11 移行で上がっているはず |
| Windows の Edge | 15〜65% | 0% | ハードウェアがあり、かつ HEVC 拡張が入っている割合(OEM の PC には無料版の「from Device Manufacturer」が最初から入っていることが多い)。導入率の公開データはなく、**最も不確か** |
| Windows の Firefox | 75〜95% | 0% | ハードウェアがあれば再生できる |
| Mac(Safari / Chrome) | 97〜100% | 85〜95% | macOS 11 以降はソフトウェアでも再生できる。WebRTC は Safari 18 以降(macOS 13 以降)と Chrome。Mac の Firefox / Edge は WebRTC で 0% |
| Linux | 20〜60% | 0〜30% | VA-API とシステムの ffmpeg しだい |
| ChromeOS | 85〜100% | 70〜95% | 未確認 |
| iOS / iPadOS | 99〜100% | 85〜95% | 録画は iOS 11 以降。WebRTC は iOS 18 以降(iPhone XS 以降)の比率(推定) |
| Android | 95〜100% | 72〜86% | 録画は OS のデコーダー。WebRTC の上限は Chrome の I2S の Android 86%。下限は Samsung Internet などの不明分を考慮した |

### 2.3 結果

| 対象 | 録画(MSE / ネイティブ) | WebRTC |
| --- | --- | --- |
| 世界・デスクトップ | 68〜90% | 59〜71% |
| 世界・モバイル | 96〜100% | 76〜89% |
| **世界・全体** | **85〜96%** | **69〜82%** |
| 日本・デスクトップ | 59〜86% | 48〜59% |
| 日本・モバイル | 97〜100% | 80〜91% |
| **日本・全体** | **77〜93%** | **64〜75%** |

計算式は「Σ(区分のシェア × 区分の対応率)」で、プラットフォームの比で重み付けした。
caniuse の 94.94% は世界・全体の録画の上限側とおおむね合う。

### 2.4 B2B の想定(監視カメラビューワー)

前提:

- 社内 PC は Windows で、ブラウザは Chrome か Edge。
- 社用スマホは iPhone か Android。比は日本のモバイル OS 比の 60 : 40。
- Windows 10 のサポート終了(2025-10)で、社内 PC の大半は Windows 11 に入れ替わっているとする。そのため、ハードウェアデコーダーがある割合は 80〜95%。

| 環境 | 録画 | WebRTC | 計算 |
| --- | --- | --- | --- |
| 社内 PC、**Chrome を指定できる** | **80〜95%** | **75〜90%** | ハードウェアデコーダーの有無だけ |
| 社内 PC、**Edge が標準** | **10〜50%** | **0%** | ハードウェアあり × HEVC 拡張の導入率(Store を止めている会社が多い。拡張は社内配布もできる) |
| 社内 PC、Chrome と Edge が混在(Edge 30〜60%) | 37〜81% | 30〜63% | 下限: 0.4×0.8 + 0.6×0.8×0.1。上限: 0.7×0.95 + 0.3×0.95×0.5 |
| 社内 PC、仮想デスクトップ(VDI) | ほぼ 0% | 0% | GPU がない。RDP / Citrix のリダイレクトを使っていなければ |
| 社用スマホ(iPhone 60%、Android 40%) | 98〜100% | 81〜91% | 0.6×(0.85〜0.95) + 0.4×(0.72〜0.86) |

B2B では**一般より結果がぶれやすい**。会社の標準ブラウザ(Edge か Chrome か)と、VDI を使っているかどうかで、ほぼ 0% からほぼ 100% まで変わる。
導入先ごとに確認ページ(`tools/browser-check/`)を開いてもらうのが確実。

## 3. 実機での確認

### 3.1 確認ページとスクリプト

`tools/browser-check/`:

| ファイル | 内容 |
| --- | --- |
| `index.html` / `check.js` | 静的な確認ページ。外部への送信はない |
| `media/*.mp4` | テスト用の 3 秒の fMP4(640×360、15fps、キーフレーム 1 秒、`empty_moov` + moof 断片)。H.265 Main(`hvc1.1.6.L63.90`)、H.265 Main10(`hvc1.2.4.L63.90`)、比較用の H.264 Main(`avc1.4D4016`) |
| `gen-media.sh` | テスト用の fMP4 を作り直す(Docker の `bluenviron/mediamtx:1.21.1-ffmpeg` の ffmpeg、libx265 / libx264) |
| `run.mjs` | Playwright で Chromium、Chrome(`channel: 'chrome'`)、WebKit、Firefox を開き、`results/<名前>.json` とスクショを保存して表を出す |
| `results/*.json` | 下の結果(スクショは `.gitignore`) |

確認ページが調べること:

- `MediaSource.isTypeSupported`(なければ `ManagedMediaSource`)と `canPlayType`
- `MediaCapabilities.decodingInfo`(`media-source` / `file`、720p / 1080p / 4K / Main10)
- `VideoDecoder.isConfigSupported`(WebCodecs、参考)
- **実再生**: fMP4 を MSE に入れたときと `<video src>` で、フレームが進んで映像が黒くないか
- `RTCRtpReceiver` / `RTCRtpSender.getCapabilities('video')` の H.265
- 送信側に H.265 エンコーダーがあれば、同じページ内の折り返し接続で H.265 だけを使い、受信側で `framesDecoded` が増えるか

```sh
cd tools/browser-check
mise exec -- npm install
mise exec -- npx playwright install firefox   # Firefox を使うとき(webkit / chromium も同様)
mise exec -- npm run check                    # headless。--headed、--only chrome,webkit も使える
# 自分のブラウザで見るとき(スマホからは同じ LAN の IP で)
mise exec -- npm run serve                    # http://localhost:8765/
```

WebRTC の確認には安全なコンテキストが要る。スマホで開くときは HTTPS で配信するか、静的ホスティングに置く。
`localhost` 以外の HTTP では `RTCPeerConnection` は動くが、`getCapabilities` の結果は環境によって変わりうる。

### 3.2 結果(Mac、Apple M5、macOS 26.6.2、2026-10-24)

| ブラウザ | MSE H.265 Main | MSE H.265 Main10 | `<video src>` H.265 | MSE H.264 | WebRTC 受信 H.265 | WebRTC 送信 H.265 | 折り返し | WebCodecs H.265 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Chrome 155.0.8059.85(`channel: 'chrome'`、headless / headed) | ○ | ○ | ○ | ○ | ○ `profile-id=1`、`2` | ○ | —(注 1) | ○(ハードウェアのみ。`prefer-software` は ×) |
| Chromium 156.0.8078.4(Playwright 同梱) | ×(注 2) | × | × | ○ | × | × | — | × |
| WebKit 27.2(Playwright、Safari 相当) | ○ | ○ | ○ | ○ | ○ | ○ | **○(56 フレームをデコード)** | ○ |
| Firefox 157.0(Playwright) | ○ | ○ | ○ | ○ | × | × | — | ×(注 3) |

- `MediaCapabilities.decodingInfo` は、Chrome、WebKit、Firefox で H.265 の 720p〜4K と Main10 のすべてが `supported / smooth / powerEfficient = true` だった。
- 注 1: Chrome の折り返しは、送信側が H.264 でも H.265 でも `framesEncoded = 0` で、フレームが出なかった。
  - VP8 では送れたので、この Mac の自動操作環境で VideoToolbox のエンコーダーが動かないのが原因と見られる。受信の可否とは関係ない。
  - Chrome 155 が KVS からの H.265 を WebRTC で受信して再生できたことは、[webrtc.md](webrtc.md) の試作で確認済み。
- 注 2: Playwright 同梱の Chromium は HEVC に対応しないビルドで、`FFmpegDemuxer: no supported streams` になる。
  **H.265 の E2E テストは `channel: 'chrome'` か WebKit で行う。**
- 注 3: Firefox は MSE / `<video>` では H.265 を再生できる。ただし WebCodecs の `VideoDecoder` は H.265 を受け付けなかった。
- WebKit は `hev1` を `canPlayType` で `""` と返す(`isTypeSupported` は true)。録画の init セグメントは **`hvc1`** にしておく(KVS / kvssink の出力は要確認)。
- Chrome 155 / Chromium は `canPlayType('application/vnd.apple.mpegurl')` に `maybe` を返した。Chrome にネイティブ HLS が入ったとみられるが、未調査。ビューワー(`viewer/src/lib/codec.ts`)は Chrome を UA でネイティブ HLS から外しているので影響しない。

Windows、Android、iPhone の実機では確認していない。確認ページを各環境で開いて JSON を集めれば、表を埋められる。

## 4. 確度の低い点

- **Windows の Edge で HEVC 拡張が入っている割合**。公開データがない。OEM の PC にはプリインストールが多いとされるが、未確認。
  Store の有料版の現在の価格と提供状況も確認できていない。これが日本と B2B の数字の幅のいちばんの原因。
- Windows の Chrome でハードウェアデコーダーがある割合。2025 年の WebRTC の I2S で 75% という数字しかない。
  録画(MSE)の数字は出ていない。Windows 11 への移行で今はもっと高いはず、という推論で上限を 95% にした。
- Edge が WebRTC で H.265 を出さないことの公式な出典。
- Samsung Internet、Opera、Brave の WebRTC H.265。Windows の Opera の MSE。
- iOS 18 以降と macOS 13 以降の比率(WebRTC の Safari 18 の条件)。推定で 85〜95% とした。
- StatCounter はページビューのシェアなので、B2B の利用者層とは違う。

## 5. ビューワーへの反映(案)

- **録画**: 判定は `viewer/src/lib/codec.ts` の `detectSupport()` に既にある(MSE / ManagedMediaSource の `isTypeSupported`、Safari はネイティブ HLS の `canPlayType`)。
  - Chrome は UA でネイティブ HLS から外しているので、Chrome の `canPlayType(HLS) = maybe` の影響はない。
  - false なら「このブラウザでは H.265 を再生できない」と案内し、次の「6. 対応しないユーザーへの選択肢」へ進む。
  - `isTypeSupported` が true でもデコードに失敗することがある(GPU のブロックリストなど。**Edge で HEVC 拡張がないときに true を返すかは未確認**)。hls.js の `BUFFER_APPEND_ERROR` や MediaError も同じ案内にする。
- **WebRTC**: 今の設計どおり、`RTCRtpReceiver.getCapabilities('video')` に `video/H265` があるときだけ H.265(main)を選べるようにし、既定は H.264(sub)。

## 6. 対応しないユーザーへの選択肢

| 選択肢 | 録画 | ライブ | 費用・手間 | 備考 |
| --- | --- | --- | --- | --- |
| A. 案内だけ(Chrome / Safari を使う、Edge に HEVC 拡張を入れる) | ○ | ○ | なし | B2B なら導入時に推奨環境として決められる。Edge の拡張は社内配布(Intune など)でも入れられる |
| B. ライブは sub(H.264)を見せる | — | ○ | 追加なし(既存の設計) | 解像度とビットレートは sub に下がる |
| C. 録画にも sub(H.264)を保存する | ○ | — | 保存量が sub の分(main の 1/4〜1/2 程度)増える | 「互換用の低画質録画」。カメラから 2 本を常に録画する |
| D. 書き出し(ダウンロード)時に H.264 に変換する | △(ダウンロードのみ) | — | 変換の計算費用(Lambda / MediaConvert など。今回は未試算) | 再生はできないが、証拠として提出する用途には足りる |
| E. 再生時にサーバーで H.264 にトランスコードする | ○ | △ | 高い(視聴時間に比例) | 同時に視聴する人が多いと重い。最後の手段 |
| F. WebCodecs + ソフトウェアデコーダー(WASM の HEVC デコーダー) | △ | △ | 開発が重い。HEVC 特許のライセンスに注意 | CPU が重く、720p 程度まで。勧めない |

**勧め**:

- ライブは B(今の設計)。
- 録画はまず A。社内 PC の推奨環境を「Chrome、または HEVC 拡張を入れた Edge」とし、ビューワーで判定して案内を出す。
- 案内では足りない顧客(Edge が標準で Store も止めている、VDI など)が見えてから、C か D を足す。
  C は運用が単純で再生もできるが、ストレージが増える。D は安いが、その場では見られない。
