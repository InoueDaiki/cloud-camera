# lambdas/common/frames.py — GetImages をやめてキーフレームを Lambda 内でデコードする(案 B)

KVS GetImages(1 枚 $0.00001)はスプライト用と検出用で 1 分 61 枚ずつ取っており、1 台・月 $26.35 × 2 = $52.7 になる
([tools/cost/README.md](../../tools/cost/README.md))。案 B では GetImages を使わず、手元にある fMP4(HLS の init + 断片、
S3 の `clip.mp4`)から **キーフレーム(1 秒に 1 枚)だけ** を PyAV でデコードする。

この文書には、試作した `frames.py`、計測の結果、費用の比較、ハンドラーへの組み込み方の案を書く。**ハンドラーへの組み込みはまだしていない。**

## 結論

- **費用**: 変わる項目(GetImages、2 本の Lambda、S3 / KVS の取り出し)の合計は、1 台・月で **現状 $59.18 → A 案 $32.23 → B 案 $6.77**(1 Mbps の場合)。
  B 案で detector が KVS の HLS から断片を取ると、取り出し料金がかかって $11.67(2 Mbps なら $16.31)になる。**detector は S3 の `clip.mp4` を読む**のがよい
- **デコード時間**: 720p の 1 分(キーフレーム 60 枚)のデコードと、640×360・160×90 への縮小、スプライトの作成で、
  512 MB なら 2.4〜3.0 秒、1,024 MB なら 1.0〜1.5 秒。1080p はその約 2 倍(下の表)
- **zip**: PyPI の `av` の wheel は展開後 91 MB あり、detector(展開後 181 MB)に足すと **272 MB で上限の 250 MB を超える**。
  FFmpeg を HEVC / H.264 のデコードだけに絞ってビルドした PyAV(`pyav_min/`、展開後 15 MB)なら **197 MB** で収まる
- **ライセンス**: PyPI の wheel は libx264 / libx265(GPL)を同梱している。`pyav_min/` は外部ライブラリを使わない LGPL 2.1+ の FFmpeg なので、こちらを推奨する
- **画質**: スプライトにも推論の入力にも問題ない。GetImages の JPEG と見た目は変わらない。デコードしたフレームは JPEG を通さないので、劣化はむしろ少ない
- **信頼性**: 検証中、3 本のうち 2 本のストリームで、ある時刻から GetImages が全コマ `MEDIA_ERROR` を返し続けた。同じ断片を HLS で取って PyAV でデコードすると、エラーなしで全フレームが読めた(下記)

## frames.py の API

```python
from common import frames

anchors = frames.anchors_from_fragments(clip.parts, [f.producer_ms for f in clip.fragments])  # video_archiver
# anchors = frames.anchors_from_hls(item["hls"]["parts"], item["segments"])                  # DynamoDB の区間の件から
r = frames.decode_keyframes(clip_bytes, start_ms=..., end_ms=..., anchors=anchors,
                            sizes={"sprite": (160, 90), "detect": (640, 360)})   # 高さ None は縦横比を保つ
for f in r.frames:            # 時刻順。区間 [start, end) の 1 秒ごとの枠に最大 1 枚
    f.ts_ms, f.slot, f.images["detect"]   # プロデューサー時刻(epoch ms)、枠の番号(0〜59)、PIL.Image
jpeg, n = frames.build_sprite(r.frames, "sprite")   # 10×6 のシート。コマがない枠は黒
r.stats   # packets / keyframes / decoded / skipped_outside / skipped_filled / decode_ms / scale_ms / total_ms など
```

- **非キーフレームはデコーダに渡さない**(パケットのキーフレーム印で選ぶ。念のため `skip_frame="NONKEY"` も付ける)。1 分 900 フレームのうちデコードするのは 60 枚
- **時刻**: 「断片の開始時刻(ListFragments の ProducerTimestamp)+ 断片の中での pts の差」。`anchors` には、clip.mp4 の中での断片のオフセットと開始時刻の組を渡す。
  パケットのファイル内の位置(`packet.pos`)から断片を決めるので、録画の切れ目があっても時刻はずれない。
  `anchors` の代わりに `base_ms`(最初のフレームの時刻)を渡すと、fMP4 の時間軸(tfdt)だけで求める。
  実 KVS の切れ目(送信を 45 秒止めて再開した区間)でも、両者の結果は一致した(KVS の HLS の tfdt は実時刻の差を保っている)
