# エッジゲートウェイ

ONVIF カメラの H.265 を**再エンコードせずに** Kinesis Video Streams(KVS)へ送るコンテナ。
Raspberry Pi 4(linux/arm64)向けで、Mac(Apple Silicon の Docker)でもそのまま動く。linux/amd64 もビルドできる。

```
カメラ ──RTSP(H.265)──▶ rtspsrc ! rtph265depay ! h265parse ! video/x-h265,stream-format=hvc1 ! h265timestamper ! kvssink ──▶ KVS
   ▲                                                                                                         (IoT 認証情報プロバイダー)
   └─ONVIF(WS-Discovery、Media2)── Python エージェント ──MQTT5──▶ AWS IoT Core(シャドウ、status、discovery、cmd)
```

## 構成

| パス | 中身 |
| --- | --- |
| `Dockerfile` | マルチステージ。producer-sdk-cpp **v3.6.0**(producer-c v1.6.2)の kvssink をビルドし、必要な GStreamer プラグインの .so だけを実行イメージに入れる(Debian trixie、約 260 MB) |
| `agent/cc_edge/agent.py` | 本体。シャドウ(またはローカル設定)の差分適用、reported、status(30 秒)、cmd、discovery |
| `agent/cc_edge/iot.py` | AWS IoT Device SDK v2(MQTT5)。LWT、再接続ごとの再購読 |
| `agent/cc_edge/onvif.py` | 標準ライブラリだけの ONVIF クライアント。WS-Discovery、WS-Security Digest、Media2 で H.265 を自動選択、GetStreamUri |
| `agent/cc_edge/ptz.py` | PTZ。ONVIF PTZ クライアント(GetServices で XAddr、プロファイルの PTZConfiguration、各操作)、cmd `ptz` の処理、`{p}/{gw}/ptz` への結果、シャドウの `reported.cameras.{id}.ptz` |
| `agent/cc_edge/pipeline.py` | パイプラインの仕様と、子プロセスの監視・バックオフ付き再起動 |
| `agent/cc_edge/runner.py` | 1 カメラ = 1 子プロセス。GStreamer を GI で組み、送信バイト数、GOP、KVS の ACK(永続化までの遅れ)を計測して JSON 行で親に渡す |
| `scripts/tmp-aws.sh` | Terraform を使わずに実 AWS で試すための一時リソース(既定 `cc-tmp-edge-*`、`TMP_PROJECT` で変更)の作成・削除 |
| `scripts/outage-test.sh`、`scripts/outage/` | 回線断試験(エッジから AWS への通信だけを iptables で遮断し、復旧後の ListFragments を集計) |
| `tests/` | 単体テスト(ONVIF は SOAP を返す小さな HTTP サーバーで検証) |

## 設定

環境変数 > 設定ファイル(`CC_CONFIG_FILE`、既定 `/certs/config.json`)> 既定値。
`/certs` にはデバイス証明書 `device.pem.crt`、秘密鍵 `private.pem.key`、ルート CA `AmazonRootCA1.pem` を置く
(`scripts/provision-edge.sh` が `config.json` と一緒に書き出す)。

| 環境変数 | 既定 | 内容 |
| --- | --- | --- |
| `CC_PROJECT` / `CC_GATEWAY_ID` | `cloud-camera` / `gw-001` | トピック `{p}/{gw}/...`、Thing `{p}-{gw}`、ストリーム `{p}-{camera_id}` |
| `CC_IOT_ENDPOINT` | | IoT のデータエンドポイント(ATS)。空か証明書がなければ MQTT に接続しない |
| `CC_IOT_CREDENTIAL_ENDPOINT` / `CC_KVS_ROLE_ALIAS` | / `{p}-kvs-role-alias` | kvssink の `iot-certificate` に渡す |
| `CC_USE_SHADOW` | `true` | `false` ならシャドウを使わず `CC_CAMERAS`(または設定ファイルの `cameras`)だけで動く |
| `CC_CAMERAS` | | JSON。シャドウの `desired.cameras` と同じ形。`rtsp_url` を書けば ONVIF を省略 |
| `CC_SINK` | `kvs` | `kvs` / `fake`(ドライラン)/ `file`(ドライラン、`CC_OUTPUT_DIR` に MKV)。`CC_DRY_RUN=1` は `fake` の別名 |
| `CC_STATUS_INTERVAL` | `30` | status と `reported.last_seen` の間隔(秒) |
| `CC_STALL_TIMEOUT` | `15` | この秒数カメラからフレームが来なければパイプラインを再起動(KVS 側の回線断では再起動しない) |
| `CC_BACKOFF_INITIAL` / `CC_BACKOFF_MAX` | `2` / `60` | 再起動のバックオフ(30 秒以上動けば初期値に戻る) |
| `CC_RTSP_PROTOCOLS` / `CC_RTSP_LATENCY_MS` | `tcp` / `200` | rtspsrc |
| `CC_KVS_BUFFER_S` / `CC_KVS_STORAGE_SIZE_MB` | `600` / `128` | 回線断の間に溜める上限(秒 / MB、1 カメラあたり)。どちらかに達したら古い方から捨てる。下の「回線断とバッファ」 |
| `CC_KVS_CONNECTION_STALENESS_S` / `CC_KVS_REPLAY_S` | `30` / `60` | ACK が途絶えて接続を張り直すまでの秒数 / 張り直したときに巻き戻して送り直す最大の秒数 |
| `CC_KVS_FRAMERATE` | `0`(30 とみなす) | kvssink の framerate。バッファの枠(フレーム数)= framerate × `CC_KVS_BUFFER_S` なので実際の fps 以上にする |
| `CC_KVS_RETENTION_HOURS` | `1` | ストリームを自動作成するときの保持期間 |
| `CC_LOG_LEVEL` | `INFO` | `DEBUG` で 5 秒ごとの計測値も出る。kvssink 自体のログは `/opt/kvs/kvs_log_configuration`(既定 WARN) |

