#!/usr/bin/env bash
# 最小構成の PyAV(FFmpeg LGPL、HEVC / H.264 デコードだけ)を作り、指定のディレクトリ(zip のステージ)に入れる。
#   lambdas/common/pyav_min/build.sh <stage_dir>
# 結果: <stage_dir>/av/、<stage_dir>/av-*.dist-info/、<stage_dir>/lib/lib{avcodec,avformat,...}.so.N
#
# docker が要る(初回のビルドは FFmpeg のコンパイルで数分)。毎回 FFmpeg をビルドしないよう、2 段でキャッシュする:
#   1. 出力(/out)を <リポジトリ>/build/.cache/pyav_min/<Dockerfile と build.sh のハッシュ>/ に置く。
#      あれば docker を使わずにコピーするだけ(docker のイメージやビルドキャッシュを消しても残る)
#   2. なければ docker build(レイヤーのキャッシュが効けば数秒)
# Dockerfile の版(FFMPEG_VERSION、AV_VERSION)や configure を変えるとハッシュが変わり、作り直す。
# PYAV_MIN_REBUILD=1 でキャッシュを無視して作り直す。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
STAGE="${1:?stage_dir を指定する}"
IMAGE=cloud-camera/pyav-min:latest

hash_of() {
  if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}
KEY="$(cat "$HERE/Dockerfile" "$HERE/build.sh" | hash_of | cut -c1-16)"
CACHE="${PYAV_MIN_CACHE:-$ROOT/build/.cache/pyav_min}/$KEY"

if [ "${PYAV_MIN_REBUILD:-0}" = 1 ] || [ ! -f "$CACHE/.complete" ]; then
  echo "pyav_min: キャッシュがないので docker でビルドする($CACHE)" >&2
  docker build --platform linux/arm64 -q -t "$IMAGE" "$HERE" >/dev/null
  CID=$(docker create --platform linux/arm64 "$IMAGE")
  trap 'docker rm "$CID" >/dev/null' EXIT
  rm -rf "$CACHE.tmp"
  mkdir -p "$CACHE.tmp"
  docker cp "$CID:/out/." "$CACHE.tmp/"
  touch "$CACHE.tmp/.complete"
  rm -rf "$CACHE"
  mv "$CACHE.tmp" "$CACHE"
fi

mkdir -p "$STAGE"
# .complete は stage に入れない
rsync -a --exclude .complete "$CACHE/" "$STAGE/"
echo "pyav_min: $(du -sk "$STAGE/av" "$STAGE/lib" | awk '{s+=$1} END {printf "%.1f MB", s/1024}')"
