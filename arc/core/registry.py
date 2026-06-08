from __future__ import annotations

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
        self._scripts = _Slot[dict[str, Any]]("script")
        self._extension_defs = _Slot[dict[str, Any]]("extension_definition")
        self._detectors = _Slot[Any]("detector")
        self._evaluators = _Slot[Any]("evaluator")
        self._loaders = _Slot[Any]("loader")
        self._prompts = _Slot[Any]("prompt")
        self._templates = _Slot[Any]("template")
        self._constraints = _Slot[Any]("constraint")
        self._vocabularies = _Slot[Any]("vocabulary")

        # Audit actions are grouped by lifecycle phase and ordered by
        # priority (design/todo.md item 7). Stored as phase → list of
        # (priority, name, action) kept sorted on insert.
        self._audit_actions: dict[str, list[tuple[int, str, Any]]] = defaultdict(list)
        self._audit_action_names: set[str] = set()

        # Report-section contributors (item 7): packages add sections to the
        # final report without core knowing their names.
        self._report_sections = _Slot[Any]("report_section")

        # Uniform package-source map for *every* package-owned slot, keyed by
        # (kind, name) → package_name. The loader records here as it registers
        # each contribution, so `/package disable` can filter any component
        # type — skills, providers, adapters, evaluators, … — not just the
        # ones with bespoke source tracking (review finding 1). Agents keep
        # their richer per-name source map above and also record here.
        self._component_source: dict[tuple[str, str], str] = {}
        self._load_errors: list[dict[str, str]] = []

    # --- package-source tracking (every slot) ---

    def record_source(self, kind: str, name: str, package_name: str | None) -> None:
        """Record which package contributed ``(kind, name)``.

        ``kind`` is the slot label ("skill", "provider", "adapter", …).
        A ``None`` package_name (a core/built-in registration) records nothing.
        """
        if package_name:
            self._component_source[(kind, name)] = package_name

    def component_source(self, kind: str, name: str) -> str | None:
        return self._component_source.get((kind, name))

    def is_disabled(self, kind: str, name: str, disabled_packages: set[str] | None) -> bool:
        """True when ``(kind, name)`` is owned by a session-disabled package."""
        if not disabled_packages:
            return False
        return self._component_source.get((kind, name)) in disabled_packages

    def filter_disabled(
        self, kind: str, names: list[str], disabled_packages: set[str] | None,
    ) -> list[str]:
        """Drop names owned by a disabled package (for list/UI surfaces)."""
        if not disabled_packages:
            return names
        return [n for n in names if not self.is_disabled(kind, n, disabled_packages)]

    # --- package-load diagnostics ---

    def record_load_error(
        self,
        package_name: str,
        kind: str,
        name: str | None,
        error: str,
    ) -> None:
        """Record a component declaration that failed while loading a package."""
        self._load_errors.append({
            "package": package_name,
            "kind": kind,
            "name": name or "",
            "error": error,
        })

    def list_load_errors(self) -> list[dict[str, str]]:
        return [dict(item) for item in self._load_errors]

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
            self.record_source("agent", name, package_name)
        logger.info("Agent registered: %s%s", name, f" ({package_name})" if package_name else "")

    def get_agent(
        self,
        name: str,
        package_name: str | None = None,
        *,
        disabled_packages: set[str] | None = None,
    ) -> Any:
        """Resolve an agent class by name (or ``package:name``).

        ``disabled_packages`` makes the lookup honour ``/package disable``
        even for agents fetched directly by name — e.g. a workflow step that
        names ``coscientist_supervisor`` outright, bypassing role resolution
        (review finding P2-1). A bare name resolved to a disabled package, or
        an explicit ``package_name`` that's disabled, raises ``KeyError``.
        """
        if package_name:
            package_agents = self._agent_sources.get(name, {})
            if package_name not in package_agents:
                raise KeyError(f"Agent not found: {package_name}:{name}")
            if disabled_packages and package_name in disabled_packages:
                raise KeyError(
                    f"Agent '{package_name}:{name}' belongs to a package "
                    f"disabled for this session"
                )
            return package_agents[package_name]
        if name not in self._agents:
            raise KeyError(f"Agent not found: {name}")
        # Honour disable for both the bare name and the ``package:name`` form.
        if disabled_packages:
            if ":" in name:
                owner = name.split(":", 1)[0]
            else:
                owner = self._agent_source_for_name.get(name) \
                    or self._component_source.get(("agent", name))
            if owner in disabled_packages:
                raise KeyError(
                    f"Agent '{name}' belongs to a package disabled for this session"
                )
        return self._agents[name]

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    def list_agent_sources(self, name: str) -> list[str]:
        return list(self._agent_sources.get(name, {}).keys())

    def agent_source(self, name: str) -> str | None:
        return self._agent_source_for_name.get(name)

    # --- skills ---

    def register_skill(self, name: str, skill: Any, package_name: str | None = None) -> None:
        self._skills.register(name, skill)
        self.record_source("skill", name, package_name)

    def get_skill(self, name: str, disabled_packages: set[str] | None = None) -> Any:
        if self.is_disabled("skill", name, disabled_packages):
            raise KeyError(
                f"Skill '{name}' belongs to a package disabled for this session"
            )
        return self._skills.get(name)

    def list_skills(self, disabled_packages: set[str] | None = None) -> list[str]:
        return self.filter_disabled("skill", self._skills.list_names(), disabled_packages)

    # --- adapters ---

    def register_adapter(self, name: str, adapter: Any, package_name: str | None = None) -> None:
        self._adapters.register(name, adapter)
        self.record_source("adapter", name, package_name)

    def get_adapter(self, name: str, disabled_packages: set[str] | None = None) -> Any:
        if self.is_disabled("adapter", name, disabled_packages):
            raise KeyError(
                f"Adapter '{name}' belongs to a package disabled for this session"
            )
        return self._adapters.get(name)

    def list_adapters(self) -> list[str]:
        return self._adapters.list_names()

    # --- asset loaders ---

    def register_loader(self, name: str, loader: Any, package_name: str | None = None) -> None:
        self._loaders.register(name, loader)
        self.record_source("loader", name, package_name)

    def get_loader(self, name: str, disabled_packages: set[str] | None = None) -> Any:
        if self.is_disabled("loader", name, disabled_packages):
            raise KeyError(
                f"Loader '{name}' belongs to a package disabled for this session"
            )
        return self._loaders.get(name)

    def list_loaders(self, disabled_packages: set[str] | None = None) -> list[str]:
        return self.filter_disabled("loader", self._loaders.list_names(), disabled_packages)

    # --- extensions ---

    def register_extension(self, name: str, extension: Any) -> None:
        self._extensions.register(name, extension)

    def get_extension(self, name: str) -> Any:
        # Historical API: returns None (not raises) for missing extensions.
        return self._extensions.get_or_none(name)

    def register_extension_definition(self, name: str, definition: dict[str, Any]) -> None:
        self._extension_defs.register(name, definition)

    def get_extension_definition(self, name: str) -> dict[str, Any] | None:
        return self._extension_defs.get_or_none(name)

    def list_extension_definitions(self) -> list[str]:
        return self._extension_defs.list_names()

    # --- providers ---

    def register_provider(self, name: str, provider: Any, package_name: str | None = None) -> None:
        self._providers.register(name, provider)
        self.record_source("provider", name, package_name)

    def get_provider(self, name: str, disabled_packages: set[str] | None = None) -> Any:
        if self.is_disabled("provider", name, disabled_packages):
            raise KeyError(
                f"Provider '{name}' belongs to a package disabled for this session"
            )
        return self._providers.get(name)

    def list_providers(self) -> list[str]:
        return self._providers.list_names()

    # --- workflows ---

    def register_workflow(self, name: str, definition: dict, package_name: str | None = None) -> None:
        self._workflows.register(name, definition)
        self.record_source("workflow", name, package_name)

    def get_workflow(self, name: str) -> dict:
        return self._workflows.get(name)

    def list_workflows(self) -> list[str]:
        return self._workflows.list_names()

    # --- package scripts ---

    def register_script(
        self,
        name: str,
        script: dict[str, Any],
        package_name: str | None = None,
    ) -> None:
        self._scripts.register(name, script)
        self.record_source("script", name, package_name)

    def get_script(self, name: str, disabled_packages: set[str] | None = None) -> dict[str, Any]:
        if self.is_disabled("script", name, disabled_packages):
            raise KeyError(
                f"Script '{name}' belongs to a package disabled for this session"
            )
        return self._scripts.get(name)

    def list_scripts(self, disabled_packages: set[str] | None = None) -> list[str]:
        return self.filter_disabled("script", self._scripts.list_names(), disabled_packages)

    # --- package resources ---

    def register_detector(self, name: str, detector: Any) -> None:
        self._detectors.register(name, detector)

    def get_detector(self, name: str) -> Any:
        return self._detectors.get(name)

    def list_detectors(self) -> list[str]:
        return self._detectors.list_names()

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

    # --- audit actions (grouped by phase, ordered by priority) ---

    def register_audit_action(self, action: Any, package_name: str | None = None) -> None:
        """Register an :class:`AuditActionContract` under its declared phase.

        Actions are kept sorted by ``priority`` (ascending) within a phase so
        the dispatcher runs them in a deterministic order. Re-registering the
        same name replaces the prior entry. ``package_name`` is stamped onto
        the action as ``_arc_package`` so a disabled package's audits can be
        filtered out at dispatch time (todo.md item 7 / review finding A).
        """
        phase = getattr(action, "phase", None)
        name = getattr(action, "name", None)
        if not phase or not name:
            logger.warning("Audit action missing name/phase — skipping: %r", action)
            return
        if package_name is not None:
            try:
                action._arc_package = package_name  # noqa: SLF001
            except Exception:  # noqa: BLE001 — some objects forbid attribute set
                pass
        priority = int(getattr(action, "priority", 100))
        bucket = self._audit_actions[phase]
        # Replace any existing entry with the same name.
        bucket[:] = [e for e in bucket if e[1] != name]
        bucket.append((priority, name, action))
        bucket.sort(key=lambda e: (e[0], e[1]))
        self._audit_action_names.add(name)
        logger.info("Audit action registered: %s (phase=%s, priority=%s)", name, phase, priority)

    def audit_actions_for_phase(
        self, phase: str, disabled_packages: set[str] | None = None,
    ) -> list[Any]:
        """Return the actions registered for ``phase``, priority order.

        Actions whose source package is in ``disabled_packages`` are excluded
        (review finding A).
        """
        disabled = disabled_packages or set()
        return [
            action for _prio, _name, action in self._audit_actions.get(phase, [])
            if getattr(action, "_arc_package", None) not in disabled
        ]

    def list_audit_actions(self) -> list[str]:
        return sorted(self._audit_action_names)

    # --- report sections ---

    def register_report_section(
        self, name: str, contributor: Any, package_name: str | None = None,
    ) -> None:
        if package_name is not None:
            try:
                contributor._arc_package = package_name  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
        self._report_sections.register(name, contributor)

    def list_report_sections(self) -> list[str]:
        return self._report_sections.list_names()

    def report_section_contributors(
        self, disabled_packages: set[str] | None = None,
    ) -> list[Any]:
        """Every registered report-section contributor (item 7).

        Contributors from a disabled package are excluded (review finding A).
        """
        disabled = disabled_packages or set()
        return [
            c for c in (self._report_sections.get(n) for n in self._report_sections.list_names())
            if getattr(c, "_arc_package", None) not in disabled
        ]

    def scoped(self, package_name: str | None) -> "PackageScopedRegistry":
        """Return a proxy that attributes every registration to ``package_name``.

        Used by the kernel when initializing an extension so the components
        the extension registers (skills, adapters, audit actions, …) inherit
        the extension's package as their source — keeping ``/package disable``
        effective for extension-created components (review finding P2-2).
        """
        return PackageScopedRegistry(self, package_name)


