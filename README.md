# Multi-Agent Harness

這是適用於 Windows 11 的 Codex 與 Antigravity 檔案交接專案。它包含本機初始化、環境檢查、角色規範，以及已驗證的 Python 交接與中斷復原模擬器。可在不同電腦各自部署，並使用相同的交接規則。

**目前支援離線模擬。** 設定中的 GUI 自動化預設關閉。專案尚未內建真正的 Antigravity GUI 橋接，也不會替你登入、送出模型任務、授權、扣額度或改動應用程式的全域設定。

**使用風險與平台條款**集中記載於 [DISCLAIMER.md](DISCLAIMER.md)，由使用者自行閱讀、評估與決定是否使用。這份風險告知不是每次任務都要求 Agent 上網重查條款的執行步驟；實際權限提示、付款提示或工具拒絕仍應停止處理。

## 架構

| 角色 | 工作 |
|---|---|
| Sol（Codex 主 Agent） | 決定路由、任務內容與驗收條件。保留派工收據、辨識重複任務、核對結果。 |
| Terra Low（Codex Computer Use） | 在使用者指定的試行中，按 Sol 的指示操作 GUI，回報所見的回執、權限提示或不明狀態；不自行更換模型或核准敏感操作。 |
| Antigravity 的 Gemini Flash | 在獨立工作目錄執行單一匿名任務，先交付結果，再發布完成標記。 |

可直接交給兩個 Codex 角色使用的規範見 [ROLES.md](ROLES.md)。模型名稱是路由目標，請以工作階段實際顯示的選項為準。

## 安裝

準備 Windows 11、Git、Windows PowerShell 5.1 或 PowerShell 7，以及 Python 3.11 以上版本（需包含 SQLite 標準函式庫）。本機模擬不需管理員、第三方 Python 套件或外部 API。若日後要實機驗證，另外透過官方來源安裝 Codex 與 Antigravity，並自行登入。

在私人儲存庫建立後，用實際的 Clone URL 取代範例。若收到資料夾副本，直接進入資料夾，從初始化開始。

```powershell
git clone '私人儲存庫的 Clone URL' MultiAgentHarness
Set-Location MultiAgentHarness
.\setup.ps1
.\doctor.ps1
python -B -m unittest -v
```

若目前 PowerShell 不允許執行本機腳本，先確認腳本來源及所在組織的政策。在允許的機器上，可只為該次 Windows PowerShell 程序指定 `RemoteSigned`，無須變更全域設定。若學校或組織政策仍禁止，請依管理者指引處理。

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup.ps1
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\doctor.ps1
```

## 設定

- [config/defaults.json](config/defaults.json) 是共用規則，包含模型路由、檔名、發佈順序、試行等待時間及額度上限。這些路由與額度規則目前由人和 Agent 按規範執行，JSON 不會啟動 GUI，也不會代替 Antigravity 量測額度。
- [config/local.example.json](config/local.example.json) 是機器專屬設定範本。初始化會建立 `.local/settings.json` 及工作目錄。預設目錄為 `.local/workspace`。重跑會保留既有設定及工作檔。
- 若要首次指定其他工作目錄，可使用 `.\setup.ps1 -WorkspacePath '.local/自訂工作區'`。初始化後要變更路徑，請先停止正在寫入的任務，直接編輯 `.local/settings.json`，再執行 `.\setup.ps1`。相對路徑以專案根目錄為準。也可填入本機的完整路徑。
- `pythonExecutable`、`codexExecutable`、`antigravityExecutable` 預設是 `null`，由 Doctor 自動探測。必要時填入可信的 `.exe` 路徑。JSON 字串可使用 `/` 作為路徑分隔符。請把帳號及憑證留在官方應用程式中。
- 兩台電腦各用自己的本機工作目錄。SQLite 收據只提供本機防重複。若要移轉一組測試狀態，先停止兩邊的寫入，再將資料庫和交接子目錄一起搬移。

Doctor 會檢查 Windows 組建、PowerShell、設定檔、Python、SQLite 及工作目錄的建立／讀取／刪除能力。它只找 Codex、Antigravity 的安裝線索。登入、模型選項、Computer Use、實際操作權限與額度都會顯示「需人工驗證」。平台使用風險另見 DISCLAIMER.md，不列入每次派工的自動審查。結束碼 `0` 表示可自動檢查的項目沒有失敗；`1` 表示有自動檢查失敗。請同時閱讀「需人工驗證」，不要把結束碼當成 GUI 已就緒。

## 使用方式

**離線驗證**：執行 `python -B -m unittest -v`。原有 24 項測試使用合成資料，涵蓋重複派工、錯誤結果、權限訊號、逾時、程序中斷與復原。若 Doctor 找到的是另一個 Python，請用它列出的執行檔路徑取代 `python`；若走 Windows Launcher，使用 `py -3 -B -m unittest -v`。

**在測試程式中使用**：[handoff_sim.py](handoff_sim.py) 的 `LocalCoordinator(directory)` 接受已建立的本機工作目錄。原有的 `run(...)` 使用本機模擬工作者，`recover(task_id)` 根據同一工作目錄的 SQLite 收據恢復追蹤。沒有命令列派工器、GUI 橋接或 MCP。最小可重現範例見 [test_handoff_stage2.py](test_handoff_stage2.py)，復原案例見 [test_handoff_stage3.py](test_handoff_stage3.py)。

**實機工作流**：使用者自行閱讀 [DISCLAIMER.md](DISCLAIMER.md) 並決定試行後，先確認帳號登入、模型選項與共用目錄，再以一筆合成任務、一次 Gemini Flash 送出及人工監看開始。Sol／Terra 不應把「每次上網重新研究平台條款」當成例行派工步驟。遇到實際權限拒絕、付費、額度不明、模型不符或回執不明時停止，由 Sol 回報，不自行繞過限制。這一步需要另外實測，初始化與 Doctor 不會啟用它。

### 檔案交接協定

1. Sol 使用邏輯 `task_id` 及任務內容雜湊保留收據。每筆派工另有 Run ID，用來識別這一次的結果。
2. 以 UTF-8 發布 `task.md`，列出 Run ID、允許的工作及結果驗收條件。模型收到的是 Run ID，資料庫防重複用的是邏輯 `task_id`。
3. 工作者先完整發布 `result.md`，第一行為 `# <Run ID>`，其後依任務規則填寫結果。最後發布 `DONE`，其內容只含相同 Run ID，可加一個行尾換行。
4. Sol 核對兩份結果檔的識別碼、內容、時間順序和原定期限。單有 `DONE` 不足以判定成功。檔案完成後請維持原樣，時間判定依賴本機時鐘與檔案系統。

