# 新增 Adapter

本文說明如何為 zMAHarness 新增一個 Worker Adapter。新增 Adapter 只需要新的 Adapter 模組與一筆 registry 註冊，不需要修改 `cli.py`、`ledger.py`、`run_manager.py`、`verifier.py` 或 `config.py`。

> 注意：新的 Adapter 一律實作下方的 Protocol，並經由 `RunManager.dispatch` 派工。`mah/fake_adapter.py` 的 `FakeAdapter` 是自帶 `dispatch` 流程的舊式測試替身，不在 Protocol 的適用範圍內，請勿參考它的寫法。

## 1. 實作 Adapter 介面

介面定義在 [`mah/adapters/base.py`](../mah/adapters/base.py) 的 `Adapter`（`typing.Protocol`，可用 `isinstance` 檢查）。`RunManager.dispatch` 依序呼叫下列成員：

| 成員 | 契約 |
| --- | --- |
| `id` | Adapter 名稱，寫入帳本 `runs.adapter` 與卡片目標，必須與註冊名稱相同。 |
| `manager` | 綁定的 `RunManager`。`dispatch` 會拒絕綁定其他 Manager 的實例。 |
| `prepare_invocation(...)` | 在預留 Grant 之前驗證輸入，建立固定的啟動計畫，不得啟動任何程序或請求。回傳物件須提供 `workspace_kind`、`cwd`、`timeout_s`。拒絕時丟出 `LedgerError`。 |
| `launch(plan)` | 先呼叫 `manager.claim_launch(run_id, invocation=...)`，再啟動 Worker 一次。這是唯一可以啟動 Worker 的地方。 |
| `observe(handle)` | 等待 Worker 結束或達到核准期限，回傳觀測結果，不得發布結果。 |
| `persist_capture(handle, capture)` | 把觀測結果封存到 Run 目錄，讓復原時不必重新啟動。 |
| `collect_saved(run_id, **options)` | 只讀取已封存的觀測，推進 Run 狀態並回傳摘要。不得啟動 Worker。CLI 的 `resolve` 與 `collect` 也會呼叫它。 |

另外提供一個工廠函式 `factory(manager, config) -> Adapter`，從本機設定建立實例。例如 `AgyPrintAdapter.from_config` 會在這裡解析 `agy_command` 的路徑。

## 2. 填寫 AdapterPolicy

`AdapterPolicy` 是 frozen dataclass。帳本、Run Manager 與 CLI 只看這些旗標，不再比對 Adapter 名稱。所有預設值都是 `False`／`None`，與帳本中未註冊名稱（例如舊紀錄）原本的行為相同。

| 旗標 | 意義 |
| --- | --- |
| `user_configurable` | 可出現在本機 TOML 的 `adapters.enabled` 與 `defaults.*.adapter`。僅供測試或內部使用的 Adapter 設為 `False`。 |
| `requires_dispatch_scope` | 卡片與 Grant 必須綁定逾時、工作目錄與任務 SHA-256；啟動時必須提供綁定的 invocation。 |
| `attended_approval_only` | 非 `codex_prompt` 核准時，只接受 `manual_cli` 加互動終端確認。 |
| `window_approval` | 本機 Windows 核准視窗可核准只針對此類 Adapter 的卡片（目前僅供合成測試）。 |
| `replays_structured_output` | 結構化輸出衝突的 FAILED Run 可從封存收據重收集。 |
| `refunds_unstarted_process` | 帶有綁定收據雜湊時，`process_not_started` 可退回派工額度。 |
| `gui_submission` | GUI 型 Adapter：LAUNCHING 的 Run 可憑 `gui_not_submitted` 取消。 |
| `template_verified` | 只能由模板 Verifier 判定成功；採納總數須與已驗證結果相符。 |
| `run_id_prefix` | CLI 產生 Run ID 的前綴，格式為 `<prefix>-<uuid4 hex>`。 |

## 3. 註冊

正式 Adapter 在 [`mah/adapters/registry.py`](../mah/adapters/registry.py) 加入：

- `ADAPTER_POLICIES[name]`：政策。
- `ADAPTER_FACTORIES[name]`：工廠函式。只有能經由 CLI `dispatch` 派工的 Adapter 才註冊工廠，CLI 的 `--adapter` 選項由此產生。
- `CAPTURE_CHECKS[name]` 與 `CAPTURE_REASONS[name]`（選用）：收據完整性檢查與失敗時的原因字串，見下一節。
- `EXECUTABLE_LOOKUPS[name]`（選用）：`lookup(config) -> 路徑或 None`，供 `mah preflight` 唯讀確認 Worker 執行檔，不得執行它。
  `EXECUTABLE_MISSING_REASONS[name]`（選用）提供找不到時給使用者看的一句話。

