"""Single-owner registration and server-side sessions with fail-closed persistent state."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

AUTH_DIRECTORY = Path("/cache/setup-auth")
SESSION_COOKIE = "__Host-github-backup-session"
SESSION_IDLE = 30 * 60
SESSION_LIFETIME = 8 * 3600


class AuthLockedError(Exception):
    """Persistent authentication is damaged; never reopen registration automatically."""


class AuthRateLimited(Exception):
    """Bound password hashing CPU and online guesses independently of proxy headers."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt),
        n=32768,
        r=8,
        p=3,
        dklen=32,
        maxmem=64 * 1024**2,
    ).hex()


def validate_password(password: str) -> None:
    if (
        not 16 <= len(password) <= 128
        or len(set(password)) < 6
        or password.casefold()
        in {"password12345678", "1234567890123456", "qwertyuiopasdfgh", "abcdefghijklmnop"}
    ):
        raise ValueError("Use a unique 16-128 character password or passphrase")


class AuthStore:
    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory or AUTH_DIRECTORY

    @contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        """Serialize hashes/transactions across threads and processes to bound memory use."""
        root = self.directory
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077:
            raise AuthLockedError("Authentication directory must be private")
        fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            database = root / "auth.sqlite3"
            marker = root.parent / ".setup-owner-deployment"
            try:
                if not marker.exists():
                    if database.exists() or (root / "registration-code").exists():
                        raise AuthLockedError("Authentication deployment marker is missing")
                    deployment = secrets.token_hex(32)
                    self._exclusive_file(marker, json.dumps({"deployment": deployment}))
                    # If initialization is interrupted after sealing the directory, fail closed.
                    self._exclusive_file(database, "")
                    code = secrets.token_hex(32)
                    self._exclusive_file(root / "registration-code", code + "\n")
                    with closing(sqlite3.connect(database)) as db, db:
                        db.executescript(
                            "CREATE TABLE metadata (deployment TEXT, registered INTEGER, "
                            "bootstrap TEXT, attempts INTEGER, window REAL);"
                            "CREATE TABLE account (id INTEGER PRIMARY KEY CHECK(id=1), "
                            "username TEXT NOT NULL, salt TEXT NOT NULL, hash TEXT NOT NULL);"
                            "CREATE TABLE sessions (token TEXT PRIMARY KEY, csrf TEXT NOT NULL, "
                            "created REAL NOT NULL, seen REAL NOT NULL);"
                        )
                        db.execute(
                            "INSERT INTO metadata VALUES (?,0,?,0,0)", (deployment, digest(code))
                        )
                    self._sync_directory()
                if not database.is_file() or database.stat().st_mode & 0o077:
                    raise AuthLockedError("Authentication database is missing or not private")
                deployment = json.loads(marker.read_text())["deployment"]
                db = sqlite3.connect(f"file:{database}?mode=rw", uri=True, timeout=5)
                try:
                    db.execute("PRAGMA secure_delete=ON")
                    db.row_factory = sqlite3.Row
                    meta = db.execute("SELECT * FROM metadata").fetchall()
                    account = db.execute("SELECT * FROM account").fetchall()
                    if (
                        len(meta) != 1
                        or meta[0]["deployment"] != deployment
                        or meta[0]["registered"] not in (0, 1)
                        or len(account) != meta[0]["registered"]
                    ):
                        raise AuthLockedError("Authentication registration seal is invalid")
                    if not account and (
                        not re.fullmatch(r"[a-f0-9]{64}", meta[0]["bootstrap"])
                        or not (root / "registration-code").is_file()
                    ):
                        raise AuthLockedError("Registration state is incomplete")
                    if account:
                        saved = account[0]
                        if (
                            not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", saved["username"])
                            or not re.fullmatch(r"[a-f0-9]{32}", saved["salt"])
                            or not re.fullmatch(r"[a-f0-9]{64}", saved["hash"])
                        ):
                            raise AuthLockedError("Authentication account is invalid")
                    with db:
                        yield db
                finally:
                    db.close()
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
                raise AuthLockedError("Authentication state is unavailable") from exc

    def _exclusive_file(self, path: Path, value: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())

    def _sync_directory(self) -> None:
        for path in (self.directory, self.directory.parent):
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def registered(self) -> bool:
        with self.database() as db:
            return bool(db.execute("SELECT registered FROM metadata").fetchone()[0])

    def registration_code(self) -> str:
        if self.registered():
            raise AuthLockedError("Registration is permanently closed")
        return (self.directory / "registration-code").read_text().strip()

    @staticmethod
    def _attempt(db: sqlite3.Connection, now: float) -> bool:
        row = db.execute("SELECT attempts, window FROM metadata").fetchone()
        attempts, window = row[0], row[1]
        if now - window >= 60:
            attempts, window = 0, now
        if attempts >= 5:
            return False
        db.execute("UPDATE metadata SET attempts=?, window=?", (attempts + 1, window))
        return True

    def register(self, username: str, password: str, confirmation: str, code: str) -> bool:
        with self.database() as db:
            meta = db.execute("SELECT * FROM metadata").fetchone()
            if meta["registered"]:
                return False
            if not self._attempt(db, time.time()):
                raise AuthRateLimited()
            if not hmac.compare_digest(digest(code), meta["bootstrap"]):
                return False
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username) or not hmac.compare_digest(
                password.encode(), confirmation.encode()
            ):
                return False
            try:
                validate_password(password)
            except ValueError:
                return False
            salt = secrets.token_hex(16)
            hashed = password_hash(password, salt)
            db.execute("INSERT INTO account VALUES (1,?,?,?)", (username, salt, hashed))
            db.execute("UPDATE metadata SET registered=1, bootstrap='', attempts=0")
        # The database seal closes registration even if this cleanup is interrupted.
        (self.directory / "registration-code").unlink(missing_ok=True)
        return True

    def login(self, username: str, password: str) -> bool:
        with self.database() as db:
            if not self._attempt(db, time.time()):
                raise AuthRateLimited()
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
            if account is None or not 1 <= len(password) <= 128:
                return False
            hashed = password_hash(password, account["salt"])
            valid = hmac.compare_digest(hashed, account["hash"])
            valid &= hmac.compare_digest(
                username.casefold().encode(), account["username"].casefold().encode()
            )
            if valid:
                db.execute("UPDATE metadata SET attempts=0")
            return valid

    def username(self) -> str:
        with self.database() as db:
            return str(db.execute("SELECT username FROM account WHERE id=1").fetchone()[0])

    def reauthenticate(self, token: str, browser: str, password: str) -> bool:
        with self.database() as db:
            now = time.time()
            if not self._attempt(db, now):
                raise AuthRateLimited()
            session = db.execute(
                "SELECT * FROM sessions WHERE token=?", (digest(token),)
            ).fetchone()
            if (
                session is None
                or now - session["created"] >= SESSION_LIFETIME
                or now - session["seen"] >= SESSION_IDLE
                or not hmac.compare_digest(session["csrf"], digest(browser))
                or not 1 <= len(password) <= 128
            ):
                return False
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
            valid = hmac.compare_digest(password_hash(password, account["salt"]), account["hash"])
            if valid:
                db.execute("UPDATE metadata SET attempts=0")
            return valid

    def change_account(
        self,
        token: str,
        browser: str,
        current_password: str,
        username: str,
        password: str,
        confirmation: str,
    ) -> bool:
        with self.database() as db:
            now = time.time()
            if not self._attempt(db, now):
                raise AuthRateLimited()
            session = db.execute(
                "SELECT * FROM sessions WHERE token=?", (digest(token),)
            ).fetchone()
            if (
                session is None
                or now - session["created"] >= SESSION_LIFETIME
                or now - session["seen"] >= SESSION_IDLE
                or not hmac.compare_digest(session["csrf"], digest(browser))
                or not 1 <= len(current_password) <= 128
            ):
                return False
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
            if not hmac.compare_digest(
                password_hash(current_password, account["salt"]), account["hash"]
            ):
                return False
            username = username.strip() or account["username"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
                return False
            if not hmac.compare_digest(password.encode(), confirmation.encode()):
                return False
            salt, hashed = account["salt"], account["hash"]
            if password:
                try:
                    validate_password(password)
                except ValueError:
                    return False
                if hmac.compare_digest(password.encode(), current_password.encode()):
                    return False
                salt = secrets.token_hex(16)
                hashed = password_hash(password, salt)
            elif username == account["username"]:
                return False
            db.execute(
                "UPDATE account SET username=?, salt=?, hash=? WHERE id=1", (username, salt, hashed)
            )
            db.execute("DELETE FROM sessions")
            db.execute("UPDATE metadata SET attempts=0")
            return True

    def create_session(self, browser: str) -> str:
        token = secrets.token_hex(32)
        now = time.time()
        with self.database() as db:
            db.execute(
                "DELETE FROM sessions WHERE created<? OR seen<?",
                (now - SESSION_LIFETIME, now - SESSION_IDLE),
            )
            db.execute(
                "DELETE FROM sessions WHERE token IN "
                "(SELECT token FROM sessions ORDER BY seen DESC LIMIT -1 OFFSET 31)"
            )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)", (digest(token), digest(browser), now, now)
            )
        return token

    def authenticated(self, token: str, browser: str) -> bool:
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            return False
        now = time.time()
        with self.database() as db:
            row = db.execute("SELECT * FROM sessions WHERE token=?", (digest(token),)).fetchone()
            if (
                row is None
                or now - row["created"] >= SESSION_LIFETIME
                or now - row["seen"] >= SESSION_IDLE
                or not hmac.compare_digest(row["csrf"], digest(browser))
            ):
                db.execute("DELETE FROM sessions WHERE token=?", (digest(token),))
                return False
            db.execute("UPDATE sessions SET seen=? WHERE token=?", (now, digest(token)))
            return True

    def logout(self, token: str) -> None:
        with self.database() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (digest(token),))


def main() -> None:
    import sys

    if sys.argv[1:] != ["registration-code"]:
        raise SystemExit("Usage: python -m github_backup.config_auth registration-code")
    try:
        print(AuthStore().registration_code())
    except AuthLockedError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
