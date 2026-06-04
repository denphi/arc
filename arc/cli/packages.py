"""CLI helpers for creating and checking ARC packages."""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import typer
import yaml

package_app = typer.Typer(
    name="package",
    help="Create and validate local ARC packages.",
    no_args_is_help=True,
)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    if not slug:
        raise typer.BadParameter("Package name cannot be empty")
    return slug


def _snake(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_").lower()


def _pascal(value: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", value)
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def _package_name(value: str) -> str:
    name = _slug(value)
    return name if name.startswith("arc-") else f"arc-{name}"


def _ensure_empty_or_new(path: Path, *, force: bool) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        raise typer.BadParameter(f"{path} exists and is not a directory")
    existing = [p.name for p in path.iterdir() if p.name != ".DS_Store"]
    if existing and not force:
        raise typer.BadParameter(
            f"{path} is not empty. Re-run with --force to add missing scaffold files."
        )


def _write_new(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@package_app.command("init")
def init_package(
    name: str = typer.Argument(..., help="Package name, for example arc-my-lab or my-lab."),
    path: Path | None = typer.Argument(
        None,
        help="Target folder. Defaults to ./<package-name>.",
    ),
    role: str = typer.Option("ideator", "--role", "-r", help="Initial strategy role."),
    strategy: str | None = typer.Option(
        None,
        "--strategy",
        "-s",
        help="Initial strategy name. Defaults to <package>_<role>.",
    ),
    description: str | None = typer.Option(
        None,
        "--description",
        "-d",
        help="Short package description.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite scaffold files if present."),
) -> None:
    """Create a local package scaffold that ARC can load from package paths."""
    pkg_name = _package_name(name)
    target = path or Path(pkg_name)
    role_name = _snake(role)
    strategy_name = _snake(strategy or f"{pkg_name.removeprefix('arc-')}_{role_name}")
    class_name = f"{_pascal(strategy_name)}Agent"
    pkg_description = description or f"Local ARC package {pkg_name}."

    _ensure_empty_or_new(target, force=force)
    target.mkdir(parents=True, exist_ok=True)

    manifest = dedent(
        f"""\
        name: {pkg_name}
        version: 0.1.0
        type: local-package
        description: {pkg_description}

        provides:
          agents:
            - name: {strategy_name}
              path: agents/{role_name}.py
              class: {class_name}
          strategies:
            - role: {role_name}
              name: {strategy_name}
              path: agents/{role_name}.py
              class: {class_name}
              description: {pkg_description}
          prompts:
            - prompts/{role_name}.md
          skills:
            - skills/{strategy_name}.md
        """
    )
    agent = dedent(
        f'''\
        """Starter ARC agent for {pkg_name}."""

        from typing import Any

        from arc.contracts.agent import AgentContract


        class {class_name}(AgentContract):
            name = "{strategy_name}"
            description = "{pkg_description}"

            async def run(self, input_data: Any) -> dict[str, Any]:
                return {{
                    "package": "{pkg_name}",
                    "strategy": self.name,
                    "input": input_data,
                    "status": "stub",
                    "notes": "Replace this stub with your package behavior.",
                }}
        '''
    )
    prompt = dedent(
        f"""\
        # {strategy_name}

        Add package-specific guidance for the `{role_name}` role here.
        """
    )
    skill = dedent(
        f"""\
        # {strategy_name}

        ## Description

        Starter skill for `{pkg_name}`. Replace this with a concrete reusable
        procedure, checklist, or domain operation.
        """
    )
    readme = dedent(
        f"""\
        # {pkg_name}

        Local ARC package scaffold.

        Add this package to `arc.toml`:

        ```toml
        [packages]
        paths = [
          "{target.as_posix()}",
        ]
        ```

        Validate it with:

        ```bash
        arc package validate {target.as_posix()}
        ```
        """
    )

    _write_new(target / "package.yaml", manifest, force=force)
    _write_new(target / "agents" / f"{role_name}.py", agent, force=force)
    _write_new(target / "prompts" / f"{role_name}.md", prompt, force=force)
    _write_new(target / "skills" / f"{strategy_name}.md", skill, force=force)
    _write_new(target / "README.md", readme, force=force)

    typer.echo(f"Created ARC package {pkg_name} at {target}")
    typer.echo("Add it to arc.toml [packages].paths, then run:")
    typer.echo(f"  arc package validate {target}")


@package_app.command("validate")
def validate_package(
    path: Path = typer.Argument(..., help="Package folder containing package.yaml."),
) -> None:
    """Validate that a local package manifest and declared Python objects load."""
    from arc.core.loader import _import_declared, _skill_name, _skill_path, load_package
    from arc.core.registry import ComponentRegistry

    manifest_path = path / "package.yaml"
    if not manifest_path.exists():
        typer.echo(f"Missing package.yaml: {manifest_path}", err=True)
        raise typer.Exit(1)

    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Invalid YAML in {manifest_path}: {exc}", err=True)
        raise typer.Exit(1)

    errors: list[str] = []
    if not manifest.get("name"):
        errors.append("package.yaml must declare name")
    if not isinstance(manifest.get("provides", {}), dict):
        errors.append("package.yaml provides must be a mapping")

    provides = manifest.get("provides", {}) or {}
    for group in (
        "agents",
        "strategies",
        "runtime_adapters",
        "providers",
        "loaders",
        "evaluators",
        "detectors",
        "audit_actions",
        "report_sections",
    ):
        for item in provides.get(group, []) or []:
            if not isinstance(item, dict):
                errors.append(f"provides.{group} entries must be mappings")
                continue
            has_entrypoint = bool(item.get("entrypoint"))
            has_file_attr = bool(item.get("path") and (item.get("class") or item.get("function")))
            if not has_entrypoint and not has_file_attr:
                errors.append(
                    f"provides.{group}.{item.get('name', '<unnamed>')} needs entrypoint "
                    "or path plus class/function"
                )
            if item.get("path") and not (path / item["path"]).exists():
                errors.append(f"declared path does not exist: {item['path']}")
                continue
            try:
                _import_declared(item, path)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"provides.{group}.{item.get('name', '<unnamed>')} failed to import: {exc}"
                )

    for item in provides.get("scripts", []) or []:
        if not isinstance(item, dict):
            errors.append("provides.scripts entries must be mappings")
            continue
        if not item.get("name") or not item.get("path"):
            errors.append("provides.scripts entries need name and path")
            continue
        if item.get("runtime", "python") != "python":
            errors.append(
                f"provides.scripts.{item.get('name')} has unsupported runtime "
                f"{item.get('runtime')!r}"
            )
        if not (path / item["path"]).exists():
            errors.append(f"declared script path does not exist: {item['path']}")

    for workflow in provides.get("workflows", []) or []:
        workflow_path = workflow.get("path") if isinstance(workflow, dict) else None
        if workflow_path and not (path / workflow_path).exists():
            errors.append(f"declared workflow path does not exist: {workflow['path']}")

    # Skills are declared as package-relative file paths; the file must exist
    # (review finding P3-1 — previously unchecked).
    resolved_skill_names: dict[str, str] = {}
    for skill in provides.get("skills", []) or []:
        try:
            skill_file = _skill_path(path, skill)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"declared skill entry is invalid: {exc}")
            continue
        if not skill_file.exists():
            errors.append(f"declared skill path does not exist: {skill_file.relative_to(path)}")
            continue
        name = _skill_name(skill, skill_file)
        if name in resolved_skill_names:
            errors.append(
                f"duplicate skill name after resolution: {name} "
                f"({resolved_skill_names[name]} and {skill_file.relative_to(path)})"
            )
        else:
            resolved_skill_names[name] = str(skill_file.relative_to(path))

    if errors:
        for error in errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(1)

    registry = ComponentRegistry()
    load_package(path, registry)
    pkg_name = manifest.get("name", path.name)

    if pkg_name not in registry.list_packages():
        typer.echo(f"ERROR: package did not register: {pkg_name}", err=True)
        raise typer.Exit(1)

    # Verify every declared contribution actually registered. ``load_package``
    # logs-and-swallows per-declaration failures (a malformed strategy missing
    # ``role``, a class that raises at instantiation, …), so a package could
    # otherwise print OK while a declared capability is silently absent
    # (review finding 3). Cross-check declarations against the registry.
    register_errors = _verify_registered(manifest, registry, path)
    if register_errors:
        for error in register_errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(1)

    typer.echo(f"OK: {pkg_name}")
    typer.echo(f"  agents:    {registry.list_agents()}")
    typer.echo(f"  skills:    {registry.list_skills()}")
    typer.echo(f"  workflows: {registry.list_workflows()}")
    typer.echo(f"  scripts:   {registry.list_scripts()}")


@package_app.command("doctor")
def doctor_package(
    path: Path = typer.Argument(..., help="Package folder containing package.yaml."),
) -> None:
    """Check local runtime requirements declared by a package."""
    from arc.runtime.environment import check_runtime_requirements

    manifest_path = path / "package.yaml"
    if not manifest_path.exists():
        typer.echo(f"Missing package.yaml: {manifest_path}", err=True)
        raise typer.Exit(1)
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Invalid YAML in {manifest_path}: {exc}", err=True)
        raise typer.Exit(1)

    pkg_name = manifest.get("name", path.name)
    checks = check_runtime_requirements(manifest.get("runtime", {}) or {})
    typer.echo(f"Doctor: {pkg_name}")
    if not checks:
        typer.echo("  No runtime requirements declared.")
        return
    failed = False
    for check in checks:
        label = check.status.upper()
        typer.echo(f"  {label:<7} {check.kind:<14} {check.name:<24} {check.detail}")
        if check.status == "missing" and check.required:
            failed = True
    if failed:
        raise typer.Exit(1)


def _verify_registered(manifest: dict, registry, package_dir: Path) -> list[str]:
    """Return an error per declared contribution that didn't register.

    The loader is lenient (it must not crash a whole startup over one bad
    package), so validation re-checks each declared name against the live
    registry. A name present in ``provides`` but absent from the registry
    means the loader swallowed an error for it.
    """
    provides = manifest.get("provides", {}) or {}
    errors: list[str] = []

    def _names(group: str) -> list[str]:
        out = []
        for item in provides.get(group, []) or []:
            if isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
        return out

    checks = {
        "agents": set(registry.list_agents()),
        "runtime_adapters": set(registry.list_adapters()),
        "providers": set(registry.list_providers()),
        "loaders": set(registry.list_loaders()),
        "scripts": set(registry.list_scripts()),
        "evaluators": set(registry.list_evaluators()),
        "detectors": set(registry.list_detectors()),
        "audit_actions": set(registry.list_audit_actions()),
        "report_sections": set(registry.list_report_sections()),
    }
    for group, registered in checks.items():
        for name in _names(group):
            if name not in registered:
                errors.append(
                    f"provides.{group}.{name} declared but did not register "
                    f"(check required manifest fields / instantiation)"
                )

    # Workflows declare a name + path.
    registered_workflows = set(registry.list_workflows())
    for item in provides.get("workflows", []) or []:
        if isinstance(item, dict) and item.get("name") and item["name"] not in registered_workflows:
            errors.append(f"provides.workflows.{item['name']} declared but did not register")

    # Skills (declared as file paths or {name, path}) + name-based resources (prompts,
    # templates, constraints, vocabularies). The loader derives the registered
    # name from explicit manifest name, markdown frontmatter, or file stem.
    from arc.core.loader import _resource_name, _skill_name, _skill_path
    resource_checks = {
        "prompts": set(registry.list_prompts()),
        "templates": set(registry.list_templates()),
        "constraints": set(registry.list_constraints()),
        "vocabularies": set(registry.list_vocabularies()),
    }
    registered_skills = set(registry.list_skills())
    for item in provides.get("skills", []) or []:
        try:
            skill_file = _skill_path(package_dir, item)
            name = _skill_name(item, skill_file)
        except Exception:
            name = _resource_name(item)
        if name and name not in registered_skills:
            errors.append(f"provides.skills.{name} declared but did not register")

    for group, registered in resource_checks.items():
        for item in provides.get(group, []) or []:
            name = _resource_name(item)
            if name and name not in registered:
                errors.append(
                    f"provides.{group}.{name} declared but did not register"
                )

    # Strategies live in the strategy catalogue, keyed by role.
    try:
        from arc.core.strategies import known_roles, list_strategies
        for item in provides.get("strategies", []) or []:
            if not isinstance(item, dict):
                continue
            role, name = item.get("role"), item.get("name")
            if not role or not name:
                errors.append(
                    f"provides.strategies.{name or '<unnamed>'} needs both role and name"
                )
                continue
            if role not in known_roles():
                errors.append(f"provides.strategies.{name}: unknown role {role!r}")
                continue
            if name not in {s.name for s in list_strategies(role)}:
                errors.append(
                    f"provides.strategies.{name} (role={role}) declared but did not register"
                )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"strategy verification failed: {exc}")

    return errors
