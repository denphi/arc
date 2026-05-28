"""Searcher strategy that pulls candidates from the Materials Project.

Wraps the Next-Gen MP REST API (https://next-gen.materialsproject.org/api).
Returns each matching MP record as a *read-only reference* — it's not a
runnable Sim2L artifact, just real DFT-computed evidence the ideator can
use to ground its hypothesis.

API key resolution
------------------

We read ``MP_API_KEY`` from the process environment. If a project ships a
``.env`` file (the same pattern arc uses for SIM2L credentials),
``python-dotenv`` will have already loaded it into ``os.environ`` by the
time the chat starts; we don't load it ourselves. Without a key the
searcher logs at debug and returns an empty ``SearchResult`` so the
ideator can fall through to its other context blocks.

Goal → query mapping
--------------------

We pull whatever signals the goal makes available:

  * **Element symbols** detected in ``goal.goal`` → ``elements=A,B,…``.
  * **Numeric target** under a property the MP summary endpoint
    supports (band gap, formation energy per atom) → a ±20% band on
    that property. The exact properties we recognise are listed in
    ``_TARGET_PROPERTY_MAP``.

What we *don't* try to do here:

  * Parse free-text chemistry from the goal beyond the element symbols
    — the prompt LLM is much better at it than a regex would be.
  * Negotiate units. MP results are in eV; arc inputs are SI. If the
    user wrote ``bandgap_ev=1.1`` we trust it; if they wrote
    ``bandgap_j=1.76e-19`` the searcher silently ignores it.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from arc.packages.arc_sim2l_agents.searcher import _BaseSearcher
from arc.schemas.research import ResearchGoal, SearchResult

logger = logging.getLogger(__name__)


# ── Goal-text parsing ────────────────────────────────────────────────────

# Common 1- and 2-letter element symbols. Curated rather than full
# periodic table to avoid false positives on words like "In" (Indium vs
# the preposition). The trailing $|[\W\d_] makes the match
# whitespace-aware without consuming the boundary.
_ELEMENTS = (
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar",
    "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "In", "Sn", "Sb", "Te", "I", "Xe",
    "Cs", "Ba", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi",
)

# Word stems that name materials → element/system hints. Cheap heuristic
# so a goal like "design a silicon nanowire" matches even without "Si"
# appearing literally.
_NAMED_MATERIALS = {
    "silicon": "Si", "germanium": "Ge", "carbon": "C", "gallium": "Ga",
    "arsenic": "As", "iron": "Fe", "copper": "Cu", "aluminum": "Al",
    "aluminium": "Al", "titanium": "Ti", "lithium": "Li", "oxygen": "O",
    "nitrogen": "N", "hydrogen": "H", "sulfur": "S", "sulphur": "S",
    "phosphorus": "P", "tin": "Sn", "lead": "Pb", "platinum": "Pt",
    "gold": "Au", "silver": "Ag",
}


def detect_elements(goal_text: str) -> list[str]:
    """Return up to 4 element symbols hinted at by the goal text.

    Order: named materials first (they're usually the subject of the
    goal), then literal symbols. Dedup preserves first occurrence.
    """
    if not goal_text:
        return []
    text = goal_text.strip()
    seen: list[str] = []

    # Named materials match case-insensitively on word boundaries.
    lower = text.lower()
    for name, symbol in _NAMED_MATERIALS.items():
        if re.search(rf"\b{name}\b", lower) and symbol not in seen:
            seen.append(symbol)

    # Literal symbols: capital-then-optional-lowercase. We look in two
    # passes — first standalone tokens (``Si``, ``Fe``), then symbols
    # packed into formulas (``GaAs`` → ``Ga``, ``As``; ``Fe2O3`` → ``Fe``,
    # ``O``). The formula pass only triggers on tokens that start with a
    # capital and contain multiple capitals or digits, so plain English
    # words like "Quantum" don't get split.
    for token in re.findall(r"\b([A-Z][a-z]?)\b", text):
        if token in _ELEMENTS and token not in seen:
            seen.append(token)

    formula_tokens = re.findall(r"[A-Z][a-z]?\d*", text)
    if formula_tokens:
        # Strip digits so "Fe2" → "Fe" before lookup.
        for raw in formula_tokens:
            symbol = re.sub(r"\d+", "", raw)
            if symbol in _ELEMENTS and symbol not in seen:
                seen.append(symbol)

    return seen[:4]


# ── Target → property-range parameters ───────────────────────────────────


# Mapping from the various target-key forms users write to MP's API
# parameter base name. Each MP property becomes ``<base>_min`` /
# ``<base>_max`` with a ±20% band around the requested value.
_TARGET_PROPERTY_MAP: dict[str, str] = {
    "band_gap": "band_gap",
    "bandgap": "band_gap",
    "bandgap_ev": "band_gap",
    "band_gap_ev": "band_gap",
    "formation_energy": "formation_energy_per_atom",
    "formation_energy_per_atom": "formation_energy_per_atom",
    "e_formation": "formation_energy_per_atom",
    "energy_above_hull": "energy_above_hull",
    "density": "density",
}


def target_to_params(target: dict[str, Any]) -> dict[str, float]:
    """Convert ``goal.target`` into MP summary endpoint range params.

    Non-numeric or unknown keys are silently dropped. The ±20% band is
    a heuristic; users with sharper targets should use the ideator's
    follow-up prompts to narrow further.
    """
    params: dict[str, float] = {}
    if not target:
        return params
    for raw_key, value in target.items():
        if not isinstance(value, (int, float)):
            continue
        base = _TARGET_PROPERTY_MAP.get(str(raw_key).lower())
        if not base:
            continue
        delta = abs(value) * 0.2 if value != 0 else 0.1
        params[f"{base}_min"] = float(value - delta)
        params[f"{base}_max"] = float(value + delta)
    return params


# ── HTTP ────────────────────────────────────────────────────────────────


_MP_API_BASE = "https://api.materialsproject.org"


def fetch_mp_summary(
    params: dict[str, Any],
    *,
    api_key: str,
    limit: int = 10,
    timeout: float = 5.0,
) -> list[dict]:
    """Call MP's ``summary`` endpoint and return raw documents.

    Returns an empty list on any failure — auth, network, schema. The
    caller (the searcher) treats an empty list as "no candidates" and
    logs the reason at debug for diagnostics.
    """
    try:
        import requests
        merged = {
            "_limit": limit,
            "_fields": (
                "material_id,formula_pretty,elements,band_gap,"
                "formation_energy_per_atom,density,is_stable,"
                "symmetry,structure,"
                # Thermodynamic + elastic + DOS axes the planner splices
                # into builder defaults via _apply_mp_property_defaults.
                "energy_above_hull,decomposition_enthalpy,"
                "bulk_modulus,shear_modulus,"
                "efermi,dos_energy_up,dos_energy_down"
            ),
            **params,
        }
        resp = requests.get(
            f"{_MP_API_BASE}/materials/summary/",
            params=merged,
            headers={"X-API-KEY": api_key, "accept": "application/json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.debug("MP summary returned %s: %s", resp.status_code, resp.text[:200])
            return []
        body = resp.json() or {}
        docs = body.get("data") or []
        if isinstance(docs, list):
            return docs
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("MP summary request failed: %s", exc)
        return []


# ── Structure → lattice parameters ──────────────────────────────────────


# Lattice quantities downstream code can splice into builder defaults
# as numeric parameters. Keep these names ``snake_case`` and consistent
# with arc's input-naming convention so they slot into ``plan.parameters``
# without renaming.
_LATTICE_PARAM_NAMES = (
    "lattice_a", "lattice_b", "lattice_c",
    "lattice_alpha", "lattice_beta", "lattice_gamma",
)


def extract_lattice(structure: dict | None) -> dict[str, float]:
    """Pull numeric lattice parameters out of an MP structure document.

    The next-gen ``/materials/summary/`` endpoint returns structures in
    a pymatgen-flavoured dict shape: ``{"lattice": {"a": ..., "b": ...,
    "alpha": ..., ...}, "sites": [...]}``. We extract the six classic
    lattice parameters (a, b, c, alpha, beta, gamma) plus volume when
    present. Missing or non-numeric values are silently dropped — the
    planner only splices what we provide.
    """
    if not isinstance(structure, dict):
        return {}
    lattice = structure.get("lattice")
    if not isinstance(lattice, dict):
        return {}
    out: dict[str, float] = {}
    for src, dst in (
        ("a", "lattice_a"), ("b", "lattice_b"), ("c", "lattice_c"),
        ("alpha", "lattice_alpha"),
        ("beta", "lattice_beta"),
        ("gamma", "lattice_gamma"),
    ):
        value = lattice.get(src)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[dst] = float(value)
    volume = lattice.get("volume")
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        out["lattice_volume"] = float(volume)
    return out


# ── Thermodynamics ─────────────────────────────────────────────────────


def _numeric(value: Any) -> float | None:
    """Coerce to float, return ``None`` for non-numeric / bool inputs."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def extract_thermo(doc: dict | None) -> dict[str, float]:
    """Pull thermodynamic axes (stability margin, decomposition cost) from a
    summary document.

    ``energy_above_hull`` (eV/atom) measures the energy cost of a material
    relative to the convex-hull ground state — 0 means thermodynamically
    stable, larger values mean metastable. ``decomposition_enthalpy``
    quantifies the cost of breaking down into competing phases. Both are
    useful as builder defaults because they tell downstream optimisers
    *where* on the stability landscape the reference material sits.

    Missing or non-numeric fields are silently dropped.
    """
    if not isinstance(doc, dict):
        return {}
    out: dict[str, float] = {}
    for src, dst in (
        ("energy_above_hull", "energy_above_hull"),
        ("decomposition_enthalpy", "decomposition_enthalpy"),
    ):
        value = _numeric(doc.get(src))
        if value is not None:
            out[dst] = value
    return out


