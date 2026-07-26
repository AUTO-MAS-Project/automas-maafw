from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT / "packages" / "automas_maafw_agent_env" / "src",
    ROOT / "packages" / "automas_maafw_interface" / "src",
    ROOT / "packages" / "automas_maafw_project_store" / "src",
    ROOT / "packages" / "automas_maafw_runtime_pool" / "src",
    ROOT / "packages" / "automas_maafw_runner" / "src",
)
for source_root in reversed(SOURCE_ROOTS):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from automas_maafw_project_store import MaaFWProjectStoreService  # noqa: E402
from automas_maafw_runtime_pool import MaaFWRuntimePoolService  # noqa: E402


def _load_managed_services_module():
    module_name = "_automas_script_maafw_managed_services_contract"
    module_path = (
        ROOT
        / "packages"
        / "automas_script_maafw_managed"
        / "src"
        / "automas_script_maafw_managed"
        / "services.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


MANAGED_SERVICES = _load_managed_services_module()
ManagedServiceError = MANAGED_SERVICES.ManagedServiceError
ManagedServiceGateway = MANAGED_SERVICES.ManagedServiceGateway


class MaaFWManagedGatewayContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary_directory.name)
        self.source = self.temp_root / "release"
        (self.source / "Bundle").mkdir(parents=True)
        (self.source / "interface.json").write_text(
            json.dumps(
                {
                    "interface_version": 2,
                    "name": "managed-contract",
                    "controller": [{"name": "adb", "type": "Adb"}],
                    "resource": [{"name": "base", "path": ["Bundle"]}],
                    "task": [
                        {
                            "name": "Start",
                            "label": "Start",
                            "entry": "Start",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.source / "Bundle" / "pipeline.json").write_text(
            "{}",
            encoding="utf-8",
        )
        self.project_store = MaaFWProjectStoreService(
            self.temp_root / "project-store"
        )
        self.runtime_pool = MaaFWRuntimePoolService(
            self.temp_root / "runtime-pool",
            installer=self._fake_installer,
        )
        self.gateway = ManagedServiceGateway(
            self.project_store,
            self.runtime_pool,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _fake_installer(
        environment_path: Path,
        requirements: tuple[str, ...] | list[str],
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        del identity
        scripts_dir = environment_path / (
            "Scripts" if os.name == "nt" else "bin"
        )
        scripts_dir.mkdir(parents=True, exist_ok=False)
        python_executable = scripts_dir / (
            "python.exe" if os.name == "nt" else "python"
        )
        python_executable.write_text("fake runtime", encoding="utf-8")
        maafw_version = next(
            (
                requirement.split("==", 1)[1]
                for requirement in requirements
                if requirement.casefold().startswith("maafw==")
            ),
            "test",
        )
        return {
            "pythonExecutable": str(python_executable),
            "maafwVersion": maafw_version,
        }

    def test_binding_protects_runtime_until_project_version_is_deleted(self) -> None:
        asyncio.run(self._binding_lifecycle())

    async def _binding_lifecycle(self) -> None:
        self.project_store.import_project(
            self.source,
            "demo",
            "unbounded",
            activate=False,
        )
        with self.assertRaisesRegex(
            ManagedServiceError,
            "拒绝创建未约束的 MaaFW 运行时",
        ):
            await self.gateway.resolve_execution(
                {"projectId": "demo", "version": "unbounded"}
            )

        self.project_store.import_project(
            self.source,
            "demo",
            "1.0",
            runtime_constraint="==4.3.0",
            activate=True,
        )
        resolution_v1 = await self.gateway.resolve_execution(
            {"projectId": "demo", "version": "1.0"}
        )
        runtime_v1 = resolution_v1["runtime"]
        bound_v1 = await self.gateway.bind_project_runtime(
            "demo",
            "1.0",
            runtime_v1,
        )
        self.assertEqual(
            bound_v1["manifest"]["runtime"]["binding"]["runtimeId"],
            runtime_v1["runtimeId"],
        )
        self.assertEqual(
            self.runtime_pool.resolve_runtime(
                {"runtimeId": runtime_v1["runtimeId"]}
            )[
                "references"
            ],
            ["maafw-project:demo@1.0"],
        )
        with self.assertRaisesRegex(ManagedServiceError, "referenced"):
            await self.gateway.delete_runtime(
                {
                    "runtimeId": runtime_v1["runtimeId"],
                    "confirmation": runtime_v1["runtimeId"],
                }
            )

        self.project_store.import_project(
            self.source,
            "demo",
            "2.0",
            runtime_constraint="==4.4.0",
            activate=True,
        )
        versions = await self.gateway.list_versions("demo")
        self.assertEqual(
            {str(item["version"]) for item in versions},
            {"unbounded", "1.0", "2.0"},
        )
        deleted_project = await self.gateway.delete_version(
            {
                "projectId": "demo",
                "version": "1.0",
                "confirmation": "demo@1.0",
            }
        )
        self.assertTrue(deleted_project["deleted"])
        self.assertEqual(
            self.runtime_pool.resolve_runtime(
                {"runtimeId": runtime_v1["runtimeId"]}
            )[
                "references"
            ],
            [],
        )

        deleted_runtime = await self.gateway.delete_runtime(
            {
                "runtimeId": runtime_v1["runtimeId"],
                "confirmation": runtime_v1["runtimeId"],
            }
        )
        self.assertTrue(deleted_runtime["deleted"])
        self.assertIsNone(
            self.runtime_pool.resolve_runtime(
                {"runtimeId": runtime_v1["runtimeId"]}
            )
        )

    def test_resolve_execution_recovers_from_stale_runtime_id_without_maafw_version(
        self,
    ) -> None:
        """D1 回归：stale runtimeId + 缺失 maafwVersion + constraint 存在时不应立即失败。

        复现路径：
        1. 导入项目并绑定运行时（manifest 同时记录 runtimeId 与 maafwVersion）
        2. 直接篡改 manifest：保留 runtimeId，删除 maafwVersion
        3. 删除底层运行时，使 runtimeId 变成 stale
        4. 再次 resolve_execution：旧实现会因 ensure_runtime 收到 stale
           runtimeId 抛 'requested runtimeId does not match the requirement
           selector'；修复后应重建运行时并回写新绑定。
        """
        asyncio.run(self._stale_runtime_id_recovery())

    async def _stale_runtime_id_recovery(self) -> None:
        self.project_store.import_project(
            self.source,
            "demo",
            "1.0",
            runtime_constraint="==4.3.0",
            activate=True,
        )
        first = await self.gateway.resolve_execution(
            {"projectId": "demo", "version": "1.0"}
        )
        first_runtime_id = first["runtime"]["runtimeId"]
        bound = await self.gateway.bind_project_runtime(
            "demo",
            "1.0",
            first["runtime"],
        )
        self.assertEqual(
            bound["manifest"]["runtime"]["binding"]["runtimeId"],
            first_runtime_id,
        )
        self.assertEqual(
            bound["manifest"]["runtime"]["binding"].get("maafwVersion"),
            "4.3.0",
        )

        # 篡改 manifest：保留 stale runtimeId，删除 maafwVersion
        project = await self.gateway.resolve_project("demo", "1.0")
        manifest_path = Path(project["manifestPath"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime"]["binding"] = {"runtimeId": first_runtime_id}
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 删除底层运行时（先清理引用，再删除运行时目录）
        self.runtime_pool.remove_reference(
            first_runtime_id,
            "maafw-project:demo@1.0",
        )
        deleted = self.runtime_pool.delete(first_runtime_id)
        self.assertTrue(deleted["deleted"])
        self.assertIsNone(
            self.runtime_pool.resolve_runtime(
                {"runtimeId": first_runtime_id}
            )
        )

        # 修复后：resolve_execution 应重建运行时并回写新绑定，不抛错。
        # runtime_id 由 canonical requirements 内容寻址，相同 constraint
        # 重建后会得到相同 ID；关键验证是 manifest binding 已被回写
        # 并包含 maafwVersion。
        recovered = await self.gateway.resolve_execution(
            {"projectId": "demo", "version": "1.0"}
        )
        recovered_runtime_id = recovered["runtime"]["runtimeId"]
        self.assertEqual(
            recovered["runtime"]["maafwVersion"],
            "4.3.0",
        )
        # 重建的运行时确实存在于 pool 中
        self.assertIsNotNone(
            self.runtime_pool.resolve_runtime(
                {"runtimeId": recovered_runtime_id}
            )
        )

        # 验证 manifest 已被回写为新绑定（含 maafwVersion）
        refreshed = await self.gateway.resolve_project("demo", "1.0")
        refreshed_binding = refreshed["manifest"]["runtime"]["binding"]
        self.assertEqual(refreshed_binding["runtimeId"], recovered_runtime_id)
        self.assertEqual(refreshed_binding.get("maafwVersion"), "4.3.0")


if __name__ == "__main__":
    unittest.main()
