# 映像コーデック: H.264 / H.265 の切り替えと比較測定(2026-10-24)

既定は H.265(ライブも録画も)。カメラごとに H.264 に切り替えられる(ビューワーから、配信を動かしたまま)。
同じ映像を同じ画質(VMAF)で比べると、**H.264 は H.265 の約 1.6 倍のビットレート**(エミュレーターと同じ x264 / x265 の
ultrafast。動きの多い場面 1.56〜1.71 倍、静かな場面 1.40〜1.45 倍)。費用はビットレートに比例する部分(取り込み、HLS の取り出し、
S3 の保存)が増え、1 台・月で **$25.6 → $34.2(+34%)**(1 Mbps 相当の H.265 を基準にした机上計算)。

## 結論の表

### 同じ画質でのビットレート比(H.264 / H.265)

1280×720、15 fps、キーフレーム 1 秒、B フレームなし、CRF の VBR(上限なし)、30 秒。VMAF は libvmaf 3.0(vmaf_v0.6.1)。

| 場面 | エンコーダー | VMAF 90 | VMAF 93 | VMAF 95 | BD-rate(VMAF 80〜97) | BD-rate(PSNR) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 交差点(人、車。動き多い) | ultrafast | 1.53 → 2.49 Mbps(1.63 倍) | 1.87 → 3.00(1.60 倍) | 2.24 → 3.50(1.57 倍) | 1.62 倍 | 1.57 倍 |
| 道路(車、バイク) | ultrafast | 1.02 → 1.76(1.72 倍) | 1.22 → 2.08(1.70 倍) | 1.41 → 2.36(1.67 倍) | 1.71 倍 | 1.61 倍 |
| 静か(1 コマ静止 + 時刻 + センサーノイズ) | ultrafast | 0.89 → 1.29(1.45 倍) | 1.42 → 1.99(1.40 倍) | — | 1.45 倍 | 1.32 倍 |
| テストパターン(エミュレーターの既定) | ultrafast | VMAF が 95〜98 に張り付き比較不能。CRF 28 同士で 0.51 → 0.58(1.13 倍) | | | — | 1.07 倍 |
| 交差点 | medium | 1.28 → 1.47(1.15 倍) | 1.54 → 1.75(1.14 倍) | 1.79 → 2.04(1.14 倍) | 1.15 倍 | 1.19 倍 |
| 静か | medium | 0.72 → 0.80(1.11 倍) | 1.12 → 1.28(1.14 倍) | — | 1.14 倍 | 1.32 倍 |

- 比は場面より **エンコーダーの設定** で大きく変わる。ultrafast の x265 / x264 で約 1.6 倍、medium では約 1.15 倍
  (ultrafast の x264 は CABAC なしの Constrained Baseline になり、圧縮が弱い。KVS の HLS の CODECS も `avc1.42c0..`)。
  実機のカメラのハードウェアエンコーダーはこの間のどこか。導入するカメラで同じ測定をする(`tools/codec`)
- 動きの多い場面ほど差が大きい(1.6〜1.7 倍)。静かな場面は 1.4 倍(ultrafast)
- 同じ CRF なら x264 と x265 でほぼ同じ VMAF だった(Quality 60 = CRF 28 の H.265 に揃う H.264 の CRF は 28.3〜28.9)。
  エミュレーターの Quality → CRF は両方同じ変換のまま(`H264_CRF_OFFSET = 0`)
- エンコードの CPU は x265 が x264 の約 2 倍(交差点の 30 秒: 47 秒 / 23 秒)。エミュレーターの H.265 は CPU が足りないとコマ落ちする(下の実 KVS)

### 実 KVS(cc-tmp-codec、交差点の動画を 2 台で同時に、Quality 60 = CRF 28、上限 8 Mbps)

19 分間(06:37〜06:56 UTC)。cam-001 = H.265、cam-002 = H.264。`tools/codec/kvs_compare.py`、生の値は `tools/codec/results/kvs_compare.json`。

