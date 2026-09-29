"""Compatibility alias for :mod:`arc.core.plan_mode`.

Plan mode moved to core so non-chat layers can check it without importing
``arc.chat``. Importing this module yields the *same* module object, so the
global flag is shared by both names.
"""

import sys

from arc.core import plan_mode as _core_plan_mode

sys.modules[__name__] = _core_plan_mode
