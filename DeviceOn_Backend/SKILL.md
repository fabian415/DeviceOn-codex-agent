# CVE 自動化修補流程 (DeviceOn Backend)

這份文件是給 Codex Agent 看的操作說明。所有「不需要判斷、純機械式」的步驟(git 分支切換、push、觸發 Azure Pipeline、等待、下載 artifacts、算 fixable/unfixable、輪數與重試次數計數、失敗回退)都已經寫進固定腳本 `cve-loop.sh`,你不需要、也不應該自己重新用 git/az/jq 組這些指令。

你只需要負責兩件「需要判斷」的事:

1. **編譯失敗**時,讀懂建置 log,判斷根因並修改 `build.gradle`(必要時直接改程式碼)。
2. **編譯成功**時,讀懂 Trivy 報告,用 `dependencyInsight` 定位每個 fixable CVE 該改哪個版號,修改 `build.gradle`。

改完都是 `git add -A && git commit -m "..."`,然後照下面的流程繼續呼叫 `cve-loop.sh`。**你不需要自己 push、merge、或決定要不要開新一輪**,這些 `cve-loop.sh` 都會做。

## 檔案總覽

| 檔案 | 用途 |
|---|---|
| `~/workspace/.env` | Azure DevOps 認證與設定,不進版控,不可印出內容 |
| `~/workspace/cve-loop.sh` | **這個流程主要呼叫的腳本**,見下方「指令合約」 |
| `~/workspace/<AZURE_DEVOPS_REPO>/Backend/` | 實際要修改 `build.gradle` 的地方;這個 git clone 跨多次 Codex 呼叫重複使用,不屬於下面的「session」 |
| `~/workspace/.cve-loop-session` | `cve-loop.sh` 自己維護,記錄目前是哪個 session,你不需要讀寫它 |
| `~/workspace/runs/<開始時間戳>/` | 一個 session 的所有機械產物都在這裡(見下方說明) |
| `runs/<時間戳>/.cve-loop-state` | 目前輪數/嘗試次數計數 |
| `runs/<時間戳>/pipeline-logs/roundN-attemptM.log` | 每次呼叫 `trigger-build` 的完整輸出 |
| `runs/<時間戳>/pipeline-artifacts/<runId>/` | 該次 pipeline run 下載回來的 artifacts |
| `runs/<時間戳>/.cve-loop-history.jsonl` | `cve-loop.sh` 自動記錄的每輪/每次嘗試機械資料,`report` 指令靠這個產報表 |
| `runs/<時間戳>/ai-patch-report-<產出時間戳>.xlsx` | `report` 指令產出的 Excel 報表 |
| `runs/<時間戳>/pr-summary.md` | `report` 指令自動產出的修補摘要(Markdown),內容跟 Excel「執行總覽」分頁一致,`report` 會直接拿它當 PR 描述用 |
| `~/workspace/generate-report.py` | `report` 指令背後呼叫的報表產生腳本,不需要、也不應該自己直接執行 |

**session 生命週期完全自動,這點只在此處說明一次,後文不再重複**:第一次呼叫 `start-round` 時,`cve-loop.sh` 會自動開一個新的 `runs/<時間戳>/` 當這次 session 的資料夾,之後同一個 session 裡的每個指令都會自動接到同一個資料夾。`report` 產出報表後會自動清掉 `.cve-loop-session`、收掉這個 session,下一次呼叫 `start-round` 就會自動開一個全新的 `runs/<時間戳>/`,`outer_round` 重新從 1 起算。你不需要自己建資料夾、搬檔案、決定路徑或做任何清理。

## 前置準備

```sh
set -a
source ~/workspace/.env
set +a
```

`.env` 需要 `AZURE_DEVOPS_PAT`、`AZURE_DEVOPS_ORG`、`AZURE_DEVOPS_PROJECT`、`AZURE_DEVOPS_REPO`、`MAIN_BRANCH`、`AZURE_PIPELINE_NAME`。**PAT 絕對不要印出來或寫進任何會被 commit 的檔案**(`build.gradle`、commit message、這份文件本身都不行)。

## `cve-loop.sh` 指令合約

