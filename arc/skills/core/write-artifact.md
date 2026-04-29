# write-artifact

## Description
Registers an `ArtifactDraft` in the artifact registry and returns an `ArtifactRecord`.

## Inputs
- `draft` (ArtifactDraft): The artifact to register.
- `version` (str, default "0.1.0"): Version string to assign.

## Outputs
- `artifact` (ArtifactRecord): The registered record with ID, state=REGISTERED, and path.

## Steps
1. Generate a new UUID for `artifact_id`.
2. Create directory `{registry_root}/{artifact_id}/{version}/`.
3. Write each file from `draft.files` to that directory.
4. Write `arc_record.json` and `arc_metadata.json`.
5. Return the `ArtifactRecord`.
