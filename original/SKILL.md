---
name: cve-auto-patch
description: Automated CVE scan-and-patch loop for the Backend (deviceon) Gradle project. Delegates the actual build and Trivy scan to the Azure Pipeline (via run-pipeline.sh) instead of running gradlew/trivy locally; the Codex Agent analyzes the pipeline's build-result artifact (trivy report on success, build log on failure) to drive AI auto-patching of build.gradle version pins/constraints. On a successful, verified-compilable build it pushes to the last-good branch, then keeps looping further patch rounds until no fixable CVEs remain or 10 rounds are exhausted. Use when asked to "auto-patch CVEs", "run the CVE pipeline", "update last-good", or otherwise automate vulnerability remediation for this repo.
---

# CVE 自動化修補流程

這個 skill 把 Backend (deviceon) 專案「觸發遠端建置/掃描 → 判讀結果 → 定位 →
修補 → 驗證 → 提交/回退」的 CVE 修補流程串成一個可重複執行的迴圈。

**建置與 Trivy 掃描一律交給 Azure Pipeline 執行**，本機不再跑
`./gradlew` 或 `trivy`。觸發 pipeline、等待完成、下載 artifacts 這幾步，
直接沿用 `run-pipeline.sh` 的邏輯（見下方「與 run-pipeline.sh 的分工」）。
Pipeline 執行完會回傳一次 artifact：**編譯成功時**內含 Trivy 掃描報告
（trivy 產出的 JSON），**編譯失敗時**內含建置失敗的 log。Codex Agent 負責
讀懂這份 artifact 的內容，據此判斷下一步該做「CVE 修補」還是「編譯錯誤
修補」。

## 0. 專案模組總覽

`settings.gradle` 定義了以下 Gradle project，注意「目錄名稱」與「Gradle
project 名稱」並不總是一致：

| Gradle project 路徑     | 對應目錄     | 產物                                   | 說明                     |
|--------------------------|--------------|----------------------------------------|--------------------------|
| `:RMMLib`                 | `RMMLib/`    | 內部函式庫 (jar)                       | 名稱未被 rename          |
| `:worker`                 | `Worker/`    | `worker-<版本>.jar` (fat jar)          | RMM Worker               |
| `:portal`                 | `WebApp/`    | `portal-<版本>.war`                    | Web 後台，套用 `war` plugin |
| `:ota-lib`                | `OTALib/`    | 內部函式庫 (jar)                       | OTA 用共用函式庫         |
| `:provisioning-worker`    | `OTAWorker/` | `provisioning-worker-<版本>.jar` (fat jar) | OTA Worker           |

版本號不是寫死在 `build.gradle`，而是 `buildSrc/src/main/groovy/project-rules.gradle`
在建置時動態決定：優先讀環境變數 `DEVICEON_TAG`，否則從根目錄
`CHANGELOG.md` 找 `# x.y.z (unreleased)` 這一行取版號。這個版號決定邏輯在
Azure Pipeline 端的建置也是同一套，本機不需要重現，只是修 `build.gradle`
時需要知道版號從哪來。

所有第三方套件版本統一定義在根目錄 `build.gradle` 的 `ext { ver_... = '...' }`
區塊（依 `ver_{Group}_{Module}` 命名），各模組的 `dependencies {}` 再用
`${ver_xxx}` 引用。**這代表大部分 CVE 修補的最終落點就是改這裡的版號**，除非
問題套件是透過某個上游函式庫「間接」帶入的舊版本（見下方 6.2 節）。

## 前置準備

### Azure DevOps 認證與環境變數

- **PAT 不要寫死在任何檔案裡**（包含這份 SKILL.md），也不要印出它的值。
  從 `~/workspace/.env` 讀取（該檔案不進版控）：

  ```sh
  set -a
  source ~/workspace/.env
  set +a
  ```

- `.env` 需要的變數與 `run-pipeline.sh` 要求的完全一致，不要再另外
  硬編任何 org/project/repo 名稱：

  ```
  AZURE_DEVOPS_PAT=xxxxxxxxxxxxxxxx
  AZURE_DEVOPS_ORG=https://dev.azure.com/wise-deviceon
  AZURE_DEVOPS_PROJECT=Sandbox
  AZURE_DEVOPS_REPO=fabianTest2
  MAIN_BRANCH=master
  AZURE_PIPELINE_NAME=fabianTest2
  ```

  若使用者直接把 PAT 貼在對話裡，先提醒他們之後應該撤銷/輪替這組 PAT，
  再照樣寫進 `.env` 使用，不要落地成其他明文檔案。

