#!/usr/bin/env bash
# Lambda の zip(build/<name>.zip)とビューワー(viewer/dist)をまとめてビルドする。
# 存在するものだけ実行する。
#
# 使い方: scripts/build-all.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
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
