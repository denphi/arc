"""CuratorAgent — canonicalises output key names across iterations.

The schema registry (stored in session.json) maps canonical key names to
their physical quantity, unit, and any aliases seen so far:

  {
    "bandgap_ev": {
      "quantity": "bandgap",
      "unit": "eV",
      "aliases": ["Eg_total", "energy_gap", "bandgap_eV"]
    }
  }

When a new artifact is built, the curator:
1. Checks each input/output key against the registry.
2. If a key is a known alias → patches workflow.py to use the canonical name.
3. If a key looks like a physics synonym but isn't registered → classifies it
   with the LLM (or heuristically) and adds it to the registry.
4. Returns the (possibly patched) artifact and the updated registry.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from arc.contracts.agent import AgentContract

# Reuse the same synonym + stop-word tables as the reviewer.
# We import lazily to avoid a hard circular dependency at module load time.


_CLASSIFY_PROMPT = """\
You are a physics schema curator. Classify the following simulation output key.

Key name : {key}
Context  : appears in a simulation with inputs {inputs} and other outputs {other_outputs}

Return a JSON object with exactly:
{{
  "quantity": "<physics quantity, e.g. bandgap, strain, temperature>",
  "unit"    : "<SI unit abbreviation or empty string, e.g. eV, GPa, K>",
  "canonical": "<preferred snake_case key name, e.g. bandgap_ev>"
}}

