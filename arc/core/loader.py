import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from arc.contracts.agent import AgentContext
from arc.core.registry import ComponentRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PackageResource:
    name: str
    kind: str
    path: str | None = None
    content: str | None = None
    data: Any = None


class MarkdownSkill:
    """Simple registry wrapper for markdown-defined package skills."""

    def __init__(self, name: str, path: Path, content: str):
        self.name = name
        self.description = _extract_markdown_section(content, "Description") or name
        self.path = str(path)
        self.content = content

    async def execute(self, inputs: dict[str, Any], context: AgentContext) -> dict[str, Any]:
        provider = context.memory.get("provider")
        if provider is None:
            return {
                "skill": self.name,
                "status": "unexecuted",
                "reason": "No provider configured for markdown skill execution",
                "inputs": inputs,
            }
        prompt = (
            f"Execute the following ARC skill using the provided inputs.\n\n"
            f"{self.content}\n\nInputs:\n{inputs}\n\n"
            "Return a concise JSON-compatible result."
        )
        return {"skill": self.name, "result": await provider.complete(prompt)}


def _extract_markdown_section(content: str, heading: str) -> str:
    marker = f"## {heading}"
    if marker not in content:
        return ""
    section = content.split(marker, 1)[1]
    if "\n## " in section:
        section = section.split("\n## ", 1)[0]
    return section.strip()


def _resource_name(value: str | dict, fallback_path: Path | None = None) -> str:
    if isinstance(value, dict):
        return value.get("name") or (fallback_path.stem if fallback_path else "")
    path = Path(value)
    return path.stem if path.suffix else value


def _load_resource(
    package_dir: Path,
    value: str | dict,
    kind: str,
    default_dir: str,
    extensions: tuple[str, ...] = (".md", ".yaml", ".yml", ".txt"),
) -> PackageResource:
    name = _resource_name(value)
    if isinstance(value, dict):
        raw_path = value.get("path")
        if not raw_path:
            return PackageResource(name=name, kind=kind, data=value)
        candidates = [package_dir / raw_path]
    else:
        raw = Path(value)
        candidates = [package_dir / value]
        if not raw.suffix:
            candidates.extend(package_dir / default_dir / f"{value}{ext}" for ext in extensions)
            dashed = value.replace("_", "-")
            candidates.extend(package_dir / default_dir / f"{dashed}{ext}" for ext in extensions)
            resource_dir = package_dir / default_dir
            if resource_dir.exists():
                wanted = set(value.replace("-", "_").split("_"))
                for ext in extensions:
                    for path in resource_dir.glob(f"*{ext}"):
                        found = set(path.stem.replace("-", "_").split("_"))
                        if found and found.issubset(wanted):
                            candidates.append(path)

    for path in candidates:
        if path.exists() and path.is_file():
            content = path.read_text()
            data: Any = content
            if path.suffix in {".yaml", ".yml"}:
                data = yaml.safe_load(content)
            return PackageResource(
                name=name,
                kind=kind,
                path=str(path),
                content=content,
                data=data,
            )
    return PackageResource(name=name, kind=kind, data=value)


def _import_class(entrypoint: str):
    """Import a class from a dotted entrypoint string like 'module.path:ClassName'."""
    module_path, class_name = entrypoint.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def load_package(package_dir: Path, registry: ComponentRegistry) -> None:
    manifest_path = package_dir / "package.yaml"
    if not manifest_path.exists():
        logger.warning("No package.yaml found in %s — skipping", package_dir)
        return

    with manifest_path.open() as f:
        manifest = yaml.safe_load(f)

    pkg_name = manifest.get("name", package_dir.name)
    logger.info("Loading package: %s", pkg_name)

    for agent_def in manifest.get("provides", {}).get("agents", []):
        try:
            agent_class = _import_class(agent_def["entrypoint"])
            registry.register_agent(agent_def["name"], agent_class)
        except Exception as exc:
            logger.error("Failed to load agent '%s': %s", agent_def.get("name"), exc)

    for skill_def in manifest.get("provides", {}).get("skills", []):
        skill_path = package_dir / skill_def
        try:
            content = skill_path.read_text()
            registry.register_skill(_resource_name(skill_def, skill_path), MarkdownSkill(
                _resource_name(skill_def, skill_path),
                skill_path,
                content,
            ))
        except Exception as exc:
            logger.error("Failed to load skill '%s': %s", skill_def, exc)

    for workflow_def in manifest.get("provides", {}).get("workflows", []):
        workflow_path = package_dir / workflow_def["path"]
        if workflow_path.exists():
            with workflow_path.open() as f:
                workflow = yaml.safe_load(f)
            registry.register_workflow(workflow_def["name"], workflow)
        else:
            logger.warning("Workflow file not found: %s", workflow_path)

    for adapter_def in manifest.get("provides", {}).get("runtime_adapters", []):
        try:
            adapter_class = _import_class(adapter_def["entrypoint"])
            registry.register_adapter(adapter_def["name"], adapter_class)
        except Exception as exc:
            logger.error("Failed to load adapter '%s': %s", adapter_def.get("name"), exc)

    for evaluator_def in manifest.get("provides", {}).get("evaluators", []):
        try:
            if isinstance(evaluator_def, dict) and "entrypoint" in evaluator_def:
                evaluator = _import_class(evaluator_def["entrypoint"])
                registry.register_evaluator(evaluator_def["name"], evaluator)
            else:
                registry.register_evaluator(_resource_name(evaluator_def), evaluator_def)
        except Exception as exc:
            logger.error("Failed to load evaluator '%s': %s", evaluator_def, exc)

    for prompt_def in manifest.get("provides", {}).get("prompts", []):
        resource = _load_resource(package_dir, prompt_def, "prompt", "prompts", (".md", ".txt"))
        registry.register_prompt(resource.name, resource)

    for template_def in manifest.get("provides", {}).get("templates", []):
        resource = _load_resource(package_dir, template_def, "template", "templates")
        registry.register_template(resource.name, resource)

    for constraint_def in manifest.get("provides", {}).get("constraints", []):
        resource = _load_resource(package_dir, constraint_def, "constraint", "constraints")
        registry.register_constraint(resource.name, resource)

    for vocabulary_def in manifest.get("provides", {}).get("vocabularies", []):
        resource = _load_resource(package_dir, vocabulary_def, "vocabulary", "vocabularies")
        registry.register_vocabulary(resource.name, resource)


def load_packages(package_paths: list[str], registry: ComponentRegistry) -> None:
    for path_str in package_paths:
        package_dir = Path(path_str)
        if package_dir.exists():
            load_package(package_dir, registry)
        else:
            logger.warning("Package path does not exist: %s", path_str)
