from arc.contracts.agent import AgentContract
from arc.packages.arc_sim2l_agents.hypothesis import stub_candidates
from arc.schemas.research import (  # noqa: F401 — needed at module level
    ResearchGoal,
    ResearchProposal,
)


def _search_catalog(catalog_url: str, keywords: list[str], limit: int = 5) -> list[dict]:
    """Search the catalog service for simulations matching any of the keywords.
    Returns an empty list if the service is unreachable."""
    try:
        import requests
        query = " ".join(keywords[:4])
        resp = requests.get(
            f"{catalog_url.rstrip('/')}/simulations/search",
            params={"query": query, "limit": limit, "status": "active"},
            timeout=3,
        )
        if resp.status_code == 200:
            return resp.json() or []
    except Exception:
        pass
    return []


def _search_results(results_url: str, sim_name: str, limit: int = 3) -> list[dict]:
    """Fetch recent results for a simulation from the results service."""
    try:
        import requests
        resp = requests.post(
            f"{results_url.rstrip('/')}/search",
            json={
                "simulation_name": sim_name,
                "input_filters": {},
                "output_filters": {},
                "limit": limit,
            },
            timeout=3,
        )
        if resp.status_code == 200:
            return resp.json().get("results", [])
    except Exception:
        pass
    return []


def _goal_keywords(goal_text: str) -> list[str]:
    """Extract meaningful keywords from a goal string."""
    import re
    stop = {"a", "an", "the", "of", "to", "for", "and", "or", "in", "at", "by",
            "via", "with", "using", "that", "this", "is", "are", "be", "i", "want"}
    words = re.sub(r"[^a-z0-9 ]", " ", goal_text.lower()).split()
    return [w for w in words if w not in stop and len(w) > 2]


def _retry_context_block(memory: dict) -> str:
    """Summarize recent rebuild reasons for ideation prompts."""
    notes = memory.get("retry_context") or []
    if not notes:
        return ""
    lines = ["Recent ARC retry context (do not repeat the failed output schema):"]
    for note in notes[-3:]:
        if not isinstance(note, dict):
            continue
        required = note.get("required_outputs") or []
        actual = list((note.get("actual_outputs") or {}).keys())
        summary = note.get("review_summary") or ""
        lines.append(
            f"  - reason={note.get('reason', 'retry')} required_outputs={required} "
            f"actual_outputs={actual} "
            f"artifact={note.get('artifact_name') or note.get('artifact_id')}"
        )
        if summary:
            lines.append(f"    review={summary[:160]}")
    return "\n".join(lines) if len(lines) > 1 else ""


