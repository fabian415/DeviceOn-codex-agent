# RUN.md — 建置環境與 CVE 自動化修補流程

本文件說明如何建置 `deviceon` (Backend) 這個多模組 Gradle 專案、如何用 Trivy
掃描專案內所有 Gradle 相依套件，以及如何用 `dependencyInsight` 追查一個有 CVE
的套件是被哪個上游 (direct/transitive) 依賴拉進來的，最終目的是把「掃描 →
定位 → 修補 → 驗證」串成一條可自動化執行的流程。

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
`CHANGELOG.md` 找 `# x.y.z (unreleased)` 這一行取版號。想固定版號重現 CI 結果時，
記得先 `export DEVICEON_TAG=x.y.z`。

所有第三方套件版本統一定義在根目錄 `build.gradle` 的 `ext { ver_... = '...' }`
區塊（依 `ver_{Group}_{Module}` 命名），各模組的 `dependencies {}` 再用
`${ver_xxx}` 引用。**這代表大部分 CVE 修補的最終落點就是改這裡的版號**，除非
問題套件是透過某個上游函式庫「間接」帶入的舊版本（後面第 5 節會說明怎麼分辨）。

---

## 1. 環境建置

以下步驟以 Ubuntu 24.04 (與本機一致) 為例，CI 端 (`Gradle@2` pipeline task)
使用的是 JDK 17，本地環境務必對齊，否則 `dependencyInsight` 解析出來的相依樹
可能因 JDK 版本相關的 dependency resolution / toolchain 差異而跟 CI 不一致。

### 1.1 安裝 JDK 17

```sh
sudo apt-get update
sudo apt-get install -y temurin-17-jdk
# 若套件庫沒有 temurin，改用：
#   sudo apt-get install -y openjdk-17-jdk

java -version   # 確認輸出為 17.x
```

### 1.2 Gradle 本身不需要另外安裝

專案已附 Gradle Wrapper（`./gradlew`），版本鎖定在
`gradle/wrapper/gradle-wrapper.properties` 中的 `8.14.5`，第一次執行
`./gradlew` 時會自動下載對應版本，不需手動安裝 Gradle，也不會受本機是否裝了
其他版本 Gradle 影響。

```sh
chmod +x ./gradlew
./gradlew --version
```

### 1.3 安裝 Trivy

```sh
# 官方 apt repo（Ubuntu/Debian 系）
sudo apt-get install -y wget gnupg
wget -qO - https://aquasecurity.github.io/trivy-repo/deb/public.key \
  | gpg --dearmor | sudo tee /usr/share/keyrings/trivy.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/trivy.gpg] https://aquasecurity.github.io/trivy-repo/deb $(lsb_release -sc) main" \
  | sudo tee /etc/apt/sources.list.d/trivy.list
sudo apt-get update
sudo apt-get install -y trivy

trivy --version
```

第一次使用前先更新弱點資料庫（之後 Trivy 每次掃描預設也會自動嘗試更新）：

```sh
trivy image --download-db-only
```

> 專案根目錄已經有一份 `deviceon.sbom.json`（由 `trivy-0.58.1` 產生的
> SPDX SBOM），可以當作「上一次掃描結果」的參考基準，用來比對新掃描是否有
> 新增/修補的套件版本。

---

## 2. 編譯專案

### 2.1 對齊 CI 的建置指令

CI（Azure Pipelines, `Gradle@2` task）實際執行的是：

```sh
./gradlew fatJar explodedWar
```

本地重現時，加上跟 CI 一致的 heap 設定：

```sh
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export GRADLE_OPTS="-Xmx3072m"

./gradlew fatJar explodedWar
```

- `fatJar`：`Worker/build.gradle` 與 `OTAWorker/build.gradle` 各自註冊了
  `fatJar` task，把 `runtimeClasspath` 上所有 jar 解壓後合併成一個 uber jar。
- `explodedWar`：定義在 `buildSrc/src/main/groovy/project-rules.gradle`，
  只會套用在有 `war` plugin 的 project（目前只有 `:portal` / `WebApp`），把
  `war` task 產生的 war 檔解壓縮到 `build/exploded`，保留原本個別 jar 檔
  （沒有被合併/改名）。

### 2.2 產物位置

```
Worker/build/libs/worker-<版本>.jar
OTAWorker/build/libs/provisioning-worker-<版本>.jar
WebApp/build/exploded/WEB-INF/lib/*.jar        # 個別、未合併的 jar
WebApp/build/libs/*.war                        # 只有跑 `war` task 才會產生
```

## 3. 用 Trivy 掃描 Gradle 相依套件

### 3.1 正式作法：收集建置產物到單一資料夾，用 `trivy rootfs` 掃

團隊實際採用的方式，是把 `./gradlew fatJar explodedWar` 產出的 **2 個 jar
+ 1 個 war**（跟 CI 的 `Gradle@2` step 註解「Build artifacts (2 jars, 1
war)」對應）集中複製到同一個資料夾，再對這個資料夾整包跑
`trivy rootfs`。Trivy 會遞迴解開 jar/war 內的巢狀套件，逐一比對 CVE
資料庫，這個結果最貼近「實際會被部署出去的東西」。

