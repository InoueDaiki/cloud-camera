#!/usr/bin/env bash
# lambdas/api を zip にする。出力: <リポジトリ直下>/build/api.zip
# boto3 は Lambda のランタイム同梱のものを使い、sqlglot(純 Python)だけ同梱する。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
out="$root/build/api.zip"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$root/build" "$work/pkg"

# 依存(uv.lock に固定した版)を arm64 / Python 3.13 用に入れる
(cd "$here" && uv export --frozen --no-dev --no-hashes --no-emit-project -o "$work/requirements.txt" >/dev/null)
uv pip install --quiet --target "$work/pkg" -r "$work/requirements.txt" \
  --python-platform aarch64-manylinux2014 --python-version 3.13 --only-binary :all:

cp "$here/handler.py" "$work/pkg/"
mkdir -p "$work/pkg/app"
cp "$here"/app/*.py "$work/pkg/app/"

rm -f "$work/pkg/.lock"
find "$work/pkg" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -f "$out"
(cd "$work/pkg" && zip -qr9 "$out" .)
echo "built $out ($(du -h "$out" | cut -f1))"
