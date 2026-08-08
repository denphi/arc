from __future__ import annotations

import hashlib
import importlib
import json
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
    """Registry wrapper that activates Markdown instructions lazily."""

    def __init__(self, name: str, path: Path, content: str | None = None):
        self.name = name
        if content is None:
            content = path.read_text(encoding="utf-8")
        frontmatter = _markdown_frontmatter(content)
        self.bundle = None
        if path.name == "SKILL.md":
            from arc.core.skill_bundle import load_skill_bundle
            self.bundle = load_skill_bundle(path)
        self.frontmatter = frontmatter
        self.description = (
            frontmatter.get("description")
            or _extract_markdown_section(content, "Description")
            or name
        )
        self.path = str(path)
        self.bundle_root = str(path.parent) if path.name == "SKILL.md" else None
        # Deliberately *not* seeded from `content`: the constructor's copy is
        # only used for frontmatter. Deferring the body read means an edited
        # SKILL.md takes effect on next activation without reloading packages.
        self._content: str | None = None
        self.metadata = {
            **frontmatter,
            "path": str(path),
            "bundle_root": self.bundle_root,
            "resources": self.list_resources() if self.bundle_root else [],
            "allowed_tools": list(self.bundle.allowed_tools) if self.bundle else [],
        }

    @property
    def content(self) -> str:
        """Load and cache instructions only when the skill is activated.

        For a canonical bundle this is the instruction *body*: the frontmatter
        is metadata the loader has already consumed, and passing it on would
        put ``output_format: json`` and the tool allow-list into the model's
        prompt as if they were instructions to follow.
        """
        if self._content is None:
            raw = Path(self.path).read_text(encoding="utf-8")
            if self.bundle is not None:
                from arc.core.skill_bundle import split_frontmatter
                _, raw = split_frontmatter(raw)
            self._content = raw
        return self._content

    def resolve_resource(self, relative_path: str) -> Path:
        """Resolve a skill-bundle resource under the bundle root."""
        if self.bundle_root is None:
            raise ValueError(f"Skill {self.name!r} is not a bundle skill")
        raw = Path(relative_path)
        if raw.is_absolute():
            raise ValueError("Skill resources must use relative paths")
        root = Path(self.bundle_root).resolve()
        path = (root / raw).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Skill resource escapes bundle root: {relative_path}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"Skill resource not found: {relative_path}")
        return path

    def read_resource(self, relative_path: str, max_bytes: int = 200_000) -> str:
        path = self.resolve_resource(relative_path)
        size = path.stat().st_size
        if size > max_bytes:
            raise ValueError(
                f"Skill resource too large to read: {size} bytes exceeds {max_bytes}"
            )
        return path.read_text(encoding="utf-8")

    def list_resources(self, subdir: str | None = None) -> list[str]:
        if self.bundle_root is None:
            return []
        root = Path(self.bundle_root).resolve()
        base = root / subdir if subdir else root
        if not base.exists() or not base.is_dir():
            return []
        resources = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name == "SKILL.md":
                continue
            try:
                resources.append(path.resolve().relative_to(root).as_posix())
            except ValueError:
                continue
        return resources

    async def execute(self, inputs: dict[str, Any], context: AgentContext) -> dict[str, Any]:
        provider = context.memory.get("provider")
        if provider is None:
            return {
                "skill": self.name,
                "status": "unexecuted",
                "reason": "No provider configured for markdown skill execution",
                "inputs": inputs,
            }
        # Render inputs as JSON so quoting / None / nested values stay stable
        # in the prompt. `default=str` lets us handle objects that are not
        # natively JSON-serialisable (Pydantic models, dataclasses, etc.)
        # without crashing the skill mid-execution.
        try:
            inputs_block = json.dumps(inputs, indent=2, default=str)
        except (TypeError, ValueError):
            inputs_block = repr(inputs)
        bundle_block = self._bundle_prompt_block()
        file_block = self._file_prompt_block(inputs, context)
        # A skill can declare `output_format: json` (or `output_schema:`) in its
        # SKILL.md frontmatter when its contract is a structured JSON object
        # rather than free text. We then steer the model to emit JSON and parse
        # the response into a dict/list, so a downstream step receives
        # structured data instead of an opaque blob. Default (unset) keeps the
        # historical free-text behaviour, so existing skills are unaffected.
        wants_json = self._wants_json_output()
        tail = (
            "Return ONLY a single valid JSON value (object or array). "
            "No markdown fences, no prose before or after."
            if wants_json
            else "Return a concise JSON-compatible result."
        )
        prompt = (
            "Execute the following ARC skill using the provided inputs.\n\n"
            f"{self.content}\n\n"
            f"{bundle_block}"
            f"{file_block}"
            f"Inputs:\n{inputs_block}\n\n"
            f"{tail}"
        )
        raw = await provider.complete(prompt)
        if not wants_json:
            return {"skill": self.name, "result": raw}
        parsed, ok = self._parse_json_result(raw)
        result: dict[str, Any] = {"skill": self.name, "result": parsed, "format": "json"}
        if not ok:
            # Parsing failed — keep the raw text and flag it so a workflow can
            # decide what to do, rather than silently passing a string where a
            # structured value was promised.
            result["format"] = "text"
            result["parse_error"] = True
        return result

    def _wants_json_output(self) -> bool:
        """True when the skill frontmatter asks for a structured JSON result."""
        fmt = str(self.frontmatter.get("output_format", "")).strip().lower()
        if fmt in {"json", "structured"}:
            return True
        # `output_schema` may be a name, an inline mapping, or a path — any
        # truthy value means "this skill returns structured JSON".
        return bool(self.frontmatter.get("output_schema"))

    @staticmethod
    def _parse_json_result(raw: str) -> tuple[Any, bool]:
        """Parse an LLM response as JSON, tolerating code fences / surrounding prose.

        Returns ``(value, ok)``. On failure ``value`` is the original string so
        callers can still surface it.
        """
        if not isinstance(raw, str):
            return raw, False
        from arc.providers.utils import strip_code_fences
        text = strip_code_fences(raw).strip()
        try:
            return json.loads(text), True
        except (TypeError, ValueError):
            pass
        # Best-effort: grab the outermost JSON object/array if the model wrapped
        # it in explanation.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if 0 <= start < end:
                try:
                    return json.loads(text[start:end + 1]), True
                except (TypeError, ValueError):
                    continue
        return raw, False

    def _bundle_prompt_block(self) -> str:
        if not self.bundle_root:
            return ""
        resources = self.list_resources()
        if not resources:
            return ""
        shown = "\n".join(f"- {item}" for item in resources[:40])
        suffix = "\n- ..." if len(resources) > 40 else ""
        return f"Skill bundle resources:\n{shown}{suffix}\n\n"

    def _file_prompt_block(self, inputs: dict[str, Any], context: AgentContext) -> str:
        store = context.files
        if store is None:
            return ""
        ids = sorted({
            value
            for value in inputs.values()
            if isinstance(value, str) and value.startswith("file_")
        })
        rows = []
        for file_id in ids:
            try:
                asset = store.get(file_id)
            except Exception:  # noqa: BLE001
                continue
            rows.append(f"- {file_id}: {asset.name} {asset.role or '-'} {asset.media_type}")
        if not rows:
            return ""
        return "Available file inputs:\n" + "\n".join(rows) + "\n\n"


