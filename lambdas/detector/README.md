# lambdas/detector — 物体検出(1 分ごと、ONNX Runtime)

EventBridge Scheduler から 1 分ごとに `{"camera_id": "cam-001"}` で起動し、`lambdas/common` の `run_pipeline("DETECT", ...)` で
処理済み位置から 1 分区間ずつ次を行う(構想.md「物体検出」「検出結果の保存」)。

1. 動画の退避(video_archiver)がその区間を確定させるまで待つ。`run_pipeline(..., upstream_kind="VIDEO")` で、
   **物体検出の処理済み位置は動画の退避の処理済み位置を越えない**([common の README「上流を待つ」](../common/README.md))
2. `handler.FRAME_SOURCE(camera_id, start, end)` でフレームを取る。既定の `frame_source.clipfile` は区間の件の `video_status` を見て、
   `DONE` なら **S3 の `clip.mp4` を GET し、キーフレーム(1 秒に 1 枚、最大 60 枚)を PyAV でデコードして幅 640(縦横比を保つ、16:9 なら 640×360)に縮める**。
   `EMPTY` なら空、`FAILED` なら予備の経路(GetImages)。**GetImages は通常使わない**。画像は S3 に置かない
3. メモリ上で前処理(レターボックス)→ ONNX Runtime(CPU)で推論 → クラスごとの NMS
4. その日の Parquet(`detections/dt=YYYY-MM-DD/{camera_id}.parquet`)の **その 1 分の行を置き換える**(条件付き PUT)
5. 区間の件に `detect_count`、`frame_count` を返す(`detect_status` などは共通処理が書く)

## ベンチマーク結果と推奨設定

計測: 2026-10-24、東京リージョン、Python 3.13、onnxruntime 1.31.0。入力はサンプル動画 `crosswalk_toronto.mp4`(CC0)を
1 秒ごとに切り出した 60 枚(GetImages と同じ 16:9 の JPEG、`@416` なら 416×234 → 416×256 で推論)。一時関数 `cc-tmp-detect-bench`(arm64)/
`cc-tmp-detect-bench-x86` で、メモリと設定を変えて計測した(`tools/lambda_bench.py`、集計は `tools/summarize_bench.py`)。
このアカウントは関数のメモリ上限が 3,008 MB で、4,096 MB は計測できなかった。

### 推奨設定

| 項目 | 推奨 | 理由 |
| --- | --- | --- |
| アーキテクチャ | **arm64** | INT8 が x86 より速く(tiny INT8 は 1,769 MB で arm 3.2 秒 / x86 4.0 秒)、GB 秒の単価も 2 割安い。x86 の U8S8 は精度が落ち(検出 1,089 → 1,013 件)、S8S8 は遅い |
| モデル | **YOLOX-Tiny、INT8(S8S8)、入力 416×256** | Nano(FP32)と費用はほぼ同じで再現率が高い。INT8 で FP32 の約 1/3 の時間。S8S8 は U8S8 と同じ結果で 5〜10% 速い |
| メモリ | **1,024 MB**(最安は 512 MB) | 下の表のとおり 512 MB でも 1 分 10 秒以内に終わるが、混雑した映像だと 1 日の終わりの Parquet の書き換えで +300 MB 使う(下記)ので余裕を見る |
| スレッド数 | 自動(`DETECT_THREADS=0`) | 1,769 MB 以下では 1 と同じ。4 は大幅に遅い。x86 は 1 が速い |
| バッチ | 1 | 4 / 12 にしても速くならない |
| グラフ最適化 | `ORT_ENABLE_ALL`(既定) | INT8 は `basic` / `disable` だと 3.5 倍遅い(QDQ の融合が効かない)。FP32 は差がない |
| 入力の形 | 16:9 のまま(416×256) | 正方形 416×416 より 37% 速い。検出結果はほぼ同じ |

**1 分あたり・1 か月あたりの Lambda 費用**(1 台、東京。arm64 $0.0000133334 / GB 秒、x86 $0.0000166667 / GB 秒、リクエスト $0.20 / 100 万件。
Pricing API で確認。1 分の所要 = 60 枚の処理 + 2.1 秒(GetImages と Parquet の読み書き、実 KVS の通しでの実測)、1 か月 = 43,200 区間、無料枠は除く)

