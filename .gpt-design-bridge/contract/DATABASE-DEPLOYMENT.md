# Database and native deployment policy

This policy chooses infrastructure from observable topology. It does not label SQLite
as a small-app database or PostgreSQL as a large-app database.

## 1. Database decision gate

Choose SQLite when all statements are true:

- one logical application owns the database;
- the application process and database file live on the same VPS/local filesystem;
- write transactions are short;
- simultaneous writers can wait their turn;
- horizontal application replicas are not planned;
- the database and its backup fit comfortably on one local volume;
- required operational features exist in SQLite.

Choose PostgreSQL before schema implementation when any statement is true:

- more than one host or independent service must access the same database;
- sustained simultaneous writes cannot queue;
- multiple application replicas are planned;
- database-level roles, centralized connection control, logical replication, or a
  PostgreSQL extension is required;
- the owner chooses PostgreSQL.

SQLite supports substantial data volumes. The practical boundary for these apps is
usually write concurrency and multi-host topology, not raw file size.

The decision is recorded in `project.json`. Changing engines later is a migration
project with its own export, validation, cutover, rollback, and reconciliation plan.
It is not a connection-string edit.

## 2. SQLite production rules

- The database file, WAL/journal files, and application process stay on one host and
  one local filesystem. Never use NFS, SMB, a shared volume, or object storage as the
  live database.
- One application process owns writes by default.
- Foreign keys are enabled.
- Double-quoted string literals and extension loading are disabled.
- Defensive mode is enabled when the driver supports it.
- A finite busy timeout is explicit; a lock timeout becomes a named error.
- Every write transaction is short. Network calls and mail sends happen outside it.
- Schema changes are forward migrations with a recorded schema version.
- The default journal mode is `DELETE`.

WAL is an optional, separately approved optimization. Before enabling it:

1. Query and record the linked SQLite version at runtime.
2. Verify that version is outside all known affected ranges in the current SQLite
   release notes.
3. Prove every process is on the same host.
4. Prove the backup method captures a consistent database, not a naked main file.
5. Exercise lock contention, checkpoint behavior, crash recovery, and restore.
6. Record the owner approval and the rollback to `DELETE`.

This gate is deliberate. SQLite documented a rare WAL-reset corruption bug affecting
versions through 3.51.2 under multiple connections writing/checkpointing
concurrently; fixed releases include 3.51.3 and listed backports. A version number
check is necessary but not sufficient because distributions can backport patches.

Official references:

- <https://www.sqlite.org/whentouse.html>
- <https://www.sqlite.org/wal.html>
- <https://www.sqlite.org/limits.html>

## 3. SQLite backup and restore

Do not copy a live SQLite main file as though it were an inert document.

The backup job uses the driver's SQLite backup API or an equivalent online backup
operation. Each backup:

- writes to a new file;
- runs `PRAGMA integrity_check` on the result;
- records schema version, byte count, SHA-256, and application release;
- encrypts before off-box transfer when the data requires it;
- retains according to the project schedule;
- produces a non-zero exit on any incomplete step.

A backup claim is incomplete until restore is tested into a separate directory,
migrations are applied, the app boots against the restored copy, and representative
counts/invariants match. Restore tests never target the live data directory.

Uploads or other durable files are backed up as a second explicit set. A database
backup does not imply uploaded files were protected.

## 4. PostgreSQL production rules

- Use one supported PostgreSQL major selected for the project and pin it in operations
  documentation.
- Use a dedicated database and least-privilege application role.
- Migrations are forward, transactional where PostgreSQL permits, and applied by one
  release actor.
- Connection pooling is explicit and bounded.
- TLS and authentication are required for any non-loopback connection.
- Backup uses a tested PostgreSQL-aware method plus off-box retention.
- Restore proof includes roles/extensions, migrations, representative invariants, and
  application boot.

The travelling drop does not run PostgreSQL. It uses the same route shapes through its
in-page fixture socket.

## 5. Native Hetzner topology

Docker and container-compose files are prohibited.

The default production topology is:

```text
Cloudflare DNS/proxy
        |
Existing Caddy or Nginx
        |
127.0.0.1:<application port>
        |
Node application managed by systemd
        |
SQLite on local disk OR PostgreSQL
```

Required separation:

- application releases: `/opt/<app>/releases/<release-id>/`;
- stable release link: `/opt/<app>/current`;
- durable data: `/var/lib/<app>/`;
- environment/secrets: `/etc/<app>/<app>.env`;
- logs: system journal plus any explicitly configured structured sink;
- backups: staging outside the live data directory, then off-box.

