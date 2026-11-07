# Docker logging and optional Uptime Kuma

The default installation is [Coolify + Docker Compose](COOLIFY.md). This worker does not
expose a website or HTTP health endpoint. It needs no domain, inbound port, or Docker socket.

## Standard Docker logging

- Scheduler events (`scheduler_started`, `backup_finished`) and completed-run manifest
  summaries are flushed, timestamped JSON lines on **stdout**.
- Application progress, warnings, and errors are JSON lines on **stderr**. An early Python
  startup exception may also produce a normal traceback on stderr.
- Child backup processes inherit the container's stdout/stderr; logs do not disappear into
  cron mail or an internal log file. `PYTHONUNBUFFERED=1` is set in the image.
- Compose uses Docker's `json-file` logging driver with `max-size: 10m` and `max-file: 3`.
  No application log file, persistent log volume, or logrotate service is needed.
- No GitHub tokens, Spaces secrets, private PEMs, or Kuma Push URLs should be printed.
  Repository names and object paths may occur in logs: treat logs as private operational data.

In Coolify, open the application's **Logs** and select the `github-backup` container.
On the Docker host, from the Compose directory:

```bash
docker compose logs --follow --tail=100 github-backup
```

A successful completed check includes events resembling:

```json
{"timestamp":"2026-11-07T15:30:00+00:00","level":"info","event":"manifest","location":"s3://company-files/github-backup/_system/manifests/RUN_ID.json","counts":{"success":12,"partial":0,"failed":0,"skipped":0},"missing_repositories":0}
{"timestamp":"2026-11-07T15:30:10+00:00","level":"info","event":"backup_finished","exit_code":0}
```

No code changes is a successful check, not a failure. A new run report is still saved but
no duplicate code bundle is uploaded. A nonzero completion code needs investigation:
partial/failing repositories, unacknowledged missing repositories, authentication/storage
errors, or `124` for the scheduler's run timeout. Use the manifest for per-repository details.

## Built-in Docker/Coolify health

Compose runs `python -m github_backup.scheduler health` every 60 seconds, with three retries.
It checks the scheduler heartbeat, completion status, and overdue idle checks. A running first
backup may be healthy before any backup has completed. Health is not proof of restoration.

Enable Coolify notifications for unhealthy resources and failed/restarting deployments.
A stopped process exits and can be restarted by Docker's restart policy. An **unhealthy but
running** container is not automatically restarted by that policy. Investigate the failure
rather than adding a restart loop that could hide it.

## Optional Uptime Kuma Push monitor

Monitoring is disabled by default: `UPTIME_KUMA_PUSH_URL` is empty. This is an outbound
Push integration, not an HTTP monitor of the worker. It requires no Kuma administrator
API key and no Docker socket access. The generated Push URL contains a secret token.

### Configure Uptime Kuma

1. In your existing Uptime Kuma instance, select **Add New Monitor**.
2. Choose monitor type **Push** and name it, for example, `GitHub backup — production`.
3. Set **Heartbeat Interval** to **180 seconds** (three minutes).
4. Set **Retries** to **0** so an explicit down report is not hidden by retry delays.
   If the UI shows a retry interval, leave its default; it is not used with zero retries.
5. Select your notification channel (email, Telegram, etc.) and send a test notification.
   Leave upside-down/inverted monitoring disabled. Configure repeat reminders to your own
   preference; they are not required for the initial alert.
6. Save and copy the generated **Push URL**, for example
   `https://kuma.example.com/api/push/SECRET_TOKEN?status=up&msg=OK&ping=`.
   Do not put it on a public status page, in a screenshot, Git commit, or support ticket.
7. In the Coolify backup application's production environment, add this URL as the secret
   runtime variable `UPTIME_KUMA_PUSH_URL`. Disable its build-time exposure. Save and redeploy.
   The application safely replaces the example status/message parameters on every push.

No second Coolify scheduled task, shell `curl` job, health-check URL, or Kuma HTTP monitor
is needed. Do not add an unconditional success ping: it would hide backup failures.

### Environment settings

| Variable | Default | Meaning |
|---|---|---|
| `UPTIME_KUMA_PUSH_URL` | empty | Disabled; set to this monitor's generated secret Push URL |
| `UPTIME_KUMA_ALLOW_HTTP` | `false` | Allow private-network HTTP explicitly; HTTPS remains the default |
| `UPTIME_KUMA_TIMEOUT_SECONDS` | `5` | Network timeout; greater than zero, at most 30 seconds |
| `UPTIME_KUMA_MAX_SUCCESS_AGE_HOURS` | `0` | Automatic: `2 × BACKUP_INTERVAL_HOURS + 1`; default hourly checks allow 3 hours |

