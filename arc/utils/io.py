"""Filesystem helpers shared across ARC.

Review item #T22: ``_atomic_write_text`` previously lived inside
``arc/memory/artifact_registry.py``. It's a generic primitive useful
anywhere we need crash-safe text persistence (the results store, the
provenance log, a future config writer, etc.), so it lives here now.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Creates a sibling temp file in the same directory (so the rename is on
    the same filesystem), writes the full payload, fsyncs, and uses
    ``os.replace`` for the swap — which is atomic on POSIX and Windows.

    Originally added as review item #A10: replaces a naive
    ``Path.write_text`` that left consumers reading half-written JSON when
    the writer was killed mid-write or hit a disk-full error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None
    tmp_name: str | None
    fd, tmp_name = None, None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None  # ownership handed to the file object
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync isn't supported on some filesystems (eg. tmpfs);
                # not fatal — the os.replace below is still atomic.
                pass
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name is not None and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
