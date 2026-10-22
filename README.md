# github-backup

`github-backup` discovers every repository visible to an authenticated GitHub user and backs up its complete Git object database and refs. It also backs up the authenticated user's public and secret gists. Version 1.1 can optionally add repository wikis, pull requests and other API metadata, release assets, Actions artifacts and logs, and Git LFS objects. It is designed for unattended, security-sensitive production operation.

The defaults match the safest low-overhead setup:

- local disk or a mounted NAS;
- every repository the token can access (owned, collaborator, and organization membership);
- incremental bare mirrors;
- public and secret gists included;
- additional GitHub service data disabled unless explicitly selected.

The user can select filesystem or S3-compatible storage, repository scope, and incremental or immutable snapshot mode at execution time or in TOML.

## Backup coverage

The default run backs up:

- all Git refs and objects exposed by each repository's clone endpoint;
- owned, collaborator, and organization repositories allowed by the token;
- public and private repositories;
- public and secret gists belonging to the authenticated user;
- resource metadata and a detailed manifest for every run.

Normal Git commits, branches, tags, notes, and pull-request refs exposed by the clone endpoint are included. Submodule pointers are included; the referenced repository is backed up when the token can discover it independently.

Additional components are opt-in with repeatable `--include` flags or `backup.additional_data`:

| Component | Data | Restore format and cost |
|---|---|---|
| `wikis` | Wiki Git history and refs | Separate bare mirror/bundle. Low overhead; an enabled but empty wiki is recorded as skipped. |
| `metadata` | Repository settings plus issues, issue events/comments, PRs, reviews/review comments, labels, milestones, branches, rulesets, collaborators, teams, deploy keys, webhook metadata, deployments, environments, secret names, and accessible security alerts/advisories | JSON/JSONL in a checksummed `tar.gz`. API-intensive; secret values are never available. |
| `releases` | Release metadata and every accessible binary release asset | JSONL plus binary files in `tar.gz`; may consume substantial bandwidth and storage. |
| `actions` | Metadata and downloads for non-expired Actions artifacts and completed-run logs still retained by GitHub | JSONL plus ZIP files in `tar.gz`; historical files already expired upstream cannot be recovered. |
| `lfs` | All reachable Git LFS objects | `tar.gz`; requires `git-lfs` and can be much larger than the Git repository. |

Select individual components:

```bash
github-backup run \
  --include wikis \
  --include metadata \
  --destination /mnt/backups/github
```

Or select every supported optional component:

```bash
github-backup run --include all --destination /mnt/backups/github
```

`all` means all data classes this application can read and archive; it does not override GitHub's API restrictions. Every component archive contains `coverage.json`. Endpoints unavailable because of token permissions, feature availability, or GitHub edition are listed there and in the run manifest. Such a component has status `partial`, and the process returns exit code `2`, so monitoring cannot mistake a coverage gap for a complete backup.

### Enterprise completeness boundary

A token-based export cannot be a byte-for-byte backup of GitHub.com or GitHub Enterprise Cloud. GitHub does not return secret values, expired artifacts, deleted objects, all internal platform state, or every package-registry blob through a single backup API. Package contents should additionally be mirrored with the native npm, Maven, NuGet, RubyGems, or OCI/container registry client; secret values must be backed up at their source secret manager.

For GitHub Enterprise Server, a complete instance backup—including configuration and user data—must use the GitHub Enterprise Server Backup Service (preferred on GHES 3.17+) or the compatible legacy Backup Utilities. This project is a portable, token-authenticated complement and is useful for independent repository-level recovery, but it must not claim appliance-level completeness. See GitHub's [backup service documentation](https://docs.github.com/en/enterprise-server/admin/backing-up-and-restoring-your-instance/about-the-backup-service-for-github-enterprise-server) and [restore documentation](https://docs.github.com/en/enterprise-server/admin/backing-up-and-restoring-your-instance/restoring-from-a-backup).

## Requirements

- Python 3.11 or later;
- Git 2.30 or later;
- Git LFS when `--include lfs` is selected;
- a GitHub token;
- `boto3` only when using S3-compatible storage.

For GitHub.com, a classic personal access token normally needs `repo` for private repositories and `gist` for secret gists. Fine-grained tokens must be granted access to the intended repositories with read access to repository metadata and contents. Organization SAML/SSO policies may require the token to be authorized separately.

Optional data requires matching read permissions—for example Actions read for artifacts/logs, administration-related read permissions for settings, and security-event access for alert data. Classic PATs used for broad organization backups commonly require `repo`, `read:org`, `read:packages`, and `security_events`; grant only the components you actually enable. The manifest is the authoritative record of what the token could retrieve.

