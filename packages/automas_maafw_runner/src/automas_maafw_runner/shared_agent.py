from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


PROJECT_MANIFEST_NAME = ".auto_mas_maafw_project.json"
SHARED_RUNTIME_KIND = "shared_runtime"


def route_managed_python_agents_to_shared_runtime(
    project_path: str | Path,
    plans: Iterable[Any],
    *,
    python_executable: str | Path | None = None,
) -> list[Any]:
    """Route managed isolated Python agents through the current worker.

    Reuse is opt-in because the runtime selector alone cannot prove that it
    contains every transitive Agent dependency. The project manifest must set
    ``runtime.sharedAgentDependenciesComplete`` to the JSON boolean ``true``.
    Legacy, incomplete, binary, and explicitly external routes stay untouched.
    """

    project = Path(project_path).resolve()
    if not _shared_agent_dependencies_complete(project):
        return []

    shared_python = str(Path(python_executable or sys.executable).resolve())
    routed: list[Any] = []
    for plan in plans:
        if bool(getattr(plan, "embedded", False)):
            continue
        if getattr(plan, "runtimeKind", None) != "isolated_venv":
            continue

        command = list(getattr(plan, "command", None) or [])
        if command:
            command[0] = shared_python
        else:
            command = [shared_python, *list(getattr(plan, "childArgs", None) or [])]
        plan.command = command
        plan.executable = shared_python
        plan.executableExists = Path(shared_python).is_file()
        plan.runtimeKind = SHARED_RUNTIME_KIND
        plan.isolatedVenvPath = None
        shared_reason = "managed project reuses the shared MaaFW runtime"
        previous_reason = str(getattr(plan, "fallbackReason", None) or "").strip()
        plan.fallbackReason = (
            f"{previous_reason}; {shared_reason}"
            if previous_reason
            else shared_reason
        )
        routed.append(plan)
    return routed


def _shared_agent_dependencies_complete(project_path: Path) -> bool:
    manifest_path = project_path / PROJECT_MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    runtime = payload.get("runtime")
    return (
        isinstance(runtime, dict)
        and runtime.get("sharedAgentDependenciesComplete") is True
    )


__all__ = [
    "PROJECT_MANIFEST_NAME",
    "SHARED_RUNTIME_KIND",
    "route_managed_python_agents_to_shared_runtime",
]
