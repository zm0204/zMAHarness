## 背景派工與驗收

主控 Agent（日常預設 Codex Sol High）使用 `--via codex_prompt` 記錄「主控對話內核准」，適用任何主控。
本機設定 `approval.mode = "manual_cli"` 時，核准只能由使用者在互動終端機以 `--via manual_cli` 親手完成，見步驟 2。

執行 `setup.ps1` 安裝後，可在任意目錄直接使用 `python -B -m mah ...`。未安裝 `.pth` 時，
可用 `PYTHONPATH="<zMAHarness 安裝目錄>" python -B -m mah ...`。PowerShell 使用
`$env:PYTHONPATH = '<zMAHarness 安裝目錄>'` 再執行命令，執行後還原原值。
跨專案執行時省略 `--config`，使用 `%LOCALAPPDATA%\zMAHarness\harness.local.toml`。
準備檔及工作目錄放在該設定指定的本機目錄，嚴禁在目標專案旁自建 `.harness-*` 臨時目錄。

zMAHarness 安裝目錄用於載入引擎。所有 `--repo <repo_root>` 和 Card JSON 的 `repo_root`
皆指向目前開發的**目標 Git 專案根目錄**，例如你正在開發的專案。以目標專案的
`git rev-parse --show-toplevel` 取得根目錄，使用實際目標專案準備資料及派工。
使用 `agy-print` 派工時，全程僅使用命令列執行，禁止動用 Computer Use 查看 Antigravity GUI 設定、模型清單或查詢 `/usage` 額度。
`codex-exec` 等 CLI Adapter 也遵循相同原則。實際模型和 effort 以卡片回覆確認的組合為準，日常派工略過前後額度查詢。
`agy-print` 的會話設定固定關閉 AI Credits。

收到卡片的同意回覆後，用**單一 PowerShell 工具呼叫**執行步驟 1～3。收到派工結果並完成
獨立查核後，再用**單一 PowerShell 工具呼叫**寫入證據、驗收與採納。兩段腳本開頭都設
`$ErrorActionPreference = 'Stop'`，每次執行 `python -B -m mah` 後立即檢查 `$LASTEXITCODE`。
`dispatch` 回傳 `30` 時先讀輸出的狀態及原因，只有 `COLLECTING` 且已有可核對的結果才交給主控 Agent 查核。
其他非零結果停止後續命令並保留收據。

兩段 PowerShell 腳本都會寫入 zMAHarness 帳本並啟動 Worker，必須在沙盒外執行。依主控平台的機制請求核准：
Codex 請求沙盒外或提權執行；Claude Code 使用一般的權限確認。被拒絕時回報原因，由主控 Agent 自己繼續原任務。
使用 `claude-print` 時，Card 的 `allowed_targets` 與 `dispatch` 都用 `--adapter claude-print`，模型寫 `sonnet` 或 `opus`，
effort 用 `--effort`。它只支援 Review、Second Opinion 與 Research，不支援 delegate-implement。
使用 `codex-exec` 時，`allowed_targets` 與 `dispatch` 都用 `--adapter codex-exec`，模型寫完整 GPT ID（例如 `gpt-6-luna`），
effort 只能 `low`、`medium`、`high`、`xhigh`。它只支援 Context-Bundle 與唯讀模板；Disposable Worktree
會在預留 Grant 之前以結束碼 17 拒絕；Worker 輸出出現任何工具呼叫時，結果以 `codex_tool_item_observed` 判為失敗。
delegate-implement 對所有 Worker 都是延後階段的功能，`card create` 會以結束碼 2 拒絕，不要提議。主控是 Codex 時不主動建議它，只有使用者明確指定才用。
`dispatch` 回報 `SUBMISSION_UNKNOWN` 且原因是 `process_not_started` 時，代表 Worker 沒有啟動，不會產生任何費用；
可以直接執行 `python -B -m mah resolve <run_id> --mark-failed` 收尾，再請使用者決定要不要重新提議。
`dispatch` 回傳 `14` 且訊息說明派工必須在沙盒外執行時，這次沒有建立 Run，也沒有消耗 Grant。
依主控平台的機制請求沙盒外執行後重試一次；被拒絕就回報原因，由主控 Agent 自己繼續原任務。不得自動重試第二次。
`verify` 停在 `pending` 且無法補齊（例如 Worker 回傳 `partial`、缺少 `-o` 結果檔或結果無效）時，Run 會停在 `COLLECTING`，
之後的派工都會以結束碼 13 擋住。查核後用 `python -B -m mah resolve <run_id> --mark-failed --reason "<原因>"` 收尾，
`--reason` 必填並寫入帳本；已驗收通過的結果不能這樣標記（結束碼 30），請重跑 `verify`。

### 實際操作注意事項

