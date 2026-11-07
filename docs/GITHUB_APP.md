# GitHub Apps, accounts and repository scope

The default [Coolify installation](COOLIFY.md) manages GitHub Apps, account scopes and
DigitalOcean Spaces in the protected setup page. Environment variables are needed only for
the setup HTTPS URL/password and optional operational settings. One setup administrator
can manage multiple personal GitHub accounts and organizations with several independent Apps.
This is a single-admin backup deployment, not a service with separate tenant accounts.

## Register Apps and connect installations

1. Open `GITHUB_SETUP_URL` and log in as **admin** using `GITHUB_SETUP_PASSWORD`.
2. Click **Connect GitHub / create another App**, then approve creation on GitHub. To own
   the App under an organization, enter its login in the optional creation field first.
3. Use that App's **Install App** link. On GitHub choose **All repositories** or **Only select
   repositories** for the personal account or organization receiving the installation.
4. Return to setup, enter the installed account's login under the matching App, and click
   **Verify and connect account**. A new connection starts **paused** to avoid broadening coverage
   without your selection. Organization policy may require approval by an owner.
5. Open **Choose repositories / pause backups**, choose a scope and save it.
6. Repeat App creation for another GitHub account, switching GitHub logins as needed, or
   install the same App on more accounts/organizations and connect each under that App.

Every App requests only repository **Contents: Read-only**, with automatic metadata read
access, disabled webhooks and no user OAuth authorization. The registration uses GitHub's
[manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest).
Its private key is transferred automatically; there is no PEM download or Base64 conversion.
Apps are publicly installable to support several accounts; installing an App alone does not
authorize a stranger's account for this worker. Only explicitly connected accounts are used.

Each connection identifies both an App and the personal/organization login. Different Apps
can serve different accounts or the same account. Overlapping selections form a **union**;
a repository is backed up once using a matching App's installation token. If one connection
loses access, other working connections continue backing up. The manifest records
`discovery_errors`, the run reports failure and monitoring alerts, even if another connection
covers overlapping repositories. Pause a connection deliberately to stop using it.

## Choose repository scope

| Scope in setup | What is backed up | Required GitHub installation access |
|---|---|---|
| All | Every current and future repository | All repositories |
| Only selected | Checked repositories only | All or selected repositories |
| All except selected | Every current and future repository except checked ones | All repositories |
| Pause | None from this connection | No API requests are needed for this connection |

The form lists repositories returned by that installation. It cannot grant access beyond
GitHub's own [installation repository selection](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app).
For minimal access, select repositories on GitHub and use **Only selected** in backup setup.
For automatic coverage of future repositories, choose All repositories on GitHub and use
**All** or **All except selected** in backup setup. To add a repository that is not accessible,
first edit the installation on GitHub, then reload the selection page.

Selections use stable numeric repository IDs, so a rename does not lose the selection.
Pausing preserves the last saved checkbox selection for later reuse.
Deleting/recreating a repository with the same name gives it a different ID; explicitly
select that new repository in Only selected mode. Empty selected lists and paused connections
create no code backups; verify that your selection contains everything you need.

A previously selected ID that becomes inaccessible stays checked in the form as **Unavailable**.
It remains expected and raises a missing/inaccessible alert. Uncheck it only to exclude it
deliberately. A lost GitHub permission is not automatically treated as your decision to exclude.
An all/all-except installation changed to selected-only fails rather than silently omitting
future repositories. Suspended or identity-mismatched installations fail closed.

Deliberately excluded or paused repositories keep all existing backups, with no pruning.
Previously archived excluded repositories appear in manifest `excluded_resources`; expected
repositories that disappear appear in `missing_resources`. An account served by several Apps
is excluded only when every applicable connection excludes it. Unknown/removed accounts
still report missing resources rather than suppressing alerts.

## Configure DigitalOcean Spaces

In the same setup page, enter bucket, region, root folder, access key ID and secret, then
**Test and save Spaces**. Defaults are region `fra1` and root `github-backup`; select your
bucket's actual region. Find the key pair under DigitalOcean **Spaces → Access Keys**, and
bucket/region under **Spaces → Buckets**. Do not use a DigitalOcean personal API token.

The save action checks versioning status, prefix listing, and private object write/read/delete
using a random disposable object inside `PREFIX/_system/setup-check/`. Use an unversioned
bucket and a key with those permissions. Failed checks retain the prior settings. Shared
buckets are supported; the backup root must be exclusive to this deployment. Key fields are
never echoed and can be left blank to keep saved values. Changing destination requires a
confirmation and leaves the old archive intact. See [the deployment guide](COOLIFY.md).

