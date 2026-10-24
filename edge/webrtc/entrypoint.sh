#!/bin/sh
# /certs(edge/.certs/<gateway_id>/ と同じ形式)から IoT 認証情報プロバイダーの設定を組み立てて master を起動する。
#
#   /certs/device.pem.crt  /certs/private.pem.key  /certs/config.json(scripts/provision-edge.sh が書き出すもの)
#
# 環境変数で上書きできるもの:
#   CC_CAMERA_ID       既定 cam-001。チャネル名の既定は {project}-{camera_id}
#   CC_WEBRTC_CHANNEL  チャネル名
#   CC_RTSP_URL        必須(例 rtsp://emulator:8554/cam1/sub)
#   CC_WEBRTC_CODEC    h264 / h265
#   CC_KVS_ROLE_ALIAS  既定は config.json の kvs_role_alias
set -eu

CERTS="${CC_CERTS_DIR:-/certs}"
CFG="$CERTS/config.json"
[ -f "$CFG" ] || { echo "$CFG がない(scripts/provision-edge.sh か scripts/webrtc/tmp-aws.sh up で作る)" >&2; exit 1; }

get() { jq -r "$1 // empty" "$CFG"; }

PROJECT="$(get .project)"
CAMERA="${CC_CAMERA_ID:-cam-001}"
export CC_WEBRTC_CHANNEL="${CC_WEBRTC_CHANNEL:-$PROJECT-$CAMERA}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$(get .region)}"
export AWS_IOT_CORE_CREDENTIAL_ENDPOINT="$(get .iot_credential_endpoint)"
export AWS_IOT_CORE_ROLE_ALIAS="${CC_KVS_ROLE_ALIAS:-$(get .kvs_role_alias)}"
export AWS_IOT_CORE_THING_NAME="$(get .thing_name)"
export AWS_IOT_CORE_CERT="$CERTS/device.pem.crt"
export AWS_IOT_CORE_PRIVATE_KEY="$CERTS/private.pem.key"
# Common.c は AWS_IOT_CORE_CERTIFICATE_ID があるとチャネル名を上書きするので消しておく
unset AWS_IOT_CORE_CERTIFICATE_ID || true

echo "channel=$CC_WEBRTC_CHANNEL thing=$AWS_IOT_CORE_THING_NAME role_alias=$AWS_IOT_CORE_ROLE_ALIAS rtsp=${CC_RTSP_URL:-} codec=${CC_WEBRTC_CODEC:-h264}"
exec /opt/kvs/bin/ccWebrtcMaster "$@"
