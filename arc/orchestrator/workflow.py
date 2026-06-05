"""Core research workflow orchestrator.

Wires together agents, adapter, registry, and stores for a single iteration.
The provider is optional — without it, agents use deterministic stub logic.
"""

import logging
import os
from pathlib import Path
from typing import Any

from arc.assets.files import FileAsset
from arc.contracts.agent import AgentContext
from arc.core.config import filter_package_paths, load_arc_toml, resolve_package_paths
from arc.core.loader import load_packages
from arc.core.registry import ComponentRegistry
from arc.memory.artifact_registry import ArtifactRegistry
from arc.memory.provenance import ProvenanceLog
from arc.memory.results_store import ResultsStore
from arc.schemas.artifact import ArtifactDraft, ArtifactRecord, ValidationResult
from arc.schemas.execution import ExecutionResult
from arc.schemas.research import ResearchGoal
from arc.schemas.review import ReviewResult
from arc.session import session_paths
from arc.runtime.backend import safe_backend_action


def _build_adapter(
    db_path: str | None = None,
    session_id: str | None = None,
    registry: ComponentRegistry | None = None,
    disabled_packages: set[str] | None = None,
):
    """Build the runtime adapter requested by ARC_RUNTIME_ADAPTER.

    ``disabled_packages`` (the session ``/package disable`` set) makes the
    package-owned adapter lookup honour disable: a default adapter selected by
    ``ARC_RUNTIME_ADAPTER`` whose package is disabled falls through to the
    built-in local adapter (review finding P3). Core adapters (local/sim2l/
    service) are never package-owned, so they're unaffected.
    """
    adapter_name = os.environ.get("ARC_RUNTIME_ADAPTER", "local").lower()
    if adapter_name in {"local", "python"}:
        from arc.runtime.local import LocalRuntimeAdapter
        logger.info("Using LocalRuntimeAdapter")
        return LocalRuntimeAdapter()
    if adapter_name in {"sim2l", "sim2l-local"}:
        from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
        logger.info("Using Sim2LRuntimeAdapter")
        return Sim2LRuntimeAdapter(db_path=db_path, session_id=session_id)
    if adapter_name in {"service", "services", "sim2l-service", "sim2l-services"}:
        os.environ.setdefault("ARC_STORAGE_MODE", "required")
        from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
        logger.info("Using Sim2LRuntimeAdapter with required service persistence")
        return Sim2LRuntimeAdapter(db_path=db_path, session_id=session_id)

    if adapter_name in {"auto"}:
        return _build_auto_adapter(db_path=db_path, session_id=session_id)

    if registry is None:
        try:
            registry = _default_registry()
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not build registry for adapter lookup: %s", exc)

    if registry is not None:
        try:
            cls = registry.get_adapter(adapter_name, disabled_packages=disabled_packages)
            logger.info("Using %s", cls.__name__)
            return _instantiate_adapter(cls, db_path=db_path, session_id=session_id)
        except KeyError:
            # Either unknown, or owned by a session-disabled package — both
            # fall through to the local adapter below.
            pass
        except TypeError:
            # Registry without the disabled_packages-aware signature.
            try:
                cls = registry.get_adapter(adapter_name)
                logger.info("Using %s", cls.__name__)
                return _instantiate_adapter(cls, db_path=db_path, session_id=session_id)
            except KeyError:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not instantiate %s adapter (%s); falling back to local",
                           adapter_name, exc)

    logger.warning("Unknown ARC_RUNTIME_ADAPTER=%s; falling back to local", adapter_name)
    from arc.runtime.local import LocalRuntimeAdapter
    return LocalRuntimeAdapter()


def _build_auto_adapter(db_path: str | None = None, session_id: str | None = None):
    """Auto-select Sim2LRuntimeAdapter when sim2l is importable, else fall back to local.

    Used by ``ARC_RUNTIME_ADAPTER=auto``. Useful for environments where the
    same code runs both with and without sim2l installed (CI, test fixtures,
    light demos). Prefer an explicit ``local``/``sim2l``/``service`` value
    in production deployments so the chosen runtime is unambiguous.
    """
    try:
        import sim2l  # noqa: F401
        from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
        logger.info("Using Sim2LRuntimeAdapter (auto-detected)")
        return Sim2LRuntimeAdapter(db_path=db_path, session_id=session_id)
    except ImportError:
        from arc.runtime.local import LocalRuntimeAdapter
        logger.info("sim2l not found — using LocalRuntimeAdapter (auto fallback)")
        return LocalRuntimeAdapter()


def _instantiate_adapter(adapter_class, db_path: str | None = None, session_id: str | None = None):
    """Instantiate an adapter class while tolerating smaller constructor signatures."""
    try:
        return adapter_class(db_path=db_path, session_id=session_id)
    except TypeError:
        try:
            return adapter_class(session_id=session_id)
        except TypeError:
            return adapter_class()

logger = logging.getLogger(__name__)