- **間引き**: 区間 `[start, end)` を `interval_ms`(既定 1,000)ごとの枠に分け、各枠の最初のキーフレームだけを使う。区間の外のキーフレームと、
  枠がもう埋まっているキーフレームはデコードしない(キーフレームが 0.5 秒おきでも 1 秒 1 枚になる)。キーフレームが 2 秒おきなら、枠は 1 つおきに空く
- **縮小**: 一番大きいサイズだけを swscale(YUV → RGB と縮小を 1 回で。既定は `AREA`)で作り、小さいサイズはその RGB から Pillow の BOX で縮める。
  swscale を 2 回呼ぶより速い(1080p、1 コアで 60 枚 297 → 99 ms。フル解像度の YUV → RGB 変換が重い)
- `output="numpy"` で HWC の RGB uint8、`"frame"` で `av.VideoFrame`(縮小しない)を返す
- 依存: PyAV と Pillow(numpy は `output="numpy"` のときだけ)。`common` の既定の依存には入れていない(使う Lambda の zip にだけ入れる)

### GetImages との違い(時刻の意味)

GetImages は指定した時刻のフレーム(多くは P フレーム。指定時刻の直後のフレーム)を返す。案 B は、その秒の枠に入るキーフレームを返す。
キーフレームの位相は送信を始めた時刻で決まる(実測では枠の開始から 0.5〜0.8 秒後)。
そのため、スプライトのコマも検出の時刻も、GetImages より最大 1 秒後ろにずれる。どちらも 1 秒に 1 枚で、時刻は実際のフレームの時刻(`ts_ms`)を使う。
`ts_ms` は必ず `[start, end)` に入るので、Parquet の「区間の行を取り除いてから足す」と矛盾しない。

## テスト

```sh
cd lambdas/common
mise exec -- uv run --with av --with pillow --with numpy pytest tests/test_frames.py
```

テスト用の H.265 fMP4(192×108、10 fps、1 秒 1 キーフレーム、`frag_keyframe+empty_moov+default_base_moof`)は、
PyAV の wheel に入っている libx265 でその場で作る。秒ごとに明るさを変え、デコードした絵がどの秒のキーフレームかまで確かめている。
扱うケース: 1 秒 1 枚、半開区間、間引き(2 秒間隔、0.5 秒ごとのキーフレーム)、時間軸の空白(録画の切れ目)、anchors による時刻の上書き、
`anchors_from_hls`、ファイルパスと numpy 出力、スレッド数、スプライト。
PyAV が入っていなければ、このテストは skip される(`uv run pytest` だけなら既存のテストだけが動く)。

## 計測(2026-10-24、東京、arm64、Python 3.13)

### 映像

エミュレーター(`emulator/`)の H.265(libx265 ultrafast、CRF + VBR、15 fps、キーフレーム 1 秒)を kvssink で一時の KVS ストリーム `cc-tmp-frames-*` に送り、
video_archiver の `build_clip` と同じ方法(HLS の init + 断片を連結)で、1 分の clip.mp4 と GetImages の JPEG を取った。

| 名前 | 中身 | 解像度 | ビットレート(実測) | clip.mp4 |
| --- | --- | --- | --- | --- |
| `t720` | テストパターン(testsrc) | 1280×720 | 0.56 Mbps | 4.2 MB |
| `s720` | サンプル動画 `crosswalk_toronto.mp4`(歩行者と車) | 1280×720 | 1.05 Mbps | 7.9 MB |
| `s1080` | 同じ動画を 1920×1080 に拡大 | 1920×1080 | 2.07 Mbps | 15.5 MB |

### デコード時間(一時関数 `cc-tmp-frames-bench`、ウォーム、2 回の中央値、秒)

S3 から clip.mp4 を GET して、60 キーフレームをデコードする。「両方」は 640×360 と 160×90 を作り、スプライト(JPEG)まで作った時間。
`pyav_min` は下記の最小ビルド、`wheel` は PyPI の av 19.0.2。