| 收據狀態 | 復原方式 |
|---|---|
| `CLAIMED` | 尚未進入派工；核對原內容與驗收規則後，可明確要求 `resume_prepared=True`。 |
| `DISPATCHING`／`SUBMISSION_UNKNOWN` | 先找既有結果。缺少可驗證交接時，保留紀錄等待人工核對。 |
| `WAITING` | 以原定期限繼續追蹤，並可核對期限內已發布的結果。 |
| `SUCCEEDED`、`INVALID_RESULT`、`HANDOFF_ERROR`、`PERMISSION_REQUIRED`、`TIMEOUT` | 已定案。保留檔案及收據供查核。 |

試行規則是單一併發、每個 `task_id` 最多送出一次、每次試行總共一次外部模型送出。收據與 GUI 送出之間無法做成同一交易，所以狀態不明時須人工對照 Antigravity 歷史及額度；請勿清除收據來重送。

## 故障排除

| 情況 | 處理 |
|---|---|
| 找不到本機設定或工作目錄 | 在專案根目錄執行 `.\setup.ps1`。若有自訂路徑，檢查 JSON 及資料夾的本機寫入權限。 |
| Python 或 SQLite 檢查失敗 | 確認 Python 3.11 以上版本及 SQLite 可用，必要時填入 `pythonExecutable` 後重跑 Doctor。 |
| Codex／Antigravity 安裝線索未偵測到 | 從開始功能表人工確認，或填入應用程式的完整執行檔路徑。安裝線索不代表已登入或有對應模型。 |
| `INVALID_RESULT` 或 `TIMEOUT` | 核對實際 Run ID、`result.md` 完整度、`DONE` 的發布順序及原定期限，保留原檔。 |
| `SUBMISSION_UNKNOWN` | 保留收據與交接目錄，人工檢查送出歷史及是否扣額度。等既有結果出現後，可再次執行復原驗收。 |

## Git 與驗證範圍

`.gitignore` 採用檔案白名單，只放行程式、範本、測試和文件。請將實際設定、任務資料、SQLite 收據及憑證留在本機。建立私人儲存庫前，以 `git status --short --untracked-files=all` 核對待加入項目；忽略規則不會移除已追蹤檔案。

已驗證 Windows 本機初始化、重跑保護、環境檢查及 24 項離線測試；依部署回報，第二台 Windows 電腦亦通過相同的離線測試。真實 GUI 操作、權限提示辨識、卡住時的中止、實際送出回執與 Gemini 額度都仍需實機驗證。
