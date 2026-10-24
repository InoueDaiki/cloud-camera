#!/usr/bin/env bash
# 回線断試験: 実 KVS に送りながらエッジのコンテナから AWS への通信だけを遮断し、復旧後に遅れて届いた断片を調べる。
#
#   mise exec -- edge/scripts/tmp-aws.sh up        (TMP_PROJECT=cc-tmp-edgebuf を付ける)
#   mise exec -- edge/scripts/outage-test.sh up     # エミュレーター(1 台)+ エッジを起動し、シャドウで配信開始
#   mise exec -- edge/scripts/outage-test.sh run 5  # 5 分遮断 → 復旧 → 送り終わるまで待つ → 集計
#   mise exec -- edge/scripts/outage-test.sh down   # コンテナを止める
#   mise exec -- edge/scripts/tmp-aws.sh down
#
# 結果は edge/.out/outage/<開始時刻>-<分>m/(result.json、edge.log)。
# 遮断の仕組みは outage/netctl.sh(iptables で DROP。エミュレーターとの RTSP と DNS は通す)。
set -euo pipefail

P="${TMP_PROJECT:-cc-tmp-edgebuf}"
GW="${TMP_GATEWAY:-gw-001}"
CAM="${TMP_CAMERA:-cam-001}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/edge/scripts/outage"
STREAM="$P-$CAM"
THING="$P-$GW"
EDGE="cc-edgebuf-edge-1"
export AWS_PROFILE="${AWS_PROFILE:-cloud-camera}" AWS_REGION="${AWS_REGION:-ap-northeast-1}" AWS_PAGER=""

compose() {
  CC_PROJECT="$P" CC_GATEWAY_ID="$GW" docker compose -p cc-edgebuf \
    -f "$ROOT/compose.yaml" -f "$HERE/compose.yaml" "$@"
}

data_ep() { aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text; }

shadow_get() {
  local tmp; tmp="$(mktemp)"
  aws iot-data get-thing-shadow --thing-name "$THING" --endpoint-url "https://$(data_ep)" "$tmp" >/dev/null
  cat "$tmp"; rm -f "$tmp"
}

# 最新の status の cam を 1 行の JSON で
last_status() {
  docker logs --since "${1:-60s}" "$EDGE" 2>&1 | grep 'cc_edge.agent: status: ' | tail -1 \
    | sed 's/.*status: //' | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['cameras'].get('$CAM') or {}))" 2>/dev/null || echo '{}'
}

up() {
  local tmp; tmp="$(mktemp)"
  cat >"$tmp" <<JSON
{"state":{"desired":{"cameras":{"$CAM":{"onvif_url":"http://emulator:8080/onvif/device_service",
  "username":"admin","password":"admin","profile_token":null,"streaming":true}}}}}
JSON
  aws iot-data update-thing-shadow --thing-name "$THING" --endpoint-url "https://$(data_ep)" \
    --cli-binary-format raw-in-base64-out --payload "fileb://$tmp" /dev/null >/dev/null
  rm -f "$tmp"
  compose up -d --no-build
  echo "配信開始を待つ…"
  for _ in $(seq 1 60); do
    s="$(last_status 30s)"
    if python3 -c "import json,sys; d=json.loads(sys.argv[1]); b=d.get('buffer') or {}; sys.exit(0 if d.get('streaming') and (b.get('persisted_lag_s') or 99) < 10 else 1)" "$s"; then
      echo "streaming: $s"; return 0
    fi
    sleep 5
  done
  echo "配信が始まらない" >&2; docker logs --tail 50 "$EDGE" >&2; return 1
}

down() {
  "$HERE/netctl.sh" unblock "$EDGE" >/dev/null 2>&1 || true
  compose down -v
}