| メモリ | 映像 | pyav_min: デコードのみ | +160×90 + スプライト | +640×360 | 両方 | wheel: 両方 | S3 GET | 最大メモリ |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 256 | s720 | 4.25 | 4.28 | 4.84 | 5.12 | 4.93 | 0.27 | 256 MB(上限) |
| 256 | t720 / s1080 | | | | 4.11 / 10.33 | 3.91 / 9.83 | | |
| 512 | s720 | 2.40 | 2.36 | 2.79 | 3.00 | 2.45 | 0.15 | 332 MB |
| 512 | t720 / s1080 | | | | 2.34 / 6.08 | 1.95 / 4.88 | | |
| 1024 | s720 | 1.19 | 1.17 | 1.39 | 1.49 | 0.99 | 0.12 | 333 MB |
| 1024 | t720 / s1080 | | | | 1.16 / 3.00 | 0.78 / 1.99 | | |
| 1769 | s720 | 0.68 | 0.68 | 0.82 | 0.86 | 0.70 | 0.11 | 336 MB |
| 1769 | t720 / s1080 | | | | 0.68 / 1.75 | 0.56 / 1.40 | | |
| 3008 | s720 | 0.35 | 0.34 | 0.40 | 0.44 | 0.44 | 0.10 | 332 MB |
| 3008 | t720 / s1080 | | | | 0.35 / 0.91 | 0.32 / 0.86 | | |

- 時間の大半はキーフレームのデコード。縮小は 0.2〜0.3 秒(1,024 MB)、スプライトの JPEG は 10〜40 ms
- 1,769 MB 以下では時間がメモリにほぼ反比例するので、GB 秒(= 費用)はメモリによらずほぼ一定(720p で 1 分あたり約 1.2〜1.5 GB 秒)
- wheel の方が 512〜1,769 MB で 2〜3 割速い。FFmpeg の版(wheel は開発版の libavcodec 63、pyav_min は 8.1 の 62)かコンパイラの違いと思われる(未調査)。
  費用の差は 1 台・月 $0.3 程度
- 最大メモリは 330〜370 MB(clip.mp4 をメモリに持つ分を含む)。**256 MB は上限に張り付く**ので、デコードする関数は 512 MB 以上にする
- 手元(Apple Silicon、docker、1 コアに制限)での縮小方法の比較(1080p、60 枚): swscale で 640 と 160 を別々に作ると 297 ms、
  640 を作ってから Pillow で 160 に縮めると 99 ms(今の実装)

### コールドスタート(s720、両方)

| メモリ | pyav_min: Init / import av / 処理全体 | wheel: Init / import av / 処理全体 |
| ---: | --- | --- |
| 512 | 0.33 / 0.14 / 5.0 秒 | 0.42〜0.45 / 0.27〜0.30 / 4.5〜4.6 秒 |
| 1024 | 0.28〜0.33 / 0.13〜0.14 / 2.2〜2.5 秒 | 0.34〜0.41 / 0.23〜0.27 / 1.8〜2.2 秒 |
| 1769 | 0.25〜0.33 / 0.10〜0.14 / 1.3〜1.5 秒 | 0.43〜0.44 / 0.28 / 1.3 秒 |
| 3008 | 0.26 / 0.12 / 1.0 秒 | 0.46〜0.52 / 0.31〜0.35 / 1.0〜1.2 秒 |

コールドでは、ウォームの時間に加えて Init(PyAV と Pillow の import)と boto3 の初期化(0.3〜1 秒)がかかる。1 分ごとの起動なら、ほぼウォームで動く。

### zip のサイズ(展開後、上限 250 MB)

| 構成 | 展開後 | zip |
| --- | ---: | ---: |
| PyPI の av 19.0.2 + Pillow(ベンチマーク用) | 111 MB(av 37 MB + av.libs 54 MB) | 37 MB |
| pyav_min + Pillow(ベンチマーク用) | 36 MB(av 11 MB + lib 4.6 MB) | 9 MB |
| detector(`lambdas/detector/build.sh`、今の main) | 181 MB | 64 MB |
| detector + PyPI の av | **272 MB(超過)** | 95 MB |
| detector + pyav_min | **197 MB(余裕 53 MB)** | 67 MB |

- PyPI の wheel の `av.libs` は、libavcodec(16 MB)のほかに libx265(8 MB)、libSvtAv1Enc(4 MB)、gnutls、libvpx、libx264、libdav1d、libxcb などを同梱している。
  これらは libavcodec / libavformat から DT_NEEDED で参照されているので、消すと読み込めない。auditwheel の patchelf 済みのため strip もできない。
  wheel のままで 250 MB に収めるには、detector の側(pyarrow など)をさらに 22 MB 以上削る必要がある
