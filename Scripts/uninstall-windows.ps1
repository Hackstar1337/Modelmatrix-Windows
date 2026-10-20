[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ModelMatrix'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'ModelMatrix'),
    [switch]$Purge,
    [switch]$KeepStartup
)
$ErrorActionPreference = 'Stop'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
& (Join-Path $PSScriptRoot 'stop-windows.ps1') -DataRoot $DataRoot -Force
$launcherTargets = @((Join-Path $InstallRoot 'ModelMatrixController.cmd'), (Join-Path $InstallRoot 'ModelMatrixTray.cmd')) | ForEach-Object { [IO.Path]::GetFullPath($_) }
function Test-OwnedShortcut([string]$LinkPath) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($LinkPath)
        $target = [string]$shortcut.TargetPath
        if ([string]::IsNullOrWhiteSpace($target)) { return $false }
        $resolved = [IO.Path]::GetFullPath($target)
        return [bool]($launcherTargets | Where-Object { [StringComparer]::OrdinalIgnoreCase.Equals($_, $resolved) })
    } catch { return $false }
}
function Remove-OwnedShortcut([string]$LinkPath) {
    if ((Test-Path -LiteralPath $LinkPath -PathType Leaf) -and (Test-OwnedShortcut $LinkPath)) {
        Remove-Item -LiteralPath $LinkPath -Force -ErrorAction Stop
        return $true
    }
    return $false
}
function Test-OwnedStartupTask([string]$TaskName) {
    try {
        $details = (& schtasks.exe /Query /TN $TaskName /FO LIST /V 2>$null | Out-String)
        return (-not [string]::IsNullOrWhiteSpace($details) -and $details.IndexOf($InstallRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    } catch { return $false }
}
if (-not $KeepStartup) {
    $taskName = "\ModelMatrix\$env:USERNAME"
    if (Test-OwnedStartupTask $taskName) { & schtasks.exe /Delete /TN $taskName /F *> $null }
    $startupLink = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\ModelMatrix Dashboard.lnk'
    [void](Remove-OwnedShortcut $startupLink)
}
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ModelMatrix'
if (Test-Path -LiteralPath $startMenu -PathType Container) {
    foreach ($link in @(Get-ChildItem -LiteralPath $startMenu -Filter '*.lnk' -File -Force -ErrorAction SilentlyContinue)) { [void](Remove-OwnedShortcut $link.FullName) }
    if (@(Get-ChildItem -LiteralPath $startMenu -Force -ErrorAction SilentlyContinue).Count -eq 0) { Remove-Item -LiteralPath $startMenu -Force -ErrorAction SilentlyContinue }
}
if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
if ($Purge -and (Test-Path -LiteralPath $DataRoot)) { Remove-Item -LiteralPath $DataRoot -Recurse -Force }
Write-Output (ConvertTo-Json @{ ok = $true; install_root_removed = $true; data_root = $DataRoot; data_purged = [bool]$Purge } -Compress)
