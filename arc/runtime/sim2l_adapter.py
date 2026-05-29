"""Sim2L runtime adapter.

Bridges the ARC RuntimeAdapterContract to the real sim2l library.
Uses sim2l's LocalExecutor, SimulationDefinition, and repository APIs.
"""

import asyncio
import logging
import os
import types
import uuid
import base64
from itertools import product
from pathlib import Path
from typing import Any

from arc.contracts.adapter import RuntimeAdapterContract
from arc.runtime.workflow_safety import (
    check_workflow_source,
    validate_workflow_import_timeout,
)
from arc.schemas.artifact import ArtifactRecord, ValidationResult
from arc.schemas.execution import ExecutionResult

logger = logging.getLogger(__name__)


def _import_workflow_func(artifact_path: str, artifact_id: str):
    """Load workflow.py's ``simulate`` function in a sandboxed namespace.

    Previously this function (a) inserted ``artifact_path`` into ``sys.path``
    permanently and (b) registered the loaded module under a synthetic name
    in ``sys.modules`` — both unbounded leaks (sys.path grew with every
    artifact; a malicious artifact dropping ``os.py`` could shadow stdlib for
    the rest of the process). Review item #A1.

    sim2l's ``SimulationDefinition`` already supports source-attached
    callables via ``func.__sim2l_source__`` (see
    ``sim2l/definition/simulation.py:97-103``), so we don't need an
    importable module at all. We exec the source in a throwaway namespace,
    stamp the source onto the function for sim2l's hash + serialization
    fast paths, and return it.
    """
    artifact_dir = Path(artifact_path)
    wf_path = artifact_dir / "workflow.py"
    if not wf_path.exists():
        raise FileNotFoundError(f"workflow.py not found at {wf_path}")
    source = wf_path.read_text()
    check_workflow_source(source)

    module_name = f"arc_artifact_{artifact_id.replace('-', '_')}"
    validate_workflow_import_timeout(source, module_name, str(wf_path))

    # Throwaway module: NOT registered in sys.modules; sys.path is NOT mutated.
    # The module exists only as a scope for ``exec`` so that intra-file
    # references (helper functions, constants) still resolve.
    module = types.ModuleType(module_name)
    module.__file__ = str(wf_path)
    exec(compile(source, str(wf_path), "exec"), module.__dict__)  # noqa: S102

    if not hasattr(module, "simulate"):
        raise AttributeError(
            "workflow.py must define a function named 'simulate(**inputs) -> dict'"
        )
    func = module.simulate
    # Attach the source so sim2l's ``SimulationDefinition`` can serialize and
    # hash without needing ``inspect.getsource`` (which would require the
    # function's module to live in sys.modules + sys.path).
    try:
        func.__sim2l_source__ = source
    except (AttributeError, TypeError):
        # Built-in / C-extension callables won't accept attribute writes,
        # but a user-defined `def simulate(...)` always will.
        pass
    return func


def _function_workflow_bundle(source: str, *, workflow_hash: str | None = None) -> dict[str, Any]:
    """Build the catalog-service portable bundle for an ARC function artifact."""

    source_bytes = source.encode("utf-8")
    return {
        "format_version": 1,
        "workflow_type": "function",
        "entrypoint": "workflow.py",
        "files": [
            {
                "path": "workflow.py",
                "encoding": "base64",
                "content_base64": base64.b64encode(source_bytes).decode("ascii"),
                "size_bytes": len(source_bytes),
                "sha256": workflow_hash,
            }
        ],
    }


