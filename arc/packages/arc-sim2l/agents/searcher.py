"""Catalog + results searcher (the role used by ``IdeatorAgent``).

Splitting search out of the ideator means we can swap in different
ranking strategies (keyword, embedding, hybrid) without changing the
ideator's prompt or downstream consumers. The ideator still owns the
hypothesis-generation prompt; the searcher only decides which catalog
entries to put in front of the prompt.

The contract is::

    class Searcher(AgentContract):
        async def search(goal: ResearchGoal) -> SearchResult

We use a ``search`` method rather than the inherited ``run`` so the
ideator can call the searcher mid-pipeline without colliding with the
agent-runtime convention that "running" an agent means producing the
agent's terminal artifact.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ResearchGoal, SearchResult

logger = logging.getLogger(__name__)


# ── Stopword + tokenisation helpers ─────────────────────────────────────


_STOPWORDS = {
    "a", "an", "the", "of", "to", "for", "and", "or", "in", "at", "by",
    "via", "with", "using", "that", "this", "is", "are", "be", "i", "want",
    "would", "like", "could", "should", "as", "on", "if", "it", "we", "you",
    "do", "does", "such", "from", "into", "over", "under", "while", "than",
}


def goal_keywords(goal_text: str) -> list[str]:
    """Strip punctuation, lowercase, drop stopwords, return tokens > 2 chars."""
    words = re.sub(r"[^a-z0-9 ]", " ", goal_text.lower()).split()
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


# ── HTTP helpers ────────────────────────────────────────────────────────


def fetch_catalog(catalog_url: str, query: str, limit: int = 5) -> list[dict]:
    """GET /simulations/search with the given query string. Empty on failure."""
    try:
        import requests
        resp = requests.get(
            f"{catalog_url.rstrip('/')}/simulations/search",
            params={"query": query, "limit": limit, "status": "active"},
            timeout=3,
        )
        if resp.status_code == 200:
            return resp.json() or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalog search failed: %s", exc)
    return []


def fetch_prior_results(results_url: str, sim_name: str, limit: int = 3) -> list[dict]:
    """POST /search with simulation_name. Empty on failure."""
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
    except Exception as exc:  # noqa: BLE001
        logger.debug("results search failed: %s", exc)
    return []


# ── Default keyword searcher ────────────────────────────────────────────


class _BaseSearcher(AgentContract):
    """Minimal ``run`` implementation so ``AgentContract`` is satisfied.

    Subclasses override :meth:`search` — that's where the actual ranking
    work lives. ``run`` is kept as a thin alias so callers can invoke a
    searcher via either method name without a surprise.
    """

    async def run(self, input_data) -> SearchResult:  # type: ignore[override]
        goal = (
            input_data
            if isinstance(input_data, ResearchGoal)
            else ResearchGoal(**input_data)
        )
        return await self.search(goal)

    async def search(self, goal: ResearchGoal) -> SearchResult:
        raise NotImplementedError


class KeywordSearcherAgent(_BaseSearcher):
    """The behaviour ``IdeatorAgent`` had before the Searcher role existed.

    Strips stopwords from the goal text, joins the surviving keywords
    with spaces, and asks the catalog service to LIKE-match them. Top
    catalog hit's prior runs are fetched from the results service.
    """

    name = "searcher"
    description = (
        "Keyword search against the sim2l catalog + recent results for the "
        "top hit. Cheap, deterministic, and the historical default."
    )

    async def search(self, goal: ResearchGoal) -> SearchResult:
        catalog_url = os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")
        results_url = os.environ.get("SIM2L_RESULTS_URL", "http://localhost:8003")

        keywords = goal_keywords(goal.goal)
        # The catalog now tokenizes + OR-matches across name/description/tags
        # and ranks by keyword-match count, so sending more keywords improves
        # recall (each extra term can only add a hit, never narrow the set).
        query = " ".join(keywords[:8])
        catalog_hits = fetch_catalog(catalog_url, query)

        prior_results: list[dict] = []
        if catalog_hits:
            top = catalog_hits[0].get("name", "")
            if top:
                prior_results = fetch_prior_results(results_url, top, limit=3)

        return SearchResult(catalog_hits=catalog_hits, prior_results=prior_results)


# ── Composite searcher ─────────────────────────────────────────────────


class CompositeSearcherAgent(_BaseSearcher):
    """Run several searcher strategies and merge their results.

    ``arc.core.strategies.resolve_role("searcher")`` creates a lightweight
    subclass with ``strategy_names`` set when the active selector contains
    ``+`` (for example ``default+embeddings+materials_project``). Searchers
    run in that order; earlier hits keep their rank, later strategies append
    new evidence.
    """

    name = "searcher_composite"
    description = "Runs multiple searcher strategies and merges their hits."
    strategy_names: tuple[str, ...] = ()

    async def search(self, goal: ResearchGoal) -> SearchResult:
        from arc.core.strategies import resolve_role

        hits: list[dict[str, Any]] = []
        prior_results: list[dict[str, Any]] = []
        seen_hits: set[str] = set()
        seen_results: set[str] = set()
        registry = self.context.memory.get("component_registry")
        loaded_packages = set(registry.list_packages()) if registry is not None else None

        for strategy_name in self.strategy_names:
            try:
                SearcherCls = resolve_role(
                    "searcher",
                    overrides={"searcher": strategy_name},
                    config={},
                    loaded_packages=loaded_packages,
                )
                result = await SearcherCls(context=self.context).search(goal)
            except Exception as exc:  # noqa: BLE001 — one source must not kill the stack
                logger.debug(
                    "composite searcher component %s failed: %s",
                    strategy_name,
                    exc,
                )
                continue

            for hit in result.catalog_hits:
                key = _dedupe_key(hit, ("source", "id", "name", "repo_path"))
                if key in seen_hits:
                    continue
                seen_hits.add(key)
                merged = dict(hit)
                merged.setdefault("metadata", {})
                if isinstance(merged["metadata"], dict):
                    merged["metadata"] = {
                        **merged["metadata"],
                        "search_strategy": strategy_name,
                    }
                hits.append(merged)

            for item in result.prior_results:
                key = _dedupe_key(item, ("run_id", "execution_id", "id", "simulation_name"))
                if key in seen_results:
                    continue
                seen_results.add(key)
                prior_results.append(item)

        return SearchResult(catalog_hits=hits, prior_results=prior_results)


def _dedupe_key(record: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            return f"{field}:{value}"
    return repr(sorted(record.items()))


# ── Embedding-based searcher ────────────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    return goal_keywords(text or "")


def _tf(tokens: list[str]) -> dict[str, float]:
    if not tokens:
        return {}
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    n = len(tokens)
    return {t: c / n for t, c in counts.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[k] * b[k] for k in common)
    da = sum(v * v for v in a.values()) ** 0.5
    db = sum(v * v for v in b.values()) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


class EmbeddingSearcherAgent(_BaseSearcher):
    """Semantic-ish searcher that re-ranks the keyword candidate set.

    Strategy:

      1. Pull a broader candidate set from the catalog (limit=20) with the
         existing keyword query — this is the *recall* step. We still need
         the catalog endpoint to do candidate generation because we don't
         maintain a local index of every simulation in this build.
      2. Build a TF vector for the goal text and for each candidate's
         ``description + input_schema_keys + output_schema_keys``.
      3. Score by cosine similarity and return the top-5 ranked by score
         instead of the catalog's default LIKE-rank.

    If the configured provider exposes an ``embed(text) → list[float]``
    coroutine we use that instead of TF vectors — the rest of the pipeline
    is identical.
    """

    name = "searcher_embeddings"
    description = (
        "Re-ranks keyword search results by cosine similarity of the goal "
        "text against each candidate's description + schema. Uses the "
        "provider's embed() when available, otherwise TF vectors."
    )

    async def search(self, goal: ResearchGoal) -> SearchResult:
        catalog_url = os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")
        results_url = os.environ.get("SIM2L_RESULTS_URL", "http://localhost:8003")

        # 1. recall — wider candidate set, then re-rank.
        keywords = goal_keywords(goal.goal)
        query = " ".join(keywords[:8])
        candidates = fetch_catalog(catalog_url, query, limit=20)
        if not candidates:
            return SearchResult(catalog_hits=[], prior_results=[])

        # 2. score — embeddings or TF cosine.
        provider = self.context.memory.get("provider")
        embed = getattr(provider, "embed", None) if provider else None
        scored: list[tuple[float, dict]] = []

        if embed is not None and callable(embed):
            try:
                goal_vec = await _safe_embed(embed, goal.goal)
                for cand in candidates:
                    doc = _candidate_doc(cand)
                    cand_vec = await _safe_embed(embed, doc)
                    scored.append((_dot_normalised(goal_vec, cand_vec), cand))
            except Exception as exc:  # noqa: BLE001 — fall through to TF
                logger.debug("embedding scoring failed (%s); falling back to TF", exc)
                scored = []

        if not scored:
            goal_tf = _tf(_tokenize(goal.goal))
            for cand in candidates:
                cand_tf = _tf(_tokenize(_candidate_doc(cand)))
                scored.append((_cosine(goal_tf, cand_tf), cand))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        ranked = [c for _, c in scored[:5]]

        prior_results: list[dict] = []
        if ranked:
            top = ranked[0].get("name", "")
            if top:
                prior_results = fetch_prior_results(results_url, top, limit=3)

        return SearchResult(catalog_hits=ranked, prior_results=prior_results)


def _candidate_doc(record: dict) -> str:
    """Flatten a catalog record into a single string for scoring."""
    parts: list[str] = []
    parts.append(record.get("description") or "")
    parts.append(record.get("name") or "")
    for k in (record.get("input_schema") or {}).keys():
        parts.append(k)
    for k in (record.get("output_schema") or {}).keys():
        parts.append(k)
    for tag in record.get("tags") or []:
        parts.append(str(tag))
    return " ".join(parts)


async def _safe_embed(embed_fn, text: str) -> list[float]:
    """Embed `text`; tolerate both sync and async embed callables.

    Returns ``[]`` (not a crash) when the embedder is unavailable or
    returns something that isn't a numeric vector — callers fall back to
    TF vectors, so a bad embedder degrades ranking rather than breaking
    the search.
    """
    import asyncio
    try:
        result = embed_fn(text)
        if asyncio.iscoroutine(result):
            result = await result
        if result is None:
            return []
        if hasattr(result, "tolist"):  # numpy / torch tensor
            result = result.tolist()
        return [float(x) for x in result]
    except Exception:  # noqa: BLE001 — embedding is best-effort
        logger.debug("embedding failed; falling back", exc_info=True)
        return []


def _dot_normalised(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    da = sum(v * v for v in a) ** 0.5
    db = sum(v * v for v in b) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n)) / (da * db)


# ── GitHub searcher (read side of the GitHub backend) ───────────────────


class GitHubSearcherAgent(_BaseSearcher):
    """Searches artifacts published to a GitHub repo by ``GitHubBackend``.

    The read-side counterpart of ``arc.runtime.backend.GitHubBackend``:
    where that backend *commits* artifacts under ``<prefix>/<name>/<version>/``,
    this searcher *lists* them via the GitHub Contents API and returns the
    ones whose name/description match the goal keywords as ``catalog_hits``
    in the standard shape (``name``, ``description``, ``input_schema``,
    ``output_schema``). Inactive (returns empty) when no GitHub config is
    present, so it degrades to "no prior artifacts" rather than failing.
    """

    name = "searcher_github"
    description = (
        "Lists artifacts published to the configured GitHub repo (the read "
        "side of the GitHub backend) and ranks them by keyword overlap with "
        "the goal. Requires GITHUB_TOKEN + ARC_GITHUB_REPO."
    )

    async def search(self, goal: ResearchGoal) -> SearchResult:
        from arc.runtime.backend import github_config
        config = github_config()
        if config is None:
            return SearchResult(catalog_hits=[], prior_results=[])

        keywords = set(goal_keywords(goal.goal))
        hits = _github_list_artifacts(config, keywords)
        return SearchResult(catalog_hits=hits, prior_results=[])


def _github_list_artifacts(config: dict, keywords: set[str], limit: int = 5) -> list[dict]:
    """List + rank artifacts in the repo's ``<prefix>/`` tree.

    Reads each ``<prefix>/<name>/<version>/arc_record.json`` and keeps the
    records whose name/description share at least one goal keyword (or all,
    when the goal has no usable keywords). Best-effort: any API failure
    yields an empty list. Uses the git trees API (one recursive call)
    rather than walking ``contents/`` per directory.
    """
    import requests

    repo = config["repo"]
    prefix = config["prefix"]
    branch = config.get("branch") or "HEAD"
    headers = {
        "Authorization": f"Bearer {config['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        tree_url = f"https://api.github.com/repos/{repo}/git/trees/{branch}"
        resp = requests.get(tree_url, headers=headers, params={"recursive": "1"}, timeout=10)
        if resp.status_code != 200:
            return []
        tree = resp.json().get("tree", [])
        record_paths = [
            node["path"] for node in tree
            if node.get("type") == "blob"
            and node["path"].startswith(f"{prefix}/")
            and node["path"].endswith("/arc_record.json")
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("github tree listing failed: %s", exc)
        return []

    hits: list[dict] = []
    for path in record_paths:
        try:
            raw_url = f"https://api.github.com/repos/{repo}/contents/{path}"
            params = {"ref": config["branch"]} if config.get("branch") else {}
            r = requests.get(raw_url, headers={**headers, "Accept": "application/vnd.github.raw"},
                             params=params, timeout=10)
            if r.status_code != 200:
                continue
            import json
            record = json.loads(r.text)
        except Exception:  # noqa: BLE001
            continue
        name = record.get("name", "")
        description = record.get("description", "")
        metadata = record.get("metadata", {}) or {}
        haystack = f"{name} {description}".lower()
        if not keywords or any(k in haystack for k in keywords):
            hits.append({
                "name": name,
                "description": description,
                "input_schema": metadata.get("sim2l_inputs", {}),
                "output_schema": metadata.get("sim2l_outputs", {}),
                "source": "github",
                "repo_path": path.rsplit("/", 1)[0],
            })
        if len(hits) >= limit:
            break
    return hits
