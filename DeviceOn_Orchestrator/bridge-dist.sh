#!/usr/bin/env bash
# 純機械式腳本：把 DeviceOn_Frontend CVE 修補後的最佳結果 build 成 dist/，
# 同步進 DeviceOn_Backend 的 WebApp/src/main/webapp/，直接 commit + push 到
# MAIN_BRANCH_BACKEND。不做任何「需要判斷」的事，Codex Agent 不應該自己重新
# 用 git/npm/rsync 組這些指令，只需要照 SKILL.md 呼叫這支腳本並讀 RESULT=。
set -uo pipefail

ORCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${ORCH_DIR}/.." && pwd)"
FRONTEND_DIR="${WORKSPACE_ROOT}/DeviceOn_Frontend"
BACKEND_DIR="${WORKSPACE_ROOT}/DeviceOn_Backend"
ENV_FILE="${ORCH_DIR}/.env"
FRONTEND_ENV_FILE="${FRONTEND_DIR}/.env"

WEBAPP_TARGET_SUBDIR="WebApp/src/main/webapp"
# webapp/ 底下這些項目屬於後端自行維護（Java web 設定等），dist 同步時一律保留，
# 不被覆蓋、也不被當成「dist 已經不需要的舊檔」刪除。
PRESERVE_ENTRIES=("WEB-INF")

if [[ ! -f "$ENV_FILE" ]]; then
  echo "找不到 .env: ${ENV_FILE}（複製 .env.sample 為 .env 並填入實際值）" >&2
  exit 1
fi
set -a
source "$ENV_FILE"
set +a

required_vars=(AZURE_DEVOPS_PAT AZURE_DEVOPS_ORG AZURE_DEVOPS_PROJECT AZURE_DEVOPS_REPO_FRONTEND AZURE_DEVOPS_REPO_BACKEND MAIN_BRANCH_BACKEND)
missing=()
for v in "${required_vars[@]}"; do
  [[ -z "${!v:-}" ]] && missing+=("$v")
