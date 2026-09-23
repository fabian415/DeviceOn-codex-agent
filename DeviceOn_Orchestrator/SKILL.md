# CVE 自動化修補流程 (DeviceOn Orchestrator)

這份文件是給 Codex Agent 看的操作說明。這個 skill 不自己修 CVE，工作是照順序協調三段流程：

```
1. Frontend CVE 自動修補   →  DeviceOn_Frontend/SKILL.md（完整跑到底）
2. dist 產物同步進 Backend →  ./bridge-dist.sh sync-dist（純機械式，本文件唯一直接負責的事）
3. Backend CVE 自動修補    →  DeviceOn_Backend/SKILL.md（完整跑到底）
```

第 1、3 步完全照抄各自的 SKILL.md 執行——**不要在跑那兩段的時候把這份文件的規則和它們的規則混著用**，那兩份文件本身就是完整、獨立的流程，走到「整體流程真正停止」（`report` 印出 `MAX_ROUNDS_REACHED` 或 `fixable=0` 的 `REPORT_GENERATED`）才算這一步做完。第 2 步是純機械式的橋接動作（build dist、同步檔案、commit、push），已經寫進 `bridge-dist.sh`，你不需要、也不應該自己重新用 git/npm/rsync 組這些指令，只需要照下面的「指令合約」呼叫它、讀 `RESULT=` 分派下一步。

## 檔案總覽

| 檔案 | 用途 |
|---|---|
| `~/workspace/DeviceOn_Orchestrator/.env` | 這個 skill 自己的 Azure DevOps 認證與設定,不進版控,不可印出內容 |
| `~/workspace/DeviceOn_Orchestrator/bridge-dist.sh` | 第 2 步唯一要呼叫的腳本,見下方「`bridge-dist.sh` 指令合約」 |
| `~/workspace/DeviceOn_Frontend/SKILL.md` | 第 1 步完整照跑的流程文件 |
| `~/workspace/DeviceOn_Backend/SKILL.md` | 第 3 步完整照跑的流程文件 |
| `~/workspace/DeviceOn_Frontend/.env` | 前端流程自己的設定;`bridge-dist.sh` 會自動從這裡讀 `MAIN_BRANCH`(前端的 main branch 名稱),不需要在 Orchestrator 的 `.env` 重複一份 |
| `~/workspace/DeviceOn_Backend/.env` | 後端流程自己的設定;`bridge-dist.sh` 不會用到這份,第 2 步是用 Orchestrator 自己的 `.env` 直接操作 Backend repo |

## 前置準備

```sh
cd ~/workspace/DeviceOn_Orchestrator
cp -n .env.sample .env   # 第一次使用時,複製範例檔,再手動填入實際值
```

`.env` 需要 `AZURE_DEVOPS_PAT`、`AZURE_DEVOPS_ORG`、`AZURE_DEVOPS_PROJECT`、`AZURE_DEVOPS_REPO_FRONTEND`、`AZURE_DEVOPS_REPO_BACKEND`、`MAIN_BRANCH_BACKEND`。**PAT 絕對不要印出來或寫進任何會被 commit 的檔案**(commit message、這份文件本身都不行)。這份 `.env` 跟 `DeviceOn_Frontend/.env`、`DeviceOn_Backend/.env` 是三份獨立的檔案,`AZURE_DEVOPS_REPO_FRONTEND`/`AZURE_DEVOPS_REPO_BACKEND` 的值應該分別對齊那兩份 `.env` 裡的 `AZURE_DEVOPS_REPO`(不需要一致到自動同步,但改動時要記得三邊一起改)。

## 整體流程

```
步驟一:前端 CVE 修補(完整照跑 DeviceOn_Frontend/SKILL.md)
    cd ~/workspace/DeviceOn_Frontend
    照 DeviceOn_Frontend/SKILL.md 的「整體流程」跑完整個 loop
    直到收到 MAX_ROUNDS_REACHED 或 fixable=0 的 BUILD_OK,且已呼叫過一次 report
    回報使用者:report 印出的 path / pr_result / pr_url / branch_cleanup

步驟二:把前端最好的修補結果同步進後端(純機械式,見下方指令合約)
    cd ~/workspace/DeviceOn_Orchestrator
    result = run("./bridge-dist.sh sync-dist")
    照下方「bridge-dist.sh 指令合約」的表格處理 RESULT
    只有 RESULT=DIST_SYNCED 或 RESULT=DIST_NO_CHANGES 才能繼續步驟三
    其餘 RESULT 一律停止,回報使用者,等待人工排除後才重新呼叫

步驟三:後端 CVE 修補(完整照跑 DeviceOn_Backend/SKILL.md)
    cd ~/workspace/DeviceOn_Backend
    照 DeviceOn_Backend/SKILL.md 的「整體流程」跑完整個 loop
    直到收到 MAX_ROUNDS_REACHED 或 fixable=0 的 BUILD_OK,且已呼叫過一次 report
    回報使用者:report 印出的 path / pr_result / pr_url
```

