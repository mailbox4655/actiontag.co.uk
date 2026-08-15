[CmdletBinding()]
param(
    [string]$EnvironmentFile,
    [ValidateSet('All', 'Hetzner', 'Cloudflare')][string]$Target = 'All',
    [ValidateSet('Connect', 'Validate')][string]$Mode = 'Connect',
    [string]$DeploymentHostname,
    [ValidateRange(0, 65535)][int]$ApplicationPort = 0,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Fail([string]$Message) { throw $Message }

function Resolve-EnvironmentFile([string]$Requested) {
    $path = if ($Requested) { $Requested } else { Join-Path $PSScriptRoot '.env.infrastructure.local' }
    $resolved = [System.IO.Path]::GetFullPath($path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        Fail "Infrastructure environment file does not exist: $resolved"
    }
    return $resolved
}

function Read-StrictDotEnv([string]$Path) {
    $values = @{}
    $lineNumber = 0
    foreach ($rawLine in [System.IO.File]::ReadAllLines($Path)) {
        $lineNumber += 1
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line.StartsWith('export ')) { Fail "$Path line ${lineNumber}: export syntax is not accepted." }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) { Fail "$Path line ${lineNumber}: expected KEY=VALUE." }
        $key = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if ($key -notmatch '^[A-Z][A-Z0-9_]*$') { Fail "$Path line ${lineNumber}: invalid variable '$key'." }
        if ($values.ContainsKey($key)) { Fail "$Path line ${lineNumber}: duplicate variable '$key'." }
        if ($value.Length -ge 2) {
            $first = $value[0]
            $last = $value[$value.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $values[$key] = $value
    }
    return $values
}

function Require-Value([hashtable]$Values, [string]$Name) {
    if (-not $Values.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace([string]$Values[$Name])) {
        Fail "Infrastructure environment is missing required variable $Name."
    }
    $value = [string]$Values[$Name]
    if ($value -match '^(SPECIMEN_|CHANGEME|REPLACE_ME|<.+>|your[-_])') {
        Fail "Infrastructure variable $Name still contains a marked placeholder."
    }
    return $value
}

function Get-HetznerConfiguration([hashtable]$Values) {
    $hostName = Require-Value $Values 'HETZNER_SSH_HOST'
    $userName = Require-Value $Values 'HETZNER_SSH_USER'
    $portText = Require-Value $Values 'HETZNER_SSH_PORT'
    $keyText = Require-Value $Values 'HETZNER_SSH_KEY_PATH'
    if ($hostName -notmatch '^[A-Za-z0-9.:-]+$') { Fail "Invalid HETZNER_SSH_HOST: $hostName" }
    if ($userName -notmatch '^[A-Za-z_][A-Za-z0-9_-]*$') { Fail "Invalid HETZNER_SSH_USER: $userName" }
    $port = 0
    if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        Fail "HETZNER_SSH_PORT must be from 1 through 65535; received '$portText'."
    }
    if ($keyText.StartsWith('~')) { Fail 'HETZNER_SSH_KEY_PATH must be absolute.' }
    $keyPath = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($keyText))
    if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) { Fail "SSH key does not exist: $keyPath" }
    $knownText = if ($Values.ContainsKey('HETZNER_SSH_KNOWN_HOSTS_PATH')) {
        [string]$Values['HETZNER_SSH_KNOWN_HOSTS_PATH']
    } else {
        Join-Path ([Environment]::GetFolderPath('UserProfile')) '.ssh\known_hosts'
    }
    $knownPath = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($knownText))
    if (-not (Test-Path -LiteralPath $knownPath -PathType Leaf)) { Fail "Known-hosts file does not exist: $knownPath" }
    return @{ Host = $hostName; User = $userName; Port = $port; Key = $keyPath; Known = $knownPath }
}

