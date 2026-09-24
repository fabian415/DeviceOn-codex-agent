# ENV_SETUP.md — 環境建置

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

### 1.4 安裝 Codex CLI

```sh
# 透過 npm 安裝（需先安裝 Node.js / npm）
npm install -g @openai/codex

# 或使用 Homebrew
# brew install codex

codex --version
```

### 1.5 設定 Codex 使用 Azure OpenAI

編輯（不存在則建立）`~/.codex/config.toml`，加入以下設定：

```toml
model = "gpt-5.3-codex"
model_provider = "azure"
model_reasoning_effort = "medium"

[model_providers.azure]
name = "Azure OpenAI"
base_url = "https://fabian-test-2.openai.azure.com/openai/v1"
env_key = "AZURE_OPENAI_API_KEY"
wire_api = "responses"

[projects."/home/advantech/workspace"]
trust_level = "trusted"
```

說明：

- `model_provider = "azure"` 搭配 `[model_providers.azure]` 區塊，指定 Codex
  改用 Azure OpenAI 端點，而非預設的 OpenAI API。
- `env_key = "AZURE_OPENAI_API_KEY"` 表示 Codex 執行時會從這個環境變數讀取
  API Key，因此需要另外在 shell 設定該變數（不要把 key 直接寫進
  `config.toml`）：

  ```sh
  export AZURE_OPENAI_API_KEY="<your-azure-openai-api-key>"
  # 建議寫進 ~/.bashrc 或 ~/.zshrc，讓每次開新 shell 都自動載入
  echo 'export AZURE_OPENAI_API_KEY="<your-azure-openai-api-key>"' >> ~/.bashrc
  ```

- `[projects."/home/advantech/workspace"]` 加上 `trust_level = "trusted"`，
  代表此專案路徑已標記為信任，Codex 在此目錄下執行指令時不會每次都詢問確認。

設定完成後可執行以下指令確認 Codex 能正確連線 Azure OpenAI：

```sh
codex
```

### 1.6 安裝 Azure CLI (`az`)

`run-pipeline.sh` 需要用 `az pipelines` 系列指令跨 VM 觸發 pipeline、查詢執行
狀態與下載 artifacts，因此本機需要安裝 Azure CLI。以下同樣以 Ubuntu 24.04 的
官方 apt repo 安裝方式為例：

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl apt-transport-https lsb-release gnupg

sudo mkdir -p /etc/apt/keyrings
curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
  | gpg --dearmor | sudo tee /etc/apt/keyrings/microsoft.gpg > /dev/null
sudo chmod go+r /etc/apt/keyrings/microsoft.gpg

AZ_DIST=$(lsb_release -cs)
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ ${AZ_DIST} main" \
  | sudo tee /etc/apt/sources.list.d/azure-cli.list

sudo apt-get update
sudo apt-get install -y azure-cli

az version
```

安裝好 CLI 本體後，還需要 `azure-devops` extension 才有 `az pipelines` 指令
（`run-pipeline.sh` 執行時若偵測到沒裝會自動嘗試安裝，這裡也可以手動先裝好）：

```sh
az extension add --name azure-devops
az extension list -o table   # 確認列表中有 azure-devops
```

> 這個 extension 支援用環境變數 `AZURE_DEVOPS_EXT_PAT` 帶入 PAT 做非互動式
> 認證（不需要 `az login`），`run-pipeline.sh` 會在執行時自動從 `.env` 的
> `AZURE_DEVOPS_PAT` 匯出這個變數，不需要另外手動 `az login`。

### 1.7 註冊 Azure Pipeline

`.env` 裡的 `AZURE_PIPELINE_NAME` 對應的是 Azure DevOps 上「Pipeline 定義」
的名稱，不是 YAML 檔本身的內容，需要先在 Azure DevOps 建立一次。若 pipeline
尚未建立，可用 `az pipelines create` 指定名稱與 YAML 路徑一次建好：

```sh
set -a
source ~/workspace/DeviceOn_Backend/.env
set +a
export AZURE_DEVOPS_EXT_PAT="$AZURE_DEVOPS_PAT"

az pipelines create \
  --name "DeviceOn-Backend-CVE-Loop" \
  --folder-path "\\DeviceOn Release" \
  --organization "https://dev.azure.com/wise-deviceon" \
  --project "DeviceOn Core" \
  --repository "Backend" \
  --repository-type tfsgit \
  --branch master \
  --yml-path scripts/devops/azure-pipelines/gradle-build-and-trivy-scan.yml \
  --skip-first-run true
```

建立完成後，把 `--name` 填的值原封不動填進 `.env` 的 `AZURE_PIPELINE_NAME=`：

```sh
AZURE_PIPELINE_NAME=DeviceOn-Backend-CVE-Loop
```

> 注意：`.env` 是用 `source` 載入的，值有空白時一定要加引號，例如
> `AZURE_DEVOPS_PROJECT="DeviceOn Core"`。沒加引號的話，bash 會把 `Core`
> 當成指令執行，變數會是空的。`cve-loop.sh` 組 URL 時會自動把空白編碼成 `%20`。

`cve-loop.sh` 之後會用這個名稱透過 `az pipelines run --name` 觸發對應的
pipeline，兩邊名稱必須完全一致。

## 2. 啟動方式
執行以下指令:
```cmd=
export AZURE_OPENAI_API_KEY='eeee'
cd ~/workspace
codex --yolo "請根據專案底下的 SKILL.md 流程，開始進行AI自動化修補CVEs漏洞。"
```

在 codex Prompt 裡，輸入
```
請根據專案底下的 SKILL.md 流程，開始進行AI自動化修補CVEs漏洞。
```

## 3. 驗證是否編譯成功?
到專案底下後，執行以下指令
```cmd=
./gradlew fatJar explodedWar
rm -rf CustomJarDeviceOn && mkdir -p CustomJarDeviceOn
cp -v Worker/build/libs/*.jar     CustomJarDeviceOn/
cp -v OTAWorker/build/libs/*.jar  CustomJarDeviceOn/
cp -v WebApp/build/libs/*.war     CustomJarDeviceOn/
trivy rootfs --scanners vuln ./CustomJarDeviceOn
```

