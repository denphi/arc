"""``/skills`` — list, view, or remove session skills.

The ``skill_extracting`` reflector strategy writes a markdown file per
actionable review into ``~/.sim2l/<session-id>/skills/learned/``. This
command surfaces those files so the user can curate them.

Usage:
    /skills                list every skill file in this session
    /skills show <name>    print the markdown body of one skill
    /skills delete <name>  remove a skill file (asks to confirm)

``<name>`` matches the filename without ``.md``; a unique prefix is
accepted so users can refer to ``design-silicon`` instead of the full
``design-silicon-89461a8b``.
"""

from __future__ import annotations

import re
from pathlib import Path

from arc.chat.input import chat_input_async
from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, GREEN, RED, c, err, header, ok, step, warn

_H1_RE = re.compile(r"^#\s+([^\n]+)", re.MULTILINE)


def _skills_dir(state: ChatState) -> Path | None:
    """Resolve ``<session>/skills/learned/`` without raising.

    Read-only: returns ``None`` when the directory doesn't yet exist
    so list / show / delete / export can detect "no skills here yet"
    cleanly. Import uses :func:`_ensure_skills_dir` instead.
    """
    try:
        from arc.session import session_paths
        paths = session_paths(state.session_id)
        base = Path(paths["artifacts"]).parent
        target = base / "skills" / "learned"
        if target.exists():
            return target
    except Exception:
        pass
    return None


def _ensure_skills_dir(state: ChatState) -> Path | None:
    """Resolve ``<session>/skills/learned/``, creating it if needed.

    Returns ``None`` when the session path can't be resolved (e.g. an
    unsafe session id). Used by :func:`_import` so a brand-new session
    can receive a shared skill library without the user having to first
    run the skill-extracting reflector.
    """
    try:
        from arc.session import session_paths
        paths = session_paths(state.session_id)
        base = Path(paths["artifacts"]).parent
        target = base / "skills" / "learned"
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception:
        return None


def _list_files(target: Path) -> list[Path]:
    from arc.core.skill_library import list_skill_entries
    return list_skill_entries(target)


def _entry_name(path: Path) -> str:
    from arc.core.skill_library import skill_entry_name
    return skill_entry_name(path)


def _first_h1(text: str) -> str:
    match = _H1_RE.search(text)
    return match.group(1).strip() if match else ""


def _match(files: list[Path], name: str) -> Path | list[Path]:
    """Resolve ``name`` to a single file. Returns the file on a unique
    match, or the list of candidates on an ambiguous prefix."""
    stem_name = name.removesuffix(".md")
    exact = [p for p in files if _entry_name(p) == stem_name]
    if exact:
        return exact[0]
    prefix = [p for p in files if _entry_name(p).startswith(stem_name)]
    if len(prefix) == 1:
        return prefix[0]
    return prefix


def _list(state: ChatState) -> None:
    target = _skills_dir(state)
    if target is None:
        print(c("  No skills directory for this session yet.", DIM))
        print(c("  Tip: /strategy reflector skill_extracting enables the "
                "skill-extracting reflector,", DIM))
        print(c("       which writes one file per actionable review.", DIM))
        return
    files = _list_files(target)
    if not files:
        print(c("  No skills written in this session yet.", DIM))
        return

    header(f"Learned skills ({len(files)})")
    print(c(f"  {target}", DIM))
    print()
    for path in files:
        try:
            head = _first_h1(path.read_text(encoding="utf-8"))
        except OSError:
            head = "(unreadable)"
        # Indent + colour. Use file stem (no .md) as the user-facing handle.
        print(f"  {c(_entry_name(path), CYAN, BOLD)}")
        if head:
            print(f"    {c(head, DIM)}")
    print()
    print(c("  /skills show <name> to view, /skills delete <name> to remove.", DIM))


def _show(state: ChatState, name: str) -> None:
    target = _skills_dir(state)
    if target is None:
        err("No skills directory for this session.")
        return
    files = _list_files(target)
    match = _match(files, name)
    if isinstance(match, list):
        if not match:
            err(f"No skill matching {name!r}.")
            return
        warn(f"Ambiguous: {name!r} matches multiple skills:")
        for p in match:
            print(f"    {c('•', CYAN)} {p.stem}")
        return

    try:
        body = match.read_text(encoding="utf-8")
    except OSError as exc:
        err(f"Could not read {match.name}: {exc}")
        return

    header(f"Skill: {_entry_name(match)}")
    print(c(f"  {match}", DIM))
    print()
    for line in body.splitlines():
        print(f"  {line}")


def _default_export_dir() -> Path:
    """Default target for ``/skills export``: ``<SIM2L_HOME>/shared/skills``.

    Picking a location under ``SIM2L_HOME`` keeps user-level skill
    sharing alongside other arc state instead of inventing a new top-
    level directory. Tests rely on the env var being set by the global
    pytest fixture.
    """
    import os
    base = Path(os.environ.get("SIM2L_HOME", str(Path.home() / ".sim2l" / "code")))
    return base / "shared" / "skills"