Once storage and at least one installation are configured, the worker begins within 30 seconds.
Changes apply on the next scheduled run without restarting. Each run uses one settings snapshot,
so an in-flight run finishes using its original destination, App credentials and selections.

## Credential storage and security

Settings are saved atomically with fsync in `/cache/github-app/credentials.json` on the
persistent `backup-cache` volume. File permissions are **0600**, directory permissions **0700**,
and ownership is UID 10001. Both containers can read this secret file, which includes App keys,
scopes and Spaces credentials. Secrets are not encrypted at rest by the application; use
host/disk encryption and protect host/Coolify access and any volume backups.

App PEMs are materialized in private `/tmp` tmpfs files with separate filenames per App ID.
Installation tokens are short-lived and not persisted. Unneeded manifest OAuth/webhook
secrets are discarded. The setup service uses one Gunicorn worker and two threads with
128 MiB memory; the backup worker remains single-process/single-transfer by default.

Use HTTPS with a valid certificate and an independent random setup password of at least
24 characters (`openssl rand -hex 32`). Restrict setup ingress at the proxy where practical.
GitHub callbacks are browser redirects; no webhook receiver or direct GitHub-to-server
connection is required. Disable request query-string logging at the proxy: callback URLs
contain single-use conversion codes. Setup access logging is disabled in the container.

Forms require a browser-bound CSRF token. Manifest states expire after an hour and are
consumed once. Restarting setup expires pending flows, so start again. Failed conversion
may leave an unused App on GitHub; remove it there before retrying. New Apps are appended;
a callback cannot overwrite another App's credentials or erase Spaces settings.

## Upgrading and recovery

The previous single-App credential file is recognized and upgraded to the version-2 settings
structure on the next save. Its owners retain **All** scope to preserve existing coverage.
New connections default to **Pause**. Existing manual environment deployments continue to
work; remove all three GitHub App variables to switch to browser-managed multiple Apps.
Saved browser Spaces settings take precedence over legacy Spaces environment values.
Verify a successful backup before removing the previous credentials or revoking old Apps.

Keep a secure independent backup of the credential directory. Container recreation preserves
it; deleting the volume loses both settings and Git cache. Lost credentials require a secure
restore or new Apps. Existing Spaces bundles and catalogs remain recoverable without GitHub.
To rotate a stored App key, stop both services, securely update only `pem` in the matching
`apps` entry (identified by `id`), preserve permissions/ownership, restart, verify a backup,
and then revoke the old key. Never paste keys into logs or shell history. Spaces keys can
be replaced and tested through the setup form without echoing the existing keys.

To stop backing up an account, use **Pause**, preserving its archive and explicit intent.
There is no destructive App/backup delete button. To remove an App deliberately, stop both
services and carefully edit only its entry in the saved `apps` list, preserving all other
Apps and `spaces`. Removed accounts may report missing repositories; follow the
[missing-repository policy](COOLIFY.md#folder-layout-in-a-shared-bucket). Do not delete the
volume or S3 prefix to reset onboarding.

## Manual environment alternative

For a deployment without a setup domain, keep the URL/password empty and do not route
traffic to `github-setup`. The original single-App environment configuration remains available:

```dotenv
GITHUB_APP_ID=123456
GITHUB_APP_OWNERS=alice,example-team,alice-work
GITHUB_APP_PRIVATE_KEY_BASE64=REPLACE_WITH_SINGLE_LINE_BASE64_PEM
AWS_ACCESS_KEY_ID=REPLACE_WITH_SPACES_ACCESS_KEY
AWS_SECRET_ACCESS_KEY=REPLACE_WITH_SPACES_SECRET
SPACES_BUCKET=company-files
SPACES_REGION=fra1
SPACES_PREFIX=github-backup
```

GitHub App environment settings override the saved browser App connections; all three must
be cleared for browser selection to take effect. Manual mode continues requiring All repositories
for each owner. Create the App in GitHub Developer settings, grant Contents read-only, disable
webhooks, choose Any account for several owners and install on every listed account. Copy the
numeric App ID, download a generated private key and encode with
`base64 < app-private-key.pem | tr -d '\n'`. Treat the output as a secret; Base64 is not encryption.

Legacy `AWS_` variable names are boto3 conventions but take **DigitalOcean Spaces** credentials,
not AWS keys. They are used only when no browser Spaces configuration exists. See
[CLI reference](CLI.md) for filesystem/NAS/TOML configurations, which are separate from setup.
