"""Genetic algorithm optimizer agent.

Runs a population-based evolutionary search over the artifact's input schema,
calling simulate() for each individual, selecting survivors by fitness
(distance to target or raw output magnitude), and producing the next generation
via crossover + mutation.

Usage (via chat.py /optimize command):
    await GeneticOptimizerAgent(context=ctx).run(artifact, target, max_generations)
"""

import math
import random
from typing import Any

from arc.contracts.agent import AgentContract
from arc.runtime.key_matching import keys_match as _keys_match
from arc.schemas.artifact import ArtifactRecord


# ── Fitness ───────────────────────────────────────────────────────────────────

def _fitness(outputs: dict[str, Any], target: dict[str, Any], registry: dict | None = None) -> float:
    """Lower is better. Returns sum of squared % error across matched target keys.

    Returns inf when no target keys match any output (not 0 — that would look
    like perfect convergence). If no target, maximises total output magnitude.
    """
    if not outputs:
        return float("inf")

    if target:
        total = 0.0
        matched = 0
        for tk, tv in target.items():
            for ok, ov in outputs.items():
                if _keys_match(tk, ok, registry):
                    if isinstance(ov, (int, float)) and ov is not None:
                        err = (ov - tv) / max(abs(tv), 1e-12)
                        total += err * err
                        matched += 1
                    break  # one output per target key
        if matched == 0:
            return float("inf")  # no match → worst possible fitness, not 0
        return total / matched

    # No target: maximise sum of numeric outputs (return negative so lower=better)
    total = sum(v for v in outputs.values() if isinstance(v, (int, float)) and v is not None)
    return -total if total else float("inf")


def _pct_off(outputs: dict, target: dict, registry: dict | None = None) -> str:
    parts = []
    for tk, tv in target.items():
        for ok, ov in outputs.items():
            if _keys_match(tk, ok, registry):
                if isinstance(ov, (int, float)):
                    pct = abs(ov - tv) / max(abs(tv), 1e-12) * 100
                    parts.append(f"{ok}={ov:.4g} ({pct:.1f}% off)")
                break
    return "  ".join(parts) if parts else "(no target key match)" if target else ""


# ── GA operators ──────────────────────────────────────────────────────────────

def _initial_population(schema: dict[str, Any], pop_size: int) -> list[dict]:
    """Sample pop_size individuals from a range around each parameter's default."""
    population = []
    for _ in range(pop_size):
        ind = {}
        for name, field in schema.items():
            default = field.get("default", 1.0)
            if default == 0:
                lo, hi = -1.0, 1.0
            else:
                lo = default * 0.1
                hi = default * 3.0
                if lo > hi:
                    lo, hi = hi, lo
            ind[name] = random.uniform(lo, hi)
        population.append(ind)
    return population


def _crossover(p1: dict, p2: dict) -> dict:
    """Uniform crossover: each gene drawn from either parent with equal prob."""
    return {k: (p1[k] if random.random() < 0.5 else p2[k]) for k in p1}


def _mutate(ind: dict, schema: dict, mutation_rate: float = 0.3, sigma_frac: float = 0.2) -> dict:
    """Gaussian perturbation on each gene with probability mutation_rate."""
    result = {}
    for name, val in ind.items():
        if random.random() < mutation_rate:
            default = schema[name].get("default", val) or val or 1.0
            sigma = abs(default) * sigma_frac or 0.1
            result[name] = val + random.gauss(0, sigma)
        else:
            result[name] = val
    return result


def _select(population: list[dict], fitnesses: list[float], n: int) -> list[dict]:
    """Tournament selection: pick n survivors via binary tournaments."""
    survivors = []
    indices = list(range(len(population)))
    for _ in range(n):
        a, b = random.sample(indices, 2)
        survivors.append(population[a] if fitnesses[a] <= fitnesses[b] else population[b])
    return survivors


# ── Agent ─────────────────────────────────────────────────────────────────────