## Mac で動かす(リポジトリ直下で)

```sh
# ドライラン: エミュレーター(2 台)→ ONVIF で H.265 を選んで → fakesink
docker compose up --build
# MKV に保存して中身を確かめる(edge/.out/)
CC_SINK=file docker compose up --build
# 実 AWS(Terraform を適用済みなら)
scripts/provision-edge.sh gw-001
CC_SINK=kvs CC_USE_SHADOW=true docker compose up --build
```

Terraform なしで実 AWS を試すとき:

```sh
mise exec -- edge/scripts/tmp-aws.sh up      # cc-tmp-edge-* を作り edge/.certs/gw-001/ に書き出す
CC_PROJECT=cc-tmp-edge CC_SINK=kvs CC_USE_SHADOW=true docker compose up --build
# シャドウの desired.cameras を書くと配信が始まる(例は docs/contracts.md)
mise exec -- edge/scripts/tmp-aws.sh down    # 全部消して、残りがないことを表示
```

単体テスト:

```sh
cd edge && mise exec -- uv venv .venv && mise exec -- uv pip install --python .venv/bin/python awsiotsdk==1.31.0 pytest
.venv/bin/python -m pytest
```

## PTZ

契約は [docs/contracts.md](../docs/contracts.md) の「PTZ」。ビューワー → API → IoT Core の `{p}/{gw}/cmd` → エッジ → ONVIF PTZ → カメラ。

- 起動時(とシャドウの desired でカメラの設定が変わったとき)に、カメラごとに GetServices で PTZ サービスを探し
  (なければ GetCapabilities)、Media2(なければ Media)の GetProfiles で PTZConfiguration の付いたプロファイルを選ぶ
  (`profile_token` の指定があればそれ、なければ H.265 のプロファイル)。GetPresets と GetStatus を取り、
  シャドウの `reported.cameras.{id}.ptz` に `{"supported", "pan", "tilt", "zoom", "presets", "move_status", "error", "updated_at"}` を書く
- PTZ サービスがない、PTZConfiguration がない、`rtsp_url` を直接使う設定(ONVIF なし)のカメラは `supported: false`。
  つながらないときも `supported: false` と `error` を書き、次の cmd で準備し直す。非対応のカメラへの cmd は `ok: false` の結果を返す
- cmd はカメラごとに届いた順に 1 つずつ処理する(押している間の continuous と、離したときの stop が逆転しない)。
  値はもう一度範囲に収める。continuous の `timeout_ms`(既定 10 秒)は ONVIF の Timeout に渡す(stop を取りこぼしても止まる)
- 結果は `{p}/{gw}/ptz` に `{"ts", "gateway_id", "camera_id", "request_id", "ok", "error"?, "position", "move_status"}`
  (set_preset は `preset_token` も)。そのあと GetStatus を 0.5 秒ごとに見て、動いている間は 1 秒に 1 回まで、止まったら必ず 1 回シャドウに書く

MQTT なしで、エミュレーターに cmd を直接流して確かめる:

```sh
docker compose up -d --build
docker compose exec edge python3 -m cc_edge.ptz http://emulator:8080/onvif/device_service \
  '{"op":"continuous","pan":0.5,"timeout_ms":3000}' '{"sleep":2}' '{"op":"stop"}' '{"op":"home"}'
```

`result` と `shadow[...]` の行に、エッジが送るはずの内容がそのまま出る。

## Raspberry Pi 4 への配置

前提: 64 bit の Raspberry Pi OS(Bookworm 以降、arm64)と Docker Engine。