# run <分>        : <分> 遮断して復旧
# run <分,分,分…> : 遮断・開通・遮断…を繰り返す(例: 5,2,5 = 5 分遮断 → 2 分開通 → 5 分遮断)。
#                   集計の「遮断中」は最初の遮断から最後の復旧まで
run() {
  local pattern="${1:?分を指定}" drain_max="${2:-1500}"
  local out="$ROOT/edge/.out/outage/$(date -u +%Y%m%dT%H%M%SZ)-${pattern//,/_}m"
  mkdir -p "$out"
  sleep "${WARMUP_S:-60}"  # 遮断前に普通に送れている区間を作る(前回の試験の追いつきを終わらせる)
  local restarts_before; restarts_before="$(docker inspect "$EDGE" --format '{{.RestartCount}} {{.State.StartedAt}}')"
  echo "== 遮断前: $(last_status)"
  local t_block="" t_unblock t_end i=0 minutes end
  : >"$out/periods.txt"
  IFS=, read -r -a steps <<<"$pattern"
  for minutes in "${steps[@]}"; do
    local now; now="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
    if (( i % 2 == 0 )); then
      [[ -z "$t_block" ]] && t_block="$now"
      "$HERE/netctl.sh" block "$EDGE"
      echo "== $(date -u +%H:%M:%S) 遮断 ${minutes} 分"
      echo "block $now" >>"$out/periods.txt"
    else
      "$HERE/netctl.sh" unblock "$EDGE"
      echo "== $(date -u +%H:%M:%S) 開通 ${minutes} 分"
      echo "unblock $now" >>"$out/periods.txt"
    fi
    end=$(( ${now%.*} + minutes * 60 ))
    while (( $(date +%s) < end )); do
      sleep 30
      echo "   $(date -u +%H:%M:%S) cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' "$EDGE") $(last_status 40s)" | cut -c1-420
    done
    i=$(( i + 1 ))
  done
  t_unblock="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
  "$HERE/netctl.sh" unblock "$EDGE"
  echo "unblock $t_unblock" >>"$out/periods.txt"
  echo "== $(date -u +%H:%M:%S) 復旧。送り終わるまで待つ(最大 ${drain_max} 秒)"
  local deadline=$(( $(date +%s) + drain_max ))
  while (( $(date +%s) < deadline )); do
    sleep 10
    s="$(last_status 20s)"
    echo "   $(date -u +%H:%M:%S) cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' "$EDGE") $s" | cut -c1-420
    if python3 -c "import json,sys; b=json.loads(sys.argv[1]).get('buffer') or {}; sys.exit(0 if (b.get('persisted_lag_s') or 99) < 5 and (b.get('unsent_s') or 0) < 3 else 1)" "$s"; then
      break
    fi
  done
  sleep 30  # 最後の断片が ListFragments に見えるまで
  t_end="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
  docker logs --since "$(( ${t_block%.*} - 120 ))" "$EDGE" >"$out/edge.log" 2>&1
  {
    echo "restart_count_before: $restarts_before"
    echo "restart_count_after:  $(docker inspect "$EDGE" --format '{{.RestartCount}} {{.State.StartedAt}}')"
  } >"$out/container.txt"
  shadow_get >"$out/shadow.json" || true
  python3 "$HERE/report.py" --stream "$STREAM" --block "$t_block" --unblock "$t_unblock" --end "$t_end" \
    --log "$out/edge.log" --out "$out/result.json" >/dev/null
  python3 - "$out" "$t_unblock" <<'PY'
import json, sys, datetime as dt
out, t_unblock = sys.argv[1], float(sys.argv[2])
r = json.load(open(f"{out}/result.json"))
sh = json.load(open(f"{out}/shadow.json"))
rep = sh.get("state", {}).get("reported", {})
last_seen = rep.get("last_seen")
ok_seen = last_seen and dt.datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp() > t_unblock
print(json.dumps({k: r[k] for k in ("during_outage", "gap_total_s", "gaps", "drain_s", "block_to_drained_s",
                                     "max_delay_s", "order", "duplicates")}, ensure_ascii=False))
e = r.get("edge", {})
print(json.dumps({k: e.get(k) for k in ("pipeline_starts_in_log", "restarts_first", "restarts_last", "streaming_always",
                                        "mqtt_disconnects", "mqtt_connects", "max_unsent_s", "max_store_used_mb",
                                        "dropped_frames_last", "max_upload_handle", "sessions_last")}, ensure_ascii=False))
print("shadow reported: online=%s last_seen=%s (復旧後に更新: %s) streaming=%s" % (
    rep.get("online"), last_seen, bool(ok_seen), (rep.get("cameras") or {}).get("cam-001", {}).get("streaming")))
PY
  cat "$out/container.txt"
  echo "結果: $out/result.json"
}

"${1:-up}" "${@:2}"