- 本機 repo 路徑固定在 `~/workspace/${AZURE_DEVOPS_REPO}`（與
  `run-pipeline.sh` 的 `REPO_DIR` 一致），修 `build.gradle` 都在
  `~/workspace/${AZURE_DEVOPS_REPO}/Backend` 下進行。

### 與 run-pipeline.sh 的分工

`run-pipeline.sh` 已經實作好「push 分支 → 觸發 pipeline → 等待完成 →
下載 artifacts」這一段，這個 skill 直接呼叫它，不重複實作：

- 呼叫前，這個 skill 自己先用 git 把本機 `nightly-build` 分支準備成
  「從 `last-good` 切出來」的狀態（見下方分支策略）。因為
  `run-pipeline.sh` 的邏輯是：**如果本機已經有 `nightly-build` 分支就直接
  把它現在的內容 push 上去**（不會另外重新從 `MAIN_BRANCH` 建立），所以
  只要先在本機把 `nightly-build` 對齊 `last-good`，`run-pipeline.sh` 就會
  照樣 push、觸發、等待、下載。
- 呼叫方式：
  ```sh
  cd ~/workspace
  ./run-pipeline.sh 2>&1 | tee /tmp/run-pipeline-<attempt>.log
  ```
- 從輸出裡取得這次執行的關鍵資訊：
  - `runId = <ID>`：這次 pipeline run 的 ID。
  - 最後一行 `status=completed result=<result>`：`result` 為
    `succeeded` / `succeededWithIssues` 視為編譯成功；其餘（`failed`、
    `canceled` 等）視為編譯失敗。
  - `artifacts 存放於: <ARTIFACTS_DIR>/<runId>`：下載回來的 artifact 目錄。
- 若要用程式化方式再次確認 result（例如 log 被截斷），可直接補一次查詢：
  ```sh
  az pipelines runs show --id "$RUN_ID" \
    --organization "$AZURE_DEVOPS_ORG" --project "$AZURE_DEVOPS_PROJECT" \
    --output json | jq -r '.result'
  ```

> **待確認事項**：目前假設 pipeline 在成功時發布的 artifact 內含 Trivy
> 掃描出的 JSON 報告（結構等同原本本機 `trivy rootfs ... --format json`
> 的輸出），失敗時發布的 artifact 內含 gradle 建置 log。若實際 artifact
> 名稱/內容跟這個假設不同，第一次跑的時候先用
> `find "<ARTIFACTS_DIR>/<runId>" -type f` 看清楚目錄結構，再調整下方
> 「判讀 artifact」步驟裡找檔案的方式（例如用 `find ... -name '*.json'`
> 抓 Trivy 報告、或用 `grep -l 'BUILD FAILED'` 抓建置失敗 log）。

## 分支策略

- `last-good`：目前已知「編譯成功且已修補」的基準分支。只透過 Azure
  Pipeline 驗證過編譯成功之後才會被更新。
- `nightly-build`：每一輪流程開始時，從 `last-good` 重新切出來的工作
  分支，所有修補（CVE 版號或建置錯誤修正）都在這裡進行，不直接改
  `last-good`。
- `last-good` 不做 `git push --force`；只透過 `--ff-only` merge 推進，
  確保它永遠代表「已知可編譯成功」的狀態。`nightly-build` 是拋棄式分支，
  `run-pipeline.sh` 對它用 `--force` push 是預期行為。

## 執行步驟

整體迴圈分兩層：**外層「CVE 修補輪」最多 10 輪**（或 fixable CVE 數降到
0 就提早停止）；每一輪外層迴圈裡，**內層「編譯修復重試」最多 3 次**
（超過就放棄這一輪、回退到 last-good、回報使用者）。

### 1. 取得 / 更新本機 repo

```sh
cd ~/workspace
if [[ -d "${AZURE_DEVOPS_REPO}/.git" ]]; then
  git -C "${AZURE_DEVOPS_REPO}" fetch origin
else
  git clone "https://${AZURE_DEVOPS_PAT}@${AZURE_DEVOPS_ORG#https://}/${AZURE_DEVOPS_PROJECT}/_git/${AZURE_DEVOPS_REPO}"
fi
cd "${AZURE_DEVOPS_REPO}"
```

### 2. 確保 last-good 分支存在

對應使用者需求的第 2 點：沒有 `last-good` 就從 `MAIN_BRANCH` 拉一份建立。

```sh
git fetch origin
if git show-ref --verify --quiet refs/remotes/origin/last-good; then
  git checkout -B last-good origin/last-good
else
  echo "沒有 last-good，從 origin/${MAIN_BRANCH} 建立"
  git checkout -B last-good "origin/${MAIN_BRANCH}"
  git push -u origin last-good
fi
```

