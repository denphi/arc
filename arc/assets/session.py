"""Session-scoped asset helpers."""

from __future__ import annotations

import os
from pathlib import Path

from arc.assets.store import FileStore
from arc.session import session_paths


def allowed_input_roots() -> tuple[Path, ...]:
    """Directories a session FileStore may import local paths from.

    Always includes the session inputs directory (``ARC_INPUTS_DIR``, default
    ``./data``). Additional roots may be granted via
    ``ARC_FILES_ALLOWED_ROOTS`` (os.pathsep-separated). Interactive CLI/chat
    attachment leaves the guard disabled so a local user can attach any path;
    HTTP API file import enables this guard unless trusted local mode is set
    (see ``arc.api.routes``).
    """
    from arc.assets.input_scan import DEFAULT_INPUTS_DIR

    roots: list[Path] = []
    inputs_dir = os.environ.get("ARC_INPUTS_DIR") or DEFAULT_INPUTS_DIR
    roots.append(Path(inputs_dir).expanduser())
    extra = os.environ.get("ARC_FILES_ALLOWED_ROOTS", "")
    for chunk in extra.split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            roots.append(Path(chunk).expanduser())
    return tuple(roots)


def session_file_store(session_id: str, *, restrict_roots: bool = False) -> FileStore:
    """Return the FileStore rooted in a session directory.

    ``restrict_roots`` enables the ``allowed_roots`` guard (imports must live
    under :func:`allowed_input_roots`). The interactive CLI/chat leave it off
    (a user attaching their own path is the trust model); the HTTP API turns it
    on unless ``ARC_FILES_TRUSTED_LOCAL`` is set, so a network caller can't
    import arbitrary server paths.
    """
    paths = session_paths(session_id)
    return FileStore(
        Path(paths["artifacts"]).parent / "files",
        allowed_roots=allowed_input_roots() if restrict_roots else None,
    )
