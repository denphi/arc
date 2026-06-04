"""Runtime environment checks for ARC packages."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeCheck:
    kind: str
    name: str
    status: str
    required: bool = False
    detail: str = ""


def check_runtime_requirements(runtime: dict) -> list[RuntimeCheck]:
    checks: list[RuntimeCheck] = []
    if not isinstance(runtime, dict):
        return checks

    python_spec = runtime.get("python")
    if python_spec:
        checks.append(_check_python(str(python_spec)))

    for item in runtime.get("commands", []) or []:
        if isinstance(item, str):
            item = {"name": item, "required": True}
        if not isinstance(item, dict) or not item.get("name"):
            continue
        checks.append(_check_command(str(item["name"]), bool(item.get("required", True))))

    for item in runtime.get("python_modules", []) or []:
        if isinstance(item, str):
            item = {"name": item, "required": True}
        if not isinstance(item, dict) or not item.get("name"):
            continue
        checks.append(_check_python_module(str(item["name"]), bool(item.get("required", True))))

    for item in runtime.get("conda", []) or []:
        checks.append(RuntimeCheck("conda", str(item), "info", False, "declared"))

    return checks


def _check_python(spec: str) -> RuntimeCheck:
    version = ".".join(str(part) for part in sys.version_info[:3])
    try:
        from packaging.specifiers import SpecifierSet
        ok = version in SpecifierSet(spec)
    except Exception:  # noqa: BLE001
        return RuntimeCheck("python", spec, "warn", True, f"running {version}; spec not parsed")
    return RuntimeCheck(
        "python",
        spec,
        "ok" if ok else "missing",
        True,
        f"running {version}",
    )


def _check_command(name: str, required: bool) -> RuntimeCheck:
    path = shutil.which(name)
    return RuntimeCheck(
        "command",
        name,
        "ok" if path else ("missing" if required else "warn"),
        required,
        path or "not found",
    )


def _check_python_module(name: str, required: bool) -> RuntimeCheck:
    spec = importlib.util.find_spec(name)
    return RuntimeCheck(
        "python_module",
        name,
        "ok" if spec else ("missing" if required else "warn"),
        required,
        "importable" if spec else "not importable",
    )
