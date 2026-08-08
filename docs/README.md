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
| Public summaries | Keep them in `docs/<section>/*.md`. |
| Diagrams | `docs/_static/*.png` |

## Conventions

- Markdown (MyST) for everything; `{eval-rst}` blocks only for autodoc.
- Keep the build **warning-free** — run `sphinx-build -b html` before pushing.
- New configuration knobs go in `docs/reference/configuration.md`.
- Build strictly (`sphinx-build -b html -W --keep-going docs docs/_build/html`);
  there is no CI, so that command is the only gate.
