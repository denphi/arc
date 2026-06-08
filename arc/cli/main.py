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

    from arc.cli.files import file_app
    from arc.cli.packages import package_app
    from arc.ui.__main__ import DEFAULT_HOST as DEFAULT_UI_HOST
    from arc.ui.__main__ import DEFAULT_PORT as DEFAULT_UI_PORT

    app = typer.Typer(name="arc", help="ARC-Sim2L — Autonomous Research Coder")
    app.add_typer(package_app, name="package")
    app.add_typer(file_app, name="file")

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
        inputs: list[str] = typer.Option(
            [],
            "--input",
            "-i",
            help="Workflow input as key=value; can be supplied multiple times.",
        ),
        build_context: list[str] = typer.Option(
            [],
            "--build-context",
            help="Pre-build context workflow name; can be supplied multiple times.",
        ),
        output: str = typer.Option(None, "--output", "-o", help="Save results to JSON file"),
    ):
        """Run a research workflow for the given goal."""
        from arc.orchestrator.workflow import ResearchWorkflow
        from arc.schemas.research import ResearchGoal

        constraints = {}
        for item in inputs or []:
            if "=" not in item:
                typer.echo(f"Invalid --input {item!r}; expected key=value", err=True)
                raise typer.Exit(1)
            key, value = item.split("=", 1)
            constraints[key] = value
        goal_obj = ResearchGoal(goal=goal, domain=domain, constraints=constraints)
        workflow = ResearchWorkflow(
            provider_name=provider,
            token=token,
            model=model,
            base_url=base_url,
            workflow_name=workflow_name,
        )
        if build_context:
            workflow._context.memory["build_context_workflows"] = list(build_context)

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
        from arc.orchestrator.workflow import _default_registry
        from arc.providers import build_provider
        p = build_provider(
            provider, token=token, base_url=base_url, registry=_default_registry(),
        )
        lister = getattr(p, "list_models", None) if p else None
        if not callable(lister):
            typer.echo(f"Unknown provider: {provider}", err=True)
            raise typer.Exit(1)
        for m in lister():
            typer.echo(m)

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
    def ui(
        host: str = typer.Option(DEFAULT_UI_HOST, "--host", help="Host interface to bind"),
        port: int = typer.Option(DEFAULT_UI_PORT, "--port", help="Port for the browser UI"),
        reload: bool = typer.Option(False, "--reload", help="Reload on code changes"),
    ):
        """Start the standalone ARC browser UI."""
        from arc.ui.__main__ import run_server

        run_server(host=host, port=port, reload=reload)

    @app.command()
    def info():
        """Show registered components + each package's declared config."""
        import os

        from arc.core.kernel import Kernel

        kernel = Kernel()
        asyncio.run(kernel.startup())
        typer.echo(f"Agents:    {kernel.registry.list_agents()}")
        typer.echo(f"Skills:    {kernel.registry.list_skills()}")
        typer.echo(f"Workflows: {kernel.registry.list_workflows()}")
        load_errors = kernel.registry.list_load_errors()
        if load_errors:
            typer.echo("\nPackage load errors:")
            for item in load_errors:
                name = f" {item['name']}" if item.get("name") else ""
                typer.echo(
                    f"  {item['package']} {item['kind']}{name}: {item['error']}"
                )

        # Declared package config: what each package reads from .env / env,
        # and whether it's currently set. Secret values are masked.
        typer.echo("\nPackage config (.env / environment):")
        any_config = False
        for pkg_name in kernel.registry.list_packages():
            manifest = kernel.registry.get_package(pkg_name)
            entries = (manifest or {}).get("config") or []
            if not entries:
                continue
            any_config = True
            typer.echo(f"  {pkg_name}:")
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                var = entry["name"]
                raw = os.environ.get(var)
                if raw:
                    shown = "********" if entry.get("secret") else raw
                    status = f"set ({shown})"
                else:
                    req = " REQUIRED" if entry.get("required") else ""
                    status = f"unset{req}"
                desc = entry.get("description", "")
                typer.echo(f"    {var:<28} {status}{('  — ' + desc) if desc else ''}")
        if not any_config:
            typer.echo("  (no packages declare config)")

    @app.command()
    def chat(
        provider: str = typer.Option(None, "--provider", "-p",
                                     help="LLM provider: anthropic | openai | openwebui"),
        token: str = typer.Option(None, "--token", "-t",
                                   help="API key / bearer token for the provider"),
        model: str = typer.Option(None, "--model", "-m", help="Model name/ID"),
        url: str = typer.Option(None, "--url", "-u", help="Provider base URL"),
        stub: bool = typer.Option(False, "--stub", help="Run without an LLM (for testing)"),
        session: str = typer.Option(None, "--session", "-s",
                                     help="Resume an existing session ID"),
        list_sessions_flag: bool = typer.Option(False, "--list-sessions",
                                                 help="List all sessions and exit"),
        delete_session_id: str = typer.Option(None, "--delete-session", metavar="SESSION_ID",
                                               help="Delete a specific session and exit"),
        delete_all: bool = typer.Option(False, "--delete-all-sessions",
                                         help="Delete ALL sessions and exit"),
        max_iterations: int = typer.Option(20, "--max-iterations",
                                            help="Max auto-iterations per goal (default 20)"),
        check: bool = typer.Option(False, "--check",
                                    help="Dry-run: report config / service / auth status and exit"),
        check_format: str = typer.Option("ansi", "--check-format",
                                          help="Format for --check output: ansi | json"),
        plan: bool = typer.Option(False, "--plan",
                                   help=(
                                       "Plan mode: show what the chat would do without writing "
                                       "files or pushing to sim2l"
                                   )),
        events: str = typer.Option("ansi", "--events",
                                    help="Event sink: ansi (default), jsonl, stdout-json, multi"),
        events_path: str = typer.Option(None, "--events-path",
                                         help="When --events=jsonl|multi, the file to write to "
                                              "(default: <session_dir>/events.jsonl)"),
        build_context: list[str] = typer.Option(
            [],
            "--build-context",
            help="Pre-build context workflow name; can be supplied multiple times.",
        ),
    ):
        """Start the interactive ARC research chat."""
        if plan:
            from arc.chat.plan_mode import set_plan_mode
            set_plan_mode(True)
            print("⚑ Plan mode active: no files will be written, no sim2l pushes.")

        # --check is a dry-run that never enters the REPL — short-circuit
        # before installing any chat sinks so its JSON output stays clean.
        if check:
            import asyncio as _asyncio

            from arc.chat.check import run_check
            from arc.chat.check_render import render
            report = _asyncio.run(run_check(
                provider=provider, token=token, base_url=url, model=model,
            ))
            output = render(report, fmt=check_format)  # type: ignore[arg-type]
            print(output)
            raise typer.Exit(report.exit_code)

        # ── Event sink wiring ───────────────────────────────────────────
        # For ANSI and stdout-json we can install the sink now (no
        # session path needed). For jsonl / multi we defer to chat_loop
        # so the default path can resolve to <session_dir>/events.jsonl.
        from pathlib import Path

        from arc.chat.events import (
            AnsiSink,
            SinkConfig,
            StdoutJsonSink,
            set_sink,
            set_sink_config,
        )
        if events == "ansi":
            set_sink(AnsiSink())
        elif events == "stdout-json":
            set_sink(StdoutJsonSink())
        elif events in ("jsonl", "multi"):
            override = Path(events_path) if events_path else None
            set_sink_config(SinkConfig(kind=events, path=override))
            # chat_loop will print the resolved path once it's known.
        else:
            print(f"unknown --events value {events!r}; expected one of "
                  f"ansi, jsonl, stdout-json, multi", file=sys.stderr)
            raise typer.Exit(2)

        # Reconstruct argv so arc.chat.main()'s argparse sees the right flags.
        args = []
        if provider:
            args += ["--provider", provider]
        if token:
            args += ["--token", token]
        if model:
            args += ["--model", model]
        if url:
            args += ["--url", url]
        if stub:
            args += ["--stub"]
        if session:
            args += ["--session", session]
        if list_sessions_flag:
            args += ["--list-sessions"]
        if delete_session_id:
            args += ["--delete-session", delete_session_id]
        if delete_all:
            args += ["--delete-all-sessions"]
        for name in build_context or []:
            args += ["--build-context", name]
        args += ["--max-iterations", str(max_iterations)]

        sys.argv = [sys.argv[0]] + args
        from arc.chat import main
        main()

else:
    def app():
        print("Install typer to use the ARC CLI: pip install typer")
        sys.exit(1)


if __name__ == "__main__":
    app()