三個步驟依序執行、不可跳步、不可平行:步驟二需要步驟一產出的 `runs/<時間戳>/pr-summary.md` 才能在 commit 訊息裡寫出修補了幾個 CVE;步驟三則需要步驟二已經把前端 dist 產物 commit 進 `MAIN_BRANCH_BACKEND`,這樣後端 `cve-loop.sh` 第一次呼叫 `start-round` 時,`ensure_last_good` 從 `origin/${MAIN_BRANCH_BACKEND}` 建立 `last-good` 才會連同新的 `WebApp/src/main/webapp/` 一起帶進去,後續 Trivy 掃描與修補才會是在「前端最新結果 + 後端」的基礎上進行。

## `bridge-dist.sh` 指令合約

```sh
cd ~/workspace/DeviceOn_Orchestrator
./bridge-dist.sh sync-dist   # 主動作:找前端最佳結果 → build dist → 同步進後端 WebApp/src/main/webapp/ → commit + push 到 MAIN_BRANCH_BACKEND
./bridge-dist.sh status      # 查目前前端最佳 ref、後端是否有未收尾的 last-good,唯讀、不做任何變更
```

`sync-dist` 內部依序做以下事情,全部是機械式判斷,不需要 Codex Agent 介入:

1. **決定前端最佳結果在哪個 ref**:`fetch origin` 後,如果 `origin/last-good` 還存在且領先 `origin/${MAIN_BRANCH}`(前端的 `MAIN_BRANCH`,腳本自動從 `DeviceOn_Frontend/.env` 讀取),代表這一輪 PR 還沒被自動合併(可能還在等分支政策、或核准失敗卡住),最佳結果在 `last-good`;否則代表已經合併進 `MAIN_BRANCH` 並且 `last-good`/`nightly-build` 已被自動刪除(或這個 session 從頭到尾都沒有任何 fixable CVE),最佳結果就是目前的 `MAIN_BRANCH`。這對應使用者說的「找到最後一次能夠 last-good 會 PR 到 MAIN_BRANCH,代表目前已經是前端 CVEs 修復的最好結果」。
2. checkout 該 ref、對齊 CI 的 Node 版本(有裝 nvm 才會生效,沒裝不當錯誤)、`npm install && npm run build`,產出 `dist/`。
3. 從 `DeviceOn_Frontend/runs/<最新時間戳>/pr-summary.md` 讀出「已修復 N 個」「初始可修復 CVE:X」這些數字,讀不到就標 `unknown`,不會用猜的湊數字。
4. 確保 `DeviceOn_Backend/${AZURE_DEVOPS_REPO_BACKEND}` 這個 clone 存在且乾淨對齊 `origin/${MAIN_BRANCH_BACKEND}`;如果發現 `origin/last-good` 已經存在(代表後端 `cve-loop.sh` 有一個 session 卡在中途、還沒跑完就被中斷),直接中止並回報,不會貿然 push——因為 push 進 `MAIN_BRANCH_BACKEND` 對那個卡住的 session 沒有幫助,它下次還是會接著用舊的 `last-good` 工作,需要人工先確認那個 session 的狀況。
5. 用 `rsync -a --delete` 把 `dist/` 的內容同步進 `WebApp/src/main/webapp/`,**保留 `WEB-INF/`**(後端自行維護的 Java web 設定,不屬於前端建置產物,dist 裡本來就不會有這個目錄)。
6. 如果同步後 `WebApp/src/main/webapp/` 底下完全沒有變更(前端這次沒有任何新的修補內容),直接回報 `DIST_NO_CHANGES`,不 commit、不 push。
7. 有變更的話,`git add -A` 該子目錄、寫英文 commit 訊息(帶入步驟 3 讀到的修補數字)、直接 push 到 `origin/${MAIN_BRANCH_BACKEND}`(**不開 PR,直接 commit**,這是使用者明確指定的行為)。

commit 訊息格式固定是:

```
Sync frontend dist from automated CVE remediation

Source: DeviceOn_Frontend @ <last-good|MAIN_BRANCH> (<commit>)
Fixed <N> CVE(s) (of <M> initially fixable) via the frontend CVE auto-remediation skill.
Synced build output into WebApp/src/main/webapp/ (WEB-INF preserved).
```

