# github-backup

`github-backup` discovers every repository visible to an authenticated GitHub user and backs up its complete Git object database and refs. It also backs up the authenticated user's public and secret gists.

The defaults match the safest low-overhead setup:

- local disk or a mounted NAS;
- every repository the token can access (owned, collaborator, and organization membership);
- incremental bare mirrors;
- public and secret gists included.

The user can select filesystem or S3-compatible storage, repository scope, and incremental or immutable snapshot mode at execution time or in TOML.

## What is backed up

- all Git refs and objects exposed by each repository's clone endpoint;
- owned, collaborator, and organization repositories allowed by the token;
- public and private repositories;
- public and secret gists belonging to the authenticated user;
- resource metadata and a detailed manifest for every run.

GitHub issues, pull-request discussions, releases, Actions artifacts, packages, wikis, and Git LFS payloads are separate services and are not included in version `0.1`. Normal Git commits, branches, tags, notes, and pull-request refs exposed by the clone endpoint are included. Submodule pointers are included; the referenced repository is backed up when the token can discover it independently.

## Requirements

- Python 3.11 or later;
- Git 2.30 or later;
- a GitHub token;
- `boto3` only when using S3-compatible storage.

For GitHub.com, a classic personal access token normally needs `repo` for private repositories and `gist` for secret gists. Fine-grained tokens must be granted access to the intended repositories with read access to repository metadata and contents. Organization SAML/SSO policies may require the token to be authorized separately.

Never put the token in the TOML file or a command argument. The application reads it from an environment variable, injects it into Git only for the matching HTTPS host, and does not save it in remotes, logs, metadata, or manifests.

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

For MinIO, Ceph, Backblaze B2, or another S3-compatible service, also pass `--s3-endpoint-url`. Standard AWS credential environment variables, profiles, instance roles, and workload identities are handled by the AWS SDK.

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
│   └── gists/GIST_ID.git/
└── snapshots/                      # snapshot mode only
    └── RUN_ID/.../*.git.bundle
```

Empty repositories have no refs and cannot be represented by a Git bundle, so snapshot mode stores them as `.empty.tar.gz` archives. Writes to JSON and local snapshot artifacts use same-filesystem atomic replacement. A process lock prevents overlapping runs against the same destination/cache. Stale local locks are recovered when their process no longer exists.

Every completed run writes an immutable manifest and replaces `latest.json`. A partial per-resource failure returns exit code `2`, records the failure in the manifest, and leaves successful backups usable. Configuration, authentication, discovery, or storage setup failures return exit code `1`.

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

The image runs as an unprivileged user. Ensure the mounted directory is writable by UID/GID `10001` or adapt ownership to your container platform.

## Operational behavior

- GitHub API calls are paginated and serialized to reduce secondary-rate-limit pressure.
- `Retry-After` and primary rate-limit reset headers are honored; transient API failures use bounded retries.
- Git transfers run in a bounded worker pool. Four workers is the default; lower it on a slow NAS or constrained GitHub Enterprise Server.
- Initial clones are written to a temporary sibling and atomically renamed only after Git succeeds.
- Existing backups are verified as bare repositories before update.
- Deleted upstream refs are pruned in incremental mode. Use snapshots when you must retain deleted branches or tags.
- S3 uploads use the AWS SDK's adaptive retries and optional server-side encryption/storage-class settings.
- Human-readable logs are the default; `--json-logs` emits one JSON object per line.

## Development

```bash
python -m unittest discover -s tests -v
ruff check .
mypy src
```

The integration tests create real local repositories and exercise initial mirror creation, incremental updates, bundle verification, manifest writing, and credential non-persistence.

## License

GNU General Public License v3.0. See [`LICENSE`](LICENSE).
