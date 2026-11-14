# claude-print 單筆 Claude Smoke Test

## 目的與範圍

以合成的單檔 Context-Bundle，對 `claude-print` 做**一次**真實呼叫（`sonnet`、effort `low`、`review`），確認三件事：
提示詞可經由 stdin 傳入、`--output-format json` 的實際欄位（特別是實際模型），以及保存一份去識別化的收據作為離線測試 fixture。
這不是 README「Worker 驗收狀態」所說的真實派工驗收；真實派工驗收由使用者在實際專案完成後才更新狀態。

## 核准依據

使用者在對話中審閱完整的 smoke 腳本後同意執行（「Approve smoke」），並指示保留主控 Claude Code session 的環境變數、只修改 guard。
這個同意就是本次 `mah grant approve <card_id> --sha <SHA-256> --via codex_prompt` 的依據（`codex_prompt` 代表主控對話內核准）。
`manual_cli` 需要互動終端，主控無法代為操作。

## 環境

- Windows 11，PowerShell 5.1，Python 3.13。
- Claude Code CLI 2.1.325，由 npm 的 `@anthropic-ai/claude-code` 安裝。套件內 `bin\claude.exe` 存在，`claude.exe --version` 可直接執行，不需要 npm postinstall。
- 本次 `claude_command` 設為 **`claude.exe` 的絕對路徑**（`C:/Users/<user>/AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe`）。
  **沒有實測**從 npm 的 `claude.cmd` 推導 `.exe` 的路徑；那段解析規則由單元測試涵蓋（`tests/v2/test_claude_print.py` 的 `ClaudeExecutableTests`）。
- 臨時根目錄：`C:\Users\<user>\AppData\Local\Temp\zmah-claude-smoke-claude-00000000000000000000000000000000`，位於 Git 與雲端同步資料夾之外。
  以 `ZMAHARNESS_HOME` 指向它，臨時 TOML 只啟用 `claude-print`；正式的 TOML 與帳本完全沒有碰。依使用者指示保留，驗收後由使用者刪除。
- 第二次嘗試（見下方）留下另一個只建到 `git init` 的臨時根目錄 `C:\Users\<user>\AppData\Local\Temp\zmah-claude-smoke-claude-00000000000000000000000000000001`，同樣保留待刪。

## 執行經過

