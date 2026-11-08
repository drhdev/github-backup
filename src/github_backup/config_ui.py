"""Escaped, server-rendered config views with neutral shadcn-style design tokens."""

# HTML fragments remain readable as complete attribute/context boundaries.
# ruff: noqa: E501

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import urlencode

from github_backup.models import Resource


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{escape(name, quote=True)}" value="{escape(value, quote=True)}">'


def field(
    name: str,
    label: str,
    value: str = "",
    *,
    kind: str = "text",
    help_text: str = "",
    required: bool = False,
    autocomplete: str = "off",
    identity: str = "",
    extra: str = "",
) -> str:
    # Call sites provide only constant labels/options; values are always escaped.
    input_id = identity or name
    required_attr = " required" if required else ""
    return (
        f'<div class="field"><label for="{escape(input_id, quote=True)}">{escape(label)}</label>'
        f'<input id="{escape(input_id, quote=True)}" name="{escape(name, quote=True)}" '
        f'type="{kind}" value="{escape(value, quote=True)}" autocomplete="{autocomplete}" '
        f'autocapitalize="none" spellcheck="false"{required_attr} {extra}>'
        + (f"<small>{escape(help_text)}</small>" if help_text else "")
        + "</div>"
    )


def check(name: str, label: str, *, checked: bool = False, detail: str = "") -> str:
    return (
        f'<label class="check"><input type="checkbox" name="{name}" value="yes"'
        + (" checked" if checked else "")
        + f"><span>{escape(label)}"
        + (f"<small>{escape(detail)}</small>" if detail else "")
        + "</span></label>"
    )


def document(
    body: str, browser: str = "", *, authenticated: bool = False, current_path: str = "/"
) -> str:
    navigation = ""
    if authenticated:
        navigation = '<nav class="navigation" aria-label="Main navigation">'
        active = current_path if current_path in ("/history", "/account") else "/"
        for href, label in (("/", "Config"), ("/history", "Activity"), ("/account", "Account")):
            selected = ' aria-current="page"' if active == href else ""
            navigation += f'<a href="{href}"{selected}>{label}</a>'
        navigation += (
            '<form method="post" action="/logout">'
            + hidden("csrf", browser)
            + '<button class="secondary">Log out</button></form></nav>'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="color-scheme" content="light dark"><title>GitHub Backup · Config</title>'
        '<link rel="stylesheet" href="/assets/config.css">'
        '<script defer src="/assets/config.js"></script></head><body>'
        '<a href="#content" class="skip button">Skip to content</a>'
        '<header class="topbar"><div class="topbar-inner">'
        '<a href="/" class="brand"><span class="brand-icon" aria-hidden="true">↳</span>'
        'GitHub Backup <span class="muted">/ Config</span></a>'
        '<label class="theme" for="theme"><span>Appearance</span>'
        '<select id="theme" aria-label="Color theme"><option value="system">System</option>'
        '<option value="light">Light</option><option value="dark">Dark</option></select></label>'
        + navigation
        + '</div></header><main id="content" class="page">'
        + body
        + "</main></body></html>"
    )


def card(identifier: str, title: str, description: str, body: str, *, opened: bool = False) -> str:
    return (
        f'<details class="card" id="{identifier}"'
        + (" open" if opened else "")
        + f"><summary><span><strong>{escape(title)}</strong><small>{escape(description)}</small>"
        '</span></summary><div class="card-body">' + body + "</div></details>"
    )


