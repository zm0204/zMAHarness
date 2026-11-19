English | [README.en.md](README.en.md)

# zMAHarness

zMAHarness 是一套 Skill 加上 CLI 工具，讓你的 AI Agent（Codex 或 Claude Code）在你同意後找其他模型協助審查、提供第二意見或研究，並由主控自己驗收結果。

zMAHarness 採用 **Single-Agent First** 工作流。主控 Agent（日常預設 Codex Sol High）平常自行完成任務。遇到獨立審查有幫助的關鍵變更時，主控會提出協作建議，等你在對話中同意後才派工。主控會獨立核對 Worker 的結果，決定採納哪些建議，並繼續完成原任務。

## 適用環境

- **作業系統**　Windows 11。
- **主控**　[Codex Desktop](https://openai.com/codex/) 或 [Claude Code](https://code.claude.com/docs/) 二選一。
- **Gemini Worker（預設）**　從 [Google 官方文件](https://antigravity.google/docs/install?tab=cli) 安裝 Antigravity CLI（`agy`），並使用個人 Google AI Pro 訂閱登入。可用的模型分級與帳號實際可用清單以 `python -B -m mah models` 為準。`gemini-pro-orchestrator-` 開頭的 Orchestrator 模型會自行派出子代理，`--settings` 的 deny 規則能否約束子代理尚未驗證，zMAHarness 一律拒絕（出卡與派工皆為結束碼 2）。
- **Claude Worker（選用）**　以 `npm install -g @anthropic-ai/claude-code` 安裝 Claude Code CLI，再執行 `claude auth login`（或執行 `claude` 依畫面）以 Claude 帳號登入。zMAHarness 啟動 Worker 時不傳入任何 API 金鑰變數（移除所有 `ANTHROPIC_` 開頭的變數），出卡前也會以 `claude auth status` 確認 CLI 是以 Claude 訂閱帳號登入；未登入、登入已到期，或使用 API 金鑰、`apiKeyHelper`、第三方供應商時都不出卡。在這兩項檢查下，Worker 會使用你的 Claude 訂閱，跟你平常使用 Claude 共用同一份用量上限；實際計費仍以 Anthropic 的帳號設定為準。你自己在終端機執行 `claude` 時不受這些檢查保護。殘餘風險：使用者層級的 `~/.claude/CLAUDE.md` 與 `~/.claude/settings.json` 裡的 hooks 等使用者設定仍會載入 Worker，無法以參數關閉；請保持這些設定不含會寫檔、連網或改變指示的內容。zMAHarness 只啟動套件內的原生 `claude.exe`，不經由 npm 的 `claude.cmd` 轉呼。
- **GPT Worker（選用）**　以 `npm install -g @openai/codex` 安裝 Codex CLI，再執行 `codex login` 以 ChatGPT 帳號登入。zMAHarness 啟動 Worker 時不傳入任何 API 金鑰變數（移除所有 `OPENAI_` 開頭的變數，以及 `CODEX_HOME` 以外的 `CODEX_` 變數），出卡前也會以 `codex login status` 確認 CLI 是以 ChatGPT 帳號登入；未登入或以 API 金鑰登入時都不出卡。在這兩項檢查下，Worker 會使用你的 ChatGPT 訂閱，跟你平常使用 Codex 共用同一份用量上限（`codex-exec` 與 Codex Desktop 共用同一份 ChatGPT 額度，Desktop 用完時 Worker 也會失敗）；實際計費仍以 OpenAI 的帳號設定為準。執行期間只要 JSONL 出現工具項目，該次結果一律判為不可驗收。`codex-exec` **只支援 Context-Bundle**。GPT code-mode 模型可能看見 `functions.exec`、`functions.wait`，兩者的執行端必須停用。每次派工前都會自動探測工具宣告，新版 CLI 註冊的未知工具會被拒絕。帳號實際可用的 GPT 模型與思考程度以 `python -B -m mah models` 為準（探測細節見 [Adapter 說明](docs/ADAPTERS.md#codex-exec)）。若 Codex 日後提供正式零工具入口，將改用該入口。Codex 仍可能載入全域 `AGENTS.md` 或在正式執行時刷新模型目錄，詳見 [Adapter 說明](docs/ADAPTERS.md#codex-exec)。最後實測版本是 codex-cli 0.169.0（見 [smoke 紀錄](docs/v2/CODEX_SMOKE_TEST.md)）；不鎖定版本，升級後由派工前的功能與工具宣告檢查把關，不合格就拒絕派工。zMAHarness 只啟動套件內的原生 `codex.exe`，不經由 npm 的 `codex.cmd` 轉呼。
- **本機工具**　Git、PowerShell 5.1 或 7、Python 3.11 以上。請將要使用的 `python.exe` 加入 PATH，並啟用 Python 使用者套件目錄。

可在一般 PowerShell 終端機執行 `winget install -e --id Python.Python.3.13`，或從 python.org 安裝並勾選 **Add python.exe to PATH**。如果 `python.exe` 指向 `Microsoft\WindowsApps` 且無法執行，那是 Microsoft Store 的假捷徑，不是真的 Python；請到「設定 → 應用程式 → 進階應用程式設定 → 應用程式執行別名」關閉 `python.exe` 和 `python3.exe`，重開終端機後再執行安裝。

## Worker 驗收狀態

| Worker | 驗收範圍 |
| --- | --- |
| Gemini | **真實派工**：T5 觸發 作者的實際 Python 專案（表格比對工具）跨專案 Second Opinion，以 Context-Bundle 模式經 `agy-print` 請求 `gemini-3.8-flash-high · high`。主控獨立查核後採納建議，帳本記錄為 `SUCCEEDED`／`ACCEPTED`。**離線測試**：以 Flash Low Review 的去識別化真實收據驗證重收集與 Verifier。其他模型與思考程度、Research 模板的真實端到端流程尚未驗收；Disposable Worktree 的真實端到端流程已由 Claude（`agy-print`）驗收。 |
| Claude（`agy-print`） | **真實派工已驗收**：`claude-sonnet-5-5-medium`，Disposable Worktree，Review，`SUCCEEDED`／`ACCEPTED`（2026-11-18），審查對象是 zMAHarness 本身。agy 搭配 Claude 時會在 JSON 前面加上說明文字，自 `28d8a9a` 起允許這種輸出。 |
| Claude（`claude-print`） | **真實派工已驗收**：作者的實際 Python 專案（表格比對工具），`sonnet · medium` 與 `opus · medium`。兩筆皆為 Context-Bundle Review，`SUCCEEDED`／`ACCEPTED`。Opus 於 2026-11-15 驗收，Worker 回報的模型為 `claude-opus-5-5`。 |
| GPT（`codex-exec`） | **真實派工已驗收**：作者的實際 Python 專案（表格比對工具），主控 Claude Code，`gpt-5.6-terra · medium`，Context-Bundle，`SUCCEEDED`／`ACCEPTED`；`gpt-6.2-sol · low` 與 `gpt-6.2-sol · medium`，Context-Bundle，`SUCCEEDED`／`ACCEPTED`（2026-11-19）。GPT 預設建議為 `gpt-6.2-sol`；`gpt-6.1-terra` 改為可指定，它唯一一次真實派工在 900 秒內沒有任何輸出而逾時。只支援 Context-Bundle。 |
| Terra GUI | 曾以隔離環境實測 V1 檔案交接，V2 正式 GUI 派工尚未驗收。 |

可用的模型目錄、帳號偵測結果與額度狀態以 `python -B -m mah models`（加 `--all` 列出未分級模型、`--refresh` 略過快取）為準。

## 名詞

| 名詞 | 意思 |
| --- | --- |
| 主控 | 你平常對話的 AI Agent（Codex Desktop 或 Claude Code）。它負責原任務、提出協作建議，並驗收 Worker 的結果。 |
| Worker | 被派工的另一個模型，經由本機 CLI（`agy`、`claude`、`codex`）執行，只做唯讀分析。 |
| Adapter | zMAHarness 啟動某一種 Worker CLI 的模組：`agy-print`（Gemini）、`claude-print`（Claude）、`codex-exec`（GPT）。 |
| 卡片 | 主控在對話中顯示的協作建議，列出模型、任務與範圍，等你同意。 |
| Grant | 你同意一張卡片後產生的授權紀錄，綁定範圍與派工次數。 |
| Run | 一次派工。每個 Run 有自己的 Run ID、收據與結果。 |
| 帳本（Ledger） | 本機 SQLite 資料庫，記錄卡片、Grant、Run 與驗收狀態。 |
| T1～T5 | 主控提出協作建議的五種觸發條件：T1 同一錯誤多次修正仍失敗；T2 變更觸及高風險路徑且差異行數多；T3 需要大量閱讀但產出很小；T4 主控額度使用率偏高；T5 你主動要求協作。門檻在本機 TOML 的 `[triggers]`。 |

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

初始化會在 clone 目錄內建立 `.local/settings.json` 與工作目錄（V1 檔案交接流程使用，已存在時沿用，不會進入 Git），並建立本機設定 `%LOCALAPPDATA%\zMAHarness\harness.local.toml`。它會將引擎路徑以 ASCII 跳脫格式註冊到目前 Python 的使用者套件目錄 `zmaharness.pth`，可支援中文 clone 路徑。完整 Skill（含 `references/`）會同步安裝到以下兩處。

- `%USERPROFILE%\.agents\skills\zmaharness\`
- `%USERPROFILE%\.claude\skills\zmaharness\`

重跑會保留現有 TOML 和 V1 本機設定，並更新引擎註冊及兩處 Skill。安裝只需要一般使用者權限，Python 端只用標準函式庫。Doctor 可離線檢查 CLI 匯入、`agy.exe` 的安裝線索、本機 TOML 與兩處 Skill 的一致性。實際登入、模型可用性及主控介面的 Skill 載入需在使用時確認。

Doctor 的每一行以 `[通過]`、`[警告]`、`[需人工驗證]` 或 `[失敗]` 開頭。只有 `[失敗]` 會讓結束碼變成 1；`[需人工驗證]` 是 Doctor 無法離線確認的項目（例如登入狀態），不代表安裝有問題。沒有安裝的選用 Worker 會顯示為 `[需人工驗證]`，不使用該 Worker 時可以忽略。輸出最後的「V1 檔案交接（舊版相容）」段落只與舊的 GUI 檔案交接流程有關。

舊安裝的本機 TOML 會保留原本的 `adapters.enabled`，不會自動加入新的 Worker。要使用 Claude Worker，請手動把 `"claude-print"` 加進 `adapters.enabled`；Doctor 找到 claude 執行檔但尚未啟用時會提示寫法。

Skill 舊名為 `harness`。重跑安裝時，若上述兩處的舊 `harness` 目錄確認是本專案的舊安裝，會自動移除；同名但不屬於本專案的 Skill 一律保留，只顯示警告。

## 在任意專案協作

安裝後，用選定的主控開啟要工作的 Git 專案。Codex Desktop 用 `$zmaharness` 呼叫，Claude Code 用 `/zmaharness`。也可以直接交辦開發工作，符合協作條件時，主控會在對話中顯示 **【zMAHarness 協作建議】**，列出觸發原因、建議模型、任務與範圍、授權限制及協作理由。卡片和回覆使用你提問的語言，品牌、模型 ID 與指令名保留原文。

出卡前，主控會執行一次唯讀的 `mah route`，一次完成既有 `mah preflight` 檢查（本機 Git 專案、本機設定、Worker 啟用與執行檔、Claude／GPT 訂閱登入）、帳號可用模型偵測與額度查詢，並依異質優先原則選出卡片的選項 2（建議）與選項 3（備選）；使用者手打指定模型或別名時則以 `mah route --override` 解析。檢查不通過或所有可用 Worker 都在冷卻／額度用完時不出卡，主控會說明原因並自己繼續原任務。`mah preflight` 仍保留供人工單項檢查。若訊息是「不是 Git 專案」或「無法檢查 Git 專案」，也可能是因為目標不在主控目前的工作資料夾內，或被沙盒限制存取；請在該專案開啟主控後再試。

- **不同意**　主控立即自行繼續原任務，同一問題不會反覆詢問。
- **繼續協作**　可選擇建議模型、備選模型，或手打調整模型、思考程度及縮小檔案範圍。主控依你的選擇準備資料與 Grant，派工後獨立驗收並決定是否採納。對話核准（`codex_prompt`）每張卡固定 1 次；終端機核准（`manual_cli`）的 Disposable Worktree 卡片最多可到 `limits.max_dispatches_per_grant`，Context-Bundle 卡片在兩種模式都是 1 次。

`python -B -m mah --help` 可在其他專案目錄執行。`--repo` 和派工卡的 `repo_root` 都應指向**目前工作的目標 Git 根目錄**。zMAHarness clone 目錄是 Python 引擎的位置。

| 審查模式 | 適用情況 |
| --- | --- |
| **Context-Bundle** | 1～2 個核心檔案能完整封裝在預設 32 KB 的任務上限內。可以審查未提交的完整檔案。 |
| **Disposable Worktree** | 跨檔、大檔或需要追查關聯檔案。快照納入未提交變更與符合規則的新檔，必要時只在分離 HEAD 上建立本機匿名提交。專案的 AI 設定檔（`.claude/`、`CLAUDE.md`、`CLAUDE.local.md`、`.mcp.json`、`AGENTS.md`、`AGENTS.override.md`、`.agents/`、`.agent/`、`.gemini/`、`GEMINI.md`，任何層級）不會放進快照，Worker 看不到。 |

兩種模式都會綁定核准範圍並保存收據。驗收成功後仍由主控決定是否採納。遇到送出狀態不明，先查現有收據與結果。Worker 與主控使用同一 Windows 帳號，唯讀限制依賴 CLI 的會話設定，工具行為未獲 OS 層完整驗證。詳見 [協作規範](.agents/skills/zmaharness/SKILL.md) 和 [使用風險](DISCLAIMER.md)。

### 同意的信任模型

- **對話核准（`codex_prompt`，預設）**：你在主控對話中同意後，由主控執行 `mah grant approve --via codex_prompt` 寫入同意紀錄。這筆紀錄是主控寫入的，CLI 只能檢查卡片雜湊、範圍與次數，無法防止不遵守 Skill 的主控自行寫入。
- **終端機核准（`manual_cli`）**：需要硬性保證時，在本機 TOML 設定 `approval.mode = "manual_cli"`。之後每張卡都必須由你在互動終端機執行 `mah grant approve <card_id> --sha <完整 SHA-256> --via manual_cli`，親手輸入畫面上的確認碼；`--via codex_prompt` 會以結束碼 10 拒絕。預設的 `codex_prompt` 模式兩種方式都接受。
- `delegate-implement`（讓 Worker 修改程式）屬於延後階段的功能，目前所有 Worker 一律拒絕，只做唯讀分析，已列入路線圖。

## 本機設定與故障排除

- **模型目錄、本機分級與額度感知**：
  - **模型目錄**：由維護者管理的 [`mah/model_catalog.toml`](mah/model_catalog.toml) 定義模型分級、別名與任務路由；`git pull` 即生效，不必重新同步 Skill。表裡沒有的模型一律視為「未分級」，可手動指定但不會自動推薦。目錄只影響推薦，不影響既有卡片、Grant 與派工授權。執行 `python -B -m mah models` 可查看目錄、帳號實際偵測結果與額度狀態（`--all` 顯示完整未分級清單、`--refresh` 重新查詢）。
  - **本機覆寫（`models.local.toml`）**：需要自訂分級或路由參數時，複製範本 [`models.local.example.toml`](models.local.example.toml) 到 `%LOCALAPPDATA%\zMAHarness\models.local.toml`（與 `harness.local.toml` 同目錄）。本機檔有語法或欄位錯誤時會整份忽略並退回內建目錄，不擋派工；內建目錄中的 `refused` 模型不可被本機取代。
  - **額度感知與快取**：僅在出卡（`mah route`）與 `mah models` 查詢，且一律採 fail-open（查不到視為 `unknown`，不擋派工）。**GPT** 經 `codex app-server` 的 `account/rateLimits/read` 主動查詢（不推論、絕不使用額度重設券）；**Claude** 由 `mah` 內部以 `Popen` 呼叫 `claude -p "/usage" --output-format json --no-session-persistence` 主動查詢（主控不得自己在 shell 執行 `/usage`），附帶防計費檢查與 CLI 斷路器；**agy** 從帳本與失敗 Run 的 `agy.stderr.txt` 429 收據唯讀推導冷卻，不改帳本 schema。偵測與額度快取存在 `state\routing_cache.json`；每次沙盒外 `mah dispatch` 完成後會以 best-effort 刷新快取，讓沙盒內的出卡在查不到時仍可沿用 24 小時內尚未重設的 `tight`／`exhausted` 訊號。
- **設定位置**：`%LOCALAPPDATA%\zMAHarness\harness.local.toml`，預設子目錄為 `state`、`worktrees`、`gui`。`agy-print` 和對話核准（`codex_prompt`）預設啟用。CLI 會話固定停用 AI Credits。
- **舊資料清理**：不會自動清理。執行 `mah cleanup` 列出超過 `privacy.retain_run_dirs_days`（預設 30 天，可用 `--older-than-days N` 覆寫）的 Run 目錄與 Disposable Worktree；預設只列出（JSON），不寫入任何東西，確認清單後加 `--apply` 才會刪除，刪除無法復原。設定值 `0` 代表停用：只能列出，`--apply` 以結束碼 2 拒絕。`doctor.ps1` 會顯示目前可清理的 Run、Worktree 數量與大約佔用空間（只統計，不刪除）。
  - 只刪除已結束、而且已採納或拒絕（`accept`／`reject-result`）的 Run；`FAILED`、`FAILED_PRE_SUBMIT`、`CANCELLED`、`ABANDONED` 不需要採納決定。未結束的 Run、成功但尚未採納或拒絕的 Run、未到期的 Run 一律保留，清單會寫出原因。
  - Disposable Worktree 只在路徑是 `worktrees\<run_id>`、而且 HEAD 仍是快照提交或派工時的提交時，才會移除；不符合時 Worktree 和 Run 目錄都保留（`worktree_present`）。Worktree 內任何層級（包含被 Git 忽略的檔案）有符號連結、junction 或其他 reparse point 時保留（`worktree_contains_link`）；`git status` 顯示任何修改或未追蹤檔案時也保留（`worktree_dirty`）。這兩項檢查在列出時和實際刪除前各做一次。清理不使用 `git worktree remove`（Windows 上它會穿過 Worktree 內的 junction，刪掉外部目錄的檔案），而是以不跟隨連結的方式自己刪除目錄，再由下方的 `git worktree prune` 移除登記。沒有 Run 紀錄的孤兒 Worktree 也會列出，只有快照提交訊息相符的才會移除；仍被待核准卡片或有效 Grant 引用的保留（`awaiting_dispatch`）。
  - Run 目錄或其中任何項目是符號連結或 junction 時拒絕刪除。帳本紀錄保留，刪除後 `mah status <run_id>` 仍可查詢（`task_evidence` 會是 `missing`）。
  - `git worktree prune` 只在 `--apply`、而且該 repo 所有可 prune 的登記都位於 `worktrees` 底下時才執行，否則只回報；只列出時，`prune` 清單以 `status: 將執行` 標示 `--apply` 會執行的 prune，但不實際執行。Run 目錄因檔案被鎖住等原因刪除失敗時，該 Run 保留並標示 `delete_failed`（附錯誤訊息），其餘項目照常處理並輸出報告。快照提交不另外清理，由 Git 自己的 gc 回收。
- **Run 卡住時**：`mah cleanup` 不會碰未結束的 Run。若 Run 卡住（例如所有派工都以結束碼 13 被拒），執行 `mah resolve <run_id> --mark-failed --reason "<原因>"` 結束它，Run 會變成 `FAILED`，Grant 不退回；不要手動刪除未結束 Run 的收據檔，否則它無法正常收集。
- **codex-exec 一律以結束碼 17 拒絕，訊息提到 system Codex configuration 或 Windows Codex policy**：`C:\ProgramData\OpenAI\Codex\config.toml`、`requirements.toml` 或登錄檔 `HKLM\SOFTWARE\Policies\OpenAI\Codex` 的 `RequirementsPath` 存在時，Codex 會套用這些系統層設定，`--ignore-user-config` 擋不住，可能改變模型供應商、模型目錄或工具，所以 zMAHarness 一律拒絕。若這些檔案是你自己建立的，移除或改名後重試；若由公司管理，請不要繞過，改用其他 Worker。
- **移除 zMAHarness**：刪除 Python 使用者套件目錄中的 `zmaharness.pth`（位置可用 `python -m site --user-site` 查詢）、上述兩處 `zmaharness` Skill 目錄，以及 `%LOCALAPPDATA%\zMAHarness`，最後刪除 clone 目錄。
- **Worker 執行檔**：zMAHarness 只從 PATH 中的絕對路徑尋找 `claude`、`codex`、`agy`，不使用目前目錄或相對路徑下的同名檔案；解析結果位於目標專案、工作樹或 Bundle 目錄內時一律拒絕（結束碼 14）。
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

- **已完成（3d）**：模型目錄（`mah/model_catalog.toml`）、本機覆寫（`models.local.toml`）、帳號可用模型偵測（`mah models`）與額度感知路由（`mah route`）。

以下項目**規劃中，尚未實作**：

1. OpenAI 相容 API Worker（DeepSeek、Ollama 等本地模型）。僅支援 Context-Bundle，需使用者自行提供 API 金鑰。
2. 委派實作（delegate-implement）：Worker 只在可拋棄的 worktree 內修改程式並產出 diff，由主控審查、自行套用並跑測試，絕不自動合併；每次派工仍需使用者同意。

## 離線驗證

在 zMAHarness clone 根目錄執行以下命令。測試使用合成資料及本機替身。

```powershell
python -B -m unittest discover -s tests/v1 -q
python -B -m unittest discover -s tests/v2 -q
```

若 Windows 未開啟開發人員模式，且一般使用者沒有建立符號連結的權限，相關測試會顯示為略過，屬於正常現象；有權限的環境仍會執行完整斷言。

V1 的檔案交接與中斷復原規則見 [角色規範](docs/ROLES.md) 和 [V1 盤點](docs/v2/V1_INVENTORY.md)。要新增 Worker Adapter，見 [docs/ADAPTERS.md](docs/ADAPTERS.md)。請讓本機設定、SQLite、任務紀錄及私人驗證報告留在本機。Git 白名單只納入專案程式、通用文件與已去識別化的測試資料。

## 開發方式

本專案由作者負責需求定義、架構設計與驗收標準，並以多個 AI 協作實作：

- **Codex（GPT）**：主要實作與測試。
- **Claude**：部分實作、規格設計與獨立驗收。
- **Gemini**：架構審查，並以 Worker 身分參與真實派工審查。

每個階段都經過獨立審查與測試驗收後才合併。本專案本身就是 zMAHarness「主控負責到底、協作需經同意、結果需獨立驗收」理念的實踐。

授權：MIT，見 [LICENSE](LICENSE)。同時請參閱 [DISCLAIMER.md](DISCLAIMER.md)。回報安全問題的方式見 [SECURITY.md](SECURITY.md)。
