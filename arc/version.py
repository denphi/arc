"""Package version.

Sourced from the installed package metadata via ``importlib.metadata`` so
``pyproject.toml`` remains the single source of truth. The literal fallback
is only used when this module is imported from a working tree that hasn't
been installed (e.g. when running tests directly without ``pip install -e``).
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("arc")
except PackageNotFoundError:
    # Editable / source-tree fallback; keep this in sync with pyproject.toml
    # only when the package is not installed.
    __version__ = "0.1.0"
