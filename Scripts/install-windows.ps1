[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ModelMatrix'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'ModelMatrix'),
    [switch]$NoBuild,
    [switch]$StartupTask,
    [switch]$StartupShortcut,
    [string]$PythonPath,
    [switch]$NoShortcuts
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $scriptPath = $MyInvocation.MyCommand.Path
    $SourceRoot = Split-Path -Parent (Split-Path -Parent $scriptPath)
}
function Invoke-Checked([string]$File, [string[]]$Arguments) { & $File @Arguments; if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $File" } }
function Ensure-Dir([string]$Path) { New-Item -ItemType Directory -Force -Path $Path | Out-Null }
function Quote-PowerShellLiteral([string]$Value) { "'" + ($Value -replace "'", "''") + "'" }
function Resolve-Python([string]$Requested) {
    if ($Requested) { return (Resolve-Path -LiteralPath $Requested -ErrorAction Stop).Path }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $raw = & $py.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Out-String
        $resolved = if ($null -eq $raw) { '' } else { $raw.Trim() }
        if ($LASTEXITCODE -eq 0 -and $resolved) { return $resolved }
    }
    throw 'Python 3.10+ is required; provide -PythonPath or install python/py -3 for the current user.'
}
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot); $InstallRoot = [IO.Path]::GetFullPath($InstallRoot); $DataRoot = [IO.Path]::GetFullPath($DataRoot)
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is not set; run as a normal Windows user.' }
$required = @('Windows\controller.py','Windows\modelmatrixctl.py','Windows\tray.py','RemoteAgent\agent_core.py','RemoteAgent\modelmatrix_agent.py')
foreach ($item in $required) { if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $item) -PathType Leaf)) { throw "Missing source file: $item" } }
$python = Resolve-Python $PythonPath
$pythonVersion = (& $python --version 2>&1).ToString().Trim()
if ($pythonVersion -notmatch '^Python (3\.([1-9][0-9]|[1-9][0-9][0-9])|[4-9])\.') { throw "Python 3.10+ required; found $pythonVersion" }
Ensure-Dir $InstallRoot; Ensure-Dir $DataRoot; Ensure-Dir (Join-Path $DataRoot 'Logs'); Ensure-Dir (Join-Path $DataRoot 'run'); Ensure-Dir (Join-Path $DataRoot 'profiles')
if (-not $NoBuild) { & (Join-Path $PSScriptRoot 'build-windows.ps1') -SourceRoot $SourceRoot -PythonPath $python }
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot 'Dashboard\dist\index.html') -PathType Leaf)) { throw 'Dashboard/dist/index.html is missing; build the dashboard or omit -NoBuild.' }
$venv = Join-Path $InstallRoot 'venv'; $venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Invoke-Checked $python @('-m','venv',$venv)
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { throw "Python venv creation failed: $venvPython" }
foreach ($relative in @('Windows','RemoteAgent')) {
    $from = Join-Path $SourceRoot $relative; $to = Join-Path $InstallRoot $relative
    Ensure-Dir $to; Copy-Item -LiteralPath $from -Destination (Split-Path -Parent $to) -Recurse -Force
}
Ensure-Dir (Join-Path $InstallRoot 'Dashboard')
Copy-Item -LiteralPath (Join-Path $SourceRoot 'Dashboard\dist') -Destination (Join-Path $InstallRoot 'Dashboard') -Recurse -Force
$controllerLauncherPs = Join-Path $InstallRoot 'ModelMatrixController.ps1'
$dataRootLiteral = Quote-PowerShellLiteral $DataRoot
$profilesLiteral = Quote-PowerShellLiteral (Join-Path $DataRoot 'profiles')
@('[CmdletBinding()]', 'param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)', '$ErrorActionPreference = ''Stop''', '$python = Join-Path $PSScriptRoot ''venv\Scripts\python.exe''', '$controller = Join-Path $PSScriptRoot ''Windows\controller.py''', "& `$python `$controller '--root' $dataRootLiteral '--profiles-dir' $profilesLiteral @Arguments", 'exit $LASTEXITCODE') | Set-Content -LiteralPath $controllerLauncherPs -Encoding UTF8
$launcher = Join-Path $InstallRoot 'ModelMatrixController.cmd'
@('@echo off', 'setlocal', 'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ModelMatrixController.ps1" %*', 'exit /b %errorlevel%') | Set-Content -LiteralPath $launcher -Encoding ASCII
$ctlPs = Join-Path $InstallRoot 'modelmatrixctl.ps1'
@('[CmdletBinding()]', 'param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)', '$ErrorActionPreference = ''Stop''', '$python = Join-Path $PSScriptRoot ''venv\Scripts\python.exe''', '$ctl = Join-Path $PSScriptRoot ''Windows\modelmatrixctl.py''', '& $python $ctl @Arguments', 'exit $LASTEXITCODE') | Set-Content -LiteralPath $ctlPs -Encoding UTF8
$ctl = Join-Path $InstallRoot 'modelmatrixctl.cmd'
@('@echo off', 'setlocal', 'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0modelmatrixctl.ps1" %*', 'exit /b %errorlevel%') | Set-Content -LiteralPath $ctl -Encoding ASCII
$trayPs = Join-Path $InstallRoot 'ModelMatrixTray.ps1'
@('[CmdletBinding()]', 'param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)', '$ErrorActionPreference = ''Stop''', '$python = Join-Path $PSScriptRoot ''venv\Scripts\python.exe''', '$tray = Join-Path $PSScriptRoot ''Windows\tray.py''', '& $python $tray @Arguments', 'exit $LASTEXITCODE') | Set-Content -LiteralPath $trayPs -Encoding UTF8
$trayLauncher = Join-Path $InstallRoot 'ModelMatrixTray.cmd'
@('@echo off', 'setlocal', 'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ModelMatrixTray.ps1" %*', 'exit /b %errorlevel%') | Set-Content -LiteralPath $trayLauncher -Encoding ASCII
if (-not $NoShortcuts -or $StartupShortcut) { $ws = New-Object -ComObject WScript.Shell }
if (-not $NoShortcuts) {
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ModelMatrix'; Ensure-Dir $startMenu
    $link = $ws.CreateShortcut((Join-Path $startMenu 'ModelMatrix Dashboard.lnk')); $link.TargetPath = $launcher; $link.Arguments = '--no-browser'; $link.WorkingDirectory = $InstallRoot; $link.Description = 'ModelMatrix local dashboard'; $link.Save()
    $trayLink = $ws.CreateShortcut((Join-Path $startMenu 'ModelMatrix Tray.lnk')); $trayLink.TargetPath = $trayLauncher; $trayLink.WorkingDirectory = $InstallRoot; $trayLink.Description = 'ModelMatrix tray companion'; $trayLink.Save()
}
if ($StartupShortcut) {
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'; Ensure-Dir $startup
    $sl = $ws.CreateShortcut((Join-Path $startup 'ModelMatrix Dashboard.lnk')); $sl.TargetPath = $launcher; $sl.Arguments = '--no-browser'; $sl.WorkingDirectory = $InstallRoot; $sl.Save()
}
if ($StartupTask) {
    $taskCommand = "`"$launcher`" --no-browser"
    $taskName = "\ModelMatrix\$env:USERNAME"
    & schtasks.exe /Create /TN $taskName /SC ONLOGON /TR $taskCommand /RL LIMITED /F | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not create the per-user startup task (exit $LASTEXITCODE)." }
}
Write-Output (ConvertTo-Json @{ ok = $true; install_root = $InstallRoot; data_root = $DataRoot; startup_task = [bool]$StartupTask; startup_shortcut = [bool]$StartupShortcut } -Compress)
