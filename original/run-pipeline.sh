#!/usr/bin/env bash
# 讀取 .env ->
#   若 workspace 下沒有專案：git clone repo，從 main 建立 nightly-build 分支並推送
#   若 workspace 下已有專案且已有 nightly-build 分支：直接從該分支推送到遠端
# -> 觸發 Azure Pipeline 對 nightly-build 執行 -> 等待完成 -> 下載該次執行的 artifacts。
set -euo pipefail

WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${WORKSPACE_DIR}/.env"
NIGHTLY_BRANCH="nightly-build"
POLL_INTERVAL_SECONDS="${PIPELINE_POLL_INTERVAL_SECONDS:-15}"
MAX_WAIT_SECONDS="${PIPELINE_MAX_WAIT_SECONDS:-3600}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "找不到 .env: ${ENV_FILE}" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

required_vars=(AZURE_DEVOPS_PAT AZURE_DEVOPS_ORG AZURE_DEVOPS_PROJECT AZURE_DEVOPS_REPO MAIN_BRANCH AZURE_PIPELINE_NAME)
missing=()
for v in "${required_vars[@]}"; do
  if [[ -z "${!v:-}" ]]; then
    missing+=("$v")
  fi
done
if (( ${#missing[@]} > 0 )); then
  echo "缺少必要的環境變數: ${missing[*]}" >&2
  cat >&2 <<'EOF'
請在 workspace/.env 中補上類似以下內容：

  AZURE_DEVOPS_PAT=xxxxxxxxxxxxxxxx
  AZURE_DEVOPS_ORG=https://dev.azure.com/wise-deviceon
  AZURE_DEVOPS_PROJECT=Sandbox
  AZURE_DEVOPS_REPO=fabianTest2
  MAIN_BRANCH=master
  AZURE_PIPELINE_NAME=fabianTest2-CI
EOF
  exit 1
fi

for cmd in git az jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "找不到指令: ${cmd}，請先安裝後再執行。" >&2
    exit 1
  fi
done

if ! az extension show --name azure-devops >/dev/null 2>&1; then
  echo "==> 安裝 az devops extension"
  az extension add --name azure-devops --only-show-errors
fi

# az devops/pipelines 指令認得這個環境變數，可免互動 az login 用 PAT 認證。
export AZURE_DEVOPS_EXT_PAT="$AZURE_DEVOPS_PAT"

ORG_HOST="${AZURE_DEVOPS_ORG#https://}"
REPO_URL="https://${AZURE_DEVOPS_PAT}@${ORG_HOST}/${AZURE_DEVOPS_PROJECT}/_git/${AZURE_DEVOPS_REPO}"
REPO_DIR="${WORKSPACE_DIR}/${AZURE_DEVOPS_REPO}"
ARTIFACTS_DIR="${WORKSPACE_DIR}/pipeline-artifacts"

echo "==> 1. 確認 workspace 下是否已有專案 (${AZURE_DEVOPS_REPO})"
if [[ -d "${REPO_DIR}/.git" ]]; then
  echo "repo 已存在於 ${REPO_DIR}"
  PROJECT_EXISTS=true
else
  echo "repo 不存在，視為全部重新來過，執行 git clone"
  git clone "$REPO_URL" "$REPO_DIR"
  PROJECT_EXISTS=false
fi

git -C "$REPO_DIR" fetch origin

echo "==> 2. 準備 ${NIGHTLY_BRANCH} 分支"
if [[ "$PROJECT_EXISTS" == true ]] && git -C "$REPO_DIR" show-ref --verify --quiet "refs/heads/${NIGHTLY_BRANCH}"; then
  echo "本地已有 ${NIGHTLY_BRANCH} 分支，直接使用該分支內容推送到遠端"
  git -C "$REPO_DIR" checkout "$NIGHTLY_BRANCH"
else
  echo "尚無 ${NIGHTLY_BRANCH} 分支，從 origin/${MAIN_BRANCH} 建立"
  git -C "$REPO_DIR" checkout -B "$NIGHTLY_BRANCH" "origin/${MAIN_BRANCH}"
fi

# nightly-build 是拋棄式分支，直接用 --force 覆蓋遠端即可，
# 不需要 --force-with-lease（該保護是為了共同維護的分支）。
git -C "$REPO_DIR" push --force -u origin "$NIGHTLY_BRANCH"

echo "==> 3. 觸發 pipeline「${AZURE_PIPELINE_NAME}」(branch: ${NIGHTLY_BRANCH})"
RUN_JSON=$(az pipelines run \
  --name "$AZURE_PIPELINE_NAME" \
  --branch "$NIGHTLY_BRANCH" \
  --organization "$AZURE_DEVOPS_ORG" \
  --project "$AZURE_DEVOPS_PROJECT" \
  --output json)

RUN_ID=$(echo "$RUN_JSON" | jq -r '.id // empty')
if [[ -z "$RUN_ID" ]]; then
  echo "觸發 pipeline 失敗，未取得 runId：" >&2
  echo "$RUN_JSON" >&2
  exit 1
fi
echo "runId = ${RUN_ID}"

echo "==> 4. 等待 pipeline 執行完成（最多 ${MAX_WAIT_SECONDS} 秒）"
elapsed=0
result="none"
while true; do
  RUN_STATUS_JSON=$(az pipelines runs show \
    --id "$RUN_ID" \
    --organization "$AZURE_DEVOPS_ORG" \
    --project "$AZURE_DEVOPS_PROJECT" \
    --output json)
  status=$(echo "$RUN_STATUS_JSON" | jq -r '.status')
  result=$(echo "$RUN_STATUS_JSON" | jq -r '.result')
  echo "  [$(date +%H:%M:%S)] status=${status} result=${result}"

  if [[ "$status" == "completed" ]]; then
    break
  fi
  if (( elapsed >= MAX_WAIT_SECONDS )); then
    echo "等待逾時（${MAX_WAIT_SECONDS} 秒），pipeline 仍在執行中，可自行到 Azure DevOps 查看 runId ${RUN_ID}" >&2
    exit 1
  fi
  sleep "$POLL_INTERVAL_SECONDS"
  elapsed=$(( elapsed + POLL_INTERVAL_SECONDS ))
done

if [[ "$result" != "succeeded" && "$result" != "succeededWithIssues" ]]; then
  echo "警告：pipeline 執行結果為 ${result}，仍嘗試取得已發布的 artifacts" >&2
fi

echo "==> 5. 下載 pipeline artifacts"
mkdir -p "${ARTIFACTS_DIR}/${RUN_ID}"
ARTIFACT_NAMES=$(az pipelines runs artifact list \
  --run-id "$RUN_ID" \
  --organization "$AZURE_DEVOPS_ORG" \
  --project "$AZURE_DEVOPS_PROJECT" \
  --output json | jq -r '.[].name')

if [[ -z "$ARTIFACT_NAMES" ]]; then
  echo "這次執行沒有任何已發布的 artifact"
else
  while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    echo "  下載 artifact: ${name}"
    az pipelines runs artifact download \
      --run-id "$RUN_ID" \
      --artifact-name "$name" \
      --path "${ARTIFACTS_DIR}/${RUN_ID}/${name}" \
      --organization "$AZURE_DEVOPS_ORG" \
      --project "$AZURE_DEVOPS_PROJECT"
  done <<< "$ARTIFACT_NAMES"
fi

echo "==> 完成"
echo "runId: ${RUN_ID}"
echo "artifacts 存放於: ${ARTIFACTS_DIR}/${RUN_ID}"
