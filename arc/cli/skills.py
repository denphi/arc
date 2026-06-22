"""CLI commands for canonical ARC skill bundles."""

from __future__ import annotations

from pathlib import Path

import typer

from arc.core.skill_bundle import load_skill_bundle, validate_skill_bundle

skill_app = typer.Typer(
    name="skill",
    help="Validate and inspect portable SKILL.md bundles.",
    no_args_is_help=True,
)


@skill_app.command("validate")
def validate_skill(
    path: Path = typer.Argument(..., help="Skill bundle directory or SKILL.md path."),
) -> None:
    """Validate an Agent Skills-style bundle."""
    errors = validate_skill_bundle(path)
    if errors:
        for error in errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(1)
    bundle = load_skill_bundle(path)
    typer.echo(f"OK: {bundle.name}")
    typer.echo(f"  description: {bundle.description}")
    typer.echo(f"  resources:   {bundle.list_resources()}")
