"""Free-text parsers used by the chat router and command layer.

Pure functions over strings → dicts/bools/strings. No I/O, no LLM
calls, no chat state. All have unit tests in
``tests/test_chat_helpers.py`` and ``tests/test_chat_classifier.py``.

The legacy underscored names (``_parse_target``, ``_parse_refinement_target``,
…) remain importable from ``arc.chat.loop`` via re-export.
"""

from __future__ import annotations

import re


# Words that are stripped from candidate target keys when parsing free
# text — they're meaningless on their own.
NOISE_WORDS: frozenset[str] = frozenset({
    "to", "of", "a", "an", "the", "for", "and", "or", "in", "at", "is", "be",
    "v", "version", "around", "about", "approximately", "roughly", "near",
})


# Unit alternation used by both the explicit ``key=value`` regex and
# the natural-language ``NUMBER UNIT`` scanner. Single source of truth
# so a new unit (e.g. ``cm⁻¹``) is added in one place.
_KEY_VALUE_UNITS = r"eV|meV|GPa|MPa|K|nm|%|GHz|THz"
# The natural-language scanner also accepts the spelled-out form
# ``angstrom`` since users write that in prose more often than ``Å``.
_NUM_UNIT_UNITS = _KEY_VALUE_UNITS + r"|angstrom"

# Regex banks reused across parsers.
_KEY_VALUE_RE = re.compile(
    rf"(\w+)[^\S\r\n]*[=:][^\S\r\n]*([0-9]+\.?[0-9]*)[^\S\r\n]*({_KEY_VALUE_UNITS})?",
    re.IGNORECASE,
)

_PHYSICS_RE = re.compile(
    r"(bandgap|band.gap|band_gap|effective.mass|temperature|strain|"
    r"energy|frequency|wavelength|pressure|stress|fermi|mobility|"
    r"conductivity|resistivity|dielectric|refractive|absorption)",
    re.IGNORECASE,
)

_NUM_UNIT_RE = re.compile(
    rf"([0-9]+\.?[0-9]*)\s*({_NUM_UNIT_UNITS})?",
    re.IGNORECASE,
)

_STRONG_TARGET_CONTEXT_RE = re.compile(
    r"(?<![a-z0-9_])(?:target|goal|set|change|update|near|around|"
    r"approximately|approx|within|close to)(?![a-z0-9_])",
    re.IGNORECASE,
)

_REFINEMENT_TARGET_VERB_RE = re.compile(
    r"(?<![a-z0-9_])(?:set|change|update|new)\s+(?:the\s+)?target(?![a-z0-9_])",
    re.IGNORECASE,
)

_REFINEMENT_TARGET_PREP_RE = re.compile(
    r"(?<![a-z0-9_])target(?![a-z0-9_])\s+"
    r"(?:to|near|around|approximately|approx|within|close to)\b",
    re.IGNORECASE,
)

_TARGET_CONTEXT_PROBE_RE = re.compile(
    r"\b(target|near|around|approximately|approx|within|close to|"
    r"as close as possible)\b",
    re.IGNORECASE,
)

_ARTIFACT_GENERATION_RE = re.compile(
    r"\b(?:write|generate|create|build|implement|reproduce|replicate|solve)\b"
    r".{0,120}"
    r"\b(?:code|solver|workflow|artifact|sim2l|fenics|dolfin|benchmark|paper)\b"
    r"|"
    r"\b(?:fenics|dolfin|sim2l|paper-replication)\b",
    re.IGNORECASE | re.DOTALL,
)


# ── parse_target ─────────────────────────────────────────────────────────


