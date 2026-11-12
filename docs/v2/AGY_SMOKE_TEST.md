# agy-print 單筆 Gemini Smoke Test

## 方案 D Context-Bundle（待使用者核准）

此路徑只接受 `--bundle-smoke`、Manifest v2、`review`、固定指示、唯一的合成
`驗證文件.txt` 和單次 Grant。來源是獨立合成 Git 儲存庫，工人工作目錄是
`worktree_root` 下另一個全新的空目錄。Bundle 內容已內嵌，模型無須讀檔或使用任何工具。

本機官方 agy 1.5.5 的 `--version` 與 `--help` 均以 0 結束。`--settings` 說明支援
會話級內嵌 JSON，且不儲存設定。Adapter 固定要求 `--sandbox`、
`--disable-slash-commands`、拒絕工具與工作區外存取的設定，以及 `useG1Credits=false`。
不傳 `--mode plan`，避免已知旗標衝突。離線替身驗證實際 argv。真實 Permissions、Sandbox
及工具限制仍標記 `unknown`，同帳號缺少 OS 強制唯讀。本次試驗接受此殘留風險，僅使用
合成資料，工作目錄與前後快照只提供可觀察狀態的證據。

### 待核准的準備指令

取得本次測試的獨立核准後，在 Harness 根目錄以 PowerShell 7 操作。以下種子 Commit 只屬於
新建的合成儲存庫。先確認官方執行檔版本和路徑，並由使用者確認個人 Google AI Pro、
AI Credit Overages = Never、CLI Use AI Credits = Off、未使用 API Key 或 Cloud 計費。
保留可觀察的前置額度，讀不到的欄位填 `unknown`。任何帳號或費用條件不明時停在派工前。

```powershell
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$agyExe = Join-Path $env:LOCALAPPDATA 'agy/bin/agy.exe'
$runId = 'bundle-smoke-' + [guid]::NewGuid().ToString('N')
$root = Join-Path $env:LOCALAPPDATA "MultiAgentHarness/$runId"
$source = Join-Path $root 'source'
$workerRoot = Join-Path $root 'workers'
$worker = Join-Path $workerRoot 'worker'
$bundle = Join-Path $root 'bundle'
New-Item -ItemType Directory -Path $source, $worker -Force | Out-Null

$utf8 = [Text.UTF8Encoding]::new($false)
$token = [guid]::NewGuid().ToString('N')
$name = '驗證文件.txt'
[IO.File]::WriteAllText((Join-Path $source $name), "標題=隔離測試`n驗證字串=$token`n", $utf8)
[IO.File]::WriteAllText((Join-Path $source '.gitignore'), "!/$name`n", $utf8)
git -C $source init --quiet
git -C $source add -- $name .gitignore
git -C $source -c user.name='Harness Smoke' -c user.email='harness-smoke@example.invalid' commit --quiet -m 'Add synthetic bundle fixture'

$instructions = Join-Path $root 'instructions.md'
[IO.File]::WriteAllText($instructions, '只分析 Context-Bundle 內的驗證文件.txt。請勿使用工具、讀寫檔案、執行命令或連線。以「<標題>｜<驗證字串>」作為 summary，findings 回傳空陣列。', $utf8)
$config = Join-Path $root 'harness.local.toml'
$exe = $agyExe.Replace([char]92, [char]47)
$configLines = @('[paths]', 'state_dir = "state"', 'worktree_root = "workers"', '',
  '[adapters]', 'enabled = ["agy-print"]', ('agy_command = "' + $exe + '"'), '',
  '[approval]', 'mode = "manual_cli"', '')
[IO.File]::WriteAllText($config, ($configLines -join [Environment]::NewLine), $utf8)
python -B -m mah --config $config bundle create --repo $source --file $name --task-file $instructions --output-dir $bundle

$task = Join-Path $bundle 'task.md'
python -B -m mah --config $config bundle check --repo $source --task-file $task
$card = Join-Path $root 'card.json'
$cardBody = @{
  card_id = $runId
  repo_root = $source
  task_key = $runId
  trigger_ids = @('T5')
  prepare_mode = 'explicit'
  allowed_templates = @('review')
  allowed_targets = @(@{adapter='agy-print'; model='gemini-3.8-flash-low'; effort_max='low'})
  max_dispatches = 1
  ttl_minutes = 15
  timeout_seconds = 60
  workspace_path = $worker
  task_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $task).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText($card, ($cardBody | ConvertTo-Json -Depth 4), $utf8)
