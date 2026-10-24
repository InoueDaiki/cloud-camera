#!/usr/bin/env bash
# ビルド → terraform apply → ビューワーを S3 に同期 → CloudFront を無効化する。
#
# 使い方:
#   scripts/deploy.sh [PROJECT] [terraform apply の追加引数...]
#   PROJECT=cc-tmp-foo scripts/deploy.sh -var 'gateways={"gw-001"=["cam-001","cam-002"]}'
#
# 環境変数:
#   PROJECT       リソース名の接頭辞(既定 cloud-camera)。Terraform のワークスペース名にも使う
#   SKIP_BUILD=1  ビルドを飛ばす
#   AUTO_APPROVE=1  apply の確認を飛ばす
#   TF_VAR_*      Terraform の変数(例: TF_VAR_lambda_zip_dir)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 第 1 引数が - で始まらなければ PROJECT とみなす
if [[ $# -gt 0 && "$1" != -* ]]; then
  PROJECT="$1"
  shift
fi
PROJECT="${PROJECT:-cloud-camera}"

tf() { mise -C "$ROOT" exec -- terraform -chdir="$ROOT/infra" "$@"; }
aws_() { mise -C "$ROOT" exec -- aws "$@"; }

echo "==> project: $PROJECT"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  "$ROOT/scripts/build-all.sh"
fi

tf init -input=false >/dev/null
# state はプロジェクトごとにワークスペースで分ける(cloud-camera と cc-tmp-* が混ざらないように)
tf workspace select -or-create "$PROJECT" >/dev/null

approve=()
[[ "${AUTO_APPROVE:-0}" == "1" ]] && approve=(-auto-approve)
tf apply -input=false ${approve[@]+"${approve[@]}"} -var "project=$PROJECT" "$@"

viewer_bucket="$(tf output -raw viewer_bucket)"
distribution_id="$(tf output -raw cloudfront_distribution_id)"
cloudfront_url="$(tf output -raw cloudfront_url)"

if [[ -d "$ROOT/viewer/dist" ]]; then
  echo "==> viewer/dist → s3://$viewer_bucket"
  # ハッシュ付きのアセットは長くキャッシュ、index.html は毎回確認させる
  aws_ s3 sync "$ROOT/viewer/dist" "s3://$viewer_bucket" --delete \
    --exclude index.html --cache-control "public, max-age=31536000, immutable"
  aws_ s3 cp "$ROOT/viewer/dist/index.html" "s3://$viewer_bucket/index.html" \
    --cache-control "no-cache"
  echo "==> CloudFront の無効化"
  aws_ cloudfront create-invalidation --distribution-id "$distribution_id" --paths "/*" \
    --query 'Invalidation.Id' --output text
else
  echo "viewer/dist がないのでビューワーの同期は飛ばす"
fi

echo "完了: $cloudfront_url"
