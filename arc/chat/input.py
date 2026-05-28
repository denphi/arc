"""Prompt-toolkit terminal input helpers.

Reads user input with line editing, history, and Emacs-style keybinds when
``prompt_toolkit`` is available; falls back to ``builtins.input`` otherwise.

Extracted from ``arc/chat/loop.py`` in Phase 1 with no behaviour change.
"""

from __future__ import annotations

import asyncio
import builtins
import os
from pathlib import Path


_PROMPT_SESSION = None
_PROMPT_RENDER = None


def chat_history_path() -> Path:
    """Return the path to the persistent line-history file.

    Honours ``$SIM2L_HOME`` so tests using ``isolated_sim2l_home`` won't
    write to the user's real history file.
    """
    root = Path(os.environ.get("SIM2L_HOME", Path.home() / ".sim2l" / "code"))
    root.mkdir(parents=True, exist_ok=True)
    return root / ".arc_chat_history"


def chat_input(prompt: str) -> str:
    """Read terminal input with history and Emacs-style editing when available."""
    session = _prompt_session()
    if session is False:
        return builtins.input(prompt)
    return session.prompt(_PROMPT_RENDER(prompt))


async def chat_input_async(prompt: str) -> str:
    """Async terminal input for use inside the chat event loop."""
    session = _prompt_session()
    if session is False:
        return await asyncio.to_thread(builtins.input, prompt)
    return await session.prompt_async(_PROMPT_RENDER(prompt))


def _prompt_session():
    """Create the shared prompt session lazily.

    Returns ``False`` when prompt_toolkit isn't usable (e.g. no TTY) so
    callers fall back to ``builtins.input``.
    """
    global _PROMPT_SESSION, _PROMPT_RENDER
    if _PROMPT_SESSION is None:
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.enums import EditingMode
            from prompt_toolkit.formatted_text import ANSI
            from prompt_toolkit.history import FileHistory

            _PROMPT_SESSION = PromptSession(
                history=FileHistory(str(chat_history_path())),
                editing_mode=EditingMode.EMACS,
            )
            _PROMPT_RENDER = ANSI
        except Exception:
            _PROMPT_SESSION = False
    return _PROMPT_SESSION


def reset_prompt_session() -> None:
    """Test hook: force the next ``chat_input`` call to re-initialize."""
    global _PROMPT_SESSION, _PROMPT_RENDER
    _PROMPT_SESSION = None
    _PROMPT_RENDER = None