| | H.265 | H.264 | 比 |
| --- | ---: | ---: | ---: |
| PutMedia の取り込み(CloudWatch `PutMedia.IncomingBytes`) | 2,496 kbps | 4,132 kbps | 1.66 |
| 断片の大きさ(ListFragments、録画している 1 秒あたり) | 2,583 kbps | 4,134 kbps | 1.60 |
| clip.mp4(1 分) | 17.1 MB(断片 53 本) | 31.0 MB(60 本) | 1.60(1 秒あたり) |
| sprite.jpg | 232 KB | 258 KB | |
| video_archiver の実行時間(512 MB) | 4.87 秒 | 3.17 秒 | 0.65 |
| detector の実行時間(1,024 MB、推論込み) | 9.28 秒(53 枚) | 9.71 秒(60 枚) | |
| detector の推論以外(推論 0.135 秒 × 枚数を引いた分) | 2.1 秒 | 1.6 秒 | |

- H.265 のエミュレーターは CPU 不足で約 11% の秒が欠けた(断片 1,010 / 1,140)。ビットレートは録画している秒あたりで比べた
- **H.264 のデコードは軽い**: キーフレーム 1 枚のデコード(1 スレッド、ローカル arm64)は H.264 7〜10 ms、H.265 17〜25 ms(ultrafast)。
  video_archiver(スプライトのデコード)は H.264 の方が大きいファイルでも 35% 短い。medium では差がほぼない(19 / 19 ms)
- 費用の差はほぼビットレートで決まる。Lambda の短縮(月 $0.3〜0.5)は帯域・保存の増加(月 $8〜23)よりずっと小さい

### 費用(`tools/cost` の desk、東京、1 台・月、視聴 2 時間、質問 100 件)

| 前提 | H.265 | H.264 | 差 |
| --- | ---: | ---: | ---: |
| H.265 1.0 Mbps、H.264 は 1.6 倍(ultrafast 相当) | **$25.64** | **$34.22** | +$8.58(+34%) |
| H.265 1.0 Mbps、H.264 は 1.15 倍(medium 相当) | $25.64 | $27.22 | +$1.58(+6%) |
| 実 KVS の交差点そのまま(2.58 / 4.13 Mbps) | $50.21 | $73.57 | +$23.36(+47%) |

内訳(1.0 / 1.6 Mbps): 取り込み $3.31 → $5.29、動画抽出(HLS の取り出し + Lambda)$6.16 → $8.45、加工・保存(S3)$8.70 → $13.26、
物体検出 $6.02 → $5.73。Lambda の実行時間は実 KVS の値(video_archiver 4.9 / 3.2 秒、detector の推論以外 2.1 / 1.6 秒)、スプライト 230 / 258 KB。

```sh
cd tools/cost
mise exec -- uv run python -m cost desk --bitrate-mbps 1.0 --archiver-duration-s 4.9 --detector-overhead-s 2.1 --sprite-kb 230
mise exec -- uv run python -m cost desk --bitrate-mbps 1.6 --archiver-duration-s 3.2 --detector-overhead-s 1.6 --sprite-kb 258
```

## 解像度の比較: 1080p / 720p × H.264 / H.265(30 fps、2026-10-28)

同じ画質(VMAF)にするのに、どのコーデックと解像度がいちばん少ないビットレートで済むか。**どちらの場面でも H.265 の 1080p が最少か、
最少とほぼ同じ**。720p に下げても通信量は減らない(細かい映像では逆に 1.5 倍に増え、VMAF 90 に届かない)。

1920×1080 の参照に対し、720p はエンコード前に bicubic で縮め、デコード後に bicubic で 1080p に拡大して比べる(1 台を全画面で見る想定。
`vmaf_v0.6.1` は 1080p の画面を画面の高さの 3 倍の距離から見る前提)。30 fps、キーフレーム 1 秒、B フレームなし、CRF の VBR、30 秒。
場面は Pexels の 2 本([tools/codec/media/SOURCES.md](../tools/codec/media/SOURCES.md))。人が少ない(夜間)の場面は今回測っていない。

