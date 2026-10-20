[CmdletBinding()]
param(
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'ModelMatrix'),
    [switch]$Force,
    [int]$TimeoutSeconds = 10,
    [string]$AuthTokenFile
)
$ErrorActionPreference = 'Stop'
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
if ($TimeoutSeconds -lt 1) { throw 'TimeoutSeconds must be at least 1.' }
$pidFile = Join-Path $DataRoot 'run\controller.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output (ConvertTo-Json @{ ok = $true; stopped = $false; reason = 'not-running' } -Compress); exit 0 }
$record = try { Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json } catch { throw 'Controller identity record is invalid; refusing to kill a PID.' }
$targetPid = 0
if (-not [int]::TryParse([string]$record.pid, [ref]$targetPid) -or $targetPid -le 0 -or [string]::IsNullOrWhiteSpace([string]$record.executable) -or [string]::IsNullOrWhiteSpace([string]$record.controller) -or [string]::IsNullOrWhiteSpace([string]$record.started_utc)) { throw 'Controller identity record is incomplete; refusing to kill a PID.' }
$token = $null
if ($AuthTokenFile) {
    if (-not (Test-Path -LiteralPath $AuthTokenFile -PathType Leaf)) { throw "Auth token file is not a file: $AuthTokenFile" }
    $token = (Get-Content -LiteralPath (Resolve-Path -LiteralPath $AuthTokenFile).Path -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($token)) { throw 'Auth token file is empty.' }
}
function Get-ValidatedProcess([int]$ProcessId, [object]$Identity) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    try { $actualExecutable = [IO.Path]::GetFullPath($process.MainModule.FileName) } catch { throw 'Cannot validate controller executable; refusing to kill a PID.' }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($actualExecutable, [IO.Path]::GetFullPath([string]$Identity.executable))) { throw 'Controller identity mismatch (executable); refusing to kill a PID.' }
    try { $actualStarted = $process.StartTime.ToUniversalTime(); $expectedStarted = [DateTime]::Parse([string]$Identity.started_utc).ToUniversalTime() } catch { throw 'Cannot validate controller creation time; refusing to kill a PID.' }
    if ([Math]::Abs(($actualStarted - $expectedStarted).TotalSeconds) -gt 2) { throw 'Controller identity mismatch (creation time); refusing to kill a PID.' }
    try { $native = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop } catch { $native = $null }
    if ($native -and -not [string]::IsNullOrWhiteSpace([string]$native.CommandLine) -and ([string]$native.CommandLine -notlike "*$([IO.Path]::GetFileName($Identity.controller))*")) { throw 'Controller identity mismatch (command); refusing to kill a PID.' }
    return $process
}
$p = Get-ValidatedProcess $targetPid $record
if (-not $p) { Remove-Item -LiteralPath $pidFile -Force; Write-Output (ConvertTo-Json @{ ok = $true; stopped = $false; reason = 'stale-identity'; forced = $false } -Compress); exit 0 }
$port = 7700
if ($record.port) { [void][int]::TryParse([string]$record.port, [ref]$port) }
$origin = "http://127.0.0.1:$port"
$forced = [bool]$Force
$shutdownRequested = $false
if (-not $Force) {
    try {
        $headers = @{ Accept = 'application/json'; Origin = $origin }
        $webSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        if ($token) {
            $headers.Authorization = 'Bearer ' + $token
            $loginBody = (ConvertTo-Json @{ token = $token } -Compress)
            Invoke-WebRequest -UseBasicParsing -Method Post -Uri ($origin + '/api/dashboard/session') -Body $loginBody -ContentType 'application/json' -Headers $headers -WebSession $webSession -TimeoutSec 3 | Out-Null
        }
        $shutdown = Invoke-WebRequest -UseBasicParsing -Method Post -Uri ($origin + '/api/shutdown') -Body '{}' -ContentType 'application/json' -Headers $headers -WebSession $webSession -TimeoutSec 3
        if ($shutdown.StatusCode -lt 200 -or $shutdown.StatusCode -ge 300) { throw "shutdown request returned HTTP $($shutdown.StatusCode)" }
        $shutdownRequested = $true
    } catch {
        if ($token) { throw "Authenticated shutdown failed: $($_.Exception.Message)" }
    }
}
$p.Refresh()
if (-not $p.HasExited -and -not $Force -and $shutdownRequested) {
    [void]$p.CloseMainWindow()
    if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
        $p = Get-ValidatedProcess $targetPid $record
        if ($p -and -not $p.HasExited) { Stop-Process -Id $targetPid -Force -ErrorAction Stop; $forced = $true }
    }
} elseif (-not $p.HasExited) {
    $p = Get-ValidatedProcess $targetPid $record
    if ($p -and -not $p.HasExited) { Stop-Process -Id $targetPid -Force -ErrorAction Stop; $forced = $true }
}
$p.Refresh()
if (-not $p.HasExited -and -not $forced) { throw 'Controller did not stop; refusing to remove its identity record.' }
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Write-Output (ConvertTo-Json @{ ok = $true; stopped = $true; pid = $targetPid; forced = $forced } -Compress)
