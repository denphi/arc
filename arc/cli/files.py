"""CLI commands for ARC FileAssets."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from arc.orchestrator.workflow import ResearchWorkflow


file_app = typer.Typer(
    name="file",
    help="Attach, inspect, and load session files.",
    no_args_is_help=True,
)


def _workflow(session_id: str) -> ResearchWorkflow:
    return ResearchWorkflow(session_id=session_id)


def _print_asset(asset) -> None:
    typer.echo(
        f"{asset.id:<18} {asset.role or '-':<14} {asset.media_type:<24} "
        f"{asset.size_bytes:>8}  {asset.name}"
    )


@file_app.command("add")
def add_file(
    path: Path = typer.Argument(..., help="Local file path to attach."),
    role: str = typer.Option(None, "--role", "-r", help="Semantic role, e.g. paper/data/image."),
    session: str = typer.Option("default", "--session", "-s", help="ARC session id."),
    copy: bool = typer.Option(True, "--copy/--index", help="Copy bytes into ARC storage."),
):
    """Attach a local file to an ARC session."""
    workflow = _workflow(session)
    asset = workflow.file_store.import_file(
        path,
        role=role,
        session_id=session,
        metadata={"source": "arc file add"},
        copy=copy,
    )
    _print_asset(asset)


@file_app.command("list")
def list_files(
    session: str = typer.Option("default", "--session", "-s", help="ARC session id."),
    role: str = typer.Option(None, "--role", "-r", help="Filter by role."),
):
    """List files attached to a session."""
    workflow = _workflow(session)
    assets = workflow.file_store.list(session_id=session, role=role)
    if not assets:
        typer.echo("No file assets found.")
        return
    typer.echo(f"{'ID':<18} {'ROLE':<14} {'MEDIA TYPE':<24} {'BYTES':>8}  NAME")
    for asset in assets:
        _print_asset(asset)


@file_app.command("show")
def show_file(
    file_id: str = typer.Argument(..., help="File asset id."),
    session: str = typer.Option("default", "--session", "-s", help="ARC session id."),
):
    """Show metadata for one file asset."""
    workflow = _workflow(session)
    asset = workflow.file_store.get(file_id)
    typer.echo(json.dumps(asset.to_dict(), indent=2, sort_keys=True))


@file_app.command("load")
def load_file(
    file_id: str = typer.Argument(..., help="File asset id."),
    loader: str = typer.Option(None, "--loader", "-l", help="Specific loader name."),
    session: str = typer.Option("default", "--session", "-s", help="ARC session id."),
):
    """Run an enabled loader and register derived file assets."""
    workflow = _workflow(session)
    produced = workflow.load_file_asset(file_id, loader=loader)
    if not produced:
        typer.echo("No derived assets produced.")
        return
    typer.echo(f"{'ID':<18} {'ROLE':<14} {'MEDIA TYPE':<24} {'BYTES':>8}  NAME")
    for asset in produced:
        _print_asset(asset)