def _default_registry() -> ComponentRegistry:
    """Build the registry used when a caller doesn't inject one.

    Loads the same ``arc.toml`` the kernel uses (cached via
    ``arc.core.config.load_arc_toml``), resolves its package paths, applies
    the shared ``[packages].enabled/disabled`` filter, and loads the
    survivors. This used to skip filtering — so a package disabled in
    ``arc.toml`` was still active on the CLI/UI/test path that instantiates a
    ``ResearchWorkflow``. Sharing ``filter_package_paths`` with ``Kernel``
    keeps both runtime paths loading an identical package set (todo.md item 3).
    """
    from arc.core.env import load_env
    load_env()  # populate os.environ from .env before packages read it
    registry = ComponentRegistry()
    config_path, config = load_arc_toml()
    package_paths = resolve_package_paths(config, config_path) if config else []
    package_config = config.get("packages", {}) if config else {}
    load_packages(filter_package_paths(package_paths, package_config), registry)
    return registry


def _resolve_package_config(registry: ComponentRegistry) -> dict[str, Any]:
    """Merge every loaded package's declared config into one dict.

    Agents read ``self.context.config[VAR]`` for vars their package
    declared in ``package.yaml``'s ``config:`` section, instead of reaching
    into ``os.environ`` with magic strings. Later packages win on a key
    collision (rare — config vars are namespaced by convention, e.g.
    ``ARC_CODEX_*``).
    """
    merged: dict[str, Any] = {}
    try:
        for pkg in registry.list_packages():
            merged.update(registry.package_config(pkg))
    except Exception as exc:  # noqa: BLE001 — config resolution must not break init
        logger.debug("package config resolution failed: %s", exc)
    return merged


def _build_provider(
    provider_name: str | None = None,
    token: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    registry: Any = None,
    disabled_packages: set[str] | None = None,
):
    """Resolve the LLM provider through the package-aware factory.

    Core ships only ``openwebui``; anthropic/openai (and any third-party
    provider) come from a package's ``provides.providers`` and are looked
    up on ``registry``. A provider from a session-disabled package is not
    selectable (review finding 1). Returns ``None`` (stub mode) when
    unset/unknown.
    """
    from arc.providers import build_provider
    name = provider_name or os.environ.get("ARC_PROVIDER", "")
    return build_provider(
        name, token=token, model=model, base_url=base_url, registry=registry,
        disabled_packages=disabled_packages,
    )