> 建立/推送新的 `last-good` 分支屬於會影響遠端共享狀態的動作，執行
> `git push` 前留意確認。

### 3. 外層迴圈：CVE 修補輪（最多 10 輪，或 fixable == 0 提早停止）

每一輪外層迴圈開始，先把 `nightly-build` 對齊當下最新的 `last-good`：

```sh
git checkout last-good
git checkout -B nightly-build last-good
```

#### 3.1 內層迴圈：觸發建置並確保可編譯（最多 3 次嘗試）

```
build_attempt = 1
while build_attempt <= 3:
    在 ~/workspace 執行 ./run-pipeline.sh，push 現在的 nightly-build 並觸發 pipeline
    讀取這次的 runId、result、artifacts 目錄

    if result 為 succeeded / succeededWithIssues:
        BUILD_OK = true
        break

    # 編譯失敗：分析失敗 log，AI 修補後重試
    從 artifacts 目錄找出建置失敗 log
    Codex Agent 分析 log 找出根因（常見情況：CVE 版號升級導致 API 不相容，
    見 6.2 節間接依賴的情況；或版號本身寫錯）
    在 nightly-build 上修改對應的 build.gradle / ext{} 版號
    git add -A && git commit -m "fix: resolve build failure after dependency bump"
    build_attempt += 1

if build_attempt > 3 and not BUILD_OK:
    # 連續 3 次都編譯失敗，放棄這一輪
    git checkout last-good
    git branch -D nightly-build
    回報使用者：這一輪嘗試的修補、最後一次的錯誤 log、建議改為人工介入
    結束整個流程
```

#### 3.2 編譯成功 → 先推進 last-good（保留可編譯存檔）

```sh
git checkout last-good
git merge --ff-only nightly-build     # nightly-build 是從 last-good 切出來的，應可 fast-forward
git push origin last-good
```

> 這一步只要編譯成功就先做，不等 CVE 是否修完——目的是隨時保留一個
> 「已知可編譯」的存檔點，即使後面的 CVE 修補中斷，`last-good` 也不會
> 倒退。

#### 3.3 判讀成功 artifact 裡的 Trivy 報告

從這次成功建置的 artifacts 目錄裡取出 Trivy 報告（實際檔名依前述
「待確認事項」調整），計算並印出 fixable / unfixable 數量：

```sh
TRIVY_JSON="$(find "<ARTIFACTS_DIR>/<runId>" -type f -name '*.json' | head -n1)"
fixable=$(jq '[.Results[]?.Vulnerabilities[]? | select(.FixedVersion != null and .FixedVersion != "")] | length' "$TRIVY_JSON")
unfixable=$(jq '[.Results[]?.Vulnerabilities[]? | select(.FixedVersion == null or .FixedVersion == "")] | length' "$TRIVY_JSON")
echo "Fixable CVEs: ${fixable} / Unfixable CVEs: ${unfixable}"
```

依 fixable 數字決定是否停止（對應使用者需求的第 3 點）：

- **fixable == 0**：不論 `unfixable` 是否為 0（unfixable 代表目前無
  `FixedVersion` 可用，升版無法解決），自動修補視為完成，結束整個流程，
  回報最終結果。
- **fixable > 0** 且外層輪數已達 10 輪：停止並回報使用者目前狀態
  （已完成輪數、仍剩多少 fixable CVE、last-good 目前狀態），交由人工
  接手。
- **fixable > 0** 且輪數未達上限：進入下一節，在 `nightly-build` 上修補
  CVE，修完後回到本節「3.」開頭，開始下一輪外層迴圈（會重新對齊最新的
  `last-good` 並再次觸發 pipeline 驗證）。

### 4. 修補找到的 CVE

Codex Agent 對 Trivy 報告裡每個有 `FixedVersion` 的項目進行修補。

#### 4.1 用 `dependencyInsight` 定位

拿到 Trivy 回報的 `group:artifact:version` 後，用 `dependencyInsight` 確認
這個版本是「哪個模組」「透過哪條依賴鏈」被選中的（這一步仍然需要本機
Gradle 環境，因為只是查詢依賴樹，不需要真的建置產物）：

```sh
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

./gradlew -q --console=plain :<project>:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency <group>:<artifact>[:<version>]
```

`<project>` 依第 0 節的對照表替換，例如：

```sh
./gradlew -q --console=plain :RMMLib:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency com.fasterxml.jackson.core:jackson-databind:2.18.6

./gradlew -q --console=plain :worker:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency com.fasterxml.jackson.core:jackson-databind:2.18.6

./gradlew -q --console=plain :portal:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency com.fasterxml.jackson.core:jackson-databind:2.18.6

./gradlew -q --console=plain :ota-lib:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency com.fasterxml.jackson.core:jackson-databind:2.18.6

./gradlew -q --console=plain :provisioning-worker:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency com.fasterxml.jackson.core:jackson-databind:2.18.6
```

