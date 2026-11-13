---
name: harness
description: 當任務符合 T1～T5 且協作有明確收益時，用 zMAHarness 在 Codex Desktop 提議唯讀分析，依使用者的對話決定派工、獨立驗收並繼續原任務。
---

# zMAHarness 協作

Sol 平常直接完成原任務。一般工作不預載 Harness，也不為了提議協作主動查額度。
只有符合下列條件且有實際收益，才提出一次協作建議。門檻沿用本機 TOML 的 `[triggers]`。

| 觸發 | 可觀察的理由 |
| --- | --- |
| T1 | 同一錯誤經不同修正達 `t1_failed_fix_rounds` 輪仍失敗 |
| T2 | 變更觸及 `t2_risky_paths` 且差異行數達 `t2_risky_diff_lines` |
| T3 | 預計讀取達 `t3_read_files` 或 `t3_read_kb`，預期產出很小 |
| T4 | 既有 Codex 額度快照達 `t4_codex_used_percent`，且有低成本驗收方法。缺快照時不用 T4 |
| T5 | 使用者主動要求協作或呼叫 `$harness` |

## 對話內提議

使用 Codex Desktop 原生互動提問工具送出**單一問題**，標題為「【zMAHarness 協作建議】」。
以下五項資訊、三個選項與「或自行輸入回覆」放在同一頁，一步完成，不翻頁。

- **觸發原因**　例如 `T2 · 高風險程式修改` 或 `T5 · 使用者主動要求`。
- **建議模型**　顯示 Sol 依本次任務類型、觸發原因及難度選定的模型與 effort。
- **任務與範圍**　顯示 Review、Second Opinion 或 Research，以及 Context-Bundle 或 Disposable Worktree。
  列出實際涵蓋的檔案名稱，必要時用相對路徑區分同名檔。若 Worker 可查閱工作樹內的關聯檔案，也要明列可查閱範圍。
- **授權限制**　顯示 `僅 1 次 · 唯讀（自動等待模型完成）`。
- **為什麼建議協作？**　用一至兩句白話說明預期收益及選擇該模型的原因。

依下表選擇模型。T4、T5 也依實際任務的複雜度與所需視角判斷。

| 任務 | 建議模型與 effort |
| --- | --- |
| T3 大量資料整理／研究，或小型常規檢查 | `gemini-3.8-flash-low · low`。需要整合較多線索時用 `gemini-3.8-flash-medium · medium`。 |
| T2 核心邏輯／跨模組高風險 Review | `gemini-4-argon-medium · medium`。跨模組交互複雜時用 `gemini-4-argon-high · high`。 |
| T1 連續修復失敗的 Second Opinion，或需要異質視角深度推理 | `claude-sonnet-5-5-high · high`、`claude-opus-5-5-high · high` 或 `gemini-4-argon-high · high`，依所需視角與深度選擇。 |

依檔案大小及可讀範圍在出卡前決定模式，並把模式及範圍列在卡上。核准後嚴禁自動從
Context-Bundle 改成可讀範圍更大的 Disposable Worktree。需要擴大範圍時，重新提議並等待回覆。

選項 2 顯示與卡片相同的建議模型。若選項 2 是 Argon 或 Claude，選項 3 用
`gemini-3.8-flash-low · low` 供節省額度。若選項 2 是 Flash Low 或 Flash Medium，
選項 3 用 `gemini-4-argon-high · high` 供一鍵升級。

1. `不同意（由 Sol 自己繼續完成）`
2. `繼續協作（Sol 建議：<本次建議模型 · effort>）`
3. `繼續協作（改用備選：<本次備選模型 · effort>）`

