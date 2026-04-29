"""ARC command-line interface."""

import asyncio
import json
import sys

try:
    import typer
    HAS_TYPER = True
except ImportError:
    HAS_TYPER = False

if HAS_TYPER:
    import typer
    app = typer.Typer(name="arc", help="ARC-Sim2L — Autonomous Research Coder")

    @app.command()
    def run(
        goal: str = typer.Argument(..., help="Research goal description"),
        domain: str = typer.Option(None, "--domain", "-d", help="Research domain"),
        iterations: int = typer.Option(1, "--iterations", "-n", help="Number of iterations"),
        provider: str = typer.Option(None, "--provider", "-p",
                                     help="LLM provider: anthropic | openai | openwebui"),
        token: str = typer.Option(None, "--token", "-t",
                                   help="API key or bearer token for the provider"),
        model: str = typer.Option(None, "--model", "-m", help="Model name/ID"),
        base_url: str = typer.Option(None, "--base-url", "-u",
                                      help="Base URL for openwebui/custom endpoints"),
        workflow_name: str = typer.Option("research-loop", "--workflow", "-w",
                                          help="Registered workflow name"),
        output: str = typer.Option(None, "--output", "-o", help="Save results to JSON file"),
    ):
        """Run a research workflow for the given goal."""
        from arc.orchestrator.workflow import ResearchWorkflow
        from arc.schemas.research import ResearchGoal

        goal_obj = ResearchGoal(goal=goal, domain=domain)
        workflow = ResearchWorkflow(
            provider_name=provider,
            token=token,
            model=model,
            base_url=base_url,
            workflow_name=workflow_name,
        )

        async def _run():
            results = []
            for i in range(iterations):
                typer.echo(f"[ARC] Iteration {i + 1}/{iterations}...")
                result = await workflow.run_once(goal_obj)
                results.append(result)
                approved = result.get("review", {}).get("approved", False)
                typer.echo(f"  Status : {result['status']}")
                typer.echo(f"  Approved: {approved}")
                if approved:
                    typer.echo("  Review approved — stopping early.")
                    break
            return results

        results = asyncio.run(_run())

        if output:
            with open(output, "w") as f:
                json.dump(results, f, indent=2, default=str)
            typer.echo(f"Results saved to {output}")
        else:
            typer.echo(json.dumps(results[-1] if results else {}, indent=2, default=str))

    @app.command()
    def models(
        provider: str = typer.Argument(..., help="Provider: openwebui | anthropic | openai"),
        token: str = typer.Option(None, "--token", "-t", help="Bearer token"),
        base_url: str = typer.Option(None, "--base-url", "-u", help="Base URL"),
    ):
        """List available models for a provider."""
        if provider == "openwebui":
            from arc.providers.openwebui.provider import OpenWebUIProvider
            p = OpenWebUIProvider(base_url=base_url, token=token)
            for m in p.list_models():
                typer.echo(m)
        elif provider == "anthropic":
            for m in ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]:
                typer.echo(m)
        elif provider == "openai":
            for m in ["gpt-4.1", "gpt-4o", "gpt-4o-mini"]:
                typer.echo(m)
        else:
            typer.echo(f"Unknown provider: {provider}", err=True)
            raise typer.Exit(1)

    @app.command()
    def serve(
        host: str = typer.Option("0.0.0.0", "--host"),
        port: int = typer.Option(8000, "--port"),
        reload: bool = typer.Option(False, "--reload"),
    ):
        """Start the ARC API server."""
        import uvicorn
        uvicorn.run("arc.api.server:app", host=host, port=port, reload=reload)

    @app.command()
    def info():
        """Show registered components."""
        from arc.core.kernel import Kernel

        kernel = Kernel()
        asyncio.run(kernel.startup())
        typer.echo(f"Agents:    {kernel.registry.list_agents()}")
        typer.echo(f"Skills:    {kernel.registry.list_skills()}")
        typer.echo(f"Workflows: {kernel.registry.list_workflows()}")

else:
    def app():
        print("Install typer to use the ARC CLI: pip install typer")
        sys.exit(1)


if __name__ == "__main__":
    app()