Rules:
- canonical must be a valid Python identifier in snake_case
- canonical should match what the user specified as a target key when possible
- unit may be empty if dimensionless or unknown
- No extra keys.
"""


@dataclass
class SchemaEntry:
    quantity: str
    unit: str
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"quantity": self.quantity, "unit": self.unit, "aliases": self.aliases}

    @classmethod
    def from_dict(cls, d: dict) -> "SchemaEntry":
        return cls(quantity=d.get("quantity", ""), unit=d.get("unit", ""), aliases=d.get("aliases", []))


def _load_registry(raw: dict) -> dict[str, SchemaEntry]:
    return {k: SchemaEntry.from_dict(v) for k, v in (raw or {}).items()}


def _dump_registry(registry: dict[str, SchemaEntry]) -> dict:
    return {k: v.to_dict() for k, v in registry.items()}


def _find_canonical(key: str, registry: dict[str, SchemaEntry]) -> str | None:
    """Return the canonical key name if `key` is a known alias or canonical."""
    key_flat = key.replace("_", "").lower()
    for canon, entry in registry.items():
        if canon.replace("_", "").lower() == key_flat:
            return canon
        for alias in entry.aliases:
            if alias.replace("_", "").lower() == key_flat:
                return canon
    return None


def _patch_workflow_key(code: str, old_key: str, new_key: str) -> str:
    """Rename a single output key in the return dict literal of workflow.py."""
    # Replace quoted key in return dict: "old_key": → "new_key":
    patched = re.sub(
        rf'(["\']){re.escape(old_key)}(["\'])\s*:',
        lambda m: f'{m.group(1)}{new_key}{m.group(2)}:',
        code,
    )
    return patched


def _heuristic_classify(key: str, target: dict) -> dict | None:
    """Quick heuristic: if key matches a target key (after flattening), use that."""
    key_flat = key.replace("_", "").lower()
    for tk in target:
        if tk.replace("_", "").lower() == key_flat:
            return {"quantity": tk, "unit": "", "canonical": tk}
    return None


class CuratorAgent(AgentContract):
    name = "curator"
    description = "Canonicalises output key names and maintains the session schema registry."

    async def run(self, artifact: Any) -> Any:
        """Patch artifact workflow.py output keys to canonical names.

        Reads `schema_registry` and `target` from context memory.
        Updates `schema_registry` in context memory with any new entries.
        Returns the (possibly modified) artifact.
        """
        from pathlib import Path

        registry = _load_registry(self.context.memory.get("schema_registry", {}))
        target: dict = self.context.memory.get("target", {})
        provider = self.context.memory.get("provider")

        # Ensure target keys are in the registry.
        for tk, tv in target.items():
            if tk not in registry:
                registry[tk] = SchemaEntry(
                    quantity=tk,
                    unit="",
                    aliases=[],
                )

        # Read the current workflow source.
        workflow_path = Path(artifact.path) / "workflow.py"
        if not workflow_path.exists():
            self.context.memory["schema_registry"] = _dump_registry(registry)
            return artifact

        code = workflow_path.read_text()

        # Find all output keys in the return dict.
        output_keys: list[str] = []
        for m in re.finditer(r'return\s*\{([^}]+)\}', code, re.DOTALL):
            body = m.group(1)
            for km in re.finditer(r'["\'](\w+)["\']\s*:', body):
                output_keys.append(km.group(1))

        if not output_keys:
            self.context.memory["schema_registry"] = _dump_registry(registry)
            return artifact

        # Get the input keys for context.
        input_keys = list(artifact.metadata.get("sim2l_inputs", {}).keys())
        other_outputs: list[str] = list(output_keys)

        patched_code = code
        artifact_outputs = dict(artifact.metadata.get("sim2l_outputs", {}))
        new_outputs: dict[str, Any] = {}

        for key in output_keys:
            canonical = _find_canonical(key, registry)

            if canonical is None:
                # Unknown key — try to classify it.
                classified = _heuristic_classify(key, target)
                if classified is None and provider:
                    try:
                        import json as _json
                        prompt = _CLASSIFY_PROMPT.format(
                            key=key,
                            inputs=input_keys,
                            other_outputs=[k for k in other_outputs if k != key],
                        )
                        raw = await provider.complete(prompt)
                        raw = raw.strip()
                        m = re.search(r"\{.*\}", raw, re.DOTALL)
                        if m:
                            classified = _json.loads(m.group())
                    except Exception:
                        pass

                if classified:
                    canonical_name: str = classified.get("canonical", key)
                    quantity: str = classified.get("quantity", key)
                    unit: str = classified.get("unit", "")
                    if canonical_name in registry:
                        # Add this key as an alias of the existing canonical.
                        if key not in registry[canonical_name].aliases and key != canonical_name:
                            registry[canonical_name].aliases.append(key)
                        canonical = canonical_name
                    else:
                        registry[canonical_name] = SchemaEntry(
                            quantity=quantity,
                            unit=unit,
                            aliases=[key] if key != canonical_name else [],
                        )
                        canonical = canonical_name
                else:
                    # Can't classify — register as-is.
                    registry[key] = SchemaEntry(quantity=key, unit="", aliases=[])
                    canonical = key

            else:
                # Known alias — add to alias list if not already there.
                if key != canonical and key not in registry[canonical].aliases:
                    registry[canonical].aliases.append(key)

            # Patch the workflow source if the key differs from canonical.
            if key != canonical:
                patched_code = _patch_workflow_key(patched_code, key, canonical)

            # Build the new output spec.
            entry = registry[canonical]
            desc = f"{entry.quantity} ({entry.unit})" if entry.unit else entry.quantity
            new_outputs[canonical] = {"type": "Number", "description": desc}

        # Write back patched workflow if changed.
        if patched_code != code:
            workflow_path.write_text(patched_code)
            # Update sim2l.yaml outputs section too.
            yaml_path = Path(artifact.path) / "sim2l.yaml"
            if yaml_path.exists():
                yaml_text = yaml_path.read_text()
                # Rebuild outputs section.
                outputs_block = _build_yaml_section(new_outputs)
                yaml_text = re.sub(
                    r'(^outputs:\n)((?:  .*\n?)*)',
                    f'outputs:\n{outputs_block}\n',
                    yaml_text,
                    flags=re.MULTILINE,
                )
                yaml_path.write_text(yaml_text)

            # Update artifact metadata in memory.
            if hasattr(artifact, 'metadata') and isinstance(artifact.metadata, dict):
                artifact.metadata["sim2l_outputs"] = new_outputs

        self.context.memory["schema_registry"] = _dump_registry(registry)
        return artifact


def _build_yaml_section(fields: dict) -> str:
    lines = []
    for name, meta in fields.items():
        lines.append(f"  {name}:")
        lines.append(f"    type: {meta.get('type', 'Number')}")
        if "default" in meta:
            lines.append(f"    default: {meta['default']}")
        if "description" in meta:
            lines.append(f"    description: {meta['description']}")
    return "\n".join(lines)