| 場面 | 中身 |
| --- | --- |
| `crowd` | 交差点を目の高さの望遠で。人が次々と横切る(動き多い)。元が 1080p の 9.3 Mbps で、動きぶれもあり細部が少ない |
| `road_dusk` | 歩道橋から見下ろした夕方の道路。車の流れと少しの歩行者。元が 4K なので細部が多い |

### 同じ VMAF(平均)になるビットレート(Mbps)

| 場面 | プリセット | VMAF | H.265 1080p | H.265 720p | H.264 1080p | H.264 720p |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| crowd | medium | 90 | 3.15 | **3.10** | 4.48 | 3.83 |
| crowd | medium | 93 | 3.54 | **3.50** | 4.95 | 4.23 |
| crowd | medium | 95 | **3.82** | 3.83 | 5.30 | 4.60 |
| crowd | ultrafast | 90 | **3.26** | 3.44 | 6.91 | 6.08 |
| crowd | ultrafast | 93 | **3.67** | 3.94 | 7.82 | 6.85 |
| road_dusk | medium | 85 | **1.28** | 1.91 | 1.96 | 2.59 |
| road_dusk | medium | 90 | **2.14** | 届かない | 3.45 | 届かない |
| road_dusk | medium | 93 | **3.42** | 届かない | 5.63 | 届かない |
| road_dusk | ultrafast | 90 | **2.73** | 届かない | 4.34 | 7.65 |
| road_dusk | ultrafast | 93 | **4.29** | 届かない | 6.31 | 届かない |

### H.265 1080p に対するビットレートの比(BD-rate、VMAF 80〜97 の幾何平均)

| 場面 | プリセット | H.265 720p | H.264 1080p | H.264 720p |
| --- | --- | ---: | ---: | ---: |
| crowd | medium | 0.98 | 1.43 | 1.22 |
| crowd | ultrafast | 1.06 | 2.13 | 1.87 |
| road_dusk | medium | 1.51 | 1.56 | 2.04 |
| road_dusk | ultrafast | 1.56 | 1.61 | 2.14 |

- **720p は細部の多い映像で頭打ちになる**: road_dusk の 720p は CRF 18 でも VMAF 89〜91 が上限(縮めた時点で失われた細部は拡大しても
  戻らない)。届く範囲でも H.265 1080p の約 1.5 倍のビットレートが要る
- **細部の少ない映像では 720p と 1080p がほぼ同じ**: crowd は元が柔らかく、H.265 では差が ±6% 以内。H.264 だけは 720p の方が 12〜15% 少ない
  (1080p の H.264 は動きの多い大きな画面の予測が弱い)
- **H.264 は H.265 1080p の 1.4〜2.1 倍**。720p15 の測定(1.6 倍 / 1.15 倍)より、1080p30 の動きの多い場面で差が広がった
  (crowd の ultrafast で 2.1 倍。x264 の ultrafast は CABAC なしの Constrained Baseline)
- 下位 5% の VMAF で揃えても順位は同じ(比は平均で揃えたときから最大 0.15 動く。`summary.json` の `vmaf_p5`)
- VMAF 93 に必要なビットレートは H.265 1080p で 3.4〜4.3 Mbps。場面の細かさで傾きが大きく違う(road_dusk は VMAF 85 なら 1.3 Mbps)
- エンコードの CPU(30 秒、平均): H.265 1080p は medium 181〜316 秒、ultrafast 117〜166 秒。720p は 1080p の約 0.55〜0.75 倍。
  実機のカメラはハードウェアエンコーダーなので、どちらのプリセットに近いかは導入するカメラで測る
- 残っていること: 人が少ない(夜間)の場面、実機のカメラのエンコーダー、上限付き VBR / CBR での実際の転送量

## 上限付き VBR: Quality と BitrateLimit の決め方(H.265 1080p30、2026-10-29)

