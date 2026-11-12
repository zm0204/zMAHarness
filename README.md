# zMAHarness

zMAHarness 是以 Codex Desktop 主控 Agent 為核心的 **Single-Agent First** 工作流。Sol 平常自己完成任務。遇到獨立審查有幫助的關鍵變更時，Sol 會提出協作建議，等你在對話中一鍵同意，再調動 Worker。Sol 會獨立核對結果，決定採納哪些建議，並繼續完成原任務。

| Worker | 目前支援 |
| --- | --- |
| Gemini | `agy-print` 與 `codex_prompt` 預設啟用。已實測對話核准、派工和收據離線重收集。 |
| Claude | 保留為 Worker 路由目標，尚未驗收真實派工。 |
| Codex | 已驗證巢狀 CLI 的基本輸出。正式 `codex-exec` 派工尚未接入。 |
| Terra GUI | 保留 V1 的檔案交接規範與離線模擬。日常 CLI 派工走命令列。 |

## 三步快速開始

需要 **Windows 11、Git、PowerShell 5.1 或 7、Python 3.11 以上**。請將要使用的 `python.exe` 加入 PATH，並保留 Python 的使用者套件目錄功能。

```powershell
# 1. 下載並進入專案
git clone https://github.com/zm0204/zMAHarness.git
Set-Location zMAHarness

# 2. 安裝到目前 Windows 使用者
.\setup.ps1

# 3. 檢查環境
.\doctor.ps1
```

初始化沿用 V1 的 `.local/settings.json` 與工作目錄，並建立 V2 本機設定。它會將引擎路徑以 ASCII 跳脫格式註冊到目前 Python 的使用者套件目錄 `zmaharness.pth`，可支援中文 clone 路徑，並將 Skill 安裝到 `%USERPROFILE%\.agents\skills\harness\SKILL.md`。重跑會保留現有 TOML 和 V1 本機設定，並更新引擎註冊及 Skill。安裝只需要一般使用者權限，Python 端只用標準函式庫。

日常使用另需 [Codex Desktop](https://openai.com/codex/)；要使用 Gemini Worker，請從 [Google 官方文件](https://antigravity.google/docs/install?tab=cli) 安裝 Antigravity CLI，並自行登入。Doctor 會檢查 CLI 匯入、`agy.exe` 的安裝線索、本機 TOML 與 Skill。實際登入、模型可用性及桌面載入由使用者確認。Doctor 可離線執行。

## 在任意專案協作

安裝後，在 Codex Desktop 開啟要工作的 Git 專案，例如 BatchTable，然後開始新對話。可以輸入 `$harness` 或直接交辦原本的開發工作。符合協作條件時，Sol 會顯示 **【zMAHarness 協作建議】**，列出觸發原因、Worker、任務類型、權限與協作理由。

- **不同意**：Sol 立即自己繼續原任務，同一問題不會反覆詢問。
- **繼續協作**：Sol 在背景準備資料與單次 Grant，派工後獨立驗收，並採納有根據的建議。你只需要在對話中選擇。

`python -B -m mah --help` 可在其他專案目錄執行。`--repo` 和派工卡的 `repo_root` 都應指向**目前工作的目標 Git 根目錄**。zMAHarness clone 目錄是 Python 引擎的位置。

| 審查模式 | 適用情況 |
| --- | --- |
| **Context-Bundle** | 1～2 個核心檔案能完整封裝在預設 32 KB 的任務上限內。可以審查未提交的完整檔案。 |
| **Disposable Worktree** | 跨檔、大檔或需要追查關聯檔案。快照納入未提交變更與符合規則的新檔，必要時只在分離 HEAD 上建立本機匿名提交。 |

兩種模式都會綁定核准範圍並保存收據。驗收成功後仍由 Sol 決定是否採納。遇到送出狀態不明，先查現有收據與結果。工作者與主控使用同一 Windows 帳號，唯讀限制依賴 CLI 的會話設定，工具行為未獲 OS 層完整驗證。詳見 [協作規範](.agents/skills/harness/SKILL.md) 和 [使用風險](DISCLAIMER.md)。

## 本機設定與故障排除

- **設定位置**：`%LOCALAPPDATA%\zMAHarness\harness.local.toml`，預設子目錄為 `state`、`worktrees`、`gui`。日常 `agy-print` 使用 CLI，會話固定停用 AI Credits，略過派工前後的額度查詢。
- **既有安裝**：新設定尚未建立時，`mah` 會退回舊的 `%LOCALAPPDATA%\MultiAgentHarness\harness.local.toml`（若存在）。執行新版初始化後會優先使用新設定。舊 Ledger 與 Run 保留在原處，查詢或收尾舊 Run 時在子命令前指定 `--config <舊設定檔>`。
- **其他專案找不到 `mah`**：確認該終端與安裝時使用相同的 Python，並啟用 Python 使用者套件目錄。移動 clone 或更換 Python 後請重跑安裝。也可暫時將引擎根目錄加入 `PYTHONPATH`。
- **找不到 Skill**：重開 Codex Desktop 或建立新對話，再輸入 `$harness`。
- **找不到 `agy`**：Doctor 會找 PATH 或標準安裝位置 `%LOCALAPPDATA%\agy\bin\agy.exe`。自訂安裝可在本機 TOML 指定 `agy_command`。
- **PowerShell 無法執行腳本**：確認來源與所在機器的執行原則。若允許，可對單次程序執行 `powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup.ps1`，並以同樣方式執行 Doctor。

## 離線驗證

在 zMAHarness clone 根目錄執行以下命令。測試使用合成資料及本機替身。

```powershell
python -B -m unittest -q
python -B -m unittest discover -s tests/v2 -q
```

V1 的檔案交接與中斷復原規則見 [角色規範](ROLES.md) 和 [V1 盤點](docs/v2/V1_INVENTORY.md)。請讓本機設定、SQLite、任務紀錄及私人驗證報告留在本機。Git 白名單只納入專案程式、通用文件與已去識別化的測試資料。
