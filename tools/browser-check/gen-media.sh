#!/usr/bin/env bash
# 確認ページ用の小さな fMP4(HEVC Main / Main10、比較用の H.264)を作る。
# ffmpeg は Docker の MediaMTX イメージ(libx265 / libx264 入り)を使う。
set -euo pipefail
cd "$(dirname "$0")/media"
IMG=bluenviron/mediamtx:1.21.1-ffmpeg
ff() { docker run --rm -v "$PWD:/w" -w /w --entrypoint ffmpeg "$IMG" -hide_banner -loglevel error -y "$@"; }
SRC=(-f lavfi -i "testsrc2=size=640x360:rate=15" -t 3)
# empty_moov + moof ごとの断片 = HLS の fMP4 と同じ形(init と media に分けずに 1 ファイルで MSE に入れる)
FRAG=(-movflags +frag_keyframe+empty_moov+default_base_moof -an)
X265="keyint=15:min-keyint=15:repeat-headers=1:log-level=error"
# カメラの main に近い 8bit Main。タグは hvc1(HLS / Safari が求める形)
ff "${SRC[@]}" -c:v libx265 -preset fast -pix_fmt yuv420p -x265-params "$X265" -tag:v hvc1 "${FRAG[@]}" hevc-main.mp4
ff "${SRC[@]}" -c:v libx265 -preset fast -pix_fmt yuv420p10le -x265-params "$X265" -tag:v hvc1 "${FRAG[@]}" hevc-main10.mp4
ff "${SRC[@]}" -c:v libx264 -preset fast -pix_fmt yuv420p -profile:v main -g 15 "${FRAG[@]}" h264-main.mp4
ls -l