```sh
cd ~/workspace
./cve-loop.sh start-round     # 開新一輪:對齊 last-good,重建 nightly-build
./cve-loop.sh trigger-build   # push nightly-build → 觸發 pipeline → 等待 → 判讀結果
./cve-loop.sh status          # 查目前輪數/嘗試次數,唯讀、不做任何變更
./cve-loop.sh report          # 彙整成 Excel 報表,只在整體流程真正停止時呼叫一次
```

每個指令最後一行永遠會印出 `RESULT=<狀態> key=value ...` 這種格式,照這個表格分派下一步:

| RESULT | exit code | 意義 | 你該做什麼 |
|---|---|---|---|
| `ROUND_STARTED` | 0 | 新一輪已備妥(`nightly-build` = 最新 `last-good`) | 呼叫 `trigger-build` |
| `MAX_ROUNDS_REACHED` | 4 | 已達 `CVE_LOOP_MAX_ROUNDS`(預設 10)輪 | 呼叫 `report` 產出報表,**停止**,回報使用者目前狀態與報表路徑 |
| `BUILD_OK` | 0 | 編譯成功,已自動 push 到 `last-good`;附 `trivy_json`、`fixable`、`unfixable` | 先用 `trivy_json` 重算一次 fixable/unfixable 並回報使用者(見「修補 CVE」);`fixable>0` → 修補、commit,再呼叫 `start-round`;`fixable=0` → 呼叫 `report`,**完成,停止** |
| `BUILD_OK_NO_TRIVY_REPORT` | 0 | 編譯成功但找不到預期的 Trivy 報告檔(artifact 結構可能變了) | **這是流程的停止點,不要自動繼續修補 CVE、也不要呼叫 `report`**(資料不完整)。用 `find "<artifacts_dir>" -type f` 列出實際檔案,整理出你觀察到的落差,回報使用者,等待人工確認 artifact 結構或更新 `cve-loop.sh` 後再繼續(細節見「常見問題」) |
| `BUILD_FAILED` | 2 | 這一輪還沒超過重試上限;附 `log` 路徑 | 讀 `log`,判斷根因(不限於改版號,必要時直接改程式碼,見「修補建置失敗」),commit,再呼叫 `trigger-build`(**不要**呼叫 `start-round`,那會把你剛剛的修補丟掉) |
| `BUILD_FAILED_GIVE_UP` | 3 | 同一輪重試已達 `CVE_LOOP_MAX_BUILD_ATTEMPTS`(預設 3)次,已自動回退到 `last-good`(這一輪的修補全部作廢) | **不是整體流程的停止點**,只代表這一輪用掉的 3 次嘗試都沒找到能編譯的做法。回報使用者這一輪試過什麼、卡在哪,然後回到外層呼叫 `start-round` 開新一輪,換更根本的做法再試,直到真的收到 `MAX_ROUNDS_REACHED` 才停止 |
| `REPORT_GENERATED` | 0 | 報表與 PR 已自動處理完畢;附 `path`、`pr_result`、`pr_id`、`pr_url`、`branch_cleanup` | 見「產出報表」一節 |

`reason=PIPELINE_INFRA_ERROR`(可能出現在 `BUILD_FAILED`/`BUILD_FAILED_GIVE_UP`)代表觸發/等待 pipeline 的過程本身沒能正常跑完(觸發失敗、等待逾時等),不一定是程式碼問題,先看 `log` 判斷是否為基礎設施問題(PAT 失效、pipeline 名稱錯、網路逾時),這種情況修 `build.gradle` 沒有用,應該直接回報使用者。

## 整體流程

