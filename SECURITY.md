# Security policy

## Supported versions

| Version | Supported |
|---|---|
| Latest `1.x` release | Yes |
| Older releases and unreleased commits | No |

Security fixes are provided for the latest published `1.x` release. Upgrade before reporting an
operational problem against an older version.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability that could expose GitHub tokens, private
repository data, backup credentials, or storage contents. Use this repository's
[private vulnerability reporting form](https://github.com/drhdev/github-backup/security/advisories/new).

Include the affected version or image digest, deployment model, reproduction steps, expected and
actual behavior, impact, and any proposed mitigation. Replace real secrets and private data with
safe test values. If private reporting is unavailable, contact the repository owner through the
contact method on their GitHub profile and ask for a private channel without sending exploit or
secret material in the first message.

Maintainers will acknowledge and assess reports on a best-effort basis, coordinate a fix and
release when validated, and credit reporters who request credit. Do not publicly disclose an
unresolved vulnerability before maintainers have had a reasonable opportunity to address it.

## Release integrity

Official releases are GitHub releases produced from version tags by the release workflow. Verify
downloaded assets with `SHA256SUMS` and GitHub artifact attestations. Prefer container digests over
mutable tags in controlled environments. See [`docs/RELEASING.md`](docs/RELEASING.md).

## Deployment requirements

- Inject GitHub and storage credentials through a secret manager or protected environment file.
- Use HTTPS GitHub and S3 endpoints. HTTP overrides exist only for deliberately isolated test systems.
- Keep filesystem destinations and S3 cache directories private to the service account. The application rejects broad Unix mode bits by default, but operators must also review ACLs and mount-level permissions.
- Enable encryption at rest for NAS volumes. S3 uploads request AES-256 server-side encryption by default; use KMS when policy requires managed keys.
- Grant the GitHub token read-only repository access and gist access. Do not grant write or administration permissions.
- Scope S3 credentials to the configured bucket prefix. Snapshot retention additionally requires list and delete permissions.
- Treat manifests and logs as sensitive metadata because they contain private repository names and failure details.
- Treat optional metadata archives as highly sensitive: they can contain private issues and pull requests, collaborator and deployment metadata, and security alerts. Secret names are exported when accessible, but GitHub never returns secret values.
- Treat release, Actions, and LFS archives as untrusted user-controlled files. Do not execute or extract them outside an isolated restore workflow.
- Monitor nonzero exit codes and test restoration on a schedule.
- Keep the application, Git, Git LFS, Python or container runtime, host OS, and dependencies
  patched. Dependabot, dependency review, `pip-audit`, CodeQL, and OpenSSF Scorecard provide
  repository signals but do not replace operational vulnerability management.