def parse_target(goal_text: str) -> dict:
    """Extract numeric targets from free text.

    Handles three forms in order of priority:
      1. Explicit ``key=value [unit]`` (highest priority).
      2. Natural language ``"NUMBER UNIT"`` adjacent to a physics noun.
      3. Shorthand: ``"target 1.1 eV"`` → assume ``bandgap_ev`` target.
    """
    text = goal_text
    targets: dict = {}

    # 1. Explicit key=value
    for m in _KEY_VALUE_RE.finditer(text):
        key = m.group(1).lower()
        if key in NOISE_WORDS:
            continue
        val = float(m.group(2))
        unit = (m.group(3) or "").lower()
        targets[f"{key}_{unit}" if unit else key] = val
    if targets:
        return targets

    # 2. NUMBER UNIT near a physics noun
    for nm in _NUM_UNIT_RE.finditer(text):
        val = float(nm.group(1))
        unit = (nm.group(2) or "").lower()
        start, end = nm.start(), nm.end()
        window = text[max(0, start - 30): end + 30]
        if not unit and not _STRONG_TARGET_CONTEXT_RE.search(window):
            continue
        pm = _PHYSICS_RE.search(window)
        if pm:
            key = re.sub(r"[\s\-]", "_", pm.group(0).lower())
            targets[f"{key}_{unit}" if unit else key] = val
    if targets:
        return targets

    # 3. Shorthand: "target 1.1 eV" → bandgap_ev: 1.1
    for nm in _NUM_UNIT_RE.finditer(text):
        val = float(nm.group(1))
        unit = (nm.group(2) or "").lower()
        if unit not in {"ev", "mev"}:
            continue
        start, end = nm.start(), nm.end()
        window = text[max(0, start - 30): end + 30].lower()
        if _TARGET_CONTEXT_PROBE_RE.search(window):
            targets[f"bandgap_{unit}"] = val
            break

    return targets


def is_artifact_generation_goal(goal_text: str) -> bool:
    """True when free text asks ARC to build/reproduce a code artifact.

    Artifact-building prompts often contain numbered instructions and labels
    such as ``skill chain:``. Those should guide deliverables, not become
    optimization targets for the generic research loop.
    """
    return bool(_ARTIFACT_GENERATION_RE.search(goal_text or ""))


# ── parse_refinement_target ──────────────────────────────────────────────


def parse_refinement_target(refinement: str) -> dict:
    """Return target updates ONLY when a refinement explicitly changes target.

    Distinct from ``parse_target``: a free-text refinement like
    ``"smaller thickness"`` mentions no target — we must not invent
    one. We only accept clear "set/change/update target" phrasing or
    explicit ``key=value``.
    """
    # Explicit key=value form is always a target update.
    if re.search(r"\w+\s*[=:]\s*[0-9]+\.?[0-9]*", refinement):
        return parse_target(refinement)
    # "set the target to X", "change target to X", etc.
    if _REFINEMENT_TARGET_VERB_RE.search(refinement):
        return parse_target(refinement)
    # "target to / near / around X"
    if _REFINEMENT_TARGET_PREP_RE.search(refinement):
        return parse_target(refinement)
    return {}


# ── parse_target_command ─────────────────────────────────────────────────


def parse_target_command(
    raw: str,
    current_target: dict | None = None,
) -> tuple[str, dict, str]:
    """Parse ``/target`` commands.

    Returns ``(action, target, detail)`` where action is one of:
      * ``"show"``   — print the current target
      * ``"set"``    — replace or merge the target with new values
      * ``"clear"``  — drop the active target
      * ``"error"``  — couldn't parse; detail explains
    """
    current_target = dict(current_target or {})
    parts = raw.split(maxsplit=1)
    if len(parts) == 1:
        return "show", current_target, ""

    arg = parts[1].strip()
    if not arg:
        return "show", current_target, ""

    lowered = arg.lower()
    if lowered in {"clear", "reset", "none", "off"}:
        return "clear", {}, ""

    merge = False
    for prefix in ("update ", "merge ", "add "):
        if lowered.startswith(prefix):
            merge = True
            arg = arg[len(prefix):].strip()
            break

    lowered = arg.lower()
    for prefix in ("set ", "replace "):
        if lowered.startswith(prefix):
            arg = arg[len(prefix):].strip()
            break

    parsed = parse_target(arg)
    if not parsed:
        return (
            "error",
            current_target,
            "Could not parse a numeric target. Try: /target result=5 "
            "or /target bandgap_ev=1.1",
        )

    if merge:
        parsed = {**current_target, **parsed}
    return "set", parsed, ""


# ── refinement classifiers ───────────────────────────────────────────────

_REBUILD_PARAM_RE = re.compile(
    r"\b(parameter|input|thickness|temperature|mass|offset)\b"
)

_REBUILD_LOGIC_RE = re.compile(
    r"\b(output|return|metric|score|compliance|formula|calculation|"
    r"schema|workflow|artifact|bug|wrong|broken|missing|always)\b"
)