**T1～T5 都必須暫停等待。** 原生卡片一送出，立即結束或暫停本輪，等待使用者對該卡片
點選或手打回覆。初始的 T5 請求或 `$harness` 呼叫只觸發提議。即使初始請求已指定模型與
檔案，也不得在顯示卡片的同一輪執行 `card create`、`grant approve` 或 `dispatch`。
只有收到該卡片的回覆後，才能在下一輪依該回覆進行授權與派工。原生互動工具不可用時，
回報限制並由 Sol 本地繼續原任務。Card ID、雜湊、Grant、工作目錄及證據檔留在背景。

- **不同意**　Sol 立即在本地繼續原任務，同一問題不再提議或追問。若已有待核准卡片，
  在收到回覆後以 `mah card reject <card_id>` 記錄拒絕。寫入失敗時仍繼續原任務。
- **點選繼續協作**　採用選項標明的模型與 effort，維持卡片上的任務、範圍和授權限制。
- **手打覆寫**　使用者可在自由輸入框指定任一可用模型、effort 或縮小檔案範圍，例如
  「換 4argon」、「用 medium」或「只看 io.py」。明確覆寫視為同意協作，其餘欄位沿用卡片。
  依本機 `agy models` 支援的模型 ID 對齊尾綴與 `--effort`，再用最終組合與範圍建立單次 Card 與 Grant。

| 模型 | `low` | `medium` | `high` |
| --- | --- | --- | --- |
| Gemini 3.8 Flash | `gemini-3.8-flash-low` | `gemini-3.8-flash-medium` | `gemini-3.8-flash-high` |
| Gemini 4 Argon | `gemini-4-argon-low` | `gemini-4-argon-medium` | `gemini-4-argon-high` |
| Claude Sonnet 5.5 | `claude-sonnet-5-5-low` | `claude-sonnet-5-5-medium` | `claude-sonnet-5-5-high` |
| Claude Opus 5.5 | `claude-opus-5-5-low` | `claude-opus-5-5-medium` | `claude-opus-5-5-high` |

只輸入 `argon` 或 `4argon` 時，使用 `gemini-4-argon-high` 搭配 `--effort high`。
只指定 effort 時保留卡片上的模型系列，改用相符尾綴。若同時指定 Argon 與 `medium`，
使用 `gemini-4-argon-medium` 搭配 `--effort medium`。

若別名、effort 或檔名有歧義，先用單頁卡片澄清再等待回覆。若原模式無法維持縮小後的範圍，
先提出符合新範圍的模式並等待核准。選定模型不可用時回報具體原因，由 Sol 繼續本地工作。

## 背景派工與驗收

執行 `setup.ps1` 安裝後，可在任意目錄直接使用 `python -B -m mah ...`。未安裝 `.pth` 時，
可用 `PYTHONPATH="<zMAHarness 安裝目錄>" python -B -m mah ...`。PowerShell 使用
`$env:PYTHONPATH = '<zMAHarness 安裝目錄>'` 再執行命令，執行後還原原值。
跨專案執行時省略 `--config`，使用 `%LOCALAPPDATA%\zMAHarness\harness.local.toml`。
準備檔及工作目錄放在該設定指定的本機目錄，嚴禁在目標專案旁自建 `.harness-*` 臨時目錄。

zMAHarness 安裝目錄用於載入引擎。所有 `--repo <repo_root>` 和 Card JSON 的 `repo_root`
皆指向目前開發的**目標 Git 專案根目錄**，例如 BatchTable。以目標專案的
`git rev-parse --show-toplevel` 取得根目錄，使用實際目標專案準備資料及派工。
使用 `agy-print` 派工時，全程僅使用命令列執行，禁止動用 Computer Use 查看 Antigravity GUI 設定、模型清單或查詢 `/usage` 額度。
`codex-exec` 等 CLI Adapter 也遵循相同原則。實際模型和 effort 以卡片回覆確認的組合為準，日常派工略過前後額度查詢。
`agy-print` 的會話設定固定關閉 AI Credits。