カメラの VBR は「画質の目標(ONVIF の Quality)+ ビットレートの上限(BitrateLimit)」で動く。x265 の CRF(= Quality)と
`vbv-maxrate`(= 上限。`vbv-bufsize` は上限の 2 秒分)で、上の 2 場面を 3 / 4 / 5 Mbps の上限でエンコードし、同じ CRF の上限なしと比べた
(`sweep --maxrate`、`capped`。生の値は `tools/codec/results/1080p30/capped.jsonl`、`capped_summary.json`)。
シャドウからの設定は desired の `encoder`([contracts.md](contracts.md))。

**目安: VMAF 93 を狙うなら、上限 4 Mbps と CRF 26 相当の Quality(エミュレーターの Quality 70)**。

| 場面 | 設定 | 平均ビットレート | VMAF(平均) | VMAF(下位 5%) |
| --- | --- | ---: | ---: | ---: |
| crowd(動き多い) | CRF 26、上限なし | 7.20 Mbps | 100.0 | 100.0 |
| crowd | CRF 26、上限 4 Mbps | 4.19 Mbps | 96.4 | 92.1 |
| road_dusk | CRF 26、上限なし | 3.48 Mbps | 93.1 | 92.1 |
| road_dusk | CRF 26、上限 4 Mbps | 3.48 Mbps(上限に届かない) | 93.1 | 92.1 |

(medium。ultrafast もほぼ同じで、crowd は VMAF 95.2 / 下位 5% 90.0、road_dusk は 4.06 Mbps で 92.7 / 91.3)

- **上限は動きの多い場面だけに効く**: crowd は上限なしだと必要以上の画質(VMAF 100)に 7.2 Mbps を使う。上限 4 Mbps で 42% 減り、
  VMAF は 96 に落ちるだけ。road_dusk は上限より下で収まるので、何も変わらない。静かな時間帯の節約(Quality で決まる)は上限で損なわれない
- **上限に当たると、画質は上限で決まる**(Quality を上げても変わらない)。crowd の VMAF(平均 / 下位 5%、medium)は上限 3 Mbps で
  88〜89 / 78〜83、4 Mbps で 95.5〜96.5 / 87〜92、5 Mbps で 99 / 96〜97
- **Quality を上げすぎると、上限に当たったときに画質が揺れる**: crowd の上限 3 Mbps では、CRF 22 の下位 5% が 77.6、CRF 30〜32 が 82.9。
  高すぎる画質目標で使い過ぎてから上限で絞られるため。Quality は「動きの多い場面の上限なしのビットレートが、上限より少し上」くらいにする
- **平均は上限を最大 6.3% 超え、1 秒単位では上限の 2〜3 倍になることがある**(上限 3 Mbps、CRF 22 の crowd で 1 秒 8.1 Mbps)。上限は
  `vbv-bufsize`(2 秒分)を含めた平均の制約で、瞬間の上限ではない。カメラのバッファの大きさは ONVIF にない(機種ごと)ので、
  回線の帯域は上限 + 余裕で見積もる。kvssink のバッファ(約 10 分)が瞬間の山を吸収する
- 残っていること: 実機のハードウェアエンコーダーでの Quality → 画質の対応と、上限への張り付き方(機種ごとに違う)。
  `tools/codec` の参照映像をカメラに映す方法はないので、実機で録った映像から VMAF 以外(ビットレートの推移、見た目)で確かめる

## 切り替えの使い方

### ビューワーから(配信を動かしたまま)

プレーヤーのツールバーの「カメラ設定」(歯車)→「映像形式」で H.265(推奨)/ H.264(互換性重視、「帯域と保存の費用が約 1.6 倍」と表示)を選ぶ。

```
ビューワー ─PUT /api/cameras/{id}/codec {"codec": "H264"}─▶ API ─▶ シャドウ desired.cameras.{id}.codec
  ─▶ エッジ: パイプラインを止め、ONVIF Media2 の SetVideoEncoderConfiguration でカメラの Encoding(と Profile)を変え、
     3 秒待って RTSP につなぎ直し、rtph26Xdepay / h26Xparse / h26Xtimestamper を組み直す
     (変えられなければその形式のプロファイルを選ぶ。それもなければもう一方のまま送り、reported.codec_error に理由)
  ─▶ reported.cameras.{id}.codec に実際の形式
```