def _extract_markdown_section(content: str, heading: str) -> str:
    marker = f"## {heading}"
    if marker not in content:
        return ""
    section = content.split(marker, 1)[1]
    if "\n## " in section:
        section = section.split("\n## ", 1)[0]
    return section.strip()


def _markdown_frontmatter(content: str) -> dict[str, Any]:
    """Return YAML frontmatter from a markdown file, if present."""
    if not content.startswith("---"):
        return {}
    rest = content[3:].lstrip("\n")
    end = rest.find("\n---")
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(rest[:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _resource_name(value: str | dict, fallback_path: Path | None = None) -> str:
    if isinstance(value, dict):
        return value.get("name") or (fallback_path.stem if fallback_path else "")
    path = Path(value)
    return path.stem if path.suffix else value


def _skill_path(package_dir: Path, value: str | dict) -> Path:
    raw_path = value.get("path") if isinstance(value, dict) else value
    if not raw_path:
        raise ValueError("Skill manifest entry needs a path")
    return package_dir / str(raw_path)


def _skill_name(value: str | dict, path: Path, content: str | None = None) -> str:
    if content is None and path.exists():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = None
    # A canonical bundle carries its portable identity in SKILL.md. Flat
    # legacy files may still use an explicit package-manifest alias.
    if content and path.name == "SKILL.md":
        frontmatter = _markdown_frontmatter(content)
        if frontmatter.get("name"):
            return str(frontmatter["name"])
    if isinstance(value, dict) and value.get("name"):
        return str(value["name"])
    if content:
        frontmatter = _markdown_frontmatter(content)
        if frontmatter.get("name"):
            return str(frontmatter["name"])
    return _resource_name(value, path)


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
    """Import a class from a dotted entrypoint string like 'module.path:ClassName'.

    Package manifests historically use distribution-style names with hyphens
    (e.g. ``arc.packages.arc-sim2l.agents.ideator``). ``importlib`` accepts
    these on CPython today because the directories exist, but the resulting
    modules are not importable through normal ``import`` statements. To keep
    the manifest format stable while making the modules first-class
    importables, this helper falls back to a filesystem-based loader that
    registers the module under an underscored alias (``arc_sim2l.agents.…``)
    when the dotted path contains hyphens.
    """
    from arc.core.module_loader import load_module_from_path, register_module_path

    module_path, class_name = entrypoint.rsplit(":", 1)

    if "-" not in module_path:
        module = importlib.import_module(module_path)
        # Claim the file for this (real, importable) module name so a strategy
        # load of the same path reuses it instead of making a second copy.
        register_module_path(module)
        return getattr(module, class_name)

    # Walk the path components to find the .py file.
    parts = module_path.split(".")
    here = Path(__file__).resolve().parents[2]  # arc/ repo root
    candidate = here / "/".join(parts[:-1]) / f"{parts[-1]}.py"
    if not candidate.exists():
        # Last resort — try CPython's behaviour for compatibility.
        module = importlib.import_module(module_path)
        register_module_path(module)
        return getattr(module, class_name)

    # Hyphenated path: resolve via the filesystem, keeping the historical
    # underscored alias as the module name when this is the first load.
    module = load_module_from_path(candidate, preferred_name=module_path.replace("-", "_"))
    return getattr(module, class_name)


def _import_from_file(module_file: Path, attr: str, module_name: str | None = None):
    """Import ``attr`` from a Python file.

    Local ARC packages can live outside ``arc.packages`` and therefore may
    not have a dotted import path. Their manifests use ``path`` + ``class`` or
    ``path`` + ``function`` declarations instead.
    """
    from arc.core.module_loader import load_module_from_path

    if not module_file.exists():
        raise ImportError(f"Cannot load {attr!r}; file does not exist: {module_file}")
    if module_name is None:
        token = "_".join(module_file.with_suffix("").parts[-4:]).replace("-", "_")
        digest = hashlib.sha256(str(module_file.resolve()).encode("utf-8")).hexdigest()[:12]
        module_name = f"arc_local_package_{token}_{digest}"
    return getattr(load_module_from_path(module_file, preferred_name=module_name), attr)


def _declared_attr_name(definition: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = definition.get(key)
        if value:
            return str(value)
    return None


def _import_declared(
    definition: dict[str, Any],
    package_dir: Path,
    *,
    attr_keys: tuple[str, ...] = ("class", "function", "callable"),
):
    """Import an object from either ``entrypoint`` or package-relative file metadata."""
    entrypoint = definition.get("entrypoint")
    if entrypoint:
        return _import_class(str(entrypoint))

    rel_path = definition.get("path")
    attr = _declared_attr_name(definition, *attr_keys)
    if not rel_path or not attr:
        raise ValueError("Manifest entry needs either entrypoint or path plus class/function")

    module_file = package_dir / str(rel_path)
    module_token = (
        "arc_local_package_"
        f"{package_dir.name.replace('-', '_')}_"
        f"{str(rel_path).replace('/', '_').replace('-', '_').removesuffix('.py')}"
    )
    return _import_from_file(module_file, attr, module_token)


def _strategy_spec_from_manifest(package_dir: Path, pkg_name: str, strategy_def: dict[str, Any]):
    """Build a StrategySpec from a package manifest strategy declaration.

    The strategy resolver historically loaded built-in strategies from
    ``arc/packages/<package>/<module_path>``. Package-declared strategies need
    the same lazy behavior without hard-coding package names in core, so the
    manifest contributes an absolute ``file_path`` resolved from the package
    directory that loaded it.
    """
    from arc.core.strategies import StrategySpec

    entrypoint = strategy_def.get("entrypoint")
    attr = _declared_attr_name(strategy_def, "class", "function", "callable")
    module_path = ""
    if entrypoint:
        module_path, entrypoint_attr = str(entrypoint).rsplit(":", 1)
        attr = attr or entrypoint_attr
    if not attr:
        raise ValueError(
            f"Strategy {strategy_def.get('name')!r} needs entrypoint or class/function"
        )
    rel_path = strategy_def.get("path")
    if rel_path:
        module_file = package_dir / rel_path
        module_rel = rel_path
    else:
        if not module_path:
            raise ValueError(
                f"Strategy {strategy_def.get('name')!r} needs path when entrypoint is omitted"
            )
        parts = module_path.split(".")
        package_tokens = {pkg_name, package_dir.name}
        package_index = next(
            (idx for idx, part in enumerate(parts) if part in package_tokens),
            None,
        )
        if package_index is None or package_index == len(parts) - 1:
            raise ValueError(
                f"Cannot map strategy entrypoint {entrypoint!r} to a file under {package_dir}"
            )
        module_rel = "/".join(parts[package_index + 1:]) + ".py"
        module_file = package_dir / module_rel
    return StrategySpec(
        name=strategy_def["name"],
        package_dir=pkg_name,
        module_path=module_rel,
        attr=attr,
        description=strategy_def.get("description", ""),
        file_path=str(module_file),
        aliases=tuple(strategy_def.get("aliases") or ()),
        backend_label=strategy_def.get("backend_label"),
        callback_profile=(
            strategy_def.get("callback_profile") if strategy_def.get("callback_profile") else None
        ),
    )


def load_package(package_dir: Path, registry: ComponentRegistry) -> None:
    manifest_path = package_dir / "package.yaml"
    if not manifest_path.exists():
        logger.warning("No package.yaml found in %s — skipping", package_dir)
        return

    try:
        with manifest_path.open() as f:
            manifest = yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read package manifest '%s': %s", manifest_path, exc)
        registry.record_load_error(package_dir.name, "manifest", str(manifest_path), str(exc))
        return

    pkg_name = manifest.get("name", package_dir.name)
    logger.info("Loading package: %s", pkg_name)
    registry.register_package(pkg_name, manifest)

    def record_error(kind: str, name: Any, exc: Exception | str) -> None:
        registry.record_load_error(pkg_name, kind, str(name or ""), str(exc))

    # Surface missing *required* config early (a clear startup warning beats
    # a late KeyError / silent empty-string deep in a run). The package still
    # loads — a required var may be set later, or the feature degrades.
    import os as _os
    for entry in manifest.get("config", []) or []:
        if (
            isinstance(entry, dict)
            and entry.get("required")
            and not _os.environ.get(entry.get("name", ""))
        ):
            logger.warning(
                "Package %r requires config %r which is unset — set it in "
                ".env or the environment. (%s)",
                pkg_name, entry.get("name"), entry.get("description", ""),
            )

    for agent_def in manifest.get("provides", {}).get("agents", []):
        try:
            agent_class = _import_declared(agent_def, package_dir)
            registry.register_agent(agent_def["name"], agent_class, package_name=pkg_name)
        except Exception as exc:
            logger.error("Failed to load agent '%s': %s", agent_def.get("name"), exc)
            record_error("agent", agent_def.get("name"), exc)

    for strategy_def in manifest.get("provides", {}).get("strategies", []):
        try:
            from arc.core.strategies import register_strategy
            spec = _strategy_spec_from_manifest(package_dir, pkg_name, strategy_def)
            register_strategy(
                strategy_def["role"],
                spec,
                make_default=bool(strategy_def.get("default", False)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load strategy '%s': %s", strategy_def.get("name"), exc)
            record_error("strategy", strategy_def.get("name"), exc)

    for skill_def in manifest.get("provides", {}).get("skills", []):
        skill_path = _skill_path(package_dir, skill_def)
        try:
            content = skill_path.read_text(encoding="utf-8")
            name = _skill_name(skill_def, skill_path, content)
            registry.register_skill(name, MarkdownSkill(
                name,
                skill_path,
                content,
            ), package_name=pkg_name)
        except Exception as exc:
            logger.error("Failed to load skill '%s': %s", skill_def, exc)
            record_error("skill", _resource_name(skill_def) if skill_def else "", exc)

    for workflow_def in manifest.get("provides", {}).get("workflows", []):
        try:
            workflow_path = package_dir / workflow_def["path"]
            if workflow_path.exists():
                with workflow_path.open() as f:
                    workflow = yaml.safe_load(f)
                registry.register_workflow(workflow_def["name"], workflow, package_name=pkg_name)
            else:
                message = f"Workflow file not found: {workflow_path}"
                logger.warning("%s", message)
                record_error("workflow", workflow_def.get("name"), message)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load workflow '%s': %s", workflow_def.get("name"), exc)
            record_error("workflow", workflow_def.get("name"), exc)

    for script_def in manifest.get("provides", {}).get("scripts", []):
        try:
            if not isinstance(script_def, dict):
                raise ValueError("script declaration must be a mapping")
            script_path = (package_dir / str(script_def["path"])).resolve()
            if not script_path.exists() or not script_path.is_file():
                raise FileNotFoundError(script_path)
            definition = dict(script_def)
            definition["path"] = str(script_path)
            definition.setdefault("runtime", "python")
            registry.register_script(definition["name"], definition, package_name=pkg_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load script '%s': %s", script_def, exc)
            name = script_def.get("name") if isinstance(script_def, dict) else script_def
            record_error("script", name, exc)

    for extension_def in manifest.get("provides", {}).get("extensions", []):
        try:
            definition = dict(extension_def)
            definition.setdefault("_package_dir", str(package_dir))
            # Stamp the owning package so the kernel can attribute the
            # components an extension registers at startup to it, keeping
            # /package disable effective for extension-created components
            # (review finding P2-2).
            definition.setdefault("_package_name", pkg_name)
            registry.register_extension_definition(extension_def["name"], definition)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load extension definition '%s': %s",
                         extension_def.get("name"), exc)
            record_error("extension", extension_def.get("name"), exc)

    for adapter_def in manifest.get("provides", {}).get("runtime_adapters", []):
        try:
            adapter_class = _import_declared(adapter_def, package_dir)
            registry.register_adapter(adapter_def["name"], adapter_class, package_name=pkg_name)
            for alias in adapter_def.get("aliases", []) or []:
                registry.register_adapter(alias, adapter_class, package_name=pkg_name)
        except Exception as exc:
            logger.error("Failed to load adapter '%s': %s", adapter_def.get("name"), exc)
            record_error("runtime_adapter", adapter_def.get("name"), exc)

    for backend_def in manifest.get("provides", {}).get("backends", []):
        # Register the backend *class* under its name. ``resolve_backend``
        # instantiates it when ``ARC_BACKEND`` / ``arc.toml [backend] kind``
        # names it — the seam that lets a package ship a publish backend
        # (S3, Zenodo, LIMS, …) without touching core.
        try:
            backend_class = _import_declared(backend_def, package_dir)
            registry.register_backend(backend_def["name"], backend_class, package_name=pkg_name)
            for alias in backend_def.get("aliases", []) or []:
                registry.register_backend(alias, backend_class, package_name=pkg_name)
        except Exception as exc:
            logger.error("Failed to load backend '%s': %s", backend_def.get("name"), exc)
            record_error("backend", backend_def.get("name"), exc)

    for provider_def in manifest.get("provides", {}).get("providers", []):
        # Register the provider *class* (not an instance) under its name.
        # ``build_provider`` instantiates it on demand with the caller's
        # token/model/base_url. This is the seam that lets a package ship a
        # new LLM provider without touching core (core ships only openwebui).
        try:
            provider_class = _import_declared(provider_def, package_dir)
            registry.register_provider(provider_def["name"], provider_class, package_name=pkg_name)
        except Exception as exc:
            logger.error("Failed to load provider '%s': %s", provider_def.get("name"), exc)
            record_error("provider", provider_def.get("name"), exc)

    for loader_def in manifest.get("provides", {}).get("loaders", []):
        try:
            loader = _import_declared(loader_def, package_dir)
            registry.register_loader(loader_def["name"], loader, package_name=pkg_name)
        except Exception as exc:
            logger.error("Failed to load loader '%s': %s", loader_def.get("name"), exc)
            record_error("loader", loader_def.get("name"), exc)

    for evaluator_def in manifest.get("provides", {}).get("evaluators", []):
        try:
            if isinstance(evaluator_def, dict) and (
                "entrypoint" in evaluator_def or "path" in evaluator_def
            ):
                evaluator = _import_declared(evaluator_def, package_dir)
                registry.register_evaluator(evaluator_def["name"], evaluator)
                registry.record_source("evaluator", evaluator_def["name"], pkg_name)
            else:
                registry.register_evaluator(_resource_name(evaluator_def), evaluator_def)
                registry.record_source("evaluator", _resource_name(evaluator_def), pkg_name)
        except Exception as exc:
            logger.error("Failed to load evaluator '%s': %s", evaluator_def, exc)
            record_error("evaluator", _resource_name(evaluator_def) if evaluator_def else "", exc)

    for prompt_def in manifest.get("provides", {}).get("prompts", []):
        try:
            resource = _load_resource(package_dir, prompt_def, "prompt", "prompts", (".md", ".txt"))
            registry.register_prompt(resource.name, resource)
            registry.record_source("prompt", resource.name, pkg_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load prompt '%s': %s", prompt_def, exc)
            record_error("prompt", _resource_name(prompt_def) if prompt_def else "", exc)

    for template_def in manifest.get("provides", {}).get("templates", []):
        try:
            resource = _load_resource(package_dir, template_def, "template", "templates")
            registry.register_template(resource.name, resource)
            registry.record_source("template", resource.name, pkg_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load template '%s': %s", template_def, exc)
            record_error("template", _resource_name(template_def) if template_def else "", exc)

    for constraint_def in manifest.get("provides", {}).get("constraints", []):
        try:
            resource = _load_resource(package_dir, constraint_def, "constraint", "constraints")
            registry.register_constraint(resource.name, resource)
            registry.record_source("constraint", resource.name, pkg_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load constraint '%s': %s", constraint_def, exc)
            name = _resource_name(constraint_def) if constraint_def else ""
            record_error("constraint", name, exc)

    for vocabulary_def in manifest.get("provides", {}).get("vocabularies", []):
        try:
            resource = _load_resource(package_dir, vocabulary_def, "vocabulary", "vocabularies")
            registry.register_vocabulary(resource.name, resource)
            registry.record_source("vocabulary", resource.name, pkg_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load vocabulary '%s': %s", vocabulary_def, exc)
            name = _resource_name(vocabulary_def) if vocabulary_def else ""
            record_error("vocabulary", name, exc)

    for detector_def in manifest.get("provides", {}).get("detectors", []):
        try:
            detector = _import_declared(detector_def, package_dir)
            registry.register_detector(detector_def["name"], detector)
            registry.record_source("detector", detector_def["name"], pkg_name)
        except Exception as exc:
            logger.error("Failed to load detector '%s': %s", detector_def.get("name"), exc)
            record_error("detector", detector_def.get("name"), exc)

    for audit_def in manifest.get("provides", {}).get("audit_actions", []):
        # An audit action is a class bound to a lifecycle phase (item 7). The
        # manifest may override the class's declared ``phase``/``priority``/
        # ``blocking`` so a package can re-bind a shared action without code.
        try:
            action_class = _import_declared(audit_def, package_dir)
            action = action_class()
            for attr in ("name", "phase", "priority", "blocking"):
                if attr in audit_def and audit_def[attr] is not None:
                    setattr(action, attr, audit_def[attr])
            registry.register_audit_action(action, package_name=pkg_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load audit action '%s': %s", audit_def.get("name"), exc)
            record_error("audit_action", audit_def.get("name"), exc)

    for section_def in manifest.get("provides", {}).get("report_sections", []):
        # A report-section contributor (item 7) adds a named section to the
        # final report without core hard-coding the package.
        try:
            section_class = _import_declared(section_def, package_dir)
            contributor = section_class()
            for attr in ("name", "section_name"):
                if attr in section_def and section_def[attr] is not None:
                    setattr(contributor, attr, section_def[attr])
            registry.register_report_section(
                getattr(contributor, "name", section_def.get("name", "section")),
                contributor,
                package_name=pkg_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load report section '%s': %s", section_def.get("name"), exc)
            record_error("report_section", section_def.get("name"), exc)


def load_packages(package_paths: list[str], registry: ComponentRegistry) -> None:
    for path_str in package_paths:
        package_dir = Path(path_str)
        if package_dir.exists():
            load_package(package_dir, registry)
        else:
            logger.warning("Package path does not exist: %s", path_str)