| arch | 設定 | メモリ | 1 分の所要(秒) | 1 分あたり | 1 か月あたり | person 再現率 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| arm64 | yolox_nano FP32 @416 | 512 | 8.1 | $0.000054 | $2.35 | 0.51 |
| arm64 | **yolox_tiny INT8(S8S8) @416** | 512 | 9.6 | $0.000064 | $2.78 | 0.61 |
| arm64 | **yolox_tiny INT8(S8S8) @416** | 768 | 7.2 | $0.000072 | $3.10 | 0.61 |
| arm64 | **yolox_tiny INT8(S8S8) @416(推奨)** | 1024 | 5.9 | $0.000078 | $3.39 | 0.61 |
| arm64 | yolox_tiny INT8(S8S8) @640 | 1024 | 10.3 | $0.000137 | $5.93 | 0.94 |
| arm64 | yolox_tiny INT8(S8S8) @416 | 1769 | 5.0 | $0.000116 | $5.03 | 0.61 |
| arm64 | yolox_tiny INT8(S8S8) @416 | 3008(Terraform の現在の既定) | 3.9 | $0.000151 | $6.54 | 0.61 |
| arm64 | yolox_tiny FP32 @416 | 1024 | 18.9 | $0.000252 | $10.89 | 0.65 |
| x86_64 | yolox_tiny INT8(U8S8) @416 | 1024 | 9.4 | $0.000157 | $6.80 | (精度低下) |
| x86_64 | yolox_tiny FP32 @416 | 1024 | 12.3 | $0.000206 | $8.89 | 0.65 |

- 試したすべての設定が 60 秒以内に終わった(最も遅い arm64 / tiny FP32 @640 / 1,024 MB でも 41 秒)。60 秒が問題になるのは
  障害後の追い上げで、1 回の起動で `MAX_MINUTES`(Terraform の既定 10)区間を処理すると 1,024 MB なら約 70 秒、512 MB なら約 100 秒(タイムアウト 300 秒の内)。
- 1 vCPU(1,769 MB)以下では時間がメモリにほぼ反比例するので GB 秒(= 推論の費用)はほぼ一定で、
  I/O 待ち(GetImages など約 2 秒)の分だけ小さいメモリが安い。1,769 MB を超えると 2 スレッド目が効き始めるが、費用は増える。
- **既定は入力幅 640**(2026-10-24 に 416 から変更)。416 の約 2.2 倍の時間(1,024 MB で推論 8.2 秒、取得込みで約 10.5 秒/分、月 約 $6)だが、person の再現率が 0.61 → 0.94 に上がるため。費用を優先するときは `DETECT_INPUT_WIDTH=416`。
- 同じ設定でも実行環境で ±30% ほどばらつく(arm64 / 1,024 MB / tiny INT8 が 1 回目 5.8 秒、別の時間帯で 3.9 秒)。表の 1,024 MB 以下は 2 回目の計測。
- 表の時間は**推論側だけ**(JPEG デコード、前処理、推論、NMS)。フレームの取得(ListFragments + GetImages、3 ページ)は別で、
  実 KVS の通しで **約 1.9 秒**(メモリによらずほぼ一定、I/O 待ち)。Parquet の読み書きは 0.1〜0.2 秒。
- 実 KVS での通し(下記)は 1,024 MB で 1 分 7.0〜7.3 秒(取得 1.9 秒 + 前処理 0.15〜0.36 秒 + 推論 4.6 秒 + NMS 0.15 秒 + 書き込み 0.1 秒)、512 MB で 9.1 秒(取得 1.9 秒 + 推論 6.3 秒)だった。

#### 精度の目安(`tools/compare_models.py`)

同じ 60 枚で、YOLOX-S(FP32、幅 640)の検出を仮の正解とし、ラベルが同じで IoU ≥ 0.5 のものを一致とした(COCO の mAP ではない)。

| 設定 | 検出数 | 適合率 | 再現率 | F1 | person 再現率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| yolox_nano FP32 @416 | 926 | 0.89 | 0.50 | 0.64 | 0.51 |
| yolox_nano INT8 @416 | 576 | 0.89 | 0.31 | 0.46 | 0.43 |
| yolox_tiny FP32 @416 | 1164 | 0.92 | 0.64 | 0.76 | 0.65 |
| **yolox_tiny INT8 @416** | 1102 | 0.93 | 0.62 | 0.75 | 0.61 |
| yolox_tiny INT8 @320 | 709 | 0.93 | 0.40 | 0.56 | 0.37 |
| yolox_tiny INT8 @512 | 1324 | 0.92 | 0.73 | 0.81 | 0.78 |
| yolox_tiny FP32 @640 | 1899 | 0.79 | 0.91 | 0.85 | 0.95 |
| yolox_s INT8 @416 | 898 | 0.96 | 0.52 | 0.68 | 0.44 |

Nano は INT8 にすると検出が 4 割減る(depthwise 畳み込みが量子化に弱い)ので、INT8 にするなら Tiny。S8S8 の結果は arm64 では U8S8 と同じ。

#### 60 枚の処理時間(秒、JPEG デコード + 前処理 + 推論 + NMS、2 回の平均)