def home_page(data: dict[str, Any], browser: str) -> str:
    csrf = hidden("csrf", browser)
    spaces = data.get("spaces") or {}
    apps = data["apps"]
    owners = [(app, owner) for app in apps for owner in app["owners"]]
    policies = [
        app.get("scopes", {}).get(owner, {"mode": "all", "repositories": []})
        for app, owner in owners
    ]
    active = sum(
        policy["mode"] != "none" and (policy["mode"] != "include" or bool(policy["repositories"]))
        for policy in policies
    )
    app_count = f"{len(apps)} App" + ("" if len(apps) == 1 else "s")
    active_count = f"{active} active connection" + ("" if active == 1 else "s")
    body = (
        '<div class="intro"><p class="eyebrow">Configuration</p><h1>Backup configuration</h1>'
        '<p class="muted">Follow these three steps once. Reopen any section to edit it later. '
        'Changes take effect on the next scheduled check, hourly by default.</p></div><ol class="steps">'
    )
    for number, target, label, state in (
        (1, "storage", "Choose storage", "Configured" if spaces else "Start here"),
        (
            2,
            "github",
            "Connect GitHub",
            f"{app_count} connected" if apps else "Not connected",
        ),
        (
            3,
            "scope",
            "Choose repositories",
            active_count if active else "Not active",
        ),
    ):
        body += f'<li><a href="#{target}">{number}. {label}<span>{escape(state)}</span></a></li>'
    body += "</ol>"
    storage = (
        '<p class="description">Use a private root folder in an unversioned DigitalOcean '
        "Spaces bucket. Other folders in a shared bucket are left alone.</p>"
        '<form method="post" action="/spaces">' + csrf + '<div class="form-grid">'
    )
    for name, label, default, help_text in (
        (
            "bucket",
            "Bucket name",
            "",
            "DigitalOcean → Spaces → Buckets. Enter the name, not a URL.",
        ),
        ("region", "Region", "fra1", "The origin region, for example fra1 or ams3."),
        (
            "prefix",
            "Backup root folder",
            "github-backup",
            "Reserve this folder for this deployment.",
        ),
    ):
        storage += field(
            name,
            label,
            spaces.get(name, default),
            required=True,
            help_text=help_text,
            identity="storage-" + name,
            extra='maxlength="200"',
        )
    storage += field(
        "access_key_id",
        "Spaces access key ID",
        kind="password",
        required=not bool(spaces),
        help_text="Leave blank to keep the saved key."
        if spaces
        else "Create under Spaces → Access Keys.",
        autocomplete="new-password",
        extra='maxlength="256"',
    )
    storage += field(
        "secret_access_key",
        "Spaces secret key",
        kind="password",
        required=not bool(spaces),
        help_text="Never displayed or prefilled. Leave blank to keep the saved secret.",
        autocomplete="new-password",
        extra='maxlength="256"',
    )
    if spaces:
        storage += field(
            "current_password",
            "Confirm your current password",
            kind="password",
            required=True,
            autocomplete="current-password",
            extra='maxlength="128"',
            help_text="Password confirmation protects existing storage settings and saved keys.",
        )
    storage += "</div>" + check(
        "confirm_destination",
        "Confirm a change of storage location",
        detail="Only needed when editing the bucket, region or root. Existing backups stay in their original location.",
    )
    storage += (
        '<div class="actions"><button>Test and save storage</button>'
        "<p>Tests listing, write, read and delete access inside your root.</p></div></form>"
    )
    body += card(
        "storage",
        "1. DigitalOcean Spaces",
        "Configured. Open to edit." if spaces else "Where your backup files will live.",
        storage,
        opened=not spaces,
    )
    github = (
        '<p class="description">Create an App, install it on a user or organization, then '
        "connect that account below. You can use several Apps and accounts.</p>"
    )
    for app in apps:
        app_id = app["id"]
        github += (
            f'<article class="item"><div class="item-heading"><h3>{escape(app["slug"])}</h3>'
            '<span class="badge">Read-only App</span></div>'
            f'<a class="button secondary" href="https://github.com/apps/{escape(app["slug"], quote=True)}/installations/new">'
            'Install App on GitHub</a><p class="footer-note">Install on a personal account '
            "or organization, then enter its login here. Organization approval may be required.</p>"
            '<form class="inline-form" method="post" action="/owners">'
            + csrf
            + hidden("app_id", app_id)
            + field(
                "owner",
                "Installed account or organization",
                identity="owner-" + app_id,
                required=True,
                extra='maxlength="100" pattern="[A-Za-z0-9][A-Za-z0-9\\-]{0,99}"',
            )
            + "<button>Connect account</button></form></article>"
        )
    github += (
        '<div class="item"><h3>Create a GitHub App</h3><form method="post" action="/connect">'
        + csrf
        + field(
            "registration_owner",
            "Organization owning the App (optional)",
            help_text="Leave blank to create it in your signed-in personal account.",
            extra='maxlength="100"',
        )
        + '<div class="actions"><button>Create GitHub App</button>'
        "<p>Continue on GitHub, then return here.</p></div></form></div>"
    )
    body += card(
        "github",
        "2. GitHub connections",
        f"{app_count}. Add or edit connections.",
        github,
        opened=bool(spaces) and not owners,
    )
    scopes = (
        '<p class="description">A new connection is paused until you choose its scope. '
        "All and All except include future repositories; Only selected is an explicit list.</p>"
    )
    if not owners:
        scopes += "<p>No accounts connected yet. Complete step 2 first.</p>"
    for app, owner in owners:
        policy = app.get("scopes", {}).get(owner, {"mode": "all", "repositories": []})
        labels = {
            "all": "All repositories",
            "none": "Paused",
            "include": "Only selected",
            "exclude": "All except selected",
        }
        scope_label = labels[policy["mode"]]
        if policy["mode"] in ("include", "exclude"):
            scope_label += f" ({len(policy['repositories'])})"
        href = "/repositories?" + urlencode({"app_id": app["id"], "owner": owner})
        scopes += (
            f'<div class="item item-heading"><div><h3>{escape(owner)}</h3>'
            f'<small>{escape(app["slug"])}</small></div><span class="badge">'
            f'{escape(scope_label)}</span><a class="button secondary" '
            f'href="{escape(href, quote=True)}">Choose repositories / edit scope</a></div>'
        )
    scopes += (
        '<p class="footer-note">Excluded and paused repositories keep their existing backups. '
        "Losing access to a repository that is still selected raises an alert.</p>"
    )
    body += card(
        "scope",
        "3. Repository scope",
        f"{active_count}. Review what is included.",
        scopes,
        opened=bool(owners) and not active,
    )
    monitor = data.get("monitoring") or {}
    monitoring = (
        '<p class="description">Optional. In Uptime Kuma, create a Push monitor with a '
        "180-second heartbeat interval, 0 retries and a notification channel.</p>"
        '<form method="post" action="/monitoring">'
        + csrf
        + check("enabled", "Enable Push monitoring", checked=bool(monitor.get("enabled")))
        + '<div class="form-grid">'
        + field(
            "url",
            "Uptime Kuma Push URL",
            kind="password",
            autocomplete="new-password",
            help_text="Paste the generated secret URL. Leave blank to keep a saved URL.",
            extra='maxlength="2048"',
        )
        + field(
            "timeout_seconds",
            "Request timeout (seconds)",
            str(monitor.get("timeout_seconds", 5)),
            kind="number",
            extra='min="0.1" max="30" step="any"',
        )
        + field(
            "max_success_age_hours",
            "Maximum backup age (hours)",
            str(monitor.get("max_success_age_hours", 0)),
            kind="number",
            extra='min="0" step="any"',
            help_text="0 calculates a safe threshold from the check interval.",
        )
        + "</div>"
        + check(
            "allow_http",
            "Allow HTTP on a trusted private network",
            checked=bool(monitor.get("allow_http")),
        )
        + '<div class="actions"><button>Save monitoring</button><p>Applies on the next scheduler poll, '
        "normally within 30 seconds. No restart needed.</p></div></form>"
    )
    body += card("monitoring", "Monitoring", "Optional Uptime Kuma alerts.", monitoring)
    return body + (
        '<section class="card"><div class="card-body item-heading"><div><h2>Verify your first backup</h2>'
        '<p class="description">Review Activity, then test restoring a bundle from Spaces.</p></div>'
        '<a href="/history" class="button secondary">View backup activity</a></div></section>'
    )