1. **イメージの配布**(どちらか)
   - レジストリ経由(推奨。台数が増えても同じ手順):
     ```sh
     aws ecr create-repository --repository-name cloud-camera-edge   # 初回のみ
     aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.ap-northeast-1.amazonaws.com
     docker buildx build --platform linux/arm64,linux/amd64 -t <account>.dkr.ecr.ap-northeast-1.amazonaws.com/cloud-camera-edge:<tag> --push edge/
     ```
     Pi 側は読み取り専用の認証情報で `docker pull`(または同じ `docker login`)。
   - レジストリなし(検証用):
     ```sh
     docker buildx build --platform linux/arm64 -t cc-edge:<tag> --load edge/
     docker save cc-edge:<tag> | gzip | ssh pi@raspberrypi 'gunzip | docker load'
     ```
2. **証明書と設定**: `scripts/provision-edge.sh gw-001` で作った `edge/.certs/gw-001/` を Pi の `/etc/cc-edge/certs/` に置く
   (`scp -r edge/.certs/gw-001 pi@raspberrypi:/tmp/ && sudo install -d -m 700 /etc/cc-edge && sudo mv /tmp/gw-001 /etc/cc-edge/certs`)。
3. **起動**:
   ```sh
   docker run -d --name cc-edge --restart unless-stopped \
     --network host \
     --stop-timeout 30 \
     -v /etc/cc-edge/certs:/certs:ro \
     --log-opt max-size=10m --log-opt max-file=3 \
     <image>:<tag>
   ```
   - `--network host`: WS-Discovery のマルチキャスト(239.255.255.250:3702)をカメラの LAN に出すため。
     ブリッジネットワークでは同じ Docker ネットワーク内にしか届かない(ONVIF の URL がシャドウにあれば配信自体は動く)。
   - `--stop-timeout 30`: 停止時に kvssink のバッファを吐き出し、`reported.online=false` と LWT を送る猶予。
4. **時刻(NTP)は必須**: ProducerTimestamp はゲートウェイの壁時計(最初のフレームを受け取った時刻 + PTS の差分)で、
   1 分区間の切り出しはこの時刻で行う。Pi は RTC を持たないので、起動直後は時計が合っていないことがある。
   ```sh
   timedatectl status                 # "System clock synchronized: yes" を確認
   sudo timedatectl set-ntp true      # systemd-timesyncd(chrony を使うなら chronyc tracking で確認)
   ```
   起動時に同期を待つなら `systemd-time-wait-sync.service` を有効にし、コンテナを systemd から起動する。
   カメラ側も NTP を合わせ、キーフレーム間隔は 60 秒を割り切れる値(1 秒か 2 秒)、解像度とプロファイルを固定する。

## 確かめた挙動(2026-10-24、ap-northeast-1)

- kvssink は H.265 の caps(`video/x-h265`)を見て codec-id `V_MPEGH/ISO/HEVC`、content-type `video/h265` を自動で設定する
- **kvssink は NAL 変換をしない**(`nal_adaptation_flags=0`)。コーデックプライベートデータは caps の `codec_data` からだけ取るので、
  h265parse の出力を `stream-format=hvc1`(HEVCDecoderConfigurationRecord)に固定する必要がある
- **RTSP から取り出したフレームには DTS がない**。kvssink は「HEADER フラグ(VPS/SPS/PPS を含む)で DTS が無効」なバッファを捨てるため、
  そのままでは全キーフレームが捨てられ、PutMedia は 30 秒ほどで切れてフラグメントが 1 つもできない。`h265timestamper` で解決
- フラグメントはキーフレームごと(キーフレーム間隔 1 秒 → 1 秒ごと)。ListFragments に見えるまで、フラグメントの終わりから中央値 0.5 秒、最大 0.9 秒
- 停止(EOS)時は途中までのフラグメント(例: 400ms)も送られる
- 読み出し側(H.265、1280x720、キーフレーム間隔 1 秒のストリームで確認):
  - GetImages: H.265 で動く(JPEG/PNG)。`WidthPixels` と `HeightPixels` は両方指定するか両方省略する。
    `MaxResults=100` でも 1 ページ 25 枚で返るので、60 枚には 3 回呼ぶ。終わりの時刻を含むので 1 分を 1 秒間隔で取ると 61 枚
  - HLS(FRAGMENTED_MP4): ON_DEMAND / LIVE とも取れる。`CODECS="hvc1.1.6.L93.90"`、init に `hvc1` + `hvcC`、セグメントは KVS のフラグメント 1 つ(1 秒)
  - GetClip: H.265 の MP4(`hvc1`)。60 秒で 1.0 秒、ちょうど 60.000 秒
  - 保持期間ちょうど 1 時間のストリームは CLI(`--data-retention-in-hours 1`)でも kvssink の自動作成(`retention-period=1`)でも作れる
