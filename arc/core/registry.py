import logging
from collections import defaultdict
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)


T = TypeVar("T")


class _Slot(Generic[T]):
    """Generic registration slot for one kind of component.

    Avoids 12 nearly-identical (register/get/list) triplets in
    `ComponentRegistry`. Each slot tracks a name → value map and emits an
    INFO log on registration just like the original code did.
    """

    __slots__ = ("kind", "_items")

    def __init__(self, kind: str):
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, value: T) -> None:
        self._items[name] = value
        logger.info("%s registered: %s", self.kind.capitalize(), name)

    def get(self, name: str) -> T:
        if name not in self._items:
            raise KeyError(f"{self.kind.capitalize()} not found: {name}")
        return self._items[name]

    def get_or_none(self, name: str) -> T | None:
        return self._items.get(name)

    def list_names(self) -> list[str]:
        return list(self._items.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._items


class ComponentRegistry:
    """Registry for agents, skills, adapters, extensions, and providers.

    Most slot types follow the same (register/get/list) pattern and are
    backed by a `_Slot` instance under the hood. Agents are special-cased
    because they additionally track which package(s) provided a given name.
    """

    def __init__(self):
        # Agents need extra metadata (per-package source tracking), so they
        # remain hand-rolled rather than using the generic `_Slot`.
        self._agents: dict[str, Any] = {}
        self._agent_sources: dict[str, dict[str, Any]] = defaultdict(dict)
        self._agent_source_for_name: dict[str, str] = {}

        # Everything else is a uniform slot.
        self._packages = _Slot[dict[str, Any]]("package")
        self._skills = _Slot[Any]("skill")
        self._adapters = _Slot[Any]("adapter")
        self._extensions = _Slot[Any]("extension")
        self._providers = _Slot[Any]("provider")
        self._workflows = _Slot[dict]("workflow")
        self._evaluators = _Slot[Any]("evaluator")
        self._prompts = _Slot[Any]("prompt")
        self._templates = _Slot[Any]("template")
        self._constraints = _Slot[Any]("constraint")
        self._vocabularies = _Slot[Any]("vocabulary")

    # --- packages ---

    def register_package(self, name: str, manifest: dict[str, Any]) -> None:
        self._packages.register(name, manifest)

    def list_packages(self) -> list[str]:
        return self._packages.list_names()

    def get_package(self, name: str) -> dict[str, Any]:
        return self._packages.get(name)

    def package_config(self, name: str) -> dict[str, Any]:
        """Resolve a package's declared ``config:`` against the environment.

        Reads the package's ``config:`` manifest section (a list of
        ``{name, default, ...}`` entries) and returns ``{var: value}`` with
        each value taken from ``os.environ`` when set, else the declared
        ``default`` (or ``""``). Packages read this instead of reaching into
        ``os.environ`` with magic strings; the manifest is the single place
        a contributor declares + documents what the package needs.

        Returns an empty dict for an unknown package or one with no
        ``config:`` section.
        """
        import os
        try:
            manifest = self._packages.get(name)
        except KeyError:
            return {}
        resolved: dict[str, Any] = {}
        for entry in (manifest or {}).get("config", []) or []:
            if isinstance(entry, dict) and entry.get("name"):
                var = entry["name"]
            elif isinstance(entry, str):
                var = entry
                entry = {"name": var}
            else:
                continue
            resolved[var] = os.environ.get(var, entry.get("default", ""))
        return resolved

    # --- agents (special-cased: per-package source tracking) ---

    def register_agent(self, name: str, agent_class: Any, package_name: str | None = None) -> None:
        self._agents[name] = agent_class
        if package_name:
            self._agent_sources[name][package_name] = agent_class
            self._agent_source_for_name[name] = package_name
            self._agents[f"{package_name}:{name}"] = agent_class
        logger.info("Agent registered: %s%s", name, f" ({package_name})" if package_name else "")

    def get_agent(self, name: str, package_name: str | None = None) -> Any:
        if package_name:
            package_agents = self._agent_sources.get(name, {})
            if package_name not in package_agents:
                raise KeyError(f"Agent not found: {package_name}:{name}")
            return package_agents[package_name]
        if name not in self._agents:
            raise KeyError(f"Agent not found: {name}")
        return self._agents[name]

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    def list_agent_sources(self, name: str) -> list[str]:
        return list(self._agent_sources.get(name, {}).keys())

    def agent_source(self, name: str) -> str | None:
        return self._agent_source_for_name.get(name)

    # --- skills ---

    def register_skill(self, name: str, skill: Any) -> None:
        self._skills.register(name, skill)

    def get_skill(self, name: str) -> Any:
        return self._skills.get(name)

    def list_skills(self) -> list[str]:
        return self._skills.list_names()

    # --- adapters ---

    def register_adapter(self, name: str, adapter: Any) -> None:
        self._adapters.register(name, adapter)

    def get_adapter(self, name: str) -> Any:
        return self._adapters.get(name)

    def list_adapters(self) -> list[str]:
        return self._adapters.list_names()

    # --- extensions ---

    def register_extension(self, name: str, extension: Any) -> None:
        self._extensions.register(name, extension)

    def get_extension(self, name: str) -> Any:
        # Historical API: returns None (not raises) for missing extensions.
        return self._extensions.get_or_none(name)

    # --- providers ---

    def register_provider(self, name: str, provider: Any) -> None:
        self._providers.register(name, provider)

    def get_provider(self, name: str) -> Any:
        return self._providers.get(name)

    def list_providers(self) -> list[str]:
        return self._providers.list_names()

    # --- workflows ---

    def register_workflow(self, name: str, definition: dict) -> None:
        self._workflows.register(name, definition)

    def get_workflow(self, name: str) -> dict:
        return self._workflows.get(name)

    def list_workflows(self) -> list[str]:
        return self._workflows.list_names()

    # --- package resources ---

    def register_evaluator(self, name: str, evaluator: Any) -> None:
        self._evaluators.register(name, evaluator)

    def get_evaluator(self, name: str) -> Any:
        return self._evaluators.get(name)

    def list_evaluators(self) -> list[str]:
        return self._evaluators.list_names()

    def register_prompt(self, name: str, prompt: Any) -> None:
        self._prompts.register(name, prompt)

    def get_prompt(self, name: str) -> Any:
        return self._prompts.get(name)

    def list_prompts(self) -> list[str]:
        return self._prompts.list_names()

    def register_template(self, name: str, template: Any) -> None:
        self._templates.register(name, template)

    def get_template(self, name: str) -> Any:
        return self._templates.get(name)

    def list_templates(self) -> list[str]:
        return self._templates.list_names()

    def register_constraint(self, name: str, constraint: Any) -> None:
        self._constraints.register(name, constraint)

    def get_constraint(self, name: str) -> Any:
        return self._constraints.get(name)

    def list_constraints(self) -> list[str]:
        return self._constraints.list_names()

    def register_vocabulary(self, name: str, vocabulary: Any) -> None:
        self._vocabularies.register(name, vocabulary)

    def get_vocabulary(self, name: str) -> Any:
        return self._vocabularies.get(name)

    def list_vocabularies(self) -> list[str]:
        return self._vocabularies.list_names()
