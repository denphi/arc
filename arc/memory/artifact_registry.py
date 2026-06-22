import json
import logging
import os
import re
import uuid
from pathlib import Path

from arc.contracts.artifact import ArtifactState
from arc.schemas.artifact import ArtifactDraft, ArtifactRecord
from arc.sim2l_schema import load_sim2l_schema
# Review item #T22: ``_atomic_write_text`` moved to ``arc.utils.io`` so
# other callers (results store, provenance log) can reuse it.
from arc.utils.io import atomic_write_text as _atomic_write_text

logger = logging.getLogger(__name__)


# Path-safe identifier matcher (review item #T2). artifact_id is a uuid4
# generated server-side so it always matches; ``version`` is caller-supplied
# and must be validated before being interpolated into a directory path.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
        raise ValueError(
            f"Unsafe {label}: {value!r}. Must match {_SAFE_ID_RE.pattern}."
        )
    return value


class ArtifactRegistry:
    """File-based artifact registry. Stores artifact files and metadata."""

    def __init__(self, root: str = "workspace/artifacts"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, draft: ArtifactDraft, version: str = "0.1.0") -> ArtifactRecord:
        # Plan-mode gate: in --plan mode the chat must not write any
        # artifact files. Importing here avoids a hard dep on arc.chat
        # from the memory layer.
        try:
            from arc.chat.plan_mode import check_plan_gate
            check_plan_gate(
                "artifact registration",
                hint=f"would create files under {self.root}/<uuid>/{version}/",
            )
        except ImportError:
            pass  # arc.chat may not be importable in non-chat contexts

        _validate_safe_id(version, "version")
        artifact_id = str(uuid.uuid4())
        artifact_path = self.root / artifact_id / version
        artifact_path.mkdir(parents=True, exist_ok=True)

        base = artifact_path.resolve()
        for filename, content in draft.files.items():
            target = (artifact_path / filename).resolve()
            if target == base or base not in target.parents:
                raise ValueError(f"Unsafe artifact filename: {filename}")
            # Refuse to follow a pre-existing symlink at ``target``. The
            # artifact_id is a fresh uuid4 so the directory was just created
            # empty, but a symlink could still be planted by anything with
            # write access to ``self.root``. Review item #A11.
            if target.is_symlink():
                raise ValueError(
                    f"Unsafe artifact filename: {filename} resolves through "
                    f"an existing symlink at {target}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            # ``O_NOFOLLOW`` would be ideal, but Path.write_text doesn't
            # support it. The is_symlink() check above is sufficient when
            # nobody else writes into this directory between the check and
            # the create.
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
        # Both flow from HTTP request input into directory components — must
        # be validated against path-traversal (review item #T2).
        _validate_safe_id(artifact_id, "artifact_id")
        _validate_safe_id(version, "version")
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

    def save(self, record: ArtifactRecord) -> ArtifactRecord:
        """Persist mutations made to a record in memory back to disk.

        Used after the curator rewrites ``description`` / ``metadata``
        (e.g. the capability summary) so ``arc_record.json`` on disk — and
        thus the GitHub backend's published copy — carries the same fields
        the live catalog push already received.
        """
        self._write_record(record)
        return record

    def _write_record(self, record: ArtifactRecord) -> None:
        record_path = self.root / record.artifact_id / record.version / "arc_record.json"
        meta_path = self.root / record.artifact_id / record.version / "arc_metadata.json"
        _atomic_write_text(record_path, json.dumps(record.model_dump(), indent=2))
        _atomic_write_text(meta_path, json.dumps(record.metadata, indent=2))

    def _metadata_with_schema(self, metadata: dict, artifact_path: Path) -> dict:
        enriched = dict(metadata or {})
        inputs, outputs = load_sim2l_schema(artifact_path)
        if inputs and not enriched.get("sim2l_inputs"):
            enriched["sim2l_inputs"] = inputs
        if outputs and not enriched.get("sim2l_outputs"):
            enriched["sim2l_outputs"] = outputs
        return enriched