class Sim2LRuntimeAdapter(RuntimeAdapterContract):
    """Executes artifact workflows through the real sim2l library.

    Each artifact's workflow.py must export a function:

        def simulate(**inputs) -> dict:
            ...
            return {"output_name": value, ...}

    The adapter deploys the function as a SimulationDefinition into a
    per-workspace SQLite database, then executes it via sim2l's LocalExecutor
    (with caching via SQUID IDs).
    """

    def __init__(
        self,
        db_path: str | None = None,
        session_id: str | None = None,
        *,
        catalog_session_id: str | None = None,
        results_session_id: str | None = None,
        cache_session_id: str | None = None,
    ):
        self._db_path = db_path or os.environ.get(
            "ARC_SIM2L_DB", str(Path.home() / ".sim2l" / "simulations.db")
        )
        self._session_id = session_id
        # Per-service session ids issued by the catalog / results / cache
        # services at chat startup (see arc.chat.sim2l_auth.login_to_services).
        # When None, the corresponding client request will get a 401
        # whenever the service has require_auth enabled — the chat layer
        # is responsible for warning the user and asking how to proceed.
        self._catalog_session_id = catalog_session_id
        self._results_session_id = results_session_id
        self._cache_session_id = cache_session_id
        self._storage_mode = os.environ.get("ARC_STORAGE_MODE", "local").lower()
        self._sim2l_ok = self._check_sim2l()
        # Accumulating record of any catalog/results push failures
        # encountered since construction. The chat reads this after each
        # run to surface a single combined warning to the user instead
        # of spamming a line per failed push.
        self.last_push_errors: list[tuple[str, str]] = []
        # Cache of deployed simulations keyed by (artifact_id, version):
        #   (workflow_hash, sim_def, sim_name, sim_version, in_schema, out_schema, catalog_persisted)
        # Reused across iterations of the research loop so we don't redeploy +
        # catalog-push on every run() — only when the workflow source changes.
        self._deploy_cache: dict[tuple[str, str], tuple] = {}

    @property
    def _services_required(self) -> bool:
        return self._storage_mode in {"service", "services", "required"}

    def _check_sim2l(self) -> bool:
        """Probe whether ``sim2l`` imports cleanly.

        Returns False on *any* import-time failure — not just ``ImportError``
        — because a broken sim2l install (eg. a SyntaxError in a submodule
        or a missing transitive dep) should also disable the adapter rather
        than crash the adapter constructor. Review item #A4.
        """
        try:
            import sim2l  # noqa: F401
            return True
        except Exception as exc:
            logger.warning(
                "sim2l import failed (%s: %s) — Sim2LRuntimeAdapter unavailable",
                type(exc).__name__, exc,
            )
            return False

    @property
    def _catalog_url(self):
        return os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")

    @property
    def _results_url(self):
        return os.environ.get("SIM2L_RESULTS_URL", "http://localhost:8003")

    @staticmethod
    def _accepted_configure_keys(sim2l_module) -> set[str]:
        """Return the set of config keys ``sim2l.configure`` will accept.

        ``sim2l.configure`` uses ``**kwargs`` and validates against the
        underlying config object's attributes, so ``inspect.signature``
        always reports ``{'kwargs'}``. We mirror that validation here by
        reading attributes directly off the config singleton.
        """
        try:
            cfg = sim2l_module.config.get_config()
        except Exception:
            return set()
        return {a for a in dir(cfg) if not a.startswith("_") and not callable(getattr(cfg, a, None))}

    def _get_repo(self):
        """Configure sim2l for this adapter and return a repository handle.

        Beyond setting the local db path, we also push the cache /
        catalog / results service URLs and session ids into sim2l's
        global config so the executor will hit the cache service on
        every run (instead of falling back to the legacy in-DB cache
        table). Without this, the cache service stays empty and the
        chat re-runs simulations that the catalog has already cached
        for another user.
        """
        import sim2l
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        cache_url = os.environ.get("SIM2L_CACHE_URL", "http://localhost:8001")
        catalog_url = self._catalog_url
        results_url = self._results_url

        configure_kwargs: dict[str, Any] = {"db_path": self._db_path}
        # Older sim2l builds don't expose every config knob. sim2l.configure
        # raises ValueError for unknown keys, so we probe the underlying
        # config object directly: if the attribute exists, the key is safe.
        # (inspect.signature is useless here — configure uses **kwargs.)
        accepted = self._accepted_configure_keys(sim2l)
        if "cache_service_url" in accepted:
            configure_kwargs["cache_service_url"] = cache_url
        if "cache_session_id" in accepted and self._cache_session_id:
            configure_kwargs["cache_session_id"] = self._cache_session_id
        if "catalog_service_url" in accepted:
            configure_kwargs["catalog_service_url"] = catalog_url
        if "catalog_session_id" in accepted and self._catalog_session_id:
            configure_kwargs["catalog_session_id"] = self._catalog_session_id
        if "results_service_url" in accepted:
            configure_kwargs["results_service_url"] = results_url
        if "results_session_id" in accepted and self._results_session_id:
            configure_kwargs["results_session_id"] = self._results_session_id

        sim2l.configure(**configure_kwargs)
        from sim2l.repository import SimulationRepository
        return SimulationRepository()

    def _catalog_client(self):
        from sim2l.database.catalog_client import CatalogClient
        # Pass the catalog-side session id when the chat has authenticated
        # to the catalog service. Without it, services in require_auth
        # mode return 401 on every push. See arc.chat.sim2l_auth.
        return CatalogClient(
            service_url=self._catalog_url,
            session_id=self._catalog_session_id,
        )

    def _results_client(self):
        from sim2l.database.results_client import ResultsClient
        # Prefer the results-service session id when we logged in; fall
        # back to the legacy chat session id so existing callers that
        # construct the adapter directly don't regress.
        sid = self._results_session_id or self._session_id
        return ResultsClient(
            base_url=self._results_url,
            session_id=sid,
        )

    def _classify_push_error(self, exc: Exception, label: str) -> None:
        """Pick the right log level for a push failure.

        * Connection refused / DNS failure → debug (service not running,
          the chat already warned about that at startup).
        * 4xx (especially 401/403) → warning, the user can act.
        * 5xx → warning, the user should know.
        * Anything else → debug.

        Also records the failure on ``self.last_push_errors`` so the chat
        layer can surface a single combined message at the end of the
        pipeline rather than spamming per-row.
        """
        import requests
        msg = f"{label}: {type(exc).__name__}: {exc}"
        if isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout)):
            logger.debug(msg)
        elif isinstance(exc, requests.exceptions.HTTPError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                logger.warning(
                    "%s service rejected request (HTTP %s) — chat needs valid "
                    "credentials. Set SIM2L_USERNAME / SIM2L_PASSWORD or check "
                    "the service's require_auth setting.", label, status,
                )
            else:
                logger.warning("%s push failed: HTTP %s", label, status)
        else:
            logger.warning("%s push failed: %s", label, msg)
        self.last_push_errors.append((label, msg))

    def _push_to_catalog(self, sim_def, sim_name: str, sim_version: str,
                         workflow_source: str | None = None) -> bool:
        """Register or update the simulation in the catalog service.

        On 409 (already registered), fetches the existing record and PATCHes
        its metadata so the workflow source stays current.
        """
        try:
            import requests as _req
            client = self._catalog_client()
            metadata = {}
            if workflow_source:
                metadata["workflow_source"] = workflow_source

            ok = client.register_simulation(
                name=sim_name,
                version=sim_version,
                description=sim_def.description or sim_name,
                workflow_type="function",
                workflow_hash=sim_def.workflow_hash,
                workflow_bundle=(
                    _function_workflow_bundle(
                        workflow_source,
                        workflow_hash=sim_def.workflow_hash,
                    )
                    if workflow_source
                    else None
                ),
                input_schema=sim_def.inputs.to_dict(),
                output_schema=sim_def.outputs.to_dict(),
                auto_approve=True,
                metadata=metadata if metadata else None,
            )

            # register_simulation returns False on 409 — update instead.
            if not ok and metadata:
                try:
                    headers = {}
                    if self._catalog_session_id:
                        headers["X-Session-ID"] = self._catalog_session_id
                    resp = _req.get(
                        f"{self._catalog_url}/simulations/{sim_name}",
                        params={"version": sim_version},
                        headers=headers,
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        sim_id = resp.json().get("id")
                        if sim_id:
                            updates = {"metadata": metadata}
                            if workflow_source:
                                updates["workflow_bundle"] = _function_workflow_bundle(
                                    workflow_source,
                                    workflow_hash=sim_def.workflow_hash,
                                )
                            return bool(client.update_simulation(sim_id, updates))
                except Exception as upd_exc:
                    logger.debug(f"Catalog update skipped: {upd_exc}")
            if not ok:
                logger.warning(
                    "Catalog push for %s/%s returned a falsy result — likely "
                    "401 (no session) or 409 with no metadata to merge. "
                    "Pass authenticated session ids to enable persistence.",
                    sim_name, sim_version,
                )
                self.last_push_errors.append((
                    "catalog",
                    f"register_simulation returned falsy for {sim_name}/{sim_version}",
                ))
            return bool(ok)
        except Exception as exc:
            self._classify_push_error(exc, "catalog")
            return False

    def _push_to_results(
        self, sim2l_result, sim_name: str, sim_version: str,
        reconciled_inputs: dict, outputs: dict,
    ) -> bool:
        """Register the execution result in the results service via /register_direct."""
        try:
            import requests
            url = f"{self._results_url}/register_direct"
            # Include the X-Session-ID header so services in require_auth
            # mode accept the request. Earlier code sent ``headers={}``
            # and got 401 on every push.
            headers = {}
            sid = self._results_session_id or self._session_id
            if sid:
                headers["X-Session-ID"] = sid
            payload = {
                "execution_id": sim2l_result.execution_id,
                "simulation_name": sim_name,
                "simulation_version": sim_version,
                "squid_id": sim2l_result.squid_id or "",
                "input_params": reconciled_inputs,
                "output_params": {k: v for k, v in outputs.items() if v is not None},
                "status": sim2l_result.status,
                "duration_seconds": sim2l_result.duration_seconds,
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as exc:
            self._classify_push_error(exc, "results")
            return False

    def _push_to_catalog_execution_registry(
        self, sim2l_result, sim_name: str, sim_version: str,
        reconciled_inputs: dict, outputs: dict,
    ) -> bool:
        """Record the execution in the catalog's execution_registry table.

        Drives the dashboard's "Total Executions" counter (which reads
        from the catalog, not from the results service). Without this
        call, the catalog stays at 0 even when /register_direct
        succeeds on the results service.

        Best-effort: a failure here doesn't fail the run. The
        ``last_push_errors`` list records what happened so the chat
        surfaces a single combined warning.
        """
        try:
            import hashlib
            import json
            import time
            import requests

            # We need the catalog's numeric simulation_id. Look it up by
            # name+version. If the simulation isn't in the catalog, this
            # GET returns 404 and we silently skip (the chat already
            # warns about that elsewhere).
            headers = {}
            if self._catalog_session_id:
                headers["X-Session-ID"] = self._catalog_session_id
            lookup = requests.get(
                f"{self._catalog_url}/simulations/{sim_name}",
                params={"version": sim_version},
                headers=headers,
                timeout=5,
            )
            if lookup.status_code == 404:
                # No catalog record yet — the dashboard counter only
                # tracks executions of catalog-known simulations.
                return False
            lookup.raise_for_status()
            sim_record = lookup.json() or {}
            sim_id = sim_record.get("id")
            if not sim_id:
                return False

            from datetime import datetime, timezone
            started_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            input_hash = hashlib.sha256(
                json.dumps(reconciled_inputs, sort_keys=True, default=str).encode("utf-8"),
            ).hexdigest()
            payload = {
                "execution_id": sim2l_result.execution_id,
                "squid_id": sim2l_result.squid_id or sim2l_result.execution_id,
                "simulation_id": sim_id,
                "user_id": None,
                "started_at": started_at,
                "completed_at": started_at,
                "duration_seconds": getattr(sim2l_result, "duration_seconds", None),
                "status": sim2l_result.status,
                "executor_type": getattr(sim2l_result, "executor_type", "isolated"),
                "cache_hit": False,
                "run_db_path": None,
                "run_db_size_bytes": None,
                "input_hash": input_hash,
                "output_count": len(outputs),
                "artifact_count": 0,
                "error_count": 1 if sim2l_result.status != "completed" else 0,
                "warning_count": 0,
                "environment": {"source": "arc.chat"},
            }
            resp = requests.post(
                f"{self._catalog_url}/executions",
                json=payload, headers=headers, timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            self._classify_push_error(exc, "catalog/executions")
            return False

    async def validate_artifact(self, artifact: ArtifactRecord) -> ValidationResult:
        if not self._sim2l_ok:
            return ValidationResult(valid=False, errors=["sim2l not installed"])

        artifact_path = Path(artifact.path)
        errors, warnings = [], []

        if not artifact_path.exists():
            errors.append(f"Artifact path does not exist: {artifact.path}")
            return ValidationResult(valid=False, errors=errors)

        wf_path = artifact_path / "workflow.py"
        if not wf_path.exists():
            errors.append("workflow.py missing — must define simulate(**inputs) -> dict")
        if not (artifact_path / "sim2l.yaml").exists():
            warnings.append("sim2l.yaml missing — using auto-detected schema")

        if not errors:
            # Validate via the subprocess sandbox so we don't touch this
            # process's sys.path / sys.modules just to check the artifact.
            # Review item #A5.
            try:
                source = wf_path.read_text()
                check_workflow_source(source)
                module_name = f"arc_artifact_validate_{artifact.artifact_id.replace('-', '_')}"
                validate_workflow_import_timeout(source, module_name, str(wf_path))
            except Exception as exc:
                errors.append(f"workflow.py import error: {exc}")

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    async def prepare_inputs(
        self,
        artifact: ArtifactRecord,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        return parameters

    def _schemas_for_artifact(self, artifact: ArtifactRecord, inputs: dict[str, Any] | None = None):
        from sim2l.schema import InputSchema, OutputSchema

        sim2l_yaml = Path(artifact.path) / "sim2l.yaml"
        if sim2l_yaml.exists():
            import yaml
            spec = yaml.safe_load(sim2l_yaml.read_text()) or {}
            in_schema = InputSchema.from_yaml(
                "\n".join(
                    f"{k}:\n  type: Number\n  default: {v.get('default', 1.0)}"
                    for k, v in spec.get("inputs", {}).items()
                )
            )
            out_schema = OutputSchema.from_yaml(
                "\n".join(
                    f"{k}:\n  type: Number"
                    for k in spec.get("outputs", {})
                )
            )
            return in_schema, out_schema

        input_values = inputs or {}
        in_fields = "\n".join(
            f"{k}:\n  type: Number\n  default: {v}"
            for k, v in input_values.items()
        )
        in_schema = InputSchema.from_yaml(in_fields or "x:\n  type: Number\n  default: 1.0")
        out_schema = OutputSchema.from_yaml("result:\n  type: Number")
        return in_schema, out_schema

    def _workflow_source(self, artifact: ArtifactRecord) -> str | None:
        """Read workflow.py source, returning ``None`` on any failure.

        Logs the failure at WARNING so a missing/unreadable file doesn't
        silently disappear into a None return. Review item #A3.
        """
        wf_path = Path(artifact.path) / "workflow.py"
        try:
            return wf_path.read_text()
        except FileNotFoundError:
            logger.debug("No workflow.py at %s — artifact has no source", wf_path)
            return None
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Cannot read workflow.py at %s: %s", wf_path, exc)
            return None

    def _simulation_definition_for_artifact(
        self,
        artifact: ArtifactRecord,
        inputs: dict[str, Any] | None = None,
    ):
        import sim2l

        func = _import_workflow_func(artifact.path, artifact.artifact_id)
        in_schema, out_schema = self._schemas_for_artifact(artifact, inputs)
        sim_name = artifact.name[:50]
        sim_version = artifact.version
        sim_def = sim2l.SimulationDefinition.from_function(
            func=func,
            name=sim_name,
            version=sim_version,
            inputs=in_schema,
            outputs=out_schema,
            description=(
                getattr(artifact, "description", "")
                or artifact.metadata.get("description")
                or artifact.metadata.get("hypothesis")
                or artifact.name
            ),
        )
        return sim_def, sim_name, sim_version, in_schema, out_schema

    def _deploy_simulation_definition(
        self,
        sim_def,
        sim_name: str,
        sim_version: str,
        workflow_source: str | None,
    ) -> bool:
        repo = self._get_repo()
        try:
            repo.deploy(sim_def)
        except ValueError:
            repo.delete(sim_name, version=sim_version)
            repo.deploy(sim_def)
        return self._push_to_catalog(sim_def, sim_name, sim_version, workflow_source)

    def _ensure_deployed(
        self,
        artifact: ArtifactRecord,
        inputs: dict[str, Any] | None = None,
    ) -> tuple[Any, str, str, Any, Any, bool]:
        """Return a deployed ``(sim_def, sim_name, sim_version, in_schema, out_schema, catalog_persisted)``.

        Reuses a previously deployed definition for the same ``(artifact_id,
        version)`` as long as ``workflow.py`` source has not changed. This
        avoids the per-iteration ``repo.delete + repo.deploy + catalog push``
        storm that otherwise occurs across a research loop's N runs.
        """
        import hashlib

        source = self._workflow_source(artifact) or ""
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        key = (artifact.artifact_id, artifact.version)

        cached = self._deploy_cache.get(key)
        if cached is not None and cached[0] == source_hash:
            (
                _,
                sim_def,
                sim_name,
                sim_version,
                in_schema,
                out_schema,
                catalog_persisted,
            ) = cached
            # If the schemas are sensitive to caller inputs (legacy behavior
            # when sim2l.yaml is missing), rebuild only the schemas — the
            # underlying simulate() callable is unchanged.
            if inputs is not None and not (Path(artifact.path) / "sim2l.yaml").exists():
                in_schema, out_schema = self._schemas_for_artifact(artifact, inputs)
            return sim_def, sim_name, sim_version, in_schema, out_schema, catalog_persisted

        sim_def, sim_name, sim_version, in_schema, out_schema = (
            self._simulation_definition_for_artifact(artifact, inputs)
        )
        try:
            catalog_persisted = self._deploy_simulation_definition(
                sim_def, sim_name, sim_version, source or None
            )
        except Exception as exc:
            logger.debug(f"deploy failed (non-fatal): {exc}")
            catalog_persisted = False

        self._deploy_cache[key] = (
            source_hash,
            sim_def,
            sim_name,
            sim_version,
            in_schema,
            out_schema,
            catalog_persisted,
        )
        return sim_def, sim_name, sim_version, in_schema, out_schema, catalog_persisted

    async def register_artifact(self, artifact: ArtifactRecord) -> dict[str, Any]:
        """Deploy an ARC artifact into Sim2L and best-effort sync it to catalog."""
        if not self._sim2l_ok:
            return {"registered": False, "error": "sim2l not installed"}
        try:
            deployed = self._ensure_deployed(artifact)
            sim_name = deployed[1]
            sim_version = deployed[2]
            catalog_persisted = deployed[5]
            if self._services_required and not catalog_persisted:
                return {
                    "registered": False,
                    "sim_name": sim_name,
                    "sim_version": sim_version,
                    "catalog_persisted": False,
                    "error": "Catalog service persistence failed",
                }
            return {
                "registered": True,
                "sim_name": sim_name,
                "sim_version": sim_version,
                "catalog_persisted": catalog_persisted,
            }
        except Exception as exc:
            return {"registered": False, "error": str(exc)}

    def _build_executor(self):
        """Pick the executor used to run an artifact's simulate() function.

        Defaults to ``IsolatedFunctionExecutor`` (review item #A2): arc
        generates and runs untrusted workflow source, so it should not run
        in the same Python process as the orchestrator. Operators who need
        the legacy in-process behaviour for performance can set
        ``ARC_EXECUTOR=local``; that path retains the legacy threat model
        and is intended for trusted-source environments only.
        """
        executor_name = os.environ.get("ARC_EXECUTOR", "isolated").lower()
        if executor_name in {"local", "in-process", "inprocess"}:
            from sim2l.executor import LocalExecutor
            logger.info(
                "Using LocalExecutor for arc artifact execution (ARC_EXECUTOR=%s). "
                "Untrusted workflow.py source will execute in the orchestrator "
                "process — only use this with trusted source.",
                executor_name,
            )
            return LocalExecutor(cache=True)
        from sim2l.executor import IsolatedFunctionExecutor
        return IsolatedFunctionExecutor(cache=True)

    async def run(
        self,
        artifact: ArtifactRecord,
        inputs: dict[str, Any],
    ) -> ExecutionResult:
        if not self._sim2l_ok:
            return ExecutionResult(
                run_id=str(uuid.uuid4()),
                status="error",
                logs=["sim2l not installed"],
            )
        try:
            sim_def, sim_name, sim_version, in_schema, out_schema, catalog_persisted = (
                await asyncio.to_thread(self._ensure_deployed, artifact, inputs)
            )
            if self._services_required and not catalog_persisted:
                return ExecutionResult(
                    run_id=str(uuid.uuid4()),
                    status="error",
                    logs=["Catalog service persistence failed"],
                    metrics={"storage_mode": self._storage_mode},
                )

            # Reconcile caller inputs with schema: keep only known fields,
            # fill unknowns from schema defaults. This prevents "Unexpected fields"
            # when the LLM generated different param names than the caller used.
            schema_defaults = {
                k: in_schema.fields[k].default
                for k in in_schema.fields
                if hasattr(in_schema.fields[k], "default")
            }
            reconciled = {**schema_defaults, **{k: v for k, v in inputs.items() if k in in_schema.fields}}

            # Execute via the configured executor (subprocess by default —
            # see _build_executor for the threat-model justification).
            # ``sim_def.run`` is a blocking call (it may spawn a subprocess
            # and wait); offload it so it doesn't stall the event loop when
            # the adapter is driven from the async API / research loop.
            executor = self._build_executor()
            sim2l_result = await asyncio.to_thread(
                sim_def.run, **reconciled, executor=executor
            )

            outputs = {
                k: getattr(sim2l_result.outputs, k, None)
                for k in out_schema.fields
            }
            # The persist/record helpers do blocking HTTP (requests.*); run
            # them off the loop too.
            results_persisted = await asyncio.to_thread(
                self._push_to_results,
                sim2l_result, sim_name, sim_version, reconciled, outputs,
            )
            if self._services_required and not results_persisted:
                return ExecutionResult(
                    run_id=sim2l_result.execution_id,
                    status="error",
                    outputs=outputs,
                    logs=["Results service persistence failed"],
                    metrics={"storage_mode": self._storage_mode},
                )

            # Also record the execution in the catalog's execution_registry
            # so the web-UI dashboard's "Total Executions" counter
            # advances. This is best-effort: a 404 (sim not in catalog)
            # or 401 doesn't fail the run, but is surfaced via
            # ``last_push_errors`` to the chat.
            await asyncio.to_thread(
                self._push_to_catalog_execution_registry,
                sim2l_result, sim_name, sim_version, reconciled, outputs,
            )

            outputs = {
                k: getattr(sim2l_result.outputs, k, None)
                for k in out_schema.fields
            }

            return ExecutionResult(
                run_id=sim2l_result.execution_id,
                status=sim2l_result.status,
                outputs=outputs,
                logs=[
                    f"sim2l run_id : {sim2l_result.execution_id}",
                    f"squid_id     : {sim2l_result.squid_id}",
                    f"duration     : {sim2l_result.duration_seconds:.3f}s",
                    f"inputs used  : {reconciled}",
                ],
                metrics={
                    "duration_seconds": sim2l_result.duration_seconds,
                    "squid_id": sim2l_result.squid_id,
                    "catalog_persisted": catalog_persisted,
                    "results_persisted": results_persisted,
                    **reconciled,
                },
            )

        except Exception as exc:
            normalized = await self.normalize_errors(exc)
            return ExecutionResult(
                run_id=str(uuid.uuid4()),
                status="error",
                logs=[normalized["message"]],
            )

    async def run_sweep(
        self,
        artifact: ArtifactRecord,
        parameter_space: dict[str, list[Any]],
    ) -> list[ExecutionResult]:
        results = []
        keys = list(parameter_space)
        if not keys:
            return [await self.run(artifact, {})]
        for values in product(*(parameter_space[key] for key in keys)):
            result = await self.run(artifact, dict(zip(keys, values)))
            results.append(result)
        return results

    async def get_status(self, run_id: str) -> str:
        if not self._sim2l_ok:
            return "unknown"
        try:
            from sim2l.result import load_result
            r = load_result(run_id)
            return r.status
        except Exception:
            return "unknown"

    async def collect_outputs(self, run_id: str) -> dict[str, Any]:
        if not self._sim2l_ok:
            return {}
        try:
            from sim2l.result import load_result
            r = load_result(run_id)
            return {k: getattr(r.outputs, k, None) for k in r.outputs.__dict__}
        except Exception:
            return {}

    async def collect_logs(self, run_id: str) -> list[str]:
        return []

    async def collect_metrics(self, run_id: str) -> dict[str, Any]:
        return {}

    async def normalize_errors(self, error: Exception) -> dict[str, Any]:
        return {"type": error.__class__.__name__, "message": str(error)}
