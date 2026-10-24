#!/usr/bin/env bash
# 全体を通す検証(E2E)。build → deploy → エッジ起動 → KVS / IoT / 退避 / 検出 / API / ブラウザ → 停止 → destroy。
# 各段階の合否を最後に表で出す。詳しくは docs/e2e.md。
#
# 使い方:
#   scripts/e2e/run.sh [PROJECT]            # 既定 cc-tmp-e2e
#
# 環境変数:
#   KEEP=1             最後に destroy しない(失敗しても残す)
#   DESTROY=0          KEEP=1 と同じ
#   SKIP_BUILD=1       ビルドを飛ばす(build/*.zip と viewer/dist を使う)
#   SKIP_DEPLOY=1      deploy を飛ばす(KEEP=1 で残した環境に対してチェックだけ回す)
#   DUMMY_MISSING=1    build/video_archiver.zip / build/detector.zip がなければ何もしないダミーを作る(該当チェックは SKIP)
#   BROWSERS=chrome    ブラウザの検証に使うもの(カンマ区切り。chrome / webkit / chromium。空で飛ばす)
#   ARCHIVE_TIMEOUT=480  S3 への退避を待つ秒数
#   DETECT_TIMEOUT=480   物体検出を待つ秒数
#   E2E_OUT=<dir>      結果とスクリーンショットの出力先(既定 ${TMPDIR}/cc-e2e/<PROJECT>-<時刻>。リポジトリ外)
#   HEADED=1           ブラウザを画面に出す
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# ビルドスクリプトは uv / node / terraform が PATH にある前提なので、mise の環境で実行し直す
if [[ -z "${CC_E2E_IN_MISE:-}" ]]; then
  CC_E2E_IN_MISE=1 exec mise -C "$ROOT" exec -- bash "$0" "$@"
fi
PROJECT="${1:-${PROJECT:-cc-tmp-e2e}}"
case "$PROJECT" in
  cloud-camera*) echo "project 名 $PROJECT は使わない(cloud-camera* は本番用)" >&2; exit 1 ;;
esac
KEEP="${KEEP:-0}"
[[ "${DESTROY:-1}" == "0" ]] && KEEP=1
BROWSERS="${BROWSERS-chrome}"
OUT="${E2E_OUT:-${TMPDIR:-/tmp}/cc-e2e/$PROJECT-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
export E2E_PROJECT="$PROJECT"
export E2E_RESULTS="$OUT/results.tsv"
export E2E_STATE="$OUT/state.json"
export E2E_TF_JSON="$OUT/tf.json"
: >"$E2E_RESULTS"
LOG="$OUT/run.log"
exec > >(tee -a "$LOG") 2>&1

COMPOSE=(docker compose -p "$PROJECT" -f "$ROOT/compose.yaml")
export CC_PROJECT="$PROJECT" CC_SINK=kvs CC_USE_SHADOW=true CC_GATEWAY_ID=gw-001

tf() { mise -C "$ROOT" exec -- terraform -chdir="$ROOT/infra" "$@"; }
py() { mise -C "$ROOT" exec -- uv run --quiet --no-project --with boto3 python "$ROOT/scripts/e2e/checks.py" "$@"; }

started=$(date +%s)
record() { # 段階 状態 秒 詳細
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >>"$E2E_RESULTS"
  echo "[$2] $1: $4"
}
# 外部コマンドの段階を実行して記録する
step() { # 名前 コマンド...
  local name="$1"; shift
  echo "==> $name"
  local t0=$(date +%s) rc
  "$@" >"$OUT/$name.log" 2>&1
  rc=$?
  tail -n 5 "$OUT/$name.log" | sed 's/^/  /'
  if [[ $rc -eq 0 ]]; then record "$name" PASS $(( $(date +%s) - t0 )) "ログ: $OUT/$name.log"
  else record "$name" FAIL $(( $(date +%s) - t0 )) "終了コード $rc(ログ: $OUT/$name.log)"; fi
  return $rc
}
check() { # 段階 [--timeout 秒]
  echo "==> $1"
  py "$@"
}

dummy_zip() { # 名前
  local name="$1" dir
  dir="$(mktemp -d)"
  cat >"$dir/handler.py" <<'PY'
# E2E 用のダミー(本物の Lambda がまだないときに deploy を通すため)。何もしない。
def lambda_handler(event, context):
    return {"dummy": True}
PY
  (cd "$dir" && zip -q "$ROOT/build/$name.zip" handler.py)
  rm -rf "$dir"
  touch "$ROOT/build/.dummy-$name"
}

