# エッジの KVS WebRTC master

カメラの RTSP を**再エンコードせずに** KVS WebRTC のシグナリングチャネル(master)へ流す C のプログラム。低遅延のライブ(PTZ の操作感)用。
録画は今までどおり kvssink(H.265、カメラごとに H.264 も可)。調査、計測、設計は [docs/webrtc.md](../../docs/webrtc.md)、契約は [docs/contracts.md](../../docs/contracts.md)。

```
カメラ ─RTSP main(H.265)─▶ rtspsrc ! rtph265depay ! h265parse ! appsink ─┐  H.265 を受けられる視聴者
       ─RTSP sub (H.264)─▶ rtspsrc ! rtph264depay ! h264parse ! appsink ─┴─ writeFrame ─▶ それ以外の視聴者(P2P か TURN)
                 C SDK v1.20.0(シグナリング WSS、ICE、DTLS-SRTP)── IoT 認証情報プロバイダー(kvssink と同じロールエイリアス)
```

- エッジのイメージ([edge/Dockerfile](../Dockerfile) の `webrtc-build` ステージ)でビルドし、`/opt/kvs/webrtc/bin/ccWebrtcMaster` に置く
  (SDK の共有ライブラリは kvssink の `libkvsCommonLws.so` と同名の別の版なので `/opt/kvs/webrtc/lib` に分ける)
- エージェント([agent/cc_edge/webrtc.py](../agent/cc_edge/webrtc.py))がカメラごとに 1 プロセス起動し、落ちたらバックオフ付きで起動し直す。
  RTSP の URL は録画側が ONVIF で解決した main と、main 以外の H.264 のプロファイル(sub)
- 視聴者ごとのコーデック: offer の m=video の並びで H.265 が H.264 より前(か H.265 だけ)なら main、そうでなければ sub。
  `pConf->videoCodec` を offer ごとに差し替えてからサンプル共通処理(`samples/common/Common.c`)にセッションを作らせるので、answer にはそのコーデックだけが載る
- RTSP は視聴者がいるストリームだけ読み、そのストリームの視聴者が 0 人になって `CC_IDLE_STOP_S` 秒で閉じる。エラーやフレームの途絶(`CC_STALL_TIMEOUT_S`)はバックオフ付きでつなぎ直す
- 新しい視聴者には次のキーフレームから送る(キーフレーム間隔 1 秒なら最大 1 秒待つ)。RTP の時刻はカメラの PTS
- 回線断: シグナリングが切れたら SDK(サンプル共通処理の `sessionCleanupWait`)がつなぎ直す。録画のプロセスとは別なので、kvssink のバッファには触らない

## 環境変数(エージェントが設定する)

| 環境変数 | 既定 | 内容 |
| --- | --- | --- |
| `CC_WEBRTC_CHANNEL` | (必須) | シグナリングチャネル名(`{p}-{camera_id}`) |
| `CC_MAIN_RTSP_URL` / `CC_MAIN_CODEC` | / `h265` | main の RTSP とコーデック |
| `CC_SUB_RTSP_URL` / `CC_SUB_CODEC` | / `h264` | sub の RTSP とコーデック(main と sub のどちらかは必須) |
| `CC_RTSP_USER` / `CC_RTSP_PASS` | | RTSP の認証 |
| `CC_RTSP_LATENCY_MS` | `0` | rtspsrc の latency |
| `CC_IDLE_STOP_S` | `10` | 視聴者が 0 人になってから RTSP を閉じるまでの秒数(エッジの `CC_WEBRTC_IDLE_STOP_S`) |
| `CC_STALL_TIMEOUT_S` | `10` | 視聴者がいるのにフレームが来なければ RTSP をつなぎ直す秒数 |
| `CC_STATS_INTERVAL_S` | `10` | 統計(JSON 1 行)の間隔 |
| `KVS_ICE_TRANSPORT_POLICY` | `all` | `relay` で TURN を強制(エッジの `CC_WEBRTC_ICE_TRANSPORT_POLICY`) |
| `AWS_IOT_CORE_*`、`AWS_KVS_CACERT_PATH` | | IoT 認証情報プロバイダー(エッジの config.json から) |
| `AWS_KVS_LOG_LEVEL` | `3` | SDK のログ(1 verbose 〜 4 warn。エッジの `CC_WEBRTC_LOG_LEVEL`) |

標準出力の JSON 行(エージェントが読む): `{"cc_event": "ready" | "signaling" | "viewer" | "reject" | "rtsp_error" | "exit", ...}` と
`{"cc_stats": true, "viewers", "sent_kbps", "sent_bytes", "streams": {"main": {...}, "sub": {...}}}`(10 秒ごと)。

## 試作との違い

試作(サイドカーのコンテナ、2026-10-24 午前)は 1 プロセス 1 コーデックで、`CC_RTSP_URL` / `CC_WEBRTC_CODEC` を受けていた。
本実装では main と sub を 1 プロセスで扱い、視聴者ごとにコーデックを選ぶ。サイドカーの Dockerfile と compose は削除した。
試作の確認ビューワーと計測([scripts/webrtc/](../../scripts/webrtc/))は、エッジ(リポジトリ直下の compose)が master を動かしていればそのまま使える。

## 制約

- 映像だけ(音声トラックは受信専用で用意するだけ)
- 視聴者は 1 チャネル最大 10(チャネルの上限、サンプル共通処理の上限とも 10)
- 再エンコードしないので、帯域推定(TWCC)によるビットレート調整はしない。回線が細い視聴者は H.264(sub)を選ぶ(ビューワーの `?live=h264`)
- シグナリングの WSS は 1 時間で張り直しになる(SDK が処理する)。2 時間以上の連続稼働は未確認
