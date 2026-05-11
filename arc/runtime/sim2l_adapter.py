"""Sim2L runtime adapter.

Bridges the ARC RuntimeAdapterContract to the real sim2l library.
Uses sim2l's LocalExecutor, SimulationDefinition, and repository APIs.
"""

import logging
import os
import sys
import types
import uuid
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
    """Load workflow function from workflow.py, registering it as a picklable module.

    sim2l pickles the function to store it in SQLite, so the module it lives in
    must be importable. We achieve this by adding the artifact directory to
    sys.path and importing under a unique module name derived from the artifact ID.
    """
    artifact_dir = Path(artifact_path)
    wf_path = artifact_dir / "workflow.py"
    if not wf_path.exists():
        raise FileNotFoundError(f"workflow.py not found at {wf_path}")
    source = wf_path.read_text()
    check_workflow_source(source)

    # Use the artifact_id to create a unique, stable module name.
    module_name = f"arc_artifact_{artifact_id.replace('-', '_')}"
    validate_workflow_import_timeout(source, module_name, str(wf_path))

    # Add the artifact directory to sys.path so Python can import it.
    artifact_dir_str = str(artifact_dir)
    if artifact_dir_str not in sys.path:
        sys.path.insert(0, artifact_dir_str)

    # Always load fresh. Artifacts can be patched under the same artifact ID.
    if module_name in sys.modules:
        del sys.modules[module_name]
    module = types.ModuleType(module_name)
    module.__file__ = str(wf_path)
    sys.modules[module_name] = module
    exec(compile(source, str(wf_path), "exec"), module.__dict__)  # noqa: S102

    if not hasattr(module, "simulate"):
        raise AttributeError(
            "workflow.py must define a function named 'simulate(**inputs) -> dict'"
        )
    return module.simulate


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

    def __init__(self, db_path: str | None = None, session_id: str | None = None):
        self._db_path = db_path or os.environ.get(
            "ARC_SIM2L_DB", str(Path.home() / ".sim2l" / "simulations.db")
        )
        self._session_id = session_id
        self._storage_mode = os.environ.get("ARC_STORAGE_MODE", "local").lower()
        self._sim2l_ok = self._check_sim2l()
        # Cache of deployed simulations keyed by (artifact_id, version):
        #   (workflow_hash, sim_def, sim_name, sim_version, in_schema, out_schema, catalog_persisted)
        # Reused across iterations of the research loop so we don't redeploy +
        # catalog-push on every run() — only when the workflow source changes.
        self._deploy_cache: dict[tuple[str, str], tuple] = {}

    @property
    def _services_required(self) -> bool:
        return self._storage_mode in {"service", "services", "required"}

    def _check_sim2l(self) -> bool:
        try:
            import sim2l  # noqa: F401
            return True
        except ImportError:
            logger.warning("sim2l not installed — Sim2LRuntimeAdapter unavailable")
            return False

    @property
    def _catalog_url(self):
        return os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")

    @property
    def _results_url(self):
        return os.environ.get("SIM2L_RESULTS_URL", "http://localhost:8003")

    def _get_repo(self):
        import sim2l
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        sim2l.configure(db_path=self._db_path)
        from sim2l.repository import SimulationRepository
        return SimulationRepository()

    def _catalog_client(self):
        from sim2l.database.catalog_client import CatalogClient
        return CatalogClient(service_url=self._catalog_url)  # no session_id — avoids install registration

    def _results_client(self):
        from sim2l.database.results_client import ResultsClient
        return ResultsClient(
            base_url=self._results_url,
            session_id=self._session_id,
        )

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
                input_schema=sim_def.inputs.to_dict(),
                output_schema=sim_def.outputs.to_dict(),
                auto_approve=True,
                metadata=metadata if metadata else None,
            )

            # register_simulation returns False on 409 — update instead.
            if not ok and metadata:
                try:
                    resp = _req.get(
                        f"{self._catalog_url}/simulations/{sim_name}",
                        params={"version": sim_version},
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        sim_id = resp.json().get("id")
                        if sim_id:
                            return bool(client.update_simulation(sim_id, {"metadata": metadata}))
                except Exception as upd_exc:
                    logger.debug(f"Catalog update skipped: {upd_exc}")
            return bool(ok)
        except Exception as exc:
            logger.warning(f"Catalog push failed (non-fatal): {exc}")
            return False

    def _push_to_results(
        self, sim2l_result, sim_name: str, sim_version: str,
        reconciled_inputs: dict, outputs: dict,
    ) -> bool:
        """Register the execution result in the results service via /register_direct."""
        try:
            import requests
            url = f"{self._results_url}/register_direct"
            headers = {}
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
            logger.warning(f"Results push failed (non-fatal): {exc}")
            return False

    async def validate_artifact(self, artifact: ArtifactRecord) -> ValidationResult:
        if not self._sim2l_ok:
            return ValidationResult(valid=False, errors=["sim2l not installed"])

        artifact_path = Path(artifact.path)
        errors, warnings = [], []

        if not artifact_path.exists():
            errors.append(f"Artifact path does not exist: {artifact.path}")
            return ValidationResult(valid=False, errors=errors)

        if not (artifact_path / "workflow.py").exists():
            errors.append("workflow.py missing — must define simulate(**inputs) -> dict")
        if not (artifact_path / "sim2l.yaml").exists():
            warnings.append("sim2l.yaml missing — using auto-detected schema")

        if not errors:
            try:
                _import_workflow_func(artifact.path, artifact.artifact_id)
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
        try:
            return (Path(artifact.path) / "workflow.py").read_text()
        except Exception:
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
            from sim2l.executor import LocalExecutor

            sim_def, sim_name, sim_version, in_schema, out_schema, catalog_persisted = (
                self._ensure_deployed(artifact, inputs)
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

            # Execute the fresh callable-backed definition. The repository stores
            # function workflows as source bytes in current sim2l, and reloading
            # them makes LocalExecutor try to unpickle source text.
            executor = LocalExecutor(cache=True)
            sim2l_result = sim_def.run(**reconciled, executor=executor)

            outputs = {
                k: getattr(sim2l_result.outputs, k, None)
                for k in out_schema.fields
            }
            results_persisted = self._push_to_results(
                sim2l_result, sim_name, sim_version, reconciled, outputs
            )
            if self._services_required and not results_persisted:
                return ExecutionResult(
                    run_id=sim2l_result.execution_id,
                    status="error",
                    outputs=outputs,
                    logs=["Results service persistence failed"],
                    metrics={"storage_mode": self._storage_mode},
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
