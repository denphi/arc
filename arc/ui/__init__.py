"""Standalone browser UI for ARC.

The UI is intentionally independent of :mod:`arc.chat`. It talks to ARC
through the core orchestration, memory, runtime, and session modules.
"""

from arc.ui.server import create_app

__all__ = ["create_app"]
