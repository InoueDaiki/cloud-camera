# KVS WebRTC による低遅延ライブ(調査と試作)

2026-10-24、ap-northeast-1。今のライブ(KVS の HLS LIVE)は数秒遅れるので、PTZ の操作感のために KVS WebRTC(シグナリングチャネル)で
低遅延のライブを出せるかを調べ、エッジのサイドカーとして試作した。録画は今までどおり kvssink で KVS ストリームへ(H.265)送る。
契約([contracts.md](contracts.md))はまだ変えていない。本実装の提案は最後の節にまとめた。

試作のコードは [edge/webrtc/](../edge/webrtc/)(master のコンテナ)と [scripts/webrtc/](../scripts/webrtc/)(確認ビューワー、署名付き URL の発行、
一時リソース、計測)。使い方は [edge/webrtc/README.md](../edge/webrtc/README.md)。

## 結論

- **ライブは H.264(サブストリーム)を基本にし、H.265 は対応ブラウザだけの選択肢にする。** 録画(kvssink、H.265)はそのまま。
  - C SDK は H.265 を送れる(v1.10.2 から)。Chrome 136 以降(ハードウェアデコーダーがある環境)と Safari 18 以降は受信できるが、
    **Firefox は受信できない**。Edge は既定で H.265 を出さない。H.264 は WebRTC の必須コーデックなので、どのブラウザでも受信できる。
  - 試作では Chrome 155(Mac)と WebKit 27.2(Playwright、Safari 相当)で、H.264 も H.265 もエミュレーターの RTSP を再エンコードせずに再生できた。
- **遅延はカメラの焼き込み時刻から画面まで 0.07〜0.14 秒(H.264)。HLS(LIVE)の 4.8 秒から 1/30 以下になる。**
  PTZ の操作から画面の変化までも、HLS の約 5.0 秒に対して WebRTC は 0.2〜0.4 秒。
- **エッジの負荷はほぼ無視できる。** master は視聴者 1 人で CPU 1% 未満、メモリ 30〜40 MiB(Apple Silicon の 1 コア比)。
  視聴者がいないときは RTSP を止めて CPU 0%。上りは視聴者 1 人あたり「カメラのビットレート + 約 15%」で、視聴者数に比例する。
- **認証は今の仕組みのまま足せる。** エッジは IoT の認証情報プロバイダー(既存のロールエイリアスのロールに権限を足す)で master になれる。
  ビューワーは API が署名付き WSS URL と ICE サーバー(TURN の一時認証情報)を発行すれば、ブラウザに AWS の認証情報を渡さずに済む(試作で確認)。
- **料金は小さい。** チャネル 1 本 $0.045/月 + メッセージ $3.375/100 万件 + TURN を使ったときだけ $0.18/1,000 分(東京)。
  P2P で届くときは映像の転送料がかからない(HLS は KVS の取り出し $0.01536/GB がかかる)。
- WebRTC インジェスト(KVS ストリームへの録画)は H.264 だけで、H.265 の録画の置き換えには使えない(置き換えない前提で可否だけ確認)。

## 1. H.265 の対応状況

### C SDK(送信側)

