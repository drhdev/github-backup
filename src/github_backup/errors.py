"""Application-specific exceptions."""


class BackupError(Exception):
    """Base class for expected backup failures."""


class ConfigurationError(BackupError):
    """Raised for invalid user configuration."""


class GitHubAPIError(BackupError):
    """Raised when the GitHub API cannot satisfy a request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitCommandError(BackupError):
    """Raised when a Git subprocess fails."""


class StorageError(BackupError):
    """Raised when a storage backend fails."""
