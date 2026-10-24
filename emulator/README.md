# ONVIF カメラエミュレーター

1 つのコンテナで複数台の ONVIF カメラ(Profile S + Media2 + 疑似 PTZ)を模擬する。エッジゲートウェイの開発と、
クラウド側(KVS、録画、物体検出)の検証に使う。arm64(Raspberry Pi、Apple Silicon)と amd64 で動く。

```
┌──────────────────── コンテナ ────────────────────┐
│ python -m onvif_emulator(asyncio、1 プロセス)    │
│  ├ ONVIF SOAP(aiohttp)  :8080 cam1, :8081 cam2 … │
│  ├ WS-Discovery           udp/3702(239.255.255.250)│
│  └ プロセス監視(再起動、ウォッチドッグ)         │
│      ├ MediaMTX           :8554 RTSP(udp 8000/8001)│
│      └ FFmpeg × 台数  testsrc / 動画ループ         │
│          ├ 疑似 PTZ: 高解像度の元映像 → crop(zmq)│
│          ├ main  H.265 libx265 → /cam{n}/main      │
│          │       (main_codec=H264 で libx264)      │
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
| PTZ | `…/onvif/ptz_service`(同上) |
| RTSP | `rtsp://<host>:8554/cam{n}/main`(既定 H.265。`EMULATOR_MAIN_CODEC` で H.264)、`rtsp://<host>:8554/cam{n}/sub`(H.264) |
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
| Media2 | GetProfiles、GetStreamUri、GetSnapshotUri、GetVideoEncoderConfigurations(main は既定 `H265`、`main_codec` が H264 なら `H264`)、GetVideoEncoderConfigurationOptions、GetVideoSourceConfigurations、SetVideoEncoderConfiguration、SetSynchronizationPoint、GetServiceCapabilities |
| PTZ(ver20) | GetServiceCapabilities、GetNodes、GetNode、GetConfigurations、GetConfiguration、GetCompatibleConfigurations、GetConfigurationOptions、GetStatus、ContinuousMove、RelativeMove、AbsoluteMove、Stop、GetPresets、SetPreset、RemovePreset、GotoPreset、GotoHomePosition、SetHomePosition |

- 認証は WS-Security UsernameToken(PasswordDigest と PasswordText)。ONVIF の PRE_AUTH(GetSystemDateAndTime、GetCapabilities、
  GetServices、GetServiceCapabilities、GetHostname、GetWsdlUrl、GetEndpointReference)は認証なしで応答する。
  誤った認証には SOAP 1.2 Fault `env:Sender` / `ter:NotAuthorized`(HTTP 400)、未実装の操作には `env:Receiver` / `ter:ActionNotSupported`。
- Media2 の `GetProfiles` は仕様どおり、`Type`(`All`、`VideoEncoder` など)を指定したときだけ Configurations を返す。
- Media(ver10)の Encoding には H265 がないため、H.265 の `Profile_Main` は VideoEncoderConfiguration を持たない(GetStreamUri は使える)。main を H.264 にしたカメラは main も持つ。
- PTZ の操作も同じ UsernameToken の認証が必要(PRE_AUTH なし)。Media の各プロファイルと、Media2 の `GetProfiles`(`Type` が `All` か `PTZ`)に
  同じ PTZConfiguration(`PTZConfig_1`)が付く。
- `SetVideoEncoderConfiguration` で変えられるのはビットレート上限、GovLength、フレームレート、Quality。変えると FFmpeg を再起動する
  (配信が数秒途切れる)。解像度とコーデックは固定で、変えようとすると `ter:InvalidArgVal` / `ter:ConfigModify`。設定はメモリ上だけで、コンテナを再起動すると戻る。

## 映像

- main: H.265(libx265)。VBR(CRF + `vbv-maxrate` で上限)、`keyint = min-keyint = fps × キーフレーム間隔`、`scenecut=0`、
  closed GOP、`repeat-headers=1`(途中から受信しても VPS/SPS/PPS が届く)、`-tune zerolatency`(B フレームなし)。
  カメラごとに `main_codec: H264`(`EMULATOR_MAIN_CODEC` / `EMULATOR_CAM{n}_MAIN_CODEC`)で H.264(libx264、同じ方式)にできる。
  Quality → CRF は H.265 が 40〜20、H.264 はそこから `H264_CRF_OFFSET` を引く(同じ Quality で同程度の画質。[docs/codec.md](../docs/codec.md))。
  H.264 で H.265 と同じ画質を保つにはビットレート上限(`EMULATOR_BITRATE_KBPS`)も上げる(目安は docs/codec.md の比)