```
loop:
    result = run("./cve-loop.sh start-round")
    if result == MAX_ROUNDS_REACHED:
        run("./cve-loop.sh report")            # 產出 Excel 報表 + 自動視情況開 PR
        stop, 回報使用者目前狀態、報表路徑、PR 結果  # 唯一真正的整體停止點(成功結束除外)

    loop:
        result = run("./cve-loop.sh trigger-build")

        if result == BUILD_OK:
            if fixable == 0:
                run("./cve-loop.sh report")     # 產出 Excel 報表 + 自動視情況開 PR
                stop, 回報使用者修補完成、報表路徑、PR 結果
            patch_cves(trivy_json)           # 見「修補 CVE」
            commit()
            break   # 回到外層 loop 開新一輪

        if result == BUILD_OK_NO_TRIVY_REPORT:
            stop, 回報使用者 artifact 結構落差(不產報表,資料不完整,不自動繼續)

        if result == BUILD_FAILED:
            patch_build_failure(log)        # 見「修補建置失敗」,必要時直接改程式碼
            commit()
            continue   # 留在內層 loop,再次 trigger-build

        if result == BUILD_FAILED_GIVE_UP:
            回報使用者這一輪的結果 (不是整體停止)
            break   # 這一輪放棄,回到外層 loop 開新一輪,換個更根本的做法再試
```

只有 `MAX_ROUNDS_REACHED` 或 `fixable=0` 的 `BUILD_OK` 是整體流程真正停止的地方,兩者都要先呼叫一次 `report` 再停止。**不要因為某個 CVE 曾經導致建置失敗就永久放棄它**:換一輪重試時優先考慮更根本的做法(讀懂用到該套件的程式碼、直接修改呼叫方式配合新版 API),而不是每次都用同一招版號升級硬碰硬。

工作目錄:修 `build.gradle` 都在 `~/workspace/${AZURE_DEVOPS_REPO}/Backend` 下(`cve-loop.sh` 已經幫你 `git checkout` 到 `nightly-build` 分支)。

## 專案模組總覽

`settings.gradle` 定義了以下 Gradle project,目錄名稱與 Gradle project 名稱不完全一致,判斷 `dependencyInsight` 該對哪個模組跑時要對照這張表:

| Gradle project 路徑 | 對應目錄 | 產物 | 說明 |
|---|---|---|---|
| `:RMMLib` | `RMMLib/` | 內部函式庫 (jar) | 名稱未被 rename |
| `:worker` | `Worker/` | `worker-<版本>.jar` (fat jar) | RMM Worker |
| `:portal` | `WebApp/` | `portal-<版本>.war` | Web 後台,套用 `war` plugin |
| `:ota-lib` | `OTALib/` | 內部函式庫 (jar) | OTA 用共用函式庫 |
| `:provisioning-worker` | `OTAWorker/` | `provisioning-worker-<版本>.jar` (fat jar) | OTA Worker |

所有第三方套件版本統一定義在根目錄 `build.gradle` 的 `ext { ver_... = '...' }` 區塊(依 `ver_{Group}_{Module}` 命名),各模組的 `dependencies {}` 再用 `${ver_xxx}` 引用。**大部分 CVE 修補的最終落點就是改這裡的版號**,除非問題套件是透過某個上游函式庫「間接」帶入的舊版本(見下方「間接依賴」)。

## 修補 CVE(`BUILD_OK` 且 `fixable > 0` 時)

`trigger-build` 印出的 `trivy_json` 路徑就是這次要處理的報告。**只要編譯成功、拿到 Trivy 報告,第一件事一律是重新算一次 fixable/unfixable 並回報給使用者**(不要只信任 `trigger-build` 印出的數字,用同一份 `trivy_json` 現算現印,確保跟接下來要處理的清單一致):

```sh
TRIVY_JSON="<trigger-build 印出的 trivy_json 路徑>"
fixable=$(jq '[.Results[]?.Vulnerabilities[]? | select(.FixedVersion != null and .FixedVersion != "")] | length' "$TRIVY_JSON")
unfixable=$(jq '[.Results[]?.Vulnerabilities[]? | select(.FixedVersion == null or .FixedVersion == "")] | length' "$TRIVY_JSON")
echo "Fixable CVEs: ${fixable} / Unfixable CVEs: ${unfixable}"
```

把這兩個數字告知使用者之後,再列出每個有 `FixedVersion` 的項目細節,準備逐一定位修補:

```sh
jq '[.Results[]?.Vulnerabilities[]? | select(.FixedVersion != null and .FixedVersion != "")
     | {pkg: .PkgName, installed: .InstalledVersion, fixed: .FixedVersion, cve: .VulnerabilityID}]' \
   "$TRIVY_JSON"
```

### 用 `dependencyInsight` 定位

