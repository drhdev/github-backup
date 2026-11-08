"""Versioned, validated setup settings and DigitalOcean connectivity checks."""

from __future__ import annotations

import re
import secrets
from typing import Any, cast

from github_backup.config import BackupConfig, S3Config

LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}\Z")


def normalize_settings(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Invalid setup settings")
    if "id" in data:  # Upgrade the original single-App file without losing its owners/key.
        data = {"version": 2, "apps": [data], "spaces": None}
    if data.get("version") != 2 or not isinstance(data.get("apps"), list):
        raise ValueError("Unsupported setup settings")
    ids = set()
    for app in data["apps"]:
        if (
            not isinstance(app, dict)
            or not str(app.get("id", "")).isdigit()
            or not LOGIN.fullmatch(str(app.get("slug", "")))
            or not isinstance(app.get("pem"), str)
            or "PRIVATE KEY-----" not in app["pem"]
            or not isinstance(app.get("owners"), list)
            or any(not isinstance(o, str) or not LOGIN.fullmatch(o) for o in app["owners"])
            or str(app["id"]) in ids
        ):
            raise ValueError("Invalid App settings")
        app["id"] = str(app["id"])
        ids.add(app["id"])
        scopes = app.setdefault("scopes", {})
        if not isinstance(scopes, dict) or any(o not in app["owners"] for o in scopes):
            raise ValueError("Invalid App scopes")
        for policy in scopes.values():
            if (
                not isinstance(policy, dict)
                or policy.get("mode") not in ("all", "include", "exclude", "none")
                or not isinstance(policy.get("repositories"), list)
                or any(not isinstance(r, str) or not r.isdigit() for r in policy["repositories"])
            ):
                raise ValueError("Invalid repository selection")
    if data.get("spaces") is not None:
        validate_spaces(data["spaces"])
    return cast(dict[str, Any], data)


def empty_settings() -> dict[str, Any]:
    return {"version": 2, "apps": [], "spaces": None}


def validate_spaces(data: dict[str, Any]) -> S3Config:
    if not isinstance(data, dict):
        raise ValueError("Invalid Spaces configuration")
    for name in ("access_key_id", "secret_access_key", "bucket", "region", "prefix"):
        if not isinstance(data.get(name), str) or not data[name].strip():
            raise ValueError("Spaces fields must not be empty")
    if not re.fullmatch(r"[a-z]{2,10}[0-9]{1,2}", data["region"]) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", data["bucket"]
    ):
        raise ValueError("Invalid Spaces region or bucket")
    if any(part in ("", ".", "..") for part in data["prefix"].split("/")) or any(
        ord(char) < 32 for char in data["prefix"]
    ):
        raise ValueError("Use a nonempty exclusive prefix without dot or empty segments")
    if len(data["prefix"]) > 200 or not re.fullmatch(r"[A-Za-z0-9_./-]+", data["prefix"]):
        raise ValueError("Use letters, digits, underscores, dots, dashes and slashes in the root")
    for name in ("access_key_id", "secret_access_key"):
        if not re.fullmatch(r"[A-Za-z0-9/+=_.-]{1,256}", data[name]):
            raise ValueError("Invalid Spaces credential format")
    s3 = S3Config(
        bucket=data["bucket"],
        prefix=data["prefix"],
        region=data["region"],
        endpoint_url=f"https://{data['region']}.digitaloceanspaces.com",
        access_key_id=data["access_key_id"],
        secret_access_key=data["secret_access_key"],
        server_side_encryption=None,
        distributed_lock=False,
    )
    BackupConfig(storage="s3", s3=s3).validated()
    return s3


def check_spaces(data: dict[str, Any]) -> None:
    """Test only a random disposable object under the reserved application prefix."""
    import boto3
    from botocore.config import Config

    s3 = validate_spaces(data)
    client = boto3.client(
        "s3",
        endpoint_url=s3.endpoint_url,
        region_name=s3.region,
        aws_access_key_id=s3.access_key_id,
        aws_secret_access_key=s3.secret_access_key,
        config=Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )
    try:
        if client.get_bucket_versioning(Bucket=s3.bucket).get("Status") in ("Enabled", "Suspended"):
            raise ValueError("Use an unversioned bucket for bounded storage")
        client.list_objects_v2(Bucket=s3.bucket, Prefix=s3.prefix.rstrip("/") + "/", MaxKeys=1)
        key = s3.prefix.rstrip("/") + "/_system/setup-check/" + secrets.token_hex(32)
        payload = secrets.token_bytes(32)
        try:
            client.put_object(Bucket=s3.bucket, Key=key, Body=payload, ACL="private")
            response = client.get_object(Bucket=s3.bucket, Key=key)
            try:
                if response["Body"].read(33) != payload:
                    raise ValueError("Spaces read-back mismatch")
            finally:
                response["Body"].close()
        finally:
            client.delete_object(Bucket=s3.bucket, Key=key)
    finally:
        client.close()
