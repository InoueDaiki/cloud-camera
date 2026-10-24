#!/usr/bin/env bash
# 視聴者を N 人つないだ状態で、エッジ(WebRTC master と kvssink)の CPU、メモリ、送信量を測る。
#
#   scripts/webrtc/measure/load.sh <視聴者数> <codec: h264|h265> [ラベル] [measure.mjs への追加引数...]
#
# 事前に edge/webrtc/compose.yaml のスタックと scripts/webrtc/serve.py を起動しておく。
set -euo pipefail
N="$1"; CODEC="$2"; LABEL="${3:-load-$CODEC-$N}"; shift 3 || shift $#
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$DIR/out}"
mkdir -p "$OUT"
pids=()
for i in $(seq 1 "$N"); do
  node "$DIR/measure.mjs" --codec "$CODEC" --shots 1 --hold 50 --label "$LABEL-v$i" --out "$OUT" "$@" >"$OUT/$LABEL-v$i.log" 2>&1 &
  pids+=("$!")
done
sleep 25   # 接続と最初のスクショを待つ
python3 "$DIR/docker_stats.py" --seconds 30 cc-webrtc-webrtc-1 cc-webrtc-edge-1 | tee "$OUT/$LABEL-docker.jsonl"
docker logs --since 30s cc-webrtc-webrtc-1 2>&1 | grep cc_stats | tail -2 | tee "$OUT/$LABEL-master.jsonl"
wait "${pids[@]}"
grep -h -E '"(local_candidate|remote_candidate|codec|fps|kbps)"' "$OUT"/"$LABEL"-v*.log | sort | uniq -c
