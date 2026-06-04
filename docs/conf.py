"""Sphinx configuration for the ARC documentation.

Markdown is handled by MyST so the existing ``design/*.md`` material and the
new core docs can be authored in Markdown. The Python API reference is
generated from docstrings via autodoc + autosummary, with optional 3rd-party
dependencies mocked so the docs build on ReadTheDocs without them.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

# Make ``arc`` importable for autodoc even from a non-installed checkout
# (RTD installs the package, but local ``sphinx-build`` may not).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# -- Project information ------------------------------------------------------

project = "ARC"
author = "ARC contributors"
copyright = f"{datetime.now():%Y}, {author}"

try:
    from arc.version import __version__ as release
except Exception:  # noqa: BLE001 - docs must build even if arc import fails
    release = "0.1.0"
version = ".".join(release.split(".")[:2])

# -- General configuration ----------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinxcontrib.mermaid",
]

# Accept both Markdown (MyST) and reStructuredText.
source_suffix = {
    ".md": "markdown",
    ".rst": "restructuredtext",
}

templates_path = ["_templates"]
# ``README.md`` is the contributor build guide, not a site page — exclude it
# from the source tree so it isn't flagged as orphaned-from-the-toctree.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "README.md"]

# The landing page is index.md.
master_doc = "index"

# -- MyST configuration -------------------------------------------------------

myst_enable_extensions = [
    "colon_fence",   # ::: fenced directives
    "deflist",       # definition lists
    "fieldlist",
    "substitution",
    "tasklist",      # - [ ] checkboxes render
]
# NB: the MyST ``linkify`` extension is intentionally NOT enabled — it
# auto-links bare ``name.ext`` tokens (e.g. ``workflow.py``,
# ``local-packages.md``) in the included design docs into bogus ``http://``
# URLs that then fail linkcheck. Real URLs are written as explicit links.
myst_heading_anchors = 4  # auto heading anchors for cross-file links

# -- Autodoc / autosummary ----------------------------------------------------

autosummary_generate = True
autodoc_typehints = "description"
autodoc_member_order = "bysource"
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}
napoleon_google_docstring = True
napoleon_numpy_docstring = True

# Optional / heavy 3rd-party deps autodoc must not need to import.
autodoc_mock_imports = [
    "anthropic",
    "openai",
    "chromadb",
    "docker",
    "sim2l",
    "kubernetes",
    "cma",
    "skopt",
    "prompt_toolkit",
    "requests",
    "yaml",
    "typer",
    "fastapi",
    "uvicorn",
    "httpx",
    "pydantic",
    "dotenv",
]

# -- Intersphinx --------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- HTML output --------------------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_title = f"ARC {version} documentation"
html_theme_options = {
    "navigation_depth": 4,
    "collapse_navigation": False,
}

# Keep the build resilient while content is in progress: warn but don't fail
# on missing cross-refs. Tighten via nitpicky once the tree is complete
# (doc_todo.md B1).
nitpicky = False
# The included design/*.md docs now cross-reference each other with proper
# ``{doc}`` targets (B5 migration), so the missing-xref suppression is no
# longer needed. We keep only:
#   * myst.header — non-consecutive heading levels in the long included docs;
#   * misc.highlighting_failure — included code blocks that declare a language
#     (json/http/python) but contain illustrative tokens (``→``, ``|``,
#     ``...``); Sphinx already retries in relaxed mode and renders them fine.
suppress_warnings = [
    "myst.header",
    "misc.highlighting_failure",
]