function Get-CloudflareConfiguration([hashtable]$Values, [string]$RequestedHostname) {
    $token = if ($env:CLOUDFLARE_API_TOKEN) { $env:CLOUDFLARE_API_TOKEN } else { Require-Value $Values 'CLOUDFLARE_API_TOKEN' }
    $accountId = Require-Value $Values 'CLOUDFLARE_ACCOUNT_ID'
    if ($accountId -notmatch '^[0-9a-f]{32}$') { Fail 'CLOUDFLARE_ACCOUNT_ID must be a 32-character hexadecimal identifier.' }
    $zone = (Require-Value $Values 'CLOUDFLARE_ZONE_NAME').ToLowerInvariant()
    $hostname = if ($RequestedHostname) { $RequestedHostname.ToLowerInvariant() } else {
        (Require-Value $Values 'CLOUDFLARE_DEPLOYMENT_HOSTNAME').ToLowerInvariant()
    }
    $expectedStatus = (Require-Value $Values 'CLOUDFLARE_EXPECTED_ZONE_STATUS').ToLowerInvariant()
    if ($expectedStatus -notin @('pending', 'active')) {
        Fail "CLOUDFLARE_EXPECTED_ZONE_STATUS must be pending or active; received '$expectedStatus'."
    }
    $pattern = '^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'
    if ($zone -notmatch $pattern -or $hostname -notmatch $pattern) { Fail 'Cloudflare zone or hostname is invalid.' }
    if ($hostname -ne $zone -and -not $hostname.EndsWith(".$zone")) {
        Fail "Cloudflare hostname '$hostname' is outside zone '$zone'."
    }
    return @{ Token = $token; AccountId = $accountId; Zone = $zone; Hostname = $hostname; ExpectedStatus = $expectedStatus }
}