- 配信は約 5〜6 秒途切れる(エミュレーターの実測。録画にも同じ長さの切れ目ができる)
- 以降の録画はその形式。それまでの録画は元の形式のまま。再生は混在してよい(下の「区間の途中の切り替え」)
- `desired.codec` と `reported.codec` が違う間、カメラ一覧に「形式を切替中…」、設定に「切り替え中…」を出し、カメラ一覧を 3 秒ごとに取り直す
- ライブ再生中に形式が変わったら、ビューワーはライブの HLS セッションを開き直す(KVS は切り替え前に開いたセッションで新しい形式の断片を返さない)
- H.265 の再生の案内(黄色の注意)は、H.264 と分かっているカメラでは出さない

### 設定で固定する

| どこ | 設定 | 既定 |
| --- | --- | --- |
| エミュレーター | `main_codec`(YAML)/ `EMULATOR_MAIN_CODEC` / `EMULATOR_CAM{n}_MAIN_CODEC` = `H265` / `H264`。Media2 の Encoding も変わる。実行中も Media2 の SetVideoEncoderConfiguration で変えられる(FFmpeg を再起動)。sub は常に H.264 | `H265` |
| エッジ | ONVIF の自動選択で優先する形式。全体は `CC_PREFER_CODEC`、カメラごとはシャドウの desired `prefer_codec`(カメラの設定は変えない)。desired `codec` はカメラの設定も変える | `H265` |
| Terraform | `camera_codecs = { "cam-002" = "H264" }` → KVS ストリームの media type が `video/h264`(作り直しなしで更新) | 全カメラ `video/h265` |

- kvssink は caps から codec-id(H.264 `V_MPEG4/ISO/AVC`、H.265 `V_MPEGH/ISO/HEVC`)と content-type を決める(producer-sdk-cpp v3.6.0 の
  `gstkvssink.cpp`)。実 KVS の HLS の CODECS は H.264 `avc1.42c01f` / `avc1.42c020`、H.265 `hvc1.1.6.L93.90`
- KVS ストリームの media type は参考情報。`video/h265` のストリームに H.264 を送っても取り込み・HLS は動いた。ビューワーからの切り替えでは変えない

## 区間の途中の切り替え(クラウド側)

KVS の HLS(ON_DEMAND)は、コーデックプライベートデータが変わったところで新しい `EXT-X-MAP` をプレイリストに出すが、
**変わった後ろの断片は同じセッションでは返さない**(400「The requested fragment does not have the same codec private data as the
rest of the session.」)。以前の video_archiver は区間を `FAILED` にしていた(detector は GetImages の予備の経路で動いた)。

- video_archiver: セッションの最初の `EXT-X-MAP` の部分だけを取り、残りは次の部分の最初の断片の時刻から HLS セッションを開き直す。
  部分ごとに `clip.mp4`、`clip.1.mp4`、`clip.2.mp4` …、`index.m3u8` は部分の頭で `EXT-X-DISCONTINUITY` と `EXT-X-MAP` を切り替える。
  区間の件に `files.clips`、`hls.sections`(部分ごとの clip、init_length、parts、codec、start_ms)、`codecs` を書く([contracts.md](contracts.md))
- スプライト / detector: 部分ごとにキーフレームをデコードして 1 つにまとめる(各断片の時刻は全部の parts を segments に当てはめて求める)
- API: vod.m3u8 は部分ごとに `EXT-X-DISCONTINUITY`、`EXT-X-MAP`、`EXT-X-PROGRAM-DATE-TIME` を入れる。メディアプレイリストなので `CODECS` はなく、
  プレーヤーは各 init から形式を読む。timeline の `minutes[].codecs` に区間の形式
