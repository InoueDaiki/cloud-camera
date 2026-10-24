#!/usr/bin/env bash
# KVS WebRTC の試作用の一時 AWS リソース(名前は cc-tmp-webrtc で始まる)を作る / 消す。
# edge/scripts/tmp-aws.sh と同じ形で、シグナリングチャネルとその権限を足したもの。
#
#   mise exec -- scripts/webrtc/tmp-aws.sh up     # 作成して edge/.certs/gw-001/ に証明書と config.json を書く
#   mise exec -- scripts/webrtc/tmp-aws.sh down   # すべて削除して、残りがないことを表示
#   mise exec -- scripts/webrtc/tmp-aws.sh check  # 残っているものを表示
#
# 作るもの:
#   IAM ロール + ロールエイリアス(IoT 認証情報プロバイダー用。KVS ストリームへの PutMedia と、
#   シグナリングチャネルの master の権限)、IoT ポリシー、Thing、証明書、
#   KVS ストリーム cc-tmp-webrtc-cam-001(保持 1 時間、録画の kvssink を同時に動かす計測用)、
#   シグナリングチャネル cc-tmp-webrtc-cam-001
set -euo pipefail

P="cc-tmp-webrtc"
GW="${TMP_GATEWAY:-gw-001}"
CAMS="${TMP_CAMERAS:-cam-001}"
REGION="${AWS_REGION:-ap-northeast-1}"
export AWS_PROFILE="${AWS_PROFILE:-cloud-camera}" AWS_REGION="$REGION" AWS_PAGER=""
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STATE="$ROOT/edge/.certs/.tmp-aws-$P.json"
THING="$P-$GW"
ROLE="$P-kvs-role"
ALIAS="$P-kvs-role-alias"
POLICY="$P-policy"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