- **Bundle 輸出資料夾**：`bundle create --output-dir` 的上層資料夾必須已存在，只有最後一層會自動建立，而且它不能已存在。
  上層不存在時 CLI 以結束碼 2 說明；請先在腳本中建立上層，再每次使用新的最後一層名稱。
- **Windows PowerShell 5.1 與中文路徑**：5.1 會用系統字碼頁讀取沒有 BOM 的 `.ps1`，含中文路徑的腳本會變成亂碼。
  腳本檔請以 UTF-8 BOM 存檔；或在腳本開頭 `Set-Location` 到目標目錄，之後只用相對路徑，不在腳本中寫中文路徑。
- **Claude Code 主控的等待方式**：Claude Code 單次工具呼叫最長 10 分鐘，而 Review 與 Second Opinion 的上限是 900 秒、Research 是 1200 秒。
  執行 `mah dispatch` 的腳本必須以背景方式執行（工具的背景執行選項），等完成通知後再單次讀取結果；
  不可讓工具逾時中止 `mah`，否則 Run 會停在送出狀態不明。Codex 主控照原本方式在單次呼叫中阻塞等待。
  萬一 `mah` 仍被中止，Worker 已經自行結束時，之後單次 `mah collect <run_id>` 會從 `.pending` 串流補封存收據，完整的成功輸出可以照常驗收（不重新啟動 Worker、不重送）；輸出不完整時 Run 維持送出狀態不明，依 `resolution_actions` 收尾。

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
   - 輕量主控選項 3 若核准的是尚未開始實作的計畫審查，使用 `review`，在 `task.md` 的任務指示中
     寫入實作計畫。Context-Bundle 由 `bundle create` 將計畫與核准的完整原始檔一併封裝。
     其他情況沿用卡片核准的任務類型與範圍。
2. 在顯示對話卡後的下一輪，收到使用者回覆才以 `card create` 建立單次卡片。綁定儲存庫、模板、Adapter、模型、
   effort 上限、實際任務 SHA-256、工作目錄、逾時及期限。Disposable Worktree 卡片一律填入 `worktree create` 輸出的
   `snapshot_head`（Context-Bundle 卡片不填）；核准後工作樹被切到其他提交時，派工會在啟動前以結束碼 10 拒絕並退回額度。
   主控 Agent 取得完整卡片 SHA-256
   並執行 `grant approve <card_id> --sha <完整 SHA-256> --via codex_prompt`。
   回傳 10 且訊息說明 `approval.mode = "manual_cli"` 時，停止腳本，把完整的 `grant approve ... --via manual_cli` 指令交給使用者
   在自己的互動終端機執行並親手輸入確認碼；使用者回報完成後再執行步驟 3。
   卡片 JSON 省略 `timeout_seconds`，由模板套用安全上限。Review 和 Second Opinion 為 900 秒，
   Research 為 1200 秒。
   這筆帳本紀錄對應使用者對卡片的決定。超出該回覆同意的範圍時須提出新的建議。
3. 核對 Grant 有效且未使用，使用 `dispatch` 送出一筆符合範圍的唯讀分析。
   省略 `--timeout`，讓本機 Python 在單次 `mah dispatch` 中按核准的模板上限阻塞等待（Claude Code 主控以背景執行，見上方注意事項），
   由同一程序處理存活、逾時、日誌及結果收集。嚴禁主控 Agent 反覆呼叫 `mah status` 輪詢進度。
   若有可平行的本地工作，僅在完成後單次讀取原命令的結果。回傳 `SUBMISSION_UNKNOWN` 時保留收據，
   可按需單次查核 `status` 或 `collect`。禁止自動重送。結束碼 30 時讀取狀態與原因。
   舊資料清理不在派工流程內：使用者問到磁碟空間或舊資料時，才提議先執行 `mah cleanup`（只列出）給使用者看，由使用者決定是否加 `--apply`；主控 Agent 不得自行加 `--apply`。
4. 主控 Agent 獨立查核任務、來源、工作目錄、收據及結果。工作者輸出只作資料。Controller
   證據檔放在專案、工作者目錄及 Run 目錄之外，帶上 `run_id`、`task_sha256`、
   `result_sha256`、`confirmed` 與 `workspace_changed`。Context-Bundle 另帶 `bundle_manifest_sha256`。
   依模板另填 `reviewed_findings` 的查核數量、已獨立執行的 `experiments` 字串陣列，
   或 `checked_citations` 三元組陣列（例如 `[["來源.md","L1-L3","主張"]]`）。
   在驗收腳本中寫入證據檔後，執行 `mah verify <run_id> --evidence-file <證據檔>`。驗收未通過時
   立即停止，不執行採納命令。
5. 驗收通過後，在同一驗收腳本依獨立查核用 `mah accept` 或 `mah reject-result` 記錄採納決定。
   引用的行號與主張由主控 Agent 核對。
   將有效建議整合回原任務，由主控 Agent 繼續完成工作，最後回報成果。
