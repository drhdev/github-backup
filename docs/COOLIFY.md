# Docker Compose on Coolify with DigitalOcean Spaces

This is the recommended deployment. A background worker backs up all configured GitHub
App installations to a private Spaces bucket. A small protected config service provides
automated GitHub App registration through an HTTPS domain. The existing CLI and filesystem backends remain available.

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
| Schedule | After GitHub setup, then every hour | Begins within 30 seconds of first account enablement |
| Recovery points | 3 latest + 2 weekly + 2 monthly + oldest baseline (up to 8 distinct) | Recent code and older safety checkpoints |
| Missing/deleted repositories | Keep indefinitely; report unavailable IDs | Upstream deletion never propagates |
| Artifact budget | 100 GiB per prefix | Refuse new artifacts before crossing budget |
| Run timeout | 12 hours | Stop hung backups and report failure |
| Workers | 1 | Process repositories sequentially |
| Worker CPU / memory limits | 0.5 CPU / 512 MiB | Prevent host resource exhaustion |
| Config CPU / memory limits | 0.25 CPU / 128 MiB | One web worker with two threads; usually idle |
| Manifest history | 14 recent manifests; independent per-repository catalogs | Bound diagnostic history |
| Extra service data | Disabled | Predictable scope and size; Git refs/history only |
| Cache | Persistent named volume | Incremental Git fetches between runs |

The scheduler measures intervals from each run's start, rather than a wall-clock cron.
Restarting the service triggers a new run. It never overlaps scheduled jobs; missed
intervals are skipped. Set BACKUP_INTERVAL_HOURS to a number of hours (minimum 1).
Do not add a second Coolify scheduled job for this service.

## Deploy and configure through the config page

