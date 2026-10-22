# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file is maintained by Release Please from Conventional Commits. Do not manually add a
release section; describe user-visible changes in commit messages and edit the generated release
pull request when additional context is useful.

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
