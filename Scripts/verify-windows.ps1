[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ModelMatrix'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'ModelMatrix'),
    [int]$Port = 7700,
    [switch]$NoControllerProbe
)
$ErrorActionPreference = 'Stop'
$required = @('Windows\controller.py','Windows\modelmatrixctl.py','Windows\tray.py','RemoteAgent\agent_core.py','RemoteAgent\modelmatrix_agent.py','Dashboard\dist\index.html')
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $InstallRoot $_) -PathType Leaf) })
if ($missing.Count -gt 0) { throw "Installation is incomplete; missing: $($missing -join ', ')" }
$py = Get-Command python -ErrorAction SilentlyContinue
$venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
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
    try { $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 3; if ($response.StatusCode -ne 200) { throw "HTTP $($response.StatusCode)" }; $report.controller = $true } catch { throw "Controller health probe failed at ${uri}: $($_.Exception.Message)" }
}
Write-Output (ConvertTo-Json $report -Compress)
