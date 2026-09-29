"""Compatibility alias for :mod:`arc.session_state`.

Session-state persistence is not HTTP-specific (the orchestrator, chat and
UI all use it), so it lives beside :mod:`arc.session`. Importing this module
yields the same module object.
"""

import sys

from arc import session_state as _session_state

sys.modules[__name__] = _session_state
