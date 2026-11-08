# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file is maintained by Release Please from Conventional Commits. Do not manually add a
release section; describe user-visible changes in commit messages and edit the generated release
pull request when additional context is useful.

## [1.4.0](https://github.com/drhdev/github-backup/compare/v1.3.0...v1.4.0) (2026-11-08)


### Features

* add secure responsive config UI and repository activity ([8131397](https://github.com/drhdev/github-backup/commit/813139770ec4bcedf3fa586293d7b7876e689b84))

## [1.3.0](https://github.com/drhdev/github-backup/compare/v1.2.0...v1.3.0) (2026-11-08)


### Features

* add browser-managed backup connections and repository scopes ([eaa2d96](https://github.com/drhdev/github-backup/commit/eaa2d96498f053d050ee15d55fec47e568e9fd79))

### Setup and migration

* Create multiple read-only GitHub Apps through the protected browser setup and connect personal or organization accounts without manual PEM handling.
* Select all repositories, specific repositories, all except selected repositories, or pause each connection. GitHub installation permissions remain the access limit.
* Configure and test DigitalOcean Spaces in setup; only the setup HTTPS URL and password are required in the environment for new deployments.
* Preserve existing copies of excluded/paused repositories. Failed connections alert while other connections continue backing up.
* Existing manual configuration remains supported. New browser connections start paused; select a backup scope before expecting code backups. Saved browser Spaces settings take precedence over legacy Spaces environment settings.
* See the [Coolify deployment guide](https://github.com/drhdev/github-backup/blob/v1.3.0/docs/COOLIFY.md) and [GitHub App setup guide](https://github.com/drhdev/github-backup/blob/v1.3.0/docs/GITHUB_APP.md).

## [1.2.0](https://github.com/drhdev/github-backup/compare/v1.1.2...v1.2.0) (2026-11-07)


### Features

* add Coolify deployment with versioned Spaces backups and monitoring ([3719df4](https://github.com/drhdev/github-backup/commit/3719df484ed344a56d96bb76461e258797a359fc))
* support GitHub App backups across accounts and organizations ([ea7c5a0](https://github.com/drhdev/github-backup/commit/ea7c5a0cfe990d04614295b115337fd69b670d06))


### Bug Fixes

* patch container base and target Ubuntu amd64 deployments ([145e7b2](https://github.com/drhdev/github-backup/commit/145e7b2f67924a43b41195e17724ee5f47e9fa6c))


### Build System

* **deps:** update GitHub Actions dependencies ([#13](https://github.com/drhdev/github-backup/issues/13)) ([4a5eeb8](https://github.com/drhdev/github-backup/commit/4a5eeb8c73e737030d8faae153c22fdb6a6f7da8))
* **deps:** update Python dependencies and synchronize lockfile ([#11](https://github.com/drhdev/github-backup/issues/11)) ([cdec103](https://github.com/drhdev/github-backup/commit/cdec103d350a18177d330e6364261c7b4eaac377))

## [1.1.2](https://github.com/drhdev/github-backup/compare/v1.1.1...v1.1.2) (2026-10-22)

### Security

* restrict release automation credentials to their required consumers and repository, remove unnecessary workflow-token permissions, and ignore local credential artifacts ([2ba78b2](https://github.com/drhdev/github-backup/commit/2ba78b2a33e93590bee21929800aa77c9233ff14))

### Build System

* **deps-dev:** update grouped Python dependencies ([939abdb](https://github.com/drhdev/github-backup/commit/939abdbddc44df26855b30ecb7d556ad887378a5))
* **deps:** update CodeQL Action to v4.39.1 ([7b4a93b](https://github.com/drhdev/github-backup/commit/7b4a93b219ca9c5fb72f48a62ddb950bd3f6e61b))

## [1.1.1](https://github.com/drhdev/github-backup/compare/v1.1.0...v1.1.1) (2026-10-22)


### Continuous Integration

* harden workflow configuration ([8dcf95c](https://github.com/drhdev/github-backup/commit/8dcf95cd734b36e84b80eac3b5b63adfa84510d9))

## [1.1.0](https://github.com/drhdev/github-backup/releases/tag/v1.1.0) (2026-10-22)

### Features

- Back up repositories and gists visible to a GitHub token in incremental or snapshot mode.
- Support local filesystems, NAS mounts, and S3-compatible object storage.
- Optionally archive wikis, repository metadata, pull requests, issues, releases, Actions data,
  Git LFS objects, settings, and accessible security metadata.
- Support GitHub.com and GitHub Enterprise Server API endpoints.
- Add verification, preflight checks, structured logs, locking, retention, and hardened defaults.

### Documentation

- Add complete installation, configuration, scheduling, monitoring, restore, security, and
  troubleshooting guidance.

### Security

- Run the container as an unprivileged user, remove unused runtime package tooling, and gate
  release images on actionable High/Critical vulnerability findings.
- Add CodeQL, dependency review, Dependabot, OpenSSF Scorecard, immutable workflow pins, SBOMs,
  checksums, and GitHub/Sigstore provenance attestations.

### Build System

- Automate Semantic Versioning, changelog pull requests, draft releases, verified Python
  artifacts, and multi-architecture GHCR images with Release Please and GitHub Actions.
- Add built-in, token-free `github-backup help [COMMAND]` documentation.

[1.1.0]: https://github.com/drhdev/github-backup/releases/tag/v1.1.0