build() {
  mkdir -p "$ROOT/build"
  # 前回ダミーを作ったものは消して作り直す(本物の build.sh が増えていれば本物になる)
  for n in video_archiver detector; do
    if [[ -f "$ROOT/build/.dummy-$n" ]]; then rm -f "$ROOT/build/$n.zip" "$ROOT/build/.dummy-$n"; fi
  done
  if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    # 物体検出のモデル(gitignore 済み)。用意済みなら何もしない
    if [[ -f "$ROOT/lambdas/detector/fetch_models.sh" ]]; then
      bash "$ROOT/lambdas/detector/fetch_models.sh" || return 1
    fi
    "$ROOT/scripts/build-all.sh" || return 1
  fi
  for n in video_archiver detector api; do
    [[ -f "$ROOT/build/$n.zip" ]] && continue
    if [[ "${DUMMY_MISSING:-0}" == "1" && "$n" != "api" ]]; then
      echo "build/$n.zip がないのでダミーを作る"
      dummy_zip "$n"
    else
      echo "build/$n.zip がない(DUMMY_MISSING=1 でダミーを作れる)" >&2
      return 1
    fi
  done
  [[ -f "$ROOT/viewer/dist/index.html" ]] || { echo "viewer/dist がない" >&2; return 1; }
}

deploy() {
  SKIP_BUILD=1 AUTO_APPROVE=1 "$ROOT/scripts/deploy.sh" "$PROJECT" || return 1
  tf output -json >"$E2E_TF_JSON"
}

provision() {
  tf workspace select "$PROJECT" >/dev/null || return 1
  TF_OUTPUT_JSON="$E2E_TF_JSON" mise -C "$ROOT" exec -- "$ROOT/scripts/provision-edge.sh" gw-001 || return 1
  # 出力先に秘密鍵を残さない(証明書は edge/.certs/gw-001/ にある)
  local tmp
  tmp="$(jq 'del(.gateway_certificates)' "$E2E_TF_JSON")" && printf '%s\n' "$tmp" >"$E2E_TF_JSON"
}