function Invoke-HetznerPreflight([hashtable]$Configuration, [int]$RequestedPort) {
    $ssh = Get-Command ssh -ErrorAction SilentlyContinue
    if (-not $ssh) { Fail 'OpenSSH client command ssh is unavailable.' }
    $probe = if ($RequestedPort) {
        "if ! command -v ss >/dev/null 2>&1; then printf 'GDB_PORT_CHECK_UNAVAILABLE\n'; elif ss -H -ltn 'sport = :$RequestedPort' | grep -q .; then printf 'GDB_PORT_IN_USE\n'; else printf 'GDB_PORT_AVAILABLE\n'; fi"
    } else { "printf 'GDB_HETZNER_OK\n'" }
    $arguments = @(
        '-i', $Configuration.Key, '-p', [string]$Configuration.Port,
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'IdentitiesOnly=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$($Configuration.Known)",
        "$($Configuration.User)@$($Configuration.Host)", $probe
    )
    $output = (& $ssh.Source @arguments 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { Fail "Hetzner strict BatchMode SSH preflight failed: $output" }
    $expected = if ($RequestedPort) { 'GDB_PORT_AVAILABLE' } else { 'GDB_HETZNER_OK' }
    if ($output -eq 'GDB_PORT_IN_USE') { Fail "Hetzner application port $RequestedPort is already listening." }
    if ($output -eq 'GDB_PORT_CHECK_UNAVAILABLE') { Fail "Hetzner cannot inspect port $RequestedPort because ss is unavailable." }
    if ($output -ne $expected) { Fail "Hetzner returned unexpected probe response '$output'." }
    return @{ status = 'pass'; endpoint = "$($Configuration.User)@$($Configuration.Host):$($Configuration.Port)"; application_port = $RequestedPort }
}

function Invoke-CloudflareGet([string]$Path, [string]$Token, [string]$Purpose) {
    try {
        return Invoke-RestMethod -Method Get -Uri "https://api.cloudflare.com/client/v4$Path" `
            -Headers @{ Authorization = "Bearer $Token" } -TimeoutSec 20
    } catch {
        $status = if ($_.Exception.Response -and $_.Exception.Response.StatusCode) { [int]$_.Exception.Response.StatusCode } else { 'no HTTP status' }
        Fail "Cloudflare $Purpose request failed ($status) at API path $Path."
    }
}

function Normalize-DnsContent([string]$Type, [string]$Content) {
    if ($Type -in @('CNAME', 'MX')) { return $Content.TrimEnd('.').ToLowerInvariant() }
    if ($Type -eq 'TXT') {
        $text = $Content.Trim()
        if ($text.Length -ge 2 -and $text[0] -eq '"' -and $text[$text.Length - 1] -eq '"') { return $text.Substring(1, $text.Length - 2) }
        return $text
    }
    return $Content
}

function Get-AuthoritativeContent([string]$Server, [string]$Name, [string]$Type) {
    $rows = @(Resolve-DnsName -Name $Name -Type $Type -Server $Server -DnsOnly -ErrorAction Stop)
    return @($rows | Where-Object { $_.Type -eq $Type } | ForEach-Object {
        if ($Type -eq 'TXT') { $_.Strings -join '' }
        elseif ($Type -eq 'CNAME') { $_.NameHost.TrimEnd('.').ToLowerInvariant() }
        elseif ($Type -eq 'MX') { "$($_.Preference)|$($_.NameExchange.TrimEnd('.').ToLowerInvariant())" }
    })
}

function Assert-DnsInventory([hashtable]$Configuration, [string]$ZoneId, [object]$ZoneResult) {
    $baselinePath = Join-Path $PSScriptRoot 'deploy\dns-baseline.json'
    $baseline = Get-Content -LiteralPath $baselinePath -Raw | ConvertFrom-Json
    if ($baseline.zone -ne $Configuration.Zone -or @($baseline.records_to_preserve_exactly).Count -ne 10) {
        Fail 'The tracked DNS baseline is absent, incomplete or for a different zone.'
    }

    $records = @()
    $page = 1
    do {
        $response = Invoke-CloudflareGet "/zones/$ZoneId/dns_records?page=$page&per_page=100" $Configuration.Token 'DNS inventory'
        if (-not $response.success) { Fail 'Cloudflare DNS inventory request was unsuccessful.' }
        $records += @($response.result)
        $totalPages = [Math]::Max(1, [int]$response.result_info.total_pages)
        $page += 1
    } while ($page -le $totalPages)

    foreach ($expected in @($baseline.records_to_preserve_exactly)) {
        $content = Normalize-DnsContent $expected.type $expected.content
        $matches = @($records | Where-Object {
            $_.type -eq $expected.type -and $_.name.ToLowerInvariant() -eq $expected.name.ToLowerInvariant() -and
            (Normalize-DnsContent $_.type ([string]$_.content)) -ceq $content
        })
        if ($matches.Count -ne 1 -or [bool]$matches[0].proxied) {
            Fail "Protected Cloudflare DNS record does not match exactly: $($expected.type) $($expected.name)."
        }
        if ($expected.type -eq 'MX' -and [int]$matches[0].priority -ne [int]$expected.priority) {
            Fail "Protected Cloudflare MX priority differs for $($expected.name)."
        }
    }

    foreach ($webName in @($Configuration.Zone, "www.$($Configuration.Zone)", "*.$($Configuration.Zone)")) {
        $matches = @($records | Where-Object { $_.type -eq 'CNAME' -and $_.name -eq $webName })
        if ($matches.Count -ne 1 -or -not [bool]$matches[0].proxied -or
            [string]$matches[0].content -notmatch '^[0-9a-f-]{36}\.cfargotunnel\.com$') {
            Fail "Web hostname '$webName' is not one proxied dedicated-tunnel CNAME."
        }
    }

    $nameServers = @($ZoneResult.name_servers)
    if ($nameServers.Count -ne 2) { Fail 'Cloudflare did not return exactly two assigned nameservers.' }
    foreach ($server in $nameServers) {
        foreach ($expected in @($baseline.records_to_preserve_exactly)) {
            $answer = if ($expected.type -eq 'MX') {
                "$($expected.priority)|$(Normalize-DnsContent 'MX' $expected.content)"
            } else { Normalize-DnsContent $expected.type $expected.content }
            if ($answer -cnotin @(Get-AuthoritativeContent $server $expected.name $expected.type)) {
                Fail "Assigned nameserver '$server' does not return the protected $($expected.type) record for $($expected.name)."
            }
        }
    }
    return @{ protected_record_count = 10; authoritative_nameservers = $nameServers; web_tunnel_record_count = 3 }
}

function Invoke-CloudflarePreflight([hashtable]$Configuration) {
    $verified = Invoke-CloudflareGet "/accounts/$($Configuration.AccountId)/tokens/verify" $Configuration.Token 'token verification'
    if (-not $verified.success -or $verified.result.status -ne 'active') { Fail 'Cloudflare token did not verify as active.' }
    $zoneName = [Uri]::EscapeDataString($Configuration.Zone)
    $zones = Invoke-CloudflareGet "/zones?name=$zoneName&page=1&per_page=50" $Configuration.Token 'zone lookup'
    $matches = @($zones.result | Where-Object { $_.name -eq $Configuration.Zone })
    if (-not $zones.success -or $matches.Count -ne 1) {
        Fail "Cloudflare expected exactly one '$($Configuration.Zone)' zone; found $($matches.Count)."
    }
    if ([string]$matches[0].status -ne $Configuration.ExpectedStatus) {
        Fail "Cloudflare zone '$($Configuration.Zone)' status is '$($matches[0].status)', expected '$($Configuration.ExpectedStatus)'."
    }
    $zoneId = [string]$matches[0].id
    $recordName = [Uri]::EscapeDataString($Configuration.Hostname)
    $records = Invoke-CloudflareGet "/zones/$zoneId/dns_records?name.exact=$recordName&page=1&per_page=100" $Configuration.Token 'exact DNS lookup'
    if (-not $records.success) { Fail "Cloudflare DNS lookup failed for '$($Configuration.Hostname)'." }
    $inventory = Assert-DnsInventory $Configuration $zoneId $matches[0]
    return @{
        status = 'pass'; zone = $Configuration.Zone; zone_status = [string]$matches[0].status
        hostname = $Configuration.Hostname; existing_record_count = @($records.result).Count
        protected_record_count = $inventory.protected_record_count
        web_tunnel_record_count = $inventory.web_tunnel_record_count
        authoritative_nameservers = $inventory.authoritative_nameservers
        proof = 'Token is active and exact zone/DNS reads succeeded.'; dns_write = 'unproved-by-read-only-preflight'
    }
}

$resolvedEnvironment = Resolve-EnvironmentFile $EnvironmentFile
$environment = Read-StrictDotEnv $resolvedEnvironment
$result = [ordered]@{ schema = 'gpt-design-bridge/infrastructure-preflight/v1'; mode = $Mode.ToLowerInvariant(); environment_file = $resolvedEnvironment }
if ($Target -in @('All', 'Hetzner')) {
    $configuration = Get-HetznerConfiguration $environment
    $result['hetzner'] = if ($Mode -eq 'Connect') { Invoke-HetznerPreflight $configuration $ApplicationPort } else {
        @{ status = 'validated'; endpoint = "$($configuration.User)@$($configuration.Host):$($configuration.Port)" }
    }
}
if ($Target -in @('All', 'Cloudflare')) {
    $configuration = Get-CloudflareConfiguration $environment $DeploymentHostname
    $result['cloudflare'] = if ($Mode -eq 'Connect') { Invoke-CloudflarePreflight $configuration } else {
        @{ status = 'validated'; zone = $configuration.Zone; zone_status = $configuration.ExpectedStatus; hostname = $configuration.Hostname; dns_write = 'unproved-by-read-only-preflight' }
    }
}

if ($Json) {
    $result | ConvertTo-Json -Depth 5
} else {
    Write-Host "Infrastructure preflight PASS ($($Mode.ToLowerInvariant()))"
    if ($result.Contains('hetzner')) { Write-Host "  Hetzner: $($result['hetzner'].status) $($result['hetzner'].endpoint)" }
    if ($result.Contains('cloudflare')) {
        Write-Host "  Cloudflare: $($result['cloudflare'].status) $($result['cloudflare'].hostname) ($($result['cloudflare'].zone_status))"
        Write-Host '  DNS Write: not claimed by this read-only preflight.'
    }
}