- `pyav_min/` は FFmpeg 8.1 を `--disable-everything --disable-autodetect` で構成し、HEVC / H.264 のデコーダとパーサー、mov(MP4)のデマクサー、
  file / pipe プロトコル、swscale だけを有効にしている。PyAV 19.0.2 はソースからこの FFmpeg にリンクしてビルドする(Lambda のベースイメージの中で)。
  ライブラリは zip の `lib/` に置く(Lambda の `LD_LIBRARY_PATH` に `/var/task/lib` が入っている)
- video_archiver(今は Pillow だけで約 20 MB)は、どちらを足しても 250 MB には余裕で収まる

### 画質(GetImages の JPEG と、デコードしたキーフレームの比較)

clip.mp4 を全フレームデコードし、GetImages(フル解像度)の各画像が、どのフレームと一致するかを PSNR で探した(1 分 = 61 枚)。

| 映像 | GetImages の JPEG(フル) | 一致したフレーム | JPEG の劣化(PSNR 平均) | 640×360 の GetImages と同じフレームの縮小 | 160×90 の同じ比較 |
| --- | --- | --- | ---: | ---: | ---: |
| t720 | 1280×720、平均 27 KB | 指定時刻の直後のフレーム。キーフレームは 61 枚中 1 枚 | 32.9 dB | 28.8 dB | 22.1 dB |
| s720 | 1280×720、平均 89 KB | 同上。キーフレームは 0 枚 | 35.3 dB | 31.8 dB | 27.2 dB |
| s1080 | 1920×1080、平均 159 KB | 同上。キーフレームは 0 枚 | 36.7 dB | 31.0 dB | 27.2 dB |

- GetImages は、指定した時刻のフレームを(P フレームも)デコードして JPEG にしている。案 B は、その秒のキーフレームそのもの(JPEG を通さない)
- 640×360 と 160×90 の PSNR が低めなのは、縮小フィルターの違い(GetImages の縮小と swscale の AREA)による差。目で見て分かる差ではない
- 640×360 を並べて見比べても、スプライトを上下に並べて見比べても、違いは時刻のずれ(最大 1 秒)だけだった。
  ぼけ、ブロックノイズ、色ずれはない。キーフレームはエンコーダが多くのビットを割り当てるので、同じ GOP の中では画質が最も良い
- スプライト(JPEG quality 70)は GetImages 版 234 KB、案 B 247 KB(s720 / s1080)。案 B の方が少し大きい(元の絵が JPEG を通っていない分、細部が残る)

### GetImages の MEDIA_ERROR(参考)

検証の途中(04:15 UTC ごろ)から、`t720` と `s720` のストリームで、GetImages が全コマ `MEDIA_ERROR` を返し続けるようになった
(解像度の指定を変えても、15 分後に取り直しても同じ。`s1080` は正常なまま)。同じ区間の断片を HLS で取った clip.mp4 は、PyAV で 900 フレームをエラーなしでデコードできた。
原因は特定していない(送信側の再起動はしていない)。現状の方式では、こうなるとスプライトは全コマ黒になり、物体検出は 0 枚になる。
案 B はデコードが自前なので、この種の失敗の影響を受けない。

## 費用の比較(1 台・月、東京の単価、43,200 区間がすべて録画あり)

変わる項目だけを並べる(KVS の取り込み・保存、S3 の保存、DynamoDB などは同じ)。単価は `tools/cost/prices_ap-northeast-1.json`
(GetImages $0.00001 / 枚、Lambda arm64 $0.0000133334 / GB 秒、KVS の HLS 取り出し $0.01536 / GB、S3 GET $0.00037 / 1,000 件、PUT $0.0047 / 1,000 件)。

前提:
- 現状の video_archiver: 512 MB、1 区間 1.9 秒(うち GetImages 0.61 秒。video_archiver の README の実測)
- 現状の detector: 640×360、1,024 MB、1 区間 10.3 秒(うち ListFragments + GetImages 1.9 秒。detector の担当の実測、月 $5.93)
- B 案のデコード時間は pyav_min の s720(1.05 Mbps)の実測。video_archiver は 512 MB で「160×90 + スプライト」2.36 秒、
  detector は 1,024 MB で「640×360」1.39 秒 + S3 GET 0.12 秒
- A 案: video_archiver が 640×360 の GetImages を 61 枚取り、スプライトはそこから縮小(+0.3 秒と推定)。60 枚を 1 ファイルにまとめて S3 に置き(1 日で消す)、detector が GET する