```sh
cd ~/workspace/${AZURE_DEVOPS_REPO}/Backend
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
./gradlew -q --console=plain :<project>:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency <group>:<artifact>
```

`<project>` 用上面模組表替換(`RMMLib`/`worker`/`portal`/`ota-lib`/`provisioning-worker`)。同一個套件在不同模組可能被解析成不同版本,對「該套件出現過的每個模組」都各跑一次。

### 判斷修補位置

- **直接依賴**(某模組 `build.gradle` 直接寫了這個 artifact):到根目錄 `build.gradle` 的 `ext { }`,找對應的 `ver_{Group}_{Module}` 變數,升級到 `FixedVersion`。
- **間接依賴**(`dependencyInsight` 的依賴路徑樹最上層是別的 SDK,例如 `aws-java-sdk-s3`):只改根目錄 `ver_*` 不一定生效(Gradle 版本衝突解決預設取最高版本,仍可能選到 SDK 內建版本)。要在**該模組**的 `build.gradle` 明確加約束:

  ```groovy
  dependencies {
      constraints {
          implementation("<group>:<artifact>:${ver_xxx}") {
              because 'CVE-XXXX-YYYYY: 強制對齊安全版本,覆蓋上游 SDK 帶入的舊版'
          }
      }
  }
  ```

  或視情況升級帶入舊版本的上游 SDK 本身版號(往往更根本)。

### 版號政策

版號升級以 Trivy 報告的 `FixedVersion` 為**下限**,不是天花板:優先用 `FixedVersion`,但如果它本身跟其他套件不相容導致編譯失敗,可以繼續往上找「能相容、且仍然修掉原本 CVE」的最低版本(細節見「修補建置失敗」)。不要因為其他無關理由(例如順手更新到最新版)跳號到不必要的更高版本。

一次只處理一批可判斷的 CVE,避免一次改太多讓下次 `BUILD_FAILED` 難以定位是哪個改動造成的。

## 修補建置失敗(`BUILD_FAILED` 時)

`trigger-build` 印出的 `log` 可能是:

- 一個目錄(`artifacts_dir` 底下確實有檔案):用 `find "$log" -type f` 看內容,通常是 pipeline 發布的建置產物/log。
- 一個檔案(`pipeline-logs/.../build-log.txt`):直接讀檔案內容,找 `FAILED`/`error:`/`Exception` 關鍵字附近的內容。

最常見的根因:上一輪修補把某個 `ver_*` 升得太高,跟其他套件的 API 不相容(編譯期或執行期錯誤),或版號字串本身打錯。**修法不限於改 `build.gradle`**,按下面順序嘗試:

1. 先確認是不是版號打錯(typo、格式錯誤)。
2. 若是 API 不相容,優先考慮改用該套件「能相容、且仍然修掉原本 CVE」的最低版本,而不是直接整個回退到升版前的版本(回退等於放棄這個 CVE 的修補)。
3. 若牽涉到間接依賴的 `constraints`,確認約束寫的版號跟 conflict resolution 實際選中的是否一致(可以重新跑一次 `dependencyInsight` 確認)。
4. **若前面幾種都解不掉(新版真的移除/改了 API),直接去讀呼叫到該套件的專案原始碼**(`grep -rl` 找出 import 該套件的 `.java`),照新版 API 改寫呼叫方式,讓專案跟新版套件相容。這是刻意允許的做法:目標是「CVE 修掉、還能編譯」,不是「不動程式碼」。改動範圍只到讓相容/能編譯為止,不要順便重構其他無關的地方。

   **但「能編譯」只是必要條件,不是目標本身**。任何時候都不能為了讓程式碼編譯過、或讓 pipeline 跑成功,就把原本會執行的邏輯換成丟 `Exception`、回傳 `null`/空結果、印警告後直接略過、或整段包 `try/catch` 吞掉錯誤——這種寫法建置會過、Trivy 也會顯示已修,但功能其實悄悄壞掉了,而且比明顯的建置失敗更難被發現。原本能用的功能,修完 CVE 之後也必須繼續能用。若新版 API 真的整個移除某個能力,先找同一套件裡功能對等的替代 API(通常只是改了名字或參數簽名,不是能力真的消失),或找官方遷移指南/CHANGELOG 確認正確用法;找到多個候選方案時,自己評估哪個最接近原本行為(功能覆蓋度、效能、與專案其他地方用法的一致性),選定後直接實作,**不要停下來等使用者決定**,也不要用丟例外或空實作繞過去。實作完成後回報使用者時,說明原本卡在哪、最後選了哪個替代方案、跟原本行為有什麼差異即可。

