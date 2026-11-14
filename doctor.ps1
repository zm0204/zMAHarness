[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'harness-config.ps1')
$script:Failed = $false
$script:LegacyLines = New-Object System.Collections.Generic.List[string]

function Report-Check {
    param([string]$Status, [string]$Name, [string]$Detail)
    if ($Status -eq '失敗') { $script:Failed = $true }
    Write-Output "[$Status] ${Name}：$Detail"
}

function Report-Legacy {
    # V1 檔案交接的項目：結束碼規則相同，訊息留到最後的「舊版相容」段落顯示。
    param([string]$Status, [string]$Name, [string]$Detail)
    if ($Status -eq '失敗') { $script:Failed = $true }
    $script:LegacyLines.Add("[$Status] ${Name}：$Detail")
}

function Find-App {
    param([string]$Name, [string]$ConfiguredPath, [string[]]$KnownPaths, [object[]]$StartApps)

    if ($ConfiguredPath) {
        try {
            $candidate = Resolve-HarnessPath $PSScriptRoot $ConfiguredPath
            if ([IO.Path]::GetExtension($candidate) -ine '.exe' -or -not [IO.File]::Exists($candidate)) {
                throw "指定的 .exe 不存在：$candidate"
            }
            Report-Legacy '通過' "$Name 安裝線索" "已找到指定檔案：$candidate"
        }
        catch { Report-Legacy '失敗' "$Name 安裝線索" $_.Exception.Message }
        return
    }

    $command = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) {
        Report-Legacy '通過' "$Name 安裝線索" "PATH 中有執行檔：$($command.Source)"
        return
    }
    foreach ($candidate in $KnownPaths) {
        if ($candidate -and [IO.File]::Exists($candidate)) {
            Report-Legacy '通過' "$Name 安裝線索" "已找到常見安裝位置：$candidate"
            return
        }
    }
    $entry = $StartApps | Where-Object { $_.Name -in @($Name, "OpenAI $Name", "Google $Name") } | Select-Object -First 1
    if ($entry) { Report-Legacy '通過' "$Name 安裝線索" "開始功能表有「$($entry.Name)」項目。" }
    else { Report-Legacy '需人工驗證' "$Name 安裝線索" '未偵測到安裝。請檢查桌面應用程式，或在本機設定填入執行檔路徑。' }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    Report-Check '失敗' 'Windows 11' '目前不是 Windows。'
}
else {
    try {
        $windows = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
        $build = [int]$windows.CurrentBuildNumber
        if ($build -ge 22000 -and $windows.InstallationType -eq 'Client') {
            Report-Check '通過' 'Windows 11' "組建 $build"
        }
        else { Report-Check '失敗' 'Windows 11' "組建 $build，安裝類型 $($windows.InstallationType)" }
    }
    catch { Report-Check '需人工驗證' 'Windows 11' '無法讀取 Windows 組建。' }
}

if ($PSVersionTable.PSVersion -ge [version]'5.1') {
    Report-Check '通過' 'PowerShell' $PSVersionTable.PSVersion.ToString()
}
else { Report-Check '失敗' 'PowerShell' '需要 PowerShell 5.1 以上。' }

try {
    $shared = Read-HarnessJson (Join-Path $PSScriptRoot 'config/defaults.json')
    if ($shared.routing.guiAutomationEnabled -isnot [bool] -or $shared.routing.guiAutomationEnabled -or
        $shared.routing.platformReviewRequired -ne $false -or $shared.routing.humanSupervisionRequired -ne $true) {
        throw '共用設定須維持 GUI 預設停用、平台風險告知與日常派工規則分離，以及人工監看。'
    }
    if ($shared.handoff.protocolVersion -ne 1 -or $shared.handoff.publishDoneLast -ne $true -or
        $shared.handoff.autoResendUnknown -ne $false -or $shared.quota.maxConcurrentTasks -ne 1 -or
        $shared.quota.maxSubmissionsPerTaskId -ne 1 -or $shared.quota.maxExternalSubmissionsPerPilot -ne 1 -or
        $shared.quota.stopOnPermissionPrompt -ne $true -or $shared.quota.stopOnPaymentPrompt -ne $true -or
        $shared.quota.stopOnUnknownModelOrQuota -ne $true) {
        throw '交接或額度規則與本階段的保護條件不符。'
    }
    Report-Legacy '通過' '共用設定' '已讀取版本 1，交接與額度規則符合範本。GUI 自動化維持停用。'
}
catch { Report-Legacy '失敗' '共用設定' $_.Exception.Message }