因為每個模組的相依樹不同，同一個套件在不同模組可能被解析成不同版本，
對「該套件出現過的每個模組」都各跑一次。

#### 4.2 解讀輸出、判斷修補位置

`dependencyInsight` 會輸出：

- **Selected/Requested reason**：這個版本是被誰要求的、又是被誰選中的
  （例如 conflict resolution：`By conflict resolution: between versions X and Y`）。
- **依賴路徑樹**：一路往上列出是哪一條 `implementation project(...)` /
  `api "group:artifact:version"` 把它拉進來的，最上層會停在某個
  `build.gradle` 裡實際寫死版號的那一行。

依輸出結果分兩種情況修補：

1. **直接依賴**（某模組的 `build.gradle` 直接寫了這個 artifact）：
   到根目錄 `build.gradle` 的 `ext { }` 區塊，找到對應的
   `ver_{Group}_{Module}` 變數（例如
   `ver_ComFasterxmlJacksonCore_JacksonDatabind`），升級到 Trivy 回報的
   `FixedVersion` 即可。
2. **間接依賴**（依賴路徑樹最上層是別的 SDK，例如
   `aws-java-sdk-s3`、`azure-messaging-eventhubs` 等）：
   單改根目錄 `ver_*` 版號不一定生效，因為 Gradle 的版本衝突解決策略
   （預設取最高版本）可能仍選到 SDK 內建的版本。這種情況要在**該模組**的
   `build.gradle` 明確加上約束：

   ```groovy
   dependencies {
       constraints {
           implementation("com.fasterxml.jackson.core:jackson-databind:${ver_ComFasterxmlJacksonCore_JacksonDatabind}") {
               because 'CVE-XXXX-YYYYY: 強制對齊安全版本，覆蓋上游 SDK 帶入的舊版'
           }
       }
   }
   ```

   或視情況升級帶入舊版本的那個上游 SDK 本身版號（往往是更根本的解法）。

一次只處理完一批可判斷的 CVE，避免一次改太多讓後續驗證失敗時難以定位問題。
修完後 commit：

```sh
git add -A
git commit -m "fix: patch CVEs found by trivy scan"
```

接著回到「3. 外層迴圈」開頭，開始下一輪（重新對齊 `last-good`、重新觸發
pipeline 驗證這批修補是否能編譯、再次檢查 fixable 數量）。

## 常見問題

- **`dependencyInsight` 找不到指定 dependency**：代表該版本在該模組的
  `runtimeClasspath` 上根本沒被解析到，換一個模組再試，或去掉版號只留
  `--dependency <group>:<artifact>` 看實際被解析成哪個版本。
- **改了 `ver_*` 版號但 `dependencyInsight` 顯示的版本沒變**：多半是被其他
  上游 SDK 用更高優先權帶入（conflict resolution 選了別的版本），照
  4.2 節第 2 種情況加 `constraints` 或升級該上游 SDK。
- **`run-pipeline.sh` 等待逾時**：預設 `PIPELINE_MAX_WAIT_SECONDS=3600`，
  可用環境變數覆蓋；逾時不代表失敗，去 Azure DevOps 上用印出的 `runId`
  查看實際狀態。
- **原本本機 Trivy 掃描已知的 `fatJar` 排除 `META-INF/maven/**`
  導致漏判問題**：現在掃描在 pipeline 端執行，若 pipeline 的掃描方式與
  舊本機流程不同，這個限制是否仍適用要以 pipeline 實際設定為準，不再由
  這個 skill 本機重現交叉驗證。

## 安全與確認事項

- PAT 只存在 `~/workspace/.env`（不進版控）與由它載入的環境變數，絕不寫入任何
  被 commit 的檔案（`build.gradle`、Trivy 報告、本 SKILL.md、log 檔等）。
- 不對 `last-good` 做 `git push --force`；`last-good` 只透過 `--ff-only` merge
  推進，確保它永遠代表「已知可編譯成功」的狀態。`nightly-build` 每輪都由
  `run-pipeline.sh` 對遠端 `--force` push，這是預期行為（拋棄式分支）。
- 修補版號只依 Trivy 回報的 `FixedVersion` 升級，不做超出 CVE 修補範圍的重構
  或版本跳號；建置失敗時的修補也只針對讓「上一步版號變更」可編譯，不順便
  做其他重構。
- 觸發 Azure Pipeline、push 到 `nightly-build` / `last-good` 都是會影響
  遠端共享狀態的動作，執行前留意目前是第幾輪、是否超過重試上限。