工廠與檢查函式請在函式內延遲 import Adapter 模組，避免 registry 載入時就載入 Worker 實作或造成循環 import。測試中可以用 `registry.register(...)` 與 `registry.unregister(...)` 暫時註冊。

**未知的 Adapter 名稱。** 入口已經會拒絕未註冊的名稱：本機設定的 `adapters.enabled` 只接受 `user_configurable` 的名稱，
卡片目標必須是已啟用的 Adapter，`dispatch` 與 `preflight` 的 `--adapter` 只接受有工廠的名稱。`policy_for` 對未註冊名稱回傳的
`DEFAULT_POLICY` 只用在讀取帳本裡的舊紀錄，讓舊 Run 仍可查詢與收尾，不會讓新的派工通過。

### 外部 CLI Worker 的共用基底

啟動本機 CLI 子程序的唯讀 Worker，請繼承 [`mah/adapters/cli_worker.py`](../mah/adapters/cli_worker.py) 的 `CliWorkerAdapter`，
並參考 `claude-print`（[`claude_print.py`](../mah/adapters/claude_print.py)）。基底負責 InvocationPlan、工作目錄快照與基線比對、
`claim_launch`、以 stdin 傳入提示詞、Popen、逾時與終止、`persist_capture`、`collect_saved`、狀態處理與收據完整性檢查。
子類別只提供：收據檔名前綴、執行檔解析、必要參數、參數向量、唯讀參數檢查、子程序環境、輸出解析，以及收據檢查的原因字串。
基底只啟動原生 `.exe`，參數向量的第一個元素必須等於重新解析出的執行檔；不經由 `.cmd`、`.bat`、`.ps1` 或 `cmd.exe` 轉呼。
程序確定沒有啟動時，Run 記為 `SUBMISSION_UNKNOWN`（原因 `process_not_started`），Grant 不退回，由 `mah resolve <run_id> --mark-failed` 收尾。

## 4. 選用能力：收據完整性檢查

若 Adapter 會封存可獨立核對的程序收據，可註冊 `check(run_directory, run_summary, result_sha256) -> bool`，並以 `capture_reason` 提供原因字串。Verifier 在結果已完成時呼叫它；回傳 `False` 時驗收維持 `pending`，原因為該 Adapter 註冊的字串（`agy-print` 為 `agy_capture_integrity`，`claude-print` 為 `claude_capture_integrity`；沒提供時為 `capture_integrity`）。沒有註冊檢查時 Verifier 直接略過。繼承 `CliWorkerAdapter` 的 Adapter 可直接註冊它的 `capture_matches`。

## 5. API 類 Adapter 的限制

透過 HTTP API 呼叫模型的 Adapter（例如 OpenAI 相容 API）無法讓模型唯讀查閱工作樹，因此只能使用 **Context-Bundle**，`prepare_invocation` 必須拒絕 Disposable Worktree。

## 6. 必要測試

在 `tests/v2/` 新增測試，至少涵蓋：

- 實例通過 `isinstance(adapter, Adapter)`。
- 政策旗標與預期行為一致（核准來源、範圍綁定、Verifier 驗收等）。
- 經由 `RunManager.dispatch` 完成一次離線派工：建立卡片、核准、派工、Verifier 驗收、採納。可參考 `tests/v2/test_adapter_registry.py` 的 `DummyAdapter`。
- 中斷與復原：`collect_saved` 不會重新啟動 Worker。
- 不得在測試中呼叫真實模型；以替身取代外部程序或網路請求。

## 7. 合併前的真實派工驗收

離線測試通過後，合併前必須完成**至少一次真實派工驗收**：以真實 Worker 完成卡片、核准、派工、主控獨立查核、`verify` 與 `accept`／`reject-result`，並在 README 的「Worker 驗收狀態」照實記錄驗收範圍。尚未驗收的部分必須標明。

## 8. 後續

- **agy 搬到共用基底。** `cli_worker.py` 是從 `AgyPrintAdapter` 移植並參數化的；為了不影響已通過真實驗收的 agy，這一階段沒有改 `agy_print.py`，所以兩者暫時有一份刻意保留的重複。等 `codex-exec` 也用同一個基底並通過真實驗收後，另開一個純重構階段，把 agy 搬到基底上，以 agy 現有的測試當護欄。
- **退款證據通用化。** 屆時把帳本退款判斷裡 agy 專屬的 `agy_capture_sha256` 通用化成 `capture_sha256`（基底已使用這個名稱），`claude-print` 就可以開啟 `refunds_unstarted_process`，讓確定沒啟動的程序退回 Grant。