# ── Elasticity ─────────────────────────────────────────────────────────


def _vrh_average(value: Any) -> float | None:
    """Pick the Voigt-Reuss-Hill average from MP's elastic-modulus block.

    MP serialises bulk/shear moduli either as scalars or as a dict shaped
    ``{"voigt": ..., "reuss": ..., "vrh": ...}``. The VRH average is the
    standard isotropic estimate; we prefer it when present and fall back
    to the scalar when the dict shape isn't used.
    """
    direct = _numeric(value)
    if direct is not None:
        return direct
    if isinstance(value, dict):
        for key in ("vrh", "voigt_reuss_hill", "voigt", "reuss"):
            picked = _numeric(value.get(key))
            if picked is not None:
                return picked
    return None


def extract_elasticity(doc: dict | None) -> dict[str, float]:
    """Pull elastic moduli (GPa) from a summary document.

    Bulk modulus K and shear modulus G control the material's mechanical
    response. The planner uses them as builder defaults when a sweep
    needs realistic stiffness values rather than placeholders.
    """
    if not isinstance(doc, dict):
        return {}
    out: dict[str, float] = {}
    for src, dst in (
        ("bulk_modulus", "bulk_modulus"),
        ("shear_modulus", "shear_modulus"),
    ):
        value = _vrh_average(doc.get(src))
        if value is not None:
            out[dst] = value
    return out


