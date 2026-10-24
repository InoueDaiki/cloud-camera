#!/usr/bin/env bash
# 物体検出 Lambda の zip を作る(コンテナイメージは使わない)。
#
#   lambdas/detector/build.sh                 # → build/detector.zip(arm64)
#   ARCH=x86_64 lambdas/detector/build.sh     # → build/detector-x86_64.zip
#   MODELS="yolox_tiny.int8 yolox_nano.dyn" lambdas/detector/build.sh   # 同梱するモデル
#   SMOKE=1 lambdas/detector/build.sh         # Lambda のベースイメージ(docker)で import と推論を確認
#
# 依存(onnxruntime、numpy、pillow、pyarrow)は Lambda 用の manylinux wheel を入れ、
# 使わないモジュール(pyarrow の Flight / S3、テスト、ヘッダーなど)を削り、strip して
# 展開後 250MB の上限に収める。boto3 は Lambda のランタイムのものを使う。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ARCH="${ARCH:-arm64}"
MODELS="${MODELS:-yolox_tiny.int8}"
case "$ARCH" in
  arm64) PLATFORM=aarch64-manylinux_2_28; OUT="$ROOT/build/detector.zip" ;;
  x86_64) PLATFORM=x86_64-manylinux_2_28; OUT="$ROOT/build/detector-x86_64.zip" ;;
  *) echo "ARCH は arm64 か x86_64" >&2; exit 1 ;;
esac
OUT="${OUT_ZIP:-$OUT}"
STAGE="$ROOT/build/detector-stage-$ARCH"

rm -rf "$STAGE"
mkdir -p "$STAGE" "$(dirname "$OUT")"

# 1. 依存(uv.lock の版に固定)
REQ="$STAGE.requirements.txt"
(cd "$HERE" && mise exec -- uv export --quiet --frozen --no-dev --no-hashes --no-emit-project -o "$REQ")
mise exec -- uv pip install --quiet --target "$STAGE" --python-platform "$PLATFORM" \
  --python-version 3.13 --only-binary :all: -r "$REQ"
rm -f "$REQ"
BEFORE=$(du -sk "$STAGE" | cut -f1)

# 2. 使わないものを削る
(
  cd "$STAGE"
  # 共通: テスト、キャッシュ、型スタブ、Cython のソース、C ヘッダー
  find . -type d \( -name tests -o -name __pycache__ \) -prune -exec rm -rf {} +
  find . -type f \( -name '*.pyi' -o -name '*.pyx' -o -name '*.pxd' -o -name '*.pxi' \) -delete
  rm -rf pyarrow/include pyarrow/src numpy/_core/include numpy/f2py numpy/_pyinstaller
  rm -rf bin

  # pyarrow: Parquet の読み書きと compute(filter / sort)だけ使う。
  # lib*.so はどれも substrait / dataset / acero に DT_NEEDED で依存しているので、その 3 つは残す
  rm -f pyarrow/libarrow_flight.so* pyarrow/libarrow_python_flight.so* pyarrow/_flight*.so pyarrow/flight.py
  rm -f pyarrow/libarrow_s3.so* pyarrow/_s3fs*.so
  rm -f pyarrow/libarrow_python_parquet_encryption.so* pyarrow/_parquet_encryption*.so \
        pyarrow/_dataset_parquet_encryption*.so pyarrow/_dataset_orc*.so pyarrow/_orc*.so pyarrow/orc.py \
        pyarrow/_hdfs*.so pyarrow/_gcsfs*.so pyarrow/_azurefs*.so pyarrow/_cuda*.so pyarrow/cuda.py \
        pyarrow/libarrow_cuda.so* pyarrow/libgandiva.so* pyarrow/_gandiva*.so pyarrow/gandiva.py \
        pyarrow/_pyarrow_cpp_tests*.so
  rm -rf pyarrow/interchange
  # wheel を展開するとシンボリックリンクが実体の複製になる。SONAME(*.so.NNNN)だけ残す
  for f in pyarrow/lib*.so pyarrow/lib*.so.*.*.*; do
    [ -e "$f" ] || continue
    case "$f" in *.cpython-*) continue ;; esac
    rm -f "$f"
  done

  # onnxruntime: 推論だけ使う(量子化・変換ツールは要らない)
  rm -rf onnxruntime/transformers onnxruntime/tools onnxruntime/quantization onnxruntime/datasets \
         onnxruntime/backend
  # onnxruntime の依存のうち実行時に使わないもの
  rm -rf google protobuf-*.dist-info sympy mpmath coloredlogs humanfriendly

  # Pillow: JPEG の読み込みとリサイズだけ使う
  rm -f PIL/_avif*.so PIL/_webp*.so PIL/_imagingft*.so PIL/_imagingcms*.so PIL/_imagingmath*.so \
        PIL/_imagingtk*.so
  rm -f pillow.libs/libavif* pillow.libs/libwebp* pillow.libs/libsharpyuv* pillow.libs/libfreetype* \
        pillow.libs/libharfbuzz* pillow.libs/libbrotli* pillow.libs/liblcms2*
)
# strip(docker があれば。STRIP=0 で省略)。pyarrow と OpenBLAS のデバッグ情報が大きい
DOCKER_PLATFORM="linux/$([ "$ARCH" = arm64 ] && echo arm64 || echo amd64)"
if [ "${STRIP:-1}" = 1 ] && command -v docker >/dev/null; then
  docker run --rm --platform "$DOCKER_PLATFORM" -v "$STAGE:/s" -v "$HERE/tools:/tools:ro" \
    public.ecr.aws/amazonlinux/amazonlinux:2023 sh /tools/strip_libs.sh
