function Read-HarnessJson {
    param([string]$Path)

    try {
        $config = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "無法讀取 JSON 設定：$Path。$($_.Exception.Message)"
    }
    if ($config -isnot [pscustomobject] -or $config.schemaVersion -ne 1) {
        throw "設定格式或 schemaVersion 不符：$Path"
    }
    return $config
}

function Read-HarnessLocalConfig {
    param([string]$Path)

    $config = Read-HarnessJson $Path
    $allowed = @('schemaVersion', 'workspace', 'pythonExecutable', 'codexExecutable', 'antigravityExecutable')
    foreach ($property in $config.PSObject.Properties) {
        if ($property.Name -notin $allowed) {
            throw "本機設定含未定義欄位：$($property.Name)"
        }
        if ($property.Name -in $allowed[2..4] -and $null -ne $property.Value -and
            ($property.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($property.Value))) {
            throw "本機設定 $($property.Name) 須為路徑字串或 null。"
        }
    }
    if ($config.workspace -isnot [string] -or [string]::IsNullOrWhiteSpace($config.workspace)) {
        throw '本機設定 workspace 須為非空的路徑字串。'
    }
    return $config
}

function Resolve-HarnessPath {
    param([string]$Root, [string]$Path)

    if ($Path -match '^[A-Za-z]:[\\/]|^\\\\') {
        return [IO.Path]::GetFullPath($Path)
    }
    if ([IO.Path]::IsPathRooted($Path)) {
        throw "請使用完整磁碟路徑或相對於專案根目錄的路徑：$Path"
    }
    return [IO.Path]::GetFullPath((Join-Path $Root $Path))
}

function Get-HarnessPythonProbe {
    param([string]$Path, [switch]$Launcher)

    # 隔離探針只使用記憶體資料庫，不執行專案程式或寫入檔案。
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
        if ($process.ExitCode -eq 0) { return $output.GetAwaiter().GetResult().Trim() }
        return $null
    }
    catch { return $null }
    finally { $process.Dispose() }
}

function Get-HarnessPythonFailureMessage {
    param([switch]$StoreAlias)

    $reason = if ($StoreAlias) {
        '這是 Microsoft Store 的假捷徑，不是真的 Python。'
    }
    else {
        '找不到可執行、版本 3.11 以上且可匯入 sqlite3 的 Python。'
    }
    return @(
        $reason
        '執行 winget install -e --id Python.Python.3.13，或從 python.org 安裝並勾選 Add python.exe to PATH。'
        '到「設定 → 應用程式 → 進階應用程式設定 → 應用程式執行別名」關閉 python.exe 和 python3.exe。'
        '重開終端機後再執行 setup.ps1。'
    ) -join [Environment]::NewLine
}

function Get-HarnessPathPython {
    $storeAlias = $false
    foreach ($command in @(Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue)) {
        $result = Get-HarnessPythonProbe $command.Source
        if ($result) { return $command.Source }
        if ($command.Source -like '*\Microsoft\WindowsApps\*') { $storeAlias = $true }
    }
    throw (Get-HarnessPythonFailureMessage -StoreAlias:$storeAlias)
}

function Get-HarnessPaths {
    # 路徑解析只實作在 mah/config.py；此處只讀取其結果，不寫入檔案。
    param([string]$PythonPath, [string]$EngineRoot)

    # 不含雙引號，避免 Windows PowerShell 5.1 傳遞原生參數時破壞引號；輸出為 ASCII JSON。
    $query = @'
import json, sys
try:
    sys.path.insert(0, sys.argv[1])
    from mah.config import path_report
    report, code = path_report(), 0
except Exception as exc:
    report, code = {'error': str(exc)}, 2
print(json.dumps(report, ensure_ascii=True))
sys.exit(code)
'@
    $raw = & $PythonPath -I -B -c $query $EngineRoot
    $exitCode = $LASTEXITCODE
    try { $paths = ($raw -join "`n") | ConvertFrom-Json }
    catch { throw '無法從 Python 取得 zMAHarness 路徑。' }
    if ($exitCode -ne 0) { throw $paths.error }
    return $paths
}

function Get-HarnessSkillFiles {
    param([string]$SourceRoot)

    foreach ($required in @('SKILL.md', 'references/dispatch.md')) {
        if (-not [IO.File]::Exists((Join-Path $SourceRoot $required))) {
            throw "技能來源缺少檔案：$required"
        }
    }
    Get-ChildItem -LiteralPath $SourceRoot -Recurse -Force -File | ForEach-Object {
        $_.FullName.Substring($SourceRoot.Length + 1)
    }
}

function Get-HarnessLegacySkillState {
    # 舊版技能名稱為 harness。只有 frontmatter 為 name: harness 且內文含 zMAHarness 時才視為本專案安裝。
    param([string]$SkillRoot)

    if (-not [IO.Directory]::Exists($SkillRoot)) { return 'Absent' }
    $attributes = [IO.File]::GetAttributes($SkillRoot)
    if ($attributes -band [IO.FileAttributes]::ReparsePoint) { return 'Foreign' }
    $skillFile = Join-Path $SkillRoot 'SKILL.md'
    if (-not [IO.File]::Exists($skillFile)) { return 'Foreign' }
    try { $text = [IO.File]::ReadAllText($skillFile, [Text.Encoding]::UTF8) }
    catch { return 'Foreign' }
    $match = [regex]::Match($text, '\A﻿?---\r?\n(.*?)\r?\n---(\r?\n|\z)', 'Singleline')
    if (-not $match.Success) { return 'Foreign' }
    $names = @($match.Groups[1].Value -split '\r?\n' | Where-Object { $_ -cmatch '^name:' })
    if ($names.Count -ne 1 -or $names[0] -cnotmatch '^name:[ \t]*harness[ \t]*$') { return 'Foreign' }
    if (-not $text.Contains('zMAHarness')) { return 'Foreign' }
    return 'Project'
}