$local = $null
try {
    $local = Read-HarnessLocalConfig (Join-Path $PSScriptRoot '.local/settings.json')
    Report-Legacy '通過' '本機設定' '已讀取 .local/settings.json。'
}
catch { Report-Legacy '失敗' '本機設定' "請執行 setup.ps1 並檢查本機設定。$($_.Exception.Message)" }

$python = $null
$pythonCommand = $null
$storeAlias = $false
if ($local.pythonExecutable) {
    try {
        $candidate = Resolve-HarnessPath $PSScriptRoot $local.pythonExecutable
        if ([IO.Path]::GetExtension($candidate) -ieq '.exe' -and [IO.File]::Exists($candidate)) {
            $python = Get-HarnessPythonProbe $candidate
            $pythonCommand = $candidate
            if (-not $python -and $candidate -like '*\Microsoft\WindowsApps\*') { $storeAlias = $true }
        }
    }
    catch { Report-Check '失敗' 'Python 路徑' $_.Exception.Message }
}
else {
    $candidates = @(Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue)
    foreach ($command in $candidates) {
        $python = Get-HarnessPythonProbe $command.Source
        if ($python) {
            $pythonCommand = $command.Source
            break
        }
        if ($command.Source -like '*\Microsoft\WindowsApps\*') { $storeAlias = $true }
    }
    if (-not $python) {
        $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($launcher) {
            $python = Get-HarnessPythonProbe $launcher.Source -Launcher
            $pythonCommand = "$($launcher.Source) -3"
        }
    }
}
if ($python) { Report-Check '通過' 'Python 及 SQLite' "$python（$pythonCommand）" }
else { Report-Check '失敗' 'Python 及 SQLite' (Get-HarnessPythonFailureMessage -StoreAlias:$storeAlias) }

# 路徑只由 mah/config.py 解析；沒有可用的 Python 時沿用原本的 LOCALAPPDATA。
$paths = $null
$pathsFailed = $false
$localAppData = $env:LOCALAPPDATA
if ($python) {
    try {
        $paths = Get-HarnessPaths (Get-HarnessPathPython) $PSScriptRoot
        $localAppData = $paths.local_appdata
        $source = switch ($paths.source) {
            'ZMAHARNESS_HOME' { '由 ZMAHARNESS_HOME 指定' }
            'sandbox_redirect' { '偵測到沙盒重新導向，改用 USERPROFILE\AppData\Local' }
            default { '預設位置' }
        }
        Report-Check '通過' 'zMAHarness 路徑' "${source}：$($paths.home)"
    }
    catch {
        $pathsFailed = $true
        Report-Check '失敗' 'zMAHarness 路徑' $_.Exception.Message
    }
}

if ($local) {
    $probeFile = $null
    $created = $false
    try {
        $workspace = Resolve-HarnessPath $PSScriptRoot $local.workspace
        if (-not [IO.Directory]::Exists($workspace)) { throw "找不到工作目錄：$workspace。請先執行 setup.ps1。" }
        $probeValue = [Guid]::NewGuid().ToString('N')
        $probeFile = Join-Path $workspace ".doctor-$probeValue.tmp"
        $stream = [IO.File]::Open($probeFile, [IO.FileMode]::CreateNew)
        $created = $true
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes($probeValue)
            $stream.Write($bytes, 0, $bytes.Length)
        }
        finally { $stream.Dispose() }
        if ([IO.File]::ReadAllText($probeFile) -cne $probeValue) { throw '工作目錄回讀內容不符。' }
        [IO.File]::Delete($probeFile)
        $created = $false
        Report-Legacy '通過' '工作目錄讀寫' $workspace
    }
    catch { Report-Legacy '失敗' '工作目錄讀寫' $_.Exception.Message }
    finally {
        if ($created) {
            try { [IO.File]::Delete($probeFile) }
            catch { Report-Legacy '失敗' '工作目錄清理' "無法移除本次探針：$probeFile" }
        }
    }
}
else { Report-Legacy '需人工驗證' '工作目錄讀寫' '讀取本機設定後才可檢查工作目錄。' }

