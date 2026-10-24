#!/usr/bin/env bash
# Lambda の zip(build/<name>.zip)とビューワー(viewer/dist)をまとめてビルドする。
# 存在するものだけ実行する。
# video_archiver と detector は最小構成の PyAV(lambdas/common/pyav_min)を docker でビルドして入れるので docker が要る
# (初回のみ FFmpeg のコンパイルで数分。2 回目からは build/.cache/pyav_min のキャッシュを使い、docker を呼ばない)。
#
# 使い方: scripts/build-all.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# mise exec を付けずに呼ばれても、mise.toml のツール(uv、node など)を使えるようにする
if command -v mise >/dev/null 2>&1; then
  eval "$(mise env -s bash)"
fi
mkdir -p build

built=()
for script in lambdas/*/build.sh; do
  [[ -f "$script" ]] || continue
  name="$(basename "$(dirname "$script")")"
  echo "==> Lambda: $name"
  bash "$script"
  built+=("$name")
done

if [[ -f viewer/package.json ]]; then
  echo "==> viewer"
  (
    cd viewer
    if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
    npm run build
  )
  built+=("viewer")
fi

if [[ ${#built[@]} -eq 0 ]]; then
  echo "ビルド対象がない(lambdas/*/build.sh、viewer/package.json のどちらもない)" >&2
else
  echo "ビルド完了: ${built[*]}"
fi
ls -l build/*.zip 2>/dev/null || true
