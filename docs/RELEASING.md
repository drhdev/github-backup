# Release and changelog automation

This project uses Release Please, Conventional Commits, Semantic Versioning, immutable action
pins, draft releases, provenance attestations, SBOMs, checksums, and GitHub Container Registry.
Normal releases require no local version edits, tags, or changelog edits.

## One-time repository setup

### 1. Create a least-privilege GitHub App

A GitHub App token is used instead of a long-lived personal access token. Events created by the
default `GITHUB_TOKEN` do not start additional workflow runs; the App token ensures that the
release pull request receives the same required CI and security checks as every other pull request.

In the repository or organization settings, create a GitHub App named for release automation:

1. Disable webhooks unless your organization requires them.
2. Give it repository **Contents: Read and write**, **Issues: Read and write**, and **Pull
   requests: Read and write**. Release Please uses the Issues permission for release labels.
3. Do not grant administration, secrets, Actions, or organization permissions.
4. Install the App only on this repository.
5. Generate a private key.
6. Add the App's client ID as the repository variable `RELEASE_APP_CLIENT_ID`.
7. Add the complete PEM private key as the Actions secret `RELEASE_APP_PRIVATE_KEY`.
8. Delete the downloaded private-key file after storing it in the secret manager.

The workflow explicitly binds the short-lived installation token to this repository and requests
only the three permissions above. Its normal `GITHUB_TOKEN` does not receive the App's issue or
pull-request write permissions. Rotate the App key according to organizational policy.

### 2. Configure Actions and package permissions

In **Settings > Actions > General**:

- allow the pinned actions used in `.github/workflows/`;
- permit GitHub Actions to create pull requests if organization policy requires the setting;
- keep the default workflow token read-only; each job declares its minimal additional permissions.

In **Settings > Packages**, allow Actions to publish the repository's GHCR package. After the
first publish, make the package public when public unauthenticated pulls are intended and link it
to this repository.

Enable private vulnerability reporting, Dependabot alerts, secret scanning, push protection, and
code scanning where the repository plan supports them.

### 3. Protect `main`

Use a ruleset or branch protection rule that requires pull requests, resolves review threads,
blocks force pushes/deletion, and requires these checks:

- `CI / quality`
- `CI / test (3.11)`, `CI / test (3.12)`, and `CI / test (3.13)`
- `CI / docker`
- `Security / codeql`
- `Security / dependency-review` for pull requests

Require CODEOWNER review when more than one trusted maintainer is available. Requiring signed
commits, linear history, and successful deployments is an organizational policy choice.

## First production release

The repository is bootstrapped at version `1.1.0`. After this automation is merged to `main`, open
**Actions > Release > Run workflow**, enter `v1.1.0`, and run it. Manual dispatch will create a
draft release/tag when absent, build and verify the exact tag, publish Python artifacts and the
multi-architecture container, and only then publish the GitHub release.

Do not reuse a version that has already been distributed with different contents. If `1.1.0` was
published outside this repository, change `pyproject.toml`, `.release-please-manifest.json`, and the
bootstrap changelog entry to the next unused SemVer before the first workflow dispatch.

## Normal release flow

1. Merge reviewed pull requests with Conventional Commit titles.
2. Release Please opens or updates one release pull request.
3. Review its version bump and generated `CHANGELOG.md` entry. Edit explanatory text in that PR if
   needed, while retaining the generated version files.
4. Merge the release pull request after all required checks pass.
5. Release Please creates a tag and draft GitHub release.
6. The same workflow checks linting, typing, tests, coverage, dependencies, package version, and a
   clean wheel installation. Trivy blocks known, fixable High/Critical OS or Python dependency
   vulnerabilities before the container is published.
7. It generates wheel/sdist artifacts, an SPDX JSON SBOM, `SHA256SUMS`, and GitHub/Sigstore
   provenance attestations.
8. It builds and publishes `linux/amd64` and `linux/arm64` images with OCI provenance and SBOMs to
   `ghcr.io/drhdev/github-backup`.
9. Only after both artifact jobs succeed does it upload assets and publish the draft release.

Version rules are:

| Commit | Version effect | Changelog |
|---|---:|---|
| `fix:` or `perf:` | Patch | Visible |
| `feat:` | Minor | Visible |
| `type!:` or `BREAKING CHANGE:` | Major | Visible |
| `docs:`, `build:`, `ci:`, `refactor:` | No bump by itself | Configured section |
| `test:` or `chore:` | No bump | Hidden by default |

Squash-merging is recommended so the pull-request title becomes the conventional commit on
`main`. Avoid manually editing `pyproject.toml`, `.release-please-manifest.json`, generated release
sections in `CHANGELOG.md`, or release tags.

## Failure and recovery

If package or container publication fails, the GitHub release remains a draft. Correct the cause,
then manually run the Release workflow with the existing tag. The workflow rebuilds from the tag,
replaces release assets, and publishes only after all jobs pass.

The manual recovery path refuses to modify a release that is already published. A published
version is immutable; fixes always receive a new patch release.

Never move or overwrite a published tag. For a defect in a published release, merge a fix and
publish a new patch version. A compromised release should be marked clearly, credentials rotated,
and a replacement version issued; deletion alone does not retract downloaded artifacts.

## Verification by consumers

Download all assets, then verify their checksums:

```bash
sha256sum --check SHA256SUMS
```

Verify GitHub provenance with GitHub CLI:

```bash
gh attestation verify github_backup-1.2.0-py3-none-any.whl \
  --repo drhdev/github-backup
```

Pin containers by digest for controlled deployments:

```bash
docker pull ghcr.io/drhdev/github-backup:1.2.0
docker image inspect ghcr.io/drhdev/github-backup:1.2.0 --format '{{index .RepoDigests 0}}'
```

The release page, package attestations, SBOM, and checksum file are the authoritative release
records. Locally generated files under `dist/` are development artifacts and are not releases.
