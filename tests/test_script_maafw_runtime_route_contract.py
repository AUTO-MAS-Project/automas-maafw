from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SOURCE = (
    ROOT / "packages" / "automas_script_maafw" / "src" / "automas_script_maafw"
)
MANAGED_SOURCE = (
    ROOT
    / "packages"
    / "automas_script_maafw_managed"
    / "src"
    / "automas_script_maafw_managed"
)


def _load_runtime_route_module():
    module_name = "_automas_script_maafw_runtime_route_contract"
    spec = importlib.util.spec_from_file_location(
        module_name,
        SCRIPT_SOURCE / "runtime_route.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class MaaFWRuntimeRouteContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.route = _load_runtime_route_module()

    def test_runtime_pool_service_supplies_resolved_root_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "pool"
            service = SimpleNamespace(
                storage_info=lambda: {
                    "root": str(root),
                    "poolId": "pool-one",
                    "rootIdentity": {"poolId": "pool-one"},
                }
            )

            route = self.route.runtime_pool_route_from_service(service)

            self.assertEqual(route.root, root.resolve())
            self.assertEqual(route.pool_id, "pool-one")

    def test_runtime_pool_service_rejects_identity_disagreement(self) -> None:
        service = SimpleNamespace(
            storage_info=lambda: {
                "root": "C:/pool",
                "poolId": "pool-one",
                "rootIdentity": {"poolId": "pool-two"},
            }
        )
        with self.assertRaisesRegex(
            self.route.MaaFWRuntimeRouteError,
            "poolId.*rootIdentity",
        ):
            self.route.runtime_pool_route_from_service(service)

    def test_managed_route_uses_complete_trusted_runtime_selector(self) -> None:
        runtime = {
            "runtimeId": "maafw-runtime-one",
            "poolId": "pool-one",
            "maafwRequirement": "maafw>=5,<6",
            "selectorRequirements": [
                "json5==0.12.1",
                "maafw>=5,<6",
                "requests==2.32.4",
            ],
        }
        project = self._project(runtime, shared=True)
        project["manifest"]["runtime"]["python"] = {
            "implementation": "cpython",
            "constraint": "==3.13.*",
        }

        route = self.route.managed_execution_route(
            managed_execution=True,
            project=project,
            runtime_binding=runtime,
            expected_pool_id="pool-one",
        )

        self.assertEqual(route.runtime_id, "maafw-runtime-one")
        self.assertEqual(route.maafw_requirement, "maafw>=5,<6")
        self.assertEqual(
            route.runtime_requirements,
            ("json5==0.12.1", "maafw>=5,<6", "requests==2.32.4"),
        )
        self.assertEqual(route.python_constraint, "==3.13.*")
        self.assertIs(route.shared_agent_dependencies_complete, True)
        self.assertEqual(route.managed_python_agent_indexes, (0,))

    def test_managed_route_accepts_packages_compatibility_alias(self) -> None:
        runtime = {
            "runtimeId": "maafw-runtime-one",
            "poolId": "pool-one",
            "maafwRequirement": "maafw==5.12.2",
            "packages": ["maafw==5.12.2", "json5==0.12.1"],
        }
        route = self.route.managed_execution_route(
            managed_execution=True,
            project=self._project(runtime, shared=True),
            runtime_binding=runtime,
            expected_pool_id="pool-one",
        )
        self.assertEqual(
            route.runtime_requirements,
            ("maafw==5.12.2", "json5==0.12.1"),
        )

    def test_ordinary_route_stays_none_but_half_injection_fails_closed(self) -> None:
        self.assertIsNone(
            self.route.managed_execution_route(
                managed_execution=False,
                project=None,
                runtime_binding=None,
                expected_pool_id="pool-one",
            )
        )
        with self.assertRaisesRegex(
            self.route.MaaFWRuntimeRouteError,
            "拒绝降级",
        ):
            self.route.managed_execution_route(
                managed_execution=False,
                project={},
                runtime_binding=None,
                expected_pool_id="pool-one",
            )

    def test_managed_route_rejects_pool_manifest_and_selector_mismatches(self) -> None:
        runtime = {
            "runtimeId": "maafw-runtime-one",
            "poolId": "pool-one",
            "maafwRequirement": "maafw==5.12.2",
            "selectorRequirements": ["maafw==5.12.2"],
        }
        with self.assertRaisesRegex(self.route.MaaFWRuntimeRouteError, "不同 Runtime Pool"):
            self.route.managed_execution_route(
                managed_execution=True,
                project=self._project(runtime),
                runtime_binding=runtime,
                expected_pool_id="pool-two",
            )

        bad_project = self._project(runtime)
        bad_project["manifest"]["runtime"]["binding"]["runtimeId"] = "other"
        with self.assertRaisesRegex(self.route.MaaFWRuntimeRouteError, "runtimeId 不一致"):
            self.route.managed_execution_route(
                managed_execution=True,
                project=bad_project,
                runtime_binding=runtime,
                expected_pool_id="pool-one",
            )

        incomplete_project = self._project(runtime)
        incomplete_project["manifest"]["runtime"]["binding"] = {
            "runtimeId": "maafw-runtime-one"
        }
        with self.assertRaisesRegex(
            self.route.MaaFWRuntimeRouteError,
            "Store manifest MaaFW requirement",
        ):
            self.route.managed_execution_route(
                managed_execution=True,
                project=incomplete_project,
                runtime_binding=runtime,
                expected_pool_id="pool-one",
            )

        bad_selector = dict(runtime)
        bad_selector["selectorRequirements"] = ["maafw==9.9.9"]
        with self.assertRaisesRegex(
            self.route.MaaFWRuntimeRouteError,
            "selectorRequirements",
        ):
            self.route.managed_execution_route(
                managed_execution=True,
                project=self._project(bad_selector),
                runtime_binding=bad_selector,
                expected_pool_id="pool-one",
            )

    def test_shared_agent_permission_only_accepts_literal_true(self) -> None:
        runtime = {
            "runtimeId": "maafw-runtime-one",
            "poolId": "pool-one",
            "maafwRequirement": "maafw==5.12.2",
            "selectorRequirements": ["maafw==5.12.2"],
        }
        project = self._project(runtime)
        project["manifest"]["runtime"]["sharedAgentDependenciesComplete"] = 1
        with self.assertRaisesRegex(
            self.route.MaaFWRuntimeRouteError,
            "拒绝降级到系统 Python",
        ):
            self.route.managed_execution_route(
                managed_execution=True,
                project=project,
                runtime_binding=runtime,
                expected_pool_id="pool-one",
            )

    def test_managed_python_agent_metadata_fails_closed_when_malformed(self) -> None:
        runtime = {
            "runtimeId": "maafw-runtime-one",
            "poolId": "pool-one",
            "maafwRequirement": "maafw==5.12.2",
            "selectorRequirements": ["maafw==5.12.2"],
        }
        project = self._project(runtime, shared=True)
        project["manifest"]["runtime"]["agent"] = [
            {
                "classification": "native",
                "interpreterRoute": "managed-python",
                "projectedChildExec": "C:/tools/python.exe",
            }
        ]
        with self.assertRaisesRegex(
            self.route.MaaFWRuntimeRouteError,
            "managed-python Agent",
        ):
            self.route.managed_execution_route(
                managed_execution=True,
                project=project,
                runtime_binding=runtime,
                expected_pool_id="pool-one",
            )

        project["manifest"]["runtime"]["agent"] = [
            {"index": 7, "classification": "native"}
        ]
        route = self.route.managed_execution_route(
            managed_execution=True,
            project=project,
            runtime_binding=runtime,
            expected_pool_id="pool-one",
        )
        self.assertEqual(route.managed_python_agent_indexes, ())

    def test_runner_task_forwards_full_managed_route_and_pool_identity(self) -> None:
        source = (SCRIPT_SOURCE / "runner_task.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        prepare_call = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and node.args
            and ast.unparse(node.args[0]) == "service.prepare_environment"
        )
        keywords = {keyword.arg for keyword in prepare_call.keywords}
        self.assertTrue(
            {
                "runtime_pool_root",
                "runtime_pool_id",
                "runtime_requirements",
                "runtime_requirement",
                "runtime_id",
                "runtime_python_constraint",
            }.issubset(keywords)
        )
        self.assertIn(
            "runner_plan.managedSharedAgentDependenciesComplete",
            source,
        )
        self.assertIn("runner_plan.managedPythonAgentIndexes", source)

    def test_adapters_inject_pool_identity_and_managed_marker(self) -> None:
        adapter_source = (SCRIPT_SOURCE / "adapter.py").read_text(encoding="utf-8")
        plugin_source = (SCRIPT_SOURCE / "plugin.py").read_text(encoding="utf-8")
        managed_source = (MANAGED_SOURCE / "adapter.py").read_text(encoding="utf-8")
        self.assertIn("runtime_pool_route_from_service", adapter_source)
        self.assertIn("task.maafw_runtime_pool_root", adapter_source)
        self.assertIn("task.maafw_runtime_pool_id", adapter_source)
        self.assertIn('"maafw.runtime_pool.v1"', plugin_source)
        self.assertIn('needs = ["maafw.runtime_pool.v1"]', plugin_source)
        self.assertIn("task.maafw_managed_execution = True", managed_source)
        self.assertIn("task.maafw_managed_route = managed_execution_route", managed_source)
        self.assertIn("执行缺少已解析的 Project Store resolution", managed_source)

    @staticmethod
    def _project(runtime: dict[str, object], *, shared: bool = False) -> dict[str, object]:
        return {
            "manifest": {
                "runtime": {
                    "binding": dict(runtime),
                    "sharedAgentDependenciesComplete": shared,
                    "agent": [
                        {
                            "index": 0,
                            "classification": "python",
                            "interpreterRoute": "managed-python",
                            "projectedChildExec": "python",
                        }
                    ],
                }
            }
        }


if __name__ == "__main__":
    unittest.main()
