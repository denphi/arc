"""Zero-dependency ``.env`` loading.

Nothing in ARC loaded ``.env`` files before this — env vars only took
effect if the shell already exported them, so the documented "put your
key in ``.env``" flow silently didn't work. ``load_env`` fixes that
without adding a ``python-dotenv`` dependency: it's a small, forgiving
``KEY=VALUE`` parser.

**Precedence:** the real process environment always wins. Files only
*fill in* variables that aren't already set, so an explicit
``export FOO=bar`` (or a value injected by a parent process / CI) is never
clobbered by a checked-in ``.env``. Search order, lowest priority first:

    ~/.arc/.env   →   ./.env   →   process environment (authoritative)

so a project-local ``./.env`` overrides the user-global ``~/.arc/.env``,
and both yield to anything already in ``os.environ``.

The loader is idempotent and cheap; entry points (kernel, CLI, API, UI)
call it once at startup before packages load.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Guard so repeated calls (multiple entry points in one process) are no-ops
# after the first successful load.
_loaded = False


def _candidate_files() -> list[Path]:
    """``.env`` files in *lowest-to-highest* precedence order."""
    return [
        Path.home() / ".arc" / ".env",
        Path.cwd() / ".env",
    ]


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a dict. Forgiving, no interpolation.

    - Blank lines and ``#`` comments are ignored.
    - A leading ``export`` keyword is stripped (so a shell-sourceable file works).
    - Surrounding single or double quotes on the value are removed.
    - Lines without ``=`` or with an empty key are skipped (not an error).

    Whitespace around the key and around the value is trimmed.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def load_env(*, force: bool = False) -> dict[str, str]:
    """Populate ``os.environ`` from the ``.env`` search path.

    Returns the dict of keys this call actually set (those not already in
    the environment). The process environment is authoritative — existing
    keys are never overwritten. Idempotent: a no-op after the first call
    unless ``force=True``.
    """
    global _loaded
    if _loaded and not force:
        return {}

    applied: dict[str, str] = {}
    # Iterate lowest-priority first; a later file (./.env) overrides an
    # earlier one (~/.arc/.env) for keys still unset in the environment.
    file_values: dict[str, str] = {}
    for path in _candidate_files():
        try:
            if path.is_file():
                file_values.update(parse_env_file(path.read_text(encoding="utf-8")))
        except OSError as exc:
            logger.debug("Could not read env file %s: %s", path, exc)

    for key, value in file_values.items():
        if key not in os.environ:  # process env wins
            os.environ[key] = value
            applied[key] = value

    _loaded = True
    if applied:
        logger.debug("Loaded %d var(s) from .env: %s", len(applied), sorted(applied))
    return applied
