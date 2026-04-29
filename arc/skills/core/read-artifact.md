# read-artifact

## Description
Reads an artifact record from the artifact registry by ID and version.

## Inputs
- `artifact_id` (str): The artifact UUID.
- `version` (str, default "0.1.0"): The artifact version.

## Outputs
- `artifact` (ArtifactRecord): The artifact record with path and metadata.

## Steps
1. Look up `artifact_id/version/arc_record.json` in the artifact registry root.
2. Parse and return as `ArtifactRecord`.
3. Raise `NotFound` if the artifact does not exist.
