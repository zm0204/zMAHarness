English | [README.en.md](README.en.md)

# zMAHarness

zMAHarness 是一套 Skill 加上 CLI 工具，讓你的 AI Agent（Codex 或 Claude Code）在你同意後找其他模型協助審查、提供第二意見或研究，並由主控自己驗收結果。

zMAHarness 採用 **Single-Agent First** 工作流。主控 Agent（日常預設 Codex Sol High）平常自行完成任務。遇到獨立審查有幫助的關鍵變更時，主控會提出協作建議，等你在對話中同意後才派工。主控會獨立核對 Worker 的結果，決定採納哪些建議，並繼續完成原任務。

## 適用環境

- **作業系統**　Windows 11。
- **主控**　[Codex Desktop](https://openai.com/codex/) 或 [Claude Code](https://code.claude.com/docs/) 二選一。
- **Worker**　從 [Google 官方文件](https://antigravity.google/docs/install?tab=cli) 安裝 Antigravity CLI（`agy`），並使用個人 Google AI Pro 訂閱登入。
- **本機工具**　Git、PowerShell 5.1 或 7、Python 3.11 以上。請將要使用的 `python.exe` 加入 PATH，並啟用 Python 使用者套件目錄。

可在一般 PowerShell 終端機執行 `winget install -e --id Python.Python.3.13`，或從 python.org 安裝並勾選 **Add python.exe to PATH**。如果 `python.exe` 指向 `Microsoft\WindowsApps` 且無法執行，那是 Microsoft Store 的假捷徑，不是真的 Python；請到「設定 → 應用程式 → 進階應用程式設定 → 應用程式執行別名」關閉 `python.exe` 和 `python3.exe`，重開終端機後再執行安裝。

## Worker 驗收狀態

| Worker | 驗收範圍 |
| --- | --- |
| Gemini | **真實派工**：T5 觸發 BatchTable 跨專案 Second Opinion，以 Context-Bundle 模式經 `agy-print` 請求 `gemini-3.8-flash-high · high`。主控獨立查核後採納建議，帳本記錄為 `SUCCEEDED`／`ACCEPTED`。**離線測試**：以 Flash Low Review 的去識別化真實收據驗證重收集與 Verifier。其他模型與思考程度、Research 模板及 Disposable Worktree 的真實端到端流程尚未驗收。 |
| Claude | Sonnet、Opus 已列為 `agy-print` 的候選模型，真實 Worker 派工尚未驗收。 |
| Codex | 已驗證巢狀 CLI 的 JSONL、Thread ID 和 UTF-8。該次工具操作遭政策拒絕，正式 `codex-exec` Adapter 尚未接入或驗收。 |
| Terra GUI | 曾以隔離環境實測 V1 檔案交接，V2 正式 GUI 派工尚未驗收。 |

Skill 所列模型 ID 以 2026 年 11 月的 `agy models` 為準，日後可能隨 CLI 或帳號可用模型變動。

## 三步快速開始

請在一般的 PowerShell 終端機執行 `setup.ps1`，不要透過 AI Agent 的受限沙盒執行；安裝需要寫入目前使用者的 Python 套件目錄與技能目錄。

```powershell
# 1. 下載並進入專案
git clone https://github.com/zm0204/zMAHarness.git
Set-Location zMAHarness

# 2. 安裝到目前 Windows 使用者
.\setup.ps1

# 3. 檢查環境
.\doctor.ps1
```

初始化沿用 V1 的 `.local/settings.json` 與工作目錄，並建立 V2 本機設定。它會將引擎路徑以 ASCII 跳脫格式註冊到目前 Python 的使用者套件目錄 `zmaharness.pth`，可支援中文 clone 路徑。完整 Skill（含 `references/`）會同步安裝到以下兩處。

- `%USERPROFILE%\.agents\skills\zmaharness\`
- `%USERPROFILE%\.claude\skills\zmaharness\`

重跑會保留現有 TOML 和 V1 本機設定，並更新引擎註冊及兩處 Skill。安裝只需要一般使用者權限，Python 端只用標準函式庫。Doctor 可離線檢查 CLI 匯入、`agy.exe` 的安裝線索、本機 TOML 與兩處 Skill 的一致性。實際登入、模型可用性及主控介面的 Skill 載入需在使用時確認。

Skill 舊名為 `harness`。重跑安裝時，若上述兩處的舊 `harness` 目錄確認是本專案的舊安裝，會自動移除；同名但不屬於本專案的 Skill 一律保留，只顯示警告。

## 在任意專案協作

安裝後，用選定的主控開啟要工作的 Git 專案。Codex Desktop 用 `$zmaharness` 呼叫，Claude Code 用 `/zmaharness`。也可以直接交辦開發工作，符合協作條件時，主控會在對話中顯示 **【zMAHarness 協作建議】**，列出觸發原因、建議模型、任務與範圍、授權限制及協作理由。卡片和回覆使用你提問的語言，品牌、模型 ID 與指令名保留原文。

出卡前，主控會先執行唯讀的 `mah preflight`，確認目標是本機 Git 專案、本機設定存在、Worker 已啟用且找得到執行檔。檢查不通過時不出卡，主控會說明原因並自己繼續原任務。若訊息是「不是 Git 專案」或「無法檢查 Git 專案」，也可能是因為目標不在主控目前的工作資料夾內，或被沙盒限制存取；請在該專案開啟主控後再試。

- **不同意**　主控立即自行繼續原任務，同一問題不會反覆詢問。
- **繼續協作**　可選擇建議模型、備選模型，或手打調整模型、思考程度及縮小檔案範圍。主控依你的選擇準備資料與單次 Grant，派工後獨立驗收並決定是否採納。

`python -B -m mah --help` 可在其他專案目錄執行。`--repo` 和派工卡的 `repo_root` 都應指向**目前工作的目標 Git 根目錄**。zMAHarness clone 目錄是 Python 引擎的位置。

| 審查模式 | 適用情況 |
| --- | --- |
| **Context-Bundle** | 1～2 個核心檔案能完整封裝在預設 32 KB 的任務上限內。可以審查未提交的完整檔案。 |
| **Disposable Worktree** | 跨檔、大檔或需要追查關聯檔案。快照納入未提交變更與符合規則的新檔，必要時只在分離 HEAD 上建立本機匿名提交。 |

兩種模式都會綁定核准範圍並保存收據。驗收成功後仍由主控決定是否採納。遇到送出狀態不明，先查現有收據與結果。Worker 與主控使用同一 Windows 帳號，唯讀限制依賴 CLI 的會話設定，工具行為未獲 OS 層完整驗證。詳見 [協作規範](.agents/skills/zmaharness/SKILL.md) 和 [使用風險](DISCLAIMER.md)。

## 本機設定與故障排除

- **設定位置**：`%LOCALAPPDATA%\zMAHarness\harness.local.toml`，預設子目錄為 `state`、`worktrees`、`gui`。`agy-print` 和對話核准（`codex_prompt`）預設啟用。CLI 會話固定停用 AI Credits，略過派工前後的額度查詢。
- **既有安裝**：新設定尚未建立時，`mah` 會退回舊的 `%LOCALAPPDATA%\MultiAgentHarness\harness.local.toml`（若存在）。執行新版初始化後會優先使用新設定。舊 Ledger 與 Run 保留在原處，查詢或收尾舊 Run 時在子命令前指定 `--config <舊設定檔>`。
- **其他專案找不到 `mah`**：確認該終端與安裝時使用相同的 Python，並啟用 Python 使用者套件目錄。移動 clone 或更換 Python 後請重跑安裝。也可暫時將引擎根目錄加入 `PYTHONPATH`。
- **找不到 Skill**：重開主控應用程式或建立新對話，再於 Codex Desktop 輸入 `$zmaharness`，或於 Claude Code 輸入 `/zmaharness`。
- **找不到 `agy`**：Doctor 會找 PATH 或標準安裝位置 `%LOCALAPPDATA%\agy\bin\agy.exe`。自訂安裝可在本機 TOML 指定 `agy_command`。
- **PowerShell 無法執行腳本**：確認來源與所在機器的執行原則。若允許，可對單次程序執行 `powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup.ps1`，並以同樣方式執行 Doctor。

### 在 Codex 沙盒中使用

Codex 在 Windows 的 workspace-write 沙盒會把 `%LOCALAPPDATA%` 重新導向到 `...\Packages\<套件名稱>\AC\...`，但 `%USERPROFILE%` 不變。zMAHarness 會自動辨識這種重新導向，改從 `%USERPROFILE%\AppData\Local` 讀取設定與帳本，Doctor 也會顯示目前使用的路徑來源。

沙盒只允許寫入工作資料夾，因此派工必須在沙盒外執行。出卡前的 `mah preflight` 偵測到沙盒時，卡片的授權限制會註明「派工時需核准一次沙盒外執行」，派工時 Codex 會請你核准一次。若仍在沙盒內派工，`mah dispatch` 會在建立 Run、消耗 Grant 之前以結束碼 14 停止。

也可以設定環境變數 `ZMAHARNESS_HOME` 指定 zMAHarness 的資料夾（取代 `%LOCALAPPDATA%\zMAHarness`），必須是絕對路徑。設定後 `setup.ps1`、Doctor 與 `mah` 都只使用該位置，不會退回舊的 `MultiAgentHarness` 路徑。`state` 和 `worktrees` 預設會建在它底下，所以它不能位於任何 Git 工作區內（包括 zMAHarness clone 本身），也不能位於雲端同步資料夾內（例如 OneDrive），否則 CLI 會拒絕載入設定。

### 選填的輕量主控設定

```toml
[controller]
light_models = []
suggested_controller = "Codex Sol High"
```

`light_models` 是模型 ID 字串清單，預設空清單。主控確認目前模型 ID 與其中一項完全相符時，才會在 T1、T2 或 T5 的卡片提供輕量主控選項。`suggested_controller` 是建議切換到的主控名稱，預設為 `Codex Sol High`，切換由你在主控介面完成。

## 路線圖

以下項目**規劃中，尚未實作**。

2) Adapter 介面重構。
3) `codex-exec`（GPT）與 `claude-print`（Claude CLI）Worker。
4) OpenAI 相容 API Worker（DeepSeek、Ollama 等本地模型）。僅支援 Context-Bundle，需使用者自行提供 API 金鑰。

## 離線驗證

在 zMAHarness clone 根目錄執行以下命令。測試使用合成資料及本機替身。

```powershell
python -B -m unittest -q
python -B -m unittest discover -s tests/v2 -q
```

若 Windows 未開啟開發人員模式，且一般使用者沒有建立符號連結的權限，相關測試會顯示為略過，屬於正常現象；有權限的環境仍會執行完整斷言。

V1 的檔案交接與中斷復原規則見 [角色規範](ROLES.md) 和 [V1 盤點](docs/v2/V1_INVENTORY.md)。請讓本機設定、SQLite、任務紀錄及私人驗證報告留在本機。Git 白名單只納入專案程式、通用文件與已去識別化的測試資料。

## 開發方式

本專案由作者負責需求定義、架構設計與驗收標準，並以多個 AI 協作實作：

- **Codex（GPT）**：主要實作與測試。
- **Claude**：部分實作、規格設計與獨立驗收。
- **Gemini**：架構審查，並以 Worker 身分參與真實派工審查。

每個階段都經過獨立審查與測試驗收後才合併。本專案本身就是 zMAHarness「主控負責到底、協作需經同意、結果需獨立驗收」理念的實踐。

授權：MIT，見 [LICENSE](LICENSE)。同時請參閱 [DISCLAIMER.md](DISCLAIMER.md)。
