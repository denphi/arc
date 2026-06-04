# ARC documentation

Sphinx + MyST documentation for ARC, published to ReadTheDocs.

## Build locally

```bash
pip install -r docs/requirements.txt
sphinx-build -b html   docs docs/_build/html        # HTML site
sphinx-build -b linkcheck docs docs/_build/linkcheck # external-link health
# open docs/_build/html/index.html
```

The build mocks optional 3rd-party imports (`anthropic`, `openai`, `chromadb`,
`docker`, `sim2l`, …) — see `autodoc_mock_imports` in `conf.py` — so it builds
without them installed. Install ARC itself (`pip install -e .`) so autodoc can
import `arc.*` for the API reference.

## Where content lives

| Area | Location |
|---|---|
| Site config | `docs/conf.py`, `.readthedocs.yaml` |
| Landing + nav | `docs/index.md` |
| Hand-written prose | `docs/<section>/*.md` (intro, architecture, core, interfaces, reference, guides, packages) |
| Auto API reference | `docs/reference/api/index.md` (renders `arc.*` docstrings) |
| Long-form design topics | `design/*.md` — **authoring source**, included into the docs via `{include}` (architecture, contracts, workflows, strategies, packages, local-packages, extensions, coding-agents). Edit those in `design/`. |
| Diagrams | `docs/_static/*.png` (copied from `design/`) |

## Conventions

- Markdown (MyST) for everything; `{eval-rst}` blocks only for autodoc.
- Keep the build **warning-free** — run `sphinx-build -b html` before pushing.
- New configuration knobs go in `docs/reference/configuration.md`.
- The requirements/plan for the docs is `doc_todo.md` at the repo root.
