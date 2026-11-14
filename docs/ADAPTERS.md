# 新增 Adapter

本文說明如何為 zMAHarness 新增一個 Worker Adapter。新增 Adapter 只需要新的 Adapter 模組與一筆 registry 註冊，不需要修改 `cli.py`、`ledger.py`、`run_manager.py`、`verifier.py` 或 `config.py`。

> 注意：新的 Adapter 一律實作下方的 Protocol，並經由 `RunManager.dispatch` 派工。`mah/fake_adapter.py` 的 `FakeAdapter` 是自帶 `dispatch` 流程的舊式測試替身，不在 Protocol 的適用範圍內，請勿參考它的寫法。

## 1. 實作 Adapter 介面

介面定義在 [`mah/adapters/base.py`](../mah/adapters/base.py) 的 `Adapter`（`typing.Protocol`，可用 `isinstance` 檢查）。`RunManager.dispatch` 依序呼叫下列成員：

| 成員 | 契約 |
| --- | --- |
| `id` | Adapter 名稱，寫入帳本 `runs.adapter` 與卡片目標，必須與註冊名稱相同。 |
| `manager` | 綁定的 `RunManager`。`dispatch` 會拒絕綁定其他 Manager 的實例。 |
| `prepare_invocation(...)` | 在預留 Grant 之前驗證輸入，建立固定的啟動計畫。正式模型派工須等到啟動權核銷。此步驟可執行版本、參數等檢查。Codex 的工具宣告探測只使用本機替身、假金鑰與合成提示。回傳物件須提供 `workspace_kind`、`cwd`、`timeout_s`。拒絕時丟出 `LedgerError`。 |
| `launch(plan)` | 先呼叫 `manager.claim_launch(run_id, invocation=...)`，再啟動 Worker 一次。這是唯一可以啟動 Worker 的地方。 |
| `observe(handle)` | 等待 Worker 結束或達到核准期限，回傳觀測結果，不得發布結果。 |
| `persist_capture(handle, capture)` | 把觀測結果封存到 Run 目錄，讓復原時不必重新啟動。 |
| `collect_saved(run_id, **options)` | 只讀取已封存的觀測，推進 Run 狀態並回傳摘要。不得啟動 Worker。CLI 的 `resolve` 與 `collect` 也會呼叫它。 |

另外提供一個工廠函式 `factory(manager, config) -> Adapter`，從本機設定建立實例。例如 `AgyPrintAdapter.from_config` 會在這裡解析 `agy_command` 的路徑。

**執行檔解析。** 以名稱查找 Worker 執行檔時，請用 [`mah/executable_path.py`](../mah/executable_path.py) 的 `which`，不要用 `shutil.which`：Windows 上的 `shutil.which` 會先搜尋目前目錄，目標專案放一個同名 `.exe` 就會被當成 Worker 執行。`which` 只走 PATH 的絕對路徑項目，略過 `.`、相對路徑與目前目錄。Adapter 若提供 `find_executable()`，`RunManager.dispatch` 會在呼叫 `prepare_invocation`（也就是探測）之前確認解析結果不在 `repo_root`、工作目錄或 `worktree_root` 內，否則以結束碼 14 拒絕；`mah preflight` 也做同樣的檢查。

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
| `allows_delegate_implement` | `delegate-implement` 是延後階段的功能。沒有這個旗標的 Adapter，卡片建立（結束碼 2）、核准與預留（結束碼 10）都會拒絕；目前只有內部測試替身 `fake` 設定它。 |

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

基底另有幾個選用掛鉤，預設值不會改變 metadata、程序收據或收據的位元組（`claude-print` 沒有使用它們）：

- `argv_context(run_id, directory, cwd)`：傳給 `build_argv` 的額外參數，例如 Run 目錄或工作目錄的路徑。
- `invocation_files(run_id, template)`：在啟動前寫入 Run 目錄的檔案（`{名稱: 位元組}`），雜湊綁定在 `invocation.json` 的 `launch_files`。
- `output_files`：CLI 自己寫進 Run 目錄的檔案名稱。結束後封存進收據（`outputs_sha256`），`collect_saved` 與 `capture_matches` 都會核對雜湊。
- `normalize_capture(capture)`：解析整份收據；預設只讀 stdout。

