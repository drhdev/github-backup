# Docker Compose on Coolify with DigitalOcean Spaces

This is the recommended deployment. A background worker backs up all configured GitHub
App installations to a private Spaces bucket. No domain, HTTP port, or public service
is needed. The existing CLI and filesystem backends remain available.

Protect Coolify and Spaces with credentials independent of the GitHub accounts being backed up.
**Disable automatic deployments from GitHub for this backup worker.** Deploy reviewed source
commits manually, or use a separately trusted image pinned by digest when available. A compromised
GitHub account must not be able to push replacement backup-worker code into Coolify and steal
its Spaces credentials. The worker never checks out or executes code from backed-up repositories.
For protection against compromise of the backup host/Spaces credentials as well, maintain an
independent offline or immutable copy; this deployment has delete permission for retention.

## Target server

Deploy on **Ubuntu 24.04 LTS or a newer supported Ubuntu server, x86_64 / AMD64** with
Docker Engine, the Docker Compose plugin, and Coolify configured on that destination.
The Compose service fixes both its build target and runtime platform to `linux/amd64`.
CI builds/scans that platform on Ubuntu 24.04 runners; releases publish the same x86_64 image.
Raspberry Pi OS and ARM64 are not the deployment targets of this stack.

Confirm the destination server before deployment:

```bash
uname -m                 # expected: x86_64
cat /etc/os-release      # Ubuntu 24.04 or a newer supported Ubuntu release
docker version
docker compose version
```

The container intentionally uses a patched, digest-pinned Python 3.13 Debian Bookworm slim
userspace. It runs on the Ubuntu host through Docker; the image distribution does not need
to match the host distribution. No Python, Git, or OpenSSL installation is needed directly
on the host for the backup worker. Select this Ubuntu x86_64 server as the Coolify destination.
Do not remove the platform setting to build accidentally for an ARM development machine.