def account_page(browser: str, *, register: bool, error: str = "") -> str:
    title, action = (
        ("Create your config account", "/register") if register else ("Welcome back", "/login")
    )
    body = '<div class="auth"><div class="intro"><p class="eyebrow">GitHub Backup</p>'
    body += (
        f'<h1>{title}</h1><p class="muted">'
        + (
            "One owner account protects your configuration."
            if register
            else "Log in to manage your backups."
        )
        + '</p></div><section class="card"><div class="card-body">'
    )
    if error:
        body += f'<div class="notice" role="alert">{escape(error)}</div>'
    if register:
        body += (
            '<p class="description">First, retrieve the one-time registration code from the '
            "<strong>github-config</strong> service terminal:</p>"
            "<p><code>python -m github_backup.config_auth registration-code</code></p>"
        )
    body += f'<form method="post" action="{action}">' + hidden("csrf", browser)
    if register:
        body += field(
            "code",
            "Registration code",
            kind="password",
            required=True,
            extra='maxlength="64"',
            autocomplete="off",
        )
    body += field(
        "username",
        "Username",
        required=True,
        autocomplete="username",
        extra='minlength="3" maxlength="64" pattern="[A-Za-z0-9_.\\-]{3,64}"',
    )
    body += field(
        "password",
        "Password",
        kind="password",
        required=True,
        autocomplete="new-password" if register else "current-password",
        extra='maxlength="128"',
        help_text="Use a unique 16-128 character password or passphrase." if register else "",
    )
    if register:
        body += field(
            "confirmation",
            "Confirm password",
            kind="password",
            required=True,
            autocomplete="new-password",
            extra='maxlength="128"',
        )
    body += f"<button>{'Create account' if register else 'Log in'}</button></form></div></section>"
    body += (
        '<p class="footer-note">Registration closes permanently after the first account. '
        "Save your credentials securely. If they are lost, a fresh deployment with a new "
        "volume is required; remote backups can be kept.</p></div>"
    )
    return body