**1 Mbps(clip.mp4 7.5 MB / 分)**

| 方式 | GetImages | video_archiver | detector | S3 / KVS 取り出し(増分) | 合計 | 現状との差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 現状(GetImages を 2 回) | $52.70 | $0.55 | $5.93 | $0 | **$59.18** | — |
| A 案(GetImages を 1 回取って共有) | $26.35 | $0.63 | $4.95 | $0.30 | **$32.23** | −$26.95 |
| **B 案(detector は S3 の clip.mp4)** | $0 | $1.05 | $5.70 | $0.02 | **$6.77** | **−$52.42** |
| B 案(detector は KVS の HLS) | $0 | $1.05 | $5.98 | $4.63 | **$11.67** | −$47.51 |

2 Mbps なら、HLS 版の取り出しは $9.27(合計 $16.31)、S3 版は変わらない(1080p はデコードが約 2 倍かかるので、Lambda が月 $0.5〜1 増える)。
tools/cost の机上計算の合計(約 $100)から見ると、B 案(S3)で約 $52 下がる。

- B 案の video_archiver の増分(+$0.50)は、GetImages の待ち時間(0.61 秒)がなくなる代わりに、デコードの CPU 時間(2.4 秒 @512 MB)が増えるため
- B 案の detector は、取得 1.9 秒が S3 GET + デコード 1.5 秒に置き換わるので、少し安くなる。JPEG のデコードもなくなる(推論の前処理が軽くなる。表には入れていない)
- S3 の GET は 1 区間 1 回で月 $0.016。区間の件の GetItem(月 43,200 回、$0.006)も要るが無視できる

## detector のフレーム源: S3 の clip.mp4 と KVS の HLS の比較

| | S3 の clip.mp4 | KVS の HLS |
| --- | --- | --- |
| 費用(1 台・月) | GET $0.016 | 取り出し $4.6(1 Mbps)〜 $9.3(2 Mbps)。ビットレートに比例 |
| 取得時間 | 0.1〜0.15 秒(7.9 MB、1,024 MB) | 約 0.5〜0.8 秒(video_archiver の実測。並列 8 本) |
| 依存 | **video_archiver が先にその区間を DONE にしている必要がある** | なし(今と同じく KVS だけ) |
| 時刻の基準 | 区間の件の `hls.parts` と `segments` から `anchors_from_hls` で作る | ListFragments の結果から `anchors_from_fragments` で作る |
| 録画なし / 失敗 | `video_status` が `EMPTY` なら空、`FAILED` なら下の代替へ | 断片がなければ空 |

**推奨は S3 の clip.mp4。** 依存は、次の扱いで吸収できる:
- detector の `LAG_SECONDS` は 60 秒(video_archiver は 30 秒)なので、通常は video_archiver が先に終わっている
- 区間の件の `video_status` がまだない(video_archiver が処理していない)ときは `common.NotReady` を投げる(試行回数に数えず、次の起動で再挑戦)。
  video_archiver の処理済み位置が KVS の保持期限(1 時間)に近づくまで遅れていたら、HLS か GetImages に切り替える
- `video_status = FAILED` の区間だけ、HLS(または GetImages)で取る。費用は失敗した区間の分だけ

## ハンドラーへの組み込み方(案)

### video_archiver

`archiver.py` の `process_minute` で、GetImages(`self._images`)の代わりに、作ったばかりの `clip.data` からスプライトを作る。

```python
from common import frames

clip = self.clip_builder(stream, start, end, frags)
anchors = frames.anchors_from_fragments(clip.parts, [f.producer_ms for f in clip.fragments])
r = frames.decode_keyframes(clip.data, start_ms=epoch_ms(start), end_ms=epoch_ms(end), anchors=anchors,
                            sizes={"sprite": (THUMB_W, THUMB_H)})
sprite, sprite_frames = frames.build_sprite(r.frames, "sprite")
```

