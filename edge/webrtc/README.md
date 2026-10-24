# エッジの KVS WebRTC master(試作、サイドカー)

カメラの RTSP を**再エンコードせずに** KVS WebRTC のシグナリングチャネル(master)へ流すコンテナ。低遅延のライブ(PTZ の操作感)用。
録画は既存のエッジ(kvssink、H.265)のまま。調査結果、計測、本実装の提案は [docs/webrtc.md](../../docs/webrtc.md)。

```
カメラ ──RTSP(sub: H.264 / main: H.265)──▶ rtspsrc ! rtph26xdepay ! h26xparse config-interval=-1 ! appsink ─▶ writeFrame ─▶ 視聴者(P2P か TURN)
                                                       C SDK(シグナリング WSS、ICE、DTLS-SRTP)── IoT 認証情報プロバイダー
```

## 構成

| パス | 中身 |
| --- | --- |
| `Dockerfile` | マルチステージ。amazon-kinesis-video-streams-webrtc-sdk-c **v1.20.0** をビルドし、`src/` の master をサンプル共通処理(`samples/common/Common.c`)と一緒にリンク。arm64 / amd64(実行イメージは arm64 で約 177 MB) |
| `src/cc_webrtc_master.c` | master 本体。映像の取り出し(appsink)、キーフレーム待ち、視聴者 0 人で RTSP を止める、10 秒ごとの統計(JSON 1 行) |
| `entrypoint.sh` | `/certs/config.json`(`scripts/provision-edge.sh` の出力)から IoT 認証情報プロバイダーの環境変数を組み立てる |
| `compose.yaml` | 試作用: エミュレーター + 既存のエッジ(kvssink)+ この master |

## 設定(環境変数)

| 環境変数 | 既定 | 内容 |
| --- | --- | --- |
| `CC_RTSP_URL` | (必須) | RTSP の URL |
| `CC_WEBRTC_CODEC` | `h264` | `h264` / `h265`(RTSP の中身と合わせる) |
| `CC_CAMERA_ID` / `CC_WEBRTC_CHANNEL` | `cam-001` / `{project}-{camera_id}` | シグナリングチャネル名 |
| `CC_KVS_ROLE_ALIAS` | config.json の `kvs_role_alias` | ロールエイリアス(kvssink と共通) |
| `CC_RTSP_LATENCY_MS` | `0` | rtspsrc の latency |
| `CC_IDLE_STOP_S` | `10` | 視聴者が 0 人になってから RTSP を止めるまでの秒数 |
| `CC_STATS_INTERVAL_S` | `10` | 統計を出す間隔 |
| `CC_TIMESTAMP` | `pts` | RTP の時刻。`pts`(カメラの PTS)/ `arrival`(受け取った時刻。カメラの送り出しが揺れるときの比較用) |
| `KVS_ICE_TRANSPORT_POLICY` | `all` | `relay` で TURN を強制 |
| `AWS_KVS_LOG_LEVEL` | `3` | SDK のログ(1 verbose 〜 4 warn) |

統計の例: `{"cc_stats": true, "viewers": 1, "frames_in": 1875, "key_frames": 125, "frames_sent": 1703, "wait_key_drops": 15, "in_kbps": 357.9, "sent_kbps": 357.9, "arrival_spread_ms": 80.1}`
(`arrival_spread_ms` は 10 秒間の「受け取った時刻 − PTS」の最大 − 最小。カメラの送り出しの揺れの目安)

## 動かす(リポジトリ直下で)

```sh
mise exec -- scripts/webrtc/tmp-aws.sh up                    # cc-tmp-webrtc-* を作り、edge/.certs/gw-001/ に証明書と config.json
docker compose -f edge/webrtc/compose.yaml up -d --build      # sub(H.264)を WebRTC、main(H.265)を kvssink で録画
mise exec -- uv run --with boto3 scripts/webrtc/serve.py      # 署名付き URL と ICE サーバーの発行 + 確認ビューワー
open "http://127.0.0.1:8765/?camera=cam-001&autostart=1"

# H.265(main)を WebRTC で流す
CC_WEBRTC_RTSP_URL=rtsp://emulator:8554/cam1/main CC_WEBRTC_CODEC=h265 docker compose -f edge/webrtc/compose.yaml up -d webrtc

# 計測(Playwright。Chrome は channel: 'chrome')
cd scripts/webrtc/measure && mise exec -- npm install
mise exec -- node measure.mjs --codec h264 --expose-host --shots 4 --ptz 5 --out out   # P2P(Mac の LAN の IP で)
mise exec -- node measure.mjs --codec h264 --relay --shots 4 --ptz 5 --out out         # TURN を強制
mise exec -- node measure.mjs --browser webkit --codec h265 --out out
mise exec -- node measure.mjs --mode hls --ptz 3 --out out                             # 比較用の HLS LIVE
CC_EDGE_CPUS=0.25 CC_WEBRTC_CPUS=0.25 docker compose -f edge/webrtc/compose.yaml up -d edge webrtc   # CPU 制限
OUT=out mise exec -- ./load.sh 3 h265 load-h265-3v --expose-host                       # 視聴者 3 人の負荷と帯域

docker compose -f edge/webrtc/compose.yaml down
mise exec -- scripts/webrtc/tmp-aws.sh down                  # 全部消して、残りがないことを表示
```

## 制約(試作)

- 映像だけ(音声トラックは受信専用で用意するだけ)。データチャネルはサンプル共通処理のまま
- 視聴者は最大 10(チャネルの上限、サンプル共通処理の上限とも 10)
- 再エンコードしないので、帯域推定(TWCC)によるビットレート調整はしない。新しい視聴者は次のキーフレームから映る
- シグナリングの WSS は 1 時間で張り直しになる(SDK が処理する)。2 時間以上の連続稼働は未確認