python -B -m mah --config $config card create --file $card
```

### 真人核准、唯一一次派工與驗收

1. 使用者核對卡片、合成資料、工作目錄身分與任務雜湊，取得卡片輸出的 `$cardSha`。
   由使用者在互動終端完成確認文字，並將回傳的 `grant_id` 存為 `$grantId`。
2. Controller 記錄來源 HEAD、Git 狀態、驗證文件與 Bundle SHA-256 以及工作目錄為空。
   啟動前再執行 `bundle check`。任何來源變動都須重新封裝與核准。
3. 核准成功後，只有下列一筆 `dispatch`。不重試、不改旗標、不切換模型。
   結束碼 30 可能是 `COLLECTING`，21 是送出狀態不明。先查 `status` 及原始收據。

```powershell
python -B -m mah --config $config grant approve $runId --sha $cardSha --via manual_cli
python -B -m mah --config $config dispatch --bundle-smoke --grant $grantId --run-id $runId --template review --adapter agy-print --model gemini-3.8-flash-low --effort low --timeout 60 --task-file $task --workspace $worker
```

4. Controller 核對執行程序、原始輸出、收據雜湊、單次 Grant 核銷、模型回覆的
   `隔離測試｜<驗證字串>`、空 findings、來源和工作目錄的前後狀態。有降級、額外
   核准提示或意外副作用時停止驗收。工具事件缺乏獨立收據時記為不可觀察。
5. 以實際 task、result、manifest 的雜湊製作獨立證據，再執行
   `mah verify $runId --evidence-file $controllerEvidence`。完成驗收後採納仍為
   `PENDING`。記錄 Token 與可觀察的訂閱／Credits 前後差異，百分比未變時記為扣量未確認。

## 歷史操作單：Disposable Worktree（仍阻擋）

首筆真實測試確認 `agy` 1.5.5 在停用 slash／skill 展開時會使 `--mode plan` 無效。
Harness 保留原安全旗標，現在會在預留授權前與啟動前拒絕已知衝突，也會拒絕缺少明確
`--mode plan` 或 `--sandbox` 的唯讀呼叫。以下命令保留供後續操作核對，目前不可重新派工。

恢復前須在隔離環境證明相容的唯讀策略。須核對 CLI 實際採用的模式、slash／skill 展開限制，
以及 Windows 上禁止工作樹寫入與越界存取的效果。只觀察旗標存在、沙箱啟用日誌或工作樹最後
仍乾淨，均不足以確認前述限制生效。

本機 `agy` 1.5.5 的 `--version`、`--help` 均以 0 結束。已確認旗標包含
`-p`、`--model`、`--effort`、`--mode`、`--sandbox`、`--settings`、`--output-format`、
`--json-schema`、`--print-timeout`、`--disable-slash-commands` 及 `--log-file`。
輸入採 Phase 0B-2 已使用的單一 `-p` 參數。中文往返及實際 schema 約束待實測。

## 核准範圍

| 項目 | 內容 |
|---|---|
| 用途 | T5，使用者明確要求的合成唯讀 `review` |
| Adapter、模型、思考程度 | `agy-print`、`gemini-3.8-flash-low`、`low` |
| 工作區 | 獨立合成儲存庫的乾淨 detached worktree，只有一份中文檔名的文件 |
| 權限 | `--mode plan --sandbox`；會話設定 `allowNonWorkspaceAccess=false` |
| 用量 | `manual_cli`，核准後 15 分鐘內最多送出一筆，逾時或狀態不明時不重送 |
| 費用 | 可能消耗 Google AI Pro 訂閱額度。先由使用者核對帳號、超額設定 Never、AI Credits Off 及未啟用 API／Cloud 計費。會話設定 `useG1Credits=false` |
| 本機測試資料 | 請求核准在新建合成儲存庫建立種子 commit，讓含隨機字串的文件能置於乾淨工作樹中。正式專案與遠端不涉及此 commit |

## 隔離資料與派工卡

若已有此輪保留的合成儲存庫及 `PROPOSED` 卡片，沿用其 `baseline.json`、
`harness.local.toml`、`task.md`、`card.bound.json` 及 detached worktree。
先核對 HEAD、Git 狀態與檔案雜湊，再用 `mah card show` 核對派工範圍。已有卡片時可直接
使用下方核准與派工指令，準備腳本用於建立全新測試環境。

以下準備指令供核准後在 Harness 專案根目錄執行。種子 commit 及執行資料只位於新建的
本機測試目錄。先將 `$AgyExe` 指向已核對的官方執行檔。使用 PowerShell 7，Git 出錯時停止。

```powershell
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$AgyExe = Join-Path $env:LOCALAPPDATA 'agy/bin/agy.exe'
$runId = 'smoke-' + [guid]::NewGuid().ToString('N')
$root = Join-Path $env:LOCALAPPDATA "MultiAgentHarness/$runId"
$seed = Join-Path $root 'seed'
$worktreeRoot = Join-Path $root 'worktrees'
$tree = Join-Path $worktreeRoot $runId
$utf8 = [System.Text.UTF8Encoding]::new($false)
$nl = [Environment]::NewLine
New-Item -ItemType Directory -Path $root, $seed, $worktreeRoot | Out-Null
$token = [guid]::NewGuid().ToString('N')
$name = '驗證文件.txt'
[IO.File]::WriteAllText((Join-Path $seed $name), (@('標題=隔離測試', "驗證字串=$token", '') -join $nl), $utf8)
git -C $seed init --quiet
git -C $seed add -- $name
git -C $seed -c user.name='Harness Smoke' -c user.email='harness-smoke@example.invalid' commit --quiet -m 'Add synthetic review fixture'
git -C $seed worktree add --quiet --detach $tree HEAD
$beforeHead = git -C $tree rev-parse HEAD
$beforeStatus = @(git -C $tree status --porcelain=v1 --untracked-files=all --ignored=matching)
if ($beforeStatus.Count -ne 0) { throw '測試工作樹不乾淨' }
$beforeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $tree $name)).Hash

