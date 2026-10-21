"""Application-specific exceptions."""


class BackupError(Exception):
    """Base class for expected backup failures."""


class ConfigurationError(BackupError):
    """Raised for invalid user configuration."""


class GitHubAPIError(BackupError):
    """Raised when the GitHub API cannot satisfy a request."""


class GitCommandError(BackupError):
    """Raised when a Git subprocess fails."""


class StorageError(BackupError):
    """Raised when a storage backend fails."""
