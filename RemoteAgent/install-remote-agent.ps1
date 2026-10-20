[CmdletBinding()]
param(
    [string]$SourceRoot = "",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "ModelMatrix\Agent"),
    [int]$Port = 7700,
    [switch]$StartupTask,
    [switch]$Foreground,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"

function Fail([string]$Message) { throw $Message }
function Require-UserPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { Fail "A source root is required; do not download an unverified remote branch." }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { Fail "Source root does not exist: $Path" }
}
function Atomic-Copy([string]$Name, [string]$Destination, [bool]$Executable = $false) {
    $source = Join-Path $SourceRoot $Name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { Fail "Staged source is missing $Name" }
    $tmp = "$Destination.new.$([guid]::NewGuid().ToString('N'))"
    Copy-Item -LiteralPath $source -Destination $tmp -Force
    Move-Item -LiteralPath $tmp -Destination $Destination -Force
}

if ($Port -lt 1 -or $Port -gt 65535) { Fail "Port must be between 1 and 65535." }
$launcher = Join-Path $InstallRoot "modelmatrix-agent.cmd"
$python = (Get-Command py -ErrorAction SilentlyContinue)
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python -and -not $Uninstall) { Fail "Python 3.10+ (py or python) is required." }

if ($Uninstall) {
    if ($StartupTask) { Unregister-ScheduledTask -TaskName "ModelMatrix Remote Agent" -Confirm:$false -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $launcher -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
    Write-Output "Removed the per-user ModelMatrix remote agent; user data was not searched or deleted."
    exit 0
}

Require-UserPath $SourceRoot
New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot "run") | Out-Null
$modules = @("agent_core.py", "discovery.py", "hf_agent.py", "client_connections.py", "platform_support.py", "modelmatrix_agent.py")
foreach ($module in $modules) {
    $destination = Join-Path $InstallRoot $module
    Atomic-Copy $module $destination
}

$pythonExe = $python.Source
$pythonArgs = if ($python.Name -eq "py.exe") { @("-3") } else { @() }
@"
@echo off
"$pythonExe" $($pythonArgs -join ' ') "$InstallRoot\modelmatrix_agent.py" --root "$InstallRoot" %*
"@ | Set-Content -LiteralPath $launcher -Encoding ASCII

$config = Join-Path $InstallRoot "config.json"
if (-not (Test-Path -LiteralPath $config)) {
    '{"profiles_dir":"' + ((Join-Path $InstallRoot "profiles") -replace '\\','\\') + '"}' | Set-Content -LiteralPath $config -Encoding UTF8
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot "profiles") | Out-Null
}
& $pythonExe @pythonArgs "$InstallRoot\modelmatrix_agent.py" --version | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "The staged agent failed its --version smoke test." }

if ($StartupTask) {
    $action = New-ScheduledTaskAction -Execute $launcher -Argument "serve --port $Port"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "ModelMatrix Remote Agent" -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName "ModelMatrix Remote Agent"
    Write-Output "Installed and started a per-user Scheduled Task on port $Port."
} elseif ($Foreground) {
    Write-Output "Starting the staged agent in the foreground on port $Port."
    & $launcher serve --port $Port
} else {
    Write-Output "Installed the per-user agent at $InstallRoot."
    Write-Output "No background task was created. Start with: $launcher serve --port $Port"
}