up() {
  mkdir -p "$ROOT/edge/.certs"
  echo "== IAM ロール $ROLE"
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"credentials.iot.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  # 録画(kvssink)と同じロールに、シグナリングチャネルの master の権限を足す(本実装の提案と同じ形)
  aws iam put-role-policy --role-name "$ROLE" --policy-name kvs --policy-document "{
    \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"PutMedia\",\"Effect\":\"Allow\",
     \"Action\":[\"kinesisvideo:DescribeStream\",\"kinesisvideo:PutMedia\",\"kinesisvideo:GetDataEndpoint\",\"kinesisvideo:TagStream\"],
     \"Resource\":\"arn:aws:kinesisvideo:$REGION:$ACCOUNT:stream/$P-*/*\"},
    {\"Sid\":\"WebRtcMaster\",\"Effect\":\"Allow\",
     \"Action\":[\"kinesisvideo:DescribeSignalingChannel\",\"kinesisvideo:GetSignalingChannelEndpoint\",
                 \"kinesisvideo:GetIceServerConfig\",\"kinesisvideo:ConnectAsMaster\"],
     \"Resource\":\"arn:aws:kinesisvideo:$REGION:$ACCOUNT:channel/$P-*/*\"}]}"
  ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE"
  echo "== ロールエイリアス $ALIAS(IAM の伝播待ちで数回再試行)"
  for _ in 1 2 3 4 5 6; do
    if aws iot create-role-alias --role-alias "$ALIAS" --role-arn "$ROLE_ARN" \
        --credential-duration-seconds 3600 >/dev/null 2>&1; then break; fi
    sleep 5
  done
  ALIAS_ARN="$(aws iot describe-role-alias --role-alias "$ALIAS" --query roleAliasDescription.roleAliasArn --output text)"

  echo "== IoT ポリシー $POLICY"
  aws iot create-policy --policy-name "$POLICY" --policy-document "{
    \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":\"iot:Connect\",\"Resource\":\"arn:aws:iot:$REGION:$ACCOUNT:client/\${iot:Connection.Thing.ThingName}\"},
    {\"Effect\":\"Allow\",\"Action\":[\"iot:Publish\",\"iot:Receive\"],\"Resource\":[
      \"arn:aws:iot:$REGION:$ACCOUNT:topic/$P/$GW/*\",
      \"arn:aws:iot:$REGION:$ACCOUNT:topic/\$aws/things/\${iot:Connection.Thing.ThingName}/shadow/*\"]},
    {\"Effect\":\"Allow\",\"Action\":\"iot:Subscribe\",\"Resource\":[
      \"arn:aws:iot:$REGION:$ACCOUNT:topicfilter/$P/$GW/*\",
      \"arn:aws:iot:$REGION:$ACCOUNT:topicfilter/\$aws/things/\${iot:Connection.Thing.ThingName}/shadow/*\"]},
    {\"Effect\":\"Allow\",\"Action\":\"iot:AssumeRoleWithCertificate\",\"Resource\":\"$ALIAS_ARN\"}]}" >/dev/null

  echo "== Thing $THING と証明書"
  aws iot create-thing --thing-name "$THING" >/dev/null
  tmp="$(mktemp -d)"
  aws iot create-keys-and-certificate --set-as-active \
    --certificate-pem-outfile "$tmp/cert.pem" --private-key-outfile "$tmp/key.pem" >"$tmp/cert.json"
  CERT_ARN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["certificateArn"])' "$tmp/cert.json")"
  aws iot attach-policy --policy-name "$POLICY" --target "$CERT_ARN"
  aws iot attach-thing-principal --thing-name "$THING" --principal "$CERT_ARN"

  echo "== KVS ストリーム(保持 1 時間)とシグナリングチャネル"
  for c in $CAMS; do
    aws kinesisvideo create-stream --stream-name "$P-$c" --data-retention-in-hours 1 --media-type video/h265 >/dev/null
    aws kinesisvideo create-signaling-channel --channel-name "$P-$c" --channel-type SINGLE_MASTER \
      --single-master-configuration MessageTtlSeconds=60 >/dev/null
    aws kinesisvideo describe-signaling-channel --channel-name "$P-$c" \
      --query 'ChannelInfo.[ChannelName,ChannelType,ChannelStatus,ChannelARN]' --output text
  done

  DATA_EP="$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text)"
  CRED_EP="$(aws iot describe-endpoint --endpoint-type iot:CredentialProvider --query endpointAddress --output text)"
  python3 - "$tmp" "$P" "$GW" "$THING" "$DATA_EP" "$CRED_EP" "$ALIAS" "$CERT_ARN" "$CAMS" "$STATE" <<'PY'
import json, sys
from pathlib import Path
tmp, p, gw, thing, data_ep, cred_ep, alias, cert_arn, cams, state = sys.argv[1:]
v = lambda x: {"value": x}
tf = {
  "project": v(p), "iot_data_endpoint": v(data_ep), "iot_credential_endpoint": v(cred_ep),
  "kvs_role_alias": v(alias),
  "gateways": v({gw: {"thing_name": thing, "cameras": cams.split()}}),
  "gateway_certificates": {"sensitive": True, "value": {gw: {
      "certificate_pem": Path(tmp, "cert.pem").read_text(), "private_key": Path(tmp, "key.pem").read_text()}}},
}
Path(tmp, "tf.json").write_text(json.dumps(tf))
Path(state).write_text(json.dumps({"cert_arn": cert_arn, "thing": thing}))
PY
  # Terraform の出力と同じ形の JSON から provision-edge.sh で書き出す(edge/.certs/ の形式をそのまま使う)
  TF_OUTPUT_JSON="$tmp/tf.json" "$ROOT/scripts/provision-edge.sh" "$GW"
  rm -rf "$tmp"
}

down() {
  set +e
  if [[ -f "$STATE" ]]; then
    CERT_ARN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["cert_arn"])' "$STATE")"
  else
    CERT_ARN="$(aws iot list-thing-principals --thing-name "$THING" --query 'principals[0]' --output text 2>/dev/null)"
  fi
  if [[ -n "${CERT_ARN:-}" && "$CERT_ARN" != "None" ]]; then
    CERT_ID="${CERT_ARN##*/}"
    aws iot detach-thing-principal --thing-name "$THING" --principal "$CERT_ARN"
    aws iot detach-policy --policy-name "$POLICY" --target "$CERT_ARN"
    aws iot update-certificate --certificate-id "$CERT_ID" --new-status INACTIVE
    aws iot delete-certificate --certificate-id "$CERT_ID" --force-delete
  fi
  DATA_EP="$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text)"
  aws iot-data delete-thing-shadow --thing-name "$THING" --endpoint-url "https://$DATA_EP" /dev/null >/dev/null 2>&1
  aws iot delete-thing --thing-name "$THING"
  aws iot delete-policy --policy-name "$POLICY"
  aws iot delete-role-alias --role-alias "$ALIAS"
  aws iam delete-role-policy --role-name "$ROLE" --policy-name kvs
  aws iam delete-role --role-name "$ROLE"
  for arn in $(aws kinesisvideo list-streams --stream-name-condition "ComparisonOperator=BEGINS_WITH,ComparisonValue=$P-" \
                 --query 'StreamInfoList[].StreamARN' --output text); do
    aws kinesisvideo delete-stream --stream-arn "$arn"
  done
  for arn in $(aws kinesisvideo list-signaling-channels --channel-name-condition "ComparisonOperator=BEGINS_WITH,ComparisonValue=$P-" \
                 --query 'ChannelInfoList[].ChannelARN' --output text); do
    aws kinesisvideo delete-signaling-channel --channel-arn "$arn"
  done
  rm -f "$STATE"
  rm -rf "$ROOT/edge/.certs/$GW"
  set -e
  sleep 5
  check
}

check() {
  echo "== 残っている $P リソース(空なら削除済み。ストリームとチャネルは DELETING のまま数十秒残ることがある)"
  echo "-- things";        aws iot list-things --query "things[?starts_with(thingName,'$P')].thingName" --output text
  echo "-- policies";      aws iot list-policies --query "policies[?starts_with(policyName,'$P')].policyName" --output text
  echo "-- role aliases";  aws iot list-role-aliases --query "roleAliases[?starts_with(@,'$P')]" --output text
  echo "-- iam roles";     aws iam list-roles --query "Roles[?starts_with(RoleName,'$P')].RoleName" --output text
  echo "-- streams";       aws kinesisvideo list-streams --stream-name-condition "ComparisonOperator=BEGINS_WITH,ComparisonValue=$P-" \
                             --query 'StreamInfoList[].[StreamName,Status]' --output text
  echo "-- channels";      aws kinesisvideo list-signaling-channels --channel-name-condition "ComparisonOperator=BEGINS_WITH,ComparisonValue=$P-" \
                             --query 'ChannelInfoList[].[ChannelName,ChannelStatus]' --output text
}

"${1:-check}"