fi
AFTER_DEPS=$(du -sk "$STAGE" | cut -f1)

# 3. 本体、共通処理、モデル
cp "$HERE"/handler.py "$HERE"/detect.py "$HERE"/parquet_store.py "$STAGE/"
rsync -a --exclude tests --exclude .venv --exclude '__pycache__' --exclude pyproject.toml \
  --exclude uv.lock --exclude README.md "$ROOT/lambdas/common/" "$STAGE/common/"
mkdir -p "$STAGE/models"
for m in $MODELS; do
  f="$HERE/models/$m.onnx"
  if [ ! -f "$f" ]; then
    echo "モデルがない: $f(fetch_models.sh を実行する)" >&2
    exit 1
  fi
  cp "$f" "$STAGE/models/"
done
# ベンチマーク用の追加ファイル(tools/build_bench.sh から使う)
if [ -n "${EXTRA_DIR:-}" ]; then
  cp -R "$EXTRA_DIR"/. "$STAGE/"
fi

# 4. 確認(任意): Lambda のベースイメージで import と推論
if [ "${SMOKE:-0}" = 1 ]; then
  docker run --rm --platform "$DOCKER_PLATFORM" \
    -v "$STAGE:/var/task:ro" -v "$HERE/tests/fixtures:/fixtures:ro" -v "$HERE/tools:/tools:ro" \
    --entrypoint python3.13 "public.ecr.aws/lambda/python:3.13-$ARCH" /tools/smoke.py
fi

# 5. zip
rm -f "$OUT"
(cd "$STAGE" && zip -q -r -9 -X "$OUT" .)
TOTAL=$(du -sk "$STAGE" | cut -f1)
echo "依存(削る前): $((BEFORE / 1024)) MB"
echo "依存(削った後): $((AFTER_DEPS / 1024)) MB"
echo "展開後の合計: $((TOTAL / 1024)) MB(上限 250 MB)"
echo "zip: $OUT $(($(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT") / 1024 / 1024)) MB"
(cd "$STAGE" && du -sk -- * | sort -n | tail -8 | awk '{printf "  %6.1f MB  %s\n", $1/1024, $2}')
if [ "$TOTAL" -gt $((250 * 1024)) ]; then
  echo "展開後のサイズが 250 MB を超えている" >&2
  exit 1
fi