```sh
./gradlew fatJar explodedWar   # 順便產出 WebApp/build/libs/*.war（explodedWar 依賴 war task）

mkdir -p CustomJarDeviceOn
cp -v Worker/build/libs/*.jar     CustomJarDeviceOn/
cp -v OTAWorker/build/libs/*.jar  CustomJarDeviceOn/
cp -v WebApp/build/libs/*.war     CustomJarDeviceOn/

# backend
trivy rootfs --scanners vuln ./CustomJarDeviceOn
trivy rootfs --scanners vuln ./CustomJarDeviceOn --format json --output trivy-output.json

# frontend（若同時要掃 deviceon-webapp 這個獨立前端 repo）
trivy fs --scanners vuln ../deviceon-webapp
```

> 這份 RUN.md 只涵蓋 Backend；`deviceon-webapp` 是另一個獨立 repo，掃描
> 方式維持 `trivy fs`（直接掃原始碼 + lockfile，不需要先建置），列在這裡
> 只是為了跟原本的雙邊掃描腳本對齊。

### 3.2 已知限制：`fatJar` 內容可能讓部分套件漏判

`Worker/build.gradle` 的 `fatJar` task 內有這一行：

```groovy
exclude('META-INF/maven/**')
```

這會把每個依賴 jar 內的 `pom.properties` / `pom.xml` 一併排除。Trivy 的
Java 掃描器主要就是靠這些 `META-INF/maven/**/pom.properties` 檔案來判斷
「這個 class 屬於哪個 group:artifact:version」；一旦被排除，Trivy 對
`worker-<版本>.jar` 內被排除的那些套件只能退而用內建的 jar hash 資料庫做
模糊比對，涵蓋率較差，**可能漏掉真實存在的 CVE**。`OTAWorker` 的 `fatJar`
沒有排除 `META-INF/maven`，`WebApp` 的 war 內也是保留個別 jar（沒有合併），
相對可靠。

若懷疑 `trivy rootfs ./CustomJarDeviceOn` 對 `worker-<版本>.jar` 內某個
套件有漏判，可以額外對 Gradle 快取（涵蓋所有模組解析出來、未經合併的原始
jar）跑一次交叉驗證：

```sh
trivy fs --scanners vuln --format json \
  --output trivy-gradle-cache.json \
  ~/.gradle/caches/modules-2/files-2.1
```

> 這個目錄裡可能混有其他專案的快取，如果要嚴格只看這個 repo 的相依，
> 用第 4 節的 `dependencies` / `dependencyInsight` 指令列出的清單去交叉比對，
> 或是改用乾淨的 `GRADLE_USER_HOME`（例如 CI 容器）單獨跑一次
> `./gradlew fatJar explodedWar` 後再掃。

### 3.3 產生可程式化解析的 SBOM（銜接自動化修補）

```sh
trivy rootfs --format spdx-json --output deviceon.sbom.json ./CustomJarDeviceOn
# 或用 cyclonedx，兩者皆含 purl（pkg:maven/<group>/<artifact>@<version>），方便程式解析
trivy rootfs --format cyclonedx --output deviceon.cdx.json ./CustomJarDeviceOn
```

自動化腳本可以直接讀 JSON 內的 `vulnerabilities[]`（`trivy rootfs
--format json` 輸出）取得每個 CVE 對應的 `PkgName`、`InstalledVersion`、
`FixedVersion`，逐一餵給下一節的 `dependencyInsight` 去定位修補點。

---

## 4. 用 `dependencyInsight` 追查上游依賴關係

拿到 Trivy 回報的 `group:artifact:version` 後，用 `dependencyInsight` 確認
這個版本是「哪個模組」「透過哪條依賴鏈」被選中的，才知道該去哪裡改版號。

### 4.1 指令語法

```sh
./gradlew -q --console=plain :<project>:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency <group>:<artifact>[:<version>]
```

`<project>` 依前面第 0 節的對照表替換，例如：

