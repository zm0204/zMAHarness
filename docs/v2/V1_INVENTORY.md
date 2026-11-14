> 這是早期設計文件，現況以 [README](../../README.md) 與 [docs/ADAPTERS.md](../ADAPTERS.md) 為準。

# MultiAgentHarness Phase 0A｜V1 原始碼盤點與相容性映射

- 盤點日期為 2026-11-11（台灣時間）。工作區為 `MultiAgentHarness`，分支 `main`，基準為 `b7277e42bdc120296ba0f52f8242c899171a878e`。開始時 `git status --short --branch --untracked-files=all` 顯示本機 `main` 與既有的 `origin/main` 追蹤引用一致，無待提交變更。兩份已複製的正式文件當時受忽略規則排除。
- 依 [正式規格](MultiAgentHarness_V2_Final_Spec.md) 及 [實作計畫](MultiAgentHarness_V2_Implementation_Plan.md) 定義 V2 目標。V1 行為以該提交的程式與測試為準。本文的映射是後續實作建議。
- `git log -n 5 --oneline` 依序為 `b7277e4`、`617d0db`、`179d02f`、`42ae6ea`、`1c483c6`。

## 1. 執行環境、檔案與離線測試

| 檔案 | V1 責任 |
| --- | --- |
| [handoff_sim.py](../../handoff_sim.py) | Python 模組。提供 `Outcome`、`validate_handoff` 與 `LocalCoordinator(directory)`，供呼叫者傳入工作目錄和模擬工作者，完成收據、一次送出、交接驗收及復原。 |
| [test_handoff_stage2.py](../../test_handoff_stage2.py)、[test_handoff_stage3.py](../../test_handoff_stage3.py) | 標準函式庫 `unittest` 的 11 項可靠性測試和 13 項復原測試。第二份以本機 Python 子程序及合成資料模擬強制中斷。 |
| [setup.ps1](../../setup.ps1)、[harness-config.ps1](../../harness-config.ps1) | 建立本機設定與工作區。透過同目錄暫存檔與移動保留既有設定。共用函式解析 JSON、檢查本機欄位並解析相對或絕對路徑。 |
| [doctor.ps1](../../doctor.ps1) | 使用本機 Python 記憶體 SQLite 做環境探針，檢查 Windows、PowerShell、設定與工作區讀寫，查找應用程式安裝線索。登入、模型、額度與 GUI 能力列為需人工驗證。 |
| [config/defaults.json](../../config/defaults.json)、[config/local.example.json](../../config/local.example.json) | 前者是供人和 Agent 遵循的路由、交接及額度設定，Doctor 會檢查其中部分欄位。後者是 `.local/settings.json` 的範本，預設工作目錄為 `.local/workspace`。Python 模組由呼叫者傳入路徑與期限，未自動載入這兩份設定。 |
| [README.md](../../README.md)、[ROLES.md](../../ROLES.md)、[DISCLAIMER.md](../../DISCLAIMER.md)、[.gitignore](../../.gitignore) | 部署指引、人工派工角色、風險告知及 Git 白名單。 |

V1 的可執行入口為 `.\setup.ps1`、`.\doctor.ps1` 及 Python 測試指令，交接流程透過匯入 Python 模組使用。基準內沒有 `mah`、MCP、套件清單或第三方 Python 依賴。Python 模組使用標準函式庫 `sqlite3`、`hashlib`、`uuid`、`pathlib`、`json`、`time`、`os.fsync`、`contextlib`、`dataclasses`、`math` 與 `re`。測試另外使用標準函式庫的 `unittest.mock`、`tempfile`、`threading`、`concurrent.futures`、`subprocess` 等。

