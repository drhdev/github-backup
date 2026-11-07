# Back up multiple accounts and organizations with a GitHub App

This feature requires the updated source containing `src/github_backup/app_auth.py`;
older published releases do not support these settings.

Create a GitHub App under GitHub Settings → Developer settings → GitHub Apps.
Disable webhooks. Grant repository **Contents: Read-only** (Metadata read is automatic).
Choose an installation availability that permits your other accounts and organizations.
Install the App separately on each account and organization you administer, choosing
**All repositories** on every installation. New repositories in those installations
are discovered on the next run. A collaborator's repositories require installation on
the owning account; membership alone does not grant an installation access.

Record the App ID and generate a private key. Transfer the PEM file securely to the Pi.
Do not paste it into logs, commits, or this chat. The program uses `openssl` to sign
App JWTs and renews installation tokens before expiry. No personal access token is needed.
Gists are not supported with installation authentication.

On Raspberry Pi OS with Python 3.11 or newer, from the updated source checkout:

```bash
sudo apt update
sudo apt install -y ca-certificates git openssl python3 python3-venv python3-pip
python3 -m venv .venv
.venv/bin/python -m pip install .
sudo install -d -m 700 /etc/github-backup
sudo install -m 600 /path/to/downloaded-private-key.pem /etc/github-backup/app-private-key.pem
```

The process running backups must own and be able to read the private key. For an initial
manual test as your SSH user, use a private directory under your home instead:

```bash
mkdir -p ~/.config/github-backup ~/github-backups
chmod 700 ~/.config/github-backup ~/github-backups
install -m 600 /path/to/downloaded-private-key.pem ~/.config/github-backup/app-private-key.pem
cp github-backup.app.example.toml ~/.config/github-backup/config.toml
nano ~/.config/github-backup/config.toml
```

Set `app_id`, the absolute private-key path, `destination` (your home backup directory),
and `app_owners` (all your personal accounts and organizations). Owner names are explicit
so a revoked or missing installation fails discovery instead of silently disappearing.
Selected-repository installations are rejected to protect the intended complete coverage.
With App authentication, `accessible`/`owned` both cover the configured installation owners;
`organizations` applies the existing explicit organization allowlist.

```bash
.venv/bin/github-backup --config ~/.config/github-backup/config.toml check
.venv/bin/github-backup --config ~/.config/github-backup/config.toml list
.venv/bin/github-backup --config ~/.config/github-backup/config.toml run
.venv/bin/github-backup --config ~/.config/github-backup/config.toml verify
```

Compare the listing to every account's inventory before scheduling. Test a restore from
the actual backup destination. The example uses snapshots and keeps all recovery points;
choose retention and monitor disk space. For unattended service use, follow the README's
systemd setup, make the service user own the PEM (mode 0600), and use this configuration
instead of the token environment file. Optional metadata, Actions, and other components
require their corresponding App read permissions and may have API coverage limitations.

GitHub reference: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation
