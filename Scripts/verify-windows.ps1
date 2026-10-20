[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ModelMatrix'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'ModelMatrix'),
    [int]$Port = 7700,
    [switch]$NoControllerProbe,
    [string]$AuthTokenFile
)
$ErrorActionPreference = 'Stop'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
$required = @('Windows\controller.py','Windows\modelmatrixctl.py','Windows\tray.py','RemoteAgent\agent_core.py','RemoteAgent\modelmatrix_agent.py','Dashboard\dist\index.html')
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $InstallRoot $_) -PathType Leaf) })
if ($missing.Count -gt 0) { throw "Installation is incomplete; missing: $($missing -join ', ')" }
$venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
$py = Get-Command python -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $venvPython -PathType Leaf) { $pythonVersion = (& $venvPython --version 2>&1).Trim() }
elseif ($py) { $pythonVersion = (& $py.Source --version 2>&1).Trim() }
else {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $launcher) { throw 'Python 3.10+ is required; python or py -3 must be available.' }
    $pythonVersion = (& $launcher.Source -3 --version 2>&1).Trim()
}
if ($pythonVersion -notmatch '^Python (3\.([1-9][0-9]|[1-9][0-9][0-9])|[4-9])\.') { throw "Python 3.10+ required; found $pythonVersion" }
$report = [ordered]@{ ok = $true; install_root = $InstallRoot; data_root = $DataRoot; python = $pythonVersion; controller = $false }
if (-not $NoControllerProbe) {
    $uri = "http://127.0.0.1:$Port/api/status"
    try {
        $headers = @{ Accept = 'application/json'; Origin = "http://127.0.0.1:$Port" }
        if ($AuthTokenFile) {
            if (-not (Test-Path -LiteralPath $AuthTokenFile -PathType Leaf)) { throw "Auth token file is not a file: $AuthTokenFile" }
            $token = (Get-Content -LiteralPath (Resolve-Path -LiteralPath $AuthTokenFile).Path -Raw).Trim()
            if ([string]::IsNullOrWhiteSpace($token)) { throw 'Auth token file is empty.' }
            $headers.Authorization = 'Bearer ' + $token
        }
        $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers $headers -TimeoutSec 3
        if ($response.StatusCode -ne 200) { throw "HTTP $($response.StatusCode)" }
        $report.controller = $true
    } catch { throw "Controller health probe failed at ${uri}: $($_.Exception.Message)" }
}
Write-Output (ConvertTo-Json $report -Compress)
