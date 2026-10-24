#!/bin/bash
# H.265 を kvssink で KVS に送り続ける(動画退避 Lambda の検証用)。
#   RTSP_URL があれば: RTSP(エミュレーターの /cam{n}/main など)を再エンコードせずに中継する(エッジと同じ形)。
#     rtph265depay の出力は DTS がなく、そのままだと kvssink が 1 フレームも送らない(実測)。h265timestamper で DTS を付ける
#   なければ: テストパターン(時刻を焼き込み)を x265 でエンコードして送る
# 環境変数: STREAM_NAME、AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN、AWS_DEFAULT_REGION
#           WIDTH / HEIGHT / FPS / BITRATE_KBPS(テストパターンのとき、省略可)
set -euo pipefail
W=${WIDTH:-1280}
H=${HEIGHT:-720}
FPS=${FPS:-15}
KBPS=${BITRATE_KBPS:-1000}

SINK=(kvssink stream-name="${STREAM_NAME}" aws-region="${AWS_DEFAULT_REGION}"
  access-key="${AWS_ACCESS_KEY_ID}" secret-key="${AWS_SECRET_ACCESS_KEY}" session-token="${AWS_SESSION_TOKEN:-}"
  storage-size=128 restart-on-error=true)

if [ -n "${RTSP_URL:-}" ]; then
  exec gst-launch-1.0 -e \
    rtspsrc location="${RTSP_URL}" protocols=tcp latency=200 \
    ! rtph265depay ! h265parse config-interval=-1 ! h265timestamper \
    ! video/x-h265,stream-format=hvc1,alignment=au \
    ! "${SINK[@]}"
fi

exec gst-launch-1.0 -e \
  videotestsrc is-live=true pattern=ball ! "video/x-raw,width=${W},height=${H},framerate=${FPS}/1" \
  ! clockoverlay time-format="%Y-%m-%d %H:%M:%S UTC" font-desc="Sans 36" \
  ! videoconvert \
  ! x265enc speed-preset=ultrafast tune=zerolatency bitrate="${KBPS}" key-int-max="${FPS}" \
      option-string="keyint=${FPS}:min-keyint=${FPS}:scenecut=0:repeat-headers=1" \
  ! h265parse \
  ! video/x-h265,stream-format=hvc1,alignment=au \
  ! "${SINK[@]}"
