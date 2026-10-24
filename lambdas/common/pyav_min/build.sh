#!/usr/bin/env bash
# 最小構成の PyAV(FFmpeg LGPL、HEVC / H.264 デコードだけ)を作り、指定のディレクトリ(zip のステージ)に入れる。
#   lambdas/common/pyav_min/build.sh <stage_dir>
# 結果: <stage_dir>/av/、<stage_dir>/av-*.dist-info/、<stage_dir>/lib/lib{avcodec,avformat,...}.so.N
# docker が要る(初回のビルドは数分。2 回目からはキャッシュが効く)。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STAGE="${1:?stage_dir を指定する}"
IMAGE=cloud-camera/pyav-min:latest
docker build --platform linux/arm64 -q -t "$IMAGE" "$HERE" >/dev/null
CID=$(docker create --platform linux/arm64 "$IMAGE")
trap 'docker rm "$CID" >/dev/null' EXIT
mkdir -p "$STAGE"
docker cp "$CID:/out/." "$STAGE/"
echo "pyav_min: $(du -sk "$STAGE/av" "$STAGE/lib" | awk '{s+=$1} END {printf "%.1f MB", s/1024}')"