```sh
# RMMLib 這個函式庫模組本身沒有直接宣告 jackson-databind，
# 但可能透過 aws-java-sdk-s3 / azure-* 等 SDK 間接帶入舊版本
./gradlew -q --console=plain :RMMLib:dependencyInsight \
  --configuration runtimeClasspath \
  --dependency com.fasterxml.jackson.core:jackson-databind:2.18.6

# 對照其他模組
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
建議對「Trivy 掃描結果中出現該套件的每個模組」都各跑一次。

### 4.2 如何解讀輸出

`dependencyInsight` 會輸出：

- **Selected/Requested reason**：這個版本是被誰要求的、又是被誰選中的
  （例如 conflict resolution：`By conflict resolution: between versions X and Y`）。
- **依賴路徑樹**：一路往上列出是哪一條 `implementation project(...)` /
  `api "group:artifact:version"` 把它拉進來的，最上層會停在某個
  `build.gradle` 裡實際寫死版號的那一行。

依輸出結果分兩種情況修補：

1. **直接依賴（某模組的 `build.gradle` 直接寫了這個 artifact）**：
   到根目錄 `build.gradle` 的 `ext { }` 區塊，找到對應的
   `ver_{Group}_{Module}` 變數（例如
   `ver_ComFasterxmlJacksonCore_JacksonDatabind`），升級到 Trivy 回報的
   `FixedVersion` 即可。目前本專案只有 `OTALib/build.gradle` 直接宣告了
   `jackson-databind`（透過 `ver_ComFasterxmlJacksonCore_JacksonDatabind`），
   其餘模組若解析出同一個套件，多半是下一種情況。

2. **間接依賴（依賴路徑樹最上層是別的 SDK，例如
   `aws-java-sdk-s3`、`azure-messaging-eventhubs` 等）**：
   單改根目錄 `ver_*` 版號不一定生效，因為 Gradle 的版本衝突解決策略
   （預設取最高版本）可能仍選到 SDK 內建的版本。這種情況要在**該模組**的
   `build.gradle` 明確加上約束，例如：

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

---

## 5. 自動化 CVE 修補流程（建議）

把前面幾節串起來，形成一條可重複執行、可放進 CI 的流程：

```
┌─────────────┐   ┌───────────────────┐   ┌────────────────────┐   ┌───────────┐   ┌───────────┐
│ ./gradlew   │──▶│ 收集 2 jars+1 war  │──▶│ 逐一針對每個 CVE   │──▶│ 依 4.2 規則 │──▶│ 重新編譯 + │
│ fatJar      │   │ 至 CustomJarDeviceOn│  │ 對受影響模組跑     │   │ 修改      │   │ 重新掃描  │
│ explodedWar │   │ 跑 trivy rootfs    │   │ dependencyInsight  │   │ build.gradle│   │ 直到 CVE  │
└─────────────┘   └───────────────────┘   └────────────────────┘   └───────────┘   │ 消失      │
                                                                                     └───────────┘
```

概念性腳本（可再包成 CI job）：

```sh
#!/usr/bin/env bash
set -euo pipefail

./gradlew fatJar explodedWar

rm -rf CustomJarDeviceOn && mkdir -p CustomJarDeviceOn
cp -v Worker/build/libs/*.jar     CustomJarDeviceOn/
cp -v OTAWorker/build/libs/*.jar  CustomJarDeviceOn/
cp -v WebApp/build/libs/*.war     CustomJarDeviceOn/

trivy rootfs --scanners vuln --format json --output trivy-report.json \
  ./CustomJarDeviceOn

# 解析出 (PkgName, InstalledVersion) 清單，逐一定位
jq -r '.Results[].Vulnerabilities[]? | "\(.PkgName) \(.InstalledVersion) \(.FixedVersion)"' \
  trivy-report.json | sort -u | while read -r pkg version fixed; do
    echo "=== ${pkg}:${version} → 建議修補至 ${fixed} ==="
    for project in RMMLib worker portal ota-lib provisioning-worker; do
      ./gradlew -q --console=plain ":${project}:dependencyInsight" \
        --configuration runtimeClasspath \
        --dependency "${pkg}:${version}" || true
    done
done
```

實際落地時建議：

1. 上述迴圈的輸出交給人審（或先用規則過濾「有 FixedVersion 且非 major
   版本跳號」的項目）再自動改 `build.gradle` 版號 / 加 `constraints`。
2. 改完後重跑 `./gradlew fatJar explodedWar` + `trivy fs`，比對新舊
   `trivy-report.json`，確認該 CVE 已從清單消失、且沒有新增其他 CVE
   （版號升級可能引入新問題）。
3. 跑一次既有測試（若專案有）與 `./gradlew build` 確保沒有 API 不相容。
4. 用修補後的 diff 自動開 PR，附上 Trivy 前後掃描結果與
   `dependencyInsight` 輸出作為修補依據，交由人工 review 合併。
5. 把「掃描 + 檢查有無新增高風險 CVE」這一段接進既有 Azure Pipeline
   （`scripts/devops/azure-pipelines/staging-pushed.yml`）的
   `Gradle@2` 建置步驟之後，作為 CI gate。

---

## 6. 常見問題

- **`java: command not found` / 版本不是 17**：確認 `JAVA_HOME` 指到 JDK 17，
  且 `PATH` 有包含 `$JAVA_HOME/bin`。
- **`dependencyInsight` 找不到指定 dependency**：代表該版本在該模組的
  `runtimeClasspath` 上根本沒被解析到，換一個模組再試，或去掉版號只留
  `--dependency <group>:<artifact>` 看實際被解析成哪個版本。
- **`trivy rootfs ./CustomJarDeviceOn` 掃出的 CVE 數量比對 Gradle 快取
  掃描結果少**：符合預期，代表 `worker-<版本>.jar` 內因 `exclude('META-INF/maven/**')`
  被排除 `pom.properties` 的套件發生漏判，可用 3.2 節的 Gradle 快取交叉驗證
  找出差異。
- **改了 `ver_*` 版號但 `dependencyInsight` 顯示的版本沒變**：多半是被其他
  上游 SDK 用更高優先權帶入（conflict resolution 選了別的版本），照
  4.2 節第 2 種情況加 `constraints` 或升級該上游 SDK。
