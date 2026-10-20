[CmdletBinding()]
param(
    [string]$SourceRoot,
    [switch]$SkipDashboard,
    [string]$PythonPath
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $scriptPath = $MyInvocation.MyCommand.Path
    $SourceRoot = Split-Path -Parent (Split-Path -Parent $scriptPath)
}
function Invoke-Checked([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $File $($Arguments -join ' ')" }
}
function Invoke-CommandChecked([string[]]$Command, [string[]]$Arguments) {
    $prefix = @($Command | Select-Object -Skip 1)
    & $Command[0] @prefix @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $($Command -join ' ') $($Arguments -join ' ')" }
}
function Resolve-Python([string]$Requested) {
    if ($Requested) {
        $candidate = (Resolve-Path -LiteralPath $Requested -ErrorAction Stop).Path
        return @($candidate)
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notmatch '\\WindowsApps\\python(?:3)?\.exe$') {
        & $python.Source --version *> $null
        if ($LASTEXITCODE -eq 0) { return @($python.Source) }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $raw = & $py.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Out-String
        $resolved = if ($null -eq $raw) { '' } else { $raw.Trim() }
        if ($LASTEXITCODE -eq 0 -and $resolved) { return @($resolved) }
    }
    throw 'Python 3.10+ is required; provide -PythonPath or install python/py -3 for the current user.'
}
function Python-Version([string[]]$Command) { $prefix = @($Command | Select-Object -Skip 1); (& $Command[0] @prefix --version 2>&1).ToString().Trim() }
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
$dashboard = Join-Path $SourceRoot 'Dashboard'
$controller = Join-Path $SourceRoot 'Windows\controller.py'
if (-not (Test-Path -LiteralPath $controller -PathType Leaf)) { throw "Windows/controller.py is missing. Build from a complete checkout." }
$python = Resolve-Python $PythonPath
$version = Python-Version $python
if ($version -notmatch '^Python (3\.([1-9][0-9]|[1-9][0-9][0-9])|[4-9])\.') { throw "Python 3.10+ required; found $version" }
if (-not $SkipDashboard) {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) { throw 'Node.js 22.12+ is required on PATH; install it per Windows/README.md.' }
    $nodeVersion = (& $node.Source --version).Trim()
    try { $nodeNumeric = [version]($nodeVersion.TrimStart('v')) } catch { throw "Could not parse Node.js version: $nodeVersion" }
    if ($nodeNumeric.Major -lt 22 -or ($nodeNumeric.Major -eq 22 -and $nodeNumeric.Minor -lt 12)) { throw "Node.js 22.12+ required; found $nodeVersion" }
    $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
    if ($pnpm) { $pnpmCommand = @($pnpm.Source) }
    else {
        $corepack = Get-Command corepack -ErrorAction SilentlyContinue
        if (-not $corepack) { throw 'pnpm is required; install it for the current user or enable it with Corepack.' }
        $pnpmCommand = @($corepack.Source, 'pnpm')
    }
    Push-Location $dashboard
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $dashboard 'pnpm-lock.yaml'))) { throw 'Dashboard/pnpm-lock.yaml is required for a frozen build.' }
        Invoke-CommandChecked $pnpmCommand @('install','--frozen-lockfile')
        Invoke-CommandChecked $pnpmCommand @('build')
    } finally { Pop-Location }
} elseif (-not (Test-Path -LiteralPath (Join-Path $dashboard 'dist\index.html') -PathType Leaf)) {
    throw 'Dashboard/dist/index.html is required with -SkipDashboard (NoBuild). Run build-windows.ps1 first.'
}
$previousCompileRoot = $env:MODELMATRIX_COMPILE_ROOT
$env:MODELMATRIX_COMPILE_ROOT = $SourceRoot
try { & $python[0] @($python | Select-Object -Skip 1) -c 'import os,pathlib; root=pathlib.Path(os.environ["MODELMATRIX_COMPILE_ROOT"]); [compile(p.read_text(encoding="utf-8"), str(p), "exec") for p in root.rglob("*.py") if "__pycache__" not in p.parts]' }
finally { if ($null -eq $previousCompileRoot) { Remove-Item Env:MODELMATRIX_COMPILE_ROOT -ErrorAction SilentlyContinue } else { $env:MODELMATRIX_COMPILE_ROOT = $previousCompileRoot } }
if ($LASTEXITCODE -ne 0) { throw 'Python source compilation failed.' }
Write-Output (ConvertTo-Json @{ ok = $true; source = $SourceRoot; dashboard = (-not $SkipDashboard); python = $version.Trim() } -Compress)
