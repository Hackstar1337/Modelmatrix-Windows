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
function Get-RecordProcess([object]$Identity) {
    $candidate = Get-Process -Id ([int]$Identity.pid) -ErrorAction SilentlyContinue
    if (-not $candidate) { return $null }
    try {
        $actual = [IO.Path]::GetFullPath($candidate.MainModule.FileName)
        $expected = [IO.Path]::GetFullPath([string]$Identity.executable)
        $created = $candidate.StartTime.ToUniversalTime()
        $expectedCreated = [DateTime]::Parse([string]$Identity.started_utc).ToUniversalTime()
    } catch { throw "Cannot validate existing controller identity (PID $($Identity.pid)); refusing to overwrite it." }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($actual, $expected)) { throw "Existing controller identity mismatch (executable); refusing to overwrite PID $($Identity.pid)." }
    if ([Math]::Abs(($created - $expectedCreated).TotalSeconds) -gt 2) { throw "Existing controller identity mismatch (creation time); refusing to overwrite PID $($Identity.pid)." }
    return $candidate
}
function Test-TcpPortAvailable([int]$PortNumber) {
    $listener = $null
    try {
        $listener = New-Object -TypeName System.Net.Sockets.TcpListener -ArgumentList @([Net.IPAddress]::Loopback, $PortNumber)
        $listener.Start(); return $true
    } catch { return $false }
    finally { if ($listener) { try { $listener.Stop() } catch {} } }
}
function Test-PortOwnedBy([int]$PortNumber, [int]$ProcessId) {
    try {
        $listeners = @(Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction Stop)
        return [bool]($listeners | Where-Object { [int]$_.OwningProcess -eq $ProcessId })
    } catch {
        try {
            $needle = ':' + $PortNumber.ToString().PadLeft(5, '0')
            $rows = @(netstat.exe -ano -p tcp 2>$null | Where-Object { $_ -match 'LISTENING' -and $_ -match $needle })
            return [bool]($rows | Where-Object { $_ -match ('\s' + [string]$ProcessId + '\s*$') })
        } catch { return $false }
    }
}
function Get-AuthHeaders([string]$Token) {
    $headers = @{ Accept = 'application/json'; Origin = "http://127.0.0.1:$Port" }
    if ($Token) { $headers.Authorization = 'Bearer ' + $Token }
    return $headers
}
function Test-ControllerReady([string]$Uri, [string]$Token) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Method Get -Uri $Uri -Headers (Get-AuthHeaders $Token) -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch { return $false }
}
function Get-ControllerRecordProcess([string]$RecordPath, [string]$ExpectedController, [string]$ExpectedRoot) {
    if (-not (Test-Path -LiteralPath $RecordPath -PathType Leaf)) { return $null }
    try { $identity = Get-Content -LiteralPath $RecordPath -Raw | ConvertFrom-Json } catch { return $null }
    if ([string]::IsNullOrWhiteSpace([string]$identity.controller) -or [string]::IsNullOrWhiteSpace([string]$identity.root)) { return $null }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath([string]$identity.controller), [IO.Path]::GetFullPath($ExpectedController))) { throw 'Controller identity record points to a different installation; refusing to continue.' }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath([string]$identity.root), [IO.Path]::GetFullPath($ExpectedRoot))) { throw 'Controller identity record points to a different data root; refusing to continue.' }
    return (Get-RecordProcess $identity)
}
function Stop-StartedProcess([object]$Process) {
    if ($Process) {
        try { $Process.Refresh() } catch {}
        if (-not $Process.HasExited) { Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue }
    }
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
if ($RelayPort -lt 1 -or $RelayPort -gt 65535) { throw 'RelayPort must be between 1 and 65535.' }
if ($Port -eq $RelayPort) { throw 'Port and RelayPort must be different.' }
$controller = Join-Path $InstallRoot 'Windows\controller.py'
$run = Join-Path $DataRoot 'run'
$pidFile = Join-Path $run 'controller.pid'
$log = Join-Path $DataRoot 'Logs\controller.log'
$errorLog = Join-Path $DataRoot 'Logs\controller-error.log'
if (-not (Test-Path -LiteralPath $controller -PathType Leaf)) { throw "Controller not installed: $controller" }
$venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython -PathType Leaf) { $pythonPath = (Resolve-Path -LiteralPath $venvPython).Path }
else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $pythonPath = $pythonCommand.Source }
}
if ([string]::IsNullOrWhiteSpace($pythonPath)) { throw 'Python 3.10+ is required on PATH or in the installed venv.' }
$token = $null
if ($AuthTokenFile) {
    if (-not (Test-Path -LiteralPath $AuthTokenFile -PathType Leaf)) { throw "Auth token file is not a file: $AuthTokenFile" }
    $token = (Get-Content -LiteralPath (Resolve-Path -LiteralPath $AuthTokenFile).Path -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($token)) { throw 'Auth token file is empty.' }
}
Ensure-Dir $run; Ensure-Dir (Split-Path -Parent $log); Ensure-Dir (Join-Path $DataRoot 'profiles')
if (Test-Path -LiteralPath $pidFile) {
    try { $old = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json } catch { throw 'Existing controller record is invalid; refusing to overwrite it.' }
    if (-not $old.pid -or -not $old.executable -or -not $old.started_utc) { throw 'Existing controller record is incomplete; refusing to overwrite it.' }
    $oldProcess = Get-RecordProcess $old
    if ($oldProcess) { throw "ModelMatrix is already running (PID $($oldProcess.Id)); stop it with stop-windows.ps1." }
    Remove-Item -LiteralPath $pidFile -Force
}
if (-not (Test-TcpPortAvailable $Port)) { throw "Controller port $Port is occupied or unavailable; refusing to start." }
if (-not (Test-TcpPortAvailable $RelayPort)) { throw "Relay port $RelayPort is occupied or unavailable; refusing to start." }
$controllerArgs = @((Join-Path $InstallRoot 'Windows\controller.py'),'--root',$DataRoot,'--profiles-dir',(Join-Path $DataRoot 'profiles'),'--port',([string]$Port))
if ($AuthTokenFile) { $controllerArgs += @('--auth-token-file',(Resolve-Path -LiteralPath $AuthTokenFile).Path) }
$help = (& $pythonPath $controller '--help' 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) { throw "Could not inspect controller command-line options (exit $LASTEXITCODE)." }
if ($help -match '(?m)--relay-port(?:\s|,|=)') { $controllerArgs += @('--relay-port',([string]$RelayPort)) }
if ($NoBrowser) { $controllerArgs += '--no-browser' }
if ($Foreground) { & $pythonPath @controllerArgs; exit $LASTEXITCODE }
$argumentLine = (($controllerArgs | ForEach-Object { Quote-Arg ($_ -as [string]) }) -join ' ')
$proc = $null
try {
    $launcher = Join-Path $InstallRoot 'Windows\background_launch.py'
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "Background launcher not installed: $launcher" }
    $launchConfig = Join-Path $run ('launch-' + [guid]::NewGuid().ToString('N') + '.json')
    try {
        @{ command = @($pythonPath) + @($controllerArgs); cwd = $InstallRoot; stdout = $log; stderr = $errorLog } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $launchConfig -Encoding UTF8
        $launchOutput = & $pythonPath $launcher --config $launchConfig
        if ($LASTEXITCODE -ne 0) { throw "Background launcher failed (exit $LASTEXITCODE)." }
        $launchIdentity = ($launchOutput | Out-String) | ConvertFrom-Json
        $proc = Get-Process -Id ([int]$launchIdentity.pid) -ErrorAction Stop
    } finally { Remove-Item -LiteralPath $launchConfig -Force -ErrorAction SilentlyContinue }
}
catch { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue; throw }
if (-not $proc -or $proc.Id -le 0) { throw 'Failed to start the controller process.' }
$started = $proc.StartTime.ToUniversalTime().ToString('o')
$record = [ordered]@{ schema = 2; pid = $proc.Id; executable = (Resolve-Path -LiteralPath $pythonPath).Path; controller = (Resolve-Path -LiteralPath $controller).Path; root = $DataRoot; arguments = @($controllerArgs); environment = @{ MODELMATRIX_DATA_ROOT = $DataRoot; MODELMATRIX_PROFILES_DIR = (Join-Path $DataRoot 'profiles') }; command_line = $argumentLine; started_utc = $started; port = $Port; relay_port = $RelayPort; shutdown_url = "http://127.0.0.1:$Port/api/shutdown" }
try { Set-Content -LiteralPath $pidFile -Value ($record | ConvertTo-Json -Compress) -Encoding UTF8 } catch { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }; Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue; throw }
$controllerProcess = $proc
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $observed = Get-ControllerRecordProcess $pidFile $controller $DataRoot
    if ($observed) { $controllerProcess = $observed }
    try { $controllerProcess.Refresh() } catch {}
    if ($controllerProcess.HasExited) { break }
    if (Test-ControllerReady "http://127.0.0.1:$Port/api/status" $token) { $ready = $true; break }
    Start-Sleep -Milliseconds 250
}
if (-not $ready) {
    Stop-StartedProcess $controllerProcess
    if ($controllerProcess.Id -ne $proc.Id) { Stop-StartedProcess $proc }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    throw "Controller failed readiness or port $Port is owned by another process; see $log and $errorLog"
}
Write-Output (ConvertTo-Json @{ ok = $true; pid = $controllerProcess.Id; port = $Port; relay_port = $RelayPort; relay_supported = [bool]($help -match '(?m)--relay-port(?:\s|,|=)'); log = $log; error_log = $errorLog } -Compress)
