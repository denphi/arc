#!/usr/bin/env python3
"""ARC interactive chat — thin launcher for ``arc.chat``.

Equivalent to ``arc chat``; kept so ``python3 examples/chat.py …`` keeps
working from a source checkout. All chat behaviour lives in
:mod:`arc.chat` (``arc/chat/loop.py``) — this file used to carry a
diverging ~2,000-line copy of it.

    python3 examples/chat.py --stub
    python3 examples/chat.py --provider openwebui --model gpt-oss:120b \\
        --url https://genai.rcac.purdue.edu/api
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arc.chat import main  # noqa: E402

if __name__ == "__main__":
    main()