收到卡片的同意回覆後，用**單一 PowerShell 工具呼叫**執行步驟 1～3。收到派工結果並完成
獨立查核後，再用**單一 PowerShell 工具呼叫**寫入證據、驗收與採納。兩段腳本開頭都設
`$ErrorActionPreference = 'Stop'`，每次執行 `python -B -m mah` 後立即檢查 `$LASTEXITCODE`。
`dispatch` 回傳 `30` 時先讀輸出的狀態及原因，只有 `COLLECTING` 且已有可核對的結果才交給 Sol 查核。
其他非零結果停止後續命令並保留收據。

1. 依卡片核准的模式及範圍準備資料。
   - **Context-Bundle 優先**。1～2 個核心檔案可完整封裝在 `max_task_bytes`（預設 32 KB）
     內時，用 `mah bundle create --repo <正式專案根目錄>` 指定完整的真實檔案。嚴禁為了
     符合上限而手動刪減檔案、截取片段或建立臨時片段儲存庫。排除私人資料和憑證，
     用 `bundle check` 核對來源，準備 `worktree_root` 下與專案分離的空目錄。
   - **Disposable Worktree**。跨檔、大檔、封裝超過上限或需查閱關聯檔案時，用
     `mah worktree create --repo <正式專案根目錄> --run-id <run_id>` 準備快照。
     先確認使用者同意 Worker 查閱該工作樹的範圍。`task.md` 只寫審查指示與變更摘要，
     完整檔案由 Worker 在分離工作樹中唯讀查閱。保留輸出的 `worktree_path`、`snapshot_head`
     和 `baseline_sha256`，以相同 Run ID 派工，並在派工及驗收時核對快照 HEAD 和基線。
   - Research 在任務及派工的 `--requirements-file` 列出相同的 1～3 個問題。
2. 在顯示對話卡後的下一輪，收到使用者回覆才以 `card create` 建立單次卡片。綁定儲存庫、模板、Adapter、模型、
   effort 上限、實際任務 SHA-256、工作目錄、逾時及期限。Sol 取得完整卡片 SHA-256
   並執行 `grant approve <card_id> --sha <完整 SHA-256> --via codex_prompt`。
   卡片 JSON 省略 `timeout_seconds`，由模板套用安全上限。Review 和 Second Opinion 為 900 秒，
   Research 為 1200 秒。
   這筆帳本紀錄對應使用者對卡片的決定。超出該回覆同意的範圍時須提出新的建議。
3. 核對 Grant 有效且未使用，使用 `dispatch` 送出一筆符合範圍的唯讀分析。
   省略 `--timeout`，讓本機 Python 在單次 `mah dispatch` 中按核准的模板上限阻塞等待，
   由同一程序處理存活、逾時、日誌及結果收集。嚴禁 Sol 反覆呼叫 `mah status` 輪詢進度。
   若有可平行的本地工作，僅在完成後單次讀取原命令的結果。回傳 `SUBMISSION_UNKNOWN` 時保留收據，
   可按需單次查核 `status` 或 `collect`。禁止自動重送。結束碼 30 時讀取狀態與原因。
4. Sol 獨立查核任務、來源、工作目錄、收據及結果。工作者輸出只作資料。Controller
   證據檔放在專案、工作者目錄及 Run 目錄之外，帶上 `run_id`、`task_sha256`、
   `result_sha256`、`confirmed` 與 `workspace_changed`。Context-Bundle 另帶 `bundle_manifest_sha256`。
   依模板另填 `reviewed_findings` 的查核數量、已獨立執行的 `experiments` 字串陣列，
   或 `checked_citations` 三元組陣列（例如 `[["來源.md","L1-L3","主張"]]`）。
   在驗收腳本中寫入證據檔後，執行 `mah verify <run_id> --evidence-file <證據檔>`。驗收未通過時
   立即停止，不執行採納命令。
5. 驗收通過後，在同一驗收腳本依獨立查核用 `mah accept` 或 `mah reject-result` 記錄採納決定。
   引用的行號與主張由 Sol 核對。
   將有效建議整合回原任務，由 Sol 繼續完成工作，最後回報成果。