class IdeatorAgent(AgentContract):
    name = "ideator"
    description = "Generates a structured research proposal from a user goal."

    async def run(self, input_data: ResearchGoal) -> ResearchProposal:
        goal = input_data if isinstance(input_data, ResearchGoal) else ResearchGoal(**input_data)

        # ── Catalog search ────────────────────────────────────────────────
        # The Searcher role decides ranking strategy (keyword vs embedding
        # vs ...). The ideator only consumes the ranked hits + prior runs.
        catalog_hits: list[dict] = []
        prior_results: list[dict] = []
        try:
            from arc.core.strategies import resolve_role as _resolve
            overrides = self.context.memory.get("strategy_overrides") or {}
            try:
                from arc.core.config import load_arc_toml
                _p, config = load_arc_toml()
            except Exception:
                config = {}
            registry = self.context.memory.get("component_registry")
            loaded_packages = set(registry.list_packages()) if registry is not None else None
            SearcherCls = _resolve(
                "searcher", overrides=overrides, config=config,
                loaded_packages=loaded_packages,
            )
            searcher = SearcherCls(context=self.context)
            search_result = await searcher.search(goal)
            catalog_hits = search_result.catalog_hits
            prior_results = search_result.prior_results
        except Exception as exc:  # noqa: BLE001 — search is best-effort
            import logging as _logging
            _logging.getLogger(__name__).debug("Searcher failed: %s", exc)

        # ── Session context ───────────────────────────────────────────────
        run_history: list[dict] = self.context.memory.get("run_history", [])
        schema_registry: dict = self.context.memory.get("schema_registry", {})

        # Store hits in context so chat.py can short-circuit the build phase.
        self.context.memory["catalog_hits"] = catalog_hits
        self.context.memory["catalog_prior_results"] = prior_results

        provider = self.context.memory.get("provider")
        if provider:
            # Build context blocks for the prompt.
            catalog_block = ""
            if catalog_hits:
                lines = ["Existing simulations in the catalog (consider reusing one):"]
                for h in catalog_hits[:3]:
                    name = h.get("name", "?")
                    desc = (h.get("description") or "")[:100]
                    ins = list((h.get("input_schema") or {}).keys())
                    outs = list((h.get("output_schema") or {}).keys())
                    lines.append(f"  - {name}: {desc}  inputs={ins}  outputs={outs}")
                catalog_block = "\n".join(lines)

            results_block = ""
            if prior_results:
                lines = [f"Prior results for '{catalog_hits[0].get('name', '')}':"]
                for r in prior_results[:3]:
                    lines.append(
                        f"  inputs={r.get('input_params', {})}  "
                        f"outputs={r.get('output_params', {})}"
                    )
                results_block = "\n".join(lines)

            history_block = ""
            if run_history:
                lines = ["Recent session runs (avoid repeating these exact parameters):"]
                for r in run_history[-3:]:
                    lines.append(f"  inputs={r.get('inputs', {})}  outputs={r.get('outputs', {})}")
                history_block = "\n".join(lines)

            registry_block = ""
            if schema_registry:
                canonical = list(schema_registry.keys())
                registry_block = f"Canonical output quantities already tracked: {canonical}"

            retry_block = _retry_context_block(self.context.memory)

            context_section = "\n\n".join(
                filter(None, [
                    catalog_block,
                    results_block,
                    history_block,
                    registry_block,
                    retry_block,
                ])
            )

            # Prefer a domain-specific prompt template when the active
            # goal has a domain that matches a package's prompts/*.md
            # H1. Falls back to the built-in template below when no
            # match is found. See arc.core.prompts.find_prompt.
            from arc.core.prompts import find_prompt, safe_format
            overrides = self.context.memory.get("prompt_overrides") or None
            template = find_prompt(
                "hypothesis_generation",
                domain=goal.domain,
                overrides=overrides,
            )

            if template is not None:
                prompt = safe_format(
                    template,
                    goal=goal.goal,
                    domain=goal.domain or "general",
                    constraints=goal.constraints,
                    context=context_section,
                    target_property=", ".join((goal.target or {}).keys()) or "unspecified",
                    material_system=(goal.constraints or {}).get("material_system", "unspecified"),
                    simulation_method=(goal.constraints or {}).get(
                        "simulation_method", "any computational method"
                    ),
                )
            else:
                prompt = (
                    "You are a scientific research assistant.\n\n"
                    f"Generate a structured research proposal for the following goal:\n"
                    f"Goal: {goal.goal}\n"
                    f"Domain: {goal.domain or 'general'}\n"
                    f"Constraints: {goal.constraints}\n\n"
                )
                if self.context.memory.get("goal_mode") == "artifact_generation":
                    prompt += (
                        "This is an artifact/code-generation request, not a "
                        "parameter-optimization study. Frame the proposal around "
                        "faithfully producing the requested computational artifact "
                        "from the user's source material. Do not invent numeric "
                        "target output keys from instruction words such as "
                        "'workflow', 'package', 'skill', or 'chain'. Preserve "
                        "requested implementation constraints such as solver stack, "
                        "paper section/figure locator, mesh handling, and plotting.\n\n"
                    )
                if context_section:
                    prompt += (
                        "Available context — use this to avoid duplicating existing work "
                        "and to build on prior findings:\n\n"
                        + context_section
                        + "\n\n"
                    )
                prompt += (
                    "The proposal should be specific, testable, and suitable "
                    "for a computational simulation."
                )

            candidates = await self._generate_candidates(provider, prompt, goal)
            return self._select_and_record(candidates, goal)

        # Provider-less stub mode: build the deterministic candidate pool and
        # return the genuinely top-*ranked* candidate — the same selection the
        # provider path makes. (Earlier this pinned the goal-faithful baseline,
        # but that returned exactly the generic phrasing the scorer penalises
        # and partially defeated item 2; the ranking is deterministic, so stub
        # mode is still fully reproducible.)
        return self._select_and_record(stub_candidates(goal), goal)

    async def _generate_candidates(
        self, provider, prompt: str, goal: ResearchGoal,
    ) -> list["ResearchProposal"]:
        """Return several candidate proposals from the provider.

        Prefers a single multi-candidate structured call (one round-trip,
        deterministic in tests). Falls back to one ``ResearchProposal`` when
        the wrapper schema isn't supported, so every provider keeps working.
        The returned list always has at least one element when the provider
        does.
        """
        n = self._num_candidates()
        if n > 1:
            multi_prompt = (
                prompt
                + f"\n\nPropose {n} DISTINCT candidate hypotheses that each frame "
                "the goal through a different lens (e.g. a parameter sweep, an "
                "optimisation, a comparative study). Return them as a list of "
                "structured proposals so the strongest can be selected."
            )
            try:
                from arc.packages.arc_sim2l_agents.hypothesis import HypothesisCandidates
                pool = await provider.complete_structured(multi_prompt, HypothesisCandidates)
                candidates = [c for c in (pool.candidates or []) if isinstance(c, ResearchProposal)]
                if candidates:
                    return candidates
            except Exception as exc:  # noqa: BLE001 — fall back to single proposal
                import logging as _logging
                _logging.getLogger(__name__).debug(
                    "Multi-candidate generation failed (%s); using single proposal.", exc,
                )
        single = await provider.complete_structured(prompt, ResearchProposal)
        return [single]

    def _num_candidates(self) -> int:
        """How many candidate hypotheses to generate (config-tunable)."""
        try:
            config = getattr(self.context, "config", None) or {}
            n = int(config.get("ARC_IDEATOR_CANDIDATES", 3))
        except (TypeError, ValueError, AttributeError):
            n = 3
        return max(1, min(n, 6))

    def _select_and_record(
        self, candidates: list["ResearchProposal"], goal: ResearchGoal,
    ) -> "ResearchProposal":
        """Rank candidates, record the pool + rationale, return the primary.

        Always returns one valid ``ResearchProposal`` — the highest-scoring
        non-rejected candidate (same selection in provider and stub mode). The
        full ranking (per-axis scores + rejection reasons), the selected
        rationale, and any diverse alternate are stashed on ``context.memory``
        so the chat/UI can show *which* hypothesis won and why, and so a later
        iteration can avoid repeating rejected ideas. Rejected/alternate
        hypotheses are also indexed into durable memory via the loop's memory
        hooks when those extensions are enabled (item 1).
        """
        from arc.packages.arc_sim2l_agents.hypothesis import select as _select

        result = _select(candidates, goal)
        ranked = list(result["ranked"])
        alternate = result["alternate"]

        primary = result["primary"] or (candidates[0] if candidates else None)
        if primary is None:
            # Degenerate safety net — should not happen, candidates is never
            # empty on either the provider or stub path.
            primary = stub_candidates(goal)[0]

        ranked_summary = [
            {
                "hypothesis": e["proposal"].hypothesis,
                "scores": e["scores"],
                "rejected": e["rejected"],
                "reason": e["reason"],
            }
            for e in ranked
        ]
        self.context.memory["ideator_candidates"] = ranked_summary
        self.context.memory["ideator_selection_rationale"] = result["rationale"]
        if alternate is not None:
            self.context.memory["ideator_alternate"] = alternate.model_dump()

        # Best-effort: record rejected/alternate hypotheses in durable memory
        # so future iterations can avoid repeating weak ideas (item 2 + item 1).
        self._index_rejected(ranked_summary, result.get("alternate"))

        return primary

    def _index_rejected(self, ranked_summary: list[dict], alternate) -> None:
        hooks = self.context.memory.get("memory_hooks")
        if hooks is None or not getattr(hooks, "enabled", False):
            return
        try:
            session = getattr(hooks, "session_id", "session")
            for i, entry in enumerate(ranked_summary):
                if not entry["rejected"]:
                    continue
                hooks._index(  # noqa: SLF001 — intentional internal reuse
                    f"rejected-hypothesis:{session}:{i}",
                    entry["hypothesis"],
                    {"kind": "rejected_hypothesis", "reason": entry["reason"],
                     "session_id": session},
                )
        except Exception:  # noqa: BLE001 — never let recording break ideation
            pass
