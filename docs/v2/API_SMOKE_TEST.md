# openai-api 真實 Smoke Test（4a-1 精簡版）

## 目的與範圍

依第 4 階段規格（`spec-api-worker.md` §9.2，S0～S10）在 `phase4a-1` 分支（基準 commit `3961358`）對 `openai-api` 執行一次免費的本機 LM Studio 真實派工驗收，確認 `preflight` → 對話出卡 → `bundle` → `card` → `grant` → `dispatch` → 獨立查核 → `verify` → `accept` → 重複 `collect` → 伺服器關閉後 `preflight` 阻擋的完整鏈路。

4a-1 尚未實作 `mah route` 與 `mah models` 的 `openai-api` 整合，本次不經 `mah route`，由主控依 `mah preflight` 結果手寫卡片，仍完整遵守先出卡、暫停、收到使用者同意回覆後才建卡核准派工的規則。

## 環境與設定

- 作業系統與執行環境：Windows 11、PowerShell 5.1、Python 3.13（只使用標準函式庫）。
- 主控環境：Antigravity（沙盒會以 `WinError 10013` 阻擋 loopback 連線，所有 `mah` 指令皆經使用者核准於沙盒外執行）。
- 本機推論伺服器：LM Studio 本機伺服器，綁定位址 `http://127.0.0.1:1234`（未開啟區域網路服務、未啟用 API 權杖驗證），載入非思考型指令模型 `qwen2.5-7b-instruct-1m`。
- 本機設定檔：`C:\Users\<user>\AppData\Local\zMAHarness\harness.local.toml`（修改前先備份為 `harness.local.toml.bak`）：
  - `[adapters].enabled` 加入 `"openai-api"`，其餘欄位保持不變。
  - 新增 `[api]`（`max_output_tokens = 8192`、`connect_timeout_s = 10`）與 `[api.providers.lmstudio]`（`kind = "lmstudio"`、`base_url = "http://127.0.0.1:1234/v1"`），未寫入任何金鑰。

## 核准依據

S5 在對話中以原生互動卡片提出單檔 `mah/adapters/base.py`（92 行，4,351 bytes）的 Context-Bundle Review 協作建議：

- 【觸發原因】`T5 · 使用者主動要求`
- 【建議模型】`lmstudio:qwen2.5-7b-instruct-1m · medium`
- 【任務與範圍】`Review · Context-Bundle（本機） · mah/adapters/base.py`
- 【授權限制】`僅 1 次 · 唯讀（自動等待模型完成；派工時需核准一次沙盒外執行）`
- 無【計費】行（本地 provider，`billing_notice = null`）。

出卡後立即暫停，直到使用者明確點選「繼續協作（主控 Agent 建議：`lmstudio:qwen2.5-7b-instruct-1m · medium`）」後，才在下一輪執行 `mah card create`、`mah grant approve --via codex_prompt` 與 `mah dispatch`。

## 執行經過（S0～S10）

| 步驟 | 執行內容 | 結果 |
| --- | --- | --- |
| S0 | 4a-1 離線測試與乾淨 clone 檢查 | 事前於 `phase4a-1`（`3961358`）通過 |
| S1 | 啟動 LM Studio 本機伺服器（`127.0.0.1:1234`）並載入 `qwen2.5-7b-instruct-1m` | 伺服器執行中，`/v1/models` 已列出目標模型 |
| S2 | 備份並更新 `harness.local.toml` | 已加入 `"openai-api"`、`[api]`、`[api.providers.lmstudio]`，無金鑰 |
| S3～S4 | `python -B -m mah preflight --repo <repo> --adapter openai-api --model lmstudio:qwen2.5-7b-instruct-1m` | 結束碼 `0`；`ok: true`、`billing: "local"`、`billing_notice: null`、`adapter_executable_found: null` |
| S5 | 對 `mah/adapters/base.py` 提出 Context-Bundle Review 協作卡並暫停 | 使用者回覆同意使用建議模型 |
| S6 | `bundle create`、`bundle check`、`card create`、`grant approve --via codex_prompt`、`dispatch` | `dispatch` 結束碼 `30`；`state: "COLLECTING"`、`state_reason: "structured_result_collected"`；HTTP `200`；耗時 `11,358 ms` |
| S7 | 主控獨立查核帳本欄位、收據完整性與 Worker Findings | 全部項目符合驗收條件（詳見下節） |
| S8 | 寫入 Controller 證據檔後執行 `mah verify` 與 `mah accept --adopted 0 --total 5` | `verify` 結束碼 `0`（`SUCCEEDED` / `result_verified`，`decision: "verified"`）；`accept` 結束碼 `0`（`ACCEPTED`） |
| S9 | 再次執行 `mah collect api-8d1f3ea7f54541b3aa631e04d4841135` | 結束碼 `0`；狀態維持 `SUCCEEDED` / `ACCEPTED`，未發出新推論請求（伺服器僅見 1 筆 `/v1/chat/completions`） |
| S10 | 使用者關閉 LM Studio 本機伺服器後，沙盒外重跑 S4 的 `mah preflight` | 結束碼 `30`；`ok: false`；原因「找不到本機模型伺服器（127.0.0.1:1234），請先在 LM Studio 開啟本機伺服器」；帳本 Runs（15）與 Grants（15）筆數及既有 Grant 狀態皆未變動 |