| 項目 | 内容 | 出典 |
| --- | --- | --- |
| H.265 の送信 | v1.10.2(2024-05-09)で追加。「Added support for H.265 codec」(PR #1965、SDP と RTP の payloader / depayloader) | [v1.10.2](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c/releases/tag/v1.10.2)、[PR #1965](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c/pull/1965) |
| その後の修正 | v1.12.0 で H.265 の `rtcp-fb` 行、v1.12.1 で Chrome 136 の大きい SDP に合わせてシグナリングのバッファを拡大、v1.14.0 で Chrome 136 向けの FMTP、v1.20.0 で H.265 の RTX(再送) | [リリース一覧](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c/releases) |
| 制約 | リリースノートに「H265 support is limited in browsers and on Android/iOS devices」。パケット化は RFC 7798 の単一 NAL と FU(断片化)だけで、AP(集約)はない(`src/source/Rtp/Codecs/RtpH265Payloader.c` を読んだ限り) | v1.10.2 のノート |
| 受信側の注意 | 「HEVC/H.265 over WebRTC is only decodable in Chrome where the platform provides a hardware HEVC decoder」。キーフレームごとに VPS/SPS/PPS が要る(`h265parse config-interval=-1`) | [docs/TROUBLESHOOTING_H265_FRAMES.md](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c/blob/v1.20.0/docs/TROUBLESHOOTING_H265_FRAMES.md)、[README](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c/blob/v1.20.0/README.md) |

試作では v1.20.0(2026-08-17)を使った。SDK の answer の H.265 の fmtp は `profile-id=2;level-id=180`(Main10、レベル 6)になるが、
カメラの Main プロファイルの映像は Chrome でそのまま再生できた(SDK の既定値。Chrome 136 に合わせたもの)。

### ブラウザ(受信側)

JS SDK(v2.9.0)はシグナリングだけで、コーデックはブラウザの RTCPeerConnection しだい
([README](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-js)、[v2.9.0](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-js/releases/tag/v2.9.0))。

| ブラウザ | WebRTC の H.265 受信 | 出典 |
| --- | --- | --- |
| Chrome(Mac / Windows / Android) | 136 から既定で有効。ただし**ハードウェアデコーダーがあるときだけ**(ソフトウェア実装は持たない)。Intent to Ship 時点のハードウェア対応率は Windows 75%、macOS 99%、Android 86% | [chromestatus](https://chromestatus.com/feature/5153479456456704)、[Chrome 136](https://developer.chrome.com/release-notes/136)、[blink-dev](https://groups.google.com/a/chromium.org/g/blink-dev/c/3h8lL8a377c/m/_vnatJetAQAJ) |
| Chrome(Linux) | ハードウェアデコーダー(VA-API など)しだい。未確認 | |
| Edge | Chromium ベースだが既定で H.265 を SDP に出さない(Microsoft Q&A のモデレーター回答。公式情報は未確認) | [Microsoft Q&A](https://learn.microsoft.com/en-ca/answers/questions/5880331/h-265-(hevc)-not-published-sent-via-webrtc-chrome) |
| Safari(Mac / iOS / iPadOS) | WebKit で既定有効(2024-01、Cocoa)。RFC 7798 のパケット化は Safari 18.0 から。実用上は **Safari 18 以降**。iOS の Chrome / Edge も WebKit なので同じ(推測) | [WebKit 273829@main](https://github.com/WebKit/WebKit/commit/89d1bc2adb)、[Safari 18.0](https://webkit.org/blog/15865/webkit-features-in-safari-18-0/) |
| Firefox | **未対応**(Bug 2001016、2014236 が未解決) | [Bug 2001016](https://bugzilla.mozilla.org/show_bug.cgi?id=2001016)、[Bug 2014236](https://bugzilla.mozilla.org/show_bug.cgi?id=2014236) |
| H.264 | WebRTC のブラウザは H.264 Constrained Baseline を必ず実装する(RFC 7742 §5)。全ブラウザで問題ない | [RFC 7742](https://www.rfc-editor.org/rfc/rfc7742) |

試作で確認したこと(Mac、Apple Silicon):

- Chrome 155(Playwright の `channel: 'chrome'`、headless)は `RTCRtpReceiver.getCapabilities('video')` に `video/H265` を出し、H.265 1280×720 を再生できた
- WebKit 27.2(Playwright)も H.265 を再生できた(`video/H265 level-id=93;tx-mode=SRST`)
- エミュレーターの sub は H.264 **High** プロファイルだが、SDP は `profile-level-id=42e01f`(Constrained Baseline)のままで Chrome / WebKit とも再生できた。
  実機のカメラでも同じとは限らないので、本番ではサブストリームを Baseline / Main にするか、実機で確認する

**結論**: ブラウザ互換を優先して、ライブは **サブストリーム(H.264)を既定**にする。H.265(メイン)はビューワーが
`RTCRtpReceiver.getCapabilities` で H.265 を確認できたときだけ選べる「高画質」にする(Firefox と Edge では出さない)。

## 2. RTSP を再エンコードせずに WebRTC に流す

- C SDK のサンプル `kvsWebrtcClientMasterGstSample` の `rtspsrc` モードは `uridecodebin ! videoconvert ! x264enc` で**再エンコードする**
  (`samples/common/GstMedia.c`)。Raspberry Pi 4 で 1 本ずつデコードとエンコードをするのは重く、「エッジは再エンコードしない」方針にも反するので使わない。
- 試作の master([edge/webrtc/src/cc_webrtc_master.c](../edge/webrtc/src/cc_webrtc_master.c))は、サンプルの共通処理(`samples/common/Common.c`。
  シグナリング、ピア接続、ICE、TURN、IoT 認証)をそのまま使い、映像だけを appsink から取り出して `writeFrame` する:

  ```
  rtspsrc protocols=tcp latency=0 ! rtph264depay ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! appsink
  rtspsrc protocols=tcp latency=0 ! rtph265depay ! h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au ! appsink
  ```

  - RTP の時刻はカメラの PTS(サンプルは 25fps 固定で時刻を進めるので使わない)
  - 新しい視聴者には最初のキーフレームから送る(途中の P フレームは捨てる)。キーフレーム間隔 1 秒なら、映像が出るまで最大 1 秒待つ
  - 視聴者が 0 人になって 10 秒たったら RTSP を止める(次の視聴者の offer で再開)
  - 帯域推定(TWCC)でエンコーダーのビットレートを変える仕組みは使えない(再エンコードしないため)。回線が細い視聴者にはサブストリームを選ぶ
- kvssink 側は hvc1(長さ前置き)で渡しているが、WebRTC 側は Annex-B(byte-stream)で渡す。同じ RTSP を 2 本の接続で読む(カメラの同時接続数に注意)。

## 3. 認証

### エッジ(master)

C SDK は IoT の認証情報プロバイダーに対応している(`-DIOT_CORE_ENABLE_CREDENTIALS=ON`、`createLwsIotCredentialProvider`)。
README の「Setup IoT」は次の 5 つのアクションを、ロールエイリアスのロールに許可する例を挙げている
([README](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c#setup-iot))。

`kinesisvideo:DescribeSignalingChannel`、`CreateSignalingChannel`(チャネルを事前に作るなら不要)、`GetSignalingChannelEndpoint`、`GetIceServerConfig`、`ConnectAsMaster`

試作では既存と同じ形のロールエイリアス(`cc-tmp-webrtc-kvs-role-alias`)のロールに、PutMedia と並べて上の 4 つ(Create 以外)を
`channel/cc-tmp-webrtc-*/*` に許可し、kvssink と master が**同じ証明書・同じロールエイリアス**で動くことを確認した
([scripts/webrtc/tmp-aws.sh](../scripts/webrtc/tmp-aws.sh))。証明書は `edge/.certs/<gateway_id>/`(`provision-edge.sh` の出力)をそのままマウントした。

### ビューワー

- ConnectAsViewer は SigV4 のクエリ署名(サービス名 `kinesisvideo`)を付けた WSS の URL。署名はサーバーでできる。JS SDK の README も
  「NodeJS backend to sign requests and send them back to a client so that the client does not need to have AWS credentials」と書いている
  ([SigV4RequestSigner](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-js#class-sigv4requestsigner))。
  `SignalingClient` は `requestSigner` を差し替えられるので、署名済みの URL を返すだけの signer を渡せばよい(試作の [viewer.js](../scripts/webrtc/viewer/viewer.js))。
- TURN の認証情報は GetIceServerConfig が返す(有効期間は **300 秒固定**、[IceServer](https://docs.aws.amazon.com/kinesisvideostreams/latest/dg/API_signaling_IceServer.html))。
  これもサーバーで呼んでブラウザに渡せる。
- 署名付き URL の有効期間は JS SDK に合わせて `X-Amz-Expires=299`。KVS が受け付ける上限は公表されていない(未確認)。
  発行から 5 分以内に接続すればよく、接続後は ICE が終われば WSS は閉じてもピア接続は続く。
- 試作の [serve.py](../scripts/webrtc/serve.py) は `cloud-camera` プロファイルの認証情報で署名と GetIceServerConfig を行い、
  ブラウザには `wss_url`、`ice_servers`、`channel_arn`、`client_id` だけを返す。発行にかかった時間は 200〜250 ms(チャネルの ARN とエンドポイントはキャッシュ)。

## 4. 料金(東京)

AWS Price List(`AmazonKinesisVideo`、publicationDate 2026-09-30)で確認した
([ap-northeast-1 の index.json](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonKinesisVideo/current/ap-northeast-1/index.json)、
[料金ページ](https://aws.amazon.com/kinesis/video-streams/pricing/))。

| 項目 | 単価(東京) | us-east-1 | usagetype / SKU | `tools/cost/` に足すキーの案 |
| --- | --- | --- | --- | --- |
| アクティブなシグナリングチャネル | $0.045 / チャネル・月 | $0.03 | `APN1-SignalingChannel` / YJNDCKF3NSS9MZQ6 | `kvs.webrtc_channel_month` |
| シグナリングメッセージ | $3.375 / 100 万件 | $2.25 | `APN1-SignalingMessages` / PNBMYUWEB8PY3KTB | `kvs.webrtc_messages` |
| TURN | $0.18 / 1,000 分 | $0.12 | `APN1-TURNMinutes` / SDC5CE5UJATY9793 | `kvs.webrtc_turn_minutes` |
| (参考)WebRTC インジェストの録画 | $0.0048 / 分 | $0.0034 | `APN1-WebRTCRecording` / SCR8AFJ8M5NEVZ63 | 使わない |
| (参考)インジェスト経由の視聴 | $0.0036 / 分 | $0.003 | `APN1-WebRTCViewer` / 885PAQ33MBWE44R6 | 使わない |

- チャネルは「その月に一度でも接続があれば」アクティブ。master を常時接続するので、カメラ 1 台につき毎月 $0.045
- メッセージは 1 回の視聴の開始で SDP 2 件 + ICE 候補 十数件(推測、未計測)。1 日 30 回視聴して月 2 万件程度 = $0.07
- TURN は TURN を経由したときだけ、1 分単位。P2P で届けば 0。1 日 1 時間を全部 TURN で見ると月 1,800 分 = $0.32
  (視聴者側とエッジ側の両方が TURN を使う場合に 2 本ぶん数えるかは未確認)
- 比較: HLS で 1 日 1 時間見ると、H.265 700 kbps で月 9.5 GB の取り出し = $0.15。WebRTC の P2P は KVS の取り出し料金がかからない
- 単価表への追加: 上の 3 つを `tools/cost/cost/prices.py` の `SPECS` に
  `Spec("kvs.webrtc_channel_month", "AmazonKinesisVideo", "{R}-SignalingChannel", "チャネル・月", ...)` のように足せば、
  Pricing API から自動で取れる(usagetype は上の表)。今回は `tools/cost/` には手を入れていない

## 5. WebRTC インジェスト(KVS ストリームへの録画)

- ConnectAsMaster のあと JoinStorageSession を呼ぶと、AWS 側のストレージセッションが viewer としてつながり、受けた映像を紐付けた
  KVS ストリームに保存する([ingest-media](https://docs.aws.amazon.com/kinesisvideostreams-webrtc-dg/latest/devguide/ingest-media.html))
- **映像は H.264 だけ**(「Video track: H.264 / Audio track: Opus」、[JoinStorageSession](https://docs.aws.amazon.com/kinesisvideostreams/latest/dg/API_webrtc_JoinStorageSession.html))。
  ビットレート 1 Mbps(soft)、セッション 1 時間、ストレージセッション経由の視聴者は最大 3
  ([quotas](https://docs.aws.amazon.com/kinesisvideostreams-webrtc-dg/latest/devguide/kvswebrtc-limits.html))
- 音声: API リファレンスは「Both audio and video tracks are currently required」のままだが、C SDK v1.17.0 のノートに
  2026-03-03 から映像だけのモードが GA とある([v1.17.0](https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c/releases/tag/v1.17.0))
- ストレージを有効にしたチャネルでは P2P の接続がなくなる(「direct peer-to-peer (master-viewer) connections no longer occur」、
  [UpdateMediaStorageConfiguration](https://docs.aws.amazon.com/kinesisvideostreams/latest/dg/API_UpdateMediaStorageConfiguration.html))
- **H.265 の録画(kvssink)の置き換えには使えない。** H.264 に切り替えてよいなら「録画 + 3 人までの視聴」を 1 本にできるが、
  録画の料金が分単位($0.0048/分 = 月 $207/台)になり、PutMedia の取り込み(GB 単位)よりずっと高い

## 6. 同時視聴数、P2P と TURN、常時接続

| 項目 | 値 | 出典 |
| --- | --- | --- |
| master | 1 チャネルに 1(hard)。カメラ 1 台 = チャネル 1 本 | [quotas](https://docs.aws.amazon.com/kinesisvideostreams-webrtc-dg/latest/devguide/kvswebrtc-limits.html) |
| 視聴者 | 1 チャネルに同時 10(soft)。C SDK のサンプル共通処理も `DEFAULT_MAX_CONCURRENT_STREAMING_SESSION 10` | 同上、`samples/common/Samples.h` |
| WSS の接続 | 最大 1 時間、アイドル 10 分(hard)。GO_AWAY のあと 1 分で切れる。C SDK は GO_AWAY / RECONNECT_ICE_SERVER を受けて張り直す | 同上 |
| TURN | 5 Mbps(hard)、1 チャネルに 50 割り当て、認証情報 5 分 | 同上 |
| GetIceServerConfig | 1 チャネル 5 TPS、アカウント 100 TPS | 同上 |
| チャネル数 | 東京はアカウントあたり 5,000 | 同上 |

- **P2P と TURN の切り替えは ICE が自動で行う。** STUN で互いに届く候補が見つかれば P2P、なければ TURN を経由する
  ([How it works](https://docs.aws.amazon.com/kinesisvideostreams-webrtc-dg/latest/devguide/kvswebrtc-how-it-works.html))。
  TURN を強制するには、ブラウザは `RTCPeerConnection({iceTransportPolicy: 'relay'})`、C SDK は `iceTransportPolicy = ICE_TRANSPORT_POLICY_RELAY`
  (サンプル共通処理は環境変数 `KVS_ICE_TRANSPORT_POLICY=relay`)
- **エッジは IoT Core の MQTT とは別に、シグナリングの WSS を常時張る。** master が待ち受けていないと viewer の offer を受けられない。
  上り帯域はほぼ使わない(試作で視聴者 0 人のとき送信 0 kbps)。代わりに、MQTT の cmd で「視聴開始」を受けたときだけ master を接続する
  オンデマンド方式もできるが、最初のフレームまでが数百 ms〜1 秒延び、チャネルの月額は 1 回でも接続すれば同じなので、常時接続を勧める
- 1 時間ごとの WSS の張り直しは今回の計測(各数分)では確かめていない。本実装の前に 2 時間以上の連続稼働で確認する

## 7. 試作の計測

環境: Mac(Apple Silicon、Docker Desktop)。エミュレーター(1 台、main H.265 1280×720 15fps、sub H.264 640×360 15fps、キーフレーム 1 秒)、
既存のエッジ(kvssink で main を `cc-tmp-webrtc-cam-001` に録画)、WebRTC master(サイドカー)を同時に動かした。
ブラウザは Playwright の Chrome 155(`channel: 'chrome'`、headless)と WebKit 27.2。TURN は東京の KVS。

### 遅延(焼き込みの UTC → ブラウザの時計、スクショ)

エミュレーターのエンコードを含む。main(H.265)はエミュレーターの x265 が sub(x264)より約 200 ms 遅れて出る
(同じ GOP のキーフレームの到着を RTSP で比べた)ので、H.265 の数字の大部分はカメラ側の遅れ。

| 方式 | 経路 | 遅延 | スクショ |
| --- | --- | --- | --- |
| WebRTC H.264(sub) Chrome | P2P(同じ LAN 相当) | 71〜102 ms(4 枚) | [chrome-h264-p2p-2.png](webrtc/chrome-h264-p2p-2.png) |
| WebRTC H.264(sub) Chrome | TURN(両側 relay、強制) | 134〜137 ms(3 枚) | [chrome-h264-relay-1.png](webrtc/chrome-h264-relay-1.png) |
| WebRTC H.265(main) Chrome | P2P | 270〜271 ms | [chrome-h265-p2p-2.png](webrtc/chrome-h265-p2p-2.png) |
| WebRTC H.265(main) Chrome | TURN(強制) | 326〜327 ms | [chrome-h265-relay-2.png](webrtc/chrome-h265-relay-2.png) |
| WebRTC H.264 WebKit | TURN(WebKit は自動で relay になった) | 89〜102 ms | [webkit-h264-2.png](webrtc/webkit-h264-2.png) |
| WebRTC H.265 WebKit | TURN | 316 ms | [webkit-h265-2.png](webrtc/webkit-h265-2.png) |
| **HLS LIVE(H.265、今の方式)** Chrome + hls.js | KVS | **4,785〜4,802 ms** | [chrome-hls-2.png](webrtc/chrome-hls-2.png) |

- ブラウザのジッタバッファは 3〜15 ms(P2P)、10〜50 ms(TURN)。RTT は P2P 1 ms、TURN 65〜90 ms
- 接続ボタンから最初のフレームまで 1.3〜3.6 秒(発行 API 0.2 秒 + シグナリング + ICE + 次のキーフレーム待ち)

### PTZ の操作 → ブラウザの映像が変わるまで

エッジのコンテナで既存の PTZ クライアント(`cc_edge.ptz.PtzClient`)から最大倍率への AbsoluteMove を送り、ブラウザ側で
映像の静止した領域の変化を `requestVideoFrameCallback` で検出した時刻との差([measure.mjs](../scripts/webrtc/measure/measure.mjs) `--ptz`)。
MQTT(API → IoT Core → エッジ)の配送は含まない。

| 方式 | 中央値(最小〜最大) | 回数 |
| --- | --- | --- |
| WebRTC H.264 P2P | 264 ms(162〜351、1 回だけ 1,916) | 6 |
| WebRTC H.264 TURN | 215 ms(178〜242) | 5 |
| WebRTC H.265 P2P | 345 ms(342〜392) | 5 |
| WebRTC H.265 TURN | 418 ms(359〜429) | 5 |
| WebRTC H.264 / H.265 WebKit(TURN) | 380 / 423 ms | 3 / 3 |
| **HLS LIVE** | **4,973 ms(4,861〜4,995)** | 3 |

スクショ: [chrome-h264-p2p-ptz.png](webrtc/chrome-h264-p2p-ptz.png)(4 行目が `MOVING`)、[chrome-hls-ptz.png](webrtc/chrome-hls-ptz.png)。
映像の遅延に、エミュレーターが操作を受けて切り出し窓を動かし始めるまでの時間が足される(切り分けていない)。

### エッジの負荷と上り帯域

[docker_stats.py](../scripts/webrtc/measure/docker_stats.py) で 30 秒の平均。CPU は Apple Silicon の 1 コアに対する割合。

| 状態 | master の CPU | master のメモリ | master の送信 | kvssink(既存のエッジ)の CPU |
| --- | --- | --- | --- | --- |
| 視聴者 0 人(シグナリングだけ、RTSP 停止) | 0.0% | 32 MiB | 0 kbps | 0.7% |
| H.264 視聴者 1 人(P2P) | 0.9% | 29 MiB | 435 kbps(映像 357 kbps) | 0.9% |
| H.265 視聴者 1 人(P2P、旧エミュレーター) | 0.7% | 38 MiB | 637 kbps(映像 552 kbps) | 0.9% |
| H.265 視聴者 3 人(P2P、旧エミュレーター) | 4.4%(最大 19.6%) | 47 MiB | 1,827 kbps | 2.2% |
| H.265 視聴者 3 人(P2P、`--cpus 0.25` を両方に) | 2.7%(最大 4.7%) | 38 MiB | 2,343 kbps(映像 2,088 kbps) | 1.4% |

- 上りは「映像のビットレート × 視聴者数 × 約 1.15」(SRTP / RTP / UDP のヘッダー)。視聴者 10 人で H.265 なら約 8 Mbps になる。
  TURN を使っても、エッジからの送信量は同じ(TURN サーバーまでは 1 本ずつ)
- `--cpus 0.25` でも映像は 15fps で届いた(3 つのブラウザのうち 2 つが計測の最後に 12〜14fps。Mac 側で Chrome を 3 つ動かした影響かは切り分けていない)。
  CPU の大半は SRTP の暗号化で視聴者数に比例する。Raspberry Pi 4(Cortex-A72 1.5 GHz)は Apple Silicon の 1 コアより数倍遅いので、
  3 人で 1 コアの 10〜15% 程度と見込む(推測、実機で未計測)
- イメージは arm64 177 MB(展開後)。C SDK と依存ライブラリ、GStreamer の必要なプラグインだけ

### 計測で見つけたこと

- **エミュレーターの RTSP が 1 秒ごとにまとめて届いていた**(同じフィルターグラフに `fps=1` のスナップショットの出力があると、
  FFmpeg がほかの出力も 1 秒ぶんためて出す)。この間は WebRTC の遅延が約 0.93 秒、ブラウザのジッタバッファが 330〜420 ms に膨らんだ。
  エミュレーターの担当が main で修正済み(出力側の `-r 1`)。上の数字はすべて修正後のエミュレーターで測った。kvssink の録画や HLS にも同じ遅れが乗っていたはず
- Docker Desktop(Mac)のコンテナからは、ブラウザの srflx 候補へのヘアピンが通らず、自動では TURN になった。
  Chrome の host 候補を mDNS で隠さない設定(`--disable-features=WebRtcHideLocalIpsWithMdns`)にすると、コンテナから Mac の LAN の IP へ届いて P2P になった。
  実機(Raspberry Pi の上の Docker、`--network host`)では、同じ LAN の視聴者は host 候補で P2P になる見込み
- headless の Chrome で、KVS の STUN / TURN のホスト名の DNS 解決がときどき 701 エラーになるが、ほかの候補で接続できた。
  トリクル ICE の途中で `connectionState` が一瞬 `failed` になってから `connected` になることがある(ビューワーは `failed` ですぐ諦めない)

## 8. 本実装の提案

### エッジへの統合

**推奨: 既存のエッジのイメージに master のバイナリを入れ、Python エージェントがカメラごとに子プロセスとして起動する。**

- 今の runner(kvssink の子プロセス)と同じ管理: エージェントが ONVIF で解決した RTSP URL を渡し、バックオフ付きで再起動し、
  master が 10 秒ごとに出す JSON 行(`cc_stats`: 視聴者数、送信量、到着の揺らぎ)を status に載せる
- 1 カメラ = 1 チャネル = 1 プロセス(1 チャネルに master は 1 つ)。証明書とロールエイリアスは kvssink と共通
- RTSP は kvssink(main)とは別の接続で sub を読む。視聴者がいないときは RTSP を閉じる
- イメージは C SDK のビルドステージを足すだけ(実行時に足すライブラリは libwebsockets、libsrtp2、usrsctp、SDK の .so と GStreamer の app / videoparsersbad など)
- サイドカー(別コンテナ)のままにする案もある。録画と独立して落ちる・更新できる利点はあるが、RTSP URL とカメラ一覧をシャドウから別に読む必要があり、
  ONVIF の処理が二重になる

### シャドウ(契約の変更案)

`desired.cameras.{id}` に足す(既定はすべて省略可):

```json
{"webrtc": {"enabled": true, "profile": "sub", "h265_profile": "main"}}
```

`reported.cameras.{id}.webrtc` にエッジが返す: `{"enabled", "channel", "state": "CONNECTED|ERROR", "viewers", "codec", "error"?}`。
`{p}/{gw}/status` の各カメラに `webrtc_viewers`、`webrtc_sent_kbps` を足す。

### チャネルと IAM

- チャネル名 `{p}-{camera_id}`(ストリームと同じ名前。リソースの種類が違うので衝突しない)。`SINGLE_MASTER`、`MessageTtlSeconds` 60
- Terraform: CloudFormation には `AWS::KinesisVideo::SignalingChannel` がある
  ([CloudFormation](https://docs.aws.eu/AWSCloudFormation/latest/TemplateReference/aws-resource-kinesisvideo-signalingchannel.html))。
  hashicorp/aws プロバイダーにチャネルのリソースがあるかは未確認。なければ awscc プロバイダーか `aws_cloudformation_stack`、
  またはエッジに `CreateSignalingChannel` を許してエッジが作る(kvssink がストリームを自動で作るのと同じ)
- ロールエイリアス `{p}-kvs-role-alias` のロール(`{p}-kvs-producer`)に足す:
  `kinesisvideo:DescribeSignalingChannel`、`GetSignalingChannelEndpoint`、`GetIceServerConfig`、`ConnectAsMaster` を `channel/{p}-*/*` に
  (ストリームの権限と同じ粒度。ゲートウェイごとに絞るなら、チャネル名を `{thing}-{camera_id}` にして `${credentials-iot:ThingName}` を使う)
- API の Lambda のロールに足す: `DescribeSignalingChannel`、`GetSignalingChannelEndpoint`、`GetIceServerConfig`、`ConnectAsViewer` を `channel/{p}-*/*` に

### API

`GET /api/cameras/{id}/webrtc?codec=h264|h265` → 

```json
{"channel_arn": "...", "region": "ap-northeast-1", "client_id": "viewer-<ランダム>",
 "wss_endpoint": "wss://...", "wss_url": "wss://...?X-Amz-...(299 秒)",
 "ice_servers": [{"urls": ["stun:stun.kinesisvideo.ap-northeast-1.amazonaws.com:443"]},
                 {"urls": ["turn:...?transport=udp", "turns:...?transport=tcp"], "username": "...", "credential": "..."}],
 "expires_at": "2026-10-24T05:09:57Z"}
```

試作の [serve.py](../scripts/webrtc/serve.py) の `issue_viewer_session` をそのまま Lambda に移せる(boto3 の `kinesisvideo` と `kinesis-video-signaling`、
botocore の `SigV4QueryAuth`)。チャネルの ARN とエンドポイントは Lambda のコンテナ内でキャッシュする(DescribeSignalingChannel は 1 チャネル 5 TPS)。
`client_id` はリクエストごとにランダムにする(視聴者の区別に使う)。エラーは既存と同じ `{"error": {...}}`
(`CAMERA_OFFLINE`、`WEBRTC_DISABLED` など)。

### ビューワー

- LIVE の再生を WebRTC にし、失敗(10 秒以内に映像が来ない、`failed` が 5 秒続く)したら今の HLS(`kvs-hls?mode=LIVE`)に切り替える
- `amazon-kinesis-video-streams-webrtc`(2.9.0)を npm で入れ、`requestSigner: { getSignedURL: async () => wss_url }` で使う
- H.265 は `RTCRtpReceiver.getCapabilities('video')` に `video/H265` があるときだけ「高画質」として選べるようにし、
  `transceiver.setCodecPreferences` で H.265 だけ(+ rtx)を offer に載せる。既定は H.264(sub)
- ICE が終わったら WSS は閉じてよい(ピア接続は続く)。WSS の 1 時間制限はピア接続には関係しない(未確認。長時間の視聴で確かめる)
- PTZ のパネルは WebRTC 再生中に有効にする(HLS では 5 秒遅れて操作感が悪い)

## 9. 試作の再現と一時リソース

- 一時リソース(すべて `cc-tmp-webrtc` で始まる): IAM ロール、ロールエイリアス、IoT ポリシー、Thing、証明書、KVS ストリーム、シグナリングチャネル。
  `scripts/webrtc/tmp-aws.sh up / down / check`
- 2026-10-24 の試作のあと `down` で削除し、`check` で何も残っていないことを確認した
