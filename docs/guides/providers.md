# Configure providers

*Turn on LLM agents. Without a provider, ARC runs in stub mode (deterministic,
offline) — which is the default and the test mode.*

## Stub mode (default)

No configuration. Every agent uses a no-LLM path; the loop runs offline and
reproducibly. Good for tests, demos, and CI.

## OpenWebUI / OpenAI-compatible gateways (core built-in)

Works with OpenWebUI, Purdue GenAI, vLLM, **and Ollama** (point `base_url` at
the Ollama endpoint):

```bash
export ARC_PROVIDER=openwebui
export OPENWEBUI_URL=https://genai.rcac.purdue.edu/api
export OPENWEBUI_KEY=your-bearer-token
export OPENWEBUI_MODEL=llama3.3:70b        # or leave blank to auto-select
```

## Anthropic / OpenAI (the `arc-providers` package)

```bash
pip install 'arc[anthropic]'   # or 'arc[openai]'

export ARC_PROVIDER=anthropic
export ANTHROPIC_API_KEY=…
export ARC_MODEL=claude-opus-4-7
```

```bash
export ARC_PROVIDER=openai
export OPENAI_API_KEY=…
export ARC_MODEL=gpt-4.1
```

## Per-command overrides

Every interface accepts inline overrides without env vars:

```bash
arc run "…"  --provider openwebui --model gpt-oss:120b --base-url https://… --token "$KEY"
arc chat     --provider anthropic --model claude-opus-4-7 --token "$ANTHROPIC_API_KEY"
```

## Check it

```bash
arc chat --check                 # reports provider/service/auth status
arc models openwebui --base-url https://… --token "$KEY"
```

See {doc}`../reference/configuration` for every provider variable, and
{doc}`../core/providers` for how resolution + stub mode work.
