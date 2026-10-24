# エッジゲートウェイ

ONVIF カメラの H.265(設定で H.264 も)を**再エンコードせずに** Kinesis Video Streams(KVS)へ送るコンテナ。
Raspberry Pi 4(linux/arm64)向けで、Mac(Apple Silicon の Docker)でもそのまま動く。linux/amd64 もビルドできる。

```
カメラ ──RTSP(H.265)──▶ rtspsrc ! rtph265depay ! h265parse ! video/x-h265,stream-format=hvc1 ! h265timestamper ! kvssink ──▶ KVS
       (H.264 のとき)       rtph264depay ! h264parse ! video/x-h264,stream-format=avc ! h264timestamper
   ▲                                                                                                         (IoT 認証情報プロバイダー)
   └─ONVIF(WS-Discovery、Media2)── Python エージェント ──MQTT5──▶ AWS IoT Core(シャドウ、status、discovery、cmd)
```

## 構成

| パス | 中身 |
| --- | --- |
| `Dockerfile` | マルチステージ。producer-sdk-cpp **v3.6.0**(producer-c v1.6.2)の kvssink をビルドし、必要な GStreamer プラグインの .so だけを実行イメージに入れる(Debian trixie、約 260 MB) |
| `agent/cc_edge/agent.py` | 本体。シャドウ(またはローカル設定)の差分適用、reported、status(30 秒)、cmd、discovery |
| `agent/cc_edge/iot.py` | AWS IoT Device SDK v2(MQTT5)。LWT、再接続ごとの再購読 |
| `agent/cc_edge/onvif.py` | 標準ライブラリだけの ONVIF クライアント。WS-Discovery、WS-Security Digest、Media2 で H.265 を自動選択(`prefer_codec` で H.264 優先にも)、GetStreamUri |
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
| `CC_PREFER_CODEC` | `H265` | ONVIF でプロファイルを自動選択するときに優先するコーデック(`H265` / `H264`)。カメラごとにシャドウの `prefer_codec` で上書きできる。優先のコーデックがなければもう一方を使う([docs/codec.md](../docs/codec.md)) |
| `CC_RTSP_PROTOCOLS` / `CC_RTSP_LATENCY_MS` | `tcp` / `200` | rtspsrc |
| `CC_KVS_BUFFER_S` / `CC_KVS_STORAGE_SIZE_MB` | `660` / `160` | 回線断の間に溜める上限(秒 / MB、1 カメラあたり)。どちらかに達したら古い方から捨てる。下の「回線断とバッファ」 |
| `CC_KVS_CONNECTION_STALENESS_S` / `CC_KVS_REPLAY_S` | `30` / `0`(= `CC_KVS_BUFFER_S`) | ACK が途絶えて接続を張り直すまでの秒数 / 張り直したときに巻き戻して送り直す最大の秒数 |
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

実 AWS での確認(2026-10-24、`TMP_PROJECT=cc-tmp-ptz edge/scripts/tmp-aws.sh up` の Thing、エッジはローカル実行、カメラは契約どおりのスタブ):
API(`POST .../ptz`、202 まで約 50 ms)→ IoT Core → エッジ → ONVIF → シャドウ → `GET .../ptz` で新しい位置が見えるまで中央値約 250 ms。
映像への反映はこれに KVS の LIVE HLS の遅れ(数秒)が加わる。

## 回線断とバッファ

回線(KVS・IoT Core への通信)が切れている間も、エッジはパイプラインを止めずに映像を **kvssink(Producer SDK)のメモリ** に溜め、
復旧後に**元のプロデューサー時刻のまま古い順に**送り直す。ディスクには溜めない(エッジは中継に徹する)。
既定で約 10 分(660 秒)の断を吸収し、それより長い断は古い方から欠落する。

### kvssink の設定と理由

ソース(producer-sdk-cpp v3.6.0、producer-c v1.6.2、PIC v1.3.1)と遮断試験で確かめた意味。