class GeneticOptimizerAgent(AgentContract):
    name = "optimizer"
    description = "Runs a genetic algorithm over an artifact's input space to reach a target."

    async def run(
        self,
        artifact: ArtifactRecord,
        target: dict[str, Any],
        max_generations: int = 10,
        pop_size: int = 8,
        elite_frac: float = 0.25,
        convergence_threshold: float = 0.0025,  # 0.25% error → stop early
        on_generation=None,  # optional async callback(gen, best_ind, best_out, fitness)
    ) -> dict[str, Any]:
        """Run GA over artifact's input schema.

        Args:
            artifact: registered ArtifactRecord to evaluate
            target: dict of {output_key: target_value}
            max_generations: hard cap on generations
            pop_size: individuals per generation
            elite_frac: fraction kept as elites each generation
            convergence_threshold: fitness threshold for early stopping
            on_generation: async callable for progress reporting

        Returns:
            {"best_inputs": ..., "best_outputs": ..., "best_fitness": ...,
             "generations_run": ..., "history": [...per-gen best...]}
        """
        adapter = self.context.memory.get("adapter")
        if adapter is None:
            raise RuntimeError("GeneticOptimizerAgent requires 'adapter' in context.memory")

        schema: dict = artifact.metadata.get("sim2l_inputs", {})
        if not schema:
            try:
                from arc.sim2l_schema import load_sim2l_schema
                schema, _ = load_sim2l_schema(artifact.path)
                if schema:
                    artifact.metadata["sim2l_inputs"] = schema
            except Exception:
                schema = {}
        if not schema:
            raise ValueError("Artifact has no sim2l_inputs schema — cannot initialise population")

        registry: dict = self.context.memory.get("schema_registry", {})

        # Resolve the simulate() callable once per optimization run so each
        # GA evaluation reuses the same in-memory function instead of
        # paying the load + safety-check cost per call. Review item #T18:
        # `_import_workflow_func` no longer mutates sys.path/sys.modules
        # (#A1) — it execs into a throwaway namespace and returns the
        # callable directly. If the import fails for any reason (the
        # artifact doesn't ship workflow.py, the source fails the safety
        # check, etc.), we fall back to `adapter.run()` per evaluation.
        _simulate_func = None
        try:
            from arc.runtime.sim2l_adapter import _import_workflow_func
            from pathlib import Path
            workflow_path = Path(artifact.path) / "workflow.py"
            if workflow_path.exists():
                _simulate_func = _import_workflow_func(artifact.path, artifact.artifact_id)
        except Exception:
            pass  # fall back to adapter.run() if direct load fails

        # Build schema defaults for input reconciliation.
        _schema_defaults = {k: v.get("default", 1.0) for k, v in schema.items()}

        async def _evaluate(ind: dict) -> dict:
            """Call simulate() directly if available, else via adapter."""
            inputs = {**_schema_defaults, **{k: v for k, v in ind.items() if k in schema}}
            if _simulate_func is not None:
                try:
                    return _simulate_func(**inputs) or {}
                except Exception:
                    return {}
            try:
                result = await adapter.run(artifact, inputs)
                return result.outputs or {}
            except Exception:
                return {}

        n_elite = max(1, int(pop_size * elite_frac))
        population = _initial_population(schema, pop_size)

        best_ind: dict = {}
        best_out: dict = {}
        best_fitness = float("inf")
        history = []

        for gen in range(max_generations):
            # Evaluate all individuals
            fitnesses: list[float] = []
            outputs_list: list[dict] = []

            for ind in population:
                out = await _evaluate(ind)
                fit = _fitness(out, target, registry)
                fitnesses.append(fit)
                outputs_list.append(out)

            # Track generation best
            gen_best_idx = min(range(len(fitnesses)), key=lambda i: fitnesses[i])
            gen_best_fit = fitnesses[gen_best_idx]
            gen_best_ind = population[gen_best_idx]
            gen_best_out = outputs_list[gen_best_idx]

            if gen_best_fit < best_fitness:
                best_fitness = gen_best_fit
                best_ind = dict(gen_best_ind)
                best_out = dict(gen_best_out)

            history.append({
                "generation": gen,
                "best_fitness": gen_best_fit,
                "best_inputs": gen_best_ind,
                "best_outputs": gen_best_out,
            })

            if on_generation:
                await on_generation(gen, gen_best_ind, gen_best_out, gen_best_fit)

            # Early stopping
            if best_fitness <= convergence_threshold:
                break

            # Next generation: elites + crossover/mutation offspring
            sorted_pop = [population[i] for i in sorted(range(len(fitnesses)), key=lambda i: fitnesses[i])]
            elites = sorted_pop[:n_elite]
            parents = _select(population, fitnesses, pop_size - n_elite)
            offspring = []
            for i in range(0, len(parents) - 1, 2):
                child = _crossover(parents[i], parents[i + 1])
                offspring.append(_mutate(child, schema))
            # Fill remainder via mutation of random elites
            while len(elites) + len(offspring) < pop_size:
                base = random.choice(elites)
                offspring.append(_mutate(base, schema, mutation_rate=0.5))

            population = elites + offspring

        # Store best in context for next manual iterate
        if best_ind:
            self.context.memory["next_parameters"] = best_ind

        return {
            "best_inputs": best_ind,
            "best_outputs": best_out,
            "best_fitness": best_fitness,
            "generations_run": len(history),
            "converged": best_fitness <= convergence_threshold,
            "history": history,
        }