- 例外(壊れた断片など)はスプライトだけの失敗にして、今と同じく `sprite_error` に残して `DONE` にする
- 区間の頭の秒(10:20:00 の枠)は、その秒に始まる断片のキーフレームが入る。前の区間の断片から続く部分は使わない(今の GetImages は、前の区間の断片から 10:20:00 の絵が取れていた)
- `build.sh`: `lambdas/common/pyav_min/build.sh "$STAGE"` を足す(Pillow はそのまま)。`common/` の rsync で `pyav_min`、`FRAMES.md` も除く
- IAM: `kinesisvideo:GetImages` が要らなくなる。メモリは 512 MB のまま(256 MB はデコードでメモリの上限に張り付く)。タイムアウト 60 秒で足りる(1 区間 3〜4 秒、1080p で 7 秒)
- ログ: `r.stats`(`decode_ms`、`scale_ms`、`decoded` など)を `minute_metrics` に入れる

### detector

`frame_source.py` に `clipfile(camera_id, start, end) -> FrameBatch` を足し、`handler.FRAME_SOURCE` を差し替える(環境変数 `FRAME_SOURCE=clip|getimages` で選ぶ)。

```python
def clipfile(camera_id, start, end):
    item = table.get_item(Key={"camera_id": camera_id, "sk": iso(start)}).get("Item") or {}
    status = item.get("video_status")
    if status is None:
        raise NotReady("video_archiver がまだ処理していない")
    if status == "EMPTY":
        return FrameBatch([], {"source": "clip", "video_status": status})
    if status == "FAILED":
        return getimages(camera_id, start, end)          # 失敗した区間だけ GetImages(または HLS)
    body = s3.get_object(Bucket=MEDIA_BUCKET, Key=item["files"]["clip"])["Body"].read()
    anchors = frames.anchors_from_hls(item["hls"]["parts"], item["segments"])
    r = frames.decode_keyframes(body, start_ms=epoch_ms(start), end_ms=epoch_ms(end), anchors=anchors,
                                sizes={"detect": (width, height)})          # 既定 640×360
    return FrameBatch([(from_epoch_ms(f.ts_ms), f.images["detect"]) for f in r.frames],
                      {"source": "clip", **r.stats})
```

- DynamoDB の数値は Decimal で返るので、`anchors_from_hls` の中で int に直している
- `detect.Detector.detect` は PIL.Image を受け付けるので、JPEG のデコードがなくなる
- `build.sh`: `pyav_min/build.sh "$STAGE"` を足す(展開後 197 MB)。PyPI の av を `pyproject.toml` に足すと 250 MB を超える
- IAM: `s3:GetObject`(メディアバケット)、`dynamodb:GetItem`(状態テーブル)。`FAILED` の代替に GetImages を残すなら、今の KVS の権限もそのまま
- 環境変数: `MEDIA_BUCKET` を足す

## ライセンス

| もの | ライセンス | 備考 |
| --- | --- | --- |
| PyAV(`av`)本体 | BSD-3-Clause | |
| PyPI の `av` 19.0.2 の wheel に同梱の FFmpeg | FFmpeg 自体は「LGPL version 3 or later」と表示 | ただし **libx264 / libx265(GPL v2+)を同梱してリンクしている**(`--enable-libx264 --enable-libx265`)。全体としては GPL のものとして扱うのが安全。av 14.x の wheel は `--enable-gpl` で、表示も GPL v3 |
| `pyav_min/` の FFmpeg 8.1 | **LGPL 2.1 or later** | `--disable-autodetect` で外部ライブラリを一切リンクしない。共有ライブラリとして動的にリンク |
| Pillow | MIT-CMU(HPND) | |

- Lambda に置くだけで第三者に配布しないので、GPL の wheel でも義務は生じにくい。それでも、デコードしか使わないのに GPL のエンコーダを抱える理由はないので、`pyav_min` を推奨する
- 著作権のライセンスとは別に、**H.265 には特許プール(Access Advance、Via LA など)がある**。GetImages では AWS の側でデコードしていたが、案 B では自前でデコードする。商用化の前に、特許の扱いを確認する(この検証の範囲外)

## 一時リソース

計測には `cc-tmp-frames-` で始まる次のリソースを使い、すべて削除した: KVS ストリーム 3 本(`cc-tmp-frames-t720` / `s720` / `s1080`)、
S3 バケット `cc-tmp-frames-<account>`、IAM ロール `cc-tmp-frames-role`、Lambda 関数 `cc-tmp-frames-bench` / `cc-tmp-frames-bench-min`(と、そのロググループ)、
docker のコンテナ・ネットワーク・イメージ(`cc-tmp-frames-emu`、`cc-tmp-frames-send-*`、`cc-tmp-frames-net`、`cc-tmp-frames-kvssink`、`cc-tmp-frames-minav`)。