class ResearchWorkflow:
    def __init__(
        self,
        provider_name: str | None = None,
        token: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        session_id: str | None = None,
        workflow_name: str = "research-loop",
        registry: ComponentRegistry | None = None,
    ):
        from arc.session import new_session_id as _new_sid
        self.session_id = session_id or _new_sid()
        self.workflow_name = workflow_name

        # All session files always go to ~/.sim2l/code/<session_id>/.
        paths = session_paths(self.session_id)
        artifact_root   = paths["artifacts"]
        results_root    = paths["runs"]
        provenance_path = paths["provenance"]
        db_path         = paths["db"]
        self._db_path = db_path

        self.registry = registry or _default_registry()
        # Retained so the provider/adapter can be rebuilt if a provider/adapter
        # package is disabled mid-session (review finding P2).
        self._provider_build_args = {
            "provider_name": provider_name, "token": token,
            "model": model, "base_url": base_url,
        }
        self.adapter = _build_adapter(
            db_path=db_path, session_id=self.session_id, registry=self.registry,
            disabled_packages=self._disabled_packages(),
        )
        # The backend handles the loop's publish actions (register /
        # persist / record). Resolves to a sim2l backend when sim2l is
        # active + the adapter supports it, else a silent no-op so ARC
        # runs fully local with no shared persistence. See
        # arc/runtime/backend.py and design/architecture.md.
        from arc.runtime.backend import resolve_backend
        self.backend = resolve_backend(self.adapter)
        self.artifacts = ArtifactRegistry(root=artifact_root)
        self.results = ResultsStore(root=results_root)
        self.provenance = ProvenanceLog(log_path=provenance_path)
        from arc.assets.input_scan import scan_inputs_from_env
        from arc.assets.session import session_file_store
        self.file_store = session_file_store(self.session_id)
        self._register_default_loaders()
        self.input_assets = scan_inputs_from_env(self.file_store, session_id=self.session_id)
        self.provider = _build_provider(
            provider_name=provider_name,
            token=token,
            model=model,
            base_url=base_url,
            registry=self.registry,
            disabled_packages=self._disabled_packages(),
        )

        # Wire the optional vector-memory + knowledge-graph extensions into
        # the loop. A clean no-op when neither extension is enabled.
        from arc.memory.hooks import MemoryHooks
        self.memory_hooks = MemoryHooks(self.registry, self.session_id)

        self._context = AgentContext(
            session_id=self.session_id,
            config=_resolve_package_config(self.registry),
            memory={
                "provider": self.provider,
                "registry": self.artifacts,
                "results": self.results,
                "provenance": self.provenance,
                "files": self.file_store,
                "file_store": self.file_store,
                "input_assets": self.input_assets,
                "adapter": self.adapter,
                # The component registry (distinct from the artifact registry
                # above) so audit actions can reach extensions/strategies.
                "component_registry": self.registry,
                # Agents query indexed memory through these without needing
                # to know whether the extensions are enabled (search returns
                # [] when disabled). See arc/memory/hooks.py.
                "memory_hooks": self.memory_hooks,
                "memory_search": self.memory_hooks.search,
            },
        )

        # Package-provided audit hooks fire at lifecycle phases (item 7). A
        # clean no-op when no package registered any audit action.
        from arc.runtime.audit import AuditDispatcher
        self.audit = AuditDispatcher(self.registry, self._context, self.provenance)

    def _register_default_loaders(self) -> None:
        """Make core asset loaders available in every workflow session."""
        try:
            from arc.assets.loaders import DEFAULT_LOADERS
            existing = set(self.registry.list_loaders())
            for loader in DEFAULT_LOADERS:
                if loader.name not in existing:
                    self.registry.register_loader(loader.name, loader)
                    existing.add(loader.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Default asset loader registration failed: %s", exc)

    def refresh_disabled_packages(self) -> None:
        """Re-resolve the provider + runtime adapter against the current
        session ``/package disable`` set (review finding P2).

        The provider and default adapter are built at construction, *before*
        session-disabled state is hydrated, so a package disabled mid-session
        would otherwise keep a live provider/adapter instance from that
        package. Chat/UI ``/package disable`` handlers call this after updating
        session state so the references are rebuilt (dropping a now-disabled
        package's provider/adapter back to stub/local). Safe to call any time;
        a no-op when nothing changes.
        """
        disabled = self._disabled_packages()

        # Rebuild the provider — a disabled provider package degrades to stub
        # mode (None). Keep context.memory["provider"] in sync.
        self.provider = _build_provider(
            registry=self.registry,
            disabled_packages=disabled,
            **self._provider_build_args,
        )
        self._context.memory["provider"] = self.provider

        # Rebuild the runtime adapter — a disabled adapter package degrades to
        # the local adapter. The backend is derived from the adapter, so
        # rebuild it too.
        self.adapter = _build_adapter(
            db_path=self._db_path, session_id=self.session_id, registry=self.registry,
            disabled_packages=disabled,
        )
        self._context.memory["adapter"] = self.adapter
        from arc.runtime.backend import resolve_backend
        self.backend = resolve_backend(self.adapter)

    def _agent(self, agent_class):
        return agent_class(context=self._context)

    def _disabled_packages(self) -> set[str]:
        """Session ``/package disable`` set — a runtime filter for every
        package-owned component the workflow resolves (review finding 1)."""
        try:
            return set(
                (self._context.memory.get("packages", {}) or {}).get("disabled", []) or []
            )
        except AttributeError:
            return set()

    def _resolve_agent_class(self, name: str):
        """Resolve a workflow step's ``agent:`` name to a class.

        For names that are strategy *roles* (ideator, planner, reviewer,
        reflector, …) this routes through the strategy resolver so the
        YAML workflow engine honours the same precedence the chat loop
        does — per-session ``/strategy`` overrides + applied recipes
        (``memory["strategy_overrides"]``), the ``ARC_STRATEGY_<ROLE>``
        env var, the ``arc.toml [strategies]`` block, then the catalogue
        default. Without this the YAML path silently ran the default
        agent regardless of overrides (TODO item 7).

        Any name that is not a known role (e.g. a package-specific agent
        like ``experiment_decomposer``, or an explicit ``package:agent``
        form) falls back to the registry lookup. The registry is also the
        fallback if the resolver raises, so a malformed override can never
        make a previously-runnable workflow unrunnable.
        """
        from arc.core.strategies import known_roles, resolve_role

        if name in known_roles():
            overrides = None
            disabled_packages = self._disabled_packages()
            try:
                overrides = self._context.memory.get("strategy_overrides") or None
            except AttributeError:
                overrides = None
            try:
                _path, config = load_arc_toml()
            except Exception:
                config = {}
            try:
                return resolve_role(
                    name, overrides=overrides, config=config,
                    disabled_packages=disabled_packages,
                    loaded_packages=set(self.registry.list_packages()),
                )
            except Exception as exc:
                logger.warning(
                    "resolve_role(%r) failed (%s) — falling back to registry",
                    name, exc,
                )
        # Non-role / explicit package agents (e.g. ``coscientist_supervisor``,
        # ``arc-coscientist:supervisor``) still honour /package disable — a
        # disabled package's agent can't be run directly (review finding P2-1).
        return self.registry.get_agent(name, disabled_packages=self._disabled_packages())

    def _dump(self, value):
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if isinstance(value, dict):
            return {k: self._dump(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._dump(v) for v in value]
        return value

    def _resolve_ref(self, ref, state: dict, workflow_config: dict):
        if isinstance(ref, dict):
            return {k: self._resolve_ref(v, state, workflow_config) for k, v in ref.items()}
        if isinstance(ref, list):
            return [self._resolve_ref(v, state, workflow_config) for v in ref]
        if not isinstance(ref, str):
            return ref
        if ref == "user_goal":
            return state["user_goal"]
        roots = {
            "memory": self._context.memory,
            "context": self._context.memory,
            "config": workflow_config,
            "inputs": state.get("inputs", {}),
            "steps": state.get("steps", {}),
        }
        parts = ref.split(".")
        if len(parts) >= 2 and parts[0] in state["steps"] and parts[1] == "output":
            value = state["steps"][parts[0]]["output"]
            for part in parts[2:]:
                if value is None:
                    break  # broken intermediate — stop walking, resolve to None
                value = self._get_field(value, part)
            return value
        if parts[0] in roots:
            value = roots[parts[0]]
            for part in parts[1:]:
                if value is None:
                    break
                value = self._get_field(value, part)
            return value
        return ref

    def _bind_workflow_inputs(self, workflow: dict, goal: ResearchGoal) -> dict[str, Any]:
        """Bind workflow-level ``inputs:`` declarations from the research goal.

        Scalar inputs come from goal fields/constraints/target/defaults. File
        inputs additionally resolve paths or ``file_*`` IDs into FileAssets,
        auto-bind a single matching session asset, and create required
        derivatives lazily through the registered loader pipeline.
        """
        schema = workflow.get("inputs") or {}
        goal_data = goal.model_dump() if hasattr(goal, "model_dump") else dict(goal)
        constraints = goal_data.get("constraints") or {}
        target = goal_data.get("target") or {}

        # Always expose the goal's first-class fields, constraints, and target
        # values. Explicit schema entries below can add defaults/required checks.
        bound: dict[str, Any] = {
            "goal": goal_data.get("goal"),
            "domain": goal_data.get("domain"),
            "mode": goal_data.get("mode"),
            "constraints": constraints,
            "target": target,
            **target,
            **constraints,
        }

        if not isinstance(schema, dict):
            return bound

        missing: list[str] = []
        for name, spec in schema.items():
            spec = spec or {}
            if not isinstance(spec, dict):
                spec = {"type": spec}
            raw = self._lookup_declared_input(name, spec, goal_data, constraints, target)
            if spec.get("type") == "file":
                if raw is None and "default" in spec:
                    raw = spec["default"]
                try:
                    asset = self._bind_file_input(name, raw, spec)
                except KeyError:
                    if spec.get("required"):
                        missing.append(name)
                    continue
                bound[name] = asset.id
                bound[f"{name}_asset"] = asset.to_dict()
                derived = self._ensure_required_derivatives(asset, spec)
                if derived:
                    bound[f"{name}_derivatives"] = {
                        role: item.to_dict() for role, item in derived.items()
                    }
                    for role, item in derived.items():
                        bound[f"{name}_{role}"] = item.id
                        if role == "extracted_text":
                            bound[f"{name}_text"] = item.id
                continue

            if raw is not None:
                bound[name] = raw
            elif "default" in spec:
                bound[name] = spec["default"]
            elif spec.get("required"):
                missing.append(name)
        if missing:
            raise ValueError(
                "Missing required workflow input(s): " + ", ".join(sorted(missing))
            )
        return bound

    def _lookup_declared_input(
        self,
        name: str,
        spec: dict[str, Any],
        goal_data: dict[str, Any],
        constraints: dict[str, Any],
        target: dict[str, Any],
    ) -> Any:
        aliases = [name]
        aliases.extend(str(alias) for alias in (spec.get("aliases") or ()))
        source = spec.get("source")
        if isinstance(source, str):
            aliases.append(source)
        for key in aliases:
            if key in constraints:
                return constraints[key]
            if key in target:
                return target[key]
            if key in goal_data:
                return goal_data[key]
        return None

    def _bind_file_input(self, name: str, raw: Any, spec: dict[str, Any]) -> FileAsset:
        asset = self._file_asset_from_value(raw, spec)
        if asset is None:
            matches = self._matching_session_assets(spec)
            if len(matches) == 1:
                asset = matches[0]
            elif len(matches) > 1:
                choices = ", ".join(f"{a.id} ({a.name})" for a in matches)
                raise ValueError(
                    f"Ambiguous workflow file input {name!r}; matching files: {choices}"
                )
            else:
                raise KeyError(name)
        self._validate_file_asset(name, asset, spec)
        return asset

    def _file_asset_from_value(self, value: Any, spec: dict[str, Any]) -> FileAsset | None:
        if value is None:
            return None
        if isinstance(value, FileAsset):
            return value
        if isinstance(value, dict):
            for key in ("file_id", "id", "asset_id"):
                if value.get(key):
                    return self.file_store.get(str(value[key]))
            if value.get("path"):
                return self.file_store.import_file(
                    value["path"],
                    role=value.get("role") or spec.get("role"),
                    session_id=self.session_id,
                    metadata={"source": "workflow_input"},
                    copy=True,
                )
            return None
        if isinstance(value, str):
            if value.startswith("file_"):
                return self.file_store.get(value)
            path = Path(value).expanduser()
            if path.exists():
                return self.file_store.import_file(
                    path,
                    role=spec.get("role"),
                    session_id=self.session_id,
                    metadata={"source": "workflow_input"},
                    copy=True,
                )
        return None

    def _matching_session_assets(self, spec: dict[str, Any]) -> list[FileAsset]:
        role = spec.get("role")
        media_type = spec.get("media_type")
        assets = self.file_store.list(session_id=self.session_id)
        out = []
        for asset in assets:
            if asset.derived_from:
                continue
            if role and asset.role != role:
                continue
            if media_type and not self._media_type_matches(asset.media_type, media_type):
                continue
            out.append(asset)
        return out

    def _validate_file_asset(self, name: str, asset: FileAsset, spec: dict[str, Any]) -> None:
        role = spec.get("role")
        media_type = spec.get("media_type")
        if role and asset.role != role:
            raise ValueError(
                f"Workflow file input {name!r} requires role {role!r}; "
                f"{asset.id} has role {asset.role!r}"
            )
        if media_type and not self._media_type_matches(asset.media_type, media_type):
            raise ValueError(
                f"Workflow file input {name!r} requires media type {media_type!r}; "
                f"{asset.id} has media type {asset.media_type!r}"
            )

    def _ensure_required_derivatives(
        self,
        asset: FileAsset,
        spec: dict[str, Any],
    ) -> dict[str, FileAsset]:
        required = spec.get("required_derivatives") or []
        out: dict[str, FileAsset] = {}
        for derivative_spec in required:
            if not isinstance(derivative_spec, dict):
                derivative_spec = {"role": str(derivative_spec)}
            role = derivative_spec.get("role")
            if not role:
                continue
            found = self._find_derivative(asset, derivative_spec)
            if found is None:
                self._run_loader_for_asset(asset, derivative_spec)
                found = self._find_derivative(asset, derivative_spec)
            if found is None:
                raise ValueError(
                    f"No loader produced required derivative role {role!r} "
                    f"for file input {asset.id}"
                )
            out[str(role)] = found
        return out

    def _find_derivative(
        self,
        asset: FileAsset,
        derivative_spec: dict[str, Any],
    ) -> FileAsset | None:
        role = derivative_spec.get("role")
        media_type = derivative_spec.get("media_type")
        for candidate in self.file_store.list(
            session_id=self.session_id,
            derived_from=asset.id,
            role=role,
        ):
            if media_type and not self._media_type_matches(candidate.media_type, media_type):
                continue
            return candidate
        return None

    def _run_loader_for_asset(
        self,
        asset: FileAsset,
        derivative_spec: dict[str, Any] | None = None,
    ) -> list[FileAsset]:
        loader_name = (derivative_spec or {}).get("loader")
        if loader_name:
            loaders = [(loader_name, self.registry.get_loader(
                loader_name, disabled_packages=self._disabled_packages(),
            ))]
        else:
            loaders = [
                (name, self.registry.get_loader(name, disabled_packages=self._disabled_packages()))
                for name in self.registry.list_loaders(disabled_packages=self._disabled_packages())
            ]

        from arc.assets.loaders import LoaderContext
        context = LoaderContext(
            file_store=self.file_store,
            workspace=Path(self.file_store.root),
            session_id=self.session_id,
            config=_resolve_package_config(self.registry),
        )
        for name, loader_obj in loaders:
            loader = self._loader_instance(loader_obj)
            can_load = getattr(loader, "can_load", None)
            if callable(can_load) and not can_load(asset):
                continue
            load = getattr(loader, "load", None)
            if not callable(load):
                continue
            produced = list(load(asset, context))
            package_name = self.registry.component_source("loader", name)
            if package_name:
                produced = [
                    self.file_store.update_metadata(item.id, {"package_name": package_name})
                    for item in produced
                ]
            return produced
        raise ValueError(f"No enabled loader can load file asset {asset.id} ({asset.name})")

    def _loader_instance(self, loader_obj: Any) -> Any:
        if isinstance(loader_obj, type):
            return loader_obj()
        return loader_obj

    def load_file_asset(self, file_id: str, loader: str | None = None) -> list[FileAsset]:
        """Run an enabled loader for a session file and return derived assets."""
        asset = self.file_store.get(file_id)
        derivative_spec = {"loader": loader} if loader else None
        return self._run_loader_for_asset(asset, derivative_spec)

    def _media_type_matches(self, actual: str | None, expected: str | list[str]) -> bool:
        if isinstance(expected, list):
            return any(self._media_type_matches(actual, item) for item in expected)
        if not expected:
            return True
        if actual == expected:
            return True
        if isinstance(expected, str) and expected.endswith("/*"):
            return bool(actual and actual.startswith(expected[:-1]))
        return False

    def _get_field(self, value, field: str):
        """Look up ``field`` on ``value`` for workflow-YAML reference resolution.

        Dict access is unrestricted (callers control their own payload), but
        attribute access is filtered: leading-underscore names — including
        every dunder — are rejected. This stops a malicious workflow YAML
        from walking the Python object graph via references like
        ``review.output.__class__.__init__.__globals__`` (review item #A9).
        """
        if isinstance(value, dict):
            return value.get(field)
        if not isinstance(field, str) or field.startswith("_"):
            return None
        return getattr(value, field, None)

    # Operators must be ordered longest-first so two-character forms (>=, <=, !=, ==)
    # are matched before single-character (>, <).
    _CONDITION_OPERATORS = ("==", "!=", ">=", "<=", ">", "<")

    @staticmethod
    def _parse_condition_literal(text: str) -> Any:
        """Parse the right-hand side of a workflow condition into a Python value.

        Accepts: ``true``/``false`` (case-insensitive), integers, floats, and
        single- or double-quoted strings. Bare identifiers are returned as
        strings so legacy ``foo == bar`` style still works.
        """
        text = text.strip()
        lower = text.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        if (text.startswith("'") and text.endswith("'")) or (
            text.startswith('"') and text.endswith('"')
        ):
            return text[1:-1]
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
        return text  # bare identifier — treat as string literal

    def _condition_matches(self, expression: str, state: dict) -> bool:
        """Evaluate a workflow ``if:`` expression like ``review.output.approved == false``.

        Supports the operators listed in ``_CONDITION_OPERATORS``. Raises
        ``ValueError`` on unsupported syntax rather than silently returning
        False, so workflow authors get a clear error instead of a confusing
        no-op.
        """
        expression = (expression or "").strip()
        if not expression:
            return False

        # Pick the first operator that appears. Longer operators come first in
        # _CONDITION_OPERATORS so ``>=`` is matched before ``>``.
        for op in self._CONDITION_OPERATORS:
            idx = expression.find(op)
            if idx >= 0:
                left = expression[:idx].strip()
                right = expression[idx + len(op):].strip()
                break
        else:
            raise ValueError(
                f"Unsupported workflow condition: {expression!r}. "
                f"Use one of {', '.join(self._CONDITION_OPERATORS)}."
            )

        lhs = self._resolve_ref(left, state, {})
        rhs = self._parse_condition_literal(right)

        # Booleans: coerce the LHS to a bool (so ``approved == false`` works
        # whether the resolved value is None or False) and compare by value.
        if isinstance(rhs, bool):
            lhs_bool = bool(lhs)
            return (lhs_bool == rhs) if op == "==" else (lhs_bool != rhs)
        if op == "==":
            return lhs == rhs or str(lhs) == str(rhs)
        if op == "!=":
            return not (lhs == rhs or str(lhs) == str(rhs))
        # Ordering operators are numeric only. Raise on mismatched types so
        # workflow authors see a configuration bug instead of a silently skipped
        # branch.
        try:
            lhs_num = float(lhs)  # type: ignore[arg-type]
            rhs_num = float(rhs)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Workflow condition {expression!r} uses ordering operator "
                f"{op!r} with non-numeric operands: {lhs!r}, {rhs!r}"
            ) from exc
        if op == ">":
            return lhs_num > rhs_num
        if op == "<":
            return lhs_num < rhs_num
        if op == ">=":
            return lhs_num >= rhs_num
        if op == "<=":
            return lhs_num <= rhs_num
        return False

    async def _execute_skill(self, name: str, input_data, state: dict):
        if name == "validate-sim2l":
            artifact = input_data
            if isinstance(input_data, ArtifactDraft):
                artifact = self.artifacts.register(input_data)
                self._context.memory["current_artifact"] = artifact
            validation = await self.adapter.validate_artifact(artifact)
            if not validation.valid:
                raise ValueError("; ".join(validation.errors))
            return artifact
        if name == "write-artifact":
            if isinstance(input_data, ArtifactDraft):
                artifact = self.artifacts.register(input_data)
                self._context.memory["current_artifact"] = artifact
                self.memory_hooks.on_artifact_registered(artifact)
                state["backend_register"] = await safe_backend_action(
                    self.backend, "register_artifact", artifact,
                )
                return artifact
            if isinstance(input_data, ArtifactRecord):
                self._context.memory["current_artifact"] = input_data
                self.memory_hooks.on_artifact_registered(input_data)
                state["backend_register"] = await safe_backend_action(
                    self.backend, "register_artifact", input_data,
                )
            return input_data
        if name == "improve-artifact":
            return {
                "status": "skipped",
                "reason": "No built-in improve implementation",
                "input": self._dump(input_data),
            }
        # A skill from a session-disabled package is not executable
        # (review finding 1) — get_skill raises KeyError in that case.
        skill = self.registry.get_skill(name, disabled_packages=self._disabled_packages())
        return await skill.execute(
            input_data if isinstance(input_data, dict) else {"input": self._dump(input_data)},
            self._context,
        )

    # Methods that workflow YAML files are allowed to invoke on an adapter.
    # Any other ``method:`` value in a step is rejected — this keeps a
    # malicious / typo'd workflow file from calling private helpers (eg.
    # ``_get_repo``, ``_push_to_catalog``) via ``getattr(adapter, …)``.
    # Review item #A6.
    _ALLOWED_ADAPTER_METHODS = frozenset({
        "run",
        "run_sweep",
        "validate_artifact",
        "prepare_inputs",
        "get_status",
        "collect_outputs",
        "collect_logs",
        "collect_metrics",
        "register_artifact",
    })

    async def _execute_workflow_step(self, step: dict, state: dict, workflow_config: dict):
        input_data = self._resolve_ref(step.get("input", "user_goal"), state, workflow_config)
        if "agent" in step:
            agent = self._agent(self._resolve_agent_class(step["agent"]))
            if step["agent"] == "reflector" and "run" in state["steps"]:
                return await agent.run(input_data, execution=state["steps"]["run"]["output"])
            output = await agent.run(input_data)
            if step["agent"] == "reviewer" and isinstance(output, ReviewResult):
                run_output = self._get_field(state["steps"].get("run", {}), "output")
                run_id = self._get_field(run_output, "run_id")
                self.memory_hooks.on_review_completed(
                    self._context.memory.get("current_artifact"), output, run_id,
                )
            return output
        if "skill" in step:
            return await self._execute_skill(step["skill"], input_data, state)
        if "script" in step:
            from arc.runtime.package_scripts import PackageScriptRunner
            script_input = input_data if isinstance(input_data, dict) else {}
            workspace = script_input.get("cwd")
            if not workspace:
                base = Path(session_paths(self.session_id)["artifacts"]).parent
                workspace = base / "workspaces" / "scripts" / str(step.get("id", "script"))
            runner = PackageScriptRunner(
                self.registry,
                self.file_store,
                session_id=self.session_id,
            )
            result = runner.run(
                step["script"],
                args=list(script_input.get("args", []) or []),
                cwd=workspace,
                timeout_s=int(step.get("timeout_s", 60) or 60),
                disabled_packages=self._disabled_packages(),
                source_asset_id=script_input.get("source_asset_id"),
            )
            return {
                "name": result.name,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "generated_assets": [asset.to_dict() for asset in result.generated_assets],
            }
        if "adapter" in step:
            method_name = step.get("method", "run")
            if method_name not in self._ALLOWED_ADAPTER_METHODS:
                allowed = ", ".join(sorted(self._ALLOWED_ADAPTER_METHODS))
                raise ValueError(
                    f"Workflow step requested disallowed adapter method "
                    f"{method_name!r}. Allowed methods: {allowed}."
                )
            adapter = self._adapter_for_step(step)
            method = getattr(adapter, method_name)
            if isinstance(input_data, dict) and "artifact" in input_data:
                artifact = input_data["artifact"]
                parameters = input_data.get("parameters", {})
                prepared = await adapter.prepare_inputs(artifact, parameters)
                state["prepared_inputs"] = prepared
                result = await method(artifact, prepared)
                if isinstance(result, ExecutionResult):
                    result_path = self.results.save(result)
                    state["result_path"] = result_path
                    self._context.memory.setdefault("run_history", []).append({
                        "run_id": result.run_id,
                        "inputs": prepared,
                        "outputs": result.outputs,
                        "metrics": result.metrics,
                    })
                    self.memory_hooks.on_result_saved(artifact, result, prepared)
                    state["backend_persist"] = await safe_backend_action(
                        self.backend, "persist_result", artifact, result, prepared,
                    )
                    state["backend_record"] = await safe_backend_action(
                        self.backend, "record_execution", artifact, result, prepared, result.outputs,
                    )
                return result
            return await method(input_data)
        raise ValueError(f"Unsupported workflow step: {step}")

    def _adapter_for_step(self, step: dict):
        adapter_name = step.get("adapter")
        if not adapter_name:
            return self.adapter
        try:
            # An adapter from a session-disabled package is not selectable
            # (review finding 1).
            adapter_class = self.registry.get_adapter(
                adapter_name, disabled_packages=self._disabled_packages(),
            )
        except KeyError:
            if adapter_name == type(self.adapter).__name__:
                return self.adapter
            raise
        if isinstance(self.adapter, adapter_class):
            return self.adapter
        return _instantiate_adapter(adapter_class, db_path=self._db_path, session_id=self.session_id)

    async def _execute_step_with_policy(self, step: dict, state: dict, workflow_config: dict):
        retry_max = int(step.get("retry_max", 0) or 0)
        attempts = retry_max + 1 if step.get("on_error") == "retry" else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._execute_workflow_step(step, state, workflow_config)
            except Exception as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
        if step.get("on_error") == "skip":
            return {"status": "skipped", "error": str(last_exc)}
        raise last_exc or RuntimeError(f"Step failed: {step.get('id')}")

    # Map a workflow step id to the audit phase that fires *after* it
    # (design/todo.md item 7). Steps not in the map don't dispatch.
    _STEP_AUDIT_PHASE = {
        "ideate": "ideation.after",
        "search": "search.after",
        "plan": "planning.after",
        "build": "build.after",
        "validate": "validation.after",
        "register": "register.after",
        "run": "execution.after",
        "review": "review.after",
        "reflect": "reflection.after",
    }

    # …and the *before* phase fired ahead of a step (the AUDIT_PHASES that
    # have a `.before` form). Lets package audits block a step pre-flight.
    _STEP_AUDIT_BEFORE_PHASE = {
        "ideate": "ideation.before",
        "validate": "validation.before",
        "run": "execution.before",
    }

    async def _dispatch_step_audit_before(self, step: dict, step_id: str, state: dict) -> None:
        """Fire the `.before` audit phase for a step that's about to run."""
        phase = self._STEP_AUDIT_BEFORE_PHASE.get(step_id)
        if phase is None or not self.audit.has_actions():
            return
        artifact = self._context.memory.get("current_artifact")
        await self.audit.dispatch(
            phase,
            iteration=self._context.iteration,
            role=step.get("agent"),
            artifact_id=self._get_field(artifact, "artifact_id"),
        )

    async def _dispatch_step_audit(self, step: dict, step_id: str, output, state: dict) -> None:
        """Fire the audit phase associated with a completed workflow step."""
        phase = self._STEP_AUDIT_PHASE.get(step_id)
        if phase is None or not self.audit.has_actions():
            return
        artifact = self._context.memory.get("current_artifact")
        run_output = self._get_field(state["steps"].get("run", {}), "output")
        await self.audit.dispatch(
            phase,
            iteration=self._context.iteration,
            role=step.get("agent"),
            artifact_id=self._get_field(artifact, "artifact_id"),
            run_id=self._get_field(run_output, "run_id"),
            output_summary=self._dump(output) if not isinstance(output, ArtifactRecord)
            else output.model_dump(),
        )

    async def _run_workflow_definition(self, workflow: dict, goal: ResearchGoal) -> dict:
        session_id = self._context.session_id
        workflow_config = workflow.get("config", {})
        max_iterations = int(workflow_config.get("max_iterations", 1))
        steps = workflow.get("steps", [])
        step_index = {step["id"]: idx for idx, step in enumerate(steps)}
        conditions = workflow.get("conditions", [])
        state = {
            "user_goal": goal,
            "inputs": self._bind_workflow_inputs(workflow, goal),
            "steps": {},
            "prepared_inputs": {},
            "result_path": None,
        }
        status = "completed"

        idx = 0
        transitions = 0
        max_transitions = int(
            workflow_config.get(
                "max_transitions",
                max(len(steps) * max_iterations * 10, len(steps) + 1),
            )
        )
        while idx < len(steps):
            if transitions >= max_transitions:
                status = "iteration_limit"
                break
            step = steps[idx]
            step_id = step["id"]
            await self._dispatch_step_audit_before(step, step_id, state)
            output = await self._execute_step_with_policy(step, state, workflow_config)
            state["steps"][step_id] = {"definition": step, "output": output}
            self.provenance.record(
                session_id,
                step_id,
                step.get("agent") or step.get("skill") or step.get("adapter", "workflow"),
                outputs=self._dump(output) if not isinstance(output, ArtifactRecord) else output.model_dump(),
            )
            await self._dispatch_step_audit(step, step_id, output, state)

            jumped = False
            for condition in conditions:
                if condition.get("after") != step_id:
                    continue
                matched = self._condition_matches(condition.get("if", ""), state)
                if matched:
                    goto = condition.get("goto")
                    if goto in step_index:
                        idx = step_index[goto]
                        jumped = True
                        break
                    # A goto pointing at a step that doesn't exist is almost
                    # always a typo — surface it instead of silently falling
                    # through to the next step.
                    logger.warning(
                        "Workflow condition after %r has goto=%r, which is not "
                        "a known step id %s — ignoring the jump.",
                        step_id, goto, sorted(step_index),
                    )
            if not jumped:
                idx += 1
            transitions += 1

        execution = self._get_field(state["steps"].get("run", {}), "output")
        review = self._get_field(state["steps"].get("review", {}), "output")
        artifact = (
            self._context.memory.get("current_artifact")
            or self._get_field(state["steps"].get("register", {}), "output")
            or self._get_field(state["steps"].get("validate", {}), "output")
        )
        validation = ValidationResult(valid=True)
        if artifact:
            validation = await self.adapter.validate_artifact(artifact)
        self._context.iteration += 1
        if self.audit.has_actions():
            await self.audit.dispatch(
                "iteration.after",
                iteration=self._context.iteration,
                artifact_id=self._get_field(artifact, "artifact_id"),
                run_id=self._get_field(execution, "run_id"),
                output_summary={"status": status},
            )
        return {
            "status": status,
            "session_id": session_id,
            "iteration": self._context.iteration,
            "proposal": self._dump(self._get_field(state["steps"].get("ideate", {}), "output")),
            "plan": self._dump(self._get_field(state["steps"].get("plan", {}), "output")),
            "artifact": self._dump(artifact),
            "validation": validation.model_dump(),
            "execution": self._dump(execution),
            "result_path": state.get("result_path"),
            "review": self._dump(review),
            "reflection": self._dump(self._get_field(state["steps"].get("reflect", {}), "output")),
            "workflow": workflow.get("name"),
            "steps": {k: self._dump(v["output"]) for k, v in state["steps"].items()},
        }

    async def run_once(self, goal: ResearchGoal) -> dict:
        session_id = self._context.session_id
        self.provenance.record(session_id, "start", "orchestrator", inputs=goal.model_dump())

        # Store target in context so reviewer can compare against it each iteration.
        if goal.target:
            self._context.memory["target"] = goal.target

        if self.audit.has_actions():
            await self.audit.dispatch(
                "goal.received",
                iteration=self._context.iteration,
                input_summary=goal.model_dump(),
            )

        try:
            workflow = self.registry.get_workflow(self.workflow_name)
        except KeyError as exc:
            # Previously this path fell through to a 60-line hard-coded
            # pipeline that duplicated the YAML workflow. That hid real
            # registration bugs (eg. a missing arc.toml or a package that
            # failed to load). Surface the error instead — the available
            # workflows are listed so users can pick a registered name.
            available = self.registry.list_workflows()
            raise KeyError(
                f"Workflow {self.workflow_name!r} is not registered. "
                f"Available workflows: {available}. "
                f"Check arc.toml [packages].paths and any errors during "
                f"package loading."
            ) from exc

        try:
            return await self._run_workflow_definition(workflow, goal)
        except Exception as exc:
            # Let package audits observe a failed run (e.g. record an error
            # in a lab notebook) before the exception propagates. The
            # workflow.error dispatch is itself best-effort.
            if self.audit.has_actions():
                try:
                    await self.audit.dispatch(
                        "workflow.error",
                        iteration=self._context.iteration,
                        output_summary={"error": str(exc)},
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise

    def assemble_report(self, **extra_sections) -> dict:
        """Assemble a structured research report from accumulated state.

        Thin pass-through to :func:`arc.runtime.audit.assemble_report` using
        this workflow's context. Packages contribute sections via
        ``extra_sections`` (item 7).
        """
        from arc.runtime.audit import assemble_report
        return assemble_report(self._context, extra_sections=extra_sections or None)
