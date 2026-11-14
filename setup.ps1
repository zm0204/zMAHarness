[CmdletBinding()]
param([ValidateNotNullOrEmpty()][string]$WorkspacePath)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'harness-config.ps1')

function Install-ZMAHarness {
    param([string]$PythonPath, [object]$Paths)

    if (-not $env:LOCALAPPDATA -or -not $env:USERPROFILE) {
        throw '需要 Windows 使用者的 LOCALAPPDATA 與 USERPROFILE。'
    }
    $skillSource = Join-Path $PSScriptRoot '.agents/skills/zmaharness'
    $skillFiles = @(Get-HarnessSkillFiles $skillSource)
    $configSource = Join-Path $PSScriptRoot 'harness.local.example.toml'
    foreach ($source in @($configSource, (Join-Path $PSScriptRoot 'mah/__main__.py'))) {
        if (-not [IO.File]::Exists($source)) { throw "安裝來源缺少檔案：$source" }
    }

    # 用 ASCII 路徑字面值相容 Python 3.11/3.12 的系統區域編碼與新版 UTF-8。
    $register = @'
import os
from pathlib import Path
import site
import sys
import tempfile

if sys.version_info < (3, 11) or site.ENABLE_USER_SITE is not True:
    sys.exit('zMAHarness needs Python 3.11+ with its user site enabled.')
root = Path(sys.argv[1]).resolve(strict=True)
site_dir = Path(site.getusersitepackages())
payload = f'import sys; p = {ascii(str(root))}; sys.path.append(p) if p not in sys.path else None\n'.encode('ascii')
site_dir.mkdir(parents=True, exist_ok=True)
target = site_dir / 'zmaharness.pth'
if target.is_file() and target.read_bytes() == payload:
    sys.exit(0)
with tempfile.NamedTemporaryFile(dir=site_dir, delete=False) as out:
    temp_path = Path(out.name)
try:
    temp_path.write_bytes(payload)
    os.replace(temp_path, target)
finally:
    temp_path.unlink(missing_ok=True)
'@
    & $PythonPath -B -c $register $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw '無法註冊 Python 使用者的 zmaharness.pth。' }

    $v2Root = $Paths.home
    $null = [IO.Directory]::CreateDirectory($v2Root)
    foreach ($name in @('state', 'worktrees', 'gui')) {
        $null = [IO.Directory]::CreateDirectory((Join-Path $v2Root $name))
    }
    $v2Config = Join-Path $v2Root 'harness.local.toml'
    if (-not [IO.File]::Exists($v2Config)) {
        $temporary = Join-Path $v2Root ("harness.{0}.tmp" -f [Guid]::NewGuid().ToString('N'))
        try {
            [IO.File]::Copy($configSource, $temporary)
            try { [IO.File]::Move($temporary, $v2Config) }
            catch [IO.IOException] { if (-not [IO.File]::Exists($v2Config)) { throw } }
        }
        finally { [IO.File]::Delete($temporary) }
    }
    if ($Paths.legacy_config -and [IO.File]::Exists($Paths.legacy_config)) {
        Write-Output '[提示] 舊設定及資料保留。預設使用新設定，既有 Run 可用 --config 指向舊設定繼續處理。'
    }

    foreach ($relativeRoot in @('.agents/skills/zmaharness', '.claude/skills/zmaharness')) {
        $skillRoot = Join-Path $env:USERPROFILE $relativeRoot
        foreach ($file in $skillFiles) {
            $destination = Join-Path $skillRoot $file
            $null = [IO.Directory]::CreateDirectory((Split-Path -Parent $destination))
            [IO.File]::Copy((Join-Path $skillSource $file), $destination, $true)
        }
        Write-Output "[通過] 已安裝使用者技能：$skillRoot"
    }
    # 新技能安裝完成後才遷移舊版 harness 技能；非本專案的同名技能一律保留。
    foreach ($relativeRoot in @('.agents/skills/harness', '.claude/skills/harness')) {
        $legacyRoot = Join-Path $env:USERPROFILE $relativeRoot
        switch (Get-HarnessLegacySkillState $legacyRoot) {
            'Project' {
                Remove-Item -LiteralPath $legacyRoot -Recurse -Force
                Write-Output "[通過] 已移除本專案的舊版技能：$legacyRoot"
            }
            'Foreign' { Write-Output "[警告] 保留非本專案的 harness 技能，未做任何變更：$legacyRoot" }
        }
    }
    Write-Output "[通過] zMAHarness 本機設定：$v2Config"
    Write-Output '[通過] 已註冊 Python 使用者模組路徑（zmaharness.pth）。'
}

try {
    # 任何本機設定、工作目錄或使用者套件寫入前，先驗證真正可用的 Python。
    $pythonPath = Get-HarnessPathPython
    # 路徑同樣在寫入前由 Python 解析；ZMAHARNESS_HOME 無效時在此停止。
    $paths = Get-HarnessPaths $pythonPath $PSScriptRoot
    $localRoot = Join-Path $PSScriptRoot '.local'
    $localFile = Join-Path $localRoot 'settings.json'
    $template = Join-Path $PSScriptRoot 'config/local.example.json'
    $override = $PSBoundParameters.ContainsKey('WorkspacePath')
    $created = $false

    if (-not (Test-Path -LiteralPath $localFile)) {
        $config = Read-HarnessLocalConfig $template
        if ($override) {
            $config.workspace = $WorkspacePath
        }
        $null = Resolve-HarnessPath $PSScriptRoot $config.workspace
        $null = [IO.Directory]::CreateDirectory($localRoot)
        $temporary = Join-Path $localRoot ("settings.{0}.tmp" -f [Guid]::NewGuid().ToString('N'))
        try {
            [IO.File]::WriteAllText($temporary, (($config | ConvertTo-Json -Depth 4) + [Environment]::NewLine))
            try {
                # 同目錄移動，目的檔已存在時失敗，保留先寫入的本機設定。
                [IO.File]::Move($temporary, $localFile)
                $created = $true
            }
            catch [IO.IOException] {
                if (-not [IO.File]::Exists($localFile)) { throw }
            }
        }
        finally {
            [IO.File]::Delete($temporary)
        }
    }

    $config = Read-HarnessLocalConfig $localFile
    $workspace = Resolve-HarnessPath $PSScriptRoot $config.workspace
    if ($override -and (Resolve-HarnessPath $PSScriptRoot $WorkspacePath) -ne $workspace) {
        throw '現有本機設定已指定工作目錄。請直接編輯 .local/settings.json，或省略 -WorkspacePath。'
    }
    $null = [IO.Directory]::CreateDirectory($workspace)

    if ($created) { Write-Output "[通過] 已建立本機設定：$localFile" }
    else { Write-Output "[通過] 已保留本機設定：$localFile" }
    Write-Output "[通過] 工作目錄：$workspace"
    Install-ZMAHarness -PythonPath $pythonPath -Paths $paths
    Write-Output '下一步：執行 .\doctor.ps1。'
    exit 0
}
catch {
    Write-Output "[失敗] 初始化：$($_.Exception.Message)"
    exit 1
}
