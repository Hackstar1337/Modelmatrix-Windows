[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ModelMatrix'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'ModelMatrix'),
    [switch]$Purge,
    [switch]$KeepStartup
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'stop-windows.ps1') -DataRoot $DataRoot -Force
if (-not $KeepStartup) {
    $taskName = "\ModelMatrix\$env:USERNAME"
    & schtasks.exe /Delete /TN $taskName /F *> $null
    Remove-Item -LiteralPath (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\ModelMatrix Dashboard.lnk') -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ModelMatrix') -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
if ($Purge -and (Test-Path -LiteralPath $DataRoot)) { Remove-Item -LiteralPath $DataRoot -Recurse -Force }
Write-Output (ConvertTo-Json @{ ok = $true; install_root_removed = $true; data_root = $DataRoot; data_purged = [bool]$Purge } -Compress)
