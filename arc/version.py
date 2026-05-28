"""Package version.

Sourced from the installed package metadata via ``importlib.metadata`` so
``pyproject.toml`` remains the single source of truth. The literal fallback
is only used when this module is imported from a working tree that hasn't
been installed (e.g. when running tests directly without ``pip install -e``).
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Literal kept here so setup.py's read_version() regex can find it during
# the editable-install bootstrap (before the package is registered).
__version__ = "0.1.0"

try:
    __version__ = _pkg_version("arc")
except PackageNotFoundError:
    pass