同一輪 3 次嘗試裡可以混用以上幾種做法;如果 3 次都還是編不過(`BUILD_FAILED_GIVE_UP`),下一輪換一個還沒試過的做法再試(見「整體流程」),不要重複同一招。

## 產出報表(整體流程真正停止前)

`cve-loop.sh` 在每次 `start-round`/`trigger-build` 時,會把輪數、嘗試次數、建置結果、`fixable`/`unfixable`、Trivy 報告路徑、這次嘗試用的 commit 等機械資料,自動附加寫進這個 session 的 `.cve-loop-history.jsonl`(不進版控,不需要你手動維護,純機械式記錄)。

只有在真正整體停止的兩個時機才呼叫一次:

```sh
cd ~/workspace
./cve-loop.sh report
```

- `MAX_ROUNDS_REACHED`(達輪數上限)
- `fixable == 0` 的 `BUILD_OK`(CVE 全部修完)

`BUILD_FAILED_GIVE_UP`、`BUILD_OK_NO_TRIVY_REPORT` **不要**呼叫 `report`——前者不是整體停止點,後者代表資料本身不完整。

執行完會印出 `RESULT=REPORT_GENERATED path=<xlsx 路徑> pr_result=<...> pr_id=<...> pr_url=<...>`。報表產在 `runs/<時間戳>/ai-patch-report-<產出時間戳>.xlsx`,包含三個分頁:

| 分頁 | 內容 |
|---|---|
| 執行總覽 | 起始/最終可修復數、修復率、不可修復數、輪數與嘗試次數上限、實際輪數、建置成功/失敗次數、總耗時 |
| 逐輪嘗試 | 每一輪每次 `trigger-build` 的修補前後 fixable/unfixable、結果、本輪減少數、耗時、變更檔案、變更行數、說明 |
| CVE 追蹤 | 每個 CVE 目前狀態(已修復/仍存在/不可修復)、版本資訊、消失於第幾輪第幾次嘗試 |

### 自動建立 `last-good → MAIN_BRANCH` 的 PR,並自動核准 + auto-complete

`report` 產完 xlsx 之後,會**自動**檢查 `origin/last-good` 是否領先 `origin/${MAIN_BRANCH}`(即這個 session 至少有一輪 `BUILD_OK` 成功把修補推進了 `last-good`):領先的話用 `az repos pr create`(若已有 active 的同名 PR 則改用 `update`,不會疊出重複 PR)建立/更新 PR,描述帶入 `pr-summary.md`。接著依序做:

1. 把 xlsx 用 Pull Request Attachments API 上傳、把下載連結補進描述最後一段。**這一步一定排在核准/auto-complete 之前**:PR 一旦被 auto-complete 合併完成,Azure DevOps 就不允許再編輯它的描述(`TF401181`),太晚補連結會靜默失敗,附件雖然上傳成功但 PR 頁面上看不到連結。
2. 用 `az repos pr set-vote --vote approve` 對這個 PR 投下核准票,再用 `az repos pr update --auto-complete true` 開啟 auto-complete——只要 `${MAIN_BRANCH}` 的分支政策(若有設定,例如 build 驗證)通過,PR 就會在**沒有人工介入**的情況下自動合併進 `${MAIN_BRANCH}`。
3. 輪詢最多 60 秒(每 5 秒查一次 PR 狀態),確認 PR 真的變成 `completed`(即已合併)。真的合併完成後,**自動刪除遠端 `last-good`、`nightly-build` 分支**——這兩個分支的任務到合併進 `${MAIN_BRANCH}` 就結束了,留著只會讓下一個 session 誤用到舊的 base。下次呼叫 `start-round` 時,`ensure_last_good` 找不到 `origin/last-good` 會自動從當下最新的 `origin/${MAIN_BRANCH}` 重新建立,`nightly-build` 也會在 `trigger-build` 時重新 force-push,不需要任何手動處理。60 秒內沒等到 `completed`(通常代表分支政策還在跑,或核准/auto-complete 失敗)則不刪分支。