## 派工摘要與收據查核（S6～S8）

- **Run ID**：`api-8d1f3ea7f54541b3aa631e04d4841135`
- **Card ID / Grant ID**：`card-419df268c0dd44c9a57a47207990c0bc` / `ef0c27b6883447479a5e266864d5145e`（`max_dispatches = 1`，派工後 `used_dispatches = 1`、`status = "EXHAUSTED"`）
- **請求目標**：`adapter = "openai-api"`、`model_requested = "lmstudio:qwen2.5-7b-instruct-1m"`、`effort_requested = "medium"`、`structured_output = "json_schema_strict"`
- **最終狀態**：`state = "SUCCEEDED"`、`state_reason = "result_verified"`、`verification.decision = "verified"`、`acceptance_status = "ACCEPTED"`
- **HTTP 與耗時**：`http_status = 200`、`finish_reason = "stop"`、`wall_milliseconds = 11358`
- **Token 統計**：`prompt_tokens = 1664`、`completion_tokens = 640`、`total_tokens = 2304`（原始回應另含 `completion_tokens_details.reasoning_tokens = 0`）
- **帳本欄位核對**：
  - `runs.pid = null`、`runs.exit_code = null`、`runs.cli_version = null`、`runs.effort_observed = null`
  - `runs.model_observed = "qwen2.5-7b-instruct-1m"`
  - `runs.conversation_id = "api:api-8d1f3ea7f54541b3aa631e04d4841135"`（與 `api.capture.json` 的 `observation.conversation_id` 一致）
  - `observation.response_id = "chatcmpl-2lx4avuutkf1rlxsg7uim8"`（只記於收據，不進帳本 `conversation_id`）
- **收據與安全核對**：
  - Run 目錄（`C:\Users\<user>\AppData\Local\zMAHarness\state\runs\api-8d1f3ea7f54541b3aa631e04d4841135`）共 10 個檔案齊全：`task.md`、`invocation.json`、`api.request.json`、`api.response.raw`、`api.process.json`、`api.capture.json`、`events.jsonl`、`result.json`、`DONE`、`verify.json`。
  - `capture_matches(run_dir, run_row, result_sha256)` 實測為 `True`；`mah verify` 通過 `api_capture_integrity` 與所有雜湊核對。
  - `api.request.json` 頂層鍵僅含 `max_tokens`、`messages`、`model`、`response_format`、`stream`，無任何工具相關鍵（`tools`、`tool_choice`、`functions`、`function_call`、`plugins`、`web_search_options`）。
  - `api.process.json` 回應標頭僅含白名單鍵 `content-type: application/json; charset=utf-8`（無 `set-cookie`）；所有收據檔皆不含請求標頭或 `Authorization`。
  - Bundle 工作目錄前後快照雜湊相同（`f19d17696e97a1bbd8f5a60a4f7317b9408ee9eccef02679620d115caf07d8d0`），`workspace_changed = false`，派工後實測目錄仍為空。

## Worker Findings 與主控獨立查核結論

Worker 回傳 5 項 `minor` 發現，主控逐一回到 `mah/adapters/base.py` 原始碼核對：