| arch | 設定 | 512 MB | 768 MB | 1024 MB | 1769 MB | 2048 MB | 3008 MB | 検出数 | person |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| arm64 | yolox_nano.dyn @416 | 6.0 | 4.1 | 3.1 | 2.9 | 1.8 | 1.5 | 888 | 510 |
| arm64 | yolox_nano.dyn @416 threads=1 | 6.1 | 4.1 | 3.0 |  |  |  | 888 | 510 |
| arm64 | yolox_nano.int8 @416 |  |  | 2.8 | 1.6 | 1.2 | 1.0 | 595 | 437 |
| arm64 | yolox_s.int8 @416 |  |  | 8.6 | 4.8 | 3.4 | 2.7 | 900 | 431 |
| arm64 | yolox_tiny.dyn @320 |  |  |  | 5.5 |  | 3.0 | 772 | 378 |
| arm64 | yolox_tiny.dyn @416 |  |  | 16.8 | 9.4 | 6.2 | 5.1 | 1145 | 651 |
| arm64 | yolox_tiny.dyn @416 batch=4 |  |  |  | 9.4 |  | 5.0 | 1145 | 651 |
| arm64 | yolox_tiny.dyn @416 opt=basic |  |  |  | 9.6 |  | 5.1 | 1145 | 651 |
| arm64 | yolox_tiny.dyn @416 opt=disable |  |  |  | 9.5 |  | 5.2 | 1145 | 651 |
| arm64 | yolox_tiny.dyn @416 opt=extended |  |  |  | 9.4 |  | 5.1 | 1145 | 651 |
| arm64 | yolox_tiny.dyn @416 threads=1 |  |  |  | 9.3 |  | 5.3 | 1145 | 651 |
| arm64 | yolox_tiny.dyn @416 threads=2 |  |  |  | 10.3 |  | 5.3 | 1145 | 651 |
| arm64 | yolox_tiny.dyn @416 正方形 |  |  |  | 15.7 |  | 8.2 | 1153 | 659 |
| arm64 | yolox_tiny.dyn @512 |  |  |  | 12.9 |  | 7.0 | 1354 | 823 |
| arm64 | yolox_tiny.dyn @640 |  |  | 39.1 | 22.4 | 14.1 | 11.7 | 1899 | 1163 |
| arm64 | yolox_tiny.int8 @320 |  |  |  | 1.9 |  | 1.1 | 722 | 362 |
| arm64 | yolox_tiny.int8 @416 | 7.8 | 5.2 | 3.9 | 3.2 | 2.2 | 1.8 | 1089 | 595 |
| arm64 | yolox_tiny.int8 @416 batch=12 |  |  |  | 3.2 |  | 1.7 | 1089 | 595 |
| arm64 | yolox_tiny.int8 @416 batch=4 |  |  |  | 3.1 |  | 1.7 | 1089 | 595 |
| arm64 | yolox_tiny.int8 @416 opt=basic |  |  |  | 11.1 |  | 6.3 | 1094 | 598 |
| arm64 | yolox_tiny.int8 @416 opt=disable |  |  |  | 11.1 |  | 6.3 | 1094 | 598 |
| arm64 | yolox_tiny.int8 @416 threads=1 | 7.7 | 5.2 | 3.9 | 3.2 |  | 2.0 | 1089 | 595 |
| arm64 | yolox_tiny.int8 @416 threads=2 |  |  |  | 4.0 |  | 2.2 | 1089 | 595 |
| arm64 | yolox_tiny.int8 @416 threads=4 |  |  |  | 10.4 |  | 5.0 | 1089 | 595 |
| arm64 | yolox_tiny.int8 @416 正方形 |  |  |  | 5.1 |  | 2.8 | 1078 | 580 |
| arm64 | yolox_tiny.int8 @512 |  |  |  | 4.4 |  | 2.5 | 1324 | 801 |
| arm64 | yolox_tiny.int8 @640 |  |  | 13.0 | 7.3 | 4.9 | 4.1 | 1848 | 1115 |
| arm64 | yolox_tiny.int8s8 @416 | 7.5 | 5.1 | 3.8 | 2.9 | 2.2 | 1.8 | 1089 | 595 |
| arm64 | yolox_tiny.int8s8 @416 threads=1 | 7.4 | 5.0 | 3.8 |  |  |  | 1089 | 595 |
| arm64 | yolox_tiny.int8s8 @640 | 16.2 | 10.9 | 8.2 |  |  |  | 1848 | 1115 |
| arm64 | yolox_tiny.int8s8 @640 threads=1 | 16.1 | 10.9 | 8.1 |  |  |  | 1848 | 1115 |
| x86_64 | yolox_nano.dyn @416 |  |  | 3.6 | 2.0 | 1.6 | 1.1 | 888 | 510 |
| x86_64 | yolox_nano.int8 @416 |  |  | 3.8 | 2.3 | 1.7 | 1.2 | 481 | 381 |
| x86_64 | yolox_s.int8 @416 |  |  | 10.6 | 5.9 | 4.6 | 3.1 | 868 | 399 |
| x86_64 | yolox_tiny.dyn @320 |  |  |  | 3.2 |  | 1.7 | 772 | 378 |
| x86_64 | yolox_tiny.dyn @416 |  |  | 10.2 | 5.3 | 4.1 | 2.8 | 1145 | 651 |
| x86_64 | yolox_tiny.dyn @416 batch=4 |  |  |  | 5.0 |  | 2.6 | 1145 | 651 |
| x86_64 | yolox_tiny.dyn @416 opt=basic |  |  |  | 6.8 |  | 3.5 | 1145 | 651 |
| x86_64 | yolox_tiny.dyn @416 opt=disable |  |  |  | 6.8 |  | 3.5 | 1145 | 651 |
| x86_64 | yolox_tiny.dyn @416 opt=extended |  |  |  | 6.6 |  | 3.4 | 1145 | 651 |
| x86_64 | yolox_tiny.dyn @416 threads=1 |  |  |  | 3.8 |  | 3.9 | 1145 | 651 |
| x86_64 | yolox_tiny.dyn @416 threads=2 |  |  |  | 5.3 |  | 2.8 | 1145 | 651 |
| x86_64 | yolox_tiny.dyn @416 正方形 |  |  |  | 8.3 |  | 4.3 | 1153 | 659 |
| x86_64 | yolox_tiny.dyn @512 |  |  |  | 7.0 |  | 3.7 | 1354 | 823 |
| x86_64 | yolox_tiny.dyn @640 |  |  | 22.6 | 11.4 | 8.9 | 6.1 | 1899 | 1163 |
| x86_64 | yolox_tiny.int8 @320 |  |  |  | 2.5 |  | 1.3 | 662 | 323 |
| x86_64 | yolox_tiny.int8 @416 |  |  | 7.3 | 4.0 | 3.1 | 2.1 | 1013 | 551 |
| x86_64 | yolox_tiny.int8 @416 batch=12 |  |  |  | 3.3 |  | 1.9 | 1013 | 551 |
| x86_64 | yolox_tiny.int8 @416 batch=4 |  |  |  | 3.4 |  | 1.9 | 1013 | 551 |
| x86_64 | yolox_tiny.int8 @416 opt=basic |  |  |  | 9.5 |  | 4.8 | 1095 | 598 |
| x86_64 | yolox_tiny.int8 @416 opt=disable |  |  |  | 9.4 |  | 4.8 | 1095 | 598 |
| x86_64 | yolox_tiny.int8 @416 threads=1 |  |  |  | 2.7 |  | 2.7 | 1013 | 551 |
| x86_64 | yolox_tiny.int8 @416 threads=2 |  |  |  | 4.1 |  | 2.1 | 1013 | 551 |
| x86_64 | yolox_tiny.int8 @416 threads=4 |  |  |  | 8.9 |  | 5.6 | 1013 | 551 |
| x86_64 | yolox_tiny.int8 @416 正方形 |  |  |  | 6.1 |  | 3.3 | 989 | 524 |
| x86_64 | yolox_tiny.int8 @512 |  |  |  | 5.5 |  | 2.8 | 1194 | 704 |
| x86_64 | yolox_tiny.int8 @640 |  |  | 15.3 | 8.3 | 6.5 | 4.5 | 1707 | 1030 |
| x86_64 | yolox_tiny.int8s8 @416 |  |  | 17.3 | 9.4 | 7.2 | 4.8 | 1095 | 598 |

