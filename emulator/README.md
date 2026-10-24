# ONVIF カメラエミュレーター

1 つのコンテナで複数台の ONVIF カメラ(Profile S + Media2)を模擬する。エッジゲートウェイの開発と、
クラウド側(KVS、録画、物体検出)の検証に使う。arm64(Raspberry Pi、Apple Silicon)と amd64 で動く。

```
┌──────────────────── コンテナ ────────────────────┐
│ python -m onvif_emulator(asyncio、1 プロセス)    │
│  ├ ONVIF SOAP(aiohttp)  :8080 cam1, :8081 cam2 … │
│  ├ WS-Discovery           udp/3702(239.255.255.250)│
│  └ プロセス監視(再起動、ウォッチドッグ)         │
│      ├ MediaMTX           :8554 RTSP(udp 8000/8001)│
│      └ FFmpeg × 台数  testsrc / 動画ループ         │
│          ├ main  H.265 libx265 → /cam{n}/main      │
│          ├ sub   H.264 libx264 → /cam{n}/sub       │
│          └ 1 秒ごとの JPEG(スナップショット)      │
└───────────────────────────────────────────────────┘
```

## 起動

```sh
docker compose -f emulator/compose.yaml up --build        # 2 台(cam-001、cam-002)
EMULATOR_CAMERAS=4 docker compose -f emulator/compose.yaml up
```

| 用途 | URL |
| --- | --- |
| ONVIF デバイスサービス(n 台目) | `http://<host>:{8080+n-1}/onvif/device_service` |
| Media / Media2 | `…/onvif/media_service`、`…/onvif/media2_service`(GetServices / GetCapabilities でも取れる) |
| RTSP | `rtsp://<host>:8554/cam{n}/main`(H.265)、`rtsp://<host>:8554/cam{n}/sub`(H.264) |
| スナップショット | `http://<host>:{8080+n-1}/onvif/snapshot.jpg` |
| 状態(JSON) | `http://<host>:8080/healthz`(全カメラの FFmpeg の fps、再起動回数など) |

認証情報の既定は `admin` / `admin`。リポジトリ直下の `compose.yaml` からは `include: [emulator/compose.yaml]` で取り込む
(サービス名 `emulator`。同じネットワークのコンテナから `http://emulator:8080/onvif/device_service` で届く)。

ホスト側のポートがほかと衝突するときは `EMULATOR_RTSP_HOST_PORT`、`EMULATOR_ONVIF_PORTS`(例 `8080-8083`)、
`EMULATOR_RTP_HOST_PORT`、`EMULATOR_RTCP_HOST_PORT`、`EMULATOR_DISCOVERY_HOST_PORT` で変えられる(コンテナ内のポートは変わらない)。

## 実装している ONVIF

| サービス | 操作 |
| --- | --- |
| WS-Discovery | 起動時の Hello(2 回)、終了時の Bye、Probe → カメラごとの ProbeMatches(EndpointReference と XAddrs がカメラごとに別)、Resolve |
| Device | GetDeviceInformation、GetSystemDateAndTime、SetSystemDateAndTime(受け付けるだけ)、GetCapabilities、GetServices、GetServiceCapabilities、GetScopes、GetHostname、GetNetworkInterfaces、GetNetworkProtocols、GetNetworkDefaultGateway、GetDNS、GetNTP、GetDiscoveryMode、GetUsers、GetRelayOutputs、GetWsdlUrl、GetEndpointReference |
| Media(Profile S) | GetProfiles、GetProfile、GetStreamUri、GetSnapshotUri、GetVideoSources、GetVideoSourceConfiguration(s)、GetVideoEncoderConfiguration(s)、GetCompatibleVideoEncoderConfigurations、GetVideoEncoderConfigurationOptions、SetVideoEncoderConfiguration(sub のみ)、SetSynchronizationPoint、音声・メタデータ系は空で返す |
| Media2 | GetProfiles、GetStreamUri、GetSnapshotUri、GetVideoEncoderConfigurations(main は `H265`)、GetVideoEncoderConfigurationOptions、GetVideoSourceConfigurations、SetVideoEncoderConfiguration、SetSynchronizationPoint、GetServiceCapabilities |

