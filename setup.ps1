[CmdletBinding()]
param([ValidateNotNullOrEmpty()][string]$WorkspacePath)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'harness-config.ps1')

try {
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
    Write-Output '下一步：執行 .\doctor.ps1。'
    exit 0
}
catch {
    Write-Output "[失敗] 初始化：$($_.Exception.Message)"
    exit 1
}
