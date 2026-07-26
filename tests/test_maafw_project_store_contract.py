from __future__ import annotations

import json
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "packages" / "automas_maafw_project_store"
PACKAGE_SRC = PACKAGE_ROOT / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from automas_maafw_project_store import (  # noqa: E402
    MANIFEST_FILE_NAME,
    MaaFWProjectStoreError,
    MaaFWProjectStoreService,
)


class MaaFWProjectStoreContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp_dir.name)
        self.source = self.temp_root / "release"
        self.source.mkdir()
        self.store = MaaFWProjectStoreService(self.temp_root / "store")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plugin_entrypoint_and_default_instance_contract(self) -> None:
        pyproject = tomllib.loads(
            (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        entrypoints = pyproject["project"]["entry-points"]["auto_mas.plugins"]
        self.assertEqual(
            entrypoints["automas_maafw_project_store"],
            "automas_maafw_project_store.plugin:Plugin",
        )
        plugin_source = (
            PACKAGE_SRC / "automas_maafw_project_store" / "plugin.py"
        ).read_text(encoding="utf-8")
        self.assertIn('provides = ["maafw.project_store.v1"]', plugin_source)
        self.assertIn("DEFAULT_INSTANCE", plugin_source)

    def test_root_project_projection_preserves_resources_and_clears_all_hashes(self) -> None:
        self._write_json(
            self.source / "interface.json",
            {
                "interface_version": 2,
                "name": "demo",
                "version": "1.0.0",
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
                "projectId",
                "version",
                "runtimeConstraint",
                "manifestPath",
                "projectInterfacePath",
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
            manifest["projectInterface"]["clearedResources"],
            [
                {"file": "config/tasks.json", "resource": "fragment-resource"},
                {"file": "interface.json", "resource": "zh"},
            ],
        )
        self.assertIn("Bundle/base/pipeline/main.json", manifest["projection"]["copied"])

    def test_assets_project_is_promoted_and_parent_agent_paths_are_rewritten(self) -> None:
        assets = self.source / "assets"
        self._write_json(
            assets / "interface.json",
            {
                "interface_version": 2,
                "name": "nested",
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
        self._write_text(self.source / "agent" / "bootstrap.py", "print('agent')\n")
        self._write_text(
            self.source / "requirements.txt",
            "maafw==5.10.4\nhttpx==0.28.1\n",
        )

        resolved = self.store.import_project(self.source, "bundled-python", "4.5")
        data_path = Path(resolved["dataPath"])
        runtime = resolved["manifest"]["runtime"]

        self.assertFalse((data_path / "python" / "python.exe").exists())
        self.assertTrue((data_path / "agent" / "bootstrap.py").is_file())
        self.assertEqual(
            runtime["agent"][0]["strippedInterpreter"],
            {
                "sourcePath": "python/python.exe",
                "reason": "embedded-python",
                "retainedEntrypoints": ["agent/bootstrap.py"],
            },
        )
        self.assertTrue(runtime["sharedAgentDependenciesComplete"])
        self.assertTrue(
            any(
                "embedded Python interpreter was stripped" in warning
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
                    case["expected"],
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
        with self.assertRaises(MaaFWProjectStoreError):
            self.store.delete_version("crud", "1.0")
        self.store.release_lease("crud", "1.0", lease_id="lease-one")
        self.store.bind_runtime("crud", "1.0", binding={"runtimeId": "maafw-5"})
        deleted = self.store.delete_version("crud", "1.0")
        self.assertTrue(deleted["deleted"])
        self.assertEqual([item["version"] for item in self.store.list_versions("crud")], ["2.0"])
        with self.assertRaises(MaaFWProjectStoreError):
            self.store.delete_version("crud", "2.0")

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