沒有領先 `${MAIN_BRANCH}` 則整段(建 PR、核准、auto-complete、刪分支)都不會做。

核准、開啟 auto-complete、刪分支這幾步失敗時只會印警告,**不會**影響 `pr_result`(因為 PR 本身已經建立/更新成功)。核准最常見的失敗原因是 Azure DevOps 分支政策開了「不可核准自己送出的變更」,而 `.env` 裡的 PAT 對應的帳號正好就是建立這個 PR 的人——這種情況下核准票會被拒絕,PR 會停在等待人工核准的狀態,自然也就不會被合併、不會刪分支。

這整段是純機械式操作,收在 `report` 指令裡自動做。**你不需要、也不應該自己呼叫 `az repos pr create`/`update`/`set-vote`、用 `curl` 打 attachments API,或自己跑 `git push origin --delete`**。你只需要讀 `pr_result` 和 `branch_cleanup`:

| `pr_result` | 意義 | 你該做什麼 |
|---|---|---|
| `PR_CREATED` | 已建立新 PR,並嘗試自動核准 + 開啟 auto-complete | 把 `pr_url` 告知使用者;若腳本印出核准或 auto-complete 失敗的警告,一併告知使用者這個 PR 可能還停在待人工核准的狀態 |
| `PR_UPDATED` | 已有 active PR,更新了描述與附件,並重新嘗試自動核准 + 開啟 auto-complete | 同上,把 `pr_url` 告知使用者 |
| `PR_SKIPPED_NO_CHANGES` | `last-good` 沒有領先 `${MAIN_BRANCH}`,不需要開 PR | 照常告知使用者報表路徑即可,不用提 PR、也不用提分支清理 |
| `PR_FAILED` | 建立/更新 PR 或上傳附件失敗(權限不足、API 逾時等) | 告知使用者自動建立 PR 失敗,需要手動到 Azure DevOps 開一個 `last-good → ${MAIN_BRANCH}` 的 PR 並附上 `path` 指向的 xlsx;不要自己嘗試用 `az repos`/`curl` 補救,先如實回報即可 |

| `branch_cleanup` | 意義 | 你該做什麼 |
|---|---|---|
| `DELETED` | PR 已確認合併完成,遠端 `last-good`、`nightly-build` 都刪除成功 | 照常告知使用者即可,不用特別提醒 |
| `FAILED` | PR 已合併完成,但刪其中至少一個遠端分支失敗 | 告知使用者哪個分支沒刪成功(看 stderr 警告),需要時手動 `git push origin --delete <branch>` |
| `SKIPPED` | `last-good` 沒有領先(沒開 PR),或 PR 60 秒內還沒變成 `completed`(核准/auto-complete 失敗,或分支政策還在跑) | 對照 `pr_result` 判斷原因:`PR_SKIPPED_NO_CHANGES` 就是正常情況;若 `pr_result` 是 `PR_CREATED`/`PR_UPDATED` 卻仍是 `SKIPPED`,代表 PR 還沒真的合併,告知使用者 PR 可能還停在待核准/待政策通過,分支先保留 |

如果某一輪早於這個 session 自己的 `.cve-loop-history.jsonl` 開始記錄之前就已經跑過(理論上只會發生在 session 內途中不明原因遺失記錄這種罕見情況),`generate-report.py` 會盡量從同個 session 底下的 `pipeline-logs`/`pipeline-artifacts` 回溯 fixable/unfixable 數字,但「變更檔案」「變更行數」這類需要 commit 資訊的欄位會老實標成「回溯資料,無法取得變更明細」,不會用猜的填數字。

你不需要、也不應該直接執行 `generate-report.py` 或自己拼報表——`report` 已經把這件事做完了。

## 常見問題