Never put the token in the TOML file or a command argument. The application reads it from an environment variable, injects it into Git only for the matching HTTPS host, refuses authenticated redirects, and does not save it in remotes, logs, metadata, or manifests. Plain HTTP is rejected unless an explicit testing-only override is enabled.

## Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

For S3 support use `python -m pip install '.[s3]'`. For development use `python -m pip install '.[dev,s3]'`.

## Quick start

Export the token through your shell, secret manager, or workload secret injection:

```bash
export GITHUB_TOKEN='github_pat_...'
```

Preview what the token can access and run the default incremental backup:

```bash
github-backup list
github-backup run --destination /mnt/backups/github
```

Back up only repositories owned by the authenticated user:

```bash
github-backup run --scope owned --destination /mnt/backups/github
```

Back up selected organizations as an immutable point-in-time snapshot:

```bash
github-backup run \
  --scope organizations \
  --organization example-inc \
  --organization second-org \
  --mode snapshot \
  --destination /mnt/backups/github
```

Upload to AWS S3:

```bash
github-backup run \
  --storage s3 \
  --s3-bucket company-backups \
  --s3-prefix github/engineering \
  --cache-dir /var/cache/github-backup
```

For MinIO, Ceph, Backblaze B2, or another S3-compatible service, also pass `--s3-endpoint-url`. Custom endpoints must use HTTPS by default; `--allow-insecure-s3-endpoint` is available only for deliberately isolated test or LAN deployments. Standard AWS credential environment variables, profiles, instance roles, and workload identities are handled by the AWS SDK.

## Configuration

Copy [`github-backup.example.toml`](github-backup.example.toml) to `github-backup.toml`, edit it, and run:

```bash
github-backup --config github-backup.toml run
```

When `./github-backup.toml` exists, it is loaded automatically. Command-line flags override TOML. The following environment variables override basic settings: `GITHUB_BACKUP_DESTINATION`, `GITHUB_BACKUP_STORAGE`, `GITHUB_BACKUP_SCOPE`, `GITHUB_BACKUP_MODE`, and `GITHUB_BACKUP_CACHE_DIR`.

The token variable name itself is configurable with `github.token_env`; its default is `GITHUB_TOKEN`.

### Runtime choices

| Decision | Values | Default | Tradeoff |
|---|---|---|---|
| Storage | `filesystem`, `s3` | `filesystem` | Filesystem/NAS mirrors update in place efficiently. S3 uses a persistent local mirror cache and uploads self-contained artifacts. |
| Scope | `accessible`, `owned`, `organizations` | `accessible` | Accessible is comprehensive. Owned is narrower. Organizations requires one or more `--organization` values. |
| Mode | `incremental`, `snapshot` | `incremental` | Incremental keeps the latest mirror and prunes refs deleted upstream. Snapshot creates immutable point-in-time artifacts and uses more storage. |
| Gists | `--gists`, `--no-gists` | included | Secret gists require suitable token access. |
| Extra data | repeatable `--include`, or `--include all` | none | Adds coverage, API calls, transfer time, and storage. Components remain independent in the manifest. |

Snapshot retention defaults to unlimited (`0`). Set `backup.snapshot_retention` or `--snapshot-retention` to retain only the newest successful snapshots. For S3, retention requires list and delete permissions on the configured prefix. Bucket lifecycle policies remain recommended as a second control.

On S3, Git cannot update a remote bundle in place. A persistent local bare-mirror cache still makes GitHub downloads incremental. The application only packages and uploads a new `latest` artifact when refs changed or the remote object is absent. Snapshot mode uploads an immutable artifact on every run.

## Storage layout

Filesystem and NAS destinations use this layout:

```text
backups/
├── manifests/
│   ├── latest.json
│   └── 20261021T120000.000000Z.json
├── metadata/
│   ├── repos/OWNER/REPOSITORY.json
│   └── gists/GIST_ID.json
├── mirrors/
│   ├── repos/OWNER/REPOSITORY.git/
│   ├── wikis/OWNER/REPOSITORY.wiki.git/
│   └── gists/GIST_ID.git/
├── supplemental/latest/repos/OWNER/REPOSITORY/
│   ├── metadata.tar.gz
│   ├── releases.tar.gz
│   ├── actions.tar.gz
│   └── lfs.tar.gz
└── snapshots/                      # snapshot mode only
    └── RUN_ID/...                  # Git bundles and component archives
```

Empty repositories have no refs and cannot be represented by a Git bundle, so snapshot mode stores them as `.empty.tar.gz` archives. Writes to JSON and local snapshot artifacts use same-filesystem atomic replacement. Local/NAS runs use a process lock; S3 runs additionally use a renewable, conditional ETag lease so separate hosts cannot update the same prefix concurrently. Expired S3 leases are recovered with compare-and-swap deletion.

