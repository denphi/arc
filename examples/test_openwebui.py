#!/usr/bin/env python3
"""
Quick test: run one ARC research iteration using OpenWebUI / Purdue GenAI.

Usage
-----
# From arc/ directory:

python3 examples/test_openwebui.py \
    --token  YOUR_BEARER_TOKEN \
    --model  llama3.3:70b \
    --url    https://genai.rcac.purdue.edu/api

# Without a token (stub mode — no LLM calls, deterministic output):
python3 examples/test_openwebui.py --stub
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Make sure the project root is on sys.path when run directly.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from arc.orchestrator.workflow import ResearchWorkflow
from arc.schemas.research import ResearchGoal


GOAL = "Explore how the input parameter affects the simulation output"


async def run(provider, token, model, base_url):
    print(f"\n{'='*60}")
    print(f"  ARC test run")
    print(f"  Provider : {provider or 'stub (no LLM)'}")
    print(f"  Model    : {model or 'auto'}")
    print(f"  Base URL : {base_url or 'n/a'}")
    print(f"  Goal     : {GOAL}")
    print(f"{'='*60}\n")

    workflow = ResearchWorkflow(
        provider_name=provider,
        token=token,
        model=model,
        base_url=base_url,
    )

    goal = ResearchGoal(goal=GOAL, domain="computational science")
    result = await workflow.run_once(goal)

    print("── Proposal ──────────────────────────────────────────────")
    print(f"  Hypothesis : {result['proposal']['hypothesis']}")
    print(f"  Objective  : {result['proposal']['objective']}")

    print("\n── Plan ──────────────────────────────────────────────────")
    print(f"  Parameters : {result['plan']['parameters']}")
    print(f"  Sweep      : {result['plan']['parameter_sweep']}")

    print("\n── Artifact ──────────────────────────────────────────────")
    print(f"  ID         : {result['artifact']['artifact_id']}")
    print(f"  Path       : {result['artifact']['path']}")

    print("\n── Execution ─────────────────────────────────────────────")
    print(f"  Run ID     : {result['execution']['run_id']}")
    print(f"  Status     : {result['execution']['status']}")
    print(f"  Outputs    : {result['execution']['outputs']}")

    print("\n── Review ────────────────────────────────────────────────")
    print(f"  Approved   : {result['review']['approved']}")
    print(f"  Summary    : {result['review']['summary']}")

    print("\n── Full result (JSON) ────────────────────────────────────")
    print(json.dumps(result, indent=2, default=str))

    return result


def main():
    parser = argparse.ArgumentParser(description="ARC OpenWebUI test")
    parser.add_argument("--token", default=None, help="Bearer token for the gateway")
    parser.add_argument("--model", default=None, help="Model name (e.g. llama3.3:70b)")
    parser.add_argument("--url", default="https://genai.rcac.purdue.edu/api",
                        help="Base URL of the OpenWebUI endpoint")
    parser.add_argument("--stub", action="store_true",
                        help="Run without LLM (deterministic stub mode)")
    args = parser.parse_args()

    if args.stub:
        provider = token = model = base_url = None
    else:
        if not args.token:
            print("ERROR: --token is required unless you use --stub", file=sys.stderr)
            print("       Get your token from https://genai.rcac.purdue.edu", file=sys.stderr)
            sys.exit(1)
        provider = "openwebui"
        token = args.token
        model = args.model
        base_url = args.url

    asyncio.run(run(provider, token, model, base_url))


if __name__ == "__main__":
    main()
