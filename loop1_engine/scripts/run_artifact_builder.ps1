param(
    [Parameter(Mandatory = $true)][string]$NodePath,
    [Parameter(Mandatory = $true)][string]$NodeModulesPath,
    [Parameter(Mandatory = $true)][string]$BuilderPath,
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$MapPath,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$MapJsonPath,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$KmlPath,
    [Parameter(Mandatory = $true)][string]$EvidenceJsonPath
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$stage = Join-Path $tempRoot ("macrostrat-review-v2-" + [guid]::NewGuid().ToString("N"))
$stageFull = [System.IO.Path]::GetFullPath($stage)
if (-not $stageFull.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    -not ([System.IO.Path]::GetFileName($stageFull)).StartsWith("macrostrat-review-v2-")) {
    throw "Refusing unsafe Artifact Tool staging directory: $stageFull"
}

New-Item -ItemType Directory -Path $stageFull | Out-Null
try {
    $stagedBuilder = Join-Path $stageFull "build_review_v2.mjs"
    Copy-Item -LiteralPath $BuilderPath -Destination $stagedBuilder
    New-Item -ItemType Junction -Path (Join-Path $stageFull "node_modules") -Target $NodeModulesPath | Out-Null

    & $NodePath $stagedBuilder $InputPath $OutputPath $MapPath $MapJsonPath $KmlPath $EvidenceJsonPath
    if ($LASTEXITCODE -ne 0) {
        throw "Node spreadsheet builder exited with code $LASTEXITCODE"
    }
}
finally {
    # The validated GUID-prefixed target is always under the operating-system temp root.
    if (Test-Path -LiteralPath $stageFull) {
        Remove-Item -LiteralPath $stageFull -Recurse -Force
    }
}