The [README installation steps](../README.md#install-with-coolify) are the primary walkthrough.
Use Docker Compose from Git, Base Directory `/`, Compose Location `/compose.yaml`, one replica
of each service and the shared persistent `backup-cache` volume. Assign an HTTPS domain to
`github-config`, forwarding to container port 8080, with no published host port. If Coolify
uses a target-port suffix in its domain field, enter `https://github-backup.example.com:8080`;
the public URL remains `https://github-backup.example.com`. The worker has no domain.

Only the config HTTPS URL is required in the environment for a new installation:

```dotenv
GITHUB_CONFIG_URL=https://github-backup.example.com
```

The URL is the exact HTTPS origin without a path or internal target port. Enable Buildtime
and Runtime on supplied variables for Compose interpolation. Save and deploy. Do not add a
Coolify scheduled task: the worker includes scheduling.

The first visit shows registration. Retrieve the one-time code from the **`github-config`
service Terminal** in Coolify:

```bash
python -m github_backup.config_auth registration-code
```

Choose your own username (3-64 letters, digits, dots, underscores or hyphens) and a unique
16-128 character password/passphrase, confirm it and store it securely. Register once, then
log in through the form. Registration is permanently disabled after that first owner is
created. No reset route or extra users are available. Forgotten credentials require a fresh
deployment with a new persistent volume; do not delete remote backups. Damaged/deleted auth
state blocks setup instead of reopening registration. Read [account recovery](GITHUB_APP.md#config-account-and-recovery).
The registration code is never displayed in the web page or ordinary container logs.

After login, fill out DigitalOcean Spaces in the form. Enter the
bucket name, region slug, root folder (default `github-backup`), Spaces access key ID and
matching secret. These values are no longer required in environment variables. Find the
bucket under DigitalOcean **Spaces Object Storage → Buckets**, and create a key under
**Spaces Object Storage → Access Keys**. This is not a DigitalOcean personal API token.
For `company-files.ams3.digitaloceanspaces.com`, enter bucket `company-files`, region `ams3`.
`fra1` is only the initial form default; choose the actual bucket region.

Use an unversioned bucket, a private exclusive prefix, and a key with object read/write/delete,
bucket listing and versioning-status read access. Shared buckets are supported: other apps'
prefixes are not counted, listed or deleted. The save action tests one disposable private
object under `PREFIX/_system/setup-check/`, including read-back and deletion. Failed tests
preserve the previous saved configuration. An interrupted probe or a key without delete
permission may leave a tiny object under that setup-check prefix; remove it after fixing
permissions. The application never changes bucket policies.

Key inputs are never prefilled or echoed. Leave them blank to keep saved credentials when
editing other fields. Changing bucket/region/root requires an explicit confirmation; this
starts a separate archive and budget, leaving the old location intact. The current backup
finishes with its original configuration; subsequent runs load the new saved configuration.
Do not reuse or overlap another worker's prefix. The new destination may be empty until the
next scheduled run; verify its first successful manifest and restore before retiring the old one.

Create one or more GitHub Apps in setup and connect their personal/organization installations.
Choose each account's repository scope: **all**, **selected**, **all except selected**, or
**pause**. GitHub may grant the App access to all repositories or only selected ones. The
backup selection can narrow that access; it cannot widen it. The all/all-except modes require
GitHub's All repositories grant so future repositories are covered. New connections start
paused. See [the GitHub setup guide](GITHUB_APP.md) for multiple Apps and accounts.

Settings are atomically stored in `/cache/github-app/credentials.json`, permissions 0600,
on the shared volume. Both containers need access to this secret file, which contains App
keys, account selections and Spaces credentials. Back it up securely and retain ownership
UID 10001. A single immutable snapshot is used for each backup run; UI edits apply next run.
Do not run parallel deployments against the same archive.

While initial setup is incomplete, a live scheduler emits `waiting_for_setup` and reports
down to Kuma. Docker health confirms that the waiting scheduler is alive, not that backups
exist. After setup, check `backup_finished`, zero failure/partial counts, the selected repo
inventory and a restore drill. An all-paused or empty selection backs up no repositories.
A failing App connection is recorded in `discovery_errors` and makes the run unhealthy;
other working connections continue backing up. Deliberate exclusions appear separately in
`excluded_resources`, with their existing archives preserved.

### Optional environment settings and manual compatibility

[.env.example](../.env.example) contains all optional schedule/retention/budget/monitoring
variables with defaults. The default is hourly checks, one transfer worker, a 100 GiB prefix
budget and 3 recent + 2 weekly + 2 monthly + oldest baseline retention. At most 8 distinct
versions remain per active repository with the default zero-day age floor. Paused/excluded
and missing repositories preserve their existing versions indefinitely and remain in the budget.

Existing deployments may keep `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `SPACES_BUCKET`,
`SPACES_REGION` (default `fra1`) and `SPACES_PREFIX` (default `github-backup`). The `AWS_` names
are boto3's standard credential names and take **DigitalOcean Spaces** values; no AWS account
is required. Saved browser Spaces settings take precedence. Remove legacy values after
verifying the saved configuration to avoid ambiguity. If none are configured, the worker
waits for browser configuration; a partially filled legacy configuration fails clearly.

Manual single-App configuration remains available through `GITHUB_APP_ID`,
`GITHUB_APP_OWNERS` and `GITHUB_APP_PRIVATE_KEY_BASE64`. Those values override browser App
connections. Clear all three to use multiple Apps and repository scopes through setup.
Manual mode continues requiring All repositories on GitHub. See [manual setup](GITHUB_APP.md#manual-environment-alternative).

The config URL has no universal default. The account password is chosen in the registration
form and is never stored in an environment variable. Optional operational variables have defaults; keep secrets out of Git/Dockerfiles. Coolify's Developer environment view can
remove omitted entries when saving; Normal view lets you edit individual masked values.
Preserve environment log redaction. The compose file builds the checked-out source; use a
reviewed revision, not automatic GitHub-triggered updates.

## Logging and optional Uptime Kuma

Logs go to stdout/stderr and are available in Coolify Logs or `docker compose logs`.
Scheduler/run events are flushed JSON on stdout; application progress/errors are JSON on stderr.
Startup failures may also write standard Python tracebacks to stderr. The Docker log driver
rotates at 3 × 10 MB; no application log files or extra log volume are required.

For optional external monitoring, create a Kuma **Push** monitor with **180-second heartbeat
interval**, **0 retries**, and a tested notification channel. In setup, enter its generated
secret URL under **Uptime Kuma**, enable monitoring and save. It takes effect within 30 seconds
without redeployment. Leave the URL input blank to retain the saved secret; uncheck Enable
and save to disable monitoring. Saved monitoring choices override legacy `UPTIME_KUMA_*`
environment values. For private HTTP Kuma, explicitly check the trusted-private-network opt-in.
The scheduler sends every 60 seconds plus status changes and reports failures and overdue
successes, rather than sending unconditional up pings. A monitoring-server failure never stops
backups. [The monitoring guide](MONITORING.md) explains all settings, startup behavior,
private HTTP connections, secrets, freshness thresholds and an alert/recovery test.

Compose owns the health check. Failed or partial runs become unhealthy until a successful
run. A dead scheduler heartbeat or an overdue completed backup also becomes unhealthy.
Configure Coolify's notifications/external monitoring to alert on unhealthy status and
repeated restarts; Docker does not restart containers just because they are unhealthy.
Container logs rotate at 3 × 10 MB. The stored credentials remain accessible to the container administrator;
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
it does not download or verify remote Spaces objects. The Spaces profile does not send an
AWS-specific AES256 SSE request ([Spaces S3 compatibility](https://docs.digitalocean.com/products/spaces/reference/s3-compatibility/)).
Configure host encryption and independent storage/access controls for your recovery requirements.

## Local Compose equivalent

```bash
cp .env.example .env
# Fill required variables with your own values; keep .env private.
chmod 600 .env
docker compose config --quiet
docker compose up -d --build
docker compose logs -f github-backup
```

Point the config domain at a local HTTPS reverse proxy targeting the config service on port 8080;
the stack intentionally does not publish host ports. The environment file and private-key
files are excluded from the build context.

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

## Config interface, account changes and activity

The page uses the neutral shadcn-style visual language with locally served CSS and a tiny
script for appearance/time formatting. It has no third-party scripts, fonts or trackers.
Light/dark/system appearance works on desktop and iPhone-sized screens; forms use labeled
controls, keyboard focus indicators and touch targets at least 44 pixels high.

Follow **Storage → GitHub connections → Repository scope**, then verify a completed check in
**Activity**. Reopen configured sections to edit them. **Account** changes require your current
password, invalidate every session and keep registration closed. Forgotten credentials still
require a fresh persistent deployment or a complete known-good authentication restore.

The worker stores at most 250 completed repository results in the private local file
`/cache/backup-history/history.sqlite3`. The config service reads it without needing S3 access
for Activity. History includes successes and failures, excludes raw error payloads/credentials,
and is independent of remote manifests and retention. History failures do not stop backups.
Deleting the local cache loses this activity history; it never deletes remote backup versions.

The current service name is `github-config` and the canonical URL variable is `GITHUB_CONFIG_URL`.
The old `GITHUB_SETUP_URL` remains a compatibility fallback; new installations should use the
new name. After upgrading, reassign the HTTPS domain to `github-config` port 8080
and remove any leftover route/container for `github-setup` without deleting volumes.
Reuse the existing `backup-cache` volume. Owners may need to log in again after the cookie
name changes; their credentials are retained. Existing private `setup-auth` storage paths and the deployment seal are intentionally
retained to preserve registered accounts. Do not rename/delete these files during upgrades.

Security headers prevent framing, caching of private pages, external resources, inline scripts
and browser referrer leakage. Values are contextually escaped; forms enforce field allowlists,
length limits, single-value parameters, CSRF tokens and typed validation. Passwords and secrets
are never inserted into the DOM as saved values. Password characters are not globally forbidden:
a password is hashed, not interpreted as HTML. Protect the host, persistent volume, proxy and
browser as well; application protections cannot secure a compromised administrator device/host.
Keep HTTPS, disable request-body/query logging, retain independent credentials and disk encryption.