Minimal HTTPS example (all other options use defaults):

```dotenv
UPTIME_KUMA_PUSH_URL=https://kuma.example.com/api/push/REPLACE_WITH_GENERATED_TOKEN
```

A slower deployment that checks daily and normally finishes within two hours could use:

```dotenv
BACKUP_INTERVAL_HOURS=24
UPTIME_KUMA_PUSH_URL=https://kuma.example.com/api/push/REPLACE_WITH_GENERATED_TOKEN
UPTIME_KUMA_MAX_SUCCESS_AGE_HOURS=27
```

Choose a success-age limit larger than the check interval plus normal run duration and a
reasonable margin. For a first backup taking several hours, expect a down report until it
finishes successfully; pause notifications temporarily if appropriate, then re-enable them.
Changing the check schedule does **not** require changing Kuma's 180-second heartbeat interval:
worker liveness pushes happen separately from the backup schedule.

### What is reported

The scheduler sends at most one repeated-status heartbeat every 60 seconds, plus immediate
status changes on its next 30-second loop. The request reports:

- **Up:** the scheduler is running, its most recently completed check succeeded, and a
  successful check is recent enough. A successful check without changed code also counts.
- **Down:** no successful check yet, a failed/partial/timed-out check, an unacknowledged
  missing/inaccessible repository, or no success within the configured freshness limit.
  An old success becomes down even if a new job is still running.
- **No heartbeat:** the container/server/scheduler is stopped or its connection to Kuma is
  broken. Kuma marks the monitor down when its heartbeat window expires.

This deliberately monitors completed backup checks, not merely a live Python process.
After fixing an error, a successful check restores up status. For an intentional repository
deletion, review recovery first and add its numeric ID to `ACKNOWLEDGED_MISSING_REPOSITORIES`
if appropriate; acknowledgment never deletes any backup.

A Kuma outage or rejected push logs a sanitized warning and does not stop backups. Delivery
is bounded by the network timeout and retried on a later heartbeat; it does not enter a
blocking retry loop. HTTPS certificates are verified. Redirects are refused, and the Push
URL/token are omitted from delivery-error logs. Use the final canonical HTTPS URL rather
than a URL that redirects from HTTP, adds a slash, or passes through a login page. A reverse
proxy may protect the Kuma UI, but the token-authenticated `/api/push/...` path must be reachable
from the backup container without an interactive login.

### Private-network Kuma

If Kuma is on a trusted private Docker/network connection, this is explicitly supported:

```dotenv
UPTIME_KUMA_PUSH_URL=http://uptime-kuma:3001/api/push/REPLACE_WITH_GENERATED_TOKEN
UPTIME_KUMA_ALLOW_HTTP=true
```

The hostname must resolve and be reachable **from the backup container**. `localhost` means
that container, not the host or a different service. Connect the services to the appropriate
shared network in Coolify, or use a reachable HTTPS hostname. Do not expose an unencrypted
Push token across an untrusted network. Prefer running Kuma and its notification delivery
outside the backup server if it must detect loss of that entire server.

### Test notifications and recovery

1. Wait for a successful backup check. Confirm `exit_code: 0`, inspect the Spaces manifest,
   and confirm the Kuma monitor becomes up.
2. After any current backup has completed, stop the worker in a maintenance window. With
   ordinary Compose this is `docker compose stop github-backup`. Do not stop Kuma too.
3. Verify a missing-heartbeat alert after about three minutes (allow Kuma's polling delay).
4. Start the worker again (`docker compose start github-backup` outside Coolify), wait for
   a successful check, and verify recovery notification. Use Coolify Stop/Start controls for
   a Coolify-managed resource.
5. Confirm an existing bundle can be restored independently; Kuma does not validate its code
   contents or detect a malicious but syntactically valid push. Repeat restore drills periodically.

References: [Uptime Kuma Push API](https://github.com/louislam/uptime-kuma/wiki/Internal-API),
[Coolify environment variables](https://coolify.io/docs/applications/configuration/environment-variables),
[Coolify Compose deployment](https://coolify.io/docs/applications/builds/docker-compose).
