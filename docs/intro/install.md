# Installation

*Supported Python, base install, optional extras, and a development setup.*

## Requirements

- **Python ≥ 3.10** (3.11 recommended; ReadTheDocs and CI build on 3.11).
- An OS with a working `multiprocessing` spawn start method (Linux/macOS/
  Windows). ARC runs generated `simulate()` code in a spawned subprocess.

## Base install

ARC's core depends only on a small, pure-Python set: FastAPI, Uvicorn,
pydantic, python-dotenv, prompt-toolkit, httpx, and PyYAML.

```bash
pip install arc
```

From a checkout (editable, for development):

```bash
git clone <repo> && cd arc
pip install -e .
```

This installs the `arc` console script (see {doc}`../interfaces/cli`).

## Optional extras

LLM providers and some runtime adapters / extensions are **optional** — ARC
runs in stub mode without them. Install only what you need:

| Extra | Pulls in | Enables |
|---|---|---|
| `anthropic` | `anthropic` SDK | the Anthropic provider (`arc-providers`) |
| `openai` | `openai` SDK | the OpenAI provider (`arc-providers`) |
| `openwebui` | `openai` SDK | the core `openwebui` provider (OpenAI-compatible gateways, Ollama) |
| `vector` | `chromadb` | the Chroma backend for `arc-vector-memory` |
| `docker` | `docker` SDK | the `arc-docker` runtime adapter |
| `sim2l` | `sim2l` | the Sim2L runtime adapter + service backends |
| `all` | anthropic + openai + typer | everything common |
| `dev` | pytest, ruff, mypy | running the test suite + linters |

```bash
pip install 'arc[anthropic]'        # one provider
pip install 'arc[all]'              # the common set
pip install -e '.[dev]'             # development
```

## Verify the install

```bash
arc --help
arc run "Verify that a parameter doubles to produce a result"   # stub-mode loop
```

A stub-mode run requires no API keys and no network. If it completes, your
install is good. Next: {doc}`quickstart`.

## Building the docs locally

```bash
pip install -r docs/requirements.txt
sphinx-build -b html docs docs/_build/html
# open docs/_build/html/index.html
```

The docs build mocks the optional 3rd-party imports (see `docs/conf.py`
`autodoc_mock_imports`), so they build without `anthropic`/`openai`/`sim2l`/
etc. installed.
