"""Validator role — runs *after* execution to grade outputs.

Distinct from ``adapter.validate_artifact``, which runs *before*
execution and checks schema / imports / safety. A Validator answers
"are these numerical outputs physically plausible?" — domain-specific
where the pre-execution check is generic.

Contract::

    class ValidatorAgent(AgentContract):
        async def validate(
            self,
            outputs: dict[str, Any],
            *,
            target: dict[str, Any] | None = None,
        ) -> ValidatorReport

The ``run`` method is implemented as a thin alias so subclasses still
satisfy ``AgentContract``'s ``@abstractmethod``.

Two implementations ship with arc:

  * :class:`NoopValidatorAgent` — the default. Returns ``passed=True``
    with no warnings, preserving today's behaviour for callers that
    didn't opt in to validation.
  * :class:`MaterialsValidatorAgent` (in :mod:`arc.packages.arc-materials`)
    — runs every applicable evaluator from arc-materials/evaluators/.
"""

from __future__ import annotations

import logging
from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ValidatorReport

logger = logging.getLogger(__name__)


class _BaseValidator(AgentContract):
    """Shared base providing the ``run`` alias for ``AgentContract``."""

    async def run(self, input_data) -> ValidatorReport:  # type: ignore[override]
        """Thin alias so the AgentContract abstractmethod is satisfied.

        Accepts either a bare ``outputs`` dict or a dict shaped
        ``{"outputs": ..., "target": ...}`` for callers that want to
        pass both in one argument.
        """
        if isinstance(input_data, dict) and "outputs" in input_data:
            return await self.validate(
                input_data["outputs"], target=input_data.get("target"),
            )
        return await self.validate(input_data)

    async def validate(
        self,
        outputs: dict[str, Any],
        *,
        target: dict[str, Any] | None = None,
    ) -> ValidatorReport:
        raise NotImplementedError


class NoopValidatorAgent(_BaseValidator):
    """Default validator: never fails, never warns.

    Preserves the legacy "no post-execution validation" behaviour for
    callers that didn't opt in. Swap to ``materials_evaluators`` (or a
    domain-specific validator) via ``/strategy validator …`` or a recipe.
    """

    name = "validator"
    description = (
        "No-op validator. Pass-through that always returns passed=True. "
        "Default so the loop's behaviour is unchanged until the user "
        "opts in to a real validator."
    )

    async def validate(
        self,
        outputs: dict[str, Any],
        *,
        target: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> ValidatorReport:
        return ValidatorReport(passed=True)
