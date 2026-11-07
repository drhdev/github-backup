# GitHub App setup for Coolify backups

The default deployment is [Coolify + Docker Compose + DigitalOcean Spaces](COOLIFY.md).
The App authenticates read-only backups across explicitly configured user and organization
accounts. No personal access token or manual installation-token rotation is needed.

## Create the App

1. In GitHub, open **Settings → Developer settings → GitHub Apps → New GitHub App**
   (or the corresponding organization developer settings).
2. Choose a unique name and supply the required homepage URL. This is a background backup
   client: it needs no public callback server. Leave OAuth callback/setup flows unused.
3. Disable **Active** under webhooks; no push-event receiver is required. The worker polls refs.
4. Under repository permissions, grant **Contents: Read-only**. Metadata read permission is
   automatic. Do not grant write, administration, or unrelated organization permissions for
   the default Git-only profile. Gists are not supported by installation authentication.
5. If backing up multiple users/organizations, choose **Any account** (a public-installable App).
   **Only on this account** restricts a private App to the account that owns it. Public
   installation availability does not publish your private key or back up strangers' accounts:
   this worker only uses the owners explicitly listed in `GITHUB_APP_OWNERS`.
6. Create the App and record its **App ID** (not the Client ID, installation ID, or App name).
7. Generate and securely download a private-key PEM. Keep it outside Git repositories.

GitHub explains [public/private installation availability](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private).

## Install on every owner

Open the App's **Install App** page. Install it separately on each personal account and
organization you want to back up, selecting **All repositories** each time. If needed, sign
into your second/third account to approve its installation. Organization policy may require
an owner's approval. New repositories within an installation are then discovered automatically.
Membership or collaborator access does not grant the App access to another owner's repositories.
The backup program rejects selected-repository installations to avoid silently missing future repos.

Example:

```dotenv
GITHUB_APP_ID=123456
GITHUB_APP_OWNERS=alice,example-team,alice-work
```

All three owners must have installed the same App with All repositories. Separate App
registrations require separate worker deployments with separate, non-overlapping Spaces prefixes.
A missing, suspended, or revoked installation fails the check while existing backup data stays safe.

## Configure the PEM in Coolify

Encode it on your own computer as one line:

```bash
base64 < app-private-key.pem | tr -d '\n'
```

Set `GITHUB_APP_PRIVATE_KEY_BASE64` to the output in the Coolify application's production
environment, masked as a secret. With the current Compose file it must be available for
both Buildtime interpolation and Runtime, as explained in [the deployment guide](COOLIFY.md). Set the other
required environment variables from [the deployment guide](COOLIFY.md). Base64 is an encoding,
not encryption. The worker decodes it to a mode-0600 file in tmpfs and uses `openssl` to sign
App JWTs, then obtains and refreshes short-lived installation tokens automatically.

Do not paste the PEM, its encoded form, tokens, or Spaces keys into logs or support requests.
Protect Coolify independently from the GitHub accounts being backed up and disable automatic
GitHub-triggered deployments of this worker. Rotate the key by adding a new key in GitHub,
updating the Coolify runtime secret, redeploying, verifying a successful check, then revoking
the previous key.

## Alternative manual CLI deployment

For a local filesystem, NAS, Python environment, or systemd setup, use the alternative
[CLI/install reference](CLI.md) and [`github-backup.app.example.toml`](../github-backup.app.example.toml).
Set `app_id`, an absolute private-key path (permissions 0600/0400), `app_owners`, and
`include_gists = false`. These TOML settings are for the CLI; the Coolify scheduler reads
its environment instead and does not require a mounted TOML config.

Reference: [GitHub installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).
