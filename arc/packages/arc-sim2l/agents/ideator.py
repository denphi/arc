from arc.contracts.agent import AgentContract, AgentContext
from arc.schemas.research import ResearchGoal, ResearchProposal  # noqa: F401 — needed at module level


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
            json={"simulation_name": sim_name, "input_filters": {}, "output_filters": {}, "limit": limit},
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


class IdeatorAgent(AgentContract):
    name = "ideator"
    description = "Generates a structured research proposal from a user goal."

    async def run(self, input_data: ResearchGoal) -> ResearchProposal:
        goal = input_data if isinstance(input_data, ResearchGoal) else ResearchGoal(**input_data)

        import os
        catalog_url = os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")
        results_url = os.environ.get("SIM2L_RESULTS_URL", "http://localhost:8003")

        # ── Catalog search ────────────────────────────────────────────────
        keywords = _goal_keywords(goal.goal)
        catalog_hits = _search_catalog(catalog_url, keywords)

        # Fetch recent results for the top catalog hit (if any).
        prior_results: list[dict] = []
        if catalog_hits:
            prior_results = _search_results(results_url, catalog_hits[0].get("name", ""), limit=3)

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
                        f"  inputs={r.get('input_params', {})}  outputs={r.get('output_params', {})}"
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

            context_section = "\n\n".join(filter(None, [catalog_block, results_block, history_block, registry_block]))

            prompt = (
                "You are a scientific research assistant.\n\n"
                f"Generate a structured research proposal for the following goal:\n"
                f"Goal: {goal.goal}\n"
                f"Domain: {goal.domain or 'general'}\n"
                f"Constraints: {goal.constraints}\n\n"
            )
            if context_section:
                prompt += (
                    "Available context — use this to avoid duplicating existing work "
                    "and to build on prior findings:\n\n"
                    + context_section
                    + "\n\n"
                )
            prompt += "The proposal should be specific, testable, and suitable for a computational simulation."
            return await provider.complete_structured(prompt, ResearchProposal)

        return ResearchProposal(
            hypothesis=f"A simulation workflow can test: {goal.goal}",
            objective=goal.goal,
            variables=["input_parameter", "output_metric"],
            methodology=(
                "Create a Sim2L artifact and evaluate outputs across selected parameters."
            ),
            expected_outcomes=(
                "The outputs reveal whether the hypothesis is supported."
            ),
            evaluation_metrics=["execution_success", "output_quality", "metric_improvement"],
            risk_level="medium",
        )
