import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const siteRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = path.resolve(siteRoot, '..');
const read = (relative) => readFile(path.join(repoRoot, relative), 'utf8');

test('Nginx preserves every legacy redirect and isolates the native API', async () => {
  const nginx = await read('deploy/actiontag-managed.nginx');
  for (const fragment of [
    '/en/price', '/en/rental-lasertag/our-rental-system', '/en/rental-lasertag/taggers',
    '/en/rental-lasertag/props', '/en/milsim-killhouse', '/en/soft-play',
    '/en/starting-your-business', '/en/home-lasertag', '/cz/',
  ]) {
    assert.ok(nginx.includes(fragment), `missing legacy redirect ${fragment}`);
  }
  assert.match(nginx, /proxy_pass http:\/\/127\.0\.0\.1:3021/);
  assert.match(nginx, /location = \/api\/contact/);
  assert.match(nginx, /return 301 https:\/\/actiontag\.co\.uk\$request_uri/);
  assert.match(nginx, /set_real_ip_from 127\.0\.0\.1/);
  assert.match(nginx, /proxy_set_header CF-Connecting-IP \$remote_addr/);
});

test('systemd units stay native, isolated and secret-free', async () => {
  const service = await read('deploy/actiontag.service');
  const tunnel = await read('deploy/cloudflared-actiontag.service');
  assert.match(service, /User=actiontag/);
  assert.match(service, /EnvironmentFile=\/etc\/actiontag\/actiontag\.env/);
  assert.match(service, /127\.0\.0\.1/);
  assert.match(tunnel, /LoadCredential=tunnel-token:\/etc\/cloudflared\/actiontag-token/);
  assert.doesNotMatch(`${service}\n${tunnel}`, /docker|POSTMARK_SERVER_TOKEN=/i);
});

test('DNS baseline retains all mail records and recorded rollback authority', async () => {
  const baseline = JSON.parse(await read('deploy/dns-baseline.json'));
  assert.deepEqual(baseline.nameservers, [
    'ns1.fastdns.hosting', 'ns2.fastdns.hosting', 'ns3.fastdns.hosting',
  ]);
  assert.equal(baseline.dnssec_parent_ds_present, false);
  assert.equal(baseline.web_records_to_replace.length, 6);
  assert.equal(baseline.records_to_preserve_exactly.length, 10);
  const names = new Set(baseline.records_to_preserve_exactly.map((record) => record.name));
  for (const name of [
    'mail.actiontag.co.uk', 'bounces.actiontag.co.uk', '_dmarc.actiontag.co.uk',
    'krs._domainkey.actiontag.co.uk', 'scph0917._domainkey.actiontag.co.uk',
    'sign._domainkey.actiontag.co.uk', 'hosting._domainkey.actiontag.co.uk',
  ]) assert.ok(names.has(name), `missing protected DNS name ${name}`);
  assert.ok(baseline.records_to_preserve_exactly.every((record) => record.proxied === false));
});

test('deployment scripts bind releases to pushed source and read-only preflight', async () => {
  const build = await read('deploy/Build-Release.ps1');
  const deploy = await read('deploy/Deploy-Release.ps1');
  const preflight = await read('Test-GPT-Design-Bridge-Infrastructure.ps1');
  const promoter = await read('deploy/promote-release.sh');
  assert.match(build, /HEAD must exactly match origin\/main/);
  assert.match(deploy, /release-gate/);
  assert.match(deploy, /\$scpOptions/);
  assert.match(deploy, /'-P', \$environment\.HETZNER_SSH_PORT/);
  assert.match(preflight, /dns_write = 'unproved-by-read-only-preflight'/);
  assert.doesNotMatch(preflight, /Method (Post|Put|Patch|Delete)/i);
  assert.match(preflight, /records_to_preserve_exactly/);
  assert.match(preflight, /authoritative_nameservers/);
  assert.match(preflight, /cfargotunnel\\\.com/);
  assert.match(promoter, /CONTACT_TO=sales@actiontag\.co\.uk/);
  assert.match(promoter, /CONTACT_CC=office@actiontag\.co\.uk/);
  assert.match(promoter, /\/etc\/lasertagsale\/lasertagsale\.env/);
  assert.doesNotMatch(promoter, /\. \/etc\/lasertagsale\/lasertagsale\.env/);
});