## モデル

| モデル | COCO mAP(val、公式) | パラメータ | ONNX | 入力 | ライセンス |
| --- | ---: | ---: | ---: | --- | --- |
| YOLOX-Nano | 25.8(416) | 0.91M | 3.7 MB | 416×416 | Apache-2.0 |
| **YOLOX-Tiny**(既定、INT8 量子化 S8S8) | 32.8(416) | 5.06M | 20 MB(INT8 5.3 MB) | 416×416 | Apache-2.0 |
| YOLOX-S | 40.5(640) | 9.0M | 36 MB(INT8 9.3 MB) | 640×640 | Apache-2.0 |

- 出典: [Megvii-BaseDetection/YOLOX](https://github.com/Megvii-BaseDetection/YOLOX)(Apache License 2.0)の公式 ONNX
  (リリース [0.1.1rc0](https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0))。mAP とパラメータ数は同リポジトリの README の値。
  Apache-2.0 なので、zip への同梱・改変(量子化)・商用利用ができる(LICENSE と NOTICE の扱いに従う)。
- Ultralytics の YOLOv5/v8/11 は AGPL-3.0 なので使わない。ほかに Apache-2.0 の候補として NanoDet-Plus、PP-PicoDet があるが、
  公式 ONNX がそのまま使えて後処理が単純な YOLOX を選んだ(比較は未実施)。
- INT8 量子化のキャリブレーション画像は [pjreddie/darknet](https://github.com/pjreddie/darknet) の `data/*.jpg`(パブリックドメイン、
  「YOLO LICENSE」)。テストの `tests/fixtures/person.jpg` も同じ出典。
- ベンチマークの画像は、エミュレーターのサンプル動画 `crosswalk_toronto.mp4`(CC0、`emulator/scripts/fetch_sample_media.py`)から
  1 秒ごとに切り出した 60 枚(歩行者と車が多数写る交差点)。

### 加工(`tools/prepare_model.py`)

- `*.dyn.onnx`: 入力の N/H/W を可変にする(Reshape の batch=1 固定を直す)。16:9 の画像を **416×256** で推論でき、
  正方形の 416×416 より計算量が約 4 割少ない。バッチ化もできる。opset は 11 → 13
- `*.int8.onnx`: 静的量子化(QDQ、重みは per-channel int8、活性は uint8、Percentile 99.99 で校正)
- `*.int8s8.onnx`(既定): 活性も int8(S8S8)。arm64 では U8S8 と同じ結果でやや速い。x86 では U8S8 が飽和して精度が落ちる(実測)が、S8S8 は遅い

## 使い方

```sh
cd lambdas/detector
mise exec -- uv sync
./fetch_models.sh                 # YOLOX の取得(SHA-256 確認)、キャリブレーション画像、*.dyn / *.int8 の作成
mise exec -- uv run pytest        # 単体テスト
./build.sh                        # → build/detector.zip(arm64)。docker が要る(pyav_min)。SMOKE=1 で Lambda のベースイメージで import と推論を確認
```

`build.sh` は Lambda 用の manylinux wheel(onnxruntime、numpy、pillow、pyarrow)を入れ、`lambdas/common` と
`models/$MODELS.onnx`(既定 `yolox_tiny.int8s8`)を同梱する。boto3 はランタイムのものを使う。

### 環境変数

Terraform と合意済み: `STATE_TABLE`、`MEDIA_BUCKET`、`DETECTIONS_BUCKET`、`STREAM_NAME_PREFIX`、`FRAME_SOURCE`、`UPSTREAM_MAX_WAIT_SECONDS`、`LAG_SECONDS`、`MAX_MINUTES`、`MAX_ATTEMPTS`、
`MEDIA_RETENTION_DAYS`。以下は省略可能(既定値で動く):

| 変数 | 既定 | 内容 |
| --- | --- | --- |
| `DETECT_MODEL` | `yolox_tiny.int8s8` | `models/{DETECT_MODEL}.onnx`。zip に同梱したものから選ぶ |
| `FRAME_SOURCE` | `clip` | `clip`(S3 の clip.mp4 をデコード)/ `getimages`(KVS GetImages、以前の方式) |
| `MEDIA_BUCKET` | (`clip` では必須) | clip.mp4 を読むメディアバケット(Terraform が全 Lambda に渡している) |
| `UPSTREAM_MAX_WAIT_SECONDS` | `1800` | `clip` のとき動画の退避を待つ上限(区間の終わりからの秒)。過ぎたら GetImages で取る |
| `DETECT_INPUT_WIDTH` | `640` | デコード(または GetImages)で作る画像の幅 = 推論の入力幅。高さは 32 の倍数に切り上げ(640 → 640×384) |
| `DETECT_IMAGE_HEIGHT` | `clip`: 縦横比を保つ / `getimages`: 幅 × 9/16 | 画像の高さ(GetImages は幅と高さの両方の指定が必須)。16:9 以外のカメラで指定する |
| `DETECT_THREADS` | `0`(自動) | ONNX Runtime の intra-op スレッド数 |
| `DETECT_BATCH` | `1` | 1 回の推論にまとめるフレーム数 |
| `DETECT_SCORE_THRESHOLD` / `DETECT_NMS_THRESHOLD` | `0.3` / `0.45` | 信頼度の下限、NMS の IoU |
| `MAX_LATE_SECONDS` | `720` | ウォーターマークを待つ上限(共通処理が読む。`clip` で動画の退避が確定させた区間には使わない) |

## フレームの取り方(`frame_source.py`)

取得は `FrameSource = (camera_id, start, end) -> FrameBatch` に切り出してあり、環境変数 `FRAME_SOURCE` で選ぶ(`frame_source.select`)。

- `FrameBatch.frames`: `[(フレームの時刻 UTC, 画像), ...]`(時刻順、`start ≤ ts < end`)。画像は JPEG / PNG の bytes、`PIL.Image`、
  HWC の RGB uint8 の numpy 配列のどれでもよい(`Detector.detect` が受け付ける)
- `FrameBatch.stats`: ログ(`detect_minute`)にそのまま出す項目

### `clip`(既定): S3 の clip.mp4 のキーフレーム

GetImages(1 枚 $0.00001、1 台・月 約 $26)をやめ、動画の退避が S3 に置いた `clip.mp4` を読んでキーフレームだけをデコードする
(`common.frames`、[lambdas/common/FRAMES.md](../common/FRAMES.md))。

| 区間の件の `video_status` | 動き |
| --- | --- |
| `DONE` | `files.clip` を GET し、`hls.parts` と `segments` から各断片の開始時刻を求めて(`frames.anchors_from_hls`)キーフレームをデコード。壊れていたら GetImages で取り直す(`fallback: decode_error`) |
| `EMPTY` | 録画なし。フレーム 0 枚(`detect_status = EMPTY`)。KVS も S3 も読まない |
| `FAILED` | 動画の退避が失敗した区間だけ、予備の経路の **GetImages** で取る(`fallback: video_failed`)。費用は失敗した区間の分だけ |
| なし | 動画の退避がまだ処理していない → `common.NotReady`(試行回数に数えず、次の起動で再挑戦)。区間の終わりから `UPSTREAM_MAX_WAIT_SECONDS` を過ぎていたら GetImages(`fallback: video_missing`) |

- 通常は `run_pipeline` が動画の退避の処理済み位置(`CURSOR#VIDEO`)で止めるので、「なし」になるのは動画の退避が止まったときだけ
- **フレームの時刻(Parquet の `ts`)は、その秒の枠に入るキーフレームの実際の時刻**(秒ちょうどではない。位相は送信開始時刻で決まり、送信ごとに一定。実 KVS の確認では毎秒 .634)。
  GetImages 版(指定した時刻の直後のフレーム)より最大 1 秒後ろになる。`sprite.jpg` の同じ枠のコマと同じフレーム
- 推論の入力は JPEG を通らない(デコードした絵を swscale で 640 に縮めたもの)ので、JPEG のデコードがなくなり、劣化も少ない
- IAM: メディアバケットの `s3:GetObject`、状態テーブルの `dynamodb:GetItem`。予備の経路のために `kinesisvideo:ListFragments` / `GetImages` も残す

### `getimages`: KVS の GetImages(以前の方式、`clip` の予備の経路)

ListFragments で録画の有無を見て、あれば GetImages で 1 秒間隔の JPEG(既定 640×360)を取る。終わりの時刻を含む 61 枚が返るので 61 枚目は捨てる。
範囲の始まりの近くに断片がないと `ResourceNotFoundException` になるので、そのときは録画がある範囲ごとに取り直す。
`FRAME_SOURCE=getimages` のときは上流(動画の退避)を待たず、自分のウォーターマークで判定する。x86_64 の zip(pyav_min なし)はこちらで使う。

## Parquet への追記(`parquet_store.py`)

1. その日のファイルを GetObject(なければ空)。ETag を覚える
2. `区間の開始 ≤ ts < 区間の終わり` の行を取り除き、新しい行を足して ts 順に並べる(再実行しても重複しない)
3. 既存なら `If-Match: ETag`、新規なら `If-None-Match: *` の条件付き PUT。412 / 409 なら読み直して最大 5 回やり直す
4. 検出が 0 件で、取り除く行もなければ PUT しない

ファイル名は区間の開始時刻(UTC)の日付なので、23:59 の区間は前日のファイル、00:00 の区間は翌日のファイルに入る。
列は docs/contracts.md のとおり(`ts` は timestamp[ms, UTC]、座標は 0〜1、`model` は `yolox_tiny_int8s8` のようにモデル名と量子化)。
zstd 圧縮。実 KVS の通しでは、人と車が多い交差点の映像で 1 分あたり約 1,000 件、3 分で 3,075 行・18 KB だった
(同じ動画の繰り返しなので圧縮が効きすぎている)。

1 日の終わりの重さ: 1 分 1,000 件 × 1,440 区間 = 144 万行(乱数の座標で 26.6 MB)の Parquet で 1 区間を置き換えると、
手元(M 系 Mac)で 0.3 秒、最大メモリ +300 MB だった。混雑した映像を 512 MB で動かすと足りなくなるおそれがあるので 1,024 MB を推奨にしている。
構想.md の想定(1 フレーム平均 2 件、1 日 17 万行)なら +40 MB 程度。

## 検証

| 内容 | 方法 | 結果 |
| --- | --- | --- |
| 前処理・後処理 | `tests/test_detect.py`(レターボックス、BGR、デコード、NMS、正規化座標) | OK |
| 既知の画像で person | `tests/fixtures/person.jpg` を全モデル(Nano / Tiny、FP32 / INT8)、幅 416 / 320 で | person(信頼度 > 0.5、位置も確認)、dog / horse |
| Parquet の冪等性 | `tests/test_parquet_store.py`(moto): 同じ区間の再実行で重複しない、検出 0 件の再実行で前回の行が消える、0 時またぎ(23:59 → 前日、00:00 → 翌日)、カメラ同士が干渉しない | OK |
| 条件付き PUT の競合 | 読んでから書くまでに別の書き手が割り込む(If-Match / If-None-Match の両方)、上限回数で諦める。**実 S3** でも 412 `PreconditionFailed` → 読み直して両方の行が残ることを確認 | OK |
| clip 版のフレーム源 | `tests/test_frame_source.py`(moto、PyAV でその場で作る H.265 の fMP4): キーフレームの時刻(秒ちょうどでない)、録画の切れ目、幅 640・縦横比、`video_status` なし → `NotReady` / 上限を過ぎたら GetImages、`EMPTY` → 空、`FAILED` と壊れた clip → GetImages、person の映る clip から Parquet まで(GetImages を呼ばない)、`upstream_kind` の受け渡し | OK |
| process_minute | `tests/test_handler.py`: GetImages の 61 枚目(終わりの時刻)を落とす、MEDIA_ERROR を飛ばす、`NotReady`、ResourceNotFound のときに録画範囲で取り直す | OK |
| zip | `SMOKE=1 ./build.sh`(Lambda のベースイメージで import・推論・Parquet) | arm64 / x86_64 とも OK |
| 実 KVS で通し | エッジなしで、サンプル動画(1280×720、H.265、1 秒ごとのキーフレーム)を `tools/kvs_put_media.py`(PutMedia)で一時ストリームに送り、本番の zip を一時関数(arm64)にデプロイして `run_pipeline` ごと実行(`tools/lambda_e2e.py`) | 下記 |

実 KVS の通しの結果(`cc-tmp-detect-h265`、H.265):

- **GetImages は H.265 で動く**(416×234 の JPEG)。幅と高さは両方指定が必要(片方だけだと `InvalidArgumentException`)。1 分を 1 秒間隔で取ると終わりの時刻を含む 61 枚が 3 ページ(1 ページ 25 枚)で返り、取得は約 1.9 秒
- 1 区間あたり 57 枚で推論、4 枚は `MEDIA_ERROR`(PutMedia のセッションの先頭と終わり、途中 2 か所。エッジからの連続送信では未確認)
- 1 区間の処理(tiny INT8): 2,048 MB で 4.2 秒(U8S8)、1,024 MB で 7.0〜7.3 秒(推論 81 ms / 枚)、512 MB で 9.1 秒(111 ms / 枚)。最大メモリ 305〜336 MB、コールドスタート 1.0〜1.2 秒(うちモデル読み込み 0.25 秒)
- DynamoDB の区間の件に `detect_status`(DONE / EMPTY)、`detect_count`、`frame_count`、`detect_duration_ms`、`detect_attempts` が入り、処理済み位置が進んだ。送信直後の区間は `minute_ready` で `not_ready` になり、送信が止まって 45 秒後に処理された
- Parquet は `detections/dt=2026-10-24/h265.parquet` に追記され、person / car / traffic light などが入った

### 実 KVS での確認(clip 版、2026-10-24)

`emulator/` のサンプル動画(`crosswalk_toronto.mp4`、H.265 1280×720 15fps、約 1 Mbps)を `lambdas/video_archiver/testing/kvssink` の送信コンテナで
一時 KVS ストリームに送り、`build.sh` で作った video_archiver と detector の zip を一時 Lambda(arm64)で毎分 1 回、video_archiver → detector の順に動かした。
一時関数の IAM には GetImages の権限を付けていない。

| 関数 | メモリ | Duration(REPORT、ウォーム) | Max Memory Used | 1 区間の内訳 |
| --- | ---: | ---: | ---: | --- |
| detector(yolox_tiny INT8 @640) | 1,024 MB | 9.4〜9.6 秒 | 531〜545 MB | 取得 1.32 秒(GetItem 6 ms + S3 GET 92 ms + デコード 0.60 秒 + 縮小 0.62 秒)、前処理 0.28 秒、推論 7.5 秒(125 ms / 枚)、NMS 0.22 秒、Parquet 0.13 秒 |

- 1 区間 60 フレーム(デコードの失敗 0、640×360)。person を含む 1 分 1,630〜1,684 件(6 分で person 5,018 件、car 1,319 件 など)
- Parquet の `ts` は毎秒 .634(キーフレームの実際の時刻)。sprite.jpg の同じ枠のコマと同じフレーム
- GetImages の呼び出し 0 回(CloudWatch `GetImages.Requests` なし、AccessDenied のログなし)
- 以前の GetImages 版(1,024 MB、@640)は取得 1.9 秒で 1 区間 10.3 秒。clip 版は取得が 1.3 秒になり、JPEG のデコードもなくなった
- 待ち合わせ: video_archiver より先に detector を呼ぶと `stopped: waiting_upstream`(区間の件を作らない、117 ms)。video_archiver が進めた後の起動で処理した
- 最大メモリは Parquet(その日の行)が増えるにつれて少しずつ増える(531 → 545 MB)

## ログ(1 行 1 JSON、`common.log`)

区間ごとに `detect_minute` を出す。主な項目: `frame_count`、`detect_count`、`source`(`clip` / `getimages`)、`fallback`(予備の経路にした理由)、
clip のとき `video_status`、`clip_bytes`、`get_item_ms`、`s3_get_ms`、`decode_ms`、`scale_ms`、`decoded`、`decode_lost`、`image_size`、
getimages のとき `image_errors`(GetImages の `MEDIA_ERROR` などの数)、`fetch_ms`(フレームの取得全体)、`preprocess_ms`、`inference_ms`、`inference_ms_per_frame`、`postprocess_ms`、
`write_read_ms` / `write_put_ms` / `write_attempts`(Parquet の読み書き)、`parquet_rows`、`parquet_bytes`、`total_ms`、`max_rss_mb`。
コールドスタートで `model_loaded`(`load_ms`)、競合時に `parquet_conflict`。

## zip のサイズ

`build/detector.zip`(arm64、既定のモデル 1 つ)は **zip 67 MB、展開後 190 MB**(上限 250 MB。zip の中のファイルの大きさの合計。`build.sh` が超えていないか確かめる)。
50 MB を超えるので Terraform は S3 経由でデプロイする。PyPI の av の wheel(展開後 91 MB)を入れると 250 MB を超えるので、最小構成の PyAV(`lambdas/common/pyav_min`)を入れる。

| 中身 | 展開後 | 備考 |
| --- | ---: | --- |
| pyarrow | 73.0 MB | 削る前 148 MB。Flight、S3、ORC、暗号化、CUDA、Gandiva などを削除、wheel 展開で複製された `lib*.so` を SONAME だけに、`strip --strip-unneeded`。substrait / dataset / acero は `lib.so` が DT_NEEDED で依存するので残す |
| onnxruntime | 49.9 MB | 量子化・変換ツール、`transformers` を削除(本体は配布時点で strip 済み) |
| numpy + numpy.libs(OpenBLAS) | 43.1 MB | テスト、f2py、ヘッダーを削除。`*.libs` は auditwheel が patchelf 済みで、strip すると読み込めない(page-aligned エラー) |
| Pillow + pillow.libs | 9.3 MB | AVIF / WebP / FreeType / LCMS を削除(JPEG のデコードとリサイズだけ使う) |
| models | 5.1 MB | `yolox_tiny.int8s8.onnx` |
| av + lib(pyav_min) | 15.2 MB | PyAV 19.0.2 + FFmpeg 8.1(LGPL、HEVC / H.264 のデコーダと mov のデマクサー、swscale だけ)。docker でビルドし、`build/.cache/pyav_min/` にキャッシュ(2 回目からは docker を使わない) |
| 本体 + common + その他 | 約 1 MB | |

- 依存は合計で削る前 279 MB → 176 MB。x86_64 版は onnxruntime が 59 MB と大きく、展開後 約 190 MB。
- AWS SDK for pandas のマネージドレイヤー(`AWSSDKPandas-Python313-Arm64:8`)は展開後 161 MB(pyarrow 20、pandas、numpy など)。
  onnxruntime と Pillow を足すと約 230 MB で収まるが、自前で削った方が 50 MB 小さく、numpy の版も自分で決められるので使っていない。
- `SMOKE=1 ./build.sh` で Lambda のベースイメージ(`public.ecr.aws/lambda/python:3.13-arm64`)に展開物を載せ、import・推論・Parquet の読み書き、
  PyAV(`/var/task/lib` の FFmpeg が読めて HEVC / H.264 のデコーダがあること)を確かめる。

## ベンチマークと検証のツール(`tools/`)

| ファイル | 内容 |
| --- | --- |
| `prepare_model.py` | 入力可変化と INT8 量子化 |
| `compare_models.py` | 設定ごとの検出結果の違い(YOLOX-S 640 を仮の正解にした適合率・再現率) |
| `make_bench_frames.py` / `build_bench.sh` | ベンチマーク用の 60 枚と、全モデルを入れたベンチマーク用 zip |
| `bench.py` | ベンチマークの Lambda ハンドラー(手元でも動く) |
| `lambda_bench.py` | 一時関数 `cc-tmp-detect-bench`(arm64)/ `cc-tmp-detect-bench-x86` を作り、メモリ × 設定で計測、削除 |
| `summarize_bench.py` | 結果を表と費用にまとめる |
| `kvs_put_media.py` | MKV を KVS の PutMedia で送る(エッジなしで GetImages を試す) |
| `lambda_e2e.py` | 本番の zip を一時関数にデプロイし、一時 KVS ストリーム・DynamoDB・S3 で通しを確かめる |
| `smoke.py` / `strip_libs.sh` | build.sh から使う |

一時リソースはすべて `cc-tmp-detect-` で始まる。`lambda_e2e.py cleanup` → `lambda_bench.py cleanup` で消え、
最後に残りがないことを確認する(`lambda_bench.py verify`)。