| # | Worker 引用 | 標題（嚴重度） | 主控回源碼查核結論 | 採納 |
| --- | --- | --- | --- | --- |
| 1 | `mah/adapters/base.py:1` | `Adapter Protocol Declaration`（`minor`） | 第 1 行為模組 docstring，`@runtime_checkable class Adapter(Protocol)` 實際起於 L46–L47；屬正面確認與一般維護提醒，非程式缺陷 | 否 |
| 2 | `mah/adapters/base.py:10` | `Default AdapterPolicy Flag Values`（`minor`） | `AdapterPolicy` 起於 L11–L12，`user_configurable: bool = False` 在 L20（引用行號偏約 10 行）；預設值與註解確實一致，不需修改 | 否 |
| 3 | `mah/adapters/base.py:29` | `AdapterPolicy Flag Completeness`（`minor`） | L19–L43 共定義 11 個旗標（含 `closes_grant_on_refund`），涵蓋完整；屬正面確認與維護提醒 | 否 |
| 4 | `mah/adapters/base.py:60` | `Documentation Accuracy`（`minor`） | L60 為 `Adapter.prepare_invocation`，旗標註解在 L19–L43；確認文件註解與契約描述清楚一致，不需修改 | 否 |
| 5 | `mah/adapters/base.py:85` | `Potential Edge Cases`（`minor`） | L84–L92 為 `persist_capture` 與 `collect_saved`，確認介面契約無邊界缺陷；屬一般審查建議 | 否 |

總計查核 `reviewed_findings = 5`，採納 `--adopted 0 --total 5`，`mah accept` 結案為 `ACCEPTED`。

## V 系列待查證項目實際觀察

| # | 項目 | 4a-1 精簡 smoke 觀察結果 |
| --- | --- | --- |
| V3 | LM Studio `/v1/models` 是否列出未載入的模型；未載入時首次請求的行為 | 本次測試前已先載入 `qwen2.5-7b-instruct-1m`，`/v1/models` 確實列出該模型且 `mah preflight` 通過。未載入模型是否出現在 `/v1/models` 及未載入時首次請求行為尚未在此批實測，留待 4a-2（`mah models`）階段確認。 |
| V4 | LM Studio 模型 ID 是否可能含 `TARGET_ID` 不允許的字元 | 本次實測模型 ID 為 `qwen2.5-7b-instruct-1m`，僅含英數字、`.`、`-`，與前綴組合成 `lmstudio:qwen2.5-7b-instruct-1m` 完全符合 `ledger.TARGET_ID`。其他可能含 `@` 等字元的 LM Studio 模型 ID 留待 4a-2 `mah models --all` 確認。 |
| V5 | LM Studio 回應的 `id` 格式、是否回報 `usage` | **已確認**。原始回應 `id` 為 `chatcmpl-2lx4avuutkf1rlxsg7uim8`（`chatcmpl-<隨機英數>`），`model` 與 `system_fingerprint` 皆為 `qwen2.5-7b-instruct-1m`，且完整回報 `usage`（`prompt_tokens`、`completion_tokens`、`total_tokens` 與 `completion_tokens_details.reasoning_tokens`）。額外觀察：非思考型模型仍在 `choices[0].message` 附帶 `"reasoning_content": ""` 與 `"tool_calls": []`；Adapter 正確將空陣列 `tool_calls` 視為正常回應、將空字串 `reasoning_content` 判定為 `reasoning_field_present: false`。 |
| V7 | LM Studio 本機伺服器的預設綁定位址、是否有 API 權杖選項 | 本次本機伺服器綁定於 `http://127.0.0.1:1234`（僅 loopback，不開啟區域網路服務，未啟用 API 權杖驗證），與規格 §2.1 設定一致。 |
| V8 | Windows 沙盒是否以 10013 擋 loopback | **於 Antigravity 環境確認**：Antigravity Windows 沙盒以 `WinError 10013` 阻擋 loopback，因此本次 `mah` 指令於沙盒外執行；S10 關閉伺服器後於沙盒外重跑 `mah preflight` 觸發連線被拒（`10061`），正確擋下並回報「找不到本機模型伺服器（127.0.0.1:1234）…」。Codex Desktop Windows 沙盒內的實際行為留待 Codex 主控實測（10061 與沙盒內 10013 分流已由離線測試涵蓋）。 |

註：為確保 S9 驗收條件「伺服器端只有一筆 `/v1/chat/completions` 請求」，本次未對不存在模型送出額外推論請求；V1、V2、V6、V12、V13 依規格留待 4a-2 或對應 provider 的 smoke 階段查證。

## 去識別化

本文件路徑中的本機使用者名稱一律以 `<user>` 取代；不記錄任何金鑰、憑證或私人專案資訊。
