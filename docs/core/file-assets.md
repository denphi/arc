# File assets and file loaders

ARC manages local files as session-scoped `FileAsset` records. A file asset is
a stable ID plus metadata and a managed storage path; workflows and skills pass
the ID, not an arbitrary user path.

File loaders turn those managed files into derived assets such as extracted
PDF text, image metadata, table previews, or package-specific domain formats.

## Session inputs

By default, ARC scans `./data` before starting a session. Set
`ARC_INPUTS_DIR` to use a different folder:

```bash
mkdir -p data
# or:
export ARC_INPUTS_DIR=~/arc-inputs
export ARC_INPUTS_IMPORT_MODE=index
export ARC_INPUTS_RECURSIVE=1
export ARC_INPUTS_MAX_FILE_MB=200
arc chat
```

At startup ARC scans metadata only. It registers PDFs as `paper`, images as
`image`, CSV/TSV as `data`, text/Markdown as `text`, and everything else as
`file`. Contents are loaded lazily when a workflow asks for derivatives or the
user runs a loader.

Set `ARC_INPUTS_IMPORT_MODE=copy` when you want startup scan to hash and copy
input files into the managed content-addressed store immediately. The default
`index` mode stores only path/stat/media metadata and materialises a file on
first read or loader use.

## CLI and chat

```bash
arc file add paper24.pdf --role paper --session my-session
arc file list --session my-session
arc file show file_abc123 --session my-session
arc file load file_abc123 --loader pdf_loader --session my-session
```

In chat the same operations are available as:

```text
/file add paper24.pdf paper
/file list
/file show file_abc123
/file load file_abc123 pdf_loader
```

## HTTP API path safety

`POST /files` accepts local server paths, so ARC restricts them by default to
the session inputs directory (`ARC_INPUTS_DIR`, default `./data`) plus any
directories listed in `ARC_FILES_ALLOWED_ROOTS` (separated with `:` on macOS
and Linux, `;` on Windows).

Set `ARC_FILES_TRUSTED_LOCAL=1` only for a private, local server where callers
are allowed to attach arbitrary paths readable by the ARC process. CLI and chat
attachments keep the guard disabled because the local user is supplying their
own path interactively.

## Default loaders

ARC ships safe default loaders:

| Loader | Inputs | Typical derived assets |
|---|---|---|
| `text_loader` | `.txt`, `.md`, `.rst` | `normalized_text` |
| `pdf_loader` | `.pdf` | `extracted_text` |
| `image_loader` | `.png`, `.jpg`, `.jpeg`, `.webp` | `image_metadata` |
| `csv_loader` | `.csv`, `.tsv` | `profile`, `preview` |
| `json_loader` | `.json`, `.jsonl` | `summary` |

Optional dependencies enrich extraction when installed. Without them, loaders
degrade gracefully and record warnings in derived asset metadata.

## Workflow file inputs

Workflow YAML can declare file inputs:

```yaml
inputs:
  paper:
    type: file
    role: paper
    media_type: application/pdf
    required: true
    required_derivatives:
      - role: extracted_text
        media_type: text/markdown
```

Binding rules:

- an explicit `file_*` ID is used directly;
- a local path is imported into the session and replaced with a file ID;
- if no value is supplied, ARC auto-binds only when exactly one session asset
  matches the role/media type;
- ambiguous or missing required files fail before steps execute;
- required derivatives run through enabled loaders before the step receives
  input.

Steps receive IDs:

```json
{
  "paper": "file_abc123",
  "paper_text": "file_def456"
}
```

## Package-provided loaders

Packages add loaders in `package.yaml`:

```yaml
provides:
  loaders:
    - name: scientific_pdf
      path: loaders/scientific_pdf.py
      class: ScientificPdfLoader
```

Loaders implement `can_load(asset)` and `load(asset, context)`. They must read
bytes through `context.file_store.path(asset.id)` and write outputs with
`context.file_store.create_derived(...)`.

Package disable semantics apply: a disabled package's loaders cannot create
new derivatives, though existing derived assets remain available.
