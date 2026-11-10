[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'harness-config.ps1')
$script:Failed = $false

function Report-Check {
    param([string]$Status, [string]$Name, [string]$Detail)
    if ($Status -eq '失敗') { $script:Failed = $true }
    Write-Output "[$Status] ${Name}：$Detail"
}

function Test-Python {
    param([string]$Path, [switch]$Launcher)

    if ($Path -like '*\Microsoft\WindowsApps\*') { return $null }

    # 固定、隔離的 Python 探針。使用記憶體資料庫，不讀任務或執行專案程式。
    $probe = 'import sys, sqlite3; db = sqlite3.connect('':memory:''); db.execute(''SELECT 1'').fetchone(); print(''Python '' + sys.version.split()[0] + '' / SQLite '' + sqlite3.sqlite_version); sys.exit(0 if sys.version_info >= (3, 11) else 2)'
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Path
    $start.Arguments = '-I -B -S -c "' + $probe + '"'
    if ($Launcher) { $start.Arguments = '-3 ' + $start.Arguments }
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        $null = $process.Start()
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(5000)) {
            $process.Kill()
            return $null
        }
        if ($process.ExitCode -eq 0) {
            return $output.GetAwaiter().GetResult().Trim()
        }
        return $null
    }
    catch { return $null }
    finally { $process.Dispose() }
}

function Find-App {
    param([string]$Name, [string]$ConfiguredPath, [string[]]$KnownPaths, [object[]]$StartApps)

    if ($ConfiguredPath) {
        try {
            $candidate = Resolve-HarnessPath $PSScriptRoot $ConfiguredPath
            if ([IO.Path]::GetExtension($candidate) -ine '.exe' -or -not [IO.File]::Exists($candidate)) {
                throw "指定的 .exe 不存在：$candidate"
            }
            Report-Check '通過' "$Name 安裝線索" "已找到指定檔案：$candidate"
        }
        catch { Report-Check '失敗' "$Name 安裝線索" $_.Exception.Message }
        return
    }

    $command = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) {
        Report-Check '通過' "$Name 安裝線索" "PATH 中有執行檔：$($command.Source)"
        return
    }
    foreach ($candidate in $KnownPaths) {
        if ($candidate -and [IO.File]::Exists($candidate)) {
            Report-Check '通過' "$Name 安裝線索" "已找到常見安裝位置：$candidate"
            return
        }
    }
    $entry = $StartApps | Where-Object { $_.Name -in @($Name, "OpenAI $Name", "Google $Name") } | Select-Object -First 1
    if ($entry) { Report-Check '通過' "$Name 安裝線索" "開始功能表有「$($entry.Name)」項目。" }
    else { Report-Check '需人工驗證' "$Name 安裝線索" '未偵測到安裝。請檢查桌面應用程式，或在本機設定填入執行檔路徑。' }
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
    Report-Check '通過' '共用設定' '已讀取版本 1，交接與額度規則符合範本。GUI 自動化維持停用。'
}
catch { Report-Check '失敗' '共用設定' $_.Exception.Message }

$local = $null
try {
    $local = Read-HarnessLocalConfig (Join-Path $PSScriptRoot '.local/settings.json')
    Report-Check '通過' '本機設定' '已讀取 .local/settings.json。'
}
catch { Report-Check '失敗' '本機設定' "請執行 setup.ps1 並檢查本機設定。$($_.Exception.Message)" }

$python = $null
$pythonCommand = $null
if ($local.pythonExecutable) {
    try {
        $candidate = Resolve-HarnessPath $PSScriptRoot $local.pythonExecutable
        if ([IO.Path]::GetExtension($candidate) -ieq '.exe' -and [IO.File]::Exists($candidate)) {
            $python = Test-Python $candidate
            $pythonCommand = $candidate
        }
    }
    catch { Report-Check '失敗' 'Python 路徑' $_.Exception.Message }
}
else {
    $candidates = @(Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue)
    foreach ($command in $candidates) {
        $python = Test-Python $command.Source
        if ($python) {
            $pythonCommand = $command.Source
            break
        }
    }
    if (-not $python) {
        $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($launcher) {
            $python = Test-Python $launcher.Source -Launcher
            $pythonCommand = "$($launcher.Source) -3"
        }
    }
}
if ($python) { Report-Check '通過' 'Python 及 SQLite' "$python（$pythonCommand）" }
else { Report-Check '失敗' 'Python 及 SQLite' '未能執行 Python 3.11 以上及 SQLite 探針。請檢查 PATH 或 pythonExecutable。' }

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
        Report-Check '通過' '工作目錄讀寫' $workspace
    }
    catch { Report-Check '失敗' '工作目錄讀寫' $_.Exception.Message }
    finally {
        if ($created) {
            try { [IO.File]::Delete($probeFile) }
            catch { Report-Check '失敗' '工作目錄清理' "無法移除本次探針：$probeFile" }
        }
    }
}
else { Report-Check '需人工驗證' '工作目錄讀寫' '讀取本機設定後才可檢查工作目錄。' }

$startApps = @()
if (Get-Command Get-StartApps -ErrorAction SilentlyContinue) {
    try { $startApps = @(Get-StartApps) } catch { }
}
$antigravityPaths = @()
if ($env:LOCALAPPDATA) { $antigravityPaths += Join-Path $env:LOCALAPPDATA 'Programs/antigravity/Antigravity.exe' }
if ($env:ProgramFiles) { $antigravityPaths += Join-Path $env:ProgramFiles 'Antigravity/Antigravity.exe' }
Find-App 'Codex' $local.codexExecutable @() $startApps
Find-App 'Antigravity' $local.antigravityExecutable $antigravityPaths $startApps
Report-Check '需人工驗證' 'Codex 工作階段' '登入、Sol 與 Terra Low 的可用性，以及 Computer Use 的權限與操作能力。'
Report-Check '需人工驗證' 'Antigravity 工作階段' '登入、Gemini Flash 的實際選項、剩餘額度與送出回執。'
Report-Check '需人工驗證' 'GUI 與平台規範' '確認允許的操作方式，並人工監看權限提示、逾時中止及檔案交接。'

if ($script:Failed) { exit 1 }
Write-Output '已完成可自動檢查的項目。標為「需人工驗證」的項目仍待確認。'
exit 0