def _export(state: ChatState, target_arg: str | None, *, force: bool) -> None:
    """Copy ``<session>/skills/learned/*.md`` to a shared directory.

    Conflict policy:
      * Identical content → silent skip (already exported).
      * Different content + ``--force`` → overwrite, warn.
      * Different content, no force → skip + warn.
    """
    source = _skills_dir(state)
    if source is None:
        err("No skills directory for this session.")
        return
    files = _list_files(source)
    if not files:
        warn("No skills in this session to export.")
        return

    target = Path(target_arg) if target_arg else _default_export_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        err(f"Could not create export directory {target}: {exc}")
        return

    copied: list[str] = []
    skipped_same: list[str] = []
    skipped_conflict: list[str] = []
    overwritten: list[str] = []

    from arc.core.skill_library import copy_skill_entry
    for src in files:
        name = _entry_name(src)
        try:
            status = copy_skill_entry(src, target, force=force)
        except OSError as exc:
            err(f"Could not export {name}: {exc}")
            continue
        {"copied": copied, "same": skipped_same, "conflict": skipped_conflict,
         "overwritten": overwritten}[status].append(name)

    ok(f"Exported to {c(str(target), CYAN)}")
    if copied:
        step("Copied",      f"{len(copied)} new")
        for name in copied:
            print(f"    {c('+', GREEN)} {name}")
    if overwritten:
        step("Overwritten", f"{len(overwritten)} (existed with different content)")
        for name in overwritten:
            print(f"    {c('~', CYAN)} {name}")
    if skipped_same:
        step("Skipped",     f"{len(skipped_same)} identical (already exported)")
    if skipped_conflict:
        step(
            "Conflicts",
            f"{len(skipped_conflict)} differ from existing files "
            f"(re-run with --force to overwrite)",
        )
        for name in skipped_conflict:
            print(f"    {c('!', RED)} {name}")


def _import(state: ChatState, source_arg: str | None, *, force: bool) -> None:
    """Copy markdown skills *into* this session from a shared directory.

    Symmetric counterpart of :func:`_export`. Source defaults to
    ``<SIM2L_HOME>/shared/skills`` so the round-trip
    export → import-into-new-session is the obvious thing.

    Conflict policy mirrors ``_export`` so users learn one model:
      * Identical content already here → silent skip.
      * Different content + ``--force`` → overwrite, warn.
      * Different content, no force → skip + warn.

    Unlike ``_export``, we create the destination on demand — a brand-
    new session may not have a ``skills/learned/`` directory yet, but
    the user can still seed it from a shared library.
    """
    source = Path(source_arg) if source_arg else _default_export_dir()
    if not source.exists():
        err(
            f"Source directory does not exist: {source}\n"
            "    Tip: /skills export from another session populates "
            "this directory."
        )
        return

    incoming = _list_files(source)
    if not incoming:
        warn(f"No skills found in {source}.")
        return

    target = _ensure_skills_dir(state)
    if target is None:
        err("Could not resolve a skills directory for this session.")
        return

    copied: list[str] = []
    skipped_same: list[str] = []
    skipped_conflict: list[str] = []
    overwritten: list[str] = []

    from arc.core.skill_library import copy_skill_entry
    for src in incoming:
        name = _entry_name(src)
        try:
            status = copy_skill_entry(src, target, force=force)
        except OSError as exc:
            err(f"Could not import {name}: {exc}")
            continue
        {"copied": copied, "same": skipped_same, "conflict": skipped_conflict,
         "overwritten": overwritten}[status].append(name)

    ok(f"Imported into {c(str(target), CYAN)}")
    step("From", str(source))
    if copied:
        step("Copied",      f"{len(copied)} new")
        for name in copied:
            print(f"    {c('+', GREEN)} {name}")
    if overwritten:
        step("Overwritten", f"{len(overwritten)} (existed with different content)")
        for name in overwritten:
            print(f"    {c('~', CYAN)} {name}")
    if skipped_same:
        step("Skipped",     f"{len(skipped_same)} identical (already present)")
    if skipped_conflict:
        step(
            "Conflicts",
            f"{len(skipped_conflict)} differ from session-local files "
            f"(re-run with --force to overwrite)",
        )
        for name in skipped_conflict:
            print(f"    {c('!', RED)} {name}")


async def _delete(state: ChatState, name: str) -> None:
    target = _skills_dir(state)
    if target is None:
        err("No skills directory for this session.")
        return
    files = _list_files(target)
    match = _match(files, name)
    if isinstance(match, list):
        if not match:
            err(f"No skill matching {name!r}.")
            return
        warn(f"Ambiguous: {name!r} matches multiple skills:")
        for p in match:
            print(f"    {c('•', CYAN)} {p.stem}")
        return

    # Destructive — confirm. The y/N prompt mirrors the rest of the chat UX.
    try:
        confirm = (await chat_input_async(
            c(f"  Delete {_entry_name(match)}? [y / N] > ", BOLD)
        )).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if confirm not in ("y", "yes"):
        print(c("  Cancelled.", DIM))
        return

    try:
        from arc.core.skill_library import delete_skill_entry
        delete_skill_entry(match)
    except OSError as exc:
        err(f"Could not delete {match.name}: {exc}")
        return
    ok(f"Removed {c(_entry_name(match), CYAN)}")


async def run(state: ChatState, argv: list[str]) -> None:
    if not argv:
        _list(state)
        return

    sub = argv[0].lower()
    rest = argv[1:]

    if sub == "list":
        _list(state)
        return
    if sub == "show":
        if not rest:
            err("Usage: /skills show <name>")
            return
        _show(state, rest[0])
        return
    if sub in ("delete", "remove", "rm"):
        if not rest:
            err("Usage: /skills delete <name>")
            return
        await _delete(state, rest[0])
        return
    if sub == "export":
        force = "--force" in rest
        positional = [r for r in rest if not r.startswith("--")]
        target = positional[0] if positional else None
        _export(state, target, force=force)
        return
    if sub == "import":
        force = "--force" in rest
        positional = [r for r in rest if not r.startswith("--")]
        source = positional[0] if positional else None
        _import(state, source, force=force)
        return

    # If no recognised subcommand, treat the first arg as a name to show.
    _show(state, argv[0])


COMMANDS = [
    SlashCommand(
        name="skills",
        summary="List, view, remove, export, or import the reflector-saved skills.",
        handler=run,
        args_help="[list|show <n>|delete <n>|export [<dir>]|import [<dir>]]",
    ),
]
