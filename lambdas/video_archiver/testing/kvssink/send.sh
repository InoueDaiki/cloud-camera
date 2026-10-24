#!/bin/bash
# テストパターン(時刻を焼き込み)を H.265(キーフレーム 1 秒、VBR 相当)で KVS に送り続ける。
# 環境変数: STREAM_NAME、AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN、AWS_DEFAULT_REGION
#           WIDTH / HEIGHT / FPS / BITRATE_KBPS(省略可)
set -euo pipefail
W=${WIDTH:-1280}
H=${HEIGHT:-720}
FPS=${FPS:-15}
KBPS=${BITRATE_KBPS:-1000}

exec gst-launch-1.0 -e \
  videotestsrc is-live=true pattern=ball ! "video/x-raw,width=${W},height=${H},framerate=${FPS}/1" \
  ! clockoverlay time-format="%Y-%m-%d %H:%M:%S UTC" font-desc="Sans 36" \
  ! videoconvert \
  ! x265enc speed-preset=ultrafast tune=zerolatency bitrate="${KBPS}" key-int-max="${FPS}" \
      option-string="keyint=${FPS}:min-keyint=${FPS}:scenecut=0:repeat-headers=1" \
  ! h265parse \
  ! video/x-h265,stream-format=hvc1,alignment=au \
  ! kvssink stream-name="${STREAM_NAME}" aws-region="${AWS_DEFAULT_REGION}" \
      access-key="${AWS_ACCESS_KEY_ID}" secret-key="${AWS_SECRET_ACCESS_KEY}" session-token="${AWS_SESSION_TOKEN:-}" \
      storage-size=128 restart-on-error=true