每個指令最後一行永遠會印出 `RESULT=<狀態> key=value ...`,照這個表格分派下一步:

| RESULT | exit code | 意義 | 你該做什麼 |
|---|---|---|---|
| `DIST_SYNCED` | 0 | 已成功 commit + push 到 `MAIN_BRANCH_BACKEND`;附 `backend_commit`、`source_ref`、`source_commit`、`fixed`、`initial_fixable`、`pr_pending`、`summary` | 把 `backend_commit` 與修補數字告知使用者,接著進入步驟三(Backend CVE 修補) |
| `DIST_NO_CHANGES` | 0 | 前端最佳結果 build 出來的 dist 跟後端目前 `WebApp/src/main/webapp/` 內容完全一樣,沒東西可 commit | 照常回報使用者「這次沒有新的前端變更需要同步」,直接進入步驟三 |
| `ABORTED_FRONTEND_NOT_READY` | 1 | 找不到 `DeviceOn_Frontend/${AZURE_DEVOPS_REPO_FRONTEND}` 這個 clone | 代表步驟一(前端 SKILL.md)還沒真的跑過、或跑在錯的路徑,回頭確認步驟一是否已完成,**不要**自己手動 clone 來繞過 |
| `ABORTED_STALE_LAST_GOOD` | 1 | 後端 repo 已經有 `origin/last-good`,代表有一個後端 `cve-loop.sh` session 卡在中途沒收尾 | **不要**自己刪那個分支或硬 push,回報使用者,請他們先確認那個未收尾的 backend session 要繼續跑完、還是要用 backend 的 `./cve-loop.sh new-session` 放棄,確認乾淨後再重跑 `sync-dist` |
| `BUILD_FAILED` | 2 | `npm install`/`npm run build` 失敗,或 build 完 `dist/` 是空的;附 `log` | 讀 `log` 判斷根因(通常代表前端這次的「最佳結果」其實有沒被 CI 抓到的建置問題,或本機 Node 版本沒對齊),回報使用者,**不要**自己動手改前端程式碼修——這不是 CVE 修補判斷,前端程式碼的修改屬於步驟一(`DeviceOn_Frontend/SKILL.md`)的範疇 |
| `PUSH_FAILED` | 3 | push 到 `MAIN_BRANCH_BACKEND` 被拒絕(通常是遠端在這之間被別人推了新的 commit,non-fast-forward) | 回報使用者,確認 `MAIN_BRANCH_BACKEND` 是否有其他人同時在動,**不要**自己 force push,排除後重新呼叫 `sync-dist` |
| `STATUS_OK` | 0 | `status` 查詢完成,印出目前前端最佳 ref、後端是否有未收尾的 last-good | 唯讀查詢,照印出的內容回報使用者即可 |

## 安全與限制事項

- PAT 只存在各自的 `.env`(`DeviceOn_Orchestrator/.env`、`DeviceOn_Frontend/.env`、`DeviceOn_Backend/.env`),絕不印出、絕不寫進任何會被 commit 的檔案。
- 步驟二是這份文件唯一直接對 `MAIN_BRANCH_BACKEND` 做 **不經 PR 的直接 push**;這是使用者明確要的行為(讓後端 CVE 修補從「已經包含最新前端結果」的狀態開始),不要因為看起來像是繞過一般的 PR 流程就自己加上開 PR/等審核的步驟,也不要反過來在其他情境自作主張直接 push 到 main/master。
- 步驟一、三分別是 `DeviceOn_Frontend/SKILL.md`、`DeviceOn_Backend/SKILL.md` 各自完整、獨立的流程,**不要**把這份文件的規則套用到那兩步裡面——那兩步該怎麼判斷編譯失敗、怎麼修 CVE、怎麼收尾產報表跟開 PR,一律照它們自己的 SKILL.md,這份文件不重複、也不覆寫那些規則。
- `bridge-dist.sh` 的 `rsync --delete` 只作用在 `WebApp/src/main/webapp/` 這個子目錄,而且明確排除 `WEB-INF/`;不要擴大同步範圍到 `WebApp/` 底下其他目錄(例如 `src/main/java`、`src/main/resources`),那些不是前端 dist 的產物。
- 三個步驟之間不要省略中間回報:每一步結束都要照該步驟自己的規則告知使用者結果(報表路徑、PR 連結、`RESULT=` 內容),不要等三步全部跑完才一次回報。
