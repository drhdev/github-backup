# Contributing

Thank you for improving `github-backup`. Contributions should keep backup integrity, least
privilege, predictable recovery, and backward compatibility ahead of convenience.

## Before opening a change

- Use a GitHub issue for a non-sensitive bug or a design discussion that affects public behavior.
- Follow [`SECURITY.md`](SECURITY.md) for suspected vulnerabilities. Never place credentials,
  private repository names, proprietary source, or real backup data in an issue or pull request.
- Keep pull requests focused. Large behavior changes should describe migration and restore impact.

## Development setup

Python 3.11 or newer, Git, and Git LFS are required. Docker is required for container changes.

```bash
git clone https://github.com/drhdev/github-backup.git
cd github-backup
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[dev,s3]'
```

The lockfile-based setup used by CI is:

```bash
python -m pip install 'uv==0.11.24'
uv sync --frozen --extra dev --extra s3
```

## Required checks

Run these before opening a pull request:

```bash
ruff check .
mypy src
pytest --cov=github_backup --cov-report=term-missing --cov-fail-under=80
pip-audit
python -m pip wheel --no-deps --wheel-dir dist .
docker build --pull -t github-backup:test .
docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  github-backup:test --version
```

With `uv`, prefix Python tools with `uv run` and use `uv build` for the package build.

Add tests for changed behavior. Tests must not use a real token or contact a live private resource.
When a live integration check is necessary, document the isolated test account, permissions, and
cleanup procedure without disclosing secrets.

## Commit and pull-request format

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) because commits merged
to `main` drive Semantic Versioning and the generated changelog:

```text
feat: add a user-visible capability
fix: prevent an incomplete snapshot from being published
perf: reduce API requests during discovery
docs: clarify NAS permissions
refactor: simplify storage selection
build: update the package build
ci: harden a workflow
```

Use `!` and a `BREAKING CHANGE:` footer for an incompatible change. `feat` produces a minor
release, `fix`/`perf` produce a patch release, and a breaking change produces a major release.
Documentation, tests, chores, and CI-only commits appear in the release PR as configured but do
not independently bump the version.

Pull requests must explain:

- the motivation and user-visible effect;
- tests performed and relevant failure cases;
- security, credential, storage, and restore implications;
- compatibility or migration impact;
- documentation updates.

## Release process

Maintainers do not manually edit version numbers or create normal release tags. Release Please
opens and updates a release pull request from conventional commits. Merging that pull request
creates a draft release. The release workflow then tests and packages the exact tag, generates an
SPDX SBOM and SHA-256 checksums, creates provenance attestations, publishes multi-architecture
container images, and publishes the GitHub release only after all release jobs pass.

See [`docs/RELEASING.md`](docs/RELEASING.md) for repository setup, first-release bootstrap,
recovery, and artifact verification.

## License

By contributing, you agree that your contribution is licensed under this project's
[GNU General Public License v3.0](LICENSE).
