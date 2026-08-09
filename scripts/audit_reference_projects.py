from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for package_name in (
    "automas_maafw_interface",
    "automas_maafw_agent_env",
    "automas_maafw_runtime_pool",
    "automas_maafw_runner",
):
    package_source = ROOT / "packages" / package_name / "src"
    if str(package_source) not in sys.path:
        sys.path.insert(0, str(package_source))

from automas_maafw_interface.loader import (  # noqa: E402
    MaaFWInterfaceLoadError,
    load_interface_model,
)
from automas_maafw_interface.preview import build_interface_preview_data  # noqa: E402
from automas_maafw_runner.run_plan import (  # noqa: E402
    MaaFWRunPlanError,
    build_maafw_run_plan,
)


def audit_reference_root(
    reference_root: Path,
    *,
    include_run_plan: bool = False,
) -> list[dict[str, Any]]:
    candidates = sorted(
        {
            path.parent.resolve()
            for pattern in ("interface.json", "interface.jsonc")
            for path in reference_root.rglob(pattern)
        },
        key=lambda path: str(path).casefold(),
    )
    return [
        audit_project(path, include_run_plan=include_run_plan)
        for path in candidates
    ]


def audit_project(
    project_root: Path,
    *,
    include_run_plan: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(project_root),
        "status": "FAIL",
    }
    try:
        interface = load_interface_model(project_root)
        preview = build_interface_preview_data(project_root, interface)
    except MaaFWInterfaceLoadError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result.update(
        {
            "status": "PASS",
            "name": interface.name,
            "version": interface.version,
            "controllers": len(interface.controller),
            "resources": len(interface.resource),
            "tasks": len(interface.task),
            "options": len(interface.option),
            "presets": len(interface.preset),
            "pretasks": len(
                interface.pretask
                if isinstance(interface.pretask, list)
                else [interface.pretask]
                if interface.pretask is not None
                else []
            ),
            "settings": len(interface.setting or []),
            "hotkeyOptions": sum(
                1 for option in interface.option.values() if option.type == "hotkey"
            ),
            "previewTasks": len(preview.tasks),
            "previewOptions": len(preview.options),
        }
    )
    if include_run_plan:
        _audit_run_plan(project_root, interface, result)
    return result


def _audit_run_plan(
    project_root: Path,
    interface: Any,
    result: dict[str, Any],
) -> None:
    controller = next(
        (
            controller
            for controller in interface.controller
            if controller.type in {"Adb", "Win32"}
        ),
        None,
    )
    if controller is None:
        result["runPlan"] = {"status": "NOT_APPLICABLE", "reason": "no Direct controller"}
        return

    try:
        plan = build_maafw_run_plan(
            project_root,
            interface,
            controller_name=controller.name,
            task_names=[task.name for task in interface.task],
        )
    except (MaaFWRunPlanError, ValueError, OSError) as exc:
        result["status"] = "PARTIAL"
        result["runPlan"] = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
        }
        return
    except Exception as exc:
        result["status"] = "PARTIAL"
        result["runPlan"] = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
        }
        return

    result["runPlan"] = {
        "status": "PASS",
        "controller": plan.controllerName,
        "controllerType": plan.controllerType,
        "resource": plan.resourceName,
        "runnableTasks": len(plan.tasks),
        "skippedTasks": len(plan.skippedTasks),
        "agents": len(plan.agents),
        "pretasks": len(plan.pretasks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only AUTO-MAS MaaFW ProjectInterface compatibility audit."
    )
    parser.add_argument("reference_root", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--run-plan",
        action="store_true",
        help="also build a no-launch Direct run plan for every compatible sample",
    )
    args = parser.parse_args()

    reference_root = args.reference_root.resolve()
    if not reference_root.is_dir():
        parser.error(f"reference root does not exist: {reference_root}")

    results = audit_reference_root(
        reference_root,
        include_run_plan=args.run_plan,
    )
    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            summary = (
                f"{result['status']} | {result['path']}"
                if result["status"] != "PASS"
                else (
                    f"PASS | {result['path']} | {result.get('name')} "
                    f"| tasks={result.get('tasks')} options={result.get('options')} "
                    f"settings={result.get('settings')} hotkeys={result.get('hotkeyOptions')} "
                    f"runPlan={result.get('runPlan', {}).get('status', 'SKIP')}"
                )
            )
            print(summary)
            if result.get("error"):
                print(f"  {result['error']}")

    return 1 if any(result["status"] != "PASS" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