| プロパティ | 値(環境変数) | 意味と理由 |
| --- | --- | --- |
| `buffer-duration` | 660 秒(`CC_KVS_BUFFER_S`) | バッファ(コンテンツビュー)に置ける映像の長さ。これを超えると古い方から捨てる(`DROP_UNTIL_FRAGMENT_START`: 断片の頭まで)。永続化の ACK が来た断片はすぐ消えるので、平常時は 1〜2 秒ぶんしか持たない。10 分ちょうどにすると、復旧から送り始めるまでの数秒〜十数秒(PutMedia の張り直し待ち)で遮断直後のぶんが押し出されるので、1 分の余裕を足した |
| `storage-size` | 160 MB(`CC_KVS_STORAGE_SIZE_MB`) | コンテンツストア(メモリのヒープ)の上限。1 カメラ 1 プロセスなのでカメラごと。足りなくなると古い方から捨てる(`DROP_TAIL_ITEM`)。1.5Mbps × 660 秒 ≈ 124MB に MKV の上乗せを見て、時間の上限が先に効く大きさにした。ヒープは使ったぶんだけ実メモリになる(試験で 40MB 使用時にコンテナ全体 130MB)。ビットレートが高いカメラは `ピーク bps × 660 / 8 × 1.1` 以上にする |
| `framerate` | 30(`CC_KVS_FRAMERATE`) | バッファの枠(フレーム数)= framerate × buffer-duration。実際の fps より小さいと、時間の上限より先に枠があふれる。fps 以上の値にしておけばよい |
| `replay-duration` | = buffer-duration(`CC_KVS_REPLAY_S`、0 で同じ) | PutMedia を張り直したとき、未 ACK の映像をどこまで巻き戻して送り直すか。回線断の間 SDK は張り直すたびに届かない接続へ書いたぶんを「送った」扱いにして送信位置を進めることがあり、巻き戻しが短いと(既定 40 秒、試験では 60 秒)その前が送り直されずに欠ける(10 分遮断で遮断直後の 54 秒が欠けた)。永続化済みはバッファから消えているので、長くしても重複は増えない |
| `connection-staleness` | 30 秒(`CC_KVS_CONNECTION_STALENESS_S`) | BUFFERING ACK がこの秒数来なければ接続を張り直す(バッファは消さない `ResetConnection`)。黙って捨てられる回線断(RST が来ない)では TCP が気づかないので、これで検出する(既定 60 秒) |
| `max-latency` | buffer-duration + 60 秒 | 未送信がこれを超えると SDK が接続を張り直す。復旧後の追いつき中に何度も張り直して再送が増えるだけなので、バッファより長くして実質使わない |
| `restart-on-error` | true | 送信エラーのとき SDK が自分で接続を張り直す(既定どおり)。false だとエラーのたびにパイプラインを止めることになりバッファが消える |
| `fragment-acks` | true | ACK で永続化を確かめ、張り直しのときに最後に受け取られた断片まで巻き戻す。status の計測値(`persisted_lag_s`)にも使う |
| `get-kvs-metrics` | true | キーフレームごとに `stream-client-metric` でバッファの計測値を出す(status の `buffer`) |

**注意(SDK がバッファを消す場合)**: PIC は DESCRIBE / GET_ENDPOINT / GET_TOKEN の呼び出しを 5 回までしか再試行せず、
尽きると continuous retry のコールバックがストリームをリセットし、**バッファを全部捨てる**(`kinesisVideoStreamResetStream`)。
回線断の間は PUT_STREAM の再試行(無制限)に留まるので、今回の試験(15 分まで)では起きなかった。
ただし、断の最中にストリーミングトークンの取り直し(最長 40 分ごと)が来て、IoT の認証情報も切れかけていると取りに行って失敗し、
kvssink が止まってバッファが消える(試験で 1 回起きた。下の結果)。認証情報の有効期間を 12 時間にすると起きにくくなる。

### エージェントの監視・再起動

パイプラインを再起動すると kvssink のメモリ上のバッファが消えるので、**カメラ側の異常だけで再起動し、KVS 側の回線断では再起動しない**。

| 事象 | 判定 | 対応 |
| --- | --- | --- |
| カメラ側(RTSP の切断、固まり) | kvssink の手前(`h26Xtimestamper` の出力)に `CC_STALL_TIMEOUT` 秒フレームが来ない、rtspsrc などの ERROR、EOS | 子プロセスを終了してバックオフ付きで再起動(従来どおり) |
| KVS への送信エラー(回線断) | kvssink の `stream-error` シグナル、PutMedia の失敗 | 数えるだけ(status の `kvs_errors`)。kvssink が自分で張り直してバッファから送り直す。フレームは手前を流れ続けるので停止判定にもかからない |
| kvssink の ERROR(再試行しても直らないエラー: 認証、ストリームなし等) | バスの ERROR(source が kvssink) | 再起動(`side: "kvs"` としてログに出す) |
| MQTT(IoT Core)の切断 | | パイプラインとは別プロセスなので配信は止まらない。再接続(最大 30 秒間隔)したら購読し直し、シャドウを取り直して reported と status を送り直す。シャドウが変わっていなければパイプラインには触らない |

