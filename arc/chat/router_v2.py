"""V2 router: LLM emits a tool call instead of an intent string.

Single function: ``route_via_tools(text, *, provider, registry, …)``.

Returns a ``ToolDecision`` describing which tool to dispatch and with
what args. The chat loop's job is then ``await registry.dispatch(state,
decision.tool, decision.args)``.

This is the OpenHarness-inspired path. It's behind the ``ARC_CHAT_V2``
flag; the heuristic ``arc.chat.router.route_input`` is still the default.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from arc.chat.tools.registry import ToolRegistry


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDecision:
    tool: str
    args: dict
    raw_reply: str = ""


_SYSTEM_TEMPLATE = """\
You are the router for an interactive research chat. The user has
typed a message; classify the intent by emitting EXACTLY ONE tool call.

Reply with a single JSON object on one line:

    {{"tool": "<name>", "args": <arguments>}}

Available tools:
{tool_descriptions}

Context:
{context_block}

Rules:
  * Pick exactly one tool. Do not emit prose or multiple objects.
  * Tool args MUST validate against the tool's schema.
  * If you're unsure, prefer answer_question over starting an action.
  * If there is no active research goal, never call refine_goal.
"""


def _build_system_prompt(registry: ToolRegistry, has_active_goal: bool) -> str:
    descriptions = []
    for tool in registry.all():
        descriptions.append(f"  - {tool.name}: {tool.description}")
    context = "There is an active research goal." if has_active_goal else "No research goal is active."
    return _SYSTEM_TEMPLATE.format(
        tool_descriptions="\n".join(descriptions),
        context_block=context,
    )


def _parse_tool_call(text: str) -> tuple[str, dict] | None:
    """Pull the first JSON object out of the reply and return (tool, args).

    Tolerates the LLM padding with markdown code fences or extra text by
    looking for the first ``{`` and matching its closing brace. Returns
    None if no parseable object found.
    """
    if not text:
        return None

    # Strip common code-fence wrappers
    stripped = re.sub(r"```(?:json)?\s*\n?", "", text).rstrip("`")

    # Find the first balanced JSON object
    depth = 0
    start = -1
    for i, ch in enumerate(stripped):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                blob = stripped[start:i + 1]
                try:
                    parsed = json.loads(blob)
                except json.JSONDecodeError:
                    return None
                if not isinstance(parsed, dict):
                    return None
                tool = parsed.get("tool")
                args = parsed.get("args")
                if not isinstance(tool, str):
                    return None
                if args is None:
                    args = {}
                if not isinstance(args, dict):
                    return None
                return tool, args
    return None


async def route_via_tools(
    text: str,
    *,
    provider,
    registry: ToolRegistry,
    has_active_goal: bool,
    fallback_tool: str = "answer_question",
    max_tokens: int = 256,
) -> ToolDecision:
    """Ask the LLM to pick a tool. Returns a ToolDecision.

    On any failure (no provider, parse error, unknown tool, validation
    error) the function falls back to the safest tool: ``answer_question``
    with the user's raw text. This keeps the chat responsive even when
    the router LLM is misbehaving.
    """
    if provider is None:
        return ToolDecision(
            tool=fallback_tool,
            args={"text": text},
            raw_reply="(no provider)",
        )

    system = _build_system_prompt(registry, has_active_goal)
    user_prompt = f"User message: {text!r}"

    try:
        reply = await provider.complete(
            user_prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("router LLM call failed: %s — falling back", exc)
        return ToolDecision(
            tool=fallback_tool,
            args={"text": text},
            raw_reply=f"(error: {type(exc).__name__})",
        )

    parsed = _parse_tool_call(reply)
    if parsed is None:
        logger.info("router LLM returned unparseable reply, falling back: %r", reply[:200])
        return ToolDecision(
            tool=fallback_tool,
            args={"text": text},
            raw_reply=reply,
        )

    tool_name, args = parsed
    tool = registry.get(tool_name)
    if tool is None:
        logger.info("router emitted unknown tool %r — falling back", tool_name)
        return ToolDecision(
            tool=fallback_tool,
            args={"text": text},
            raw_reply=reply,
        )

    # Validate args against the tool's Pydantic schema BEFORE returning the
    # decision. Downstream callers (the chat loop adapter) read args
    # directly without going through ToolRegistry.dispatch, so without this
    # check a misbehaving LLM could inject unvalidated keys/values.
    try:
        validated = tool.schema(**args)
    except Exception as exc:  # noqa: BLE001 — Pydantic ValidationError or anything weirder
        logger.info(
            "router emitted invalid args for %r (%s) — falling back",
            tool_name, type(exc).__name__,
        )
        return ToolDecision(
            tool=fallback_tool,
            args={"text": text},
            raw_reply=reply,
        )

    # Pass the cleaned dict (Pydantic normalised types, dropped extras).
    return ToolDecision(
        tool=tool_name,
        args=validated.model_dump(),
        raw_reply=reply,
    )