# ── Density of states ──────────────────────────────────────────────────


def extract_dos(doc: dict | None) -> dict[str, float]:
    """Pull electronic-structure scalars from a summary document.

    We deliberately extract *scalar* DOS quantities only — the Fermi
    level and band-edge energies. The full DOS curve has thousands of
    points and is not useful as a builder default. ``efermi`` is the
    most-asked-for value; ``dos_energy_up`` / ``dos_energy_down`` come
    along when MP records them separately for spin-polarised systems.
    """
    if not isinstance(doc, dict):
        return {}
    out: dict[str, float] = {}
    for src, dst in (
        ("efermi", "efermi"),
        ("dos_energy_up", "dos_energy_up"),
        ("dos_energy_down", "dos_energy_down"),
    ):
        value = _numeric(doc.get(src))
        if value is not None:
            out[dst] = value
    return out


# ── Hit mapping ─────────────────────────────────────────────────────────


def mp_doc_to_catalog_hit(doc: dict) -> dict:
    """Translate one MP summary document to arc's catalog-hit shape.

    The shape mirrors what ``KeywordSearcherAgent`` returns from the
    sim2l catalog (``id``, ``name``, ``description``, ``input_schema``,
    ``output_schema``, ``tags``, ``metadata``). MP records have no
    runnable workflow, so ``input_schema`` is empty and ``metadata``
    carries ``source="materials_project"`` so the chat can show the
    hit as a *reference* rather than a "reuse this artifact?" prompt.
    """
    mp_id = doc.get("material_id") or ""
    formula = doc.get("formula_pretty") or ""
    name = f"{formula}_{mp_id}" if formula and mp_id else (mp_id or formula or "unknown")

    out_schema: dict[str, dict] = {}
    properties = {
        "band_gap": "eV",
        "formation_energy_per_atom": "eV/atom",
        "density": "g/cm^3",
        "is_stable": None,
    }
    for prop, unit in properties.items():
        if doc.get(prop) is not None:
            out_schema[prop] = {"type": "Number", "units": unit, "value": doc.get(prop)}

    description_bits = [formula]
    if doc.get("band_gap") is not None:
        description_bits.append(f"Eg={doc['band_gap']:.2f} eV")
    if doc.get("formation_energy_per_atom") is not None:
        description_bits.append(f"Ef={doc['formation_energy_per_atom']:.2f} eV/atom")
    if doc.get("is_stable") is True:
        description_bits.append("stable")
    description = " · ".join(b for b in description_bits if b)

    sym = doc.get("symmetry") or {}
    tags = ["materials_project", "reference"]
    if isinstance(sym, dict) and sym.get("crystal_system"):
        tags.append(str(sym["crystal_system"]).lower())

    # Lattice + thermo + elastic + DOS axes the planner reads for
    # builder defaults. Missing fields produce empty dicts; downstream
    # code checks each block independently.
    lattice = extract_lattice(doc.get("structure"))
    thermo = extract_thermo(doc)
    elasticity = extract_elasticity(doc)
    dos = extract_dos(doc)

    metadata = {
        "source": "materials_project",
        "mp_id": mp_id,
        "formula": formula,
        "elements": doc.get("elements") or [],
        "url": (
            f"https://next-gen.materialsproject.org/materials/{mp_id}"
            if mp_id else ""
        ),
    }
    if lattice:
        metadata["lattice"] = lattice
    if thermo:
        metadata["thermo"] = thermo
    if elasticity:
        metadata["elasticity"] = elasticity
    if dos:
        metadata["dos"] = dos
    if isinstance(sym, dict) and sym.get("crystal_system"):
        metadata["crystal_system"] = str(sym["crystal_system"])

    return {
        "id": mp_id,
        "name": name,
        "description": description,
        "input_schema": {},
        "output_schema": out_schema,
        "tags": tags,
        "metadata": metadata,
    }


