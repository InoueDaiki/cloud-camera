#!/usr/bin/env bash
# Terraform の出力から、エッジ用の証明書と設定を edge/.certs/<gateway_id>/ に書き出す。
#
#   scripts/provision-edge.sh                    # infra/ の terraform output を読む(全ゲートウェイ)
#   scripts/provision-edge.sh gw-001             # 指定したゲートウェイだけ
#   TF_OUTPUT_JSON=out.json scripts/provision-edge.sh   # terraform output -json の保存結果を使う
#   PROJECT=cc-tmp-e2e scripts/provision-edge.sh         # そのプロジェクトのワークスペースの output を読む
#                                                       # (deploy.sh は project 名と同じワークスペースを使う。既定 cloud-camera)
#
# 前提とする Terraform の output:
#   project, iot_data_endpoint, iot_credential_endpoint, kvs_role_alias, webrtc_channel_prefix(なければ {project}-),
#   gateways             = { gw-001 = { thing_name, cameras } }
#   gateway_certificates = { gw-001 = { certificate_pem, private_key } }(sensitive)
#
# 出力(edge/.certs/<gateway_id>/、gitignore 済み):
#   device.pem.crt  private.pem.key(0600)  AmazonRootCA1.pem  config.json
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# mise exec を付けずに呼ばれても、mise.toml の terraform を使う
if command -v mise >/dev/null 2>&1; then
  eval "$(mise -C "$ROOT" env -s bash)"
fi
OUT_BASE="${OUT_BASE:-$ROOT/edge/.certs}"
INFRA_DIR="${INFRA_DIR:-$ROOT/infra}"
ROOT_CA_URL="https://www.amazontrust.com/repository/AmazonRootCA1.pem"
REGION="${AWS_REGION:-ap-northeast-1}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [[ -n "${TF_OUTPUT_JSON:-}" ]]; then
  cp "$TF_OUTPUT_JSON" "$tmp/tf.json"
else
  # 今選ばれているワークスペースではなく、PROJECT のワークスペースを読む
  TF_WORKSPACE="${PROJECT:-cloud-camera}" terraform -chdir="$INFRA_DIR" output -json >"$tmp/tf.json"
fi

curl -fsSL "$ROOT_CA_URL" -o "$tmp/AmazonRootCA1.pem"
grep -q "BEGIN CERTIFICATE" "$tmp/AmazonRootCA1.pem"

python3 - "$tmp/tf.json" "$tmp/AmazonRootCA1.pem" "$OUT_BASE" "$REGION" "$@" <<'PY'
import json, os, sys, shutil
from pathlib import Path

tf_path, ca_path, out_base, region, *only = sys.argv[1:]
tf = json.loads(Path(tf_path).read_text())

def out(name):
    if name not in tf:
        sys.exit(f"Terraform の output {name} がない")
    return tf[name]["value"]

project = out("project")
gateways = out("gateways")
certs = out("gateway_certificates")
targets = only or sorted(gateways)
for gw in targets:
    if gw not in gateways or gw not in certs:
        sys.exit(f"ゲートウェイ {gw} が gateways / gateway_certificates にない")
    d = Path(out_base) / gw
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    (d / "device.pem.crt").write_text(certs[gw]["certificate_pem"])
    key = d / "private.pem.key"
    key.write_text(certs[gw]["private_key"])
    os.chmod(key, 0o600)
    shutil.copy(ca_path, d / "AmazonRootCA1.pem")
    cfg = {
        "project": project,
        "gateway_id": gw,
        "thing_name": gateways[gw]["thing_name"],
        "region": region,
        "iot_endpoint": out("iot_data_endpoint"),
        "iot_credential_endpoint": out("iot_credential_endpoint"),
        "kvs_role_alias": out("kvs_role_alias"),
        # KVS WebRTC のシグナリングチャネル名の接頭辞(チャネル名 = 接頭辞 + カメラ ID。master はこのロールエイリアスで接続する)
        "webrtc_channel_prefix": tf.get("webrtc_channel_prefix", {}).get("value", f"{project}-"),
        "use_shadow": True,
        "sink": "kvs",
        # 参考情報(実際の配信対象はシャドウの desired.cameras で決まる)
        "known_cameras": gateways[gw].get("cameras", []),
    }
    (d / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    print(f"{d}: thing={cfg['thing_name']} cameras={cfg['known_cameras']}")
PY