$startApps = @()
if (Get-Command Get-StartApps -ErrorAction SilentlyContinue) {
    try { $startApps = @(Get-StartApps) } catch { }
}
$antigravityPaths = @()
if ($localAppData) { $antigravityPaths += Join-Path $localAppData 'Programs/antigravity/Antigravity.exe' }
if ($env:ProgramFiles) { $antigravityPaths += Join-Path $env:ProgramFiles 'Antigravity/Antigravity.exe' }
Find-App 'Codex' $local.codexExecutable @() $startApps
Find-App 'Antigravity' $local.antigravityExecutable $antigravityPaths $startApps

$v2Settings = $null
$v2Config = $null
if ($paths) { $v2Config = $paths.config_path }
$hasV2Config = $v2Config -and [IO.File]::Exists($v2Config)
if (-not $hasV2Config) {
    $detail = if (-not $python) { '請先解決上方的 Python 問題。' }
              elseif ($pathsFailed) { '請先解決上方的 zMAHarness 路徑問題。' }
              else { "找不到 $v2Config。請執行 setup.ps1。" }
    Report-Check '失敗' 'zMAHarness TOML' $detail
}

$savedPythonPath = $env:PYTHONPATH
$locationPushed = $false
try {
    if (-not $python) { throw '請先解決上方的 Python 問題。' }
    $pathPython = Get-HarnessPathPython
    if (-not $env:LOCALAPPDATA) { throw '缺少 LOCALAPPDATA，無法從引擎以外的目錄測試。' }
    Push-Location -LiteralPath $env:LOCALAPPDATA
    $locationPushed = $true
    $env:PYTHONPATH = $null
    $help = & $pathPython -B -m mah --help 2>&1
    if ($LASTEXITCODE -ne 0 -or ($help -join "`n") -notmatch 'zMAHarness') {
        throw '無法從引擎以外的目錄執行 python -B -m mah --help。請檢查 Python 使用者安裝。'
    }
    $registration = 'import importlib.util, pathlib, site, sys; spec = importlib.util.find_spec(''mah.__main__''); pth = pathlib.Path(site.getusersitepackages()) / ''zmaharness.pth''; entry = pathlib.Path(sys.argv[1]).resolve() / ''mah'' / ''__main__.py''; sys.exit(0 if pth.is_file() and spec and pathlib.Path(spec.origin).resolve() == entry else 2)'
    & $pathPython -B -c $registration $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw '目前 python 找到其他副本，或 zmaharness.pth 尚未註冊。請重跑此副本的 setup.ps1。' }
    Report-Check '通過' 'zMAHarness 跨專案 CLI' '已從引擎以外的目錄執行 --help，並核對安裝來源。'
    if ($hasV2Config) {
        try {
            $readToml = 'import json, sys; from mah.config import load_config; print(json.dumps(load_config(sys.argv[1]).values[''adapters'']))'
            $configResult = & $pathPython -B -c $readToml $v2Config 2>&1
            if ($LASTEXITCODE -ne 0) { throw '無法載入本機 TOML。' }
            $v2Settings = ($configResult -join "`n") | ConvertFrom-Json
            Report-Check '通過' 'zMAHarness TOML' $v2Config
        }
        catch { Report-Check '失敗' 'zMAHarness TOML' $_.Exception.Message }
    }
}
catch {
    Report-Check '失敗' 'zMAHarness 跨專案 CLI' $_.Exception.Message
    if ($hasV2Config) {
        $detail = if ($python) { '設定檔存在，請檢查 Python 安裝後再檢查內容。' }
                  else { '請先解決上方的 Python 問題。' }
        Report-Check '需人工驗證' 'zMAHarness TOML' $detail
    }
}
finally {
    $env:PYTHONPATH = $savedPythonPath
    if ($locationPushed) { Pop-Location }
}