edge_up() {
  "${COMPOSE[@]}" build emulator || return 1
  # 物体検出で人や車が映るように、既定はサンプル動画(人の多い交差点)をループ再生する
  local src="${E2E_SOURCE:-file:/media/crosswalk_toronto.mp4}"
  if [[ "$src" == file:/media/* && ! -f "$ROOT/emulator/media/${src#file:/media/}" ]]; then
    echo "サンプル動画を取得する"
    docker run --rm -v "$ROOT/emulator/media:/media" cloud-camera/onvif-emulator:latest \
      python /app/scripts/fetch_sample_media.py --out /media || return 1
  fi
  echo "映像: $src"
  # edge の up でも emulator の設定が比べられるので、両方に渡す(片方だけだと作り直される)
  export EMULATOR_SOURCE="$src"
  "${COMPOSE[@]}" up -d --wait emulator && "${COMPOSE[@]}" up -d --build edge
}

edge_down() {
  "${COMPOSE[@]}" logs --no-color --timestamps edge >"$OUT/edge-container.log" 2>&1 || true
  "${COMPOSE[@]}" logs --no-color --timestamps emulator >"$OUT/emulator-container.log" 2>&1 || true
  "${COMPOSE[@]}" down --remove-orphans
}

browser_check() { # ブラウザ
  local b="$1" s3_at="" modes="live,kvs"
  if [[ ",$E2E_DUMMY," != *",video_archiver,"* ]]; then
    s3_at="$(jq -r '.s3_section // empty' "$E2E_STATE" 2>/dev/null)"
    if [[ -n "$s3_at" ]]; then
      # 区間の頭から 20 秒後
      s3_at=$(( $(date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$s3_at" +%s 2>/dev/null || date -u -d "$s3_at" +%s) * 1000 + 20000 ))
      modes="live,kvs,s3"
    fi
  fi
  echo "==> browser-$b ($modes)"
  local t0=$(date +%s) url
  url="$(jq -r '.cloudfront_url.value' "$E2E_TF_JSON")"
  local headed=()
  [[ "${HEADED:-0}" == "1" ]] && headed=(--headed)
  mise -C "$ROOT" exec -- node "$ROOT/scripts/e2e/browser.mjs" --url "$url" --out "$OUT/screenshots" \
    --browser "$b" --modes "$modes" ${s3_at:+--s3-at "$s3_at"} ${headed[@]+"${headed[@]}"} >"$OUT/browser-$b.log" 2>&1
  grep -E '^(ENV|CONSOLE)' "$OUT/browser-$b.log" | cut -c1-400 | sed 's/^/  /'
  local el=$(( $(date +%s) - t0 ))
  local got=0
  while IFS= read -r line; do
    got=1
    local j="${line#RESULT }" mode ok detail
    mode="$(jq -r .mode <<<"$j")"; ok="$(jq -r .ok <<<"$j")"
    detail="$(jq -r '"\(.engine // "-")、再生元=\(.chip // "-")、4 秒で \(.advanced // "-")s 進む、\(.videoWidth // 0)x\(.videoHeight // 0)、error=\(.error // "なし")、overlay=\(.overlay // "なし")、\(.screenshot)"' <<<"$j")"
    record "browser-$b-$mode" "$([[ $ok == true ]] && echo PASS || echo FAIL)" "$el" "$detail"
  done < <(grep '^RESULT ' "$OUT/browser-$b.log")
  [[ $got -eq 1 ]] || record "browser-$b" FAIL "$el" "結果なし(ログ: $OUT/browser-$b.log)"
  if [[ -z "$s3_at" ]]; then
    record "browser-$b-s3" SKIP 0 "S3 に退避された区間がない(video_archiver がダミーか archive が失敗)"
  fi
}

destroy() {
  AUTO_APPROVE=1 "$ROOT/scripts/destroy.sh" "$PROJECT" || return 1
  # 削除した環境の証明書を消す(別の project の証明書なら残す)
  local d="$ROOT/edge/.certs/gw-001"
  if [[ -f "$d/config.json" && "$(jq -r .project "$d/config.json")" == "$PROJECT" ]]; then
    rm -rf "$d"
    echo "$d を削除した"
  fi
}

print_table() {
  echo
  echo "== E2E 結果: $PROJECT(全体 $(( ($(date +%s) - started) / 60 )) 分)=="
  printf '%-22s %-5s %6s  %s\n' "段階" "結果" "秒" "詳細"
  while IFS=$'\t' read -r n s sec d; do
    printf '%-22s %-5s %6s  %s\n' "$n" "$s" "$sec" "$d"
  done <"$E2E_RESULTS"
  echo
  echo "出力: $OUT"
}

finish() {
  local rc=0
  grep -q $'\tFAIL\t' "$E2E_RESULTS" && rc=1
  print_table
  exit $rc
}

# ------------------------------------------------------------------ 本体
echo "==> project: ${PROJECT}、出力: $OUT"

deployed=0
if [[ "${SKIP_DEPLOY:-0}" == "1" ]]; then
  tf workspace select "$PROJECT" >/dev/null && tf output -json >"$E2E_TF_JSON" && deployed=1
else
  step build build || finish
  E2E_DUMMY="$(cd "$ROOT/build" && ls .dummy-* 2>/dev/null | sed 's/^\.dummy-//' | paste -sd, -)"
  step deploy deploy && deployed=1
fi
export E2E_DUMMY="${E2E_DUMMY:-$(cd "$ROOT/build" 2>/dev/null && ls .dummy-* 2>/dev/null | sed 's/^\.dummy-//' | paste -sd, -)}"
[[ -n "$E2E_DUMMY" ]] && echo "ダミーの Lambda: $E2E_DUMMY"

if [[ $deployed -eq 1 ]]; then
  # 残骸チェック用に証明書 ID を控える(表には出さない)
  E2E_RESULTS= py certs || true
  if step provision provision && step edge-up edge_up; then
    check shadow
    check kvs --timeout 150
    check cameras --timeout 120
    check api-kvs
    check archive --timeout "${ARCHIVE_TIMEOUT:-480}"
    check detect --timeout "${DETECT_TIMEOUT:-480}"
    check api-s3
    check search
    check cursors --timeout 60
    IFS=',' read -r -a bs <<<"$BROWSERS"
    for b in ${bs[@]+"${bs[@]}"}; do browser_check "$b"; done
    check stop --timeout 90
    # メトリクスは数分遅れるので最後のほうで見る
    check getimages --timeout 300
  fi
  step edge-down edge_down || true
fi

if [[ "$KEEP" == "1" ]]; then
  record destroy SKIP 0 "KEEP=1。後で scripts/destroy.sh $PROJECT"
elif [[ "${SKIP_DEPLOY:-0}" != "1" || "${DESTROY:-}" == "1" ]]; then
  step destroy destroy
  check residue --timeout 180
fi
finish
