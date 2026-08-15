# ActionTag native production deployment

## Approved production topology

`actiontag.co.uk` is an Astro static site served by the Hetzner host's existing
Nginx. A dedicated unprivileged `actiontag` service handles only `/api/contact` and
`/healthz` on `127.0.0.1:3021`. A separate remotely managed Cloudflare Tunnel reaches
the Nginx virtual host. Docker is not used.

The contact service reuses the existing LaserTagSale Postmark server token, verified
sender identity and message stream, copied into `/etc/actiontag/actiontag.env`; it
does not read the LaserTagSale environment at runtime. Messages use
`sales@actiontag.co.uk` as `To`, `office@actiontag.co.uk` as `Cc`, and the visitor as
`Reply-To`. Tokens and message bodies never enter Git or deployment evidence.

Production locations:

- releases: `/opt/actiontag/releases/<12-character-commit>`;
- active link: `/opt/actiontag/current`;
- rollback link: `/opt/actiontag/previous`;
- secrets: `/etc/actiontag/actiontag.env`;
- tunnel credential: `/etc/cloudflared/actiontag-token`;
- health state: `/var/lib/actiontag/health-state.json`;
- deployment backups: `/var/backups/actiontag/`.

## Preflight

Copy `.env.infrastructure.example` to the ignored `.env.infrastructure.local`. Use a
Cloudflare token limited to this zone with only Zone Read and DNS Read. Validate and
then connect without printing the token:

```powershell
.\Test-GPT-Design-Bridge-Infrastructure.ps1 -Mode Validate -Target All -ApplicationPort 3021
.\Test-GPT-Design-Bridge-Infrastructure.ps1 -Mode Connect -Target All -ApplicationPort 3021
```

Before the registrar cutover, `CLOUDFLARE_EXPECTED_ZONE_STATUS=pending`. Change it to
`active` and repeat the preflight after Cloudflare activates the delegation.

## Release

Only a clean commit exactly matching `origin/main` can become an artifact. The build
uses `git archive`, locked dependencies, unit tests and the Astro production build;
the artifact contains no dependency tree, environment, database or old Joomla site.

```powershell
.\deploy\Build-Release.ps1
.\deploy\Deploy-Release.ps1 `
  -ArtifactPath .\out\releases\actiontag-<commit>-linux-x64.tar.gz `
  -VerificationUrl https://actiontag.redevelopingdeveloper.uk
```

The promoter checksum-verifies and path-validates the archive, records a checksum-
verified host/configuration backup, installs the release and units, validates Nginx,
then proves loopback, Nginx, preview, canonical metadata and the neighboring
LaserTagSale health endpoint. A failed activation restores the recorded Nginx,
systemd, environment and release-link state. Successful releases retain `current`
and `previous`; older releases are not deleted automatically.

## DNS and mail preservation

The canonical machine-readable pre-cutover inventory is `dns-baseline.json`. Before
the switch, also download **Save to file** from ADM.Tools into ignored deployment
evidence and hash it. Current rollback delegation and web origins are:

- `ns1.fastdns.hosting`, `ns2.fastdns.hosting`, `ns3.fastdns.hosting`;
- IPv4 `136.243.81.57`;
- IPv6 `2a01:4f8:212:2f08::1`;
- no parent DS/DNSSEC delegation.

In Cloudflare, replace only apex, `www` and wildcard A/AAAA web records with proxied
CNAMEs to the dedicated ActionTag tunnel. Preserve, DNS-only and byte-for-byte:

- MX `actiontag.co.uk` priority 0 to `mx.services`;
- `mail.actiontag.co.uk` CNAME to `mail.ukraine.com.ua`;
- `bounces.actiontag.co.uk` CNAME to `spgo.io`;
- the SPF record including SendPulse, Ukraine.com.ua, `185.104.44.58`, SparkPost and
  Mailgun;
- DMARC, Google verification and all `krs`, `scph0917`, `sign` and `hosting` DKIM
  selectors.

Never proxy mail, bounce, MX or TXT records. Query both assigned Cloudflare
nameservers directly and compare the exact record set before changing delegation.

## Cutover gate

Do not change nameservers until all of these pass:

1. BlackBox final and repository release gates on the exact commit.
2. Unit tests and Astro production build.
3. Strict BatchMode SSH and exact-zone/DNS read-only Cloudflare preflight.
4. Preview health reports the exact release.
5. Owned Chrome proves desktop/mobile layout, locale navigation, assets, legacy
   redirects and the contact-form success flow.
6. Up to two labelled contact tests are accepted by Postmark for both approved
   recipients; no message content or token appears in logs.
7. Both Cloudflare authoritative nameservers return the exact mail inventory.
8. Neighboring Hetzner services and LaserTagSale remain healthy.

Then replace the registrar nameservers with the exact pair assigned by the new
Cloudflare zone. Keep the old hosting untouched for at least 72 hours. After
activation, repeat production health, TLS, canonical, contact, DNS and preflight
proofs. Direct-origin TLS may be added through the existing Nginx/Certbot installation
after delegation is active; its Nginx server block includes the managed release
snippet and must not be replaced by later deployments.

## Rollback

Application rollback is independent from DNS rollback:

```bash
sudo /opt/actiontag/current/deploy/rollback-release.sh <previous-release>
```

For a cutover rollback, restore the three `fastdns.hosting` nameservers at ADM.Tools.
The old hosting remains available at the recorded A/AAAA origins during the rollback
window. Do not delete Cloudflare or old-provider records while delegation caches may
still contain either authority.