$agyName = if ($v2Settings.agy_command) { $v2Settings.agy_command } else { 'agy' }
$agyPath = $null
try {
    if ($agyName -match '[\\/]') {
        $candidate = Resolve-HarnessPath (Split-Path -Parent $v2Config) $agyName
        if ([IO.File]::Exists($candidate) -and [IO.Path]::GetExtension($candidate) -ieq '.exe') { $agyPath = $candidate }
    }
    else {
        $agyExe = if ($agyName.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) { $agyName } else { "$agyName.exe" }
        $agyPath = (Get-Command $agyExe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
        if (-not $agyPath -and $agyName -ceq 'agy' -and $localAppData) {
            $candidate = Join-Path $localAppData 'agy/bin/agy.exe'
            if ([IO.File]::Exists($candidate)) { $agyPath = $candidate }
        }
    }
    if ($agyPath) { Report-Check '通過' 'agy 執行檔' $agyPath }
    else { Report-Check '需人工驗證' 'agy 執行檔' '未找到 agy.exe。使用 Gemini Worker 前請安裝官方 Antigravity CLI，或檢查 TOML 的 agy_command。' }
}
catch { Report-Check '失敗' 'agy 執行檔' $_.Exception.Message }

# codex-exec 的殘餘風險：Codex 會把 CODEX_HOME 的全域指示注入 Worker，無法以設定關閉。只提示，不影響結束碼。
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } elseif ($env:USERPROFILE) { Join-Path $env:USERPROFILE '.codex' } else { $null }
if ($codexHome) {
    $globalInstructions = @('AGENTS.md', 'AGENTS.override.md') | ForEach-Object { Join-Path $codexHome $_ } |
        Where-Object { [IO.File]::Exists($_) -and (Get-Item -LiteralPath $_).Length -gt 0 }
    if ($globalInstructions) {
        Report-Check '需人工驗證' 'Codex 全域指示' ("codex-exec Worker 會注入這些檔案的內容：" + ($globalInstructions -join '、') + "。Context-Bundle 模式工具全關，影響僅限回答內容，輸出仍受 schema 與 Verifier 約束。")
    }
}

