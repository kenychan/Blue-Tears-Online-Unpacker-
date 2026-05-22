param(
    [Parameter(Mandatory=$true)]
    [string]$ArchiveRoot,

    [Parameter(Mandatory=$true)]
    [string]$RuntimeImage,

    [string]$Out = "",
    [string]$Python = "python",
    [string[]]$Archives = @()
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not $Out) {
    $Out = Join-Path $repoRoot "data\extracted_named"
}
$manifestDir = Join-Path $repoRoot "exports\manifests"
New-Item -ItemType Directory -Force -Path $manifestDir | Out-Null

$argsList = @(
    (Join-Path $PSScriptRoot "sfa_named_extract_v4.py"),
    "--root", $ArchiveRoot,
    "--out", $Out,
    "--runtime-image", $RuntimeImage,
    "--manifest", (Join-Path $manifestDir "sfa_named_manifest.csv")
)
if ($Archives.Count -gt 0) {
    $argsList += "--archives"
    $argsList += $Archives
}

& $Python @argsList