バッファがあふれたときは、捨てたフレーム数(未送信のまま捨てたもの)と、上限を超えて失った秒数(`lost_s`。最後に永続化された時刻からバッファの最古までの差)を WARNING で出す。
kvssink 自身も捨てたフレームごとに `droppedFrame` の WARN を出す(15fps なら 1 秒に 15 行)。

### 遮断試験

```sh
TMP_PROJECT=cc-tmp-edgebuf mise exec -- edge/scripts/tmp-aws.sh up
mise exec -- edge/scripts/outage-test.sh up        # エミュレーター 1 台 + エッジ(実 KVS、シャドウ)。ホストのポートは使わない
mise exec -- edge/scripts/outage-test.sh run 10    # 10 分遮断 → 復旧 → 送り終わるまで待つ → ListFragments を集計
mise exec -- edge/scripts/outage-test.sh run 5,2,5 # 5 分遮断 → 2 分開通 → 5 分遮断
mise exec -- edge/scripts/outage-test.sh down
TMP_PROJECT=cc-tmp-edgebuf mise exec -- edge/scripts/tmp-aws.sh down
```

- 遮断は `scripts/outage/netctl.sh`: iptables だけ入った小さなコンテナをエッジと同じネットワーク名前空間に入れ、
  同じ Docker ネットワーク(エミュレーター)とループバック以外の送受信を DROP する(RST を返さない、黙って捨てる回線断)
- 集計は `scripts/outage/report.py`: ListFragments を PRODUCER_TIMESTAMP と SERVER_TIMESTAMP の両方で引いて断片番号で合わせ、
  欠け(プロデューサー時刻で 0.5 秒以上の隙間)、遅れ(サーバー時刻 − プロデューサー時刻)、順序、重複を出す。結果は `edge/.out/outage/`

#### 結果(2026-10-24、ap-northeast-1、Mac の Docker、エミュレーター 1 台 1280x720 15fps H.265 キーフレーム 1 秒 約 0.7Mbps、既定の設定)

断片 = 1 秒(キーフレームごと)。「欠け」はプロデューサー時刻で並べたときの隙間。「送り終わり」は復旧から遮断中ぶんの最後の断片が永続化されるまで。
「最大の遅れ」はサーバー時刻 − プロデューサー時刻の最大(クラウドが断片を待つ必要がある長さ)。

| 遮断 | 遮断中の断片 | 欠け | 送り終わり(復旧から) | 遮断開始から送り終わりまで | 最大の遅れ | 順序の逆転 | 重複 | 再起動 | 未送信の最大 / メモリ使用 | 捨てたフレーム |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 分 | 60 / 60 本 | なし | 23 秒 | 83 秒 | 72 秒 | 0 | 0 | 0 | 71 秒 / 6MB | 0 |
| 5 分 | 299 / 300 本 | 1 秒(遮断 +98 秒の 1 本、エラー ACK) | 74 秒 | 374 秒 | 314 秒 | 0 | 0 | 0 | 267 秒 / 26MB | 0 |
| 10 分 | 599 / 600 本 | なし | 133 秒 | 733 秒 | 613 秒 | 0 | 0 | 0 | 561 秒 / 51MB | 0 |
| 15 分(上限超え) | 633 / 900 本 | 先頭 266 秒(上限超えで捨てた。`lost_s` = 266.4 と一致)+ 1 秒(遮断 +280 秒、エラー ACK) | 152 秒 | 1052 秒 | 659 秒 | 0 | 0 | 0 | 660 秒 / 56MB | 3255 |
| 5 分 → 2 分開通 → 5 分 | 720 / 720 本 | 1 秒(2 回目の遮断 +95 秒、エラー ACK) | 76 秒 | 796 秒 | 316 秒 | 0 | 0 | 0 | 301 秒 / 26MB | 0 |

- 全試験で、パイプラインの再起動なし(status の `restarts` 0、コンテナの RestartCount 0)、`streaming` は true のまま。
  MQTT は遮断の約 60 秒後に PINGRESP のタイムアウトで切断を検知し、復旧から 40 秒以内に再接続して reported(`online`、`last_seen`)と status が戻った。
  シャドウを取り直してもパイプラインには触らない(差分なし)