### codex-exec

`codex-exec`（[`codex_exec.py`](../mah/adapters/codex_exec.py)）只啟動原生 `codex.exe`，**只支援 Context-Bundle**。`-s read-only` 無法把讀取範圍綁在 Disposable Worktree 內。

- Disposable Worktree 與非唯讀模板一律在預留 Grant 之前以結束碼 17 拒絕。預設設定 `defaults.delegate_implement.adapter` 雖然仍是
  `codex-exec`，delegate-implement 卡片在建立時就以結束碼 2 拒絕（延後階段的功能，所有 Worker 都一樣）。
- 執行期間 JSONL 只要出現 `agent_message`、`reasoning`、`error` 以外的 item（0.154.0 的工具類有 `command_execution`、`file_change`、
  `mcp_tool_call`、`collab_tool_call`、`web_search`、`todo_list`，也包括日後新增的類型），結果一律判為 `FAILED`，原因 `codex_tool_item_observed`，
  不寫出 `result.json` 與 `DONE`；收據的 `observation.tool_items` 記錄實際類型。`error` 是 CLI 的提示（smoke 中為 Code Mode 不可用），不算工具。
- 結構化結果以 `-o` 寫出的 last message 檔為準，並必須與 JSONL 最後一則 agent message 一致；`--output-schema` 由共用的
  `_response_schema` 以程式轉換成嚴格相容版（移除 `minLength` 等關鍵字、`const` 改成單值 `enum`），Verifier 仍以完整模板規則驗收。
