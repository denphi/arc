# Use the API

*Drive a research loop over HTTP. Start the server, then issue requests with a
session id.*

```bash
arc serve                      # → http://localhost:8000/docs
```

If `ARC_API_TOKEN` is set, add `-H "Authorization: Bearer $ARC_API_TOKEN"` to
the data/run requests (see {doc}`../architecture/security`).

## Run a loop iteration

```bash
SID=demo
curl -s -X POST "http://localhost:8000/research/start?session_id=$SID" \
  -H "Content-Type: application/json" \
  -d '{"goal": "Verify parameter doubling", "target": {"result": 2.0}}' | jq .
```

The response includes the proposal, plan, artifact, execution, and review.

## Pick a strategy for the session

```bash
curl -s -X POST "http://localhost:8000/strategies/optimizer?session_id=$SID" \
  -H "Content-Type: application/json" -d '{"impl": "bayesopt"}'
```

## Apply a preset

```bash
curl -s -X POST "http://localhost:8000/presets/bayesian-materials/apply?session_id=$SID" \
  -H "Content-Type: application/json" -d '{}'
```

## Inspect artifacts and results

```bash
curl -s "http://localhost:8000/artifact?session_id=$SID" | jq .
curl -s "http://localhost:8000/results?session_id=$SID" | jq .
```

See {doc}`../interfaces/api` for the full route list.