- 認証は WS-Security UsernameToken(PasswordDigest と PasswordText)。ONVIF の PRE_AUTH(GetSystemDateAndTime、GetCapabilities、
  GetServices、GetServiceCapabilities、GetHostname、GetWsdlUrl、GetEndpointReference)は認証なしで応答する。
  誤った認証には SOAP 1.2 Fault `env:Sender` / `ter:NotAuthorized`(HTTP 400)、未実装の操作には `env:Receiver` / `ter:ActionNotSupported`。
- Media2 の `GetProfiles` は仕様どおり、`Type`(`All`、`VideoEncoder` など)を指定したときだけ Configurations を返す。
- Media(ver10)の Encoding には H265 がないため、H.265 の `Profile_Main` は VideoEncoderConfiguration を持たない(GetStreamUri は使える)。
- `SetVideoEncoderConfiguration` で変えられるのはビットレート上限、GovLength、フレームレート、Quality。変えると FFmpeg を再起動する
  (配信が数秒途切れる)。解像度とコーデックは固定で、変えようとすると `ter:InvalidArgVal` / `ter:ConfigModify`。設定はメモリ上だけで、コンテナを再起動すると戻る。

## 映像

- main: H.265(libx265)。VBR(CRF + `vbv-maxrate` で上限)、`keyint = min-keyint = fps × キーフレーム間隔`、`scenecut=0`、
  closed GOP、`repeat-headers=1`(途中から受信しても VPS/SPS/PPS が届く)、`-tune zerolatency`(B フレームなし)。
- sub: H.264(libx264)、低解像度、GOP は main と同じ。
- 時刻の焼き込み: フレームを作った時点の UTC(ミリ秒)、カメラ名、フレーム番号。1 フレームの壁時計(`RTCTIME`)から日付〜ミリ秒を
  まとめて出すので、桁ごとにずれない。遅延の計測は「受信側の時刻 − 焼き込まれた時刻」で行える(エンコードの遅延を含む)。
- 動画ファイル: `-re -stream_loop -1` で実時間ループ再生。fps フィルターで CFR にそろえてから `setpts=N` で振り直すので、
  ループの継ぎ目でも出力の pts は単調増加し、途切れない(結合テストで確認)。解像度が違う動画は main の解像度に縮小して黒帯で埋める。

## 設定

環境変数か YAML(`EMULATOR_CONFIG`。例は [config.example.yaml](config.example.yaml))。優先順位は
「既定値 < YAML の defaults < YAML の cameras[i] < 環境変数(全体)< 環境変数(カメラ別 `EMULATOR_CAM{n}_*`)」。
空文字の環境変数は無視する。

| 環境変数 | 既定 | 内容 |
| --- | --- | --- |
| `EMULATOR_CAMERAS` | 2 | カメラ台数 |
| `EMULATOR_ONVIF_PORT` | 8080 | 1 台目の ONVIF ポート(n 台目は +n-1) |
| `EMULATOR_RTSP_PORT` | 8554 | RTSP ポート |
| `EMULATOR_DISCOVERY` | true | WS-Discovery を有効にする |
| `EMULATOR_ADVERTISE_HOST` | (自動) | XAddrs と URI に入れるホスト。未指定なら、SOAP はリクエストの Host ヘッダー、Discovery は応答先への経路の IP |
| `EMULATOR_RTSP_AUTH` | false | true で RTSP の読み出しにカメラの認証情報(Basic / Digest)が必要 |
| `EMULATOR_STREAMS` | true | false で MediaMTX と FFmpeg を起動しない(SOAP だけ) |
| `EMULATOR_AUTH_MAX_SKEW_S` | 0 | UsernameToken の Created の許容ずれ(秒)。0 で検査しない |
| `EMULATOR_LOG_LEVEL` | INFO | |
| `EMULATOR_USERNAME` / `EMULATOR_PASSWORD` | admin / admin | 認証情報 |
| `EMULATOR_SOURCE` | testsrc | `testsrc`、`testsrc2`、`smptebars`、`smptehdbars`、`rgbtestsrc`、`mandelbrot`、`life`、`file:/media/xxx.mp4` |
| `EMULATOR_WIDTH` / `EMULATOR_HEIGHT` | 1280 / 720 | main の解像度 |
| `EMULATOR_FPS` | 15 | フレームレート(main と sub) |
| `EMULATOR_BITRATE_KBPS` | 1024 | main の VBR 上限 |
| `EMULATOR_KEYFRAME_INTERVAL_S` | 1 | キーフレーム間隔(秒)。60 を割り切れる値にする |
| `EMULATOR_QUALITY` | 60 | ONVIF の Quality(0〜100、CRF 40〜20 に変換) |
| `EMULATOR_SUB_WIDTH` / `EMULATOR_SUB_HEIGHT` / `EMULATOR_SUB_BITRATE_KBPS` | 640 / 360 / 384 | sub の設定 |
| `EMULATOR_PRESET` | ultrafast | x264 / x265 の preset |
| `EMULATOR_NAME`、`EMULATOR_MANUFACTURER`、`EMULATOR_MODEL`、`EMULATOR_FIRMWARE`、`EMULATOR_LOCATION` | | 機器情報(名前はカメラ別のみ。既定 `cam-001` …) |

