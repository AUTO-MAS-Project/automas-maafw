from __future__ import annotations

import asyncio
import json
import stat
import sys
import tempfile
import time
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "packages" / "automas_maafw_project_store"
PACKAGE_SRC = PACKAGE_ROOT / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from automas_maafw_project_store import (  # noqa: E402
    CHECKOUT_MARKER_NAME,
    MANIFEST_FILE_NAME,
    STORE_KIND,
    STORE_MARKER_NAME,
    STORE_SCHEMA_VERSION,
    MaaFWProjectStoreError,
    MaaFWProjectStoreService,
)
from automas_maafw_project_store import service as project_store_service  # noqa: E402


class MaaFWProjectStoreContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp_dir.name)
        self.source = self.temp_root / "release"
        self.source.mkdir()
        self.store = MaaFWProjectStoreService(
            self.temp_root / "store",
            run_root=self.temp_root / "runs",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plugin_entrypoint_and_default_instance_contract(self) -> None:
        pyproject = tomllib.loads(
            (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        entrypoints = pyproject["project"]["entry-points"]["auto_mas.plugins"]
        self.assertEqual(pyproject["project"]["version"], "0.2.2")
        self.assertEqual(
            entrypoints["automas_maafw_project_store"],
            "automas_maafw_project_store.plugin:Plugin",
        )
        plugin_source = (
            PACKAGE_SRC / "automas_maafw_project_store" / "plugin.py"
        ).read_text(encoding="utf-8")
        self.assertIn('provides = ["maafw.project_store.v1"]', plugin_source)
        self.assertIn("DEFAULT_INSTANCE", plugin_source)

    def test_root_marker_identity_is_stable_and_json_friendly(self) -> None:
        first = self.store.storage_info()
        marker = json.loads(
            (self.store.root / STORE_MARKER_NAME).read_text(encoding="utf-8")
        )

        self.assertEqual(marker["schemaVersion"], STORE_SCHEMA_VERSION)
        self.assertEqual(marker["kind"], STORE_KIND)
        self.assertEqual(first["storeId"], marker["storeId"])
        self.assertEqual(first["root"], str(self.store.root))
        self.assertFalse(first["isDefault"])
        self.assertEqual(first["rootIdentity"], self.store.rootIdentity)

        reopened = MaaFWProjectStoreService(
            self.store.root,
            run_root=self.store.run_root,
        )
        self.assertEqual(reopened.storage_info()["storeId"], first["storeId"])

    def test_configured_root_rejects_unknown_non_empty_directory(self) -> None:
        unknown = self.temp_root / "unknown"
        unknown.mkdir()
        (unknown / "sentinel.txt").write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "non-empty directory without a valid project-store marker",
        ):
            MaaFWProjectStoreService(unknown)

        self.assertEqual((unknown / "sentinel.txt").read_text(encoding="utf-8"), "keep")
        self.assertFalse((unknown / STORE_MARKER_NAME).exists())

    def test_invalid_marker_fails_closed(self) -> None:
        invalid = self.temp_root / "invalid-marker"
        invalid.mkdir()
        (invalid / STORE_MARKER_NAME).write_text(
            json.dumps(
                {
                    "schemaVersion": STORE_SCHEMA_VERSION,
                    "kind": "not-a-project-store",
                    "storeId": "00000000-0000-0000-0000-000000000000",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MaaFWProjectStoreError, "marker kind is invalid"):
            MaaFWProjectStoreService(invalid)

    def test_legacy_default_layout_is_adopted_without_moving_content(self) -> None:
        working_directory = self.temp_root / "legacy-host"
        legacy_root = working_directory / "data" / "maafw_project_store"
        (legacy_root / "projects").mkdir(parents=True)
        (legacy_root / ".staging").mkdir()
        sentinel = legacy_root / "projects" / "sentinel"
        sentinel.mkdir()

        with patch(
            "automas_maafw_project_store.service.Path.cwd",
            return_value=working_directory,
        ):
            adopted = MaaFWProjectStoreService()

        self.assertTrue((legacy_root / STORE_MARKER_NAME).is_file())
        self.assertTrue(sentinel.is_dir())
        self.assertTrue(adopted.storage_info()["isDefault"])

    def test_plugin_uses_root_from_context_config(self) -> None:
        from automas_maafw_project_store.plugin import Plugin

        configured = self.temp_root / "configured-store"
        configured_runs = self.temp_root / "configured-runs"
        context = type(
            "Context",
            (),
            {
                "config": {
                    "Root": str(configured),
                    "RunRoot": str(configured_runs),
                }
            },
        )()

        plugin = Plugin(context)

        self.assertEqual(plugin.service.root, configured.resolve())
        self.assertEqual(plugin.service.run_root, configured_runs.resolve())

    def test_configured_roots_must_be_absolute_and_separate(self) -> None:
        with self.assertRaisesRegex(MaaFWProjectStoreError, "absolute path"):
            MaaFWProjectStoreService("relative-store")
        with self.assertRaisesRegex(MaaFWProjectStoreError, "absolute path"):
            MaaFWProjectStoreService(
                self.temp_root / "absolute-store",
                run_root="relative-runs",
            )
        with self.assertRaisesRegex(MaaFWProjectStoreError, "separate path trees"):
            MaaFWProjectStoreService(
                self.temp_root / "shared",
                run_root=self.temp_root / "shared" / "runs",
            )

    def test_checkout_is_isolated_reused_and_does_not_mutate_store(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "checkout",
                "version": "1.0.0",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_text(self.source / "Bundle" / "pipeline.json", "{}")
        resolved = self.store.import_project(self.source, "demo", "1.0.0")
        store_data = Path(resolved["dataPath"])
        first = self.store.checkout_project("demo", "1.0.0", "script-one")
        checkout_data = Path(first["dataPath"])
        self.assertNotEqual(checkout_data, store_data)
        self.assertFalse((checkout_data / MANIFEST_FILE_NAME).exists())
        self.assertTrue(
            (checkout_data.parent / CHECKOUT_MARKER_NAME).is_file()
        )
        (checkout_data / "user-output.json").write_text("{}", encoding="utf-8")
        (checkout_data / "Bundle" / "pipeline.json").write_text(
            '{"changed": true}',
            encoding="utf-8",
        )
        self.assertEqual(
            (store_data / "Bundle" / "pipeline.json").read_text(encoding="utf-8"),
            "{}",
        )

        first_last_used_at = first["lastUsedAt"]
        with patch.object(
            project_store_service.time,
            "time",
            return_value=time.time() + 60,
        ):
            reused = self.store.checkout_project("demo", "1.0.0", "script-one")
        other = self.store.checkout_project("demo", "1.0.0", "script-two")
        self.assertTrue(reused["reused"])
        self.assertGreater(reused["lastUsedAt"], first_last_used_at)
        self.assertTrue((Path(reused["dataPath"]) / "user-output.json").is_file())
        self.assertNotEqual(reused["dataPath"], other["dataPath"])
        self.assertFalse((Path(other["dataPath"]) / "user-output.json").exists())
        self.assertEqual(reused["storeId"], resolved["storeId"])
        self.assertEqual(reused["runRootId"], self.store.storage_info()["runRootId"])
        inventory = self.store.inventory()
        self.assertTrue(inventory["complete"])
        self.assertEqual(
            {item["scriptId"] for item in inventory["checkouts"]},
            {"script-one", "script-two"},
        )

    def test_checkout_rejects_tampered_immutable_store_payload(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "checkout-integrity",
                "version": "1.0.0",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_text(self.source / "Bundle" / "pipeline.json", "{}")
        resolved = self.store.import_project(self.source, "demo", "1.0.0")
        store_file = Path(resolved["dataPath"]) / "Bundle" / "pipeline.json"
        store_file.write_text('{"tampered": true}', encoding="utf-8")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "payload integrity check failed",
        ):
            self.store.checkout_project("demo", "1.0.0", "script-one")

        checkouts_root = self.store.run_root / "scripts" / "script-one" / "checkouts"
        self.assertFalse(checkouts_root.exists())

    def test_existing_checkout_rejects_tampered_immutable_store_payload(
        self,
    ) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "checkout-integrity-reuse",
                "version": "1.0.0",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_text(self.source / "Bundle" / "pipeline.json", "{}")
        resolved = self.store.import_project(self.source, "demo", "1.0.0")
        checkout = self.store.checkout_project("demo", "1.0.0", "script-one")
        checkout_sentinel = Path(checkout["dataPath"]) / "keep.txt"
        checkout_sentinel.write_text("keep", encoding="utf-8")

        store_file = Path(resolved["dataPath"]) / "Bundle" / "pipeline.json"
        store_file.write_text('{"tampered": true}', encoding="utf-8")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "payload integrity check failed",
        ):
            self.store.checkout_project("demo", "1.0.0", "script-one")
        self.assertEqual(checkout_sentinel.read_text(encoding="utf-8"), "keep")

    def test_corrupt_checkout_fails_closed_without_overwrite(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "checkout",
                "version": "1.0.0",
                "task": [],
            },
        )
        self.store.import_project(self.source, "demo", "1.0.0")
        checkout = self.store.checkout_project("demo", "1.0.0", "script-one")
        marker_path = Path(checkout["dataPath"]).parent / CHECKOUT_MARKER_NAME
        marker_path.write_text("{}", encoding="utf-8")
        sentinel = Path(checkout["dataPath"]) / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(MaaFWProjectStoreError, "marker"):
            self.store.checkout_project("demo", "1.0.0", "script-one")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_checkout_gc_requires_orphan_context_confirmation_and_no_lease(
        self,
    ) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "checkout-gc",
                "version": "1.0.0",
                "task": [],
            },
        )
        self.store.import_project(self.source, "demo", "1.0.0")
        checkout = self.store.checkout_project(
            "demo",
            "1.0.0",
            "script-orphan",
        )
        checkout_path = Path(checkout["dataPath"]).parent
        self.store.acquire_checkout_lease(
            checkout["checkoutId"],
            "script-orphan",
            "run-lease",
            owner="MaaFWManaged:script-orphan",
            ttl_seconds=300,
        )

        protected = self.store.collect_garbage(
            dry_run=True,
            grace_seconds=0,
            keep_latest=1,
            checkout_context={
                "managedBindings": {},
                "activeScriptIds": [],
                "confirmed": False,
            },
        )["checkoutGarbageCollection"]
        self.assertIn("active-lease", protected["kept"][0]["reasons"])
        self.store.release_checkout_lease(
            checkout["checkoutId"],
            "script-orphan",
            "run-lease",
        )

        conservative = self.store.collect_garbage(
            dry_run=False,
            grace_seconds=0,
            keep_latest=1,
            checkout_context={
                "managedBindings": {},
                "activeScriptIds": [],
                "confirmed": False,
            },
        )["checkoutGarbageCollection"]
        self.assertIn(
            "explicit-confirmation-required",
            conservative["kept"][0]["reasons"],
        )
        self.assertTrue(checkout_path.is_dir())

        applied = self.store.collect_garbage(
            dry_run=False,
            grace_seconds=0,
            keep_latest=1,
            checkout_context={
                "managedBindings": {},
                "activeScriptIds": [],
                "confirmed": True,
            },
        )["checkoutGarbageCollection"]
        self.assertEqual(
            [item["checkoutId"] for item in applied["deleted"]],
            [checkout["checkoutId"]],
        )
        self.assertFalse(checkout_path.exists())

    def test_checkout_gc_keeps_current_binding_active_operation_and_legacy_marker(
        self,
    ) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "checkout-gc",
                "version": "1.0.0",
                "task": [],
            },
        )
        self.store.import_project(self.source, "demo", "1.0.0")
        current = self.store.checkout_project("demo", "1.0.0", "script-current")
        active = self.store.checkout_project("demo", "1.0.0", "script-active")
        legacy = self.store.checkout_project("demo", "1.0.0", "script-legacy")
        legacy_marker = Path(legacy["dataPath"]).parent / CHECKOUT_MARKER_NAME
        legacy_data = json.loads(legacy_marker.read_text(encoding="utf-8"))
        legacy_data.pop("leases", None)
        legacy_marker.write_text(json.dumps(legacy_data), encoding="utf-8")

        result = self.store.collect_garbage(
            dry_run=False,
            grace_seconds=0,
            keep_latest=1,
            checkout_context={
                "managedBindings": {
                    "script-current": {
                        "projectId": "demo",
                        "version": "1.0.0",
                    }
                },
                "activeScriptIds": ["script-active"],
                "confirmed": True,
            },
        )["checkoutGarbageCollection"]
        reasons = {
            item["scriptId"]: set(item["reasons"])
            for item in result["kept"]
        }
        self.assertIn("managed-script-binding", reasons["script-current"])
        self.assertIn("active-operation", reasons["script-active"])
        self.assertIn("checkout-lease-unavailable", reasons["script-legacy"])
        self.assertTrue(Path(current["dataPath"]).is_dir())
        self.assertTrue(Path(active["dataPath"]).is_dir())
        self.assertTrue(Path(legacy["dataPath"]).is_dir())

    def test_directory_import_fails_closed_when_source_changes_during_snapshot(
        self,
    ) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "race",
                "version": "1.0.0",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        payload = self.source / "Bundle" / "pipeline.json"
        self._write_text(payload, '{"generation": 1}')
        original_copy = project_store_service.shutil.copy2
        mutated = False

        def copy_then_mutate(source: Path, destination: Path) -> str:
            nonlocal mutated
            result = original_copy(source, destination)
            if not mutated:
                mutated = True
                payload.write_text('{"generation": 2}', encoding="utf-8")
            return result

        with patch.object(
            project_store_service.shutil,
            "copy2",
            side_effect=copy_then_mutate,
        ), self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "source directory changed while it was being imported",
        ):
            self.store.import_project(self.source, "race", "1.0.0")

        self.assertEqual(self.store.list_projects(), [])
        self.assertEqual(list((self.store.root / ".staging").iterdir()), [])

    def test_directory_import_uses_compact_snapshot_for_portable_python_tree(
        self,
    ) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "portable-python",
                "version": "1.0.0",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})

        relative_directory = Path(
            "temp/temp_res/resource_v3.28.6_extracted/python/Lib/site-packages/"
            "pip-26.1.2.dist-info/licenses/src/pip/_vendor/cachecontrol"
        )
        store = MaaFWProjectStoreService(
            self.temp_root / "project-store-long",
            run_root=self.temp_root / "project-runs-long",
        )
        compact_snapshot = store.root / ".staging" / ("a" * 32)
        while len(str(compact_snapshot / relative_directory)) < 230:
            relative_directory /= "vendorpkg"
        legacy_snapshot = (
            store.root
            / ".staging"
            / f"import-directory-{'a' * 32}"
            / "source"
        )
        if sys.platform == "win32":
            self.assertLess(len(str(compact_snapshot / relative_directory)), 248)
            self.assertGreaterEqual(
                len(str(legacy_snapshot / relative_directory)),
                248,
            )

        self._write_text(
            self.source / relative_directory / "LICENSE.txt",
            "portable python license payload",
        )
        resolved = store.import_project(
            self.source,
            "portable-python",
            "1.0.0",
        )

        self.assertEqual(resolved["projectId"], "portable-python")
        self.assertFalse((Path(resolved["dataPath"]) / "temp").exists())
        self.assertEqual(list((store.root / ".staging").iterdir()), [])

    def test_racing_repeat_import_does_not_reuse_existing_version(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "race",
                "version": "1.0.0",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        payload = self.source / "Bundle" / "pipeline.json"
        self._write_text(payload, '{"generation": 1}')
        imported = self.store.import_project(self.source, "race", "1.0.0")
        stored_payload = Path(imported["dataPath"]) / "Bundle" / "pipeline.json"
        original_copy = project_store_service.shutil.copy2
        mutated = False

        def copy_then_mutate(source: Path, destination: Path) -> str:
            nonlocal mutated
            result = original_copy(source, destination)
            if not mutated:
                mutated = True
                payload.write_text('{"generation": 2}', encoding="utf-8")
            return result

        with patch.object(
            project_store_service.shutil,
            "copy2",
            side_effect=copy_then_mutate,
        ), self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "source directory changed while it was being imported",
        ):
            self.store.import_project(self.source, "race", "1.0.0")

        self.assertEqual(
            stored_payload.read_text(encoding="utf-8"),
            '{"generation": 1}',
        )

    def test_inventory_reports_version_directory_without_manifest(self) -> None:
        orphan = self.store.root / "projects" / "orphan" / "versions" / "1.0"
        orphan.mkdir(parents=True)
        (orphan / "data").mkdir()

        snapshot = self.store.inventory()

        self.assertFalse(snapshot["complete"])
        self.assertTrue(
            any(
                "manifest is missing" in str(item.get("error") or "")
                for item in snapshot["errors"]
            )
        )

    def test_inventory_reports_tampered_immutable_payload(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "inventory-integrity",
                "version": "1.0.0",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_text(self.source / "Bundle" / "pipeline.json", "{}")
        imported = self.store.import_project(self.source, "demo", "1.0.0")
        tampered_path = Path(imported["dataPath"]) / "Bundle" / "pipeline.json"
        tampered_path.write_text('{"tampered": true}', encoding="utf-8")

        snapshot = self.store.inventory()

        self.assertFalse(snapshot["complete"])
        error = next(
            item
            for item in snapshot["errors"]
            if item.get("projectId") == "demo" and item.get("version") == "1.0.0"
        )
        self.assertEqual(error["scope"], "project-version")
        self.assertEqual(error["path"], str(Path(imported["dataPath"]).parent))
        self.assertIn("payload integrity check failed", error["error"])

    def test_reparse_root_is_rejected_when_supported(self) -> None:
        target = self.temp_root / "reparse-target"
        target.mkdir()
        link = self.temp_root / "reparse-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaisesRegex(MaaFWProjectStoreError, "reparse points"):
            MaaFWProjectStoreService(link)

    def test_resource_lifecycle_transaction_is_task_reentrant(self) -> None:
        async def verify() -> None:
            release_owner = asyncio.Event()
            waiter_entered = asyncio.Event()

            async def waiter() -> None:
                await release_owner.wait()
                async with self.store.resource_lifecycle_transaction():
                    waiter_entered.set()

            waiting_task = asyncio.create_task(waiter())
            async with self.store.resource_lifecycle_transaction():
                async with self.store.resource_lifecycle_transaction():
                    pass
                release_owner.set()
                await asyncio.sleep(0)
                self.assertFalse(waiter_entered.is_set())

                async def inherited_child() -> None:
                    async with self.store.resource_lifecycle_transaction():
                        pass

                with self.assertRaisesRegex(
                    MaaFWProjectStoreError,
                    "cannot cross asyncio tasks",
                ):
                    await asyncio.create_task(inherited_child())

            await waiting_task
            self.assertTrue(waiter_entered.is_set())

        asyncio.run(verify())

    def test_root_project_projection_preserves_resources_and_clears_all_hashes(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "demo",
                "version": "1.0.0",
                "runtime": {
                    "python": {
                        "implementation": "cpython",
                        "requires": "==3.13.*",
                    }
                },
                "controller": [
                    {
                        "name": "adb",
                        "type": "Adb",
                        "attach_resource_path": ["Bundle/shared"],
                    }
                ],
                "resource": [
                    {
                        "name": "zh",
                        "path": ["Bundle/base", "Bundle/zh"],
                        "hash": "native-root-hash",
                    },
                    {
                        "name": "en",
                        "path": ["Bundle/base", "Bundle/en"],
                    },
                ],
                "agent": {
                    "child_exec": "python",
                    "child_args": ["-u", "agent/main.py"],
                },
                "import": ["config/tasks.json"],
                "task": [],
            },
        )
        self._write_json(
            self.source / "config" / "tasks.json",
            {
                "resource": [
                    {
                        "name": "fragment-resource",
                        "path": ["Bundle/base"],
                        "hash": "native-fragment-hash",
                    }
                ],
                "pretask": {"exec": "agent/helper.py"},
                "task": [],
            },
        )
        for relative in (
            "Bundle/base/pipeline/main.json",
            "Bundle/zh/image/a.png",
            "Bundle/en/image/a.png",
            "Bundle/shared/model/model.bin",
            "agent/main.py",
            "agent/helper.py",
            "agent/Agent.exe",
            "agent/native.cp313-win_amd64.pyd",
            "agent/__pycache__/main.pyc",
            "plugins/custom/plugin.py",
            "frontend/MFAAvalonia.exe",
            "frontend/MFAAvalonia.runtimeconfig.json",
            "runtime/MaaFramework.dll",
            "cache/result.bin",
            "update.ps1",
            "MXU.exe",
        ):
            self._write_text(self.source / relative, relative)
        self._write_text(self.source / "requirements.txt", "maafw==v5.10.4\n")

        resolved = self.store.import_project(
            self.source,
            "demo",
            "1.0.0",
            reference="script:one",
        )

        self.assertEqual(
            set(resolved),
            {
                "dataPath",
                "storeId",
                "activeLeaseIds",
                "projectId",
                "version",
                "runtimeConstraint",
                "manifestPath",
                "projectInterfacePath",
                "summary",
                "manifest",
            },
        )
        self.assertEqual(resolved["runtimeConstraint"], "==v5.10.4")
        data_path = Path(resolved["dataPath"])
        projected_interface = json.loads(
            (data_path / "interface.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            projected_interface["resource"][0]["path"],
            ["./Bundle/base", "./Bundle/zh"],
        )
        self.assertNotIn("hash", projected_interface["resource"][0])
        projected_fragment = json.loads(
            (data_path / "config" / "tasks.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("hash", projected_fragment["resource"][0])

        for relative in (
            "Bundle/base/pipeline/main.json",
            "Bundle/zh/image/a.png",
            "Bundle/en/image/a.png",
            "Bundle/shared/model/model.bin",
            "agent/main.py",
            "agent/helper.py",
            "agent/Agent.exe",
            "agent/native.cp313-win_amd64.pyd",
            "plugins/custom/plugin.py",
            "requirements.txt",
        ):
            self.assertTrue((data_path / relative).is_file(), relative)
        for relative in (
            "agent/__pycache__/main.pyc",
            "frontend/MFAAvalonia.exe",
            "runtime/MaaFramework.dll",
            "cache/result.bin",
            "update.ps1",
            "MXU.exe",
        ):
            self.assertFalse((data_path / relative).exists(), relative)

        manifest_path = data_path / MANIFEST_FILE_NAME
        self.assertEqual(Path(resolved["manifestPath"]), manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["runtime"]["references"], ["script:one"])
        self.assertEqual(manifest["runtime"]["constraint"], "==v5.10.4")
        self.assertTrue(manifest["runtime"]["sharedAgentDependenciesComplete"])
        self.assertEqual(manifest["requiredPythonAbi"], ["cp313-win_amd64"])
        self.assertEqual(manifest["runtime"]["requiredPythonAbi"], ["cp313-win_amd64"])
        self.assertEqual(
            manifest["runtime"]["agent"][0]["abiTags"],
            ["cp313-win_amd64"],
        )
        self.assertEqual(
            manifest["runtime"]["agent"][0]["interpreterRoute"],
            "managed-python",
        )
        self.assertEqual(
            manifest["projectInterface"]["clearedResources"],
            [
                {"file": "config/tasks.json", "resource": "fragment-resource"},
                {"file": "interface.json", "resource": "zh"},
            ],
        )
        self.assertIn("Bundle/base/pipeline/main.json", manifest["projection"]["copied"])
        self.assertEqual(manifest["capabilities"]["counts"]["controllers"], 1)
        self.assertEqual(manifest["capabilities"]["counts"]["resources"], 3)
        self.assertEqual(manifest["capabilities"]["counts"]["agents"], 1)
        self.assertEqual(
            manifest["shells"]["families"],
            ["MFAAvalonia", "MXU"],
        )
        self.assertGreater(manifest["size"]["sourceTreeBytes"], 0)
        self.assertLess(
            manifest["size"]["projectedBytes"],
            manifest["size"]["sourceTreeBytes"],
        )
        self.assertEqual(resolved["summary"]["capabilities"], manifest["capabilities"])

    def test_assets_project_is_promoted_and_parent_agent_paths_are_rewritten(self) -> None:
        assets = self.source / "assets"
        self._write_json(
            assets / "interface.json",
            {
                "interface_version": 2,
                "name": "nested",
                "runtime": {
                    "python": {
                        "implementation": "cpython",
                        "requires": "==3.13.*",
                    }
                },
                "languages": {"zh_cn": "resource/i18n.json"},
                "resource": [
                    {"name": "base", "path": ["./resource/base"]},
                ],
                "agent": {
                    "child_exec": "python",
                    "child_args": ["-u", "./../agent/main.py"],
                },
                "pretask": {"exec": "./../agent/tool.py"},
                "import": ["resource/tasks.json"],
                "task": [],
            },
        )
        self._write_json(assets / "resource" / "tasks.json", {"task": []})
        self._write_json(assets / "resource" / "i18n.json", {"name": "名称"})
        self._write_json(assets / "resource" / "base" / "pipeline.json", {})
        self._write_text(self.source / "agent" / "main.py", "print('main')\n")
        self._write_text(self.source / "agent" / "tool.py", "print('tool')\n")
        self._write_text(self.source / "requirements.txt", "maafw>=5.0,<6\n")
        self._write_text(self.source / "MFW.exe", "ui shell")
        self._write_text(self.source / "MFW.runtimeconfig.json", "{}")

        resolved = self.store.import_project(self.source, "nested", "2.0.0")
        data_path = Path(resolved["dataPath"])

        self.assertTrue((data_path / "interface.json").is_file())
        self.assertFalse((data_path / "assets").exists())
        self.assertTrue((data_path / "resource" / "base" / "pipeline.json").is_file())
        self.assertTrue((data_path / "agent" / "main.py").is_file())
        self.assertTrue((data_path / "agent" / "tool.py").is_file())
        self.assertFalse((data_path / "MFW.exe").exists())
        self.assertFalse((data_path / "MFW.runtimeconfig.json").exists())

        projected = json.loads(
            (data_path / "interface.json").read_text(encoding="utf-8")
        )
        self.assertEqual(projected["agent"]["child_args"][1], "./agent/main.py")
        self.assertEqual(projected["pretask"]["exec"], "./agent/tool.py")
        self.assertEqual(projected["import"], ["./resource/tasks.json"])
        self.assertEqual(projected["languages"]["zh_cn"], "./resource/i18n.json")
        self.assertEqual(projected["resource"][0]["path"], ["./resource/base"])
        self.assertEqual(resolved["runtimeConstraint"], ">=5.0,<6")
        self.assertTrue(
            resolved["manifest"]["runtime"]["sharedAgentDependenciesComplete"]
        )

    def test_complete_resource_roots_override_only_their_blacklisted_name(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "explicit-blacklisted-roots",
                "controller": [
                    {
                        "name": "adb",
                        "type": "Adb",
                        "attach_resource_path": ["venv"],
                    }
                ],
                "resource": [
                    {"name": "runtime-data", "path": ["runtime"]},
                    {"name": "python-data", "path": ["python"]},
                ],
                "task": [],
            },
        )
        for relative in (
            "runtime/pipeline/main.json",
            "runtime/MaaFramework.dll",
            "runtime/python.exe",
            "runtime/MFAAvalonia.exe",
            "python/model/data.bin",
            "python/python.exe",
            "venv/controller/calibration.json",
            "venv/MaaFramework.dll",
        ):
            self._write_text(self.source / relative, relative)

        resolved = self.store.import_project(self.source, "explicit-roots", "1.0")
        data_path = Path(resolved["dataPath"])

        for relative in (
            "runtime/pipeline/main.json",
            "python/model/data.bin",
            "venv/controller/calibration.json",
        ):
            self.assertTrue((data_path / relative).is_file(), relative)
        for relative in (
            "runtime/MaaFramework.dll",
            "runtime/python.exe",
            "runtime/MFAAvalonia.exe",
            "python/python.exe",
            "venv/MaaFramework.dll",
        ):
            self.assertFalse((data_path / relative).exists(), relative)

    def test_stripped_bundled_python_is_allowed_when_entrypoint_is_retained(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "bundled-python",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "agent": {
                    "child_exec": "python/python.exe",
                    "child_args": ["-u", "agent/bootstrap.py"],
                },
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "python" / "python.exe", "embedded runtime")
        self._write_text(
            self.source / "python" / "python313._pth",
            "python313.zip\n",
        )
        self._write_text(self.source / "agent" / "bootstrap.py", "print('agent')\n")
        self._write_text(
            self.source / "requirements.txt",
            "maafw==5.10.4\nhttpx==0.28.1\n",
        )

        resolved = self.store.import_project(self.source, "bundled-python", "4.5")
        data_path = Path(resolved["dataPath"])
        runtime = resolved["manifest"]["runtime"]
        projected_interface = json.loads(
            (data_path / "interface.json").read_text(encoding="utf-8")
        )

        self.assertFalse((data_path / "python" / "python.exe").exists())
        self.assertTrue((data_path / "agent" / "bootstrap.py").is_file())
        self.assertEqual(projected_interface["agent"]["child_exec"], "python")
        self.assertEqual(
            runtime["agent"][0]["strippedInterpreter"],
            {
                "sourcePath": "python/python.exe",
                "reason": "embedded-python",
                "retainedEntrypoints": ["agent/bootstrap.py"],
            },
        )
        self.assertEqual(runtime["agent"][0]["interpreterRoute"], "managed-python")
        self.assertEqual(runtime["agent"][0]["projectedChildExec"], "python")
        self.assertEqual(
            runtime["python"],
            {
                "implementation": "cpython",
                "constraint": "==3.13.*",
                "sources": ["embedded-python-marker"],
            },
        )
        self.assertEqual(resolved["summary"]["pythonConstraint"], "==3.13.*")
        self.assertTrue(runtime["sharedAgentDependenciesComplete"])
        self.assertTrue(
            any(
                "embedded Python interpreter was stripped" in warning
                for warning in resolved["manifest"]["warnings"]
            )
        )

    def test_missing_bundled_python_uses_release_runtime_dll_marker(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "bundled-python-dll",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "agent": {
                    "child_exec": "python/python.exe",
                    "child_args": ["-u", "agent/bootstrap.py"],
                },
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "python312.dll", "embedded runtime")
        self._write_text(self.source / "agent" / "bootstrap.py", "pass\n")
        self._write_text(
            self.source / "requirements.txt",
            "maafw==5.10.4\n",
        )

        resolved = self.store.import_project(
            self.source,
            "bundled-python-dll",
            "1.0",
        )
        data_path = Path(resolved["dataPath"])
        runtime = resolved["manifest"]["runtime"]

        self.assertFalse((data_path / "python312.dll").exists())
        self.assertEqual(
            runtime["python"],
            {
                "implementation": "cpython",
                "constraint": "==3.12.*",
                "sources": ["bundled-python-runtime-library"],
            },
        )
        self.assertEqual(runtime["requiredPythonAbi"], ["cp312"])
        self.assertEqual(runtime["agent"][0]["abiTags"], ["cp312"])
        self.assertEqual(
            runtime["agent"][0]["strippedInterpreter"]["sourcePath"],
            "python/python.exe",
        )
        self.assertEqual(runtime["agent"][0]["interpreterRoute"], "managed-python")

    def test_ambiguous_release_runtime_dll_markers_fail_closed(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "ambiguous-python-dll",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "agent": {
                    "child_exec": "python/python.exe",
                    "child_args": ["-u", "agent/bootstrap.py"],
                },
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "python312.dll", "runtime 3.12")
        self._write_text(self.source / "python313.dll", "runtime 3.13")
        self._write_text(self.source / "agent" / "bootstrap.py", "pass\n")
        self._write_text(self.source / "requirements.txt", "maafw==5.10.4\n")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "multiple interpreter minors",
        ):
            self.store.import_project(
                self.source,
                "ambiguous-python-dll",
                "1.0",
            )

    def test_release_python_dll_does_not_reclassify_native_agent(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "native-with-shell-python",
                "agent": {"child_exec": "agent/native.exe"},
                "task": [],
            },
        )
        self._write_text(self.source / "agent" / "native.exe", "native")
        self._write_text(self.source / "python312.dll", "shell runtime")

        resolved = self.store.import_project(
            self.source,
            "native-with-shell-python",
            "1.0",
        )
        runtime = resolved["manifest"]["runtime"]

        self.assertIsNone(runtime["python"])
        self.assertEqual(runtime["requiredPythonAbi"], [])
        self.assertEqual(runtime["agent"][0]["classification"], "native")
        self.assertNotIn("interpreterRoute", runtime["agent"][0])

    def test_unpinned_maafw_uses_unique_bundled_framework_version(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "bundled-framework-version",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "requirements.txt", "MaaFw\n")
        framework = self.source / "runtimes" / "win-x64" / "MaaFramework.dll"
        framework.parent.mkdir(parents=True)
        framework.write_bytes(b"latest_id\x00v5.12.1\x00DoNothing")

        resolved = self.store.import_project(
            self.source,
            "bundled-framework-version",
            "1.0",
        )

        self.assertEqual(resolved["runtimeConstraint"], "==5.12.1")
        self.assertEqual(
            resolved["manifest"]["runtime"]["constraint"],
            "==5.12.1",
        )

    def test_declared_maafw_constraint_must_match_bundled_framework(self) -> None:
        self._write_minimal_project()
        framework = self.source / "maafw" / "MaaFramework.dll"
        framework.parent.mkdir(parents=True)
        framework.write_bytes(b"latest_id\x00v5.12.1\x00DoNothing")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "does not match bundled MaaFramework",
        ):
            self.store.import_project(
                self.source,
                "framework-version-mismatch",
                "1.0",
                runtime_constraint="==5.10.4",
            )

    def test_conflicting_bundled_framework_versions_fail_closed(self) -> None:
        self._write_minimal_project()
        for relative, version in (
            ("maafw/MaaFramework.dll", "5.12.1"),
            ("runtimes/win-x64/MaaFramework.dll", "5.10.4"),
        ):
            framework = self.source / relative
            framework.parent.mkdir(parents=True, exist_ok=True)
            framework.write_bytes(
                f"latest_id\x00v{version}\x00DoNothing".encode("ascii")
            )

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "declare different versions",
        ):
            self.store.import_project(
                self.source,
                "framework-version-conflict",
                "1.0",
            )

    def test_framework_with_historical_versions_is_not_inference_evidence(
        self,
    ) -> None:
        self._write_minimal_project()
        framework = self.source / "maafw" / "MaaFramework.dll"
        framework.parent.mkdir(parents=True)
        framework.write_bytes(
            b"current\x00v5.12.2\x00historical\x00v5.10.5\x00"
        )

        resolved = self.store.import_project(
            self.source,
            "framework-history",
            "1.0",
            runtime_constraint="==5.12.2",
        )

        self.assertEqual(resolved["runtimeConstraint"], "==5.12.2")

    def test_framework_inference_ignores_update_and_pip_residue(self) -> None:
        self._write_minimal_project()
        binaries = {
            "runtimes/win-x64/native/MaaFramework.dll": b"v5.12.2\x00",
            "temp/update/MaaFramework.dll": b"v5.10.5\x00",
            "python/Lib/site-packages/~-a/bin/MaaFramework.dll": b"v4.5.4\x00",
            "python/Lib/site-packages/~.a/bin/MaaFramework.dll": (
                b"v5.3.0-beta.5\x00"
            ),
        }
        for relative, content in binaries.items():
            framework = self.source / relative
            framework.parent.mkdir(parents=True, exist_ok=True)
            framework.write_bytes(content)

        resolved = self.store.import_project(
            self.source,
            "framework-residue",
            "1.0",
        )

        self.assertEqual(resolved["runtimeConstraint"], "==5.12.2")

    def test_extensionless_windows_native_exec_is_retained_and_rewritten(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "native-agent",
                "agent": [{"child_exec": "agent/go-service"}],
                "import": ["tasks/pretasks/GameSetting.json"],
                "task": [],
            },
        )
        self._write_json(
            self.source / "tasks" / "pretasks" / "GameSetting.json",
            {
                "pretask": {
                    "exec": "agent/go-service",
                    "args": ["--pretask", "GameSetting"],
                }
            },
        )
        self._write_text(self.source / "agent" / "go-service.exe", "native")

        resolved = self.store.import_project(self.source, "native-agent", "1.0")
        data_path = Path(resolved["dataPath"])
        projected_interface = json.loads(
            (data_path / "interface.json").read_text(encoding="utf-8")
        )
        projected_pretask = json.loads(
            (data_path / "tasks" / "pretasks" / "GameSetting.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertTrue((data_path / "agent" / "go-service.exe").is_file())
        self.assertEqual(
            projected_interface["agent"][0]["child_exec"],
            "./agent/go-service.exe",
        )
        self.assertEqual(
            projected_pretask["pretask"]["exec"],
            "./agent/go-service.exe",
        )

    def test_declared_python_constraint_must_match_bundled_interpreter(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "python-constraint-mismatch",
                "runtime": {
                    "python": {
                        "implementation": "cpython",
                        "requires": ">=3.12,<3.13",
                    }
                },
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "agent": {
                    "child_exec": "python/python.exe",
                    "child_args": ["-u", "agent/bootstrap.py"],
                },
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "python" / "python.exe", "embedded")
        self._write_text(
            self.source / "python" / "python313._pth",
            "python313.zip\n",
        )
        self._write_text(self.source / "agent" / "bootstrap.py", "pass\n")
        self._write_text(self.source / "requirements.txt", "maafw==5.12.2\n")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "does not match the bundled interpreter marker",
        ):
            self.store.import_project(
                self.source,
                "python-constraint-mismatch",
                "1.0",
            )
        self.assertEqual(self.store.list_projects(), [])

    def test_exact_patch_constraint_accepts_matching_bundled_minor(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "python-exact-patch",
                "runtime": {
                    "python": {
                        "implementation": "cpython",
                        "requires": "==3.13.14",
                    }
                },
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "agent": {
                    "child_exec": "python/python.exe",
                    "child_args": ["-u", "agent/bootstrap.py"],
                },
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "python" / "python.exe", "embedded")
        self._write_text(
            self.source / "python" / "python313._pth",
            "python313.zip\n",
        )
        self._write_text(self.source / "agent" / "bootstrap.py", "pass\n")
        self._write_text(self.source / "requirements.txt", "maafw==5.12.2\n")

        resolved = self.store.import_project(
            self.source,
            "python-exact-patch",
            "1.0",
        )

        runtime = resolved["manifest"]["runtime"]
        self.assertEqual(runtime["python"]["constraint"], "==3.13.14")
        self.assertEqual(
            runtime["agent"][0]["interpreterRoute"],
            "managed-python",
        )

    def test_python_agent_without_runtime_identity_is_rejected(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "python-unknown-abi",
                "agent": {
                    "child_exec": "python",
                    "child_args": ["agent.py"],
                },
                "task": [],
            },
        )
        self._write_text(self.source / "agent.py", "pass\n")
        self._write_text(self.source / "requirements.txt", "maafw==5.12.2\n")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "runtime ABI is unknown",
        ):
            self.store.import_project(
                self.source,
                "python-unknown-abi",
                "1.0",
            )

    def test_missing_bundled_python_uses_managed_route_when_entrypoint_exists(
        self,
    ) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "missing-bundled-python",
                "runtime": {
                    "python": {
                        "implementation": "cpython",
                        "requires": "==3.12.*",
                    }
                },
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "agent": {
                    "child_exec": "python/python.exe",
                    "child_args": ["-u", "agent/bootstrap.py"],
                    "embedded": True,
                },
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "agent" / "bootstrap.py", "print('agent')\n")
        self._write_text(
            self.source / "requirements.txt",
            "maafw==5.10.4\nhttpx==0.28.1\n",
        )

        resolved = self.store.import_project(
            self.source,
            "missing-bundled-python",
            "4.5",
        )
        data_path = Path(resolved["dataPath"])
        runtime = resolved["manifest"]["runtime"]
        projected_interface = json.loads(
            (data_path / "interface.json").read_text(encoding="utf-8")
        )

        self.assertEqual(projected_interface["agent"]["child_exec"], "python")
        self.assertTrue((data_path / "agent" / "bootstrap.py").is_file())
        self.assertEqual(runtime["agent"][0]["interpreterRoute"], "managed-python")
        self.assertEqual(
            runtime["agent"][0]["strippedInterpreter"]["reason"],
            "missing-embedded-python",
        )
        self.assertTrue(runtime["sharedAgentDependenciesComplete"])
        self.assertTrue(
            any(
                "was missing and was replaced" in warning
                for warning in resolved["manifest"]["warnings"]
            )
        )

    def test_required_agent_entrypoint_under_filtered_directory_is_rejected(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "missing-agent",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "agent": {
                    "child_exec": "python",
                    "child_args": ["-u", "runtime/bootstrap.py"],
                },
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "runtime" / "bootstrap.py", "print('agent')\n")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "required agent.*resource-only projection",
        ):
            self.store.import_project(self.source, "missing-agent", "1.0")
        self.assertEqual(self.store.list_projects(), [])

    def test_required_pretask_under_filtered_directory_is_rejected(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "missing-pretask",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "pretask": {"exec": "venv/bootstrap.py"},
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})
        self._write_text(self.source / "venv" / "bootstrap.py", "print('pretask')\n")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "required pretask.*resource-only projection",
        ):
            self.store.import_project(self.source, "missing-pretask", "1.0")
        self.assertEqual(self.store.list_projects(), [])

    def test_shared_agent_dependency_flag_rejects_ambiguous_sources(self) -> None:
        cases = {
            "plain": {"requirements": "maafw==5.10.4\nhttpx==0.28.1\n", "expected": True},
            "missing": {"expected": False},
            "nested": {
                "requirements": "maafw==5.10.4\n",
                "extra": {"agent/requirements-agent.txt": "httpx==0.28.1\n"},
                "expected": False,
            },
            "pyproject": {
                "requirements": "maafw==5.10.4\n",
                "extra": {"pyproject.toml": "[project]\nname='agent'\n"},
                "expected": False,
            },
            "lock": {
                "requirements": "maafw==5.10.4\n",
                "extra": {"uv.lock": "version = 1\n"},
                "expected": False,
            },
            "include": {
                "requirements": "maafw==5.10.4\n-r agent/requirements-agent.txt\n",
                "extra": {"agent/requirements-agent.txt": "httpx==0.28.1\n"},
                "expected": False,
            },
        }

        for name, case in cases.items():
            with self.subTest(name=name):
                source = self.temp_root / f"release-{name}"
                self._write_json(
                    source / "interface.json",
                    {
                        "interface_version": 2,
                        "name": name,
                        "runtime": {
                            "python": {
                                "implementation": "cpython",
                                "requires": "==3.12.*",
                            }
                        },
                        "resource": [{"name": "base", "path": ["Bundle"]}],
                        "agent": {
                            "child_exec": "python/python.exe",
                            "child_args": ["-u", "agent/bootstrap.py"],
                        },
                        "task": [],
                    },
                )
                self._write_json(source / "Bundle" / "pipeline.json", {})
                self._write_text(source / "python" / "python.exe", "embedded runtime")
                self._write_text(source / "agent" / "bootstrap.py", "print('agent')\n")
                requirements = case.get("requirements")
                if isinstance(requirements, str):
                    self._write_text(source / "requirements.txt", requirements)
                extra = case.get("extra")
                if isinstance(extra, dict):
                    for relative, content in extra.items():
                        self._write_text(source / relative, content)

                if case["expected"]:
                    resolved = self.store.import_project(
                        source,
                        f"deps-{name}",
                        "1.0",
                        activate=False,
                    )
                    self.assertIs(
                        resolved["manifest"]["runtime"][
                            "sharedAgentDependenciesComplete"
                        ],
                        True,
                    )
                else:
                    with self.assertRaisesRegex(
                        MaaFWProjectStoreError,
                        "dependencies are incomplete",
                    ):
                        self.store.import_project(
                            source,
                            f"deps-{name}",
                            "1.0",
                            activate=False,
                        )

    def test_root_resource_still_excludes_known_shells_and_embedded_runtime(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "opaque",
                "resource": [{"name": "all", "path": ["."]}],
                "agent": {"type": "Custom", "child_exec": "Agent.exe"},
                "task": [],
            },
        )
        self._write_text(self.source / "Bundle" / "pipeline.json", "{}")
        self._write_text(self.source / "Agent.exe", "required opaque agent")
        self._write_text(self.source / "MFAAvalonia.exe", "ui")
        self._write_text(self.source / "MFAAvalonia.deps.json", "{}")
        self._write_text(self.source / "MXU.exe", "ui")
        self._write_text(self.source / "MFW.resources" / "ui.dat", "ui")
        self._write_text(self.source / "python" / "python.exe", "runtime")

        resolved = self.store.import_project(self.source, "opaque", "1.0")
        data_path = Path(resolved["dataPath"])
        self.assertTrue((data_path / "Bundle" / "pipeline.json").is_file())
        self.assertTrue((data_path / "Agent.exe").is_file())
        self.assertFalse((data_path / "MFAAvalonia.exe").exists())
        self.assertFalse((data_path / "MFAAvalonia.deps.json").exists())
        self.assertFalse((data_path / "MXU.exe").exists())
        self.assertFalse((data_path / "MFW.resources").exists())
        self.assertFalse((data_path / "python").exists())
        self.assertTrue(resolved["manifest"]["flags"]["opaqueAgent"])
        self.assertTrue(resolved["manifest"]["flags"]["conservative"])
        self.assertFalse(
            resolved["manifest"]["runtime"]["sharedAgentDependenciesComplete"]
        )

    def test_unsafe_parent_path_is_rejected_instead_of_creating_broken_version(self) -> None:
        outside = self.temp_root / "outside"
        outside.mkdir()
        self._write_json(outside / "pipeline.json", {})
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "unsafe",
                "resource": [
                    {"name": "escape", "path": ["../outside"]},
                ],
                "task": [],
            },
        )

        with self.assertRaises(MaaFWProjectStoreError):
            self.store.import_project(self.source, "unsafe", "1.0")
        self.assertEqual(self.store.list_projects(), [])

    def test_version_crud_reference_reconciliation_and_delete_protection(self) -> None:
        self._write_minimal_project()
        self.store.import_project(self.source, "crud", "1.0", activate=True)
        self.store.import_project(self.source, "crud", "2.0", activate=False)

        self.assertEqual(self.store.resolve_project("crud", touch=False)["version"], "1.0")
        self.store.switch_version("crud", "2.0")
        self.store.set_references("crud", "1.0", ["script:a", "script:a", "task:b"])
        with self.assertRaises(MaaFWProjectStoreError):
            self.store.delete_version("crud", "1.0")

        reconciled = self.store.set_references("crud", "1.0", [])
        self.assertEqual(reconciled["manifest"]["runtime"]["references"], [])
        leased = self.store.acquire_lease(
            "crud",
            "1.0",
            owner="worker:one",
            lease_id="lease-one",
        )
        self.assertEqual(leased["lease"]["leaseId"], "lease-one")
        self.assertEqual(leased["activeLeaseIds"], ["lease-one"])
        with self.assertRaises(MaaFWProjectStoreError):
            self.store.delete_version("crud", "1.0")
        self.store.release_lease("crud", "1.0", lease_id="lease-one")
        self.store.bind_runtime("crud", "1.0", binding={"runtimeId": "maafw-5"})
        deleted = self.store.delete_version("crud", "1.0")
        self.assertTrue(deleted["deleted"])
        self.assertEqual([item["version"] for item in self.store.list_versions("crud")], ["2.0"])
        with self.assertRaises(MaaFWProjectStoreError):
            self.store.delete_version("crud", "2.0")

    def test_version_dto_exposes_only_unexpired_active_lease_ids(self) -> None:
        self._write_minimal_project()
        self.store.import_project(self.source, "leases", "1.0", activate=False)
        with patch(
            "automas_maafw_project_store.service.time.time",
            return_value=1_000.0,
        ):
            self.store.acquire_lease(
                "leases",
                "1.0",
                owner="worker:one",
                lease_id="lease-active",
                ttl_seconds=10,
            )
        with patch(
            "automas_maafw_project_store.service.time.time",
            return_value=1_005.0,
        ):
            active = self.store.list_versions("leases")[0]
        with patch(
            "automas_maafw_project_store.service.time.time",
            return_value=1_011.0,
        ):
            expired = self.store.list_versions("leases")[0]

        self.assertEqual(active["activeLeaseIds"], ["lease-active"])
        self.assertEqual(expired["activeLeaseIds"], [])
        self.assertEqual(
            expired["manifest"]["runtime"]["leases"][0]["leaseId"],
            "lease-active",
        )

    def test_gc_dry_run_grace_and_keep_latest(self) -> None:
        self._write_minimal_project()
        self.store.import_project(self.source, "gc", "1.0", activate=False)
        self.store.import_project(self.source, "gc", "2.0", activate=True)
        self.store.import_project(self.source, "gc", "3.0", activate=False)

        future = time.time() + 3600
        preview = self.store.collect_garbage(
            project_id="gc",
            dry_run=True,
            grace_seconds=0,
            keep_latest=1,
            now=future,
        )
        self.assertEqual(
            [(item["projectId"], item["version"]) for item in preview["candidates"]],
            [("gc", "1.0")],
        )
        self.assertEqual(preview["deleted"], [])

        applied = self.store.collect_garbage(
            project_id="gc",
            dry_run=False,
            grace_seconds=0,
            keep_latest=1,
            now=future,
        )
        self.assertEqual([item["version"] for item in applied["deleted"]], ["1.0"])
        self.assertGreater(applied["reclaimedBytes"], 0)
        self.assertEqual(
            {item["version"] for item in self.store.list_versions("gc")},
            {"2.0", "3.0"},
        )

    def test_real_gc_refuses_incomplete_inventory_but_dry_run_reports_it(self) -> None:
        self._write_minimal_project()
        imported = self.store.import_project(
            self.source,
            "gc-safe",
            "1.0",
            activate=False,
        )
        corrupt = self.store.root / "projects" / "corrupt-project"
        corrupt.mkdir()

        preview = self.store.collect_garbage(
            dry_run=True,
            grace_seconds=0,
            keep_latest=0,
            now=time.time() + 3600,
        )
        self.assertFalse(preview["complete"])
        self.assertTrue(preview["inventoryErrors"])
        self.assertEqual(preview["deleted"], [])
        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "inventory is incomplete",
        ):
            self.store.collect_garbage(
                dry_run=False,
                grace_seconds=0,
                keep_latest=0,
                now=time.time() + 3600,
            )
        self.assertTrue(Path(imported["dataPath"]).is_dir())

    def test_gc_refuses_manifest_with_malformed_lease(self) -> None:
        self._write_minimal_project()
        imported = self.store.import_project(
            self.source,
            "gc-lease-safe",
            "1.0",
            activate=False,
        )
        manifest_path = Path(imported["manifestPath"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime"]["leases"] = [
            {
                "leaseId": "possibly-active",
                "owner": "runner",
                "expiresAt": "not-a-timestamp",
            }
        ]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        preview = self.store.collect_garbage(
            dry_run=True,
            grace_seconds=0,
            keep_latest=0,
            now=time.time() + 3600,
        )
        self.assertFalse(preview["complete"])
        self.assertTrue(preview["inventoryErrors"])
        self.assertEqual(preview["deleted"], [])
        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "inventory is incomplete",
        ):
            self.store.collect_garbage(
                dry_run=False,
                grace_seconds=0,
                keep_latest=0,
                now=time.time() + 3600,
            )
        self.assertTrue(Path(imported["dataPath"]).is_dir())

    def test_same_version_is_idempotent_but_cannot_be_overwritten(self) -> None:
        self._write_minimal_project()
        first = self.store.import_project(self.source, "immutable", "1.0")
        second = self.store.import_project(self.source, "immutable", "1.0")
        self.assertEqual(
            first["manifest"]["source"]["hash"],
            second["manifest"]["source"]["hash"],
        )

        self._write_text(self.source / "Bundle" / "pipeline.json", '{"changed": true}')
        with self.assertRaises(MaaFWProjectStoreError):
            self.store.import_project(self.source, "immutable", "1.0")

    def test_source_without_python_runtime_uses_framed_projected_hash(self) -> None:
        self._write_minimal_project()
        projected_hash = project_store_service._calculate_projected_source_hash(
            self.source,
            [Path("interface.json"), Path("Bundle/pipeline.json")],
        )

        imported = self.store.import_project(self.source, "legacy-hash", "1.0")

        self.assertIsNone(imported["manifest"]["runtime"]["python"])
        source_hash = imported["manifest"]["source"]["hash"]
        payload_hash = imported["manifest"]["payload"]["hash"]
        self.assertEqual(
            source_hash["value"],
            projected_hash,
        )
        self.assertEqual(source_hash["schemaVersion"], 2)
        self.assertEqual(payload_hash["schemaVersion"], 2)
        self.assertEqual(
            source_hash["domain"],
            project_store_service._PROJECTED_SOURCE_HASH_DOMAIN_NAME,
        )
        self.assertEqual(
            payload_hash["domain"],
            project_store_service._STORE_PAYLOAD_HASH_DOMAIN_NAME,
        )
        self.assertEqual(source_hash["framing"], payload_hash["framing"])

    def test_python_runtime_hash_is_idempotent_and_tracks_constraint(self) -> None:
        def write_bundled_python_project(root: Path, compact_version: str) -> None:
            self._write_json(
                root / "interface.json",
                {
                    "interface_version": 2,
                    "name": "bundled-python-hash",
                    "resource": [{"name": "base", "path": ["Bundle"]}],
                    "agent": {
                        "child_exec": "python/python.exe",
                        "child_args": ["-u", "agent/bootstrap.py"],
                    },
                    "task": [],
                },
            )
            self._write_json(root / "Bundle" / "pipeline.json", {})
            self._write_text(root / "python" / "python.exe", "embedded runtime")
            self._write_text(
                root / "python" / f"python{compact_version}._pth",
                f"python{compact_version}.zip\n",
            )
            self._write_text(root / "agent" / "bootstrap.py", "pass\n")
            self._write_text(root / "requirements.txt", "maafw==5.10.4\n")

        source_312 = self.temp_root / "release-python-312"
        source_313 = self.temp_root / "release-python-313"
        write_bundled_python_project(source_312, "312")
        write_bundled_python_project(source_313, "313")

        first = self.store.import_project(source_312, "python-312", "1.0")
        repeated = self.store.import_project(source_312, "python-312", "1.0")
        different_constraint = self.store.import_project(
            source_313,
            "python-313",
            "1.0",
        )

        self.assertEqual(
            first["manifest"]["source"]["hash"],
            repeated["manifest"]["source"]["hash"],
        )
        self.assertEqual(
            first["manifest"]["payload"]["hash"],
            different_constraint["manifest"]["payload"]["hash"],
        )
        self.assertEqual(
            first["manifest"]["runtime"]["python"]["constraint"],
            "==3.12.*",
        )
        self.assertEqual(
            different_constraint["manifest"]["runtime"]["python"]["constraint"],
            "==3.13.*",
        )
        self.assertNotEqual(
            first["manifest"]["source"]["hash"],
            different_constraint["manifest"]["source"]["hash"],
        )

    def test_zip_import_infers_declared_version_and_exposes_inventory(self) -> None:
        archive = self.temp_root / "M9A-release-name-does-not-match.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr(
                "M9A/interface.json",
                json.dumps(
                    {
                        "interface_version": 2,
                        "name": "m9a",
                        "version": "v1.2.3",
                        "controller": [{"name": "adb", "type": "Adb"}],
                        "resource": [{"name": "base", "path": ["Bundle"]}],
                        "task": [{"name": "Daily"}],
                    }
                ),
            )
            output.writestr("M9A/Bundle/pipeline.json", "{}")
            output.writestr("M9A/MFW.exe", "shell")

        resolved = self.store.import_project(archive, "m9a")

        self.assertEqual(resolved["version"], "v1.2.3")
        self.assertEqual(resolved["manifest"]["source"]["kind"], "zip")
        self.assertEqual(resolved["manifest"]["source"]["projectPath"], ".")
        self.assertEqual(len(resolved["manifest"]["source"]["archiveSha256"]), 64)
        self.assertEqual(
            resolved["summary"]["capabilities"]["controllerTypes"],
            ["Adb"],
        )
        self.assertEqual(
            resolved["summary"]["capabilities"]["taskNames"],
            ["Daily"],
        )
        self.assertEqual(resolved["summary"]["shells"]["families"], ["MFW"])
        self.assertEqual(
            self.store.list_versions("m9a")[0]["summary"],
            resolved["summary"],
        )
        projects = self.store.list_projects()
        self.assertEqual(projects[0]["summary"], resolved["summary"])
        self.assertEqual(
            projects[0]["versionSummaries"][0]["summary"],
            resolved["summary"],
        )
        self.assertEqual(list((self.store.root / ".staging").iterdir()), [])

    def test_explicit_version_must_match_project_interface_version(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "versioned",
                "version": "v2.0.0",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})

        equivalent = self.store.import_project(
            self.source,
            "versioned",
            "2.0.0",
            activate=False,
        )
        self.assertEqual(equivalent["version"], "v2.0.0")

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "does not match ProjectInterface version",
        ):
            self.store.import_project(self.source, "mismatch", "3.0.0")

        unversioned = self.temp_root / "release-v9.9.9"
        self._write_json(
            unversioned / "interface.json",
            {
                "interface_version": 2,
                "name": "unversioned",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_json(unversioned / "Bundle" / "pipeline.json", {})
        with self.assertRaisesRegex(MaaFWProjectStoreError, "version is required"):
            self.store.import_project(unversioned, "unversioned")

        self.assertEqual(
            {item["projectId"] for item in self.store.list_projects()},
            {"versioned"},
        )

    def test_zip_rejects_traversal_links_devices_and_expansion_bombs(self) -> None:
        unsafe_members: list[tuple[str, zipfile.ZipInfo | str, bytes]] = [
            ("traversal", "../escape.txt", b"escape"),
        ]
        symlink = zipfile.ZipInfo("link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        unsafe_members.append(("symlink", symlink, b"target"))
        device = zipfile.ZipInfo("device")
        device.create_system = 3
        device.external_attr = (stat.S_IFCHR | 0o666) << 16
        unsafe_members.append(("device", device, b""))

        for name, member, content in unsafe_members:
            with self.subTest(name=name):
                archive = self.temp_root / f"{name}.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    output.writestr(member, content)
                with self.assertRaises(MaaFWProjectStoreError):
                    self.store.import_project(archive, name)
                self.assertEqual(list((self.store.root / ".staging").iterdir()), [])

        bomb = self.temp_root / "bomb.zip"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("payload.bin", b"x" * 65)
        with patch.object(
            project_store_service,
            "MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES",
            64,
        ):
            with self.assertRaisesRegex(
                MaaFWProjectStoreError,
                "uncompressed size exceeds",
            ):
                self.store.import_project(bomb, "bomb")
        self.assertEqual(list((self.store.root / ".staging").iterdir()), [])

    def test_tree_hash_v2_frames_content_lengths_and_separates_domains(self) -> None:
        one_file = self.temp_root / "hash-one-file"
        two_files = self.temp_root / "hash-two-files"
        one_file.mkdir()
        two_files.mkdir()
        prefix = b"prefix"
        suffix = b"suffix"
        self._write_text(two_files / "a", prefix.decode("ascii"))
        self._write_text(two_files / "b", suffix.decode("ascii"))
        (one_file / "a").write_bytes(
            prefix + len(b"b").to_bytes(8, "big") + b"b" + suffix
        )

        legacy_one = project_store_service._calculate_projected_source_hash_legacy(
            one_file,
            [Path("a")],
        )
        legacy_two = project_store_service._calculate_projected_source_hash_legacy(
            two_files,
            [Path("a"), Path("b")],
        )
        framed_one = project_store_service._calculate_projected_source_hash(
            one_file,
            [Path("a")],
        )
        framed_two = project_store_service._calculate_projected_source_hash(
            two_files,
            [Path("a"), Path("b")],
        )

        self.assertEqual(legacy_one, legacy_two)
        self.assertNotEqual(framed_one, framed_two)
        self.assertNotEqual(
            framed_two,
            project_store_service._calculate_tree_hash(
                two_files,
                [Path("a"), Path("b")],
                domain=project_store_service._STORE_PAYLOAD_HASH_DOMAIN,
            ),
        )

    def test_schema_two_manifest_migrates_without_changing_hash_identity(self) -> None:
        self._write_minimal_project()
        imported = self.store.import_project(
            self.source,
            "legacy-manifest",
            "1.0",
        )
        data_path = Path(imported["dataPath"])
        manifest_path = Path(imported["manifestPath"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy_source_hash = (
            project_store_service._calculate_projected_source_hash_legacy(
                self.source,
                [Path("interface.json"), Path("Bundle/pipeline.json")],
            )
        )
        legacy_payload_hash = project_store_service._calculate_store_payload_hash(
            data_path,
            hash_schema_version=(
                project_store_service._LEGACY_TREE_HASH_SCHEMA_VERSION
            ),
        )
        manifest["schemaVersion"] = (
            project_store_service.LEGACY_MANIFEST_SCHEMA_VERSION
        )
        manifest.pop("hashCompatibility", None)
        for section_name, digest in (
            ("source", legacy_source_hash),
            ("payload", legacy_payload_hash),
        ):
            hash_value = manifest[section_name]["hash"]
            hash_value["value"] = digest
            hash_value.pop("schemaVersion", None)
            hash_value.pop("domain", None)
            hash_value.pop("framing", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        first_checkout = self.store.checkout_project(
            "legacy-manifest",
            "1.0",
            "script-one",
        )
        migrated = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            migrated["schemaVersion"],
            project_store_service.MANIFEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            migrated["source"]["hash"]["value"],
            legacy_source_hash,
        )
        self.assertEqual(
            migrated["payload"]["hash"]["value"],
            legacy_payload_hash,
        )
        self.assertEqual(migrated["source"]["hash"]["schemaVersion"], 1)
        self.assertEqual(migrated["payload"]["hash"]["schemaVersion"], 1)
        self.assertEqual(
            migrated["source"]["hash"]["framing"],
            project_store_service._LEGACY_TREE_HASH_FRAMING,
        )
        self.assertEqual(
            migrated["hashCompatibility"][
                "migratedFromManifestSchemaVersion"
            ],
            project_store_service.LEGACY_MANIFEST_SCHEMA_VERSION,
        )

        second_checkout = self.store.checkout_project(
            "legacy-manifest",
            "1.0",
            "script-one",
        )
        self.assertEqual(
            second_checkout["checkoutId"],
            first_checkout["checkoutId"],
        )
        self.assertTrue(second_checkout["reused"])
        self.assertTrue(self.store.inventory()["complete"])

    def test_manifest_validator_fails_closed_across_resolve_and_list_paths(self) -> None:
        self._write_minimal_project()
        imported = self.store.import_project(
            self.source,
            "manifest-validation",
            "1.0",
        )
        manifest_path = Path(imported["manifestPath"])
        original = json.loads(manifest_path.read_text(encoding="utf-8"))

        def missing_source_hash(value: dict[str, object]) -> None:
            del value["source"]["hash"]  # type: ignore[index]

        def invalid_payload_scope(value: dict[str, object]) -> None:
            value["payload"]["hash"]["scope"] = "other"  # type: ignore[index]

        def missing_project_interface(value: dict[str, object]) -> None:
            value["projectInterface"]["path"] = "missing.json"  # type: ignore[index]

        def wrong_project_interface(value: dict[str, object]) -> None:
            value["projectInterface"]["path"] = (  # type: ignore[index]
                "Bundle/pipeline.json"
            )

        def malformed_binding(value: dict[str, object]) -> None:
            value["runtime"]["binding"] = []  # type: ignore[index]

        def identityless_binding(value: dict[str, object]) -> None:
            value["runtime"]["binding"] = {}  # type: ignore[index]

        def mismatched_runtime_constraint(value: dict[str, object]) -> None:
            value["runtimeConstraint"] = "==different"

        mutations = {
            "source": missing_source_hash,
            "payload": invalid_payload_scope,
            "project-interface": missing_project_interface,
            "project-interface-identity": wrong_project_interface,
            "binding-type": malformed_binding,
            "binding-identity": identityless_binding,
            "runtime-constraint": mismatched_runtime_constraint,
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                corrupted = json.loads(json.dumps(original))
                mutate(corrupted)
                manifest_path.write_text(json.dumps(corrupted), encoding="utf-8")
                for operation in (
                    lambda: self.store.resolve_project(
                        "manifest-validation",
                        "1.0",
                        touch=False,
                    ),
                    lambda: self.store.list_versions("manifest-validation"),
                    self.store.list_projects,
                ):
                    with self.assertRaises(MaaFWProjectStoreError):
                        operation()
                inventory = self.store.inventory()
                self.assertFalse(inventory["complete"])
                self.assertTrue(inventory["errors"])
                manifest_path.write_text(json.dumps(original), encoding="utf-8")

    def test_invalid_runtime_binding_is_rejected_before_manifest_write(self) -> None:
        self._write_minimal_project()
        imported = self.store.import_project(
            self.source,
            "binding-validation",
            "1.0",
        )
        manifest_path = Path(imported["manifestPath"])
        before = manifest_path.read_bytes()

        with self.assertRaisesRegex(
            MaaFWProjectStoreError,
            "runtime.binding.runtimeId",
        ):
            self.store.bind_runtime(
                "binding-validation",
                "1.0",
                binding={},
            )

        self.assertEqual(manifest_path.read_bytes(), before)

    def _write_minimal_project(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "minimal",
                "resource": [{"name": "base", "path": ["Bundle"]}],
                "task": [],
            },
        )
        self._write_json(self.source / "Bundle" / "pipeline.json", {})

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