- **`dependencyInsight` 找不到指定 dependency**:該版本在該模組的 `runtimeClasspath` 上沒被解析到,換一個模組再試,或去掉版號只留 `--dependency <group>:<artifact>` 看實際解析成哪個版本。
- **改了 `ver_*` 但 `dependencyInsight` 顯示版本沒變**:多半被其他上游 SDK 用更高優先權帶入,照「間接依賴」加 `constraints` 或升級該 SDK。
- **`BUILD_OK_NO_TRIVY_REPORT`**:目前腳本假設 Trivy 報告會出現在 artifact 裡路徑含 `Trivy-Report` 的 `*.json` 檔(實測過一次真實成功的 run 是這樣)。如果 pipeline 改了發布方式會抓不到。這是流程停止點,不要自動繼續,只需診斷並回報:

  ```sh
  ARTIFACTS_DIR="<trigger-build 印出的 artifacts_dir>"
  find "$ARTIFACTS_DIR" -type f
  ```

  把實際看到的檔案結構(可能不叫 `Trivy-Report`,或不在預期子目錄)回報給使用者,說明預期路徑跟實際路徑的落差,供之後更新 `cve-loop.sh` 的 `find_trivy_json`。之後怎麼繼續由使用者決定,不要自己算 fixable/unfixable 後就接著修補或呼叫 `report`。

- **`reason=PIPELINE_INFRA_ERROR` 的 `BUILD_FAILED`**:`log` 內容可能是觸發/等待 pipeline 流程的完整輸出(不是 gradle log),先確認是不是 PAT/pipeline 名稱/逾時這類基礎設施問題,不要誤當成程式碼問題去改 `build.gradle`。
- **建置失敗時抓 log 用的 REST API fallback 尚未實測**:`cve-loop.sh` 在 artifacts 目錄找不到內容時,會改用 Azure DevOps Build REST API(`/_apis/build/builds/<runId>/logs`)抓 log,這條路徑還沒在這個組織上驗證過;如果它也失敗,`log` 會退回整段觸發流程的輸出,必要時直接照印出的 `run_id` 到 Azure DevOps UI 上看。

## 安全與限制事項

- PAT 只存在 `~/workspace/.env`,絕不印出、絕不寫進任何會被 commit 的檔案(見「前置準備」)。
- 不要對 `last-good` 做 `git push --force`;`cve-loop.sh` 只用 `--ff-only` merge 推進它。`nightly-build` 是拋棄式分支,被 `--force` push 是預期行為,不用理會。
- 不要自己手動跑 `git merge`/`git push origin last-good`/建立新一輪的 `nightly-build`,也不要自己建立/刪除 `runs/` 底下的資料夾或動 `.cve-loop-session`——這些都由 `cve-loop.sh` 自動處理(見「檔案總覽」),手動介入會讓輪數/重試次數計數與實際狀態不一致。真的遇到 session 卡住、流程被中斷的情況,用 `./cve-loop.sh new-session` 放棄目前 session(不會動到已產生的資料,只是讓下次 `start-round` 開新的),這不是正常流程的一部分。
- 不要自己手動呼叫 `az repos pr create`/`update`/`set-vote`、用 `curl` 打 PR attachments API,或自己跑 `git push origin --delete <branch>`(見「產出報表」),只有收到 `pr_result=PR_FAILED` 或 `branch_cleanup=FAILED` 時才需要回報使用者請他們手動處理。
- **`last-good → ${MAIN_BRANCH}` 的 PR 會被自動核准並開啟 auto-complete,符合分支政策就會自動合併進 `${MAIN_BRANCH}`,不會等人工 review;確認合併完成後還會自動刪掉遠端 `last-good`、`nightly-build` 分支**(見「產出報表」)。這是刻意設計的行為,不是 bug;如果之後需要改回「只開 PR、等人工核准」或「不要自動刪分支」,要改的是 `cve-loop.sh` 的 `sync_last_good_pr()`,不要在這裡用其他方式繞過。
- 版號政策見「修補 CVE」;修建置失敗需要改程式碼配合新版 API 時,只改到能相容、能編譯為止,不順便重構無關的地方。
- **「能編譯」「建置成功」不等於「修好了」**,細節與處理方式見「修補建置失敗」一節——不能為了讓 pipeline 跑成功就把原本邏輯換成丟例外、回傳空結果或吞掉錯誤,那只是把「建置失敗」換成「功能悄悄壞掉」,而且更難被發現。
