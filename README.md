# github-backup

[![CI](https://github.com/drhdev/github-backup/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/drhdev/github-backup/actions/workflows/ci.yml)
[![Security](https://github.com/drhdev/github-backup/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/drhdev/github-backup/actions/workflows/security.yml)

Back up the Git code in your personal GitHub accounts and organizations to DigitalOcean
Spaces, so you can recover when GitHub is unavailable or an account is compromised.
**The default installation is Docker Compose deployed through Coolify on an Ubuntu 24.04 LTS
or newer supported Ubuntu server with an x86_64 (Intel/AMD 64-bit) CPU.**
Compose explicitly builds and runs `linux/amd64`; no Raspberry Pi OS or ARM hardware is required.

Connect one or more GitHub Apps to personal accounts and organizations and choose **all**,
**selected**, or **all except selected** repositories per installation. All modes that cover
future repositories discover them automatically. The worker checks hourly and uploads a new
self-contained backup only when Git refs change. It never runs the backed-up code.

## What you get by default

- Private backups under a configurable `github-backup/` folder in an existing shared Spaces bucket.
- Latest 3 versions + up to 2 weekly and 2 monthly checkpoints + oldest baseline: at most 8 distinct
  code versions per active repository after retention, often fewer because selections overlap.
- Indefinite preservation of missing/deleted repositories; disappearance alerts instead of deletion.
- A 100 GiB per-prefix upload budget that preserves existing backups when space is insufficient.
- One worker, single-threaded Git packing/uploads, 0.5 CPU and 512 MiB container limits.
- Persistent Git cache, no overlapping scheduled jobs, retries, verified local bundles, and catalog checks.
- JSON logs on stdout/stderr, Docker health checks, bounded logs, and optional Uptime Kuma Push monitoring.

The budget needs space for a new backup before pruning. Large repositories can require more
memory/disk. The defaults cover Git history and refs, **not Git LFS payloads**, release assets,
issues, Actions artifacts, secrets, or submodule contents from other repositories. See
[coverage and recovery](docs/COOLIFY.md#coverage-and-incident-recovery).

## Install with Coolify

### 1. Deploy in Coolify

1. Add `https://github.com/drhdev/github-backup` using the **Docker Compose** build strategy.
   Set Base Directory to `/` and Docker Compose Location to `/compose.yaml`.
2. Keep one replica of each service and the persistent `backup-cache` volume.
3. Assign an **HTTPS domain to `github-setup`, target port 8080**. If Coolify encodes
   the target port in its domain field, use `https://github-backup.example.com:8080`.
   The external URL remains `https://github-backup.example.com` on port 443.
   Leave `github-backup` without a domain or published port.
4. **Disable automatic deployments, previews, and overlapping/rolling replacements.**
5. Set just these two required environment variables, keep the password masked, and deploy:

```dotenv
GITHUB_SETUP_URL=https://github-backup.example.com
GITHUB_SETUP_PASSWORD=REPLACE_WITH_OUTPUT_OF_OPENSSL_RAND_HEX_32
```

Generate the password with `openssl rand -hex 32`. The URL must be the exact external HTTPS
origin without a path or internal `:8080` suffix. Enable Buildtime and Runtime for supplied
values in Coolify. No Spaces credentials, bucket settings, GitHub App IDs or PEMs need to
be entered in the environment. See [the deployment guide](docs/COOLIFY.md).

### 2. Configure DigitalOcean Spaces in the setup page

Open the setup URL and log in as **admin** with your setup password. Enter:

| Setup field | Where to get it |
|---|---|
| Bucket name | DigitalOcean **Spaces Object Storage → Buckets**; use a shared unversioned bucket or create one |
| Region | Bucket origin hostname: `company-files.ams3.digitaloceanspaces.com` means `ams3` |
| Root folder | Choose an exclusive prefix; default `github-backup` |
| Access key ID and secret | DigitalOcean **Spaces Object Storage → Access Keys → Create Access Key**; use the Spaces key pair, not a personal API token |

Keep the backup prefix private. The Spaces key needs bucket listing and versioning-status
read access, plus object read/write/delete permissions. Click **Test and save Spaces**.
The check writes, reads back and deletes a random temporary object inside your prefix.
Credentials are saved privately on the persistent volume and are never shown in the page.

### 3. Connect GitHub Apps and choose repositories

Click **Connect GitHub / create another App**, then approve creation on GitHub. Use the
installation link to install it on a personal account or organization. GitHub lets you grant
**All repositories** or **Only select repositories**. Return to setup, enter the account's
login and click **Verify and connect account**, then **Choose repositories / pause backups**.
New connections start paused until you choose and save a scope:

| Backup scope | Behavior |
|---|---|
| All | Back up every repository, including future ones; requires All repositories on GitHub |
| Only selected | Back up checked repositories; supports GitHub selected-repository installations |
| All except selected | Back up everything except checked repositories, including future ones; requires All repositories on GitHub |
| Pause | Create no backups for this account; preserve existing copies |

Repeat **create another App** for additional GitHub accounts or organizations, switching
GitHub logins when necessary. You can register an App under an organization by entering
its login in the optional creation field. One App may also have several installations;
multiple Apps can cover the same account, with the union of their selections backed up once.
Organization approval may be required. Every App requests read-only repository Contents
with automatic metadata access. No PEM download, Base64 conversion or personal token is needed.

Once Spaces and at least one installation are configured, the scheduler starts within
30 seconds. Changes apply on the next scheduled run without restarting containers. Excluded
repositories keep all existing backups without pruning; selected repositories that become
inaccessible still alert. Selections use repository IDs, so renames preserve the selection.
See [GitHub setup and recovery](docs/GITHUB_APP.md).

Optional scheduling, capacity, retention and monitoring settings retain the defaults in
[.env.example](.env.example). Existing manual environment deployments remain supported;
saved browser Spaces settings take precedence over their legacy Spaces variables.

### 4. Verify the first backup

Open the Coolify logs. A successful run emits `backup_finished` with `exit_code: 0`.
Check `github-backup/_system/manifests/latest.json` in Spaces: expected selected repositories
must be present, `missing_resources` and `discovery_errors` empty, and failure/partial counts zero.
`excluded_resources` lists previously archived repositories intentionally excluded or paused.
An unchanged repository is a successful check without a duplicate bundle. An explicitly
paused/empty selection produces no code backups, even if the check succeeds.

Download a bundle and its catalog, verify SHA-256, then test cloning and `git fsck --full`.
**A green container alone does not prove recovery works.** See [restore verification](docs/COOLIFY.md#validate-recovery).

## Storage layout and retention

```text
company-files/                         # shared bucket
├── invoices/                          # another application: untouched
└── github-backup/                      # SPACES_PREFIX
    ├── alice/website/20261107153000/
    │   └── repository-123-<unique-id>.bundle
    ├── example-team/api/20261107154500/
    │   └── repository-456-<unique-id>.bundle
    └── _system/                       # catalogs and run manifests
```

Timestamp folders use UTC `YYYYMMDDHHMMSS`. Repository IDs protect history across renames
and deletion/recreation; previous backups remain referenced even when the name changes.
Retention selects the newest versions plus checkpoints from the previous completed UTC
weeks/months and the oldest baseline. It does not synthesize duplicate copies for inactive periods.
Missing repositories are preserved until you explicitly review and purge them administratively.

The budget counts only live objects inside `SPACES_PREFIX/`, not other bucket folders. When
it cannot fit a new artifact, it fails visibly rather than delete protected recovery points.
[Retention, shared-bucket rules, and migration](docs/COOLIFY.md#retention-and-capacity).

## Docker logs and monitoring

Application diagnostics are JSON on **stderr**; scheduler/run events are JSON on **stdout**.
Docker and Coolify collect both streams. No application log files or log-volume mounts are required.
Compose rotates Docker logs at **10 MB × 3 files**. Locally:

```bash
docker compose logs --follow --tail=100 github-backup
```

Compose checks scheduler health every minute. Failed/partial runs, unacknowledged missing
repositories, or stale checks become unhealthy. Docker's restart policy does not restart a
container solely for being unhealthy; configure notifications and inspect the logs.

**Optional Uptime Kuma:** create a **Push** monitor with a **180-second heartbeat interval**,
**0 retries** and your notification channel. Copy its generated Push URL into:

```dotenv
UPTIME_KUMA_PUSH_URL=https://kuma.example.com/api/push/REPLACE_WITH_PUSH_TOKEN
```

The worker pushes at most every 60 seconds, plus status changes. It reports failure or
overdue successful checks even while the container is running; if the worker disappears,
Kuma detects the missing heartbeat. The URL is secret. No Docker socket or inbound worker
port is required. See [the monitoring guide](docs/MONITORING.md) for startup behavior,
freshness thresholds, private-network HTTP, notification tests, and all optional variables.
Leave the URL empty to disable it completely.

## Documentation

- [Complete Coolify deployment and environment reference](docs/COOLIFY.md)
- [GitHub App creation and multi-account installation](docs/GITHUB_APP.md)
- [Docker logging and optional Uptime Kuma](docs/MONITORING.md)
- [CLI, personal tokens, filesystem/NAS, systemd, and alternative installs](docs/CLI.md)
- [Contributing](CONTRIBUTING.md), [releasing](docs/RELEASING.md), [support](SUPPORT.md), [security](SECURITY.md)

Keep Coolify and Spaces credentials independent of the accounts being backed up. This worker
has deletion rights for retention; maintain an independent offline/immutable copy if your threat
model also includes compromise of the backup host or Spaces credentials.

Licensed under [GPL-3.0-only](LICENSE).
