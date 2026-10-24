#!/usr/bin/env bash
# エッジのコンテナから外(AWS)への通信だけを遮断する / 戻す。エミュレーター(同じ Docker ネットワーク)との RTSP は生かす。
#
#   edge/scripts/outage/netctl.sh block   [container]   # 遮断
#   edge/scripts/outage/netctl.sh unblock [container]   # 復旧
#   edge/scripts/outage/netctl.sh status  [container]   # 今のルール
#
# エッジのイメージには iptables を入れないので、iptables だけ入った小さなコンテナ(cc-edgebuf-netctl)を
# エッジと同じネットワーク名前空間(--network container:...)に NET_ADMIN 付きで入れてルールを書く。
# 遮断は DROP(RST を返さない)。実際の回線断(上流で黙って捨てられる)に近く、TCP はタイムアウトまで気づかない。
# DNS(Docker の 127.0.0.11)とループバック、同じ Docker ネットワークのサブネットは通す。
set -euo pipefail

CMD="${1:-status}"
CONTAINER="${2:-cc-edgebuf-edge-1}"
IMAGE="cc-edgebuf-netctl:1"
CHAIN="CC_OUTAGE"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -q -t "$IMAGE" - >/dev/null <<'DOCKERFILE'
FROM alpine:3.20
RUN apk add --no-cache iptables
DOCKERFILE
fi

# エッジがつながっている Docker ネットワークのサブネット(複数あれば全部)
subnets() {
  docker inspect "$CONTAINER" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
    | tr ' ' '\n' | grep -v '^$' | while read -r net; do
      docker network inspect "$net" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}'
    done | tr ' ' '\n' | grep -v '^$' | grep -v ':'
}

ipt() {
  docker run --rm --network "container:$CONTAINER" --cap-add NET_ADMIN "$IMAGE" sh -c "$1"
}

case "$CMD" in
  block)
    # 送信元はどちらの向きでも自分(サブネット内)なので、OUTPUT は宛先、INPUT は送信元で判定する
    rules="iptables -N ${CHAIN}_OUT 2>/dev/null || iptables -F ${CHAIN}_OUT;"
    rules+=" iptables -N ${CHAIN}_IN 2>/dev/null || iptables -F ${CHAIN}_IN;"
    rules+=" iptables -A ${CHAIN}_OUT -o lo -j RETURN; iptables -A ${CHAIN}_IN -i lo -j RETURN;"
    for s in $(subnets); do
      rules+=" iptables -A ${CHAIN}_OUT -d $s -j RETURN; iptables -A ${CHAIN}_IN -s $s -j RETURN;"
    done
    rules+=" iptables -A ${CHAIN}_OUT -j DROP; iptables -A ${CHAIN}_IN -j DROP;"
    rules+=" iptables -C OUTPUT -j ${CHAIN}_OUT 2>/dev/null || iptables -I OUTPUT -j ${CHAIN}_OUT;"
    rules+=" iptables -C INPUT -j ${CHAIN}_IN 2>/dev/null || iptables -I INPUT -j ${CHAIN}_IN;"
    ipt "$rules"
    echo "blocked $CONTAINER (allow: lo $(subnets | tr '\n' ' '))"
    ;;
  unblock)
    ipt "iptables -D OUTPUT -j ${CHAIN}_OUT 2>/dev/null; iptables -D INPUT -j ${CHAIN}_IN 2>/dev/null;
         for c in ${CHAIN}_OUT ${CHAIN}_IN $CHAIN; do iptables -F \$c 2>/dev/null; iptables -X \$c 2>/dev/null; done;
         iptables -D OUTPUT -j $CHAIN 2>/dev/null; iptables -D INPUT -j $CHAIN 2>/dev/null; true"
    echo "unblocked $CONTAINER"
    ;;
  status)
    ipt "iptables -S"
    ;;
  *)
    echo "usage: $0 block|unblock|status [container]" >&2
    exit 2
    ;;
esac
