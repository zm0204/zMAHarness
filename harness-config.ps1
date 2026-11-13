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

function Get-HarnessPathPython {
    $command = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command -or $command.Source -like '*\Microsoft\WindowsApps\*') {
        throw '請將可直接執行的 Python 3.11 以上版本加入 PATH，並重新開啟終端。'
    }
    return $command.Source
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