The service runs as a dedicated unprivileged account. Its environment file is not in
Git and is readable only by the service administrator/account. The Node process binds
loopback; the host's explicitly selected existing Caddy or Nginx service is the public
door. Never activate a second competing proxy.

## 6. Cloudflare

Cloudflare owns DNS and optional proxy/WAF behavior. Application source does not carry
Cloudflare API tokens.

Before cutover record:

- hostname and record type;
- origin address;
- proxy status;
- TLS mode;
- caching exclusions for authenticated/API paths;
- WebSocket requirement if any;
- rollback DNS value and expected propagation behavior.

Do not claim Cloudflare is configured from the existence of a hostname in source.
Query or observe the real DNS/proxy state during deployment proof.

### 6.1 Access preflight and permission evidence

Keep workstation access values in the Git-ignored `.env.infrastructure.local`.
Before creating a deployment seed or changing external state, run:

```powershell
.\Test-GPT-Design-Bridge-Infrastructure.ps1 -Mode Connect -Target All
```

The preflight must:

- use non-interactive SSH, the configured private-key path, strict host-key checking,
  and a read-only remote probe;
- verify that the Cloudflare API token is active;
- resolve exactly one active configured zone;
- query the exact deployment hostname without changing it;
- never print, export, or persist the token in an evidence artifact;
- fail with the exact missing variable, local path, endpoint, zone, or hostname.

An active token and successful GET requests prove connectivity and the exercised read
permissions. They do not prove DNS Write. Seal DNS Write only when the intended record
create/update succeeds; first record the prior record value, and retain the response
plus the literal rollback operation as deployment evidence. Do not create a disposable
DNS record merely to make a preflight claim.

## 7. Postmark

Postmark sends system email. The server token, sender signature/domain, and message
stream are environment configuration, never designer-drop content.

At boot:

- if mail is required and configuration is missing, fail loudly;
- if a feature can operate without mail, that mode is explicit in project config and
  visible to operators;
- never log a token or full sensitive message body.

Each send records a correlation identifier and Postmark response identifier. A UI
claims `sent` only after the accepted server response. Test and production streams are
separate where the account supports them.

## 8. systemd and the selected reverse proxy

The generated systemd unit must:

- set the working directory to the stable current release;
- load the protected environment file;
- bind the expected user/group;
- restart on unexpected failure with a bounded delay;
- expose a clear startup failure in the journal;
- stop cleanly before timeout;
- apply hardening compatible with the app's required writable paths.

The selected existing Caddy or Nginx service must:

- terminate TLS or follow the explicitly chosen Cloudflare-origin TLS model;
- proxy only to the loopback application port;
- preserve the real client/proxy headers needed by the app;
- serve correct content types;
- avoid caching personalized/API responses;
- expose a health route without exposing internals.

Before mutation, record which service owns ports 80/443, its current vhosts, and
owner-approved neighboring sites. Add only the exact application hostname, validate
before reload, preserve a rollback copy, and recheck the neighbors afterward.

## 9. Release, rollback, and proof

A native release is:

1. preflight host capacity, supported Node LTS, database selection, and secrets;
2. prove current backup and restore path;
3. stage a versioned release;
4. install locked dependencies and run build/checks in the staged release;
5. run forward migrations once;
6. switch the stable link;
7. restart systemd and check its journal;
8. probe the loopback health endpoint;
9. retry direct-origin HTTPS and the public Cloudflare hostname through the selected
   proxy, reporting progress rather than treating the first post-reload 404 as final;
10. exercise system mail when it is in scope;
11. walk the production UI in visible independent Chrome/Chromium;
12. record release, migration, backup, hashes, and observations.

Rollback switches to a known compatible release. A release that applied an
irreversible schema migration requires a forward-repair plan; pretending an old
binary can read a new schema is not rollback.

## 10. Reconstructability and cleanup

A release source must be clean, committed, and mechanically proven reachable from the
configured reconstruction remote ref before production activation. “There is a Git
repository” is not sufficient: an unpushed commit may be the only copy capable of
rebuilding the release.

Every server release has a marker binding its source commit and reconstruction ref.
After the new release passes health, retain `current` and the immediately previous
proved release. Only older, marked, non-linked releases whose source commit still
exists and is reachable from that remote ref are reproducible cleanup candidates.
Retain an unmarked or ambiguous path and name why. Never classify persistent database
files, uploads, environment/secrets, backups, designer-return archives, travelling
database escrow, adoption baselines, or required evidence as build debris.

The deployment health and doctor loops surface elapsed time and last-known phase.
They have no implicit overall deadline; an operator may select a release-specific
deadline through the documented environment setting. Per-request connection limits
remain explicit network diagnostics. When a deadline fires, retain logs and report
the exact last state rather than rerunning or weakening the check.