def refinement_needs_artifact_rebuild(refinement: str) -> bool:
    """True when a refinement describes broken artifact logic that needs
    a new artifact build, not just a parameter retry.

    Heuristic: if the refinement mentions a parameter word AND also a
    logic word, treat as rebuild. If only a parameter word, just a
    param retry — return False.
    """
    text = refinement.lower()
    if _REBUILD_PARAM_RE.search(text):
        if not _REBUILD_LOGIC_RE.search(text):
            return False
    return bool(_REBUILD_LOGIC_RE.search(text))


# ── normalize_chat_command ───────────────────────────────────────────────


def normalize_chat_command(raw: str) -> str:
    """Accept slash commands AND common backslash typos like ``\\help``."""
    stripped = raw.strip()
    if stripped.startswith("\\"):
        return "/" + stripped[1:]
    return stripped


# ── token-relevance / refinement detection ──────────────────────────────


_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "that", "this", "what", "when", "where",
    "why", "how", "can", "could", "would", "should", "want", "need",
    "help", "please", "about", "from", "into", "then", "than", "your",
    "model", "question", "answer", "explain",
})

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

_REFINEMENT_WORDS: frozenset[str] = frozenset({
    "target", "output", "input", "parameter", "schema", "workflow",
    "artifact", "metric", "score", "compliance", "formula", "calculation",
    "bug", "wrong", "broken", "missing", "always", "increase", "decrease",
    "smaller", "larger", "higher", "lower", "adjust", "fix", "address",
})


def tokens_for_relevance(text: str) -> set[str]:
    """Return content tokens from ``text`` for relevance scoring.

    Lower-cases, splits on non-alphanumerics, keeps tokens of length ≥3
    that aren't in ``_STOP_WORDS``.
    """
    return {
        part
        for part in _TOKEN_SPLIT_RE.split(text.lower())
        if len(part) >= 3 and part not in _STOP_WORDS
    }


def is_related_refinement(
    text: str,
    primary_goal: str,
    artifact,
    ctx,
) -> bool:
    """Decide whether free text should be treated as a refinement of the
    active research goal.

    Returns True when the input mentions concepts already present in
    the goal / target / schema / artifact metadata, OR when it uses
    refinement-specific vocabulary (e.g. "smaller", "fix the bug")
    alongside a context-tokens overlap.

    Slash commands and backslash typos are never refinements.
    """
    lowered = text.lower()
    if lowered.startswith(("/", "\\")):
        return False

    context_texts: list = [primary_goal or ""]
    target = ctx.memory.get("target", {})
    context_texts.extend(target.keys())
    registry = ctx.memory.get("schema_registry", {})
    context_texts.extend(registry.keys())

    if artifact is not None:
        context_texts.extend([
            getattr(artifact, "name", ""),
            getattr(artifact, "description", None)
            or (getattr(artifact, "metadata", {}) or {}).get("description", "")
            or (getattr(artifact, "metadata", {}) or {}).get("hypothesis", ""),
        ])
        meta = getattr(artifact, "metadata", {}) or {}
        context_texts.extend((meta.get("sim2l_inputs", {}) or {}).keys())
        context_texts.extend((meta.get("sim2l_outputs", {}) or {}).keys())

    # Exact key/name references are strong evidence (e.g. a schema key
    # like target_bandgap_compliance).
    for value in context_texts:
        v = str(value).lower()
        if v and len(v) >= 3 and v in lowered:
            return True

    context_tokens: set[str] = set()
    for value in context_texts:
        context_tokens.update(tokens_for_relevance(str(value)))
    text_tokens = tokens_for_relevance(text)
    if len(text_tokens & context_tokens) >= 2:
        return True

    return bool(text_tokens & _REFINEMENT_WORDS and text_tokens & context_tokens)


# ── goal composition ────────────────────────────────────────────────────


def build_refined_goal(primary_goal: str, refinements: list[str]) -> str:
    """Compose the full goal text from primary goal + accumulated refinements."""
    if not refinements:
        return primary_goal
    ref_block = "; ".join(refinements)
    return (
        f"{primary_goal}\n\n"
        f"Additional constraints and refinements:\n{ref_block}"
    )
