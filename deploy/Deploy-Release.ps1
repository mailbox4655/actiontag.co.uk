[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ArtifactPath,
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.infrastructure.local'),
    [string]$PublicUrl = 'https://actiontag.co.uk',
    [string]$VerificationUrl = 'https://actiontag.redevelopingdeveloper.uk',
    [switch]$DeleteLocalArtifact
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE." }
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($raw in [IO.File]::ReadAllLines($Path)) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) { throw "Invalid infrastructure environment line in $Path." }
        $values[$line.Substring(0, $separator).Trim()] = $line.Substring($separator + 1).Trim().Trim('"').Trim("'")
    }
    return $values
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$resolvedArtifact = (Resolve-Path -LiteralPath $ArtifactPath).Path
$artifactName = [IO.Path]::GetFileName($resolvedArtifact)
if ($artifactName -notmatch '^actiontag-([0-9a-f]{12})-linux-x64\.tar\.gz$') {
    throw 'Artifact name must be actiontag-<12-character-commit>-linux-x64.tar.gz.'
}
$release = $Matches[1]
$checksumPath = "$resolvedArtifact.sha256"
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) { throw "Missing checksum sidecar: $checksumPath" }
foreach ($url in @($PublicUrl, $VerificationUrl)) {
    if ($url -notmatch '^https://[A-Za-z0-9.-]+$') { throw "Invalid HTTPS origin: $url" }
}

$trackedStatus = @(git -C $repoRoot status --porcelain=v1 --untracked-files=no)
$currentCommit = (git -C $repoRoot rev-parse HEAD).Trim()
$remoteCommit = (git -C $repoRoot rev-parse origin/main).Trim()
if ($LASTEXITCODE -ne 0 -or $trackedStatus.Count -gt 0 -or $currentCommit -ne $remoteCommit -or $currentCommit.Substring(0, 12) -ne $release) {
    throw 'Deployment requires a clean tracked tree whose HEAD exactly matches both origin/main and the artifact release.'
}
$sidecar = (Get-Content -LiteralPath $checksumPath -Raw).Trim()
if ($sidecar -notmatch '^([0-9a-f]{64})  ([^\\/]+)$' -or $Matches[2] -ne $artifactName) {
    throw 'Checksum sidecar format or artifact name is invalid.'
}
$expectedHash = $Matches[1]
$actualHash = (Get-FileHash -LiteralPath $resolvedArtifact -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) { throw 'Artifact checksum does not match its sidecar.' }

$resolvedEnvironment = (Resolve-Path -LiteralPath $EnvironmentFile).Path
& (Join-Path $repoRoot 'Test-GPT-Design-Bridge-Infrastructure.ps1') -EnvironmentFile $resolvedEnvironment -Mode Connect -Target All
if ($LASTEXITCODE -ne 0) { throw 'Infrastructure preflight failed.' }
Invoke-Checked 'python' @('-X', 'utf8', (Join-Path $repoRoot '.gpt-design-bridge\tools\gpt_blackbox.py'), '--repo', $repoRoot, 'release-gate')

$environment = Read-DotEnv $resolvedEnvironment
foreach ($name in @('HETZNER_SSH_HOST', 'HETZNER_SSH_USER', 'HETZNER_SSH_PORT', 'HETZNER_SSH_KEY_PATH', 'HETZNER_SSH_KNOWN_HOSTS_PATH')) {
    if (-not $environment.ContainsKey($name) -or -not $environment[$name]) { throw "Missing infrastructure variable $name." }
}
$destination = "$($environment.HETZNER_SSH_USER)@$($environment.HETZNER_SSH_HOST)"
$sshOptions = @(
    '-i', [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($environment.HETZNER_SSH_KEY_PATH)),
    '-p', $environment.HETZNER_SSH_PORT,
    '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
    '-o', "UserKnownHostsFile=$([IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($environment.HETZNER_SSH_KNOWN_HOSTS_PATH)))"
)
$scpOptions = @(
    '-i', [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($environment.HETZNER_SSH_KEY_PATH)),
    '-P', $environment.HETZNER_SSH_PORT,
    '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
    '-o', "UserKnownHostsFile=$([IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($environment.HETZNER_SSH_KNOWN_HOSTS_PATH)))"
)
$uploadId = [guid]::NewGuid().ToString('N')
$remoteArtifact = "/var/tmp/actiontag-$release-$uploadId-linux-x64.tar.gz"
$remotePromoter = "/var/tmp/actiontag-promote-$release-$uploadId.sh"
$promoter = (Resolve-Path (Join-Path $PSScriptRoot 'promote-release.sh')).Path
$uploaded = $false

try {
    Invoke-Checked 'ssh' ($sshOptions + @($destination, "set -eu; command -v cloudflared >/dev/null; command -v nginx >/dev/null; test -x /opt/node24/bin/node; test -s /etc/cloudflared/actiontag-token; test ! -e '$remoteArtifact'; test ! -e '$remotePromoter'"))
    Invoke-Checked 'scp' ($scpOptions + @($resolvedArtifact, "${destination}:$remoteArtifact"))
    Invoke-Checked 'scp' ($scpOptions + @($promoter, "${destination}:$remotePromoter"))
    $uploaded = $true
    Invoke-Checked 'ssh' ($sshOptions + @($destination, "bash '$remotePromoter' '$release' '$remoteArtifact' '$expectedHash' '$PublicUrl' '$VerificationUrl'"))

    $health = Invoke-RestMethod -Uri "$VerificationUrl/healthz" -Method Get -TimeoutSec 20
    if ($health.status -ne 'healthy' -or $health.release -ne $release) {
        throw 'Public verification health did not report the promoted release.'
    }
    Invoke-Checked 'ssh' ($sshOptions + @($destination, "set -eu; test `"`$(readlink -f /opt/actiontag/current)`" = '/opt/actiontag/releases/$release'; systemctl is-active actiontag.service cloudflared-actiontag.service nginx; test ! -e '$remoteArtifact'; test ! -e '$remotePromoter'"))
    $uploaded = $false
} finally {
    if ($uploaded) {
        & ssh @sshOptions $destination "rm -f -- '$remoteArtifact' '$remotePromoter'" 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Remote upload cleanup failed.' }
    }
}

if ($DeleteLocalArtifact) {
    Remove-Item -LiteralPath $resolvedArtifact -Force
    Remove-Item -LiteralPath $checksumPath -Force
}
[pscustomobject]@{ Release = $release; PublicUrl = $PublicUrl; VerificationUrl = $VerificationUrl; LocalArtifactRetained = -not $DeleteLocalArtifact } | Format-List
