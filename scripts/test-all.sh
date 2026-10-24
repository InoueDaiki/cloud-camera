#!/usr/bin/env bash
# 全コンポーネントの単体テスト(と、ビューワーと Terraform の静的検査)をまとめて実行する。
# AWS には接続しない。最後に結果の表を出し、1 つでも失敗すれば終了コード 1。
#
# 使い方: scripts/test-all.sh [名前...]   例: scripts/test-all.sh api viewer
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if command -v mise >/dev/null 2>&1; then
  eval "$(mise env -s bash)"
fi

# 名前 → 実行するコマンド(ディレクトリはリポジトリ直下からの相対)
declare -a NAMES=(emulator edge common video_archiver detector api cost viewer infra)
cmd_for() {
  case "$1" in
    emulator)       echo "cd emulator && uv run pytest -q" ;;
    # edge は awsiotsdk を venv に入れて実行する(edge/README.md)
    edge)           echo "cd edge && { [[ -x .venv/bin/python ]] || { uv venv -q .venv && uv pip install -q --python .venv/bin/python awsiotsdk==1.31.0 pytest; }; } && .venv/bin/python -m pytest -q" ;;
    common)         echo "cd lambdas/common && uv run pytest -q" ;;
    video_archiver) echo "cd lambdas/video_archiver && uv run pytest -q" ;;
    # モデルがないと一部が skip される(lambdas/detector/fetch_models.sh で取得)
    detector)       echo "cd lambdas/detector && uv run pytest -q" ;;
    api)            echo "cd lambdas/api && uv run pytest -q" ;;
    cost)           echo "cd tools/cost && uv run pytest -q" ;;
    # package-lock.json が node_modules より新しければ入れ直す(依存が増えたとき)
    viewer)         echo "cd viewer && { [[ node_modules/.package-lock.json -nt package-lock.json ]] || npm ci --silent; } && npm run -s lint && npm run -s typecheck && npm run -s test" ;;
    infra)          echo "cd infra && terraform fmt -check -recursive && terraform init -backend=false -input=false >/dev/null && terraform validate -no-color" ;;
  esac
}

targets=("$@")
[[ ${#targets[@]} -eq 0 ]] && targets=("${NAMES[@]}")

LOG_DIR="$ROOT/build/test-logs"
mkdir -p "$LOG_DIR"
declare -a results=()
failed=0
for name in "${targets[@]}"; do
  cmd="$(cmd_for "$name")"
  if [[ -z "$cmd" ]]; then
    echo "不明な名前: $name(${NAMES[*]})" >&2
    exit 2
  fi
  echo "==> $name"
  start=$SECONDS
  if (eval "$cmd") >"$LOG_DIR/$name.log" 2>&1; then
    status="OK"
  else
    status="NG"
    failed=1
    tail -20 "$LOG_DIR/$name.log"
  fi
  # pytest / vitest の最後の要約行を拾う
  summary="$(grep -E '(passed|failed|skipped|Tests +[0-9]|Success!)' "$LOG_DIR/$name.log" | tail -1 | sed 's/\x1b\[[0-9;]*m//g')"
  results+=("$(printf '%-15s %-3s %4ss  %s' "$name" "$status" "$((SECONDS - start))" "$summary")")
done

echo
echo "結果(ログ: build/test-logs/)"
printf '%s\n' "${results[@]}"
exit $failed
