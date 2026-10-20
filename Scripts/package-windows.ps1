[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$OutputDirectory,
    [string]$Version = 'dev',
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
function Ensure-Dir([string]$Path) { New-Item -ItemType Directory -Force -Path $Path | Out-Null }
if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $scriptPath = $MyInvocation.MyCommand.Path
    $SourceRoot = Split-Path -Parent (Split-Path -Parent $scriptPath)
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) { $OutputDirectory = Join-Path $SourceRoot 'dist-windows' }
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot); $OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory); Ensure-Dir $OutputDirectory
if (-not $SkipBuild) { & (Join-Path $PSScriptRoot 'build-windows.ps1') -SourceRoot $SourceRoot }
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot 'Dashboard\dist\index.html') -PathType Leaf)) { throw 'Dashboard/dist/index.html is required; do not package an unbuilt dashboard.' }
$stage = Join-Path ([IO.Path]::GetTempPath()) ('ModelMatrix-windows-' + [guid]::NewGuid().ToString('N')); Ensure-Dir $stage
try {
    foreach ($relative in @('Windows','RemoteAgent','Dashboard\dist','Scripts')) {
        $src = Join-Path $SourceRoot $relative
        if (Test-Path -LiteralPath $src) {
            $destination = Join-Path $stage $relative
            Ensure-Dir (Split-Path -Parent $destination)
            Copy-Item -LiteralPath $src -Destination $destination -Recurse -Force
        }
    }
    foreach ($relative in @('LICENSE','README.md','SETUP.md','Windows\README.md','Controller\RUNTIME_SUPPORT.md','RemoteAgent\README.md','Dashboard\package.json','Dashboard\pnpm-lock.yaml','Dashboard\pnpm-workspace.yaml')) {
        $src = Join-Path $SourceRoot $relative
        if (Test-Path -LiteralPath $src -PathType Leaf) {
            $destination = Join-Path $stage $relative
            Ensure-Dir (Split-Path -Parent $destination)
            Copy-Item -LiteralPath $src -Destination $destination -Force
        }
    }
    foreach ($relative in @('Windows\tests','RemoteAgent\tests')) {
        $src = Join-Path $SourceRoot $relative
        if (Test-Path -LiteralPath $src -PathType Container) {
            $destination = Join-Path $stage $relative
            Ensure-Dir (Split-Path -Parent $destination)
            Copy-Item -LiteralPath $src -Destination $destination -Recurse -Force
        }
    }
    $zip = Join-Path $OutputDirectory ("ModelMatrix-$Version-windows.zip"); if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }; Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant(); Set-Content -LiteralPath ($zip + '.sha256') -Value "$hash *$(Split-Path -Leaf $zip)" -Encoding ASCII
    Write-Output (ConvertTo-Json @{ ok = $true; package = $zip; sha256 = $hash } -Compress)
} finally { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