$config = Join-Path $root 'harness.local.toml'
$exe = $AgyExe.Replace([char]92, [char]47)
$configLines = @('[paths]', 'state_dir = "state"', 'worktree_root = "worktrees"', '',
    '[adapters]', 'enabled = ["agy-print"]', ('agy_command = "' + $exe + '"'), '',
    '[approval]', 'mode = "manual_cli"', '')
[IO.File]::WriteAllText($config, ($configLines -join $nl), $utf8)

$task = Join-Path $root 'task.md'
$taskLines = @(
    '唯讀檢查目前工作區的「驗證文件.txt」，其他檔案不在任務範圍。'
    '請勿修改檔案或執行網路操作。根據文件內容，以「<標題>｜<驗證字串>」作為 summary。'
    'findings 可為空陣列。無法讀取時回報 blocked 並說明原因，勿推測內容。'
)
[IO.File]::WriteAllText($task, ($taskLines -join $nl), $utf8)

$card = Join-Path $root 'card.json'
$cardBody = @{
  card_id = $runId
  repo_root = $seed
  task_key = $runId
  trigger_ids = @('T5')
  prepare_mode = 'explicit'
  allowed_templates = @('review')
  allowed_targets = @(@{adapter='agy-print'; model='gemini-3.8-flash-low'; effort_max='low'})
  max_dispatches = 1
  ttl_minutes = 15
  timeout_seconds = 60
  workspace_path = $tree
  task_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $task).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText($card, ($cardBody | ConvertTo-Json -Depth 4), $utf8)
