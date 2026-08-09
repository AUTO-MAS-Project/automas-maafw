from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import platform
import sys
import sysconfig
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


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
from automas_maafw_runtime_pool import pool as runtime_pool  # noqa: E402


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
        self.fake_python_identities: dict[str, dict[str, str]] = {}
        self.real_python_probe = runtime_pool.probe_python_identity
        self.python_probe_patch = mock.patch.object(
            runtime_pool,
            "probe_python_identity",
            side_effect=self._probe_fake_python,
        )
        self.python_probe_patch.start()
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
        self.python_probe_patch.stop()
        self.temporary_directory.cleanup()

    def _fake_installer(
        self,
        environment_path: Path,
        requirements: tuple[str, ...] | list[str],
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        scripts_dir = environment_path / (
            "Scripts" if os.name == "nt" else "bin"
        )
        scripts_dir.mkdir(parents=True, exist_ok=False)
        python_executable = scripts_dir / (
            "python.exe" if os.name == "nt" else "python"
        )
        python_executable.write_text("fake runtime", encoding="utf-8")
        implementation, cache_tag, soabi = str(identity["pythonAbi"]).split(":", 2)
        version = str(identity["pythonVersion"])
        self.fake_python_identities[self._runtime_key(python_executable)] = {
            "implementation": implementation,
            "cacheTag": cache_tag,
            "soabi": soabi,
            "version": version,
            "shortVersion": ".".join(version.split(".")[:2]),
            "platform": str(identity["platform"]),
            "architecture": str(identity["architecture"]),
        }
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

    def _probe_fake_python(self, python_executable: str | Path) -> dict[str, str]:
        identity = self.fake_python_identities.get(
            self._runtime_key(python_executable)
        )
        if identity is not None:
            return dict(identity)
        return self.real_python_probe(python_executable)

    @staticmethod
    def _runtime_key(path: str | Path) -> str:
        prefix = "maafw-runtime-"
        for part in Path(path).parts:
            if part.startswith(prefix) and len(part) >= len(prefix) + 24:
                return part[: len(prefix) + 24]
        return str(Path(path).resolve())

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

    def test_resolve_execution_uses_immutable_store_selector_not_checkout(
        self,
    ) -> None:
        async def scenario() -> None:
            (self.source / "requirements.txt").write_text(
                "requests==2.31.0\n",
                encoding="utf-8",
            )
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
            checkout_requirements = Path(checkout["dataPath"]) / "requirements.txt"
            checkout_requirements.write_text(
                "requests==99.0.0\ncheckout-only==1.0\n",
                encoding="utf-8",
            )

            resolution = await self.gateway.resolve_execution(
                {
                    "projectId": "demo",
                    "version": "1.0",
                    "scriptId": "script-one",
                }
            )

            selector = resolution["runtime"]["selectorRequirements"]
            self.assertIn("requests==2.31.0", selector)
            self.assertNotIn("requests==99.0.0", selector)
            self.assertNotIn("checkout-only==1.0", selector)
            self.assertEqual(
                set(resolution["runtimeRequest"]["requirements"]),
                set(selector),
            )
            self.assertEqual(
                checkout_requirements.read_text(encoding="utf-8"),
                "requests==99.0.0\ncheckout-only==1.0\n",
            )

        asyncio.run(scenario())

    def test_resolve_execution_routes_store_python_constraint_to_cp313(self) -> None:
        interface = json.loads(
            (self.source / "interface.json").read_text(encoding="utf-8")
        )
        interface["runtime"] = {
            "python": {
                "implementation": "cpython",
                "requires": "==3.13.*",
            }
        }
        (self.source / "interface.json").write_text(
            json.dumps(interface, ensure_ascii=False),
            encoding="utf-8",
        )
        self.project_store.import_project(
            self.source,
            "cp313-demo",
            "1.0",
            runtime_constraint="==5.12.2",
        )
        cp313 = {
            "implementation": "cpython",
            "cacheTag": "cpython-313",
            "soabi": "cp313-win_amd64",
            "version": "3.13.14",
            "shortVersion": "3.13",
            "platform": sysconfig.get_platform() or sys.platform,
            "architecture": platform.machine() or "unknown",
        }

        async def scenario() -> None:
            with (
                mock.patch.object(
                    self.runtime_pool.pool,
                    "resolve_python",
                    return_value={
                        "executable": "C:/pool/python/cpython-3.13/python.exe",
                        "identity": cp313,
                        "source": "pool-managed",
                        "constraint": "==3.13.*",
                    },
                ),
                mock.patch.object(
                    runtime_pool,
                    "probe_python_identity",
                    return_value=cp313,
                ),
            ):
                resolution = await self.gateway.resolve_execution(
                    {
                        "projectId": "cp313-demo",
                        "version": "1.0",
                        "scriptId": "script-cp313",
                    }
                )

            self.assertEqual(
                resolution["runtimeRequest"]["python"],
                {
                    "implementation": "cpython",
                    "constraint": "==3.13.*",
                },
            )
            self.assertEqual(
                resolution["runtime"]["identity"]["pythonVersion"],
                "3.13.14",
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

    def test_reversible_bind_restores_store_and_pool_reference_deltas(self) -> None:
        project_reference = "maafw-project:demo@1.0"
        script_reference = "maafw-script:script-one"

        class ProjectStore:
            def __init__(self) -> None:
                self.binding: dict[str, Any] | None = {
                    "runtimeId": "runtime-old",
                    "poolId": "pool-one",
                }
                self.references = {"external-project-reference"}

            def resolve_project(self, project_id, version, *, touch=True):
                del touch
                return {
                    "projectId": project_id,
                    "version": version,
                    "dataPath": "C:/store/demo/1.0",
                    "manifest": {
                        "runtime": {
                            "binding": (
                                dict(self.binding)
                                if self.binding is not None
                                else None
                            ),
                            "references": sorted(self.references),
                        }
                    },
                }

            def bind_runtime(
                self,
                _project_id,
                _version,
                *,
                binding=None,
                reference=None,
                touch=True,
            ):
                del touch
                if binding is not None:
                    self.binding = dict(binding)
                if reference is not None:
                    self.references.add(reference)
                return self.resolve_project("demo", "1.0")

            def release_runtime(
                self,
                _project_id,
                _version,
                *,
                reference=None,
                clear_binding=False,
            ):
                if reference is not None:
                    self.references.discard(reference)
                if clear_binding:
                    self.binding = None
                return self.resolve_project("demo", "1.0")

        class RuntimePool:
            def __init__(self) -> None:
                self.references = {
                    "runtime-old": {
                        project_reference,
                        "external-old-reference",
                    },
                    "runtime-new": {"external-new-reference"},
                }

            def resolve_runtime(self, request):
                runtime_id = request["runtimeId"]
                return {
                    "runtimeId": runtime_id,
                    "pythonExecutable": "C:/runtime/python.exe",
                    "references": sorted(self.references[runtime_id]),
                }

            def add_reference(self, runtime_id, value):
                self.references[runtime_id].add(value)
                return self.resolve_runtime({"runtimeId": runtime_id})

            def remove_reference(self, runtime_id, value):
                self.references[runtime_id].discard(value)
                return self.resolve_runtime({"runtimeId": runtime_id})

        async def scenario():
            project_store = ProjectStore()
            runtime_pool = RuntimePool()
            gateway = ManagedServiceGateway(project_store, runtime_pool)
            committed = await gateway.bind_project_runtime_reversible(
                "demo",
                "1.0",
                {
                    "runtimeId": "runtime-new",
                    "poolId": "pool-one",
                    "pythonExecutable": "C:/runtime/python.exe",
                },
                project_reference=script_reference,
            )
            self.assertEqual(
                project_store.binding["runtimeId"],
                "runtime-new",
            )
            self.assertIn(script_reference, project_store.references)
            self.assertIn(
                project_reference,
                runtime_pool.references["runtime-new"],
            )
            self.assertNotIn(
                project_reference,
                runtime_pool.references["runtime-old"],
            )

            restored = await gateway.rollback_project_runtime_binding(
                committed["rollback"]
            )
            return project_store, runtime_pool, restored

        project_store, runtime_pool, restored = asyncio.run(scenario())
        self.assertTrue(restored["restored"])
        self.assertEqual(
            project_store.binding,
            {"runtimeId": "runtime-old", "poolId": "pool-one"},
        )
        self.assertEqual(
            project_store.references,
            {"external-project-reference"},
        )
        self.assertEqual(
            runtime_pool.references["runtime-old"],
            {project_reference, "external-old-reference"},
        )
        self.assertEqual(
            runtime_pool.references["runtime-new"],
            {"external-new-reference"},
        )

    def test_reversible_first_bind_clears_binding_and_new_references(self) -> None:
        async def scenario() -> None:
            self.project_store.import_project(
                self.source,
                "demo",
                "1.0",
                runtime_constraint="==4.3.0",
                activate=True,
            )
            resolution = await self.gateway.resolve_execution(
                {
                    "projectId": "demo",
                    "version": "1.0",
                    "scriptId": "script-first-bind",
                    "deferRuntimeBinding": True,
                }
            )
            runtime = resolution["runtime"]
            committed = await self.gateway.bind_project_runtime_reversible(
                "demo",
                "1.0",
                runtime,
                project_reference="maafw-script:script-first-bind",
            )
            self.assertEqual(
                committed["project"]["manifest"]["runtime"]["binding"][
                    "runtimeId"
                ],
                runtime["runtimeId"],
            )

            await self.gateway.rollback_project_runtime_binding(
                committed["rollback"]
            )
            restored_project = await self.gateway.resolve_project(
                "demo",
                "1.0",
            )
            restored_runtime = await self.gateway.resolve_runtime(
                {"runtimeId": runtime["runtimeId"], "touch": False}
            )
            self.assertIsNone(
                restored_project["manifest"]["runtime"].get("binding")
            )
            self.assertNotIn(
                "maafw-script:script-first-bind",
                restored_project["manifest"]["runtime"].get(
                    "references",
                    [],
                ),
            )
            self.assertNotIn(
                "maafw-project:demo@1.0",
                restored_runtime["references"],
            )

        asyncio.run(scenario())


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