- sub: H.264(libx264)、低解像度、GOP は main と同じ。
- 時刻の焼き込み: フレームを作った時点の UTC(ミリ秒)、カメラ名、フレーム番号。1 フレームの壁時計(`RTCTIME`)から日付〜ミリ秒を
  まとめて出すので、桁ごとにずれない。遅延の計測は「受信側の時刻 − 焼き込まれた時刻」で行える(エンコードの遅延を含む)。
- 動画ファイル: `-re -stream_loop -1` で実時間ループ再生。fps フィルターで CFR にそろえてから `setpts=N` で振り直すので、
  ループの継ぎ目でも出力の pts は単調増加し、途切れない(結合テストで確認)。解像度が違う動画は main の解像度に縮小して黒帯で埋める。

## 疑似 PTZ

光学 PTZ の代わりに、元映像から切り出す範囲(窓)を動かす(デジタル PTZ)。座標は ONVIF の Generic 空間
(docs/contracts.md の「PTZ」節)。

```
元映像(main × 2、例 2560×1440)→ zmq → crop(窓)→ scale(main の解像度)→ 時刻・名前・PTZ の焼き込み → main / sub / スナップショット
                                    ↑
              PTZ の状態(Python): 操作のたびと、動いている間はフレームごとに送る
```

| 値 | 範囲 | 映像 |
| --- | --- | --- |
| pan | -1〜1 | 窓の水平位置(-1 左端、1 右端) |
| tilt | -1〜1 | 窓の垂直位置(-1 下端、1 上端) |
| zoom | 0〜1 | 倍率 = 1 + zoom × (最大倍率 − 1)。0 が全体(このときは pan / tilt を動かしても見た目は変わらない) |

- 動き: 速度上限付きで等速に動く(瞬間移動しない)。ContinuousMove の速度 -1〜1、Absolute / Relative / GotoPreset / GotoHomePosition の
  Speed 0〜1 は最大速度に対する割合。GetStatus の MoveStatus は `MOVING` / `IDLE`
- ContinuousMove: Timeout(0.1〜600 秒に丸める。省略時 10 秒)で自動停止。省略した軸は変えない。端に着いたら止まる
- 範囲外の位置・速度・差分は Fault にせず丸める(RelativeMove の pan / tilt は ±2 まで、zoom は ±1)
- プリセット: 最大 32 個。トークンは `"1"`、`"2"` …(空いている最小の番号)。名前の重複は `ter:PresetExist`。
  ホーム位置の既定は (0, 0, 0)、SetHomePosition で今の位置にする
- 状態(位置、プリセット、ホーム)はカメラごとにメモリ上だけ。コンテナを再起動すると戻る。FFmpeg が再起動しても
  (SetVideoEncoderConfiguration など)今の窓から始まる
- main / sub / スナップショットは同じ窓。焼き込みの 4 行目に `PTZ pan … tilt … zoom … (倍率)` が出る(動いている間は `MOVING`)。
  時刻とカメラ名は切り出しのあとに描くので、常に左上に見える
- `/healthz` の `cameras.*.ptz` に位置、倍率、FFmpeg に送ったコマンド数とエラー数が出る

### onvif-zeep での操作例

```python
from datetime import timedelta
from onvif import ONVIFCamera

cam = ONVIFCamera("127.0.0.1", 8080, "admin", "admin")
ptz = cam.create_ptz_service()  # GetCapabilities の PTZ の XAddr を使う
p = "Profile_Main"              # Profile_Sub でも同じ窓

ptz.AbsoluteMove({"ProfileToken": p, "Position": {"PanTilt": {"x": 0.5, "y": -0.2}, "Zoom": {"x": 1}}})
ptz.ContinuousMove({"ProfileToken": p, "Velocity": {"PanTilt": {"x": -1, "y": 0}, "Zoom": {"x": 0}},
                    "Timeout": timedelta(seconds=2)})
ptz.Stop({"ProfileToken": p})
st = ptz.GetStatus({"ProfileToken": p})
print(st.Position.PanTilt.x, st.Position.PanTilt.y, st.Position.Zoom.x, st.MoveStatus.PanTilt)
token = ptz.SetPreset({"ProfileToken": p, "PresetName": "玄関"})
ptz.GotoPreset({"ProfileToken": p, "PresetToken": token})
ptz.GotoHomePosition({"ProfileToken": p})
```

ホスト側のポートをずらして起動しているときは、GetCapabilities の XAddr がコンテナ内のポート(8080)を指す(既存の仕様)。
`cam.xaddrs["http://www.onvif.org/ver20/ptz/wsdl"]` を書き換えるか、同じ Docker ネットワークから呼ぶ。
エッジの PTZ クライアント(MQTT なし)でも操作できる:

```sh
python3 -m cc_edge.ptz http://emulator:8080/onvif/device_service '{"op":"absolute","pan":0.5,"zoom":1}' '{"sleep":3}' '{"op":"home"}'
```

### 映像の仕組みと選んだ理由

FFmpeg の `zmq` フィルター(Debian の FFmpeg 7.1 は libzmq 付き)で、`crop` に実行中のコマンドを送る。Python 側は
pyzmq の DEALER ソケットで UNIX ドメインソケット(`{runtime_dir}/cam{n}.ptz.sock`。ホストのポートと衝突しない)に送る。

- crop の式は `w=iw/倍率`、`h=ow*高さ/幅`、`x=(in_w-out_w)*fx`、`y=(in_h-out_h)*fy`。ズームは `w` のコマンド 1 つで `h` も変わる
  (縦横比が崩れない)。`x` / `y` はフレームごとに評価されるので、倍率が変わっても位置の比率が保たれる
- 動いている間はフレーム間隔ごと、止まっていれば 1 秒ごと(FFmpeg の再起動に備えた再送。値が同じなら映像は変わらない)に送る
- crop と scale の間に `null` を挟む。FFmpeg 7.1 は crop と scale を直結すると、コマンドで crop の出力サイズを変えた直後から
  フレームが流れなくなる
- 出力の解像度・コーデック・GOP は変わらず、FFmpeg も再起動しないので配信は途切れない
- スナップショットの間引きは出力側の `-r 1` で行う。フィルターの中で `fps=1` にすると、FFmpeg 7.1 はフィルターグラフを
  いちばん出力の少ない出口に合わせて回すため、main / sub が 1 秒ごとにまとめて送り出されていた(PTZ を入れる前からの挙動。
  RTSP がバースト到着になり、映像への反映が最大 1 秒ぶれていた)

ほかの方式との比較: Python でフレームを受け取って切り出す方式(FFmpeg 2 本 + rawvideo のパイプ)は、高解像度の生フレームを
毎秒数百 MB コピーするので Raspberry Pi では重い。`sendcmd` は起動時に決めた時刻のコマンドしか送れない。`zmq` なら追加の
コピーがなく、操作から 1 フレーム以内にフィルターへ届く。

計測(Mac / Apple Silicon の Docker、2 台、1280×720・15fps、`ultrafast`):

| 条件 | CPU(コンテナ全体) |
| --- | --- |
| PTZ 無効 | 約 62% |
| PTZ 有効、元映像 1 倍(`EMULATOR_PTZ_SOURCE_SCALE=1`、ズームは拡大のみ) | 約 62% |
| PTZ 有効、元映像 2 倍(既定)、止まっている | 約 118% |
| 同上、1 台を動かし続ける | 約 171% |
| PTZ 有効、元映像 3 倍、止まっている | 約 158% |

増えるのは高解像度の元映像の生成(testsrc は RGB で作るので YUV への変換も)と縮小で、切り出しとコマンドはほぼ無視できる。
動かしている間は画面全体が動くので、エンコードの負荷も上がる。Raspberry Pi では `EMULATOR_PTZ_SOURCE_SCALE=1`
(ズームするとぼやける)から試す。

操作から映像に反映されるまで: SOAP の送信から、変化したフレームが RTSP(sub)で届くまで中央値 0.24 秒(0.21〜0.28 秒、10 回。
受信側のデコードを含む)。エッジの CLI(`python3 -m cc_edge.ptz`)では、ONVIF の命令が返ってから約 0.26 秒(プロセスの起動と
準備を含めて約 0.33 秒)。

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
| `EMULATOR_MAIN_CODEC` | H265 | main のコーデック(`H265` / `H264`)。sub は常に H.264 |
| `EMULATOR_BITRATE_KBPS` | 1024 | main の VBR 上限 |
| `EMULATOR_KEYFRAME_INTERVAL_S` | 1 | キーフレーム間隔(秒)。60 を割り切れる値にする |
| `EMULATOR_QUALITY` | 60 | ONVIF の Quality(0〜100、H.265 は CRF 40〜20 に変換。H.264 は同程度の画質になる CRF) |
| `EMULATOR_SUB_WIDTH` / `EMULATOR_SUB_HEIGHT` / `EMULATOR_SUB_BITRATE_KBPS` | 640 / 360 / 384 | sub の設定 |
| `EMULATOR_PRESET` | ultrafast | x264 / x265 の preset |
| `EMULATOR_PTZ` | true | 疑似 PTZ を有効にする(false で PTZ サービスを出さず、映像も PTZ なしの構成) |
| `EMULATOR_PTZ_MAX_ZOOM` | 4 | zoom = 1 のときの倍率(1〜16) |
| `EMULATOR_PTZ_PAN_TILT_SPEED` | 0.5 | 速度 1 のときの pan / tilt の速さ(単位 / 秒。-1→1 に 4 秒) |
| `EMULATOR_PTZ_ZOOM_SPEED` | 0.5 | 速度 1 のときの zoom の速さ(単位 / 秒。0→1 に 2 秒) |
| `EMULATOR_PTZ_SOURCE_SCALE` | 2 | テストパターンの元映像を main の何倍で作るか(1〜4)。動画ファイルは元の解像度(main の縦横比に黒帯で合わせ、main より小さければ main の大きさ) |
| `EMULATOR_NAME`、`EMULATOR_MANUFACTURER`、`EMULATOR_MODEL`、`EMULATOR_FIRMWARE`、`EMULATOR_LOCATION` | | 機器情報(名前はカメラ別のみ。既定 `cam-001` …) |