# claude 原生執行檔的解析只在 Python（mah preflight）；沒有可用的 Python 時略過。
if ($python -and -not $pathsFailed) {
    try {
        $claudeQuery = 'import json, sys; sys.path.insert(0, sys.argv[1]); from mah.preflight import preflight; r = preflight(sys.argv[1], ''claude-print''); print(json.dumps({''found'': r[''adapter_executable_found''], ''enabled'': r[''adapter_enabled'']}))'
        $claudeRaw = & (Get-HarnessPathPython) -I -B -c $claudeQuery $PSScriptRoot
        if ($LASTEXITCODE -ne 0) { throw '無法檢查 claude-print。' }
        $claude = ($claudeRaw -join "`n") | ConvertFrom-Json
        if (-not $claude.found) {
            Report-Check '需人工驗證' 'claude-print' '未找到 claude 原生執行檔（.exe）。使用 Claude Worker 前請安裝 Claude Code CLI，或在本機 TOML 把 claude_command 設成 claude.exe 的絕對路徑。'
        }
        elseif (-not $claude.enabled) {
            Report-Check '需人工驗證' 'claude-print' '已找到 claude 原生執行檔，但 claude-print 不在 adapters.enabled。要使用 Claude Worker，請在本機 TOML 改成 enabled = ["codex-exec", "gui-terra", "agy-print", "claude-print"]。'
        }
        else { Report-Check '通過' 'claude-print' '已啟用，並找到 claude 原生執行檔。' }
    }
    catch { Report-Check '需人工驗證' 'claude-print' $_.Exception.Message }

    # codex-exec 只查設定與執行檔位置，不執行 codex。
    try {
        $codexQuery = 'import json, sys; sys.path.insert(0, sys.argv[1]); from mah.config import adapter_enabled, load_config; from mah.adapters.registry import executable_lookup; c = load_config(None); print(json.dumps({''found'': executable_lookup(''codex-exec'')(c) is not None, ''enabled'': adapter_enabled(c, ''codex-exec'')}))'
        $codexRaw = & (Get-HarnessPathPython) -I -B -c $codexQuery $PSScriptRoot
        if ($LASTEXITCODE -ne 0) { throw '無法檢查 codex-exec。' }
        $codex = ($codexRaw -join "`n") | ConvertFrom-Json
        if (-not $codex.found) {
            Report-Check '需人工驗證' 'codex-exec' '未找到 codex 原生執行檔（.exe）。使用 GPT Worker 前請安裝 Codex CLI，或在本機 TOML 把 codex_command 設成 codex.exe 的絕對路徑。'
        }
        elseif (-not $codex.enabled) {
            Report-Check '需人工驗證' 'codex-exec' '已找到 codex 原生執行檔，但 codex-exec 不在 adapters.enabled。要使用 GPT Worker，請在本機 TOML 的 adapters.enabled 加入 "codex-exec"。'
        }
        else { Report-Check '通過' 'codex-exec' '已啟用，並找到 codex 原生執行檔。' }
    }
    catch { Report-Check '需人工驗證' 'codex-exec' $_.Exception.Message }
}

$skillSource = Join-Path $PSScriptRoot '.agents/skills/zmaharness'
$skillTargets = [ordered]@{
    'Codex 使用者技能' = '.agents/skills/zmaharness'
    'Claude Code 使用者技能' = '.claude/skills/zmaharness'
}
foreach ($name in $skillTargets.Keys) {
    try {
        if (-not $env:USERPROFILE) { throw '找不到 USERPROFILE。' }
        $skillRoot = Join-Path $env:USERPROFILE $skillTargets[$name]
        foreach ($file in @(Get-HarnessSkillFiles $skillSource)) {
            $installed = Join-Path $skillRoot $file
            if (-not [IO.File]::Exists($installed)) { throw "缺少 $file，請重新執行 setup.ps1。" }
            $actual = [Convert]::ToBase64String([IO.File]::ReadAllBytes($installed))
            $expected = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $skillSource $file)))
            if ($actual -cne $expected) { throw "$file 與此副本不同，請重新執行 setup.ps1。" }
        }
        Report-Check '通過' $name $skillRoot
    }
    catch {
        $detail = if ($python) { $_.Exception.Message } else { '請先解決上方的 Python 問題。' }
        Report-Check '失敗' $name $detail
    }
}

if ($env:USERPROFILE) {
    foreach ($relativeRoot in @('.agents/skills/harness', '.claude/skills/harness')) {
        $legacyRoot = Join-Path $env:USERPROFILE $relativeRoot
        try {
            if ((Get-HarnessLegacySkillState $legacyRoot) -eq 'Project') {
                Report-Check '警告' '舊版技能' "本專案的舊版 harness 技能仍在：$legacyRoot。重新執行 setup.ps1 可移除。"
            }
        }
        catch { Report-Check '警告' '舊版技能' "無法檢查 ${legacyRoot}：$($_.Exception.Message)" }
    }
}

Write-Output '--- V1 檔案交接（舊版相容） ---'
Write-Output '以下項目只用於 V1 的 GUI 檔案交接流程；只使用 CLI Worker 時，「需人工驗證」的項目可以忽略。'
foreach ($line in $script:LegacyLines) { Write-Output $line }

if ($script:Failed) { exit 1 }
Write-Output '已完成可自動檢查的項目。標為「需人工驗證」的項目仍待確認。'
exit 0
