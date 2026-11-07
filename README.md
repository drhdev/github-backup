# github-backup

[![CI](https://github.com/drhdev/github-backup/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/drhdev/github-backup/actions/workflows/ci.yml)
[![Security](https://github.com/drhdev/github-backup/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/drhdev/github-backup/actions/workflows/security.yml)

Back up the Git code in your personal GitHub accounts and organizations to DigitalOcean
Spaces, so you can recover when GitHub is unavailable or an account is compromised.
**The default installation is Docker Compose deployed through Coolify on an Ubuntu 24.04 LTS
or newer supported Ubuntu server with an x86_64 (Intel/AMD 64-bit) CPU.**
Compose explicitly builds and runs `linux/amd64`; no Raspberry Pi OS or ARM hardware is required.

Install one GitHub App with **All repositories** on each account and organization. New
repositories are discovered automatically. The worker checks hourly and uploads a new
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

### 1. Create and install your GitHub App

Follow [GitHub App setup](docs/GITHUB_APP.md): grant repository **Contents: Read-only**
(Metadata read is automatic), disable webhooks, and install it with **All repositories**
on each user and organization to back up. Record its **App ID** and download a private-key PEM.
A personal access token is not required. Being a member of an organization is insufficient:
the App must be installed on that organization with its approval.

Encode the key as a single line on your own computer:

```bash
base64 < app-private-key.pem | tr -d '\n'
```

Treat this output as a secret; base64 is not encryption.

### 2. Prepare DigitalOcean Spaces

Use an existing shared **unversioned** bucket or create one. Keep the backup prefix private.
Create a Spaces key that can list, read, write, and delete this backup data, and read bucket
versioning status. The worker does not change bucket policies or other applications' files.
Record the bucket name and its region, e.g. `fra1` (Frankfurt) or `ams3` (Amsterdam).

### 3. Configure the Coolify application

1. In the target Coolify project/environment, add a resource from this Git repository.
   For public source, use `https://github.com/drhdev/github-backup`.
2. Choose the **Docker Compose** build pack / Compose build strategy. Set Base Directory to `/`
   and Docker Compose Location to `/compose.yaml` (the file at the repository root).
3. Use a reviewed source revision containing this feature. The Compose file builds the image;
   old published images do not contain this scheduler. Keep **one replica** and the named cache volume.
4. Leave domain/port settings empty: this is a background worker with no inbound HTTP listener.
5. **Disable automatic deployments, preview deployments, and overlapping/rolling replacements.**
   A compromised GitHub account must not be able to deploy replacement worker code with Spaces secrets.
6. In **Environment Variables**, set the six required values below in the production environment.
   For the current Compose file, enable **Buildtime and Runtime** on all six required values
   so Coolify can interpolate Compose during its build command. Keep secret values masked. See the [complete deployment guide](docs/COOLIFY.md) for UI details.
7. Save and deploy. Do not add a Coolify scheduled task: the container includes its own scheduler.

### 4. Set the environment

These six values must be supplied; placeholders will not work:

```dotenv
GITHUB_APP_ID=123456
GITHUB_APP_OWNERS=alice,example-team,alice-work
GITHUB_APP_PRIVATE_KEY_BASE64=REPLACE_WITH_SINGLE_LINE_BASE64_PEM
AWS_ACCESS_KEY_ID=REPLACE_WITH_SPACES_KEY
AWS_SECRET_ACCESS_KEY=REPLACE_WITH_SPACES_SECRET
SPACES_BUCKET=company-files
```

`GITHUB_APP_OWNERS` is a comma-separated list of accounts/organizations where the App is installed.
The Spaces credentials are a Spaces access-key pair, not a DigitalOcean API token.
`SPACES_BUCKET` is just the bucket name, without a URL or folder.

### Where to find each required value

| Environment variable | Value to enter | Where to find it |
|---|---|---|
| `GITHUB_APP_ID` | Numeric App ID | GitHub **Settings → Developer settings → GitHub Apps → your App → General**; copy **App ID**, not Client ID or installation ID |
| `GITHUB_APP_OWNERS` | User/org login names, comma-separated | The account and organization names in GitHub URLs, e.g. `github.com/alice` and `github.com/example-team`; install the App on each first |
| `GITHUB_APP_PRIVATE_KEY_BASE64` | Single-line base64 of your downloaded PEM | Your App's **General → Private keys → Generate a private key**, then run the base64 command above locally |
| `AWS_ACCESS_KEY_ID` | **DigitalOcean Spaces access key ID** | DigitalOcean **Spaces Object Storage → Access Keys → Create Access Key** |
| `AWS_SECRET_ACCESS_KEY` | **Secret belonging to that same Spaces key** | Copy the secret when creating the Spaces key; if lost, create a replacement pair and update both values |
| `SPACES_BUCKET` | Existing bucket name, e.g. `company-files` | DigitalOcean **Spaces Object Storage → Buckets**; open the destination bucket |

**Why the `AWS_` names?** Spaces implements the S3 API, and this project uses boto3's standard
credential environment variables. Put **DigitalOcean** credentials in them: no AWS account,
AWS IAM key, or AWS bill is involved. `SPACES_BUCKET` and `SPACES_REGION` configure this
application; they are not credential-provider variable names. Keep the exact names shown—
renaming them to `SPACES_KEY`/`SPACES_SECRET` is not supported by this configuration.
See [DigitalOcean's S3 SDK guide](https://docs.digitalocean.com/products/spaces/reference/aws-sdks/)
and [Spaces key creation](https://docs.digitalocean.com/products/spaces/how-to/manage-access/).

Use a separate Spaces key for this worker with access to the intended bucket and the required
read/write/delete operations. Do not paste a DigitalOcean personal API token, the key's display
name, an endpoint URL, or the GitHub App key into either Spaces credential field.
For example, the bucket origin `company-files.ams3.digitaloceanspaces.com` means
`SPACES_BUCKET=company-files` and `SPACES_REGION=ams3`. Keep `SPACES_PREFIX=github-backup`
unless you want a different folder; that prefix is your choice, not a secret to obtain elsewhere.

Optional values already have working defaults. **Change the region if your bucket is not in Frankfurt:**

```dotenv
SPACES_REGION=fra1
SPACES_PREFIX=github-backup
BACKUP_INTERVAL_HOURS=1
BACKUP_RETENTION=3
BACKUP_KEEP_WEEKLY=2
BACKUP_KEEP_MONTHLY=2
BACKUP_KEEP_OLDEST=true
BACKUP_MAX_STORAGE_GIB=100
```

[`.env.example`](.env.example) lists **every configurable variable and its default**.
In Coolify, configure these values in the application; do not commit a real `.env` or PEM file.
For ordinary Docker Compose outside Coolify, copy `.env.example` to `.env`, fill the required
values, then run `docker compose up -d --build`.

### 5. Verify the first backup

Open the Coolify container logs. A completed healthy run emits `backup_finished` with
`exit_code: 0`. An unchanged repository is still a successful check and does not create a new bundle.

Check `github-backup/_system/manifests/latest.json` in Spaces: all expected repositories
should be represented, `missing_resources` empty, and failure/partial counts zero. Download
a bundle and its repository catalog, verify the SHA-256, then test cloning and `git fsck --full`.
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