done
if (( ${#missing[@]} > 0 )); then
  echo "缺少必要的環境變數: ${missing[*]}" >&2
  exit 1
fi

for c in git jq npm node rsync; do
  command -v "$c" >/dev/null 2>&1 || { echo "找不到指令: ${c}" >&2; exit 1; }
done

FRONTEND_REPO_DIR="${FRONTEND_DIR}/${AZURE_DEVOPS_REPO_FRONTEND}"
BACKEND_REPO_DIR="${BACKEND_DIR}/${AZURE_DEVOPS_REPO_BACKEND}"

# 對齊 CI 用的 Node 版本（best-effort，沒裝 nvm 或沒裝 20.9.0 就跳過，不當成錯誤）。
align_node_version() {
  if [[ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]]; then
    # shellcheck disable=SC1091
    source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
    nvm use 20.9.0 >/dev/null 2>&1 || true
  fi
}

# 前端 MAIN_BRANCH 的定義只存在 DeviceOn_Frontend/.env，這裡故意不在自己的
# .env 重複一份，避免兩邊設定漂移；只用 grep 讀值，不直接 source 整個檔案
# （避免跟自己已經載入的變數互相污染）。
get_frontend_main_branch() {
  if [[ ! -f "$FRONTEND_ENV_FILE" ]]; then
    echo "找不到前端 .env: ${FRONTEND_ENV_FILE}" >&2
    return 1
  fi
  local v
  v="$(grep -E '^MAIN_BRANCH=' "$FRONTEND_ENV_FILE" | tail -1 | cut -d= -f2-)"
  if [[ -z "$v" ]]; then
    echo "無法從 ${FRONTEND_ENV_FILE} 讀到 MAIN_BRANCH" >&2
    return 1
  fi
  echo "$v"
}

# 決定「前端目前最好的 CVE 修補結果」在哪個 ref 上：
# - origin/last-good 還在、而且領先 origin/${MAIN_BRANCH_FRONTEND}：代表這一輪
#   修補還沒被自動合併（PR 還沒過、或分支政策還在跑），最好的結果在 last-good。
# - 否則（last-good 不存在，或存在但沒領先 MAIN_BRANCH）：代表已經合併進
#   MAIN_BRANCH 且 cve-loop.sh 已自動刪掉 last-good/nightly-build，或這次
#   session 從頭到尾都沒有任何 fixable CVE，最好的結果就是目前的 MAIN_BRANCH。
determine_best_ref() {
  if [[ ! -d "${FRONTEND_REPO_DIR}/.git" ]]; then
    echo "RESULT=ABORTED_FRONTEND_NOT_READY reason=repo_not_cloned frontend_repo_dir=${FRONTEND_REPO_DIR}"
    exit 1
  fi

  MAIN_BRANCH_FRONTEND="$(get_frontend_main_branch)" || exit 1

  git -C "$FRONTEND_REPO_DIR" fetch origin --prune --quiet

  BEST_REF=""
  SOURCE_LABEL=""
  PR_PENDING=false

  if git -C "$FRONTEND_REPO_DIR" show-ref --verify --quiet "refs/remotes/origin/last-good"; then
    local ahead
    ahead=$(git -C "$FRONTEND_REPO_DIR" rev-list --count "origin/${MAIN_BRANCH_FRONTEND}..origin/last-good")
    if (( ahead > 0 )); then
      BEST_REF="origin/last-good"
      SOURCE_LABEL="last-good"
      PR_PENDING=true
    fi
  fi

  if [[ -z "$BEST_REF" ]]; then
    BEST_REF="origin/${MAIN_BRANCH_FRONTEND}"
    SOURCE_LABEL="${MAIN_BRANCH_FRONTEND}"
    PR_PENDING=false
  fi

  BEST_COMMIT="$(git -C "$FRONTEND_REPO_DIR" rev-parse --short "$BEST_REF")"
}

# 從這次 frontend session 產出的 pr-summary.md 讀「修補了幾個 CVE」，純字串擷取，
# 讀不到就老實標 unknown，不用猜的湊數字。
read_fixed_count() {
  FIXED_COUNT="unknown"
  INITIAL_FIXABLE="unknown"
  local latest_summary
  latest_summary="$(ls -t "${FRONTEND_DIR}"/runs/*/pr-summary.md 2>/dev/null | head -1 || true)"
  SUMMARY_FILE="${latest_summary:-}"
  if [[ -n "$latest_summary" ]]; then
    FIXED_COUNT="$(grep -oP '已修復\s*\K[0-9]+(?=\s*個)' "$latest_summary" | head -1 || true)"
    INITIAL_FIXABLE="$(grep -oP '初始可修復 CVE[：:]\s*\K[0-9]+' "$latest_summary" | head -1 || true)"
    [[ -z "$FIXED_COUNT" ]] && FIXED_COUNT="unknown"
    [[ -z "$INITIAL_FIXABLE" ]] && INITIAL_FIXABLE="unknown"
  fi
}

build_dist() {
  align_node_version
  git -C "$FRONTEND_REPO_DIR" checkout -B bridge-dist-build "$BEST_REF" --quiet

  local build_log="${ORCH_DIR}/.bridge-build.log"
  if ! ( cd "$FRONTEND_REPO_DIR" && npm install --no-audit --no-fund && npm run build ) > "$build_log" 2>&1; then
    echo "RESULT=BUILD_FAILED reason=npm_build_failed log=${build_log} source_ref=${SOURCE_LABEL} source_commit=${BEST_COMMIT}"
    exit 2
  fi

  DIST_DIR="${FRONTEND_REPO_DIR}/dist"
  if [[ ! -d "$DIST_DIR" ]] || [[ -z "$(ls -A "$DIST_DIR" 2>/dev/null)" ]]; then
    echo "RESULT=BUILD_FAILED reason=empty_dist source_ref=${SOURCE_LABEL} source_commit=${BEST_COMMIT}"
    exit 2
  fi
}

# 確保後端 repo clone 存在、乾淨對齊 origin/${MAIN_BRANCH_BACKEND}。
# 若已經有 origin/last-good（代表 backend 的 cve-loop.sh 有一個 session 卡在
# 中途、還沒收尾），直接中止：這種情況如果硬 push 進 MAIN_BRANCH_BACKEND，
# backend 那個未收尾的 session 之後還是會繼續在舊的 last-good 上工作，
# 不會用到我們剛同步進去的 dist，需要人工先確認/收掉那個 session。
ensure_backend_ready() {
  if [[ ! -d "${BACKEND_REPO_DIR}/.git" ]]; then
    local repo_url="https://${AZURE_DEVOPS_PAT}@${AZURE_DEVOPS_ORG#https://}/${AZURE_DEVOPS_PROJECT}/_git/${AZURE_DEVOPS_REPO_BACKEND}"
    git clone --quiet "$repo_url" "$BACKEND_REPO_DIR"
  fi

  git -C "$BACKEND_REPO_DIR" fetch origin --prune --quiet

  if git -C "$BACKEND_REPO_DIR" show-ref --verify --quiet "refs/remotes/origin/last-good"; then
    echo "RESULT=ABORTED_STALE_LAST_GOOD reason=backend_last_good_exists backend_repo_dir=${BACKEND_REPO_DIR}"
    exit 1
  fi

  git -C "$BACKEND_REPO_DIR" checkout -B "${MAIN_BRANCH_BACKEND}" "origin/${MAIN_BRANCH_BACKEND}" --quiet
}

sync_and_commit() {
  local target_dir="${BACKEND_REPO_DIR}/${WEBAPP_TARGET_SUBDIR}"
  if [[ ! -d "$target_dir" ]]; then
    echo "找不到後端目標目錄: ${target_dir}" >&2
    exit 1
  fi

  local rsync_excludes=()
  local e
  for e in "${PRESERVE_ENTRIES[@]}"; do
    rsync_excludes+=(--exclude "/${e}")
  done

  rsync -a --delete "${rsync_excludes[@]}" "${DIST_DIR}/" "${target_dir}/"

  if [[ -z "$(git -C "$BACKEND_REPO_DIR" status --porcelain -- "$WEBAPP_TARGET_SUBDIR")" ]]; then
    echo "RESULT=DIST_NO_CHANGES source_ref=${SOURCE_LABEL} source_commit=${BEST_COMMIT}"
    exit 0
  fi

  read_fixed_count

  local commit_msg
  commit_msg="Sync frontend dist from automated CVE remediation

Source: DeviceOn_Frontend @ ${SOURCE_LABEL} (${BEST_COMMIT})
Fixed ${FIXED_COUNT} CVE(s) (of ${INITIAL_FIXABLE} initially fixable) via the frontend CVE auto-remediation skill.
Synced build output into ${WEBAPP_TARGET_SUBDIR}/ (WEB-INF preserved)."

  git -C "$BACKEND_REPO_DIR" add -A -- "$WEBAPP_TARGET_SUBDIR"
  git -C "$BACKEND_REPO_DIR" commit --quiet -m "$commit_msg"

  if ! git -C "$BACKEND_REPO_DIR" push origin "HEAD:${MAIN_BRANCH_BACKEND}"; then
    echo "RESULT=PUSH_FAILED reason=rejected_or_non_fast_forward branch=${MAIN_BRANCH_BACKEND}"
    exit 3
  fi

  local backend_commit
  backend_commit="$(git -C "$BACKEND_REPO_DIR" rev-parse --short HEAD)"
  echo "RESULT=DIST_SYNCED backend_commit=${backend_commit} source_ref=${SOURCE_LABEL} source_commit=${BEST_COMMIT} fixed=${FIXED_COUNT} initial_fixable=${INITIAL_FIXABLE} pr_pending=${PR_PENDING} summary=${SUMMARY_FILE:-none}"
}

cmd_status() {
  determine_best_ref
  echo "frontend_repo_dir=${FRONTEND_REPO_DIR}"
  echo "frontend_main_branch=${MAIN_BRANCH_FRONTEND}"
  echo "best_ref=${SOURCE_LABEL} (${BEST_COMMIT}) pr_pending=${PR_PENDING}"
  if [[ -d "${BACKEND_REPO_DIR}/.git" ]]; then
    git -C "$BACKEND_REPO_DIR" fetch origin --prune --quiet
    if git -C "$BACKEND_REPO_DIR" show-ref --verify --quiet "refs/remotes/origin/last-good"; then
      echo "backend_last_good=exists (可能有未收尾的 backend session)"
    else
      echo "backend_last_good=none"
    fi
  else
    echo "backend_repo_dir=${BACKEND_REPO_DIR} (尚未 clone)"
  fi
  echo "RESULT=STATUS_OK"
}

cmd_sync_dist() {
  determine_best_ref
  build_dist
  ensure_backend_ready
  sync_and_commit
}

main() {
  local action="${1:-}"
  case "$action" in
    sync-dist)
      cmd_sync_dist
      ;;
    status)
      cmd_status
      ;;
    *)
      echo "用法: $0 {sync-dist|status}" >&2
      exit 1
      ;;
  esac
}

main "$@"
