#!/usr/bin/env bash
# バケットを(旧バージョンと削除マーカーも含めて)空にしてから terraform destroy する。
#
# 使い方:
#   scripts/destroy.sh [PROJECT] [terraform destroy の追加引数...]
#
# 環境変数:
#   PROJECT         リソース名の接頭辞(既定 cloud-camera)。Terraform のワークスペース名
#   AUTO_APPROVE=1  確認を飛ばす
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -gt 0 && "$1" != -* ]]; then
  PROJECT="$1"
  shift
fi
PROJECT="${PROJECT:-cloud-camera}"

tf() { mise -C "$ROOT" exec -- terraform -chdir="$ROOT/infra" "$@"; }
aws_() { mise -C "$ROOT" exec -- aws "$@"; }

echo "==> project: $PROJECT"
if [[ "${AUTO_APPROVE:-0}" != "1" ]]; then
  read -r -p "$PROJECT のリソースとバケットの中身をすべて削除する。続けるなら project 名を入力: " answer
  [[ "$answer" == "$PROJECT" ]] || { echo "中止"; exit 1; }
fi

tf init -input=false >/dev/null
tf workspace select "$PROJECT" >/dev/null

# 先にスケジュールを消す(バケットを空にしている間に Lambda が書き込まないように)
# state にあるバケット名を読む
buckets="$(tf output -json 2>/dev/null | jq -r '[.media_bucket, .detections_bucket, .athena_bucket, .viewer_bucket, .artifacts_bucket] | .[] | select(. != null) | .value')"
# 途中で失敗した destroy の後は output が state から消えている。state に残っているバケットを読む
if [[ -z "$buckets" ]]; then
  buckets="$(tf state list 2>/dev/null | grep -E '^aws_s3_bucket\.this\[' | while read -r addr; do
    tf state show -no-color "$addr" | awk '$1 == "bucket" { gsub(/"/, "", $3); print $3; exit }'
  done)"
fi

if [[ -n "$buckets" ]]; then
  for group in $(aws_ scheduler list-schedule-groups --name-prefix "$PROJECT" --query 'ScheduleGroups[].Name' --output text); do
    [[ "$group" == "$PROJECT" ]] || continue
    for s in $(aws_ scheduler list-schedules --group-name "$group" --query 'Schedules[].Name' --output text); do
      aws_ scheduler delete-schedule --group-name "$group" --name "$s" || true
    done
  done
fi

empty_bucket() {
  local bucket="$1"
  aws_ s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 || { echo "  $bucket はない"; return; }
  echo "  $bucket を空にする"
  while true; do
    local batch
    batch="$(aws_ s3api list-object-versions --bucket "$bucket" --max-items 1000 --output json \
      | jq -c '{Objects: ([(.Versions // [])[], (.DeleteMarkers // [])[]] | map({Key, VersionId})), Quiet: true}')"
    [[ "$(jq '.Objects | length' <<<"$batch")" -eq 0 ]] && break
    aws_ s3api delete-objects --bucket "$bucket" --delete "$batch" >/dev/null
  done
}

echo "==> バケットを空にする"
for b in $buckets; do
  empty_bucket "$b"
done

echo "==> terraform destroy"
approve=()
[[ "${AUTO_APPROVE:-0}" == "1" ]] && approve=(-auto-approve)
# スケジュールを消す前に起動していた Lambda が、空にした後のバケットに書き込むことがある
# (video_archiver は断片が出そろうのを待ってから書くので、数十秒後に PUT する)。
# そのときは BucketNotEmpty で失敗するので、もう一度空にしてやり直す
if ! tf destroy -input=false ${approve[@]+"${approve[@]}"} -var "project=$PROJECT" "$@"; then
  echo "==> destroy に失敗した。バケットをもう一度空にしてやり直す"
  for b in $buckets; do
    empty_bucket "$b"
  done
  tf destroy -input=false ${approve[@]+"${approve[@]}"} -var "project=$PROJECT" "$@"
fi

echo "完了: $PROJECT を削除した"
