import json
import logging
import uuid
from pathlib import Path

from arc.contracts.artifact import ArtifactState
from arc.schemas.artifact import ArtifactDraft, ArtifactRecord
from arc.sim2l_schema import load_sim2l_schema

logger = logging.getLogger(__name__)


class ArtifactRegistry:
    """File-based artifact registry. Stores artifact files and metadata."""

    def __init__(self, root: str = "workspace/artifacts"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, draft: ArtifactDraft, version: str = "0.1.0") -> ArtifactRecord:
        artifact_id = str(uuid.uuid4())
        artifact_path = self.root / artifact_id / version
        artifact_path.mkdir(parents=True, exist_ok=True)

        base = artifact_path.resolve()
        for filename, content in draft.files.items():
            target = (artifact_path / filename).resolve()
            if target == base or base not in target.parents:
                raise ValueError(f"Unsafe artifact filename: {filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        metadata = self._metadata_with_schema(draft.metadata, artifact_path)
        metadata.setdefault("description", draft.description)
        record = ArtifactRecord(
            artifact_id=artifact_id,
            name=draft.name,
            description=draft.description,
            version=version,
            state=ArtifactState.REGISTERED,
            path=str(artifact_path),
            metadata=metadata,
        )
        self._write_record(record)
        return record

    def update_state(self, artifact_id: str, version: str, state: ArtifactState) -> ArtifactRecord:
        record = self.get(artifact_id, version)
        record.state = state
        self._write_record(record)
        return record

    def get(self, artifact_id: str, version: str = "0.1.0") -> ArtifactRecord:
        record_path = self.root / artifact_id / version / "arc_record.json"
        if not record_path.exists():
            raise FileNotFoundError(f"Artifact not found: {artifact_id}/{version}")
        record = ArtifactRecord.model_validate_json(record_path.read_text())
        metadata = self._metadata_with_schema(record.metadata, Path(record.path))
        if not record.description:
            record.description = metadata.get("description", "")
        if metadata != record.metadata:
            record.metadata = metadata
            try:
                self._write_record(record)
            except OSError:
                pass
        return record

    def list_all(self) -> list[ArtifactRecord]:
        records = []
        for record_path in self.root.glob("*/*/arc_record.json"):
            try:
                record = ArtifactRecord.model_validate_json(record_path.read_text())
            except Exception as exc:
                # Log the failure so corrupt records aren't silently invisible.
                # A common cause is a partial write during a previous crash;
                # users can remove or repair the file based on the path logged.
                logger.warning(
                    "Skipping unreadable artifact record %s: %s",
                    record_path, exc,
                )
                continue
            if not record.description:
                record.description = record.metadata.get("description", "")
            records.append(record)
        return records

    def _write_record(self, record: ArtifactRecord) -> None:
        record_path = self.root / record.artifact_id / record.version / "arc_record.json"
        record_path.write_text(json.dumps(record.model_dump(), indent=2))
        meta_path = self.root / record.artifact_id / record.version / "arc_metadata.json"
        meta_path.write_text(json.dumps(record.metadata, indent=2))

    def _metadata_with_schema(self, metadata: dict, artifact_path: Path) -> dict:
        enriched = dict(metadata or {})
        inputs, outputs = load_sim2l_schema(artifact_path)
        if inputs and not enriched.get("sim2l_inputs"):
            enriched["sim2l_inputs"] = inputs
        if outputs and not enriched.get("sim2l_outputs"):
            enriched["sim2l_outputs"] = outputs
        return enriched
