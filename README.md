# github-backup

[![CI](https://github.com/drhdev/github-backup/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/drhdev/github-backup/actions/workflows/ci.yml)
[![Security](https://github.com/drhdev/github-backup/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/drhdev/github-backup/actions/workflows/security.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/drhdev/github-backup/badge)](https://securityscorecards.dev/viewer/?uri=github.com/drhdev/github-backup)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/downloads/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)

Production-oriented, token-authenticated backups for GitHub.com and GitHub Enterprise Server. `github-backup` discovers repositories a GitHub user can access, creates verified Git mirrors, backs up the user's gists, and can optionally export wikis, pull-request and issue metadata, releases, GitHub Actions data, Git LFS objects, repository settings, and security metadata.

Backups can be written to local disk, a mounted NAS, AWS S3, or an S3-compatible object store. The default is a resource-efficient incremental backup to the local filesystem; scope, storage, and coverage can be changed in TOML or for one run.

> [!IMPORTANT]
> A backup is only proven when it can be restored. Run `github-backup verify` and perform scheduled restore drills. This project is designed for reliable unattended operation, but production readiness also requires protected storage, monitoring, retention, and tested recovery procedures.

## Contents

- [Purpose and use cases](#purpose-and-use-cases)
- [Backup coverage](#backup-coverage)
- [Defaults and selectable options](#defaults-and-selectable-options)
- [Reliability and security design](#reliability-and-security-design)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Create a GitHub token](#create-a-github-token)
- [Install on Debian, Ubuntu, or Raspberry Pi OS](#install-on-debian-ubuntu-or-raspberry-pi-os)
- [Run with Docker](#run-with-docker)
- [Configuration](#configuration)
- [Storage backends](#storage-backends)
- [Command-line reference](#command-line-reference)
- [Test a deployment](#test-a-deployment)
- [Automate backups with systemd](#automate-backups-with-systemd)
- [Logs, monitoring, and exit codes](#logs-monitoring-and-exit-codes)
- [Restore and disaster recovery](#restore-and-disaster-recovery)
- [Updating and troubleshooting](#updating-and-troubleshooting)
- [Releases, artifacts, and supply-chain verification](#releases-artifacts-and-supply-chain-verification)
- [Production checklist](#production-checklist)
- [Contributing, security, and license](#contributing-security-and-license)

## Purpose and use cases

Git hosting availability is not an independent backup. Repositories can be deleted, access can be lost, branches can be force-pushed, credentials or settings can be compromised, and retained service data such as Actions artifacts can expire.

`github-backup` is intended for:

- developers who need independent copies of personal repositories and gists;
- teams scheduling backups to an encrypted server or NAS;
- organizations requiring portable Git bundles and auditable manifests in S3;
- GitHub Enterprise Server operators adding repository-level recovery to the official appliance backup;
- migrations, retention, incident recovery, and disaster-recovery exercises;
- environments requiring a read-only token, least privilege, deterministic configuration, and non-interactive operation.

It is not a two-way synchronization service and does not automatically recreate GitHub resources. Git data is directly restorable with Git. Optional service data is exported as portable JSON/JSONL and original binary files for inspection and controlled import.

## Backup coverage

### Included by default

Every normal run backs up:

- every public and private repository visible to the token under the selected scope;
- complete Git object databases and advertised refs in verified bare mirrors;
- branches, tags, notes, commit history, and other refs exposed by Git;
- the authenticated user's public and secret gists;
- resource metadata and an immutable run manifest plus `latest.json`.

The default `accessible` scope includes repositories where the user is owner, collaborator, or organization member, subject to token repository selection, permissions, organization policy, and SSO authorization. Submodule pointers are included in Git history; referenced repositories are backed up separately only when independently discoverable.

### Optional components

Optional data is disabled by default because it increases API traffic, storage, token permissions, and run time. Enable components with repeatable `--include` flags or `backup.additional_data`.

| Component | Exported data | Format and notes |
|---|---|---|
| `wikis` | Wiki Git history and refs | Separate verified mirror or bundle. Empty wikis are recorded as skipped. |
| `metadata` | Repository settings; issues, comments, and events; pull requests, details, reviews, and comments; labels; milestones; branches; rulesets; collaborators; teams; deploy keys; webhook metadata; deployments; environments; secret names; security alerts and advisories | JSON/JSONL in a checksummed `tar.gz`. GitHub never returns secret values. |
| `releases` | Release metadata and accessible release assets | JSONL plus original binary assets in `tar.gz`. Potentially large. |
| `actions` | Non-expired Actions artifacts and completed-run logs still retained by GitHub | JSONL and ZIP files in `tar.gz`. Already-expired upstream data is unrecoverable. |
| `lfs` | All reachable Git LFS objects | `tar.gz`; requires `git-lfs` and can be much larger than Git data. |

```bash
# Wikis and pull-request/issue/settings metadata.
github-backup run --include wikis --include metadata

# Every optional component implemented by this version.
github-backup run --include all
```

Each supplementary archive contains `coverage.json`. Endpoints unavailable because of permissions, edition, plan, or feature state are recorded there and in the manifest. The component becomes `partial` and the process exits `2`, so monitoring cannot mistake a coverage gap for success.

### Enterprise completeness boundary

“All” means all classes implemented here that the token and GitHub API allow. No token-based utility can create a byte-for-byte backup of GitHub.com or GitHub Enterprise Cloud. GitHub does not expose secret values, expired artifacts, deleted objects, every package blob, all audit history, or internal platform state through one backup API.

Plan separate controls for:

- GitHub Packages and container images, using native package/OCI clients;
- Actions, Dependabot, Codespaces, and environment secret **values**, backed up at the source secret manager;
- enterprise audit logs, identity-provider configuration, billing, projects, discussions, and data not listed above;
- data deleted before the first successful backup.

For GitHub Enterprise Server, use GitHub's official [Backup Service](https://docs.github.com/en/enterprise-server/admin/backing-up-and-restoring-your-instance/about-the-backup-service-for-github-enterprise-server) or compatible Backup Utilities for full instance recovery. This project complements but does not replace that appliance backup.

## Defaults and selectable options

| Setting | Default | Effect |
|---|---:|---|
| Storage | `filesystem` | Local disk or mounted NAS. |
| Destination | `./backups` | Relative to the working directory. |
| Scope | `accessible` | Owned, collaborator, and organization-member repositories visible to the token. |
| Mode | `incremental` | Updates mirrors and transfers only new Git data; deleted upstream refs are pruned. |
| Gists | enabled | Public and secret gists owned by the authenticated user. |
| Optional data | none | Wikis, metadata, releases, Actions, and LFS are opt-in. |
| Workers | `4` | Bounded simultaneous transfers/exports. |
| Git/API retries | `3` / `5` | Bounded transient-failure retries with backoff. |
| Verification | enabled | `git fsck --connectivity-only` after mirror updates. |
| Minimum free space | `1.0 GiB` | Refuses to start below this local threshold. |
| Snapshot retention | `0` | Keep all snapshots. |
| S3 encryption | `AES256` | Request SSE-S3. |
| Insecure HTTP | disabled | GitHub and custom S3 endpoints require HTTPS. |
| Broad Unix permissions | rejected | Destination/cache cannot be accessible by group or others. |

Runtime choices:

| Decision | Choices | Trade-off |
|---|---|---|
| Storage | `filesystem`, `s3` | Filesystem enables direct restores. S3 adds off-host object storage but needs a persistent local cache. |
| Scope | `accessible`, `owned`, `organizations` | Maximum token-visible coverage, user-owned only, or explicit organization allowlist. |
| Mode | `incremental`, `snapshot` | Minimum bandwidth/storage versus immutable recovery points. |
| Gists | `--gists`, `--no-gists` | Include or omit the authenticated user's gists. |
| Extra data | component names or `all` | More recovery context at the cost of permissions, traffic, and storage. |

## Reliability and security design

- REST collections are paginated and large collections stream to JSONL.
- API requests honor `Retry-After` and rate-limit reset headers with bounded backoff.
- Git transfers have bounded concurrency, timeouts, retries, and non-interactive authentication.
- Initial clones use temporary siblings and atomic rename only after success.
- Existing backups must be valid bare repositories; mirrors and bundles are verified.
- Local writes use `0077`, atomic manifest/artifact replacement, and a per-destination process lock.
- S3 uses adaptive SDK retries and a renewable conditional ETag lease across hosts.
- Per-resource/component outcomes are isolated and recorded in immutable manifests.
- Credentials are injected only for the matching HTTPS origin; authenticated redirects are refused and secrets are redacted from Git errors.
- The token comes only from an environment variable, never TOML or a CLI argument.
- The container runs as unprivileged UID/GID `10001`.
- The final container omits build tooling and `pip`, which are not needed at runtime.
- CI tests Python 3.11–3.13, linting, strict typing, coverage, and dependency auditing.
- CodeQL, dependency review, Dependabot, OpenSSF Scorecard, immutable workflow-action pins, and
  Trivy container gates and automated release provenance provide additional supply-chain controls.

GitHub rate limits and upstream retention still apply. Lower `workers` for slow NAS devices, small Raspberry Pis, or constrained enterprise instances.

## Requirements

- Python 3.11 or newer;
- Git 2.30 or newer;
- Git LFS only for `--include lfs`;
- HTTPS access to GitHub and any S3 endpoint;
- a GitHub token with read access to required resources;
- the `s3` package extra when using object storage.

## Quick start

```bash
# Read the token without putting it in shell history.
read -rsp 'GitHub token: ' GITHUB_TOKEN && printf '\n'
export GITHUB_TOKEN

github-backup --version
github-backup help
github-backup help run
github-backup check
github-backup list
github-backup run --destination /mnt/backups/github
github-backup verify --destination /mnt/backups/github
```

Global options (`--config`, `--verbose`, `--json-logs`) go **before** the command. Command options (`--destination`, `--scope`, `--include`) go after `run`, `list`, or `check`.

## Create a GitHub token

GitHub recommends fine-grained PATs when they support the use case. Prefer a separate read-only backup identity, set an expiration, document ownership, and rotate before expiry. See [GitHub's token guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

### Fine-grained token (preferred)

1. Open **Settings → Developer settings → Personal access tokens → Fine-grained tokens** or <https://github.com/settings/personal-access-tokens/new>.
2. Name it (for example `github-backup-prod`), set a justified expiration, and select the resource owner.
3. Select every repository that must be backed up.
4. Grant read-only **Contents** and **Metadata** permissions.
5. Add read permissions required by enabled components: Gists, Issues, Pull requests, Actions, Administration, Deployments, Environments, security alerts, and organization members as applicable.
6. Generate it, store it in a secret manager/protected environment file, and obtain organization approval if required.

A fine-grained token has one selected resource owner. For personal repositories plus multiple organizations, prefer separate least-privilege runs/tokens per owner. A classic PAT may be necessary for one broad cross-owner run.

### Classic token

1. Open **Settings → Developer settings → Personal access tokens → Tokens (classic)**.
2. Generate a token with expiration and a descriptive note.
3. Select `repo` for private repository data and `gist` for secret gists.
4. Add `read:org` for required organization/team information and `security_events` only for security-alert metadata.
5. Do not grant write scopes solely for backup.

SAML SSO organizations can require separate authorization. Use **Configure SSO** next to the token and follow GitHub's [SSO instructions](https://docs.github.com/en/enterprise-cloud@latest/authentication/authenticating-with-single-sign-on/authorizing-a-personal-access-token-for-use-with-single-sign-on).

Typical permission mapping:

| Feature | Typical minimum access |
|---|---|
| Repository Git data and LFS | Contents read for selected repositories. |
| Gists | Gists read or classic `gist`. |
| Issues/pull requests | Issues and Pull requests read. |
| Settings, collaborators, hooks, keys, rulesets | Administration read plus required organization read access. |
| Releases | Contents read. |
| Actions artifacts/logs | Actions read. |
| Deployments/environments | Deployments and Environments read. |
| Alerts/advisories | Applicable security-events, Dependabot, secret-scanning, or advisory read permission. |

GitHub's [fine-grained permission reference](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) is authoritative. Always test actual coverage.

Inject the token interactively:

```bash
read -rsp 'GitHub token: ' GITHUB_TOKEN && printf '\n'
export GITHUB_TOKEN
```

For systemd, create `/etc/github-backup/environment` with `sudoedit`:

```text
GITHUB_TOKEN=github_pat_REPLACE_WITH_REAL_VALUE
```

```bash
sudo chown root:root /etc/github-backup/environment
sudo chmod 600 /etc/github-backup/environment
```

For Docker, pass this file with `--env-file`. Never commit a token, store it in TOML, pass it as a command argument, bake it into an image, or paste it into an issue. Rotate immediately after suspected exposure.

## Install on Debian, Ubuntu, or Raspberry Pi OS

Install a published version for production instead of an arbitrary `main` revision. The examples
use `1.1.0`; replace it with the current version shown on the
[Releases page](https://github.com/drhdev/github-backup/releases).

### Local user installation

```bash
python3 --version  # must be 3.11 or newer
sudo apt update
sudo apt install -y ca-certificates git git-lfs python3 python3-pip python3-venv

git clone --branch v1.1.0 --depth 1 https://github.com/drhdev/github-backup.git
cd github-backup
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
# For S3 instead: .venv/bin/python -m pip install '.[s3]'

.venv/bin/github-backup --version
.venv/bin/github-backup help
```

Use `.venv/bin/github-backup` directly or activate it with `. .venv/bin/activate`.

### Dedicated production installation

```bash
sudo apt update
sudo apt install -y ca-certificates git git-lfs python3 python3-pip python3-venv
sudo useradd --system --create-home --home-dir /var/lib/github-backup \
  --shell /usr/sbin/nologin github-backup

sudo git clone --branch v1.1.0 --depth 1 \
  https://github.com/drhdev/github-backup.git /opt/github-backup
sudo python3 -m venv /opt/github-backup/.venv
sudo /opt/github-backup/.venv/bin/python -m pip install --upgrade pip
sudo /opt/github-backup/.venv/bin/python -m pip install '/opt/github-backup[s3]'

sudo install -d -o root -g github-backup -m 0750 /etc/github-backup
sudo install -d -o github-backup -g github-backup -m 0700 /var/cache/github-backup
sudo install -d -o github-backup -g github-backup -m 0700 /srv/github-backup
sudo install -o root -g github-backup -m 0640 \
  /opt/github-backup/github-backup.example.toml /etc/github-backup/config.toml
```

Set `destination = "/srv/github-backup"` and `cache_dir = "/var/cache/github-backup"` in `/etc/github-backup/config.toml`. On Raspberry Pi, prefer an SSD to an SD card and reduce `workers` to `1` or `2` when resources are constrained.

## Run with Docker

Install Docker using its official [Ubuntu](https://docs.docker.com/engine/install/ubuntu/), [Debian](https://docs.docker.com/engine/install/debian/), or [Raspberry Pi OS](https://docs.docker.com/engine/install/raspberry-pi-os/) guide. A simple distribution-package setup is:

```bash
sudo apt update
sudo apt install -y docker.io git
sudo systemctl enable --now docker
sudo docker version
```

The Docker group is root-equivalent; examples use `sudo docker` instead of adding users to it.

For a published release, prefer the signed multi-architecture GHCR image and pin its digest in
controlled deployments:

```bash
sudo docker pull ghcr.io/drhdev/github-backup:1.1.0
sudo docker image inspect ghcr.io/drhdev/github-backup:1.1.0 \
  --format '{{index .RepoDigests 0}}'
export BACKUP_IMAGE=ghcr.io/drhdev/github-backup:1.1.0
```

To build locally from reviewed source instead:

```bash
git clone --branch v1.1.0 --depth 1 https://github.com/drhdev/github-backup.git
cd github-backup
sudo docker build --pull -t github-backup:local .
export BACKUP_IMAGE=github-backup:local

sudo install -d -o 10001 -g 10001 -m 0700 /srv/github-backup
sudo install -d -o 10001 -g 10001 -m 0700 /var/cache/github-backup
sudo install -d -o root -g root -m 0750 /etc/github-backup
sudo install -o root -g root -m 0644 \
  github-backup.example.toml /etc/github-backup/config.toml
sudoedit /etc/github-backup/environment
sudo chmod 600 /etc/github-backup/environment
```

Put `GITHUB_TOKEN=...` in the environment file. Set container paths in the config:

```toml
[backup]
destination = "/backups"
cache_dir = "/cache"
```

Preflight and run:

```bash
sudo docker run --rm \
  --env-file /etc/github-backup/environment \
  --mount type=bind,src=/etc/github-backup/config.toml,dst=/config.toml,readonly \
  --mount type=bind,src=/srv/github-backup,dst=/backups \
  --mount type=bind,src=/var/cache/github-backup,dst=/cache \
  "$BACKUP_IMAGE" --config /config.toml check

sudo docker run --rm --name github-backup \
  --env-file /etc/github-backup/environment \
  --mount type=bind,src=/etc/github-backup/config.toml,dst=/config.toml,readonly \
  --mount type=bind,src=/srv/github-backup,dst=/backups \
  --mount type=bind,src=/var/cache/github-backup,dst=/cache \
  "$BACKUP_IMAGE" --config /config.toml --json-logs run
```

The image uses UID/GID `10001`. Do not make the S3 cache ephemeral: it holds mirrors that make later Git transfers incremental.

## Configuration

```bash
cp github-backup.example.toml github-backup.toml
chmod 600 github-backup.toml
${EDITOR:-vi} github-backup.toml
github-backup --config github-backup.toml check
```

`./github-backup.toml` loads automatically. Precedence is CLI → supported non-secret environment variables → TOML → defaults.

### Complete TOML example

```toml
[backup]
storage = "filesystem"               # filesystem or s3
destination = "/mnt/backups/github"  # local disk or mounted NAS
cache_dir = "/var/cache/github-backup" # persistent S3 working cache
scope = "accessible"                 # accessible, owned, organizations
organizations = []                    # required for organizations scope
mode = "incremental"                  # incremental or snapshot
include_gists = true
additional_data = []                  # wikis, metadata, releases, actions, lfs
workers = 4                            # 1..32
git_timeout_seconds = 3600
git_max_retries = 3
verify_mirrors = true
lock_timeout_seconds = 0               # fail immediately if another run owns lock
minimum_free_space_gib = 1.0
allow_insecure_permissions = false
snapshot_retention = 0                 # 0 keeps every snapshot

[github]
api_url = "https://api.github.com"
api_version = "2022-11-28"
token_env = "GITHUB_TOKEN"
timeout_seconds = 30
max_retries = 5
allow_insecure_http = false

[s3]
bucket = ""
prefix = "github-backup"
# endpoint_url = "https://s3.example.com"
# region = "eu-central-1"
# storage_class = "STANDARD_IA"
server_side_encryption = "AES256"
lock_lease_seconds = 900                # minimum 60
allow_insecure_endpoint = false
```

Supported environment overrides are `GITHUB_BACKUP_DESTINATION`, `GITHUB_BACKUP_STORAGE`, `GITHUB_BACKUP_SCOPE`, `GITHUB_BACKUP_MODE`, and `GITHUB_BACKUP_CACHE_DIR`. Standard AWS SDK credential/profile/role variables are supported. Prefer workload roles over long-lived S3 keys.

For GitHub Enterprise Server:

```toml
[github]
api_url = "https://github.example.com/api/v3"
```

Install the internal CA in the host/container trust store. Do not disable TLS verification; HTTP overrides exist only for isolated testing.

## Storage backends

### Filesystem or NAS

```bash
github-backup run --destination /mnt/nas/github
```

NAS storage must support reliable locking, same-directory atomic rename, and normal file semantics. Ensure it is mounted before starting. Mode `0700` is required by default; use `allow_insecure_permissions = true` only after confirming equivalent NAS ACLs. Encrypt storage and replicate it independently.

```text
backups/
├── manifests/{RUN_ID.json,latest.json}
├── metadata/{repos,gists}/...
├── mirrors/
│   ├── repos/OWNER/REPOSITORY.git/
│   ├── wikis/OWNER/REPOSITORY.wiki.git/
│   └── gists/GIST_ID.git/
├── supplemental/latest/repos/OWNER/REPOSITORY/*.tar.gz
└── snapshots/RUN_ID/...              # snapshot mode
```

### S3-compatible storage

```bash
python -m pip install '.[s3]'
github-backup run --storage s3 \
  --s3-bucket company-backups \
  --s3-prefix github/engineering \
  --s3-region eu-central-1 \
  --cache-dir /var/cache/github-backup
```

Add `--s3-endpoint-url https://...` for MinIO, Ceph, Backblaze B2, or another compatible service. A prefix-scoped identity generally needs `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, multipart upload permissions, and KMS permissions when applicable.

Incremental S3 mode updates persistent local mirrors and uploads `latest` only when refs changed or an object is absent. Snapshot mode uploads immutable artifacts every run. Enable bucket versioning, object lock, replication, and lifecycle policies as required. Retention needs list/delete permissions.

### Incremental and snapshot retention

Incremental mode represents current upstream state; pruning means it is not historical retention. Snapshot mode creates self-contained bundles (or `.empty.tar.gz` for empty repositories):

```bash
github-backup run --mode snapshot --snapshot-retention 30 \
  --destination /mnt/backups/github
```

Retention runs only after a successful snapshot. `0` keeps all snapshots.

## Command-line reference

Help requires no config and no token:

```bash
github-backup help
github-backup help run
github-backup help list
github-backup help check
github-backup help verify
github-backup --help
github-backup run --help
```

| Command | Purpose | Token |
|---|---|---:|
| `run` | Discover and create/update backups. | yes |
| `list` | Preview resources without writing backups. | yes |
| `check` | Validate config, Git/LFS, authentication, discovery, and storage. | yes |
| `verify` | Verify local mirrors, bundles, archives, and digests. | no |
| `help [COMMAND]` | General or command-specific help. | no |

Global options: `--version`, `--config PATH`, `--verbose`, and `--json-logs`.

Examples:

```bash
github-backup run --scope owned --no-gists --destination /mnt/backups/github

github-backup run --scope organizations \
  --organization example-inc --organization second-org \
  --mode snapshot --destination /mnt/backups/github

github-backup --json-logs run --include all --destination /mnt/backups/github
github-backup list --json >github-resources.json
```

`github-backup help run` is the authoritative option list for the installed version.

## Test a deployment

Run these in order before scheduling:

```bash
# 1. Safe preflight.
github-backup --config /etc/github-backup/config.toml check

# 2. Review scope.
github-backup --config /etc/github-backup/config.toml list
github-backup --config /etc/github-backup/config.toml list --json >/tmp/github-resources.json

# 3. First backup with detailed logs.
github-backup --config /etc/github-backup/config.toml --verbose run

# 4. Inspect manifest.
python3 -m json.tool /srv/github-backup/manifests/latest.json | less

# 5. Offline verification.
env -u GITHUB_TOKEN github-backup --config /etc/github-backup/config.toml verify

# 6. Restore smoke test.
restore_dir=$(mktemp -d)
git clone /srv/github-backup/mirrors/repos/OWNER/REPOSITORY.git "$restore_dir/REPOSITORY"
git -C "$restore_dir/REPOSITORY" log -1 --oneline
```

Inspect `coverage.json` in optional archives. `check --include all` checks the common path and Git LFS availability, but only `run --include all` exercises every optional API endpoint. Use a test destination first if volume is unknown.

## Automate backups with systemd

systemd is recommended on Debian/Ubuntu/Raspberry Pi OS for protected secrets, missed-run recovery, exit monitoring, and journald logs. This example assumes the production paths above.

Create the service:

```bash
sudoedit /etc/systemd/system/github-backup.service
```

Add:

```ini
[Unit]
Description=Back up GitHub repositories and service data
Documentation=https://github.com/drhdev/github-backup
Wants=network-online.target
After=network-online.target remote-fs.target
RequiresMountsFor=/srv/github-backup

[Service]
Type=oneshot
User=github-backup
Group=github-backup
WorkingDirectory=/var/lib/github-backup
EnvironmentFile=/etc/github-backup/environment
ExecStart=/opt/github-backup/.venv/bin/github-backup --config /etc/github-backup/config.toml --json-logs run
UMask=0077
TimeoutStartSec=infinity
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=6
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/srv/github-backup /var/cache/github-backup

[Install]
WantedBy=multi-user.target
```

Create the timer:

```bash
sudoedit /etc/systemd/system/github-backup.timer
```

Add:

```ini
[Unit]
Description=Run GitHub backup daily

[Timer]
OnCalendar=*-*-* 03:15:00
RandomizedDelaySec=15m
Persistent=true
Unit=github-backup.service

[Install]
WantedBy=timers.target
```

Enable and test:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now github-backup.timer
sudo systemctl start github-backup.service
sudo systemctl status github-backup.service
sudo systemctl status github-backup.timer
sudo systemctl list-timers github-backup.timer
```

Enable the timer, not the oneshot service. An inactive service after success is normal.

For a Docker installation, use the same timer and create a separate oneshot service whose `ExecStart` is the tested container command:

```ini
[Unit]
Description=Back up GitHub with the github-backup container
Wants=network-online.target docker.service
After=network-online.target docker.service remote-fs.target
RequiresMountsFor=/srv/github-backup

[Service]
Type=oneshot
ExecStart=/usr/bin/docker run --rm --name github-backup --read-only --cap-drop=ALL --security-opt=no-new-privileges --env-file /etc/github-backup/environment --mount type=bind,src=/etc/github-backup/config.toml,dst=/config.toml,readonly --mount type=bind,src=/srv/github-backup,dst=/backups --mount type=bind,src=/var/cache/github-backup,dst=/cache ghcr.io/drhdev/github-backup:1.1.0 --config /config.toml --json-logs run
TimeoutStartSec=infinity
```

Save that as `/etc/systemd/system/github-backup-docker.service`, change the timer's `Unit=` to `github-backup-docker.service`, then reload and enable the timer as shown above. Pin a reviewed release or digest, keep secrets outside the image, and keep storage/cache persistent. `--name github-backup` also makes `docker logs -f github-backup` available while the scheduled container is running.

## Logs, monitoring, and exit codes

Application logs go to **stderr**. Final backup/discovery/verification summaries go to **stdout**:

```bash
github-backup --config config.toml run \
  >/var/log/github-backup-summary.log \
  2>/var/log/github-backup.log
```

`--json-logs` writes one structured JSON log record per stderr line; final stdout remains text. Logs and manifests are sensitive metadata.

```bash
sudo journalctl -u github-backup.service -f
sudo journalctl -u github-backup.service -n 200 --no-pager
sudo journalctl -u github-backup.service --since today
sudo journalctl -u github-backup.service -p warning..alert

sudo docker logs -f github-backup
sudo docker inspect github-backup --format '{{.State.ExitCode}}'
```

| Exit | Meaning | Response |
|---:|---|---|
| `0` | Complete success. | Record success; continue verification and restore testing. |
| `1` | Fatal config, auth, discovery, dependency, lock, or storage error. | Alert and inspect stderr/journal. |
| `2` | Run finished with failed resources or partial components. | Alert and inspect `latest.json`. |
| `130` | Interrupted. | Confirm the next run completes. |

Monitor time since last successful manifest, failed/partial counts, unexpected repository-count changes, disk/cache capacity, token expiry, S3/KMS errors, and restore-test results. GitHub documents current [REST API rate-limit behavior](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api); the client responds to relevant headers and bounded retries.

## Restore and disaster recovery

Restore a mirror:

```bash
git clone /mnt/backups/github/mirrors/repos/OWNER/REPOSITORY.git restored-repository
```

Restore all refs to a **new** remote:

```bash
git clone --mirror /mnt/backups/github/mirrors/repos/OWNER/REPOSITORY.git recovered.git
git -C recovered.git push --mirror https://github.com/OWNER/NEW-REPOSITORY.git
```

`push --mirror` can overwrite/delete destination refs. Use only an approved recovery target.

Restore a snapshot:

```bash
git clone /mnt/backups/github/snapshots/RUN_ID/repos/OWNER/REPOSITORY.git.bundle \
  restored-repository

aws s3 cp s3://BUCKET/PREFIX/snapshots/RUN_ID/repos/OWNER/REPOSITORY.git.bundle .
git clone ./REPOSITORY.git.bundle restored-repository
```

Verify local data:

```bash
github-backup verify --destination /mnt/backups/github
```

Verification checks mirrors, bundles, tar containers, and recorded supplementary SHA-256 digests. S3 objects must currently be downloaded into a filesystem layout first. Wikis use the same Git restore process. Metadata archives preserve records but do not automatically replay issues, pull requests, identities, webhooks, secrets, or settings. Test an organization-specific importer before an incident, and treat downloaded assets as untrusted.

Use the 3-2-1 principle where practical: three copies, two storage types, one off-site or isolated. Schedule both single-repository and wider recovery exercises.

## Updating and troubleshooting

Test updates against a non-production destination and replace `1.2.0` with the reviewed target
release. Source install:

```bash
cd /opt/github-backup
sudo git fetch --tags --prune
sudo git checkout --detach v1.2.0
sudo /opt/github-backup/.venv/bin/python -m pip install '/opt/github-backup[s3]'
sudo /opt/github-backup/.venv/bin/github-backup --version
sudo systemctl start github-backup.service
```

Docker update:

```bash
sudo docker pull ghcr.io/drhdev/github-backup:1.2.0
sudo docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  ghcr.io/drhdev/github-backup:1.2.0 --version
```

Keep the previous version until a backup, verify, and restore smoke test pass.

| Symptom | Cause/resolution |
|---|---|
| Token not set / `401` | Export the configured token variable; rotate expired/revoked tokens. |
| `403` | Check permissions, repository selection, SSO, organization approval, and rate limits. |
| Missing repositories | Compare `list --json` with inventory; review token owner/selection and scope. |
| Partial component | Inspect its `coverage.json` and the manifest for unavailable endpoints. |
| Broad permissions rejected | Correct owner and `chmod 700`; override only with equivalent reviewed NAS ACLs. |
| Lock held | Confirm another process/host is not active; do not casually delete live locks. |
| Low free space | Expand/free storage or set a justified threshold with external monitoring. |
| `git-lfs` missing | Install it or omit the LFS component. |
| S3 access denied | Review bucket/prefix IAM, endpoint, KMS, and multipart permissions. |
| Docker cannot write | Make mounts writable by UID/GID `10001`; never make them world-writable. |
| NAS absent | Fix the mount and `RequiresMountsFor`; confirm with `findmnt`. |

Use `github-backup --config /etc/github-backup/config.toml --verbose check` for safe diagnostics. Do not enable Git curl tracing around production credentials.

## Releases, artifacts, and supply-chain verification

Future versions and changelog entries are managed automatically from Conventional Commits. A
Release Please pull request updates the SemVer version and [`CHANGELOG.md`](CHANGELOG.md). After it
is reviewed and merged, the release workflow:

1. creates a draft tag/release;
2. repeats linting, strict typing, tests, coverage, and dependency auditing on that exact tag;
3. builds and installs the wheel in a clean environment and rejects container images with known,
   fixable High/Critical vulnerabilities;
4. creates wheel and source archives, an SPDX JSON SBOM, and `SHA256SUMS`;
5. records GitHub/Sigstore provenance attestations;
6. publishes `linux/amd64` and `linux/arm64` images with OCI provenance and SBOMs to GHCR;
7. publishes the GitHub release only after every artifact job succeeds.

Verify downloaded files from one release directory:

```bash
sha256sum --check SHA256SUMS
gh attestation verify github_backup-1.1.0-py3-none-any.whl \
  --repo drhdev/github-backup
```

Maintainers should read [`docs/RELEASING.md`](docs/RELEASING.md) for the one-time GitHub App and
repository-rules setup, the first release, version rules, and failed-release recovery. Contributors
must use the commit conventions in [`CONTRIBUTING.md`](CONTRIBUTING.md); normal releases must not
be assembled or tagged manually.

## Production checklist

- [ ] Dedicated, read-only, least-privilege token with owner and rotation date.
- [ ] Organization approval/SSO confirmed; `list --json` matches authoritative inventory.
- [ ] Secrets in a protected environment file or workload secret manager.
- [ ] Dedicated unprivileged account/container user and restrictive ACLs.
- [ ] Filesystem/NAS encryption or S3 encryption/bucket policy configured.
- [ ] Snapshot/versioning/object-lock policy chosen for deleted-ref retention.
- [ ] Capacity monitoring and explicit retention policy enabled.
- [ ] Alerts on exits `1`/`2`, stale manifests, and count changes.
- [ ] Timer handles missed runs and overlapping runs remain locked out.
- [ ] `verify` and a real restore drill pass on schedule.
- [ ] Dependencies, OS, base image, and application receive updates.
- [ ] Official GHES appliance backup remains enabled where applicable.

The `1.x` series is designed for stable production use and will continue to evolve. Planned improvements include broader documented service coverage, stronger restore tooling, object-store verification, operational metrics, and GitHub API compatibility. Production-ready does not mean every GitHub API is covered or remove the need for operator monitoring.

## Contributing, security, and license

Contributions are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) contains the development commands,
test requirements, pull-request checklist, Conventional Commit rules, and automated release
process. Community participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), and
operational help is described in [`SUPPORT.md`](SUPPORT.md).

Use [GitHub Issues](https://github.com/drhdev/github-backup/issues) for non-sensitive reproducible
bugs and proposals. Read [`SECURITY.md`](SECURITY.md) before deploying or reporting a
vulnerability. Report credential/private-data vulnerabilities through GitHub's private
vulnerability reporting, not a public issue.

`github-backup` is licensed under the [GNU General Public License v3.0](LICENSE). Contributions are
provided under the same license.
