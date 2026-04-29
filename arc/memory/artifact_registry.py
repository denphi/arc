import json
import uuid
from pathlib import Path

from arc.contracts.artifact import ArtifactState
from arc.schemas.artifact import ArtifactDraft, ArtifactRecord


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

        record = ArtifactRecord(
            artifact_id=artifact_id,
            name=draft.name,
            version=version,
            state=ArtifactState.REGISTERED,
            path=str(artifact_path),
            metadata=draft.metadata,
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
        return ArtifactRecord.model_validate_json(record_path.read_text())

    def list_all(self) -> list[ArtifactRecord]:
        records = []
        for record_path in self.root.glob("*/*/arc_record.json"):
            try:
                records.append(ArtifactRecord.model_validate_json(record_path.read_text()))
            except Exception:
                pass
        return records

    def _write_record(self, record: ArtifactRecord) -> None:
        record_path = self.root / record.artifact_id / record.version / "arc_record.json"
        record_path.write_text(json.dumps(record.model_dump(), indent=2))
        meta_path = self.root / record.artifact_id / record.version / "arc_metadata.json"
        meta_path.write_text(json.dumps(record.metadata, indent=2))