New local files are created under a `0077` umask, and the destination/cache must not be group- or world-accessible. Existing destinations with broader Unix mode bits are rejected. Set `allow_insecure_permissions=true` only when access is deliberately controlled by another mechanism such as a reviewed NAS ACL. The application also checks configurable minimum free space before starting.

Every completed run writes an immutable manifest and replaces `latest.json`. A partial component or per-resource failure returns exit code `2`, records the exact result in the manifest, and leaves successful backups usable. Skipped resources such as an enabled but empty wiki are recorded without failing the run. Configuration, authentication, discovery, or storage setup failures return exit code `1`.

## Restore

Restore from an incremental filesystem mirror:

```bash
git clone /mnt/backups/github/mirrors/repos/OWNER/REPOSITORY.git restored-repository
```

Restore all refs into an existing empty remote:

```bash
git clone --mirror /mnt/backups/github/mirrors/repos/OWNER/REPOSITORY.git recovered.git
git -C recovered.git push --mirror https://github.com/OWNER/NEW-REPOSITORY.git
```

Restore from a snapshot bundle:

```bash
git clone /mnt/backups/github/snapshots/RUN_ID/repos/OWNER/REPOSITORY.git.bundle restored-repository
```

For S3, download the selected `.bundle` first and use the same command. Extract an `.empty.tar.gz` archive directly when restoring an empty repository. Always test restores periodically; a successful upload is not a substitute for a restore drill.

Verify every local mirror and snapshot artifact without requiring a GitHub token:

```bash
github-backup verify --destination /mnt/backups/github
```

This runs Git connectivity checks on mirrors, verifies bundles against their corresponding mirrors, and validates empty-repository and supplementary archives.

Wiki mirrors and bundles use the same Git restore process. Supplementary archives are portable `tar.gz` files with JSON/JSONL metadata and original binary downloads. They deliberately preserve source records rather than attempting destructive automatic recreation through GitHub APIs; restoration should be rehearsed with an organization-specific importer and identity mapping. `verify` validates each tar container and checks its SHA-256 digest against the latest manifest.

## Scheduling

Use an external scheduler so credentials remain under the platform's secret-management policy. A systemd service can execute:

```ini
[Service]
Type=oneshot
EnvironmentFile=/etc/github-backup/environment
ExecStart=/opt/github-backup/.venv/bin/github-backup --config /etc/github-backup/config.toml run
User=github-backup
Group=github-backup
PrivateTmp=true
NoNewPrivileges=true
```

Run it from a systemd timer, Kubernetes CronJob, CI scheduler, or equivalent. Do not place the token directly in a crontab command.

## Container

```bash
docker build -t github-backup:local .
docker run --rm \
  -e GITHUB_TOKEN \
  -v /mnt/backups/github:/backups \
  github-backup:local run --destination /backups
```

The image runs as an unprivileged user. Ensure the mounted directory is owned by UID/GID `10001`, is writable, and normally has mode `0700`, or adapt ownership to your container platform.

## Operational behavior

- GitHub API calls are paginated; discovery is serialized to reduce secondary-rate-limit pressure.
- Optional repository components use a bounded worker pool. JSON list responses are streamed to JSONL so large issue or pull-request histories do not need to reside in memory.
- `Retry-After` and primary rate-limit reset headers are honored; transient API failures use bounded retries.
- Git transfers run in a bounded worker pool and retry transient failures with backoff. Four workers and three Git retries are the defaults; lower concurrency on a slow NAS or constrained GitHub Enterprise Server.
- Initial clones are written to a temporary sibling and atomically renamed only after Git succeeds.
- Existing backups are verified as bare repositories and checked with `git fsck --connectivity-only` after update.
- Deleted upstream refs are pruned in incremental mode. Use snapshots when you must retain deleted branches or tags.
- S3 uploads use adaptive retries, AES-256 server-side encryption by default, and renewable distributed leases. KMS or an explicitly disabled encryption setting can be configured when required.
- Human-readable logs are the default; `--json-logs` emits one JSON object per line.
- Application dependencies are resolved by `uv.lock`; CI uses frozen installs, audits dependencies, and pins third-party Actions to immutable commits.

## Development

```bash
python -m unittest discover -s tests -v
ruff check .
mypy src
pip-audit
```

The integration tests create real local repositories and exercise initial mirror creation, incremental updates, bundle verification, manifest writing, and credential non-persistence.

## License

GNU General Public License v3.0. See [`LICENSE`](LICENSE).