class PackageScopedRegistry:
    """Registry proxy that stamps a package source on every registration.

    Delegates all reads to the wrapped registry. For ``register_*`` methods it
    forwards the call, passing ``package_name`` when the method accepts it and
    otherwise recording the source afterward by inspecting what newly appeared
    in the matching slot. This lets extension code keep calling the plain
    ``registry.register_skill(name, skill)`` API while still attributing the
    result to the extension's package (review finding P2-2).
    """

    # register method → (kind, lister) for methods that DON'T take package_name.
    _SOURCE_ONLY = {
        "register_evaluator": ("evaluator", "list_evaluators"),
        "register_detector": ("detector", "list_detectors"),
        "register_prompt": ("prompt", "list_prompts"),
        "register_template": ("template", "list_templates"),
        "register_constraint": ("constraint", "list_constraints"),
        "register_vocabulary": ("vocabulary", "list_vocabularies"),
    }
    # register methods that accept a package_name keyword directly.
    _PACKAGE_AWARE = {
        "register_agent", "register_skill", "register_adapter",
        "register_provider", "register_loader", "register_workflow", "register_audit_action",
        "register_report_section", "register_script",
    }

    def __init__(self, registry: ComponentRegistry, package_name: str | None):
        self._registry = registry
        self._package_name = package_name

    def __getattr__(self, attr: str) -> Any:
        target = getattr(self._registry, attr)
        if self._package_name is None or not attr.startswith("register_"):
            return target

        if attr in self._PACKAGE_AWARE:
            def _wrapped(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("package_name", self._package_name)
                return target(*args, **kwargs)
            return _wrapped

        if attr in self._SOURCE_ONLY:
            kind, lister = self._SOURCE_ONLY[attr]

            def _wrapped_source(*args: Any, **kwargs: Any) -> Any:
                before = set(getattr(self._registry, lister)())
                result = target(*args, **kwargs)
                after = getattr(self._registry, lister)()
                for name in after:
                    if name not in before:
                        self._registry.record_source(kind, name, self._package_name)
                return result
            return _wrapped_source

        # register_extension / register_extension_definition / register_package
        # have no per-session disable semantics — forward unchanged.
        return target