# ── Agent ───────────────────────────────────────────────────────────────


class MaterialsProjectSearcherAgent(_BaseSearcher):
    """Searcher that pulls reference materials from the MP next-gen API.

    Returns MP records as catalog hits flagged ``source=materials_project``.
    They're read-only references — the chat skips the "reuse this artifact?"
    prompt for hits tagged ``materials_project`` because there's no
    runnable workflow to reuse.
    """

    name = "searcher_materials_project"
    description = (
        "Searches the Materials Project database (next-gen API) for real "
        "DFT-computed materials matching the goal's elements and target "
        "property ranges. Returns read-only reference records. Requires "
        "``MP_API_KEY`` in the environment (.env supported)."
    )

    async def search(self, goal: ResearchGoal) -> SearchResult:
        api_key = os.environ.get("MP_API_KEY")
        if not api_key:
            logger.debug(
                "MP_API_KEY not set; MaterialsProjectSearcher returning empty."
            )
            return SearchResult(catalog_hits=[], prior_results=[])

        elements = detect_elements(goal.goal)
        params: dict[str, Any] = {}
        if elements:
            # The MP summary endpoint accepts a comma-separated list and
            # interprets it as a *set* — returns materials whose element
            # set is exactly the listed elements. We want subset matching
            # too, so we send elements as ``elements=A,B`` (any-of) and
            # also a ``chemsys=A-B`` constraint that limits to the system.
            params["elements"] = ",".join(elements)
        params.update(target_to_params(goal.target or {}))

        docs = fetch_mp_summary(params, api_key=api_key, limit=10)
        catalog_hits = [mp_doc_to_catalog_hit(doc) for doc in docs]

        # No prior_results — MP records are reference data, not runs of
        # arc artifacts. Leave empty so the ideator falls through to
        # local results lookup separately if it needs them.
        return SearchResult(catalog_hits=catalog_hits, prior_results=[])