カメラ別は `EMULATOR_CAM2_SOURCE=file:/media/traffic_bangkok.mp4` のように `EMULATOR_CAM{n}_` を付ける(`_ONVIF_PORT`、`_PTZ` なども可)。
YAML のキーは小文字(`ptz`、`ptz_max_zoom` など)。

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
| `test_ptz.py` | onvif-zeep で PTZ の全操作、GetCapabilities / GetServices / Media / Media2 のプロファイル、速度上限付きの滑らかな移動、Timeout での自動停止、Stop、範囲の丸め、プリセット、ホーム、認証、公式 WSDL と strict な zeep でのスキーマ検証 |
| `test_ptz_units.py` | 動きのシミュレーション(時計を差し替え)、切り出し窓、FFmpeg のコマンド、設定、PTZ 無効のカメラ |
| `test_ptz_media.py` | PTZ で RTSP の映像が変わること(main / sub / スナップショットが同じ窓)、動かしている間も pts が単調・途切れない・キーフレーム間隔と解像度・コーデックが変わらない・FFmpeg が再起動しない、操作から映像反映までの遅延(`PTZ_SHOTS_DIR` を指定するとフレームの PNG と `ptz_latency.json` を書き出す) |

`tests/wsdl/` は ONVIF 公式(github.com/onvif/specs 26.06)の Media2 / PTZ WSDL と onvif.xsd に、W3C / OASIS の補助スキーマを加えて
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
1280×720・15fps × 2 台で CPU 0.6 コア程度(PTZ 無効)、既定の疑似 PTZ(元映像 2 倍)で 1.2 コア程度(「疑似 PTZ」の計測)。

## 既知の制約

- 1 台ごとに ONVIF のポートを分けている(onvif-zeep などはデバイスサービスのパスを `/onvif/device_service` 固定で組み立てるため)。
- Events、Imaging、音声、マルチキャスト配信、HTTPS、HTTP Digest 認証は未実装。
- PTZ は Generic 空間だけ(度などの絶対座標の空間、プリセットツアー、PTZ の SetConfiguration、Media の Add/RemovePTZConfiguration はない)。
  プリセットとホームは保存しない。焼き込みの 4 行目はテキストファイルを毎フレーム読み直すので、窓より 1 フレーム遅れることがある。
- 疑似 PTZ のズームが鮮明なのは元映像の倍率まで(既定の 2 倍なら 2 倍ズームまで。それ以上は拡大でぼやける)。
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
| `src/onvif_emulator/ptz.py` | 疑似 PTZ の状態と動きのシミュレーション(プリセット、ホーム、Timeout) |
| `src/onvif_emulator/ptz_service.py` | ONVIF PTZ サービスの操作 |
| `src/onvif_emulator/ptz_link.py` | PTZ の状態を FFmpeg(zmq → crop)と焼き込みに反映するループ |
| `src/onvif_emulator/discovery.py` | WS-Discovery |
| `src/onvif_emulator/streams.py` | MediaMTX の設定、FFmpeg のコマンド |
| `src/onvif_emulator/supervisor.py` | 子プロセスの監視と再起動 |
| `src/onvif_emulator/app.py` | 全体の起動・停止、HTTP サーバー |
| `scripts/fetch_sample_media.py` | サンプル動画の取得 |
| `scripts/vendor_wsdl.sh` | テスト用 WSDL の取り込み |
