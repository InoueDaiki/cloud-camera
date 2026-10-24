# lambdas/detector — 物体検出(1 分ごと、ONNX Runtime)

EventBridge Scheduler から 1 分ごとに `{"camera_id": "cam-001"}` で起動し、`lambdas/common` の `run_pipeline("DETECT", ...)` で
処理済み位置から 1 分区間ずつ次を行う(構想.md「物体検出」「検出結果の保存」)。

1. `kvs.minute_ready` で断片が出そろったかを確かめる(送信が遅れていれば `NotReady` で次の起動に回す)
2. ListFragments で録画の有無を見て、あれば **GetImages で 1 秒間隔の JPEG(最大 60 枚、既定 416×234)** を取る。画像は S3 に置かない
3. メモリ上で前処理(レターボックス)→ ONNX Runtime(CPU)で推論 → クラスごとの NMS
4. その日の Parquet(`detections/dt=YYYY-MM-DD/{camera_id}.parquet`)の **その 1 分の行を置き換える**(条件付き PUT)
5. 区間の件に `detect_count`、`frame_count` を返す(`detect_status` などは共通処理が書く)

## ベンチマーク結果と推奨設定

(この節は tools/summarize_bench.py の出力から作る。下記「ベンチマーク」参照)

BENCH_PLACEHOLDER

## モデル

| モデル | COCO mAP(val、公式) | パラメータ | ONNX | 入力 | ライセンス |
| --- | ---: | ---: | ---: | --- | --- |
| YOLOX-Nano | 25.8(416) | 0.91M | 3.7 MB | 416×416 | Apache-2.0 |
| **YOLOX-Tiny**(既定、INT8 量子化) | 32.8(416) | 5.06M | 20 MB(INT8 5.3 MB) | 416×416 | Apache-2.0 |
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
- `*.int8s8.onnx`: 活性も int8(S8S8)。x86 の VNNI なし CPU では U8S8 が飽和して精度が落ちることがあるための比較用

## 使い方

```sh
cd lambdas/detector
mise exec -- uv sync
./fetch_models.sh                 # YOLOX の取得(SHA-256 確認)、キャリブレーション画像、*.dyn / *.int8 の作成
mise exec -- uv run pytest        # 単体テスト
./build.sh                        # → build/detector.zip(arm64)。SMOKE=1 で Lambda のベースイメージで import と推論を確認
```

`build.sh` は Lambda 用の manylinux wheel(onnxruntime、numpy、pillow、pyarrow)を入れ、`lambdas/common` と
`models/$MODELS.onnx`(既定 `yolox_tiny.int8`)を同梱する。boto3 はランタイムのものを使う。

### 環境変数

Terraform と合意済み: `STATE_TABLE`、`DETECTIONS_BUCKET`、`STREAM_NAME_PREFIX`、`LAG_SECONDS`、`MAX_MINUTES`、`MAX_ATTEMPTS`、
`MEDIA_RETENTION_DAYS`。以下は省略可能(既定値で動く):

| 変数 | 既定 | 内容 |
| --- | --- | --- |
| `DETECT_MODEL` | `yolox_tiny.int8` | `models/{DETECT_MODEL}.onnx`。zip に同梱したものから選ぶ |
| `DETECT_INPUT_WIDTH` | `416` | GetImages で取る幅 = 推論の入力幅。高さは 32 の倍数に切り上げ(416 → 416×256) |
| `DETECT_IMAGE_HEIGHT` | 幅 × 9/16 | GetImages で取る高さ(幅と高さは両方指定が必須)。16:9 以外のカメラで指定する |
| `DETECT_THREADS` | `0`(自動) | ONNX Runtime の intra-op スレッド数 |
| `DETECT_BATCH` | `1` | 1 回の推論にまとめるフレーム数 |
| `DETECT_SCORE_THRESHOLD` / `DETECT_NMS_THRESHOLD` | `0.3` / `0.45` | 信頼度の下限、NMS の IoU |
| `MAX_WAIT_SECONDS` | `600` | 断片が出そろうのを待つ上限(`kvs.minute_ready`、video_archiver と共通) |

## Parquet への追記(`parquet_store.py`)

1. その日のファイルを GetObject(なければ空)。ETag を覚える
2. `区間の開始 ≤ ts < 区間の終わり` の行を取り除き、新しい行を足して ts 順に並べる(再実行しても重複しない)
3. 既存なら `If-Match: ETag`、新規なら `If-None-Match: *` の条件付き PUT。412 / 409 なら読み直して最大 5 回やり直す
4. 検出が 0 件で、取り除く行もなければ PUT しない

ファイル名は区間の開始時刻(UTC)の日付なので、23:59 の区間は前日のファイル、00:00 の区間は翌日のファイルに入る。
列は docs/contracts.md のとおり(`ts` は timestamp[ms, UTC]、座標は 0〜1、`model` は `yolox_tiny_int8` のようにモデル名と量子化)。
zstd 圧縮。実 KVS の通しでは、人と車が多い交差点の映像で 1 分あたり約 1,000 件、3 分で 3,075 行・18 KB だった。

## ログ(1 行 1 JSON、`common.log`)

区間ごとに `detect_minute` を出す。主な項目: `frame_count`、`detect_count`、`image_errors`(GetImages の `MEDIA_ERROR` などの数)、
`fetch_ms`(ListFragments + GetImages)、`preprocess_ms`、`inference_ms`、`inference_ms_per_frame`、`postprocess_ms`、
`write_read_ms` / `write_put_ms` / `write_attempts`(Parquet の読み書き)、`parquet_rows`、`parquet_bytes`、`total_ms`、`max_rss_mb`。
コールドスタートで `model_loaded`(`load_ms`)、競合時に `parquet_conflict`。

## zip のサイズ

ZIP_PLACEHOLDER

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