| 項目 | 宣告需求及本輪實測 |
| --- | --- |
| Windows | V1 以 Windows 11 為目標。本機組建 26300。 |
| Python 與 SQLite | V1 文件要求 Python 3.11 以上及 `sqlite3`，本輪實測 Python 3.13.5、SQLite 3.49.1。 |
| PowerShell 與 Git | 腳本標示支援 Windows PowerShell 5.1 或 PowerShell 7。本輪執行環境為 PowerShell 7.6.9、Git for Windows 2.50.1。 |
| Codex 與 Antigravity | 離線測試不依賴兩套應用程式。本輪未探測 CLI 旗標、登入或真實派工能力。 |

在專案根目錄執行下列命令，`-B` 避免產生 Python 位元組碼快取。

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python -B -m unittest discover -s . -p 'test_*.py' -v
```

| 時點 | 實際結果 | unittest 耗時 | PowerShell 牆鐘時間 |
| --- | --- | ---: | ---: |
| 盤點前 | 24 項全數通過，0 失敗、0 錯誤、結束碼 0 | 3.418 秒 | 3.937 秒 |
| 盤點後 | 24 項全數通過，0 失敗、0 錯誤、結束碼 0 | 3.376 秒 | 3.668 秒 |

測試涵蓋同一任務的並行派工、內容衝突、舊 Run ID、逾時、錯誤或缺失結果、模擬權限提示、程序中斷、遷移舊收據，以及工作目錄搬移。本輪可確認的 V2 驗收基線為 `AT-18`。V1 的模擬案例可作為 `AT-11`、`AT-12`、`AT-13`、`AT-16`、`AT-17` 的參考，但尚未測到 V2 對應介面。

## 2. 識別碼、內容雜湊與 SQLite

| 項目 | V1 實際行為 | V2 映射 |
| --- | --- | --- |
| 邏輯 `task_id` | `run()` 接受 1–64 字元英數、底線及連字號，首字為英數，並拒絕列出的 Windows 保留名稱。用作收據主鍵及工作子目錄名稱。 | 保留 V1 邏輯鍵。V2 的 `runs.run_id` 標記一次派工，`cards.task_key` 用於建議次數，三者各有用途。 |
| Run ID | `claim()` 以 `f'{task_id}-{uuid4().hex}'` 產生，UUID 是 32 位小寫十六進位字串。傳給工作者，並寫入 `task.md`、`result.md` 的標題和 `DONE`。 | 可原樣沿用格式。V2 需用獨立 `run_id` 作主鍵，重新派工使用新 ID 並記錄 `parent_run_id`。 |
| 任務指紋 | `sha256(repr((request, expected)).encode('utf-8')).hexdigest()`。同一 `task_id` 比對完整指紋，`repr` 對型別、順序及表示方式敏感。 | 保留為 V1 冪等指紋。V2 另對發布後的 `task.md` 實際位元組計算 `task_sha256`，對派工卡定義一致的序列化並計算 `content_sha256`。 |
| 結果完整性 | 以 Run ID、預期 Markdown 行及時間順序驗收，未計算或持久化 `result.md`／`DONE` 的 SHA-256。 | V2 新增 `result_sha256`，依 §5.2 的格式校驗。匯入舊格式時，可對實際收集的檔案計算雜湊供日後比對，無法回推原始發布時的雜湊。 |

程式依據為 [`claim()` 與 `run()`](../../handoff_sim.py#L64) 及 [`read_completion()`](../../handoff_sim.py#L190)。

`LocalCoordinator(directory)` 將資料庫放在 `<directory>/receipts.sqlite3`，呼叫者須提供已存在的目錄。採用 V1 設定範本時為 `.local/workspace/receipts.sqlite3`。本輪用合成臨時資料庫查詢 `sqlite_master`、`PRAGMA table_info`、`index_list`、`index_xinfo` 與 `foreign_key_list`，結果如下。[建表與增量遷移](../../handoff_sim.py#L38)

| V1 `receipts` 欄位 | 型別與語義 | V2 欄位或處理 |
| --- | --- | --- |
| `task_id` | `TEXT PRIMARY KEY`，V1 任務鍵 | `runs` 無此欄。建議可選 `legacy_task_id` 關聯 `receipts(task_id)`，供確實需要承接舊收據的 run 使用。 |
| `fingerprint` | `TEXT NOT NULL`，V1 任務與驗收規則的指紋 | 留在原表，獨立於 `runs.task_sha256`、`runs.result_sha256` 與 `cards.content_sha256`。 |
| `status` | `TEXT NOT NULL`，由程式維護 | V2 `runs.state` 的語義對照見下一節。 |
| `detail` | `TEXT NOT NULL`，結果或原因說明 | 可作為 `state_reason` 或 `run_events.detail_json` 的參考，保留來源語義並避免把提示詞寫入新事件。 |
| `run_id` | 可空 `TEXT`，舊收據可沒有值 | 有有效識別碼時可供對照 V2 `runs.run_id`。V1 沒有 `run_id` 唯一約束，關聯優先使用 V1 主鍵。 |
| `deadline` | 可空 `REAL`，Unix 秒的絕對期限 | V2 §5.1 沒有同名欄位。保留舊值，V2 須保存可重建的當次期限或逾時快照。 |
| `expected` | 可空 `TEXT`，預期欄位與**值**的 JSON 陣列 | 僅供 V1 相容驗收。V2 用模板 schema 和 run 檔案；舊值不宜複製進 V2 中繼資料。 |

實測僅有 `receipts` 與主鍵自動索引 `sqlite_autoindex_receipts_1`，索引以 `BINARY` 排序。沒有額外索引、外鍵或狀態 `CHECK`。SQLite 對該 `TEXT PRIMARY KEY` 的 `table_info.notnull` 回報為 0，正常 `run()` 另有任務 ID 驗證。Windows 大小寫變體的防重複由 `find()` 的 `COLLATE NOCASE` 搭配 `claim()` 交易實現，資料表本身沒有不分大小寫的唯一約束。

### 建議的增量整合

- 保留 `receipts`、既有欄位及 Python 呼叫介面。依計畫在遷移前先備份。若需要承接 V1 資料，以 V2 的 repo 外本機狀態目錄承載資料庫與附加表，保留原 V1 工作區的使用方式。切換或搬移前要確認要由哪一份資料庫維護狀態。
- 新增 `runs` 為 V2 生命週期表。可沿用 V1 Run ID 格式及條件更新策略。需要承接的舊 run 可透過可空的 `legacy_task_id` 連回 `receipts(task_id)`。對缺少 `run_id` 的舊收據保留原本的人工核對語義，避免憑空補寫授權、模型、時間或驗收狀態。舊結果可走相容收集流程，無須批次改寫歷史資料。
- 新增 `cards` 保存派工範圍、觸發條件、內容雜湊及核准前狀態。新增 `grants` 關聯 `cards(card_id)`，管理核准方式、期限、允許範圍與扣還筆數。`runs.grant_id` 對應 `grants(grant_id)`。檢查全域併發、預留筆數及建立 `RESERVED` run 要在同一個 `BEGIN IMMEDIATE` 交易完成。
- `schema_meta`、`run_events`、`quota_snapshots`、`run_costs`、`suggestions` 皆為新表。`receipts` 未保留狀態事件、額度快照或採納統計。V2 遷移與備份由後續階段實作。

## 3. V1 離線能力與人工 GUI 的證據

- **已實作的離線流程。** `claim()` 用 `BEGIN IMMEDIATE` 序列化同一任務的查詢與新增，同 ID 同指紋回報 `DUPLICATE`，不同指紋回報 `TASK_ID_CONFLICT`。正常流程僅允許以 `WHERE status='CLAIMED' AND run_id=?` 取得送出權。`record()` 用允許的來源狀態更新，避免覆寫已定案收據。這是單一資料庫內的同任務防重複，不是跨任務的併發閘門。[防重複與條件更新](../../handoff_sim.py#L56)
- **已實作的 V1 原子發布。** `run()` 先提交 `DISPATCHING`、期限與驗收條件，再建立專用子目錄。它以獨占模式寫 `task.md.tmp`，執行 `flush()`、`fsync()`，再用同目錄 `replace()` 發布 `task.md`。測試替身依序用暫存檔發布 `result.md` 及 `DONE`。正式模組負責讀取和驗收結果，未包含通用結果發布器。[任務發布](../../handoff_sim.py#L104)、[替身發布](../../test_handoff_stage2.py#L17)
- **已實作的離線驗收及復原。** `read_completion()` 核對 UTF-8、Run ID、結果內容、權限訊號、讀取前後修改時間及 `task.md ≤ result.md ≤ DONE ≤ deadline`。`await_result()` 用單調時鐘配合絕對期限等待檔案。`recover(task_id)` 先看既有收據和檔案，無法確認時保留 `SUBMISSION_UNKNOWN`，復原流程不呼叫 `worker.submit()`。舊版缺 Run ID 的未定案收據轉人工核對。已定案的 `TIMEOUT` 不會改為成功。[復原與驗收](../../handoff_sim.py#L143)
- **人工 GUI 的歷史證據。** 正式規格 §2.1／§2.2 記載 Sol → Terra Low → Antigravity Gemini Flash → 檔案交接在兩台電腦曾成功，學校端使用固定工作區加相對路徑。V1 README 記載第二台通過離線測試的部署回報。本輪另以唯讀方式核對一份既有工作階段合成案例，`task.md`、`result.md` 首行及 `DONE` 的 Run ID 相符。三份檔案證實交接格式，無法單獨證實機器、實際模型、額度或送出回執。本輪未重跑 GUI，也沒有把人工流程當成 repo 中可執行的 Adapter。[人工操作規範](../../ROLES.md)、[正式規格 §2](MultiAgentHarness_V2_Final_Spec.md#2-現況基礎與事實驗證項目)

## 4. V2 功能對照

| V2 元件或能力 | V1 狀態 | 可用證據及缺口 |
| --- | --- | --- |
| V1 任務 ID、Run ID 與防重複 | 已實作 | `claim()` 和 `run()` 可重用，同一邏輯任務的識別與搶占已有離線測試。 |
| SQLite 本機收據 | 已實作 | `connect()`、`find()`、`receipt()`、`record()`，已有舊欄位的增量補欄。V2 的備份、版本中繼資料及關聯表仍待新增。 |
| Grant Ledger 與派工卡 | 尚未實作 | V1 JSON 的額度上限是設定值，由 Doctor 核對。V1 程式未實作核准、有效期、範圍、交易扣還額度、跨任務併發或同 repo 授權取代。 |
| Run Manager、事件與狀態機 | 部分實作 | V1 有條件更新、局部狀態機及單筆復原。V2 的完整狀態、事件歷程、pid／conversation ID、啟動掃描與人工處置命令尚缺。 |
| 原子寫入與 `DONE` 驗收 | 部分實作 | V1 任務發布與純文字交接檢查可重用。V2 通用原子寫入、JSON 結果與帶雜湊的完成標記待新增。 |
| `gui-terra` Adapter | 尚未實作 | 已有人工操作規範及歷史交接。程式沒有 `handoff.json`、`mah gui mark`、模型觀察欄位或相容轉換器。 |
| `codex-exec`、`agy-print` 及 Adapter Registry | 尚未實作 | V1 的 `worker.submit()`／`needs_permission()` 為模擬介面。Doctor 的安裝線索無法驗證真實旗標、訂閱額度或程序事件。 |
| Verifier | 部分實作 | V1 有固定格式 Markdown 驗收。尚缺四種模板 schema、獨立驗收命令、diff、唯讀違規檢查及獨立採納狀態。 |
| Controller、`mah` CLI、Skill 與模板 | 尚未實作 | 有角色說明及部署／診斷腳本。沒有派工 CLI、Skill、觸發條件或模板登錄。 |
| Workspace Manager | 部分實作 | V1 有指定目錄、任務子目錄及本機路徑解析。尚缺 V2 repo 外的路徑規範、Git worktree 建立、隔離、合併與清理。 |
| Quota & Cost Recorder | 尚未實作 | 未保存平台額度、token、牆鐘成本或採納率。 |
| Windows 部署及診斷 | 部分實作 | 可沿用 V1 初始化及 Doctor 探測方法。V2 TOML、Adapter 探測及同步目錄檢查待實作。 |

### 狀態字串映射（Spec §6.1–§6.4）

| V1 實際狀態 | V2 映射及語義差異 |
| --- | --- |
| `CLAIMED` | 接近準備前的 `RESERVED`，但 V1 只有任務占位、未扣額度。`PREPARED` 是 V2 新增的「任務與工作區已就緒」。 |
| `DISPATCHING` | 接近 `LAUNCHING`。V1 於建立任務目錄前已進入此狀態，涵蓋 V2 的部分準備階段。 |
| `WAITING` | 接近 `RUNNING`。V1 以模擬提交呼叫返回為依據，未保存外部送出回執或程序資訊。 |
| `SUBMISSION_UNKNOWN` | 同名可沿用。V1 可讀既有結果，但尚缺程序與對話證據、`--attach`、`--mark-failed`、`--abandon`、`--redispatch` 等操作。 |
| `SUCCEEDED` | 同名可沿用。V1 表示固定檔案驗收成功。V2 還需經 `COLLECTING` 與 schema 驗證，並獨立記錄是否採納。 |
| `INVALID_RESULT` | 可映射為 `FAILED` 並保留明確驗收原因，包含缺檔、錯誤識別碼或格式等。 |
| `HANDOFF_ERROR`、`PERMISSION_REQUIRED` | 不能按名稱一律映射為 `FAILED_PRE_SUBMIT`。依出錯時點及外部送出證據判定 `FAILED`、`FAILED_PRE_SUBMIT` 或 `SUBMISSION_UNKNOWN`，並保留原因。 |
| `TIMEOUT` | V1 已定案，後續 `DONE` 不可翻案。V2 `TIMED_OUT` 可經 `resolve` 進入 `COLLECTING` 或 `FAILED`，須保留兩種語義。 |
| `DUPLICATE`、`TASK_ID_CONFLICT`、`NOT_FOUND` | 呼叫結果，沒有寫入 `receipts.status`，也不應視為 V2 run 狀態。 |
| 其他 V2 新增狀態 | `COLLECTING`、`CANCELLED`、`ABANDONED`、`FAILED_PRE_SUBMIT` 沒有 V1 同名狀態。Grant 的 `ACTIVE`、`EXHAUSTED`、`EXPIRED`、`CLOSED`、`SUPERSEDED` 及驗收的 `PENDING`、`ACCEPTED`、`REJECTED`、`MERGED`、`DISCARDED` 皆需新增。V1 類別常數 `ACTIVE` 只是可變動的收據狀態集合。 |

### 可直接沿用的程式

| V1 程式 | 可沿用的用法 |
| --- | --- |
| `Outcome`、`validate_handoff()` | 保留 V1 回傳契約。可作為已知預期行的 V1 Markdown 驗收器，不能替代 V2 JSON schema。 |
| `LocalCoordinator.connect()`、`find()`、`receipt()` | 繼續存取原 V1 收據。保留 SQLite Row 及大小寫不敏感的邏輯鍵查詢。 |
| `LocalCoordinator.claim()`、`record()`、`run()` | 保留整套 V1 流程。V2 包裝或增量擴充時，沿用 Run ID、交易與條件更新原則。`run()` 內的指紋與檔案發布目前是內嵌程式，尚無獨立工具模組。 |
| `LocalCoordinator.read_completion()`、`await_result()`、`recover()` | 可作為 V1 資料的相容收集與復原入口。呼叫者要提供 V1 預期內容，並維持舊期限和回傳狀態。 |
| `Resolve-HarnessPath`、`Read-HarnessLocalConfig`、`Test-Python`、`Find-App` | 保留舊部署入口及本機路徑、Python／SQLite、安裝線索的檢查方式。V2 TOML 和 CLI 能力探測需要自己的實作。 |
| `MockAntigravity`、`publish()`、`success()` 和強制中斷案例 | 可供離線回歸及新測試設計參考。這些是測試替身。 |

## 5. 衝突、設計待確認與 Phase 0B 阻礙

| 發現 | 對應規格及建議 |
| --- | --- |
| V1 與 V2 的識別、檔案與雜湊契約不同。 | §5.1、§5.2、§10.4。V1 使用 `<directory>/<task_id>` 與純文字 `DONE`。V2 以 `runs/<run_id>` 為主，CLI 的 `DONE` 是含結果雜湊的 JSON。收集時按協定版本處理。V1 已知預期值驗收不能直接套到未知格式的歷史人工結果。 |
| V1 建構子補欄時沒有自動備份。 | §5.1、計畫 §0.2。V1 `expected` 也可能含原始值。建議保留舊表與歷史語義，V2 在任何遷移前備份，並把驗收原文限制在本機 run 檔案。 |
| V1 的預設 SQLite 路徑在 repo 的忽略目錄內，JSON 允許自訂工作區。 | §4.9、§11.1。V2 `state_dir`、`worktree_root` 須位於 repo 外並排除同步目錄。應保留 V1 JSON 及入口，另外接入 V2 TOML。V1 Doctor 要求 GUI 停用、人工監看及試行一次送出，不能直接改作 V2 額度執行器。 |
| V1 期限涵蓋準備，且 `worker.submit()` 同步呼叫，輪詢逾時無法終止該呼叫。V1 `TIMEOUT` 一旦落盤就終結。 | §6.1、§6.4、§10。分清準備、啟動、執行與收集期限；為 V2 的 Adapter 定義可中止及可辨識的送出證據，獨立處理 `TIMED_OUT` 復原。 |
| V1 僅用同任務條件更新防重複，復原按需查單筆，`DISPATCHING` 沒有所有者證據。 | §4.3、§4.4。V2 須交易式控制所有 run 的本機併發，並記錄程序、啟動時間或 GUI 回報。應避免把仍在執行的工作誤判為孤兒。 |
| 正式規格與實作計畫已有 D-1 至 D-6 的介面處置。 | 後續按 [實作計畫 §3](MultiAgentHarness_V2_Implementation_Plan.md) 處理。另需釐清 D-6 所提 Trigger 與計畫 §0.2 只允許建表／加欄的遷移限制；未釐清前可把應用層交易檢查列為必要。 |
| §10.4 的 `--not-submitted` 會取消並歸還筆數，§6.1 未畫出 `LAUNCHING → CANCELLED`。 | 需明訂可接受的未送出證據與對應轉移，避免把送出不明算為可歸還。§6.4 的無旗標復原與 Phase 3「只輸出證據」也須明確界定何時允許變更狀態。 |

Phase 0B 的主要未決事實包括官方 `agy` 的 Windows 可用性、旗標、編碼、權限、輸出 schema 與訂閱扣量；`codex exec` 巢狀呼叫和 Windows 原生核准攔截；以及雙機 GUI 的可辨識模型／思考程度、實際送出回執、取消與額度證據。Spec §2.3 的 V-A、V-B、V-C、V-D 結果及路徑 A／B／C 均留待使用者確認後驗證。

## 6. 文件追蹤

前置整理保留原有 `*` 白名單，只放行必要的兩層目錄與兩份定稿、本盤點共三份 Markdown 檔。以 `git check-ignore` 驗證其他 Markdown、SQLite、Token、環境設定及暫存目錄仍被忽略。兩份定稿的內容以整理前後的 SHA-256 核對。