カメラ別は `EMULATOR_CAM2_SOURCE=file:/media/traffic_bangkok.mp4` のように `EMULATOR_CAM{n}_` を付ける(`_ONVIF_PORT` も可)。

## サンプル動画(物体検出の検証用)

```sh
# コンテナの ffmpeg で取得・変換して emulator/media/ に置く(mp4 は gitignore 済み)
docker compose -f emulator/compose.yaml build
docker run --rm -v ./emulator/media:/media cloud-camera/onvif-emulator python /app/scripts/fetch_sample_media.py --out /media
# 2 台目を動画ループにして起動
EMULATOR_CAM2_SOURCE=file:/media/traffic_bangkok.mp4 docker compose -f emulator/compose.yaml up
```

原本(WebM)を Wikimedia Commons から取り、1280×720 の H.264 mp4(音声なし)に変換する。

| ファイル | 内容 | 長さ | ライセンス | 作者 / 出典 |
| --- | --- | --- | --- | --- |
| `crosswalk_toronto.mp4` | トロント Yonge & Dundas のスクランブル交差点(歩行者多数、車) | 57 秒 | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | Raysonho @ Open Grid Scheduler / Grid Engine — [File:DiagonalCrosswalkYongeDundas.webm](https://commons.wikimedia.org/wiki/File:DiagonalCrosswalkYongeDundas.webm) |
| `traffic_bangkok.mp4` | バンコクの道路(車、トラック、バン、歩行者) | 24 秒 | パブリックドメイン(米国連邦政府職員の職務著作物) | U.S. Army National Guard video by Staff Sgt. James Bunn(DVIDS 1009429)— [File:Bangkok Traffic During Enduring Partners 26 (1009429).webm](https://commons.wikimedia.org/wiki/File:Bangkok_Traffic_During_Enduring_Partners_26_(1009429).webm) |

どちらも商用利用・改変・再配布が可能でクレジット表記も不要(ただし表記を推奨)。映っている人物の肖像や店舗の商標には別の権利が
ありうるので、検証以外(公開デモの素材など)に使うときは注意する。

## テスト

```sh
# 1. Docker で結合テスト(実際の WS-Discovery マルチキャスト、onvif-zeep、ffprobe を使う)
docker compose -f emulator/compose.yaml up -d --build
docker compose -f emulator/compose.yaml --profile test run --rm --build tests

# 動画ループの継ぎ目も確かめる(2 台目を動画にして 55 秒読む)
EMULATOR_CAM2_SOURCE=file:/media/traffic_bangkok.mp4 docker compose -f emulator/compose.yaml up -d
EMULATOR_CAM2_SOURCE=file:/media/traffic_bangkok.mp4 EMULATOR_LOOP_CAMERA=2 \
  docker compose -f emulator/compose.yaml --profile test run --rm -e EMULATOR_LOOP_SECONDS=55 tests

# 2. ホストだけで(映像なし。テストのプロセス内で SOAP と Discovery を起動する)
cd emulator && mise exec -- uv run pytest
```

| テスト | 内容 |
| --- | --- |
| `test_onvif.py` | Discovery(全台、EPR がカメラごとに別)→ 認証 → GetDeviceInformation → Media2 で H.265 プロファイル → GetStreamUri の一連の流れ、誤パスワード・誤ユーザーの拒否(Digest / Text)、Fault の形式、PRE_AUTH、Device / Media1 の各操作、strict な zeep での Media2 スキーマ検証 |
| `test_media.py` | ffprobe で main が hevc・指定解像度、sub が h264・低解像度、キーフレーム間隔が設定どおり、pts が単調増加、スナップショット、Media2 の SetVideoEncoderConfiguration で GOP が 2 秒に変わること、動画ループの連続性、全プロセスの健全性 |
| `test_units.py` | 設定の優先順位、UsernameToken の検証、FFmpeg / MediaMTX の設定生成 |

`tests/wsdl/` は ONVIF 公式(github.com/onvif/specs 26.06)の Media2 WSDL と onvif.xsd に、W3C / OASIS の補助スキーマを加えて
外部参照をローカルに書き換えたもの([scripts/vendor_wsdl.sh](scripts/vendor_wsdl.sh) で再生成できる)。onvif-zeep 同梱の WSDL には
Media2 がないため、Media2 を使うクライアント(エッジなど)はこの WSDL を `ONVIFService(xaddr, user, pass, ".../ver20/media/wsdl/media.wsdl",
binding_name="{http://www.onvif.org/ver20/media/wsdl}Media2Binding")` のように使える(`tests/onvif_helpers.py` の `media2()` を参照)。

## Docker for Mac と WS-Discovery

- **同じ Docker ネットワークのコンテナからは届く**(結合テストの `tests` サービスがマルチキャストの Probe で全台を見つけることを確認済み)。
  リポジトリ直下の compose で `include` すれば、エッジのコンテナも同じネットワークに入る。
- **Mac のホスト(や LAN)からのマルチキャストは届かない**。Docker Desktop はコンテナを Linux VM の中のブリッジに置くので、
  マルチキャストは VM の外に出入りしない。`network_mode: host` も Mac では LAN に直結しない。
- ホストから試すときは `127.0.0.1:3702` へユニキャストで Probe を送れば応答する(UDP のポート転送経由)。ただし XAddrs は
  コンテナの IP(172.x)になるので、`EMULATOR_ADVERTISE_HOST=127.0.0.1` を付けて起動する(SOAP の URI はリクエストの Host を使うので不要)。
- Raspberry Pi など Linux で LAN の実機クライアント(ONVIF Device Manager など)から探すときは、`network_mode: host` で起動する
  (ポート公開は不要。ONVIF は 8080〜、RTSP は 8554 をそのまま使う)。

## Raspberry Pi で動かすとき

ソフトウェアエンコード(libx265)は重い。Raspberry Pi 4 での性能は未計測。1 台・1280×720・15fps・`ultrafast` から試し、
`/healthz` の `speed` が 1.0x を下回る(実時間に追いつかない)ときは台数・解像度・fps を下げる。Mac(Apple Silicon)では
1280×720・15fps × 2 台で CPU 0.6 コア程度。

## 既知の制約

- 1 台ごとに ONVIF のポートを分けている(onvif-zeep などはデバイスサービスのパスを `/onvif/device_service` 固定で組み立てるため)。
- Events、PTZ、Imaging、音声、マルチキャスト配信、HTTPS、HTTP Digest 認証は未実装。
- 時刻はコンテナ(ホスト)の時計そのまま。SetSystemDateAndTime は受け付けるが何もしない。
- UsernameToken の Nonce の再利用(リプレイ)は検査しない。
- スナップショットは認証なし。
- 設定変更(SetVideoEncoderConfiguration)は保存しない。変更時は数秒(FFmpeg の終了待ち)配信が止まる。
- RTSP の UDP 転送は Docker のポート転送越しだと届かないことがある。ホストから再生するときは TCP(`-rtsp_transport tcp`)を使う。

## ファイル

| パス | 内容 |
| --- | --- |
| `src/onvif_emulator/config.py` | 設定(YAML + 環境変数) |
| `src/onvif_emulator/soap.py` | SOAP エンベロープ、Fault、UsernameToken の検証 |
| `src/onvif_emulator/services.py` | Device / Media / Media2 の操作(手書きの XML テンプレート) |
| `src/onvif_emulator/discovery.py` | WS-Discovery |
| `src/onvif_emulator/streams.py` | MediaMTX の設定、FFmpeg のコマンド |
| `src/onvif_emulator/supervisor.py` | 子プロセスの監視と再起動 |
| `src/onvif_emulator/app.py` | 全体の起動・停止、HTTP サーバー |
| `scripts/fetch_sample_media.py` | サンプル動画の取得 |
| `scripts/vendor_wsdl.sh` | テスト用 WSDL の取り込み |
