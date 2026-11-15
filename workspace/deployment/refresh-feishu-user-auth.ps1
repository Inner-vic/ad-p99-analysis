param(
    [string]$ManifestPath = (Join-Path $PSScriptRoot 'cc-connect-lark-isolation.json'),
    [switch]$CheckOnly,
    [switch]$ForceVerify
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$python = [string]$manifest.python_executable
if (-not $python -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The pinned Python runtime is unavailable.'
}
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([System.IO.Path]::GetFullPath($manifest.workspace_root) -ne $workspace) {
    throw 'Manifest workspace does not match this deployment.'
}
$arguments = @(
    (Join-Path $PSScriptRoot 'feishu_auth_maintenance.py'),
    '--manifest', (Resolve-Path -LiteralPath $ManifestPath).Path
)
if ($CheckOnly) { $arguments += '--check-only' }
if ($ForceVerify) { $arguments += '--force-verify' }
Set-Location -LiteralPath $workspace
$env:PYTHONUTF8 = '1'
& $python @arguments
exit $LASTEXITCODE
