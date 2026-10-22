# Security policy

## Supported versions

Security fixes are provided for the latest `1.x` release. Older versions should be upgraded before reporting operational problems.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability that could expose GitHub tokens, private repository data, backup credentials, or storage contents. Use GitHub's private vulnerability reporting feature for this repository. Include the affected version, deployment model, reproduction steps, and impact. Do not include live credentials or private source code.

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
