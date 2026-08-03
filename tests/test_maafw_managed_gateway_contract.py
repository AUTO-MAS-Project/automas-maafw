from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
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
            self.temp_root / "project-store",
            run_root=self.temp_root / "project-runs",
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

    def test_resolve_execution_prefers_script_isolated_checkout(self) -> None:
        async def scenario() -> None:
            imported = self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
            )
            resolution = await self.gateway.resolve_execution(
                {
                    "projectId": "demo",
                    "version": "1.0",
                    "scriptId": "script-one",
                }
            )
            self.assertTrue(resolution["projectCheckout"]["available"])
            self.assertTrue(resolution["projectCheckout"]["used"])
            self.assertNotEqual(resolution["projectPath"], imported["dataPath"])
            self.assertEqual(
                resolution["checkout"]["scriptId"],
                "script-one",
            )
            self.assertEqual(
                resolution["runtime"]["poolId"],
                self.runtime_pool.storage_info()["poolId"],
            )

        asyncio.run(scenario())

    def test_resolve_execution_rejects_store_identity_mismatch(self) -> None:
        async def scenario() -> None:
            imported = self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
            )
            with self.assertRaisesRegex(
                ManagedServiceError,
                "Project Store 身份",
            ):
                await self.gateway.resolve_execution(
                    {
                        "projectId": "demo",
                        "version": "1.0",
                        "scriptId": "script-one",
                        "expectedStoreId": (
                            "00000000-0000-0000-0000-000000000000"
                        ),
                        "expectedProjectManifest": imported["manifest"],
                    }
                )

            adopted = await self.gateway.resolve_execution(
                {
                    "projectId": "demo",
                    "version": "1.0",
                    "scriptId": "script-one",
                    "expectedStoreId": "",
                    "expectedProjectManifest": imported["manifest"],
                }
            )
            self.assertEqual(adopted["project"]["storeId"], imported["storeId"])

            stale_manifest = json.loads(json.dumps(imported["manifest"]))
            stale_manifest["source"]["hash"]["value"] = "0" * 64
            with self.assertRaisesRegex(
                ManagedServiceError,
                "来源哈希",
            ):
                await self.gateway.resolve_execution(
                    {
                        "projectId": "demo",
                        "version": "1.0",
                        "scriptId": "script-two",
                        "expectedStoreId": "",
                        "expectedProjectManifest": stale_manifest,
                    }
                )

        asyncio.run(scenario())

    def test_resolve_execution_refuses_store_without_checkout(self) -> None:
        class StoreWithoutCheckout:
            def __init__(self, store):
                self._store = store

            def resolve_project(self, project_id, version=None, *, touch=False):
                return self._store.resolve_project(
                    project_id,
                    version,
                    touch=touch,
                )

        async def scenario() -> None:
            self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
            )
            gateway = ManagedServiceGateway(
                StoreWithoutCheckout(self.project_store),
                self.runtime_pool,
            )
            with self.assertRaisesRegex(
                ManagedServiceError,
                "拒绝把不可变 Store payload",
            ):
                await gateway.resolve_execution(
                    {
                        "projectId": "demo",
                        "version": "1.0",
                        "scriptId": "script-one",
                    }
                )

        asyncio.run(scenario())

    def test_real_gc_rejects_incomplete_inventory_before_deleting(self) -> None:
        async def scenario() -> None:
            self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
            )
            checkout = self.project_store.checkout_project(
                "demo",
                "1.0",
                "script-one",
            )
            marker = Path(checkout["dataPath"]).parent / (
                ".auto_mas_maafw_checkout.json"
            )
            marker.write_text("{}", encoding="utf-8")
            runtime = self.runtime_pool.ensure(["maafw==4.3.0"])

            with self.assertRaisesRegex(
                ManagedServiceError,
                "资源盘点不完整",
            ):
                await self.gateway.collect_garbage(
                    dry_run=False,
                    grace_days=0,
                    keep_latest=0,
                )

            self.assertIsNotNone(
                self.runtime_pool.resolve_runtime(
                    {"runtimeId": runtime["runtimeId"]}
                )
            )

        asyncio.run(scenario())

    def test_global_inventory_reports_corrupt_manifest_without_reconciliation(
        self,
    ) -> None:
        async def scenario() -> None:
            imported = self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
            )
            Path(imported["manifestPath"]).write_text(
                "{broken",
                encoding="utf-8",
            )

            result = await self.gateway.global_inventory([])

            self.assertFalse(result["complete"])
            self.assertTrue(result["errors"])
            self.assertTrue(result["references"]["scripts"]["skipped"])
            self.assertTrue(result["references"]["runtimes"]["skipped"])

        asyncio.run(scenario())

    def test_global_inventory_exposes_authoritative_project_active_lease_ids(
        self,
    ) -> None:
        async def scenario() -> None:
            self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
            )
            self.project_store.acquire_lease(
                "demo",
                "1.0",
                owner="managed:test",
                lease_id="project-lease",
            )

            result = await self.gateway.global_inventory([])

            self.assertTrue(result["complete"])
            self.assertEqual(
                result["versions"][0]["activeLeaseIds"],
                ["project-lease"],
            )

        asyncio.run(scenario())

    def test_global_inventory_annotates_checkout_binding_and_orphan_state(
        self,
    ) -> None:
        async def scenario() -> None:
            self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
            )
            checkout = self.project_store.checkout_project(
                "demo",
                "1.0",
                "script-one",
            )
            script_record = {
                "id": "script-one",
                "type": "MaaFWManaged",
                "config": {
                    "Managed": {
                        "ProjectId": "demo",
                        "Version": "1.0",
                    }
                },
            }

            current = await self.gateway.global_inventory([script_record])
            current_checkout = current["checkouts"][0]
            self.assertEqual(current_checkout["checkoutId"], checkout["checkoutId"])
            self.assertTrue(current_checkout["scriptAvailable"])
            self.assertTrue(current_checkout["bindingCurrent"])
            self.assertIsNone(current_checkout["orphanReason"])

            orphaned = await self.gateway.global_inventory([])
            orphan_checkout = orphaned["checkouts"][0]
            self.assertFalse(orphan_checkout["scriptAvailable"])
            self.assertFalse(orphan_checkout["bindingCurrent"])
            self.assertEqual(
                orphan_checkout["orphanReason"],
                "managed-script-missing",
            )

        asyncio.run(scenario())

    def test_gateway_checkout_gc_requires_explicit_confirmation(self) -> None:
        async def scenario() -> None:
            self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
            )
            checkout = self.project_store.checkout_project(
                "demo",
                "1.0",
                "script-orphan",
            )
            checkout_root = Path(checkout["dataPath"]).parent
            active_checkout = self.project_store.checkout_project(
                "demo",
                "1.0",
                "script-active",
            )
            active_checkout_root = Path(active_checkout["dataPath"]).parent

            conservative = await self.gateway.collect_garbage(
                dry_run=False,
                grace_days=0,
                keep_latest=1,
                script_records=[],
                active_script_ids=[],
                checkout_gc_confirmed=False,
            )
            kept = conservative["projectStore"]["checkoutGarbageCollection"]
            self.assertIn(
                "explicit-confirmation-required",
                kept["kept"][0]["reasons"],
            )
            self.assertTrue(checkout_root.is_dir())

            applied = await self.gateway.collect_garbage(
                dry_run=False,
                grace_days=0,
                keep_latest=1,
                script_records=[],
                active_script_ids=["script-active"],
                checkout_gc_confirmed=True,
            )
            deleted = applied["projectStore"]["checkoutGarbageCollection"]
            self.assertEqual(
                [item["checkoutId"] for item in deleted["deleted"]],
                [checkout["checkoutId"]],
            )
            self.assertFalse(checkout_root.exists())
            self.assertTrue(active_checkout_root.is_dir())
            active_kept = next(
                item
                for item in deleted["kept"]
                if item["checkoutId"] == active_checkout["checkoutId"]
            )
            self.assertIn("active-operation", active_kept["reasons"])

        asyncio.run(scenario())

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
                {
                    "projectId": "demo",
                    "version": "unbounded",
                    "scriptId": "script-binding",
                }
            )

        self.project_store.import_project(
            self.source,
            "demo",
            "1.0",
            runtime_constraint="==4.3.0",
            activate=True,
        )
        resolution_v1 = await self.gateway.resolve_execution(
            {
                "projectId": "demo",
                "version": "1.0",
                "scriptId": "script-binding",
            }
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
            {
                "projectId": "demo",
                "version": "1.0",
                "scriptId": "script-recovery",
            }
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
            {
                "projectId": "demo",
                "version": "1.0",
                "scriptId": "script-recovery",
            }
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

    def test_bind_failure_compensates_only_reference_added_by_attempt(self) -> None:
        reference = "maafw-project:demo@1.0"

        class ProjectStore:
            def resolve_project(self, project_id, version, *, touch=True):
                del touch
                return {
                    "projectId": project_id,
                    "version": version,
                    "dataPath": "C:/store/demo/1.0",
                    "manifest": {
                        "runtime": {"binding": {"runtimeId": "runtime-old"}}
                    },
                }

            def bind_runtime(self, *_args, **_kwargs):
                raise RuntimeError("injected bind failure")

        class RuntimePool:
            def __init__(self, references):
                self.references = set(references)
                self.remove_calls = 0

            def resolve_runtime(self, request):
                return {
                    "runtimeId": request["runtimeId"],
                    "pythonExecutable": "C:/runtime/python.exe",
                    "references": sorted(self.references),
                }

            def add_reference(self, runtime_id, value):
                self.references.add(value)
                return {
                    "runtimeId": runtime_id,
                    "references": sorted(self.references),
                }

            def remove_reference(self, runtime_id, value):
                self.remove_calls += 1
                self.references.discard(value)
                return {
                    "runtimeId": runtime_id,
                    "references": sorted(self.references),
                }

        async def scenario(preexisting: bool):
            runtime_pool = RuntimePool([reference] if preexisting else [])
            gateway = ManagedServiceGateway(ProjectStore(), runtime_pool)
            with self.assertRaisesRegex(ManagedServiceError, "injected bind failure"):
                await gateway.bind_project_runtime(
                    "demo",
                    "1.0",
                    {
                        "runtimeId": "runtime-new",
                        "pythonExecutable": "C:/runtime/python.exe",
                    },
                )
            return runtime_pool

        newly_added = asyncio.run(scenario(False))
        self.assertNotIn(reference, newly_added.references)
        self.assertEqual(newly_added.remove_calls, 1)

        already_present = asyncio.run(scenario(True))
        self.assertIn(reference, already_present.references)
        self.assertEqual(already_present.remove_calls, 0)


class MaaFWManagedGatewayEventLoopContractTest(unittest.TestCase):
    """同步服务方法不得在宿主事件循环线程上内联执行。

    project_store / runtime_pool 的服务方法都是同步 def，内部会做 venv 创建、
    pip install（各 300s 超时）、整树 sha256+copytree、runtime 目录遍历。
    托管适配器与托管 HTTP 动作全部在事件循环上调用它们，内联执行会把整个
    后端卡死数十秒到十分钟。
    """

    def test_synchronous_service_methods_run_off_the_event_loop(self) -> None:
        observed: dict[str, int] = {}

        class BlockingService:
            def resolve_runtime(self, request: dict[str, Any]) -> dict[str, Any]:
                observed["worker"] = threading.get_ident()
                return {"runtimeId": request["runtimeId"], "pythonExecutable": "py"}

        async def scenario() -> None:
            observed["loop"] = threading.get_ident()
            gateway = ManagedServiceGateway(
                project_store=object(),
                runtime_pool=BlockingService(),
            )
            value = await gateway.resolve_runtime({"runtimeId": "maafw-runtime-x"})
            self.assertEqual(value["runtimeId"], "maafw-runtime-x")

        asyncio.run(scenario())

        self.assertIn("worker", observed)
        self.assertNotEqual(
            observed["worker"],
            observed["loop"],
            "同步服务方法仍在事件循环线程上执行，会阻塞整个后端",
        )

    def test_the_event_loop_stays_responsive_while_a_service_call_blocks(self) -> None:
        heartbeat_seen = threading.Event()
        observed: dict[str, bool] = {}

        class BlockingService:
            def resolve_runtime(self, request: dict[str, Any]) -> dict[str, Any]:
                # 模拟 venv 创建 / pip install 这类长时间同步 subprocess。
                # 事件循环若被阻塞，heartbeat 无法在本方法返回前跑起来。
                observed["heartbeat_before_return"] = heartbeat_seen.wait(timeout=5)
                return {"runtimeId": request["runtimeId"], "pythonExecutable": "py"}

        async def scenario() -> None:
            gateway = ManagedServiceGateway(
                project_store=object(),
                runtime_pool=BlockingService(),
            )

            async def heartbeat() -> None:
                await asyncio.sleep(0.05)
                heartbeat_seen.set()

            await asyncio.wait_for(
                asyncio.gather(
                    gateway.resolve_runtime({"runtimeId": "maafw-runtime-y"}),
                    heartbeat(),
                ),
                timeout=15,
            )

        try:
            asyncio.run(scenario())
        finally:
            heartbeat_seen.set()

        self.assertTrue(
            observed.get("heartbeat_before_return"),
            "同步服务方法执行期间事件循环无法调度其它协程，后端会整体假死",
        )

    def test_coroutine_service_methods_are_still_awaited_directly(self) -> None:
        observed: dict[str, int] = {}

        class AsyncService:
            async def resolve_runtime(self, request: dict[str, Any]) -> dict[str, Any]:
                observed["worker"] = threading.get_ident()
                return {"runtimeId": request["runtimeId"], "pythonExecutable": "py"}

        async def scenario() -> None:
            observed["loop"] = threading.get_ident()
            gateway = ManagedServiceGateway(
                project_store=object(),
                runtime_pool=AsyncService(),
            )
            value = await gateway.resolve_runtime({"runtimeId": "maafw-runtime-z"})
            self.assertEqual(value["runtimeId"], "maafw-runtime-z")

        asyncio.run(scenario())

        self.assertEqual(observed["worker"], observed["loop"])

    def test_service_failures_are_still_reported_as_managed_errors(self) -> None:
        class FailingService:
            def resolve_runtime(self, request: dict[str, Any]) -> dict[str, Any]:
                del request
                raise ValueError("boom")

        async def scenario() -> None:
            gateway = ManagedServiceGateway(
                project_store=object(),
                runtime_pool=FailingService(),
            )
            with self.assertRaises(ManagedServiceError) as raised:
                await gateway.resolve_runtime({"runtimeId": "maafw-runtime-w"})
            self.assertIn("boom", str(raised.exception))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