- 実 KVS で確認: 1 分に 2 回切り替えた区間(H.265 → H.264 → H.265)が 3 部分(14 / 19 / 17 断片)になり、スプライト 50 コマ、detector も clip から 50 枚で `DONE`
- **本物の Chrome(Playwright の `channel: 'chrome'`、hls.js)で vod.m3u8 を再生**(`viewer/scripts/codec-play.mjs`):
  その 1 分(49.5 秒)と前後を含む 3 分(169.5 秒)を最後まで再生、エラー 0。`BUFFER_CODECS` が hvc1 → avc1 → hvc1 と切り替わり、
  切り替え後の絵(焼き込みの `H264`)も確認

### 残っている課題

- 切り替え直後、S3 への退避前(直近 1〜2 分)を KVS の ON_DEMAND で再生すると、切り替え点で止まる(同じ理由。ライブは開き直す)。退避後は問題ない
- 06:57 の 2 回切り替えの区間は Lambda で `FAILED`(400)になった。同じ条件の再現(11:21)では成功し、原因は特定できていない。
  予備の経路(detector の GetImages)で検出は動く。部分の取り直しを断片の 400 で再試行するなら `archiver.build_clips` に足す
- `scripts/destroy.sh` は、途中で失敗して state の output が消えるとバケットを空にできない(手で空にした)

## 測定の方法

`tools/codec/`(Dockerfile: Debian trixie の libx264 / libx265 + ソースからビルドした libvmaf 3.0 と FFmpeg 7.1)。

```sh
docker build -t cc-codec-vmaf:1 tools/codec
docker run --rm -v $PWD/tools/codec:/opt/codec:ro -v $PWD/emulator/media:/media:ro -v /tmp/codec:/work cc-codec-vmaf:1 prepare
docker run --rm -v $PWD/tools/codec:/opt/codec:ro -v /tmp/codec:/work cc-codec-vmaf:1 sweep /work/ref/*.mkv --presets ultrafast --jobs 4
docker run --rm -v $PWD/tools/codec:/opt/codec:ro -v /tmp/codec:/work cc-codec-vmaf:1 summarize
```

1080p30 と 720p の比較(素材は [tools/codec/media/SOURCES.md](../tools/codec/media/SOURCES.md) から取り直して置く)。
4 並列で ultrafast 約 1 時間、medium 約 3 時間(10 コア):

```sh
docker run --rm -v $PWD/tools/codec:/opt/codec:ro -v $PWD/tools/codec/media:/media:ro -v /tmp/codec1080:/work cc-codec-vmaf:1 \
  prepare --scenes crowd,road_dusk --size 1920x1080 --fps 30
docker run --rm -v $PWD/tools/codec:/opt/codec:ro -v /tmp/codec1080:/work cc-codec-vmaf:1 sweep /work/ref/crowd.mkv /work/ref/road_dusk.mkv \
  --sizes 1280x720,1920x1080 --presets ultrafast,medium --jobs 4 \
  --crf-h264 18,20,22,24,26,28,30,32,34,36,38,40 --crf-h265 20,22,24,26,28,30,32,34,36,38,40,42,44
docker run --rm -v $PWD/tools/codec:/opt/codec:ro -v /tmp/codec1080:/work cc-codec-vmaf:1 summarize --targets 80,85,90,93,95
```

- 参照映像(FFV1 の可逆)はサンプル動画(`emulator/scripts/fetch_sample_media.py` の交差点、道路)、1 コマ静止 + ノイズ、testsrc に
  エミュレーターと同じ時刻の焼き込みを足したもの。エンコードの引数はエミュレーターと同じ(GOP 1 秒固定、シーンカットなし、B フレームなし、zerolatency)
- CRF を 18〜38 で変え、VMAF とビットレートの曲線から、同じ VMAF になるビットレートを log 補間して比を出す(BD-rate は VMAF 80〜97 の幾何平均)
- 参照の MKV の時刻(1 ms 単位)と 1/15 秒がずれると libvmaf の framesync が 1 つ前のフレームと比べてしまう(VMAF が 82 前後に落ちた)。両方の pts をフレーム番号で振り直している
- 結果: `tools/codec/results/results.jsonl`(1 件 = 1 エンコード)、`summary.json`、`kvs_compare.json`。1080p30 と 720p の比較は `tools/codec/results/1080p30/`
