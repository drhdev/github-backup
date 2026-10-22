"""Back up GitHub repositories and gists."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("github-backup")
except PackageNotFoundError:  # pragma: no cover - only possible from an uninstalled source tree
    __version__ = "0.0.0+unknown"