- **順序**: 届いた順(サーバー時刻)に並べてもプロデューサー時刻は単調増加(逆転 0)。送り直しは古い順。**重複**: 0
- **送り直しの速さ**: 約 5.5 断片/秒(1 秒の断片で実時間の約 5.5 倍、0.7Mbps のとき約 3.8Mbps)。帯域ではなく断片の数で頭打ちになっているように見える(推測)。
  新しい映像も同時に出るので、溜まった N 秒は約 N/4.5 秒で追いつく(10 分 → 133 秒)。回線が細い(ビットレート × 5 倍より遅い)ときはもっと遅くなる。キーフレーム間隔 2 秒なら約 2 倍速くなる見込み(推測)
- **最大の遅れ**は「断の長さ(上限 660 秒)+ 数秒〜十数秒」。古い断片から送るので、復旧後に最初に届く断片がいちばん遅れる。
  クラウドの `MAX_LATE_SECONDS`(区間の終わりから 720 秒)に対し、10 分の断で 613 秒、上限を超える断でも 659 秒で、間に合う。
  上限を伸ばすとき(`CC_KVS_BUFFER_S`)は `MAX_LATE_SECONDS` も合わせる
- **1 本だけ欠けることがある**: 1 分を超える断で、断片 1 本(1 秒)が欠けることがあった(5 分・15 分・繰り返しで各 1 本、10 分ではなし)。
  KVS がエラー ACK(`result=4006` = MKV が不正)を返して受け付けなかったもの。遮断中に PutMedia が送信途中で切れた断片を送り直したときに起きると見られる(SDK の挙動。設定では避けられなかった)。
  ログに `KVS がエラー ACK を返した` と出て、status の `error_acks` が増える。欠けは 1 秒単位で、その前後の映像の時刻は正しい(焼き込み時刻で確認)
- **CPU とセッション数**: 遮断中もエッジのコンテナの CPU は 1〜11%(Mac)、PutMedia の張り直しは 20 秒に 1 回程度(15 分で 91 回)。張り直しが暴走したり CPU が張り付いたりはしなかった
- **replay-duration を短くしたとき**(60 秒、修正前): 10 分の断で遮断直後の 54 秒が欠けた(未送信の秒数がバッファ全体より 60 秒短いまま進み、巻き戻しが届かなかった)。バッファと同じ長さにしてからは欠けない
- **認証情報の取り直しと重なったとき**(修正前の 1 回目の 5 分 → 2 分 → 5 分): 起動から約 49 分後、遮断中に kvssink が IoT の認証情報を取り直そうとして失敗し
  (`getStreamingTokenResultEvent ... 0x5200002a` → `0x16000001`)、kvssink が ERROR を出してパイプラインが止まり、**バッファが消えた**(337 秒の欠け、復旧までに 6 回の再起動)。
  PIC はストリーミングトークンを最長 40 分ごとに取り直し、IoT の認証情報(ロールエイリアスの `credential_duration`、既定 3600 秒)が切れかけていると IoT のエンドポイントに取りに行く。
  回線断と重なると止まるので、**`credential_duration` は最大の 43200 秒(12 時間)にする**のを勧める(ロールの `max_session_duration` も合わせて上げる。`tmp-aws.sh` は `TMP_CREDENTIAL_S`)
- **時刻**: 回線断の間もフレームは kvssink の手前を流れ続け、プロデューサー時刻は「最初のフレームを受け取った壁時計 + PTS の差分」で付く(h265timestamper は DTS を付けるだけで PTS は変えない)。
  焼き込み時刻と比べると、遅れて届いた断片の時刻も正しかった。ただし 44 分・3 回の遮断のあと、プロデューサー時刻が焼き込み時刻より約 1.3 秒先に進んでいた
  (起動時 +1.9 秒 → +3.3 秒。エラー ACK のあった遮断のたびに約 0.9 秒ずつ。原因は未特定で、rtspsrc のジッターバッファの時刻か SDK の再接続まわりの可能性)。1 分区間の切り出しには影響しない大きさ

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

- kvssink は H.265 の caps(`video/x-h265`)を見て codec-id `V_MPEGH/ISO/HEVC`、content-type `video/h265` を自動で設定する。
  H.264(`video/x-h264`)なら codec-id は既定の `V_MPEG4/ISO/AVC`、content-type `video/h264`(producer-sdk-cpp v3.6.0 の
  `gstkvssink.cpp` の `init_track_data`。H.264 は `stream-format=avc` の `codec_data`(avcC)がコーデックプライベートデータになる)
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