def credentials_page(username: str, browser: str, error: str = "") -> str:
    body = (
        '<div class="intro"><p class="eyebrow">Account</p><h1>Account settings</h1>'
        '<p class="muted">Change your username, password, or both. Confirm with your current '
        "password. All browsers will be signed out after saving.</p></div>"
    )
    if error:
        body += f'<div class="notice" role="alert">{escape(error)}</div>'
    return body + (
        '<section class="card"><div class="card-body"><form method="post" action="/account">'
        + hidden("csrf", browser)
        + '<div class="form-grid">'
        + field(
            "username",
            "Username",
            username,
            autocomplete="username",
            required=True,
            extra='minlength="3" maxlength="64" pattern="[A-Za-z0-9_.\\-]{3,64}"',
        )
        + field(
            "current_password",
            "Current password",
            kind="password",
            required=True,
            autocomplete="current-password",
            extra='maxlength="128"',
        )
        + field(
            "password",
            "New password (optional)",
            kind="password",
            autocomplete="new-password",
            extra='minlength="16" maxlength="128"',
            help_text="Leave blank to keep your current password.",
        )
        + field(
            "confirmation",
            "Confirm new password",
            kind="password",
            autocomplete="new-password",
            extra='maxlength="128"',
        )
        + '</div><div class="actions"><button>Update account and sign out</button>'
        "<p>Your configuration and backup files stay in place.</p></div></form></div></section>"
    )


