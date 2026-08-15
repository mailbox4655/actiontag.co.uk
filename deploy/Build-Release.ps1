[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\out\releases'),
    [string]$PublicUrl = 'https://actiontag.co.uk'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Checked([string]$Program, [string[]]$Arguments, [string]$WorkingDirectory) {
    Push-Location $WorkingDirectory
    try {
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$trackedStatus = @(git -C $repoRoot status --porcelain=v1 --untracked-files=no)
if ($LASTEXITCODE -ne 0 -or $trackedStatus.Count -gt 0) {
    throw 'Release builds require a clean tracked Git working tree.'
}
$commit = (git -C $repoRoot rev-parse HEAD).Trim()
$remoteCommit = (git -C $repoRoot rev-parse origin/main).Trim()
$release = $commit.Substring(0, 12)
if ($commit -notmatch '^[0-9a-f]{40}$' -or $commit -ne $remoteCommit) {
    throw 'Release HEAD must exactly match origin/main before packaging.'
}
if ($PublicUrl -notmatch '^https://[A-Za-z0-9.-]+$') {
    throw 'PublicUrl must be one HTTPS origin without a path.'
}

$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null
$artifact = Join-Path $resolvedOutput "actiontag-$release-linux-x64.tar.gz"
$checksum = "$artifact.sha256"
foreach ($candidate in @($artifact, $checksum)) {
    if (Test-Path -LiteralPath $candidate) { throw "Refusing to overwrite release output: $candidate" }
}

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$tempRoot = Join-Path $tempBase ("actiontag-build-" + [guid]::NewGuid().ToString('N'))
$sourceRoot = Join-Path $tempRoot 'source'
$packageRoot = Join-Path $tempRoot 'package'
$sourceArchive = Join-Path $tempRoot 'source.tar'
New-Item -ItemType Directory -Path $sourceRoot, $packageRoot | Out-Null

try {
    Invoke-Checked 'git' @('-C', $repoRoot, 'archive', '--format=tar', "--output=$sourceArchive", $commit) $repoRoot
    Invoke-Checked 'tar.exe' @('-xf', $sourceArchive, '-C', $sourceRoot) $repoRoot
    Invoke-Checked 'npm.cmd' @('ci') (Join-Path $sourceRoot 'site')
    Invoke-Checked 'npm.cmd' @('test') (Join-Path $sourceRoot 'site')
    Invoke-Checked 'npm.cmd' @('run', 'build') (Join-Path $sourceRoot 'site')

    New-Item -ItemType Directory -Path (Join-Path $packageRoot 'site'), (Join-Path $packageRoot 'deploy') | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'site\dist') -Destination (Join-Path $packageRoot 'site\dist') -Recurse
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'site\server') -Destination (Join-Path $packageRoot 'site\server') -Recurse
    Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'deploy') -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $packageRoot 'deploy')
    }

    $nodeVersion = (& node --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch '^v(?:2[2-9]|[3-9][0-9])\.') {
        throw "Unsupported build Node version: $nodeVersion"
    }
    $manifest = @(
        'schema=actiontag-linux-release-v1',
        "release=$release",
        "commit=$commit",
        "node=$nodeVersion",
        'platform=linux-x64',
        "public_url=$PublicUrl"
    ) -join "`n"
    [System.IO.File]::WriteAllText((Join-Path $packageRoot 'RELEASE-MANIFEST'), "$manifest`n", [Text.UTF8Encoding]::new($false))

    Invoke-Checked 'tar.exe' @('-czf', $artifact, '-C', $packageRoot, '.') $repoRoot
    $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText($checksum, "$hash  $([IO.Path]::GetFileName($artifact))", [Text.Encoding]::ASCII)
    [pscustomobject]@{ Release = $release; Commit = $commit; Artifact = $artifact; Sha256 = $hash } | Format-List
} finally {
    $resolvedTemp = [System.IO.Path]::GetFullPath($tempRoot)
    if (-not $resolvedTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedTemp) -notmatch '^actiontag-build-[0-9a-f]{32}$') {
        throw "Refusing to clean an unexpected temporary path: $resolvedTemp"
    }
    if (Test-Path -LiteralPath $resolvedTemp) { Remove-Item -LiteralPath $resolvedTemp -Recurse -Force }
}