References: [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
[Compose platform/build selection](https://docs.docker.com/reference/compose-file/build/).

## Defaults

| Setting | Default | Purpose |
|---|---|---|
| Schedule | Immediately on start, then every hour | First backup needs no waiting |
| Recovery points | 3 latest + 2 weekly + 2 monthly + oldest baseline (up to 8 distinct) | Recent code and older safety checkpoints |
| Missing/deleted repositories | Keep indefinitely; report unavailable IDs | Upstream deletion never propagates |
| Artifact budget | 100 GiB per prefix | Refuse new artifacts before crossing budget |
| Run timeout | 12 hours | Stop hung backups and report failure |
| Workers | 1 | Process repositories sequentially |
| CPU / memory limits | 0.5 CPU / 512 MiB | Prevent host resource exhaustion |
| Manifest history | 14 recent manifests; independent per-repository catalogs | Bound diagnostic history |
| Extra service data | Disabled | Predictable scope and size; Git refs/history only |
| Cache | Persistent named volume | Incremental Git fetches between runs |

The scheduler measures intervals from each run's start, rather than a wall-clock cron.
Restarting the service triggers a new run. It never overlaps scheduled jobs; missed
intervals are skipped. Set BACKUP_INTERVAL_HOURS to a number of hours (minimum 1).
Do not add a second Coolify scheduled job for this service.

## Set up GitHub and Spaces

1. Follow [GitHub App setup](GITHUB_APP.md). Install the App with **All repositories** on
   each user and organization, with Contents read permission. Gists require another
   authentication method and are disabled here.
2. Use an existing shared **unversioned** Spaces bucket or create one. Keep this backup
   prefix private; do not enable public access/CDN for it. The bucket may contain other
   applications’ data outside `SPACES_PREFIX/`; the worker does not touch that data.
   Use a bucket-scoped Spaces key with Read/Write/Delete objects access, plus the ability
   to list the bucket and read bucket versioning status. A read-only key cannot rotate backups.
3. Choose the region slug, e.g. `fra1`, and bucket name. The code uses the origin endpoint
   `https://fra1.digitaloceanspaces.com`, not the CDN or bucket-prefixed endpoint.
4. Base64-encode the App PEM as a single line on your own computer:

   ```bash
   base64 < app-private-key.pem | tr -d '\n'
   ```

   Store this output as a secret. Base64 is an encoding, not encryption. The runtime
   writes a mode-0600 key into tmpfs and removes its encoded value from child environments.

## Deploy from Git in current Coolify

In the target Coolify project/environment select **+ New**, then choose the Git repository
(public repository, deploy key, or supported Git App source). For this public source use
`https://github.com/drhdev/github-backup`. Select the **Docker Compose** build pack / Compose
build strategy. Set **Base Directory** to `/` and **Docker Compose Location** to `/compose.yaml`
(the repository-root file), save, and review the parsed Compose content. Leave domains and
published ports unset: the worker has no inbound HTTP listener. Do not use Docker Compose Empty
with this file's relative `build: .` alone; the build requires the Git checkout and Dockerfile. Use a branch or reviewed commit containing this feature; old
published images do not contain the scheduler. The Compose definition builds the
Dockerfile from the checked-out source. Keep exactly one replica, disable preview
instances for this resource, and avoid rolling/parallel deployments against the same prefix.

Open the application's **Environment Variables** in its production environment. Enable
**Buildtime and Runtime for all six required values**. The current Compose file uses
`${NAME:?message}` for them, and Coolify resolves those expressions during `docker compose build`
as well as startup. Runtime-only values therefore cause the "required variable is missing a value"
error before the image build starts. This is Compose configuration interpolation, not a backup
running during the build. Do not add credentials to the Dockerfile or commit them to source.
Optional Kuma settings can remain runtime-only because their Compose expressions have defaults. Use Normal view for individual masked/locked secrets.
Developer view accepts `.env` text, but saving that view synchronizes its contents and can remove
omitted existing entries. Keep environment-value log redaction enabled. Save environment changes
and redeploy to apply them. See [Coolify's environment variable documentation](https://coolify.io/docs/applications/configuration/environment-variables).

Set these runtime environment variables; mark keys and secrets as sensitive:

```dotenv
GITHUB_APP_ID=123456
GITHUB_APP_OWNERS=your-user,your-org,second-user,third-user
GITHUB_APP_PRIVATE_KEY_BASE64=BASE64_PEM_HERE
AWS_ACCESS_KEY_ID=SPACES_KEY_HERE
AWS_SECRET_ACCESS_KEY=SPACES_SECRET_HERE
SPACES_BUCKET=your-existing-shared-bucket
SPACES_REGION=fra1
SPACES_PREFIX=github-backup
```

Exactly six values have no usable default: `GITHUB_APP_ID`, `GITHUB_APP_OWNERS`,
`GITHUB_APP_PRIVATE_KEY_BASE64`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `SPACES_BUCKET`.
The values above are placeholders: replace all six. `SPACES_REGION=fra1` works only if your
bucket is in Frankfurt; change it to the bucket's actual region, e.g. `nyc3` or `ams3`.
Use only the bucket name in `SPACES_BUCKET`, not an endpoint URL or folder path.

## Find the values: GitHub and DigitalOcean

| Required variable | Source | What to copy |
|---|---|---|
| `GITHUB_APP_ID` | GitHub **Settings → Developer settings → GitHub Apps → your backup App → General** | Numeric **App ID**; not Client ID, installation ID, or App name |
| `GITHUB_APP_OWNERS` | GitHub profile/org URLs for each installation | Login names, e.g. `alice,example-team,alice-work`; no URL prefixes or display names |
| `GITHUB_APP_PRIVATE_KEY_BASE64` | App **General → Private keys → Generate a private key** | Download the PEM, encode it using the command above, paste the single-line output |
| `AWS_ACCESS_KEY_ID` | DigitalOcean **Spaces Object Storage → Access Keys → Create Access Key** | Spaces **access key ID**, not the key's friendly name |
| `AWS_SECRET_ACCESS_KEY` | Same Spaces key-creation result | Matching **secret key**, saved securely at creation; replace the pair if lost |
| `SPACES_BUCKET` | DigitalOcean **Spaces Object Storage → Buckets → destination bucket** | Bucket name alone; existing shared buckets are supported |

### Why DigitalOcean credentials use AWS-prefixed names

The S3 client is boto3. Its standard credential-provider variables are `AWS_ACCESS_KEY_ID`
and `AWS_SECRET_ACCESS_KEY`, including when a custom DigitalOcean endpoint is configured.
**Both values come from DigitalOcean Spaces. You do not need an AWS account or IAM keys.**
The scheduler sets `https://SPACES_REGION.digitaloceanspaces.com` as the endpoint; it does
not send these backups to Amazon. `SPACES_BUCKET`, `SPACES_REGION`, and `SPACES_PREFIX` are
application configuration rather than boto3 credential-provider settings. Preserve the names
exactly; unsupported aliases such as `SPACES_ACCESS_KEY_ID` will not configure this worker.

### Create the Spaces key pair

Open [DigitalOcean's control panel](https://cloud.digitalocean.com/), select the team owning
the bucket, then **Spaces Object Storage → Access Keys → Create Access Key**. Give the key
a recognizable name, select the intended bucket and the required read/write/delete access,
and retain both the access key ID and its matching secret. Existing bucket policies may
restrict which key scopes are compatible: follow the [Spaces access guide](https://docs.digitalocean.com/products/spaces/how-to/manage-access/).
The worker must also be allowed to list objects and read versioning status. Do not grant
unnecessary access to other applications' buckets merely because they share a team.

Enter the two values in the matching `AWS_...` fields in Coolify. They are different from a
DigitalOcean personal API token, SSH key, GitHub private key, or the Spaces key's friendly name.
If the secret was not saved, create a replacement pair, update **both** variables, test a
successful backup, then retire the old key. Never paste real secrets into a support request.

### Find region and choose the folder

Open the destination bucket in the Spaces control panel. Its region and origin hostname
identify `SPACES_REGION`: e.g. `company-files.ams3.digitaloceanspaces.com` corresponds to
`SPACES_BUCKET=company-files` and `SPACES_REGION=ams3`. Use the origin hostname, not a CDN or
custom-domain hostname. `fra1` is only the default and must be changed if the bucket lives elsewhere.
`SPACES_PREFIX=github-backup` is a folder name you choose; no additional key or token is needed.

References: [DigitalOcean with S3 SDKs](https://docs.digitalocean.com/products/spaces/reference/aws-sdks/),
[boto3 credential environment variables](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html).

For example, a bucket named `company-files` in Amsterdam that also stores invoices:

```dotenv
GITHUB_APP_ID=123456
GITHUB_APP_OWNERS=alice,example-team,alice-work
GITHUB_APP_PRIVATE_KEY_BASE64=REPLACE_WITH_YOUR_SINGLE_LINE_BASE64_PEM
AWS_ACCESS_KEY_ID=REPLACE_WITH_YOUR_SPACES_ACCESS_KEY
AWS_SECRET_ACCESS_KEY=REPLACE_WITH_YOUR_SPACES_SECRET
SPACES_BUCKET=company-files
SPACES_REGION=ams3
SPACES_PREFIX=github-backup
```

This writes under `s3://company-files/github-backup/`; files such as
`s3://company-files/invoices/2026.pdf` are not listed, counted toward the budget, or deleted.
Reserve the entire configured root exclusively for this worker. To use another root, set
`SPACES_PREFIX=backups/github` (no leading/trailing slash, empty components, `.` or `..`).
Changing it starts a separate archive and budget; the old prefix is left intact. Do not point
two workers at the same prefix or use overlapping prefixes.

| Optional variable | Default | Meaning |
|---|---|---|
| `SPACES_REGION` | `fra1` | Must match your bucket's region |
| `SPACES_PREFIX` | `github-backup` | Exclusive folder inside a shared bucket |
| `BACKUP_INTERVAL_HOURS` | `1` | Poll interval; minimum one hour |
| `BACKUP_RETENTION` | `3` | Most recent changed versions per active repo |
| `BACKUP_KEEP_WEEKLY` | `2` | Latest version in each of previous two completed UTC calendar weeks |
| `BACKUP_KEEP_MONTHLY` | `2` | Latest version in each of previous two completed UTC calendar months |
| `BACKUP_KEEP_OLDEST` | `true` | Pin the first successful version as a baseline |
| `BACKUP_MIN_AGE_DAYS` | `0` | Optional age floor; enabling it can exceed the count limit |
| `BACKUP_MANIFEST_RETENTION` | `14` | Recent diagnostic run reports, not code versions |
| `BACKUP_MAX_STORAGE_GIB` | `100` | Admission budget for this prefix, not the bucket |
| `BACKUP_WORKERS` | `1` | Parallel repository operations |
| `BACKUP_TIMEOUT_HOURS` | `12` | Maximum run duration |
| `BACKUP_CPUS` | `0.5` | Container CPU limit |
| `BACKUP_MEMORY_LIMIT` | `512m` | Container memory limit; raise for large repos |
| `ACKNOWLEDGED_MISSING_REPOSITORIES` | empty | Comma-separated IDs for acknowledged unavailable repos |
| `UPTIME_KUMA_PUSH_URL` | empty (disabled) | Generated secret URL of an optional Kuma Push monitor |
| `UPTIME_KUMA_ALLOW_HTTP` | `false` | Explicit private-network HTTP opt-in |
| `UPTIME_KUMA_TIMEOUT_SECONDS` | `5` | Monitoring request timeout, max 30 seconds |
| `UPTIME_KUMA_MAX_SUCCESS_AGE_HOURS` | `0` | Auto: `2 × BACKUP_INTERVAL_HOURS + 1`; default 3 hours |

Defaults apply when optional entries are absent. Compose also uses them for empty optional
values; avoid explicitly blank numeric values when launching Python without Compose.
`AWS_EC2_METADATA_DISABLED=true` is an internal fixed setting: this deployment uses the supplied
Spaces key rather than EC2 instance credentials.

All optional entries and defaults are in [.env.example](../.env.example). Retention and budget
must remain positive. No `.env` file needs to be committed. Deploy and inspect logs for
`backup_finished` with `exit_code: 0`. The first startup may be healthy while that initial
backup is running; green process health alone does not prove a completed backup.

## Logging and optional Uptime Kuma

Logs go to stdout/stderr and are available in Coolify Logs or `docker compose logs`.
Scheduler/run events are flushed JSON on stdout; application progress/errors are JSON on stderr.
Startup failures may also write standard Python tracebacks to stderr. The Docker log driver
rotates at 3 × 10 MB; no application log files or extra log volume are required.

For optional external monitoring, create a Kuma **Push** monitor with **180-second heartbeat
interval**, **0 retries**, and a tested notification channel. Add its generated secret URL to
`UPTIME_KUMA_PUSH_URL` and redeploy. Leave the value empty to disable monitoring completely.
The scheduler sends every 60 seconds plus status changes and reports failures and overdue
successes, rather than sending unconditional up pings. A monitoring-server failure never stops
backups. [The monitoring guide](MONITORING.md) explains all settings, startup behavior,
private HTTP connections, secrets, freshness thresholds and an alert/recovery test.

Compose owns the health check. Failed or partial runs become unhealthy until a successful
run. A dead scheduler heartbeat or an overdue completed backup also becomes unhealthy.
Configure Coolify's notifications/external monitoring to alert on unhealthy status and
repeated restarts; Docker does not restart containers just because they are unhealthy.
Container logs rotate at 3 × 10 MB. The key remains accessible to the container administrator;
protect Coolify, host access, and the underlying secret store.

The volume is initialized by Docker for UID 10001. If replacing it with a host bind mount,
set the correct owner and private permissions. Size local storage for the Git mirrors plus
one temporary bundle by default (more if increasing worker count). Local free-space checks run before backups. Git pack operations use one thread and modest
cache/window settings; transfers also run single-threaded. The 512 MiB limit is a containment
limit, not a guarantee that any-size repository will fit. Very large repositories or blobs may
need BACKUP_MEMORY_LIMIT raised and a longer timeout. Throttled transfers retry after two minutes
below 1 KiB/s. Persistent cache growth still needs host disk monitoring. If a cache is corrupt,
stop the worker and remove only its local cache mirrors; remote catalogs remain authoritative.

## Retention and capacity

The recommended profile uses `versioned` mode. It fetches and verifies Git mirrors on every
poll but creates a self-contained bundle only when the digest of the advertised Git refs
changes relative to the last remotely committed version. This detects commits, force-pushes,
branch/tag creation, and deleted refs; it does not trust API `pushed_at` timestamps. If refs
are identical, no new code artifact is uploaded. Small run manifests are still written to
prove the worker checked. Polling can miss a push and deletion/rewrite between polls; it
cannot recover code that GitHub no longer exposes before it has been backed up.

## Folder layout in a shared bucket

```text
company-files/                              # existing shared bucket
├── invoices/                               # another application: untouched
└── github-backup/                          # SPACES_PREFIX (configurable)
    ├── alice/
    │   └── website/
    │       ├── 20261107153000/
    │       │   └── repository-123-<unique-id>.bundle
    │       └── 20261109104520/
    │           └── repository-123-<unique-id>.bundle
    ├── example-team/
    │   └── api/
    │       └── 20261107154500/
    │           └── repository-456-<unique-id>.bundle
    └── _system/                           # internal indexes and run reports
        ├── catalog/repository/123.json
        └── manifests/latest.json
```

Folder timestamps are **UTC `YYYYMMDDHHMMSS`**, captured when the local artifact is ready
for upload. The file includes the stable repository ID and a unique suffix so multiple
backups in one second or deleted-and-recreated repositories cannot overwrite each other.
Empty repositories use `.empty.tar.gz` instead of `.bundle`. Unchanged polls do not create
a timestamp folder. S3 folders are key prefixes, so deleting a folder's final object removes
that folder from the listing.

Each repository's `_system/catalog/repository/GITHUB_ID.json` records its name, owner,
artifact keys, timestamps, ref digests, sizes and SHA-256 checksums. Renaming or transferring
a repository sends future backups to the new owner/name folders. Existing backups stay at
their original paths and remain referenced in the same ID-based catalog. Deleting and recreating
a repository with the same name uses a new catalog and filenames, preserving the old backup.
Catalogs and code can be downloaded from Spaces without access to GitHub.

Existing legacy `catalog/` and `versions/` data are read and migrated lazily: new catalogs use
`_system/`, and new artifacts use the human-readable layout. Existing files are not moved or
mass-deleted; they remain in the budget. Use the current catalog when restoring. Do not add
bucket-wide lifecycle expiration rules to implement application retention.

Each upload is bundled and locally verified, hashed in a streaming pass, uploaded with one
transfer stream, then checked using remote size and checksum metadata before its catalog is
updated. HEAD metadata verification is not a full remote read-back or restore test. Missing
remote artifacts are repaired even if no Git refs changed. Cache loss does not create duplicate
remote versions. If upload or catalog publication fails, the previous catalog stays valid;
no existing artifact is overwritten. An orphan upload is removed after 48 hours on a later
inventory pass only when an existing valid catalog proves it is unreferenced and its repository
is currently visible. Failure to read a catalog is fatal, never interpreted as an empty catalog.

For visible repositories, retention takes the **union** of these selections:

- The latest 3 changed versions.
- The newest version in each of the previous 2 completed UTC calendar weeks (Monday–Sunday).
- The newest version in each of the previous 2 completed UTC calendar months.
- The first successful version, pinned as an older baseline.

A version selected more than once is stored only once: the default maximum is **8 distinct
versions per active repository**, often fewer. No duplicate backups are synthesized for weeks
or months without changes. The oldest baseline remains even when all recent pushes happen
within a short interval. Calendar representatives rotate as time passes; this is a compact
policy choice, not a guarantee of recovery from every incident or detection delay.
`BACKUP_MIN_AGE_DAYS=0` replaces the previous 30-day age floor to avoid retaining hundreds
of hourly changes. Setting it above zero adds an explicit age floor and removes the 8-copy bound.

Retention runs only for repositories whose current version was successfully verified; it
never prunes an unavailable repository. Catalog updates precede artifact deletion, so an
interruption leaves extra objects instead of dangling references. Failed deletes surface
as backup failures. Budget limits still apply when retention selects fewer versions.

A missing repository is reported as **missing or inaccessible**, not asserted to be deleted:
revoked permissions, ownership changes, and deletion may look identical. All its versions
are retained indefinitely. Health becomes unhealthy until access is restored or its numeric
repository ID is added to `ACKNOWLEDGED_MISSING_REPOSITORIES` (comma-separated). Acknowledging
only silences this warning; it never deletes the catalog or code. After an intentional deletion,
verify an offline restore and keep the archived versions. Purging them requires a separate,
explicit administrative action after reviewing the catalog and recovery requirements.

The 100 GiB budget is an admission guard, not a billing quota. Upload workers synchronize
full-size reservations against the current prefix inventory and leave 16 MiB for control data.
If another version cannot fit, the run fails while existing versions remain intact. Raise the
reviewed budget or explicitly remove reviewed old data to recover capacity. Count retention,
tiered retention and indefinite deleted-repository preservation cannot together
provide an absolute storage-size guarantee: stopping new uploads is the safe response.
At least one new version must fit before old versions are pruned. Versioned mode never
silently deletes data previously written under legacy `snapshots/` or `latest/` prefixes;
review migration storage before switching an existing deployment.

Use one exclusive prefix inside the shared bucket and exactly one writer. The Compose profile uses a local
volume lock, because Spaces' documented API does not establish the conditional PUT
semantics required by the CLI's cross-host ETag lease. Do not run another host/deployment
against this prefix. For other S3 deployments the distributed lease stays enabled by default.

Versioned or formerly versioned buckets are rejected when budgeting is enabled: deleting
current keys does not remove historical versions. Existing noncurrent data, incomplete
multipart uploads, other prefixes, and bucket overhead do not belong to the artifact budget.
Set provider billing alerts and a bucket lifecycle rule to abort incomplete multipart
uploads after one day. A sample rule is in [spaces-lifecycle.json](spaces-lifecycle.json).
The sample filters **only `github-backup/`**; change that filter to match a custom prefix.
Merge it with existing lifecycle rules using an administrative credential; do not overwrite
unrelated policies. The application never changes bucket-wide policies automatically.
Avoid age expiration of snapshots by default: it can delete the last good recovery point
if the backup worker has been failing or offline for a long period.

## Validate recovery

Check the first manifest at `SPACES_PREFIX/_system/manifests/latest.json`: all expected accounts
and repositories must be present, missing_resources must be empty, and failure/partial counts
must be zero. Download a
retained `.bundle` and its repository catalog using a Spaces-compatible client, then restore:

```bash
git clone /path/to/downloaded-repository.git.bundle restored-repository
git -C restored-repository fsck --full
git -C restored-repository log -1 --oneline
```

Repeat restore drills periodically. The CLI's `verify` command verifies filesystem backups;
it does not download or verify remote Spaces objects. Encryption at rest uses the existing
S3 AES256 request; configure independent access and retention controls as needed.

## Local Compose equivalent

```bash
cp .env.example .env
# Fill required variables with your own values; keep .env private.
chmod 600 .env
docker compose config --quiet
docker compose up -d --build
docker compose logs -f github-backup
```

The environment file and private-key files are excluded from the build context.

References: [Coolify Compose from Git](https://coolify.io/docs/applications/builds/docker-compose),
[Coolify application environment variables](https://coolify.io/docs/applications/configuration/environment-variables),
[DigitalOcean Spaces API](https://docs.digitalocean.com/reference/api/spaces/).

## Coverage and incident recovery

The default backs up Git history and refs exposed by GitHub, not Git LFS payloads, release
assets, Actions data, issues, secrets, or submodule contents from other repositories. Those
need separate coverage; the legacy CLI supports optional exports, while versioned mode
currently rejects `additional_data` rather than misrepresenting its retention.

For recovery when GitHub is unavailable: list the `SPACES_PREFIX/_system/catalog/` prefix, download the desired
catalog and version, compare the downloaded file's SHA-256 with `sha256` in that catalog,
then clone the bundle and run `git fsck --full`. Older versions are essential after malicious
force-pushes; latest does not necessarily mean trustworthy code. Empty repositories use
`.empty.tar.gz` instead of Git bundles. Validate the archive and extract it into an isolated
restore directory. Run periodic restore drills independent of the backup worker.

[CISA's ransomware guidance](https://www.cisa.gov/sites/default/files/2023-10/StopRansomware-Guide-508C-v3_1.pdf)
recommends offline backups and regular restoration testing. The storage budget is deliberately
fail-closed: it does not sacrifice the last good copies to keep a green status.