1. **第一次：guard 停下（呼叫模型之前，未建立任何檔案）。** 腳本原本只要有任何 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL` 就停。
   主控 Claude Code 桌面程式在自己的 process 設了 `ANTHROPIC_BASE_URL`（指向官方 `api.anthropic.com`），使用者與機器層級都沒有設定。
   使用者決定保留這個 session 的變數，讓 smoke 實測 Worker 環境過濾，並把 guard 改成：
   - `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`：Process、User、Machine 任何層級有設定就停（會影響計費）。
   - `ANTHROPIC_BASE_URL`：只有 User 或 Machine 層級有設定才停；Process 層級允許保留，不清除。
2. **第二次：在 `git add` 停下（呼叫模型之前）。** 主控用 `2>&1` 包住整個腳本，Windows PowerShell 5.1 因此把 Git 的 CRLF 警告當成終止錯誤。腳本未修改，改為不加 `2>&1` 重跑。
3. **第三次：完成。** 只呼叫一次模型，成功。

## 派工前的環境過濾證據（只記錄名稱，不記錄值）

派工前，在同一個環境中呼叫實際的 Worker 環境建構函式 `mah.adapters.claude_print.claude_environment()`：

- **斷言結果：** Worker 環境中沒有任何 `ANTHROPIC_*`、`CLAUDECODE` 或 `CLAUDE_CODE_*`（白名單刻意允許的 `CLAUDE_CONFIG_DIR`、`CLAUDE_CODE_GIT_BASH_PATH` 例外，本次兩者都未設定）；Process 層級的 `ANTHROPIC_BASE_URL` 出現在被濾掉的清單中。
- **傳給 Worker 的（16 個）：** `APPDATA`、`COMSPEC`、`HOMEDRIVE`、`HOMEPATH`、`LOCALAPPDATA`、`PATH`、`PATHEXT`、`PROGRAMDATA`、`PROGRAMFILES`、`PROGRAMFILES(X86)`、`SYSTEMDRIVE`、`SYSTEMROOT`、`TEMP`、`TMP`、`USERPROFILE`、`WINDIR`
- **被濾掉的（76 個）：** `AI_AGENT`、`ALLUSERSPROFILE`、`ANTHROPIC_BASE_URL`、`API_TIMEOUT_MS`、`BAGGAGE`、`CHROME_CRASHPAD_PIPE_NAME`、`CLAUDECODE`、`CLAUDE_AGENT_SDK_MCP_NO_PREFIX`、`CLAUDE_AGENT_SDK_VERSION`、`CLAUDE_CODE_ACCOUNT_UUID`、`CLAUDE_CODE_CHILD_SESSION`、`CLAUDE_CODE_DESKTOP_APP_VERSION`、`CLAUDE_CODE_DISABLE_CRON`、`CLAUDE_CODE_DISABLE_TERMINAL_TITLE`、`CLAUDE_CODE_EAGER_FLUSH`、`CLAUDE_CODE_EMIT_STARTUP_TIMING`、`CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES`、`CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL`、`CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING`、`CLAUDE_CODE_ENTRYPOINT`、`CLAUDE_CODE_HOST_SESSION_ID`、`CLAUDE_CODE_HOST_SHORT_TEMP_DIRS`、`CLAUDE_CODE_MESSAGING_SOCKET`、`CLAUDE_CODE_MESSAGING_TOKEN`、`CLAUDE_CODE_OAUTH_SCOPES`、`CLAUDE_CODE_ORGANIZATION_UUID`、`CLAUDE_CODE_REPORT_FINDINGS`、`CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH`、`CLAUDE_CODE_SESSION_ATTENDED`、`CLAUDE_CODE_SESSION_ID`、`CLAUDE_CODE_SIMPLE`、`CLAUDE_CODE_STARTUP_FAILURE_RESULTS`、`CLAUDE_CODE_TERMINAL_MCP_TOOLS`、`CLAUDE_CODE_USER_EMAIL`、`CLAUDE_PID`、`CLAUDE_PREVIEW_CLASSIFIER_FLOOR`、`COMMONPROGRAMFILES`、`COMMONPROGRAMFILES(X86)`、`COMMONPROGRAMW6432`、`COMPUTERNAME`、`COREPACK_ENABLE_AUTO_PIN`、`DISABLE_AUTOUPDATER`、`DISABLE_MICROCOMPACT`、`DRIVERDATA`、`GCM_INTERACTIVE`、`GIT_ASKPASS`、`GIT_EDITOR`、`GIT_TERMINAL_PROMPT`、`LOGONSERVER`、`MCP_CONNECTION_NONBLOCKING`、`MCP_SERVER_CONNECTION_BATCH_SIZE`、`NODEFAULTCURRENTDIRECTORYINEXEPATH`、`NODE_USE_SYSTEM_CA`、`NO_COLOR`、`NUMBER_OF_PROCESSORS`、`ONEDRIVE`、`ONEDRIVECONSUMER`、`OS`、`PROCESSOR_ARCHITECTURE`、`PROCESSOR_IDENTIFIER`、`PROCESSOR_LEVEL`、`PROCESSOR_REVISION`、`PROGRAMW6432`、`PROMPT`、`PSEXECUTIONPOLICYPREFERENCE`、`PSMODULEPATH`、`PUBLIC`、`PYTHONDONTWRITEBYTECODE`、`PYTHONIOENCODING`、`SENTRY-TRACE`、`USERDOMAIN`、`USERDOMAIN_ROAMINGPROFILE`、`USERNAME`、`USE_LOCAL_OAUTH`、`USE_STAGING_OAUTH`、`ZMAHARNESS_HOME`

## 實際參數向量

Context-Bundle。`--json-schema` 的內容（1182 個字元，依 review 模板產生）以 `<schema>` 代替：

```text
C:\Users\<user>\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe -p --output-format json --json-schema <schema> --model sonnet --effort low --restricted --strict-mcp-config --permission-prompts none --no-session-persistence --tools ""
```

提示詞（Run 標頭、固定指示與封裝後的 `task.md`）由 stdin 傳入，檔案為 Run 目錄的 `claude.prompt.txt`，雜湊記錄在 `invocation.json` 的 `prompt_sha256`。
參數中沒有任何禁止的旗標或工具：沒有 `dangerously`、`bypass`、`--bare`、`--add-dir`、`--allowedTools`、`--mcp-config`、`--settings`、`--resume`；
`--permission-mode` 未出現（Bundle 不使用）；`--tools` 為空字串。

## 結果

- 結束碼 0，stderr 0 位元組，`wall_milliseconds` 4090，工作目錄前後皆為空（`workspace_changed=false`）。
- 分類：`COLLECTING` / `structured_result_collected`；`dispatch` 依慣例回傳 30（已收集、待驗收）。
- **stdin 提示詞可行：** 模型依 stdin 中的指示回傳了結果。
- **`model_observed`：** 取自 `modelUsage` 唯一的鍵，為 `claude-sonnet-5-5`。輸出中沒有實際 effort 的欄位，`effort_observed` 填 `unknown`。
- **`result` 與結構化結果一致：** `result` 是 JSON 字串，內容與 `structured_output` 完全相同，所以保留 claude-print 的 response 一致性檢查。
  實際值（`run_id` 與驗證字串是合成資料）：

  ```json
  {"schema_version":"2.0","run_id":"claude-00000000000000000000000000000000","template":"review","status":"completed","summary":"isolation test|0123456789abcdef0123456789abcdef","blocked_reason":null,"open_questions":[],"template_payload":{"findings":[]}}
  ```
- **主控獨立查核：** summary 與合成的 `verify.txt` 標題及驗證字串一致，findings 為空，Bundle 工作目錄未變動。
- `mah verify`：`SUCCEEDED` / `verified`（`claude_capture_integrity` 收據檢查通過）。`mah accept --adopted 0 --total 0`：`ACCEPTED`。Run 正式結案。

## 輸出欄位（`--output-format json`）

頂層欄位共 28 個：`api_error_status`、`credential_expires_at`、`duration_api_ms`、`duration_ms`、`fast_mode_disabled_reason`、`fast_mode_state`、`first_content_frame_ms`、`is_error`、`modelUsage`、`num_turns`、`permission_denials`、`queued_turn_count`、`result`、`result_index`、`safety_stops`、`session_id`、`stop_reason`、`structured_output`、`subagent_stats`、`subtype`、`terminal_reason`、`time_to_request_ms`、`total_cost_usd`、`ttft_ms`、`ttft_stream_ms`、`type`、`usage`、`uuid`。

Adapter 使用的對應：`session_id` → `conversation_id`；`type == "result"`、`subtype == "success"` 且 `is_error == false` → `SUCCESS`；
`result` → response；`structured_output` → 結構化結果；另取 `num_turns`、`usage` 的四個 token 欄位、`permission_denials` 的數量與 `duration_ms`；
`modelUsage` 只有一個鍵時作為 `model_observed`，否則為 `unknown`。`total_cost_usd` 與 `modelUsage.*.costBasis="list"` 是 CLI 依定價表估算的金額；
本次以訂閱登入執行，未把它記錄為實際費用。

## 去識別化

Fixture `tests/v2/fixtures/claude-2.1.325-bundle-smoke-redacted.json` 保留原始的緊湊位元組格式，只替換：

- `session_id` → `00000000-0000-4000-8000-000000000001`
- `uuid` → `00000000-0000-4000-8000-000000000002`
- `credential_expires_at` → `<redacted>`

輸出中沒有使用者路徑或 email。本文件中的使用者路徑一律寫成 `C:\Users\<user>`；環境變數只記錄名稱。

## 殘留風險

- 即使加了 `--no-session-persistence`，claude 仍可能寫入自己的使用者層級狀態（例如 `~/.claude.json`、日誌）。唯讀保證只涵蓋專案與工作樹，與 agy 相同。
- Worker 與主控使用同一個 Windows 帳號；唯讀限制依賴 CLI 的 `--restricted`、`--tools` 與 `--permission-prompts none`，沒有 OS 層的強制。