python -B -m mah --config $config card create --file $card
```

由使用者在自己的終端執行下列命令，核對卡片和 SHA-256 後親自輸入當次確認文字。
`$cardSha` 使用建立派工卡的輸出。核准後將輸出中的 `grant_id` 設為 `$grantId`。
Desktop `codex_prompt` 仍待 V-A8。卡片顯示的逾時須為 60 秒，工作樹與任務 SHA-256
須與基線相符。修改任何一項都需要新卡片與新 Grant。

```powershell
python -B -m mah --config $config grant approve $runId --sha $cardSha --via manual_cli
```

## 唯一一筆待核准的模型指令

```powershell
python -B -m mah --config $config dispatch --grant $grantId --run-id $runId --template review --adapter agy-print --model gemini-3.8-flash-low --effort low --timeout 60 --task-file $task --workspace $tree
```

Adapter 以陣列執行 `agy.exe`，CLI 時限和父程序等待時間均為 60 秒。若 PowerShell 因非零碼中止，
先讀取該次 JSON 與 Run 狀態。結束碼 30 可表示仍在 `COLLECTING` 等待獨立驗收。
結束碼 21 表示狀態不明，只可查詢 `status` 或用 `collect`／`resolve` 讀取既有證據。

## 驗收

1. 核對 `invocation.json`、`agy.process.json`、stdout／stderr 雜湊、conversation ID、用量及時間。
   經使用者同意後比對測試前後 `/usage` 與 Credits；讀數不足時記錄扣量未確認。
2. Controller 直接讀取工作樹中原始 UTF-8 文件，逐字比對 `result.json` 的 `summary` 與
   `"隔離測試｜$token"`。比較 HEAD、含未追蹤及忽略檔案的 `git status`、文件 SHA-256，要求與派工前一致。
3. 使用現有 Python `Verifier` 傳入與 `task.md`／`result.json` 雜湊綁定的
   `VerificationEvidence`。Controller 核對後才填 `confirmed=True`、
   `workspace_changed=False` 及實際查核發現數。驗收成功後採納仍為 `PENDING`。
4. 分開記錄「內容核對成功」與「工具操作有獨立收據」。若 `json` 格式沒有工具事件，
   後者標記不可觀察。檔案內容吻合只能驗證回覆與文件一致，V-B7 仍須實測。
5. 對 `--json-schema` 的結果優先驗證 CLI 頂層的 `structured_output`。收集須通過共通與
   模板 schema、來源一致性和既有 Verifier。保留 `response` 及原始收據。結果無法驗收時
   保留現有狀態及證據，停止本次派工。

## Windows 唯讀權限驗證草案（另待核准）

現有派工攔截仍生效。下列方案用於後續驗證安全邊界，尚無可執行的正式模型派工命令。
本輪只確認 Windows 原生 AppContainer 的合成讀寫結果。操作不使用原本的 run 或 Grant。
[官方 Permissions](https://antigravity.google/docs/permissions) ·
[官方終端沙箱](https://antigravity.google/docs/sandbox)

### 方案與暫存設定

先評估 A 的純讀檔配置。明確允許指定合成檔案，拒絕所有寫檔、終端命令、MCP 與瀏覽工具。
終端禁令涵蓋沙箱內外。這個測試可驗證命令的政策拒絕，終端 AppContainer 的檔案權限須
另有 OS 證據。需要保留終端操作，或無法證明所有工具路徑均受規則管控時，評估 B，
以獨立受限身分及檔案系統權限保護工作樹與外部對照目錄，並分離必要的可寫暫存區。
證據不足時維持 C，禁止正式唯讀派工。

供下一輪核對的 `--settings` JSON 草案如下。將路徑替換成該次合成檔案的正規化絕對路徑。
官方規則在 Windows 會去磁碟機代號並轉成斜線。測試使用同一磁碟區的實體路徑。
`Deny` 優先於 `Allow`，全域拒絕 `read_file(*)` 會連指定的讀取一併拒絕。
[CLI 設定 Schema](https://antigravity.google/schemas/v1/settings.json)

```json
{
  "toolPermission": "request-review",
  "enableTerminalSandbox": true,
  "disableSlashCommands": true,
  "allowNonWorkspaceAccess": false,
  "useG1Credits": false,
  "permissions": {
    "allow": ["read_file(/實際合成工作樹/驗證文件.txt)"],
    "ask": [],
    "deny": ["write_file(*)", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"]
  }
}
```

此草案保留停用 slash／skill 展開。以可查核的工具規則及外層隔離替代對 plan 模式的依賴，
須先驗證後才能成為新啟動策略。暫存設定如何合併既有規則，以及 v1.5.5 是否自動載入
Skills、外掛或其他可寫入工具，均須查核。測試須確認有效規則與工具清單。若同一身分會
自動載入個人 Skills 或外掛，先證明可關閉載入，或改用獨立低權限身分，由使用者操作登入。
設定無法確認、沙箱不可用、執行轉為 unsandboxed 或模式降級時，終止測試。
現有 JSON Adapter 尚無逐工具收據驗收入口。若採官方 `stream-json` 的 `init` 與
`step_update`，須先核對 v1.5.5 格式及離線解析，仍由 Ledger 控制同一筆派工。
[官方 Headless 模式](https://antigravity.google/docs/cli/headless) ·
[官方 Skills](https://antigravity.google/docs/skills)

### 單筆實機驗收條件

1. 取得一次性核准後準備獨立合成儲存庫及乾淨 detached worktree。中文 `驗證文件.txt`
   內含一次性的隨機字串，只留在文件中。另準備工作樹內 `內部禁止寫入.txt` 和工作樹外
   同一隔離根目錄的 `外部禁止寫入.txt`。外部對照檔排除系統 temp、build cache、
   正式專案及任何憑證位置。記錄三個檔案的 SHA-256、HEAD、Git 狀態及外部目錄清單。
2. 經有效一次性 Grant 送出一筆。要求內建檔案工具讀取中文檔及回報隨機字串，再用編輯
   工具對兩個對照檔各嘗試一次無害追加。另以終端工具對兩個對照檔各提出一次同樣操作。
   所有目標限定於列出的合成檔案。拒絕後保留收據，不換工具重試。
3. Controller 獨立比對隨機字串與可信的讀檔成功事件。四個寫入嘗試各須有能綁定目標
   與工具的拒絕事件。A 的終端預期為 `command(*)` 政策拒絕。若評估 B，須有獨立受限
   程序／OS 權限證據。未嘗試、模型文字、結束碼 0 或沙箱啟用日誌均不足以驗收。
4. 重算三個 SHA-256，核對 HEAD、含未追蹤及忽略項目的 Git 狀態和外部目錄清單。
   檔案變動、工具繞道寫入、意外權限提示或有效權限不可觀察時，判未通過。
