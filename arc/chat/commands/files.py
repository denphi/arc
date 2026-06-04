"""``/file`` commands for session FileAssets."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, c, err, ok


def _print_asset(asset) -> None:
    print(
        f"    {c(asset.id, CYAN):<28} "
        f"{(asset.role or '-'):<14} {asset.media_type:<24} {asset.name}"
    )


async def file_handler(state: ChatState, argv: list[str]) -> None:
    if not argv or argv[0] not in {"add", "list", "show", "load"}:
        err("Usage: /file add <path> [role] | /file list | /file show <id> | /file load <id> [loader]")
        return

    action = argv[0]
    workflow = state.workflow
    store = workflow.file_store

    if action == "list":
        assets = store.list(session_id=workflow.session_id)
        if not assets:
            print(c("  No file assets found.", DIM))
            return
        print(c(f"  Session files ({len(assets)}):", BOLD))
        for asset in assets:
            _print_asset(asset)
        return

    if action == "add":
        if len(argv) < 2:
            err("Usage: /file add <path> [role]")
            return
        role = argv[2] if len(argv) >= 3 else None
        try:
            asset = store.import_file(
                argv[1],
                role=role,
                session_id=workflow.session_id,
                metadata={"source": "/file add"},
                copy=True,
            )
        except Exception as exc:  # noqa: BLE001
            err(str(exc))
            return
        ok(f"Attached {asset.name} as {asset.id}.")
        return

    if action == "show":
        if len(argv) != 2:
            err("Usage: /file show <id>")
            return
        try:
            asset = store.get(argv[1])
        except Exception as exc:  # noqa: BLE001
            err(str(exc))
            return
        print(c(f"  {asset.id}", BOLD, CYAN))
        print(f"    name       {asset.name}")
        print(f"    role       {asset.role or '-'}")
        print(f"    media      {asset.media_type}")
        print(f"    bytes      {asset.size_bytes}")
        print(f"    derived    {asset.derived_from or '-'}")
        return

    if action == "load":
        if len(argv) < 2:
            err("Usage: /file load <id> [loader]")
            return
        loader = argv[2] if len(argv) >= 3 else None
        try:
            produced = workflow.load_file_asset(argv[1], loader=loader)
        except Exception as exc:  # noqa: BLE001
            err(str(exc))
            return
        if not produced:
            print(c("  No derived assets produced.", DIM))
            return
        ok(f"Loaded {argv[1]} and created {len(produced)} derived asset(s).")
        for asset in produced:
            _print_asset(asset)


COMMANDS = [
    SlashCommand(
        name="file",
        summary="Attach, list, inspect, or load session files",
        handler=file_handler,
        args_help="add|list|show|load ...",
    ),
]
