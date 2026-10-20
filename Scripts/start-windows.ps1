[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ModelMatrix'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'ModelMatrix'),
    [int]$Port = 7700,
    [int]$RelayPort = 7701,
    [switch]$NoBrowser,
    [switch]$Foreground,
    [string]$AuthTokenFile
)
$ErrorActionPreference = 'Stop'
function Ensure-Dir([string]$Path) { New-Item -ItemType Directory -Force -Path $Path | Out-Null }
function Quote-Arg([string]$Value) { '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }
$controller = Join-Path $InstallRoot 'Windows\controller.py'; $run = Join-Path $DataRoot 'run'; $pidFile = Join-Path $run 'controller.pid'; $log = Join-Path $DataRoot 'Logs\controller.log'; $errorLog = Join-Path $DataRoot 'Logs\controller-error.log'
if (-not (Test-Path -LiteralPath $controller -PathType Leaf)) { throw "Controller not installed: $controller" }
$venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'; if (Test-Path -LiteralPath $venvPython -PathType Leaf) { $pythonPath = $venvPython } else { $pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source }
if ([string]::IsNullOrWhiteSpace($pythonPath)) { throw 'Python 3.10+ is required on PATH or in the installed venv.' }
Ensure-Dir $run; Ensure-Dir (Split-Path -Parent $log)
if (Test-Path -LiteralPath $pidFile) {
    try { $old = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json } catch { throw 'Existing controller record is invalid; refusing to overwrite it.' }
    $oldProcess = Get-Process -Id ([int]$old.pid) -ErrorAction SilentlyContinue
    if ($oldProcess) { throw "ModelMatrix is already running (PID $($oldProcess.Id)); stop it with stop-windows.ps1." }
    Remove-Item -LiteralPath $pidFile -Force
}
$args = @((Join-Path $InstallRoot 'Windows\controller.py'),'--root',$DataRoot,'--profiles-dir',(Join-Path $DataRoot 'profiles'),'--port',([string]$Port))
if ($AuthTokenFile) {
    $tokenPath = (Resolve-Path -LiteralPath $AuthTokenFile -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) { throw "Auth token file is not a file: $AuthTokenFile" }
    $args += @('--auth-token-file', $tokenPath)
}
$help = (& $pythonPath $controller '--help' 2>&1 | Out-String)
if ($help -match '(?m)--relay-port(?:\s|,|=)') { $args += @('--relay-port',([string]$RelayPort)) }
if ($NoBrowser) { $args += '--no-browser' }
if ($Foreground) { & $pythonPath @args; exit $LASTEXITCODE }
$argumentLine = (($args | ForEach-Object { Quote-Arg ($_ -as [string]) }) -join ' ')
$proc = Start-Process -FilePath $pythonPath -ArgumentList $argumentLine -WorkingDirectory $InstallRoot -RedirectStandardOutput $log -RedirectStandardError $errorLog -PassThru -WindowStyle Hidden
if (-not $proc -or $proc.Id -le 0) { throw 'Failed to start the controller process.' }
$started = $proc.StartTime.ToUniversalTime().ToString('o')
$record = [ordered]@{ schema = 1; pid = $proc.Id; executable = (Resolve-Path -LiteralPath $pythonPath).Path; controller = (Resolve-Path -LiteralPath $controller).Path; root = (Resolve-Path -LiteralPath $DataRoot).Path; command_line = $argumentLine; started_utc = $started; port = $Port; shutdown_url = "http://127.0.0.1:$Port/api/shutdown" }
Set-Content -LiteralPath $pidFile -Value ($record | ConvertTo-Json -Compress) -Encoding UTF8
Start-Sleep -Milliseconds 500
if ($proc.HasExited) { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue; throw "Controller exited during startup (code $($proc.ExitCode)); see $log and $errorLog" }
Write-Output (ConvertTo-Json @{ ok = $true; pid = $proc.Id; port = $Port; relay_port = $RelayPort; relay_supported = [bool]($help -match '(?m)--relay-port(?:\s|,|=)'); log = $log; error_log = $errorLog } -Compress)