def repository_page(app: dict[str, Any], owner: str, browser: str, repos: list[Resource]) -> str:
    scope = app.get("scopes", {}).get(owner, {"mode": "all", "repositories": []})
    selected = set(scope["repositories"])
    body = (
        '<div class="intro"><p class="eyebrow">Repository scope</p>'
        f'<h1>{escape(owner)}</h1><p class="muted">Choose what this connection backs up. '
        "GitHub installation permissions are always the access limit.</p></div>"
        '<section class="card"><div class="card-body"><form method="post" action="/scope">'
        + hidden("csrf", browser)
        + hidden("app_id", app["id"])
        + hidden("owner", owner)
        + '<fieldset><legend class="visually-hidden">Backup scope</legend><div class="scope-options">'
    )
    for mode, label, description in (
        (
            "all",
            "All repositories",
            "Current and future repositories. Requires All repositories on GitHub.",
        ),
        ("include", "Only selected", "Back up only the checked repositories below."),
        (
            "exclude",
            "All except selected",
            "Everything except checked repositories, including future ones.",
        ),
        (
            "none",
            "Pause this connection",
            "Keep existing copies and stop new checks from this connection.",
        ),
    ):
        body += (
            f'<label class="check option"><input type="radio" name="mode" value="{mode}"'
            + (" checked" if mode == scope["mode"] else "")
            + f"><span><strong>{label}</strong>"
            f"<small>{description}</small></span></label>"
        )
    body += (
        '</div></fieldset><h2>Repository selection</h2><p class="description">'
        "The checked list is included in Only selected mode and excluded in All except selected mode. "
        "Change repository access in the App installation on GitHub if a repository is missing.</p>"
        '<div class="repository-list">'
    )
    visible = set()
    for repo in repos:
        visible.add(repo.identifier)
        body += (
            f'<label class="check"><input type="checkbox" name="repositories" value="{escape(repo.identifier, quote=True)}"'
            + (" checked" if repo.identifier in selected else "")
            + f"><span>{escape(repo.name)}"
            f"<small>{'Private' if repo.private else 'Public'}</small></span></label>"
        )
    for identifier in sorted(selected - visible):
        body += (
            f'<label class="check"><input type="checkbox" name="repositories" '
            f'value="{escape(identifier, quote=True)}" checked><span>Unavailable repository ID '
            f"{escape(identifier)}<small>Still expected. Uncheck only to exclude deliberately.</small></span></label>"
        )
    if not repos and not selected:
        body += '<p class="empty muted">No repositories are accessible to this installation.</p>'
    return body + (
        '</div><div class="actions"><button>Save backup scope</button>'
        '<a class="button secondary" href="/#scope">Back to config</a></div></form>'
        '<p class="footer-note">All except selected requires All repositories access on GitHub. '
        "Exclusions never delete old backups. Only selected with an empty list backs up nothing. "
        "Selected repositories that lose access still alert.</p>"
        "</div></section>"
    )


def history_page(rows: list[dict[str, Any]], page: int, query: str) -> str:
    filtered = [r for r in rows if query.casefold() in f"{r['owner']}/{r['repository']}".casefold()]
    pages = max(1, (len(filtered) + 24) // 25)
    page = min(page, pages)
    body = (
        '<div class="intro"><p class="eyebrow">Activity</p><h1>Backup activity</h1>'
        '<p class="muted">The latest 250 completed repository checks across all accounts. '
        'Success includes checks where no Git refs changed.</p></div><form class="search" method="get" action="/history">'
        + field("q", "Find a repository", query, extra='maxlength="100"')
        + '<button class="secondary">Search</button></form><p class="description" id="timezone">Times shown in UTC.</p>'
        '<div class="table-wrap"><table><thead><tr><th scope="col">Repository</th>'
        '<th scope="col">Result</th><th scope="col">Completed</th><th scope="col">Duration</th></tr></thead><tbody>'
    )
    for row in filtered[(page - 1) * 25 : page * 25]:
        stamp = str(row["finished"])
        when = datetime.fromisoformat(stamp).strftime("%d %b %Y, %H:%M:%S UTC")
        status = {
            "success": "Success",
            "failed": "Failed",
            "partial": "Partial",
            "skipped": "Skipped",
        }.get(row["status"], "Unknown")
        body += (
            f"<tr><td>{escape(str(row['owner']))}/{escape(str(row['repository']))}</td>"
            f'<td><span class="badge">{status}</span></td><td><time data-local '
            f'datetime="{escape(stamp, quote=True)}" title="{escape(when, quote=True)}">{escape(when)}</time></td>'
            f"<td>{float(row['duration']):.1f} seconds</td></tr>"
        )
    if not filtered:
        body += (
            '<tr><td colspan="4" class="empty">No completed repository checks '
            "match yet. Activity starts with this deployment; old manifests are not imported.</td></tr>"
        )
    body += '</tbody></table></div><div class="pagination">'
    body += f'<span>{len(filtered)} of {len(rows)} retained checks · Page {page} of {pages}</span><div class="actions">'
    for target, label in ((page - 1, "Previous"), (page + 1, "Next")):
        if 1 <= target <= pages:
            href = "/history?" + urlencode({"page": target, "q": query})
            body += f'<a class="button secondary" href="{escape(href, quote=True)}">{label}</a>'
    return (
        body
        + '</div></div><p class="footer-note">Repository names are private configuration data. '
        "Raw errors, credentials, Git URLs and artifact paths are never copied into this activity list. For overall backup health, check the worker or Kuma.</p>"
    )