- 派工前以空白 `CODEX_HOME` 檢查 `features list`，並用同版 CLI 實際探測頂層 `tools` 和 `input` 裡的 `additional_tools`。只接受空集合或 `functions.exec`、`functions.wait` 這一組，再加上該模型中繼資料宣告、已在下方查證的 EXPLAINED_TOOLS。這兩個 code-mode 宣告可見時，執行端的 `code_mode_host` 與 `disable_in_process_fallback` 必須皆為 `false`。多出任何工具或無法驗證時，以結束碼 17 拒絕。日後若 Codex 提供正式的零工具入口，改用該入口。
- 正式端使用 ChatGPT 登入下的 `CODEX_HOME/models_cache.json`。探測複製這份快取到全新的無憑證 `CODEX_HOME`，並經由 `127.0.0.1` 的 `/models` 提供完全相同的位元組。必須先觀察到 CLI 取得目錄，才接受後續工具宣告。來源、複本、HTTP 回應與模型項目的雜湊寫入 `invocation.json`。探測結果以執行檔、模型、effort、參數與功能狀態、模型中繼資料為鍵存放在 state。啟動前會重新核對，證據不同就拒絕。
- 正式 argv 固定含 `--ignore-user-config`（3b 起），所以 `config.toml` 的 `mcp_servers`、`plugins`、`[features]` 不會載入 Worker，和探測使用的空白 `CODEX_HOME` 等價（rust-v0.169.0 [`config/src/loader/mod.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/config/src/loader/mod.rs#L550-L566) 會把使用者層換成空表）。
- 快取不新鮮（距 `fetched_at` 超過 270 秒）時，prepare 會用正式環境執行一次 `codex debug models`（有網路、不推論）刷新後再讀取；0.169.0 實測會改寫快取。launch 緊接在 prepare 之後，快取仍新鮮，不會再刷新。期間若有其他 Codex 程序改寫快取，hash 不同就以結束碼 17 拒絕，重新派工即可。
- `codex-exec` 與 Codex Desktop 共用同一份 ChatGPT 額度。額度用完時，CLI 在 JSONL 回報 `error` 與 `turn.failed`，Run 以 `FAILED` / `process_failed` 結束，模型沒有推論。
- 不鎖定 CLI 版本：快取的 `client_version` 必須等於實際執行的 `codex --version`。升級後只要功能清單或工具宣告不合格，就以結束碼 17 拒絕。
- 殘餘風險：`debug models` 不接受 `--ignore-user-config`，會讀 `config.toml`；若其中設定了非預設的模型供應商，或 Codex 的正式程序在啟動後自行刷新模型目錄，正式執行看到的目錄可能和探測不同。JSONL 工具項目檢查與工作目錄前後快照仍會執行，涵蓋可觀察的工具事件與檔案變更。
- **模型中繼資料宣告的工具（EXPLAINED_TOOLS）。** `gpt-6.1-sol` 的 `experimental_supported_tools` 含 `send_message_to_user_async`，CLI 會依中繼資料註冊這個工具，`--disable` 關不掉（[`spec_plan.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/tools/spec_plan.rs#L1399-L1407)）。它只在該模型的中繼資料宣告它時才允許出現在探測集合中；Terra 等模型列的是名稱相近的 `send_user_message_async`，不會解鎖它。依 rust-v0.169.0 查證：
  - handler 只產生一則 `AgentMessage` item 並回傳 `{"accepted":true}`，不讀寫檔案、不執行程序、不連網（[`send_message_to_user_async.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/tools/handlers/send_message_to_user_async.rs#L59-L103)）。
  - 每次呼叫送一筆遙測，只含 thread、turn、call ID、工具名稱、時間與狀態，不含訊息內容（[`control_tool_analytics.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/tools/control_tool_analytics.rs#L29-L58)）。
  - 它在 JSONL 中顯示為一般的 `agent_message`，A7 的工具項目檢查看不出它被呼叫過（[`event_processor_with_jsonl_output.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L484-L495)）。內容一樣要經過 `-o` 與 JSONL 一致性檢查、schema 與 Verifier。
  - extension 生命週期 hook：exec 的 in-process app-server 不論功能開關都會註冊 11 個 extension（[`app-server/src/extensions.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/extensions.rs#L52-L142)）。item 完成時只呼叫各 extension 的 `on_item_completed_with_metadata`（[`session/mod.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/session/mod.rs#L3102-L3145)），預設實作什麼都不做（[`contributors.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/ext/extension-api/src/contributors.rs#L249-L270)）。唯一覆寫它的是 goal extension：只在有 state DB 時註冊，且只在 Goals 功能開啟時動作，而 `goals` 在固定 disable 清單裡；即使開啟，也只更新記憶體中的回合統計（[`goal/extension.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/ext/goal/src/extension.rs#L297-L312)、[`goal/accounting.rs`](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/ext/goal/src/accounting.rs#L175-L215)）。

  2026-11-15 的 Sol smoke（[CODEX_SMOKE_TEST.md](v2/CODEX_SMOKE_TEST.md)）派工當下，帳號回傳的 `gpt-6.1-sol` 中繼資料沒有宣告 `send_message_to_user_async`，B1 只擷取到 `functions.exec`、`functions.wait`，所以這個工具沒有在真實派工中驗證到。同一個模型的中繼資料在相隔數十秒的兩次抓取之間曾經不同，因此每次派工都要重新探測。Skill 目前仍建議 Terra。舊版真實派工收據仍可查核。
- **探測表**（`codex-cli 0.169.0`，2026-11-15，本機替身，不推論；每次派工前都會重新探測，這張表只是當時的紀錄）。
  所有模型的 `tool_mode` 都是 `code_mode_only`。B2 在每一列都相同：disable 78 項，仍開啟的只有 `secret_auth_storage`、`unified_exec`（`shell_tool` 為 `false`），以及 7 個 `removed` 階段的已說明功能。帳號另有兩個隱藏模型，不列在表中。

  | 模型 | effort | 擷取到的工具集合 | 結果 |
  | --- | --- | --- | --- |
  | `gpt-6.1-sol` | low、high | `functions.exec`、`functions.wait`、`functions.send_message_to_user_async` | 通過（中繼資料宣告了 `send_message_to_user_async`） |
  | `gpt-6.1-terra` | low、high | `functions.exec`、`functions.wait` | 通過 |
  | `gpt-6-astra` | low、high | `functions.exec`、`functions.wait` | 通過 |
  | `gpt-6-sol` | low、high | `functions.exec`、`functions.wait` | 通過 |
  | `gpt-6-luna` | low、high | `functions.exec`、`functions.wait` | 通過 |
  | `gpt-5.6-sol` | low、high | `functions.exec`、`functions.wait` | 通過 |
  | `gpt-5.6-terra` | low、high | `functions.exec`、`functions.wait` | 通過 |
  | `gpt-5.6-luna` | low、high | `functions.exec`、`functions.wait` | 通過 |

  在加入 EXPLAINED_TOOLS 之前，`gpt-6.1-sol` 兩種 effort 都以「functions 底下出現未預期的 `send_message_to_user_async`」被拒絕。6.x 的其他模型在中繼資料列的是 `send_user_message_async` 和 `clock`，0.169.0 沒有註冊這兩個工具；日後版本若註冊，B1 會拒絕。
- 出卡前檢查（`mah preflight`）也會經由 `codex app-server` 的 `account/rateLimits/read` 唯讀查詢 ChatGPT 額度（不推論）。只有明確查到 `ordinaryUsageAllowed` 為 `false` 時，出卡前檢查才以「ChatGPT 額度已用完，重設時間 <本地時間>」擋下；查詢失敗、逾時或格式無法解讀時不擋，Doctor 會顯示結果。程式只送出 `initialize`、`initialized` 和這個查詢，不使用額度重設券。
- 殘餘風險：`CODEX_HOME` 的全域 `AGENTS.md`（及 `AGENTS.override.md`）會注入 Worker，目前沒有可關閉的設定；`project_doc_max_bytes=0` 只擋專案層級的指示。
  這些指示可能影響回答內容，輸出仍受 schema 與 Verifier 約束。Doctor 偵測到非空的全域指示時會以「需人工驗證」提示。

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

- **agy 搬到共用基底。** `cli_worker.py` 是從 `AgyPrintAdapter` 移植並參數化的；為了不影響已通過真實驗收的 agy，這一階段沒有改 `agy_print.py`，所以兩者暫時有一份刻意保留的重複。`claude-print` 與 `codex-exec` 都已使用同一個基底並通過真實驗收，之後另開一個純重構階段，把 agy 搬到基底上，以 agy 現有的測試當護欄。
- **退款證據通用化。** 屆時把帳本退款判斷裡 agy 專屬的 `agy_capture_sha256` 通用化成 `capture_sha256`（基底已使用這個名稱），`claude-print` 就可以開啟 `refunds_unstarted_process`，讓確定沒啟動的程序退回 Grant。
- **帳本欄位與暫存檔。** CLI Worker 的帳本 `runs.model_observed`、`runs.cli_version` 目前為 null（實際值記在收據的 `observation` 與 `cli_version`），Run 目錄也會留下 `<prefix>.stdout.pending`、`<prefix>.stderr.pending`。這兩項延到「agy 搬上共用基底」的重構階段一起處理。

### 開源前修補階段延後的待辦

- **從 `.pending` 補封存的復原路徑。** 程序收據封存前若中斷，目前只留下 `.pending` 串流，需要一條從它們補封存、但不重新啟動 Worker 的復原路徑。
- **終止整個 Worker 程序樹。** 逾時或主控程序結束時，目前只終止 Worker 本身；改用 Windows Job Object，讓子程序一併結束。
- **清理指令。** 實作 Disposable Worktree 登記（`git worktree`）、快照提交與 Run 目錄的清理指令，並接上 `privacy.retain_run_dirs_days`（目前尚未實作，不會自動清理）。
- **卡片綁定快照 HEAD。** Disposable Worktree 卡片改為同時綁定快照提交的 HEAD，而不只綁定工作目錄身分與任務 SHA-256。
- **帳本 `model_observed`、`cli_version` 寫入與 `.pending` 殘留**：同上一項，排在 agy 搬上共用基底的重構階段。
- **收據雜湊鏈沒有金鑰。** 屬於同一 Windows 帳號下的已知限制，見 [DISCLAIMER.md](../DISCLAIMER.md)。
