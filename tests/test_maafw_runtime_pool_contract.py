from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_POOL_PACKAGE = ROOT / "packages" / "automas_maafw_runtime_pool"
RUNTIME_POOL_SOURCE = RUNTIME_POOL_PACKAGE / "src"
RUNNER_PACKAGE = ROOT / "packages" / "automas_maafw_runner"
RUNNER_SOURCE = RUNNER_PACKAGE / "src" / "automas_maafw_runner"
AGENT_ENV_PACKAGE = ROOT / "packages" / "automas_maafw_agent_env"
AGENT_ENV_SOURCE = AGENT_ENV_PACKAGE / "src"
INTERFACE_SOURCE = ROOT / "packages" / "automas_maafw_interface" / "src"

for source_path in (RUNTIME_POOL_SOURCE, AGENT_ENV_SOURCE, INTERFACE_SOURCE):
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))

from automas_maafw_runtime_pool import (  # noqa: E402
    MaaFWRuntimeIdentityError,
    MaaFWRuntimePoolError,
    MaaFWRuntimePoolService,
    build_runtime_identity,
)
from automas_maafw_agent_env.planner import (  # noqa: E402
    build_maafw_agent_command_plans,
)


class MaaFWRuntimePoolContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.pool_root = Path(self.temporary_directory.name) / "pool"
        self.install_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.service = MaaFWRuntimePoolService(
            self.pool_root,
            installer=self._fake_installer,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _fake_installer(
        self,
        environment_path: Path,
        requirements: tuple[str, ...] | list[str],
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        self.install_calls.append((tuple(requirements), identity))
        scripts_dir = environment_path / ("Scripts" if os.name == "nt" else "bin")
        scripts_dir.mkdir(parents=True, exist_ok=False)
        python_name = "python.exe" if os.name == "nt" else "python"
        python_executable = scripts_dir / python_name
        python_executable.write_text("fake runtime", encoding="utf-8")
        maafw_version = next(
            (
                item.split("==", 1)[1]
                for item in requirements
                if item.casefold().startswith("maafw==")
            ),
            None,
        )
        return {
            "pythonExecutable": str(python_executable),
            "maafwVersion": maafw_version or "test",
            "resolvedRequirements": list(requirements),
            "source": "fake-installer",
            "installer": {
                "name": "uv",
                "version": "test",
                "environment": "uv-venv",
                "dependencies": "uv-pip",
            },
            "cache": {
                "kind": "uv",
                "scope": "pool",
                "shared": True,
                "path": str(self.pool_root / "cache" / "uv"),
                "relativeToPool": "cache/uv",
            },
            "link": {"mode": "hardlink"},
        }

    def test_canonical_selector_is_shared_without_project_path_identity(self) -> None:
        first = self.service.ensure_runtime(
            {
                "requirements": ["MaaFW==4.3.0", "json5==0.14.0"],
                "metadata": {"projectPath": "C:/projects/one"},
            }
        )
        second = self.service.ensure_runtime(
            {
                "packages": ["json5 == 0.14.0", "maafw == 4.3.0"],
                "metadata": {"projectPath": "D:/projects/two"},
            }
        )

        self.assertEqual(first["runtimeId"], second["runtimeId"])
        self.assertEqual(len(self.install_calls), 1)
        self.assertRegex(first["runtimeId"], r"^maafw-runtime-[0-9a-f]{24}$")
        self.assertTrue(
            {
                "runtimeId",
                "pythonExecutable",
                "venvPath",
                "packages",
                "maafwRequirement",
                "maafwVersion",
                "selectorRequirements",
                "resolvedRequirements",
            }.issubset(first)
        )
        self.assertEqual(first["maafwRequirement"], "maafw==4.3.0")
        self.assertEqual(first["maafwVersion"], "4.3.0")
        self.assertEqual(first["selectorRequirements"], first["packages"])
        self.assertEqual(
            first["resolvedRequirements"],
            ["json5==0.14.0", "maafw==4.3.0"],
        )
        self.assertRegex(first["pythonPatchVersion"], r"^\d+\.\d+\.\d+")
        installer_metadata = first["installerMetadata"]
        self.assertEqual(installer_metadata["installer"]["name"], "uv")
        self.assertTrue(installer_metadata["cache"]["shared"])
        self.assertEqual(installer_metadata["cache"]["relativeToPool"], "cache/uv")
        self.assertEqual(installer_metadata["link"]["mode"], "hardlink")
        json.dumps(first)

        identity = build_runtime_identity(["maafw==4.3.0"])
        self.assertRegex(identity["pythonVersion"], r"^\d+\.\d+$")
        self.assertNotIn("projectPath", identity)

    def test_different_maafw_versions_are_isolated(self) -> None:
        first = self.service.ensure_runtime("maafw==4.2.0")
        second = self.service.ensure_runtime("maafw==4.3.0")

        self.assertNotEqual(first["runtimeId"], second["runtimeId"])
        self.assertNotEqual(first["venvPath"], second["venvPath"])
        self.assertEqual(len(self.install_calls), 2)

    def test_manifest_is_atomic_and_runtime_id_is_validated_before_install(self) -> None:
        runtime = self.service.ensure_runtime(
            {"requirements": ["maafw==4.3.0", "pydantic==2.11.7"]}
        )
        manifest_path = Path(runtime["path"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["runtimeId"], runtime["runtimeId"])
        self.assertEqual(
            manifest["selectorRequirements"],
            runtime["selectorRequirements"],
        )
        self.assertEqual(
            manifest["resolvedRequirements"],
            runtime["resolvedRequirements"],
        )
        self.assertEqual(list((self.pool_root / ".staging").iterdir()), [])
        self.assertEqual(list(self.pool_root.rglob("*.tmp-*")), [])

        install_count = len(self.install_calls)
        wrong_runtime_id = "maafw-runtime-" + ("0" * 24)
        with self.assertRaises(MaaFWRuntimePoolError):
            self.service.ensure_runtime(
                {
                    "runtimeId": wrong_runtime_id,
                    "requirements": ["maafw==4.3.0"],
                }
            )
        self.assertEqual(len(self.install_calls), install_count)

        by_id = self.service.ensure_runtime({"runtimeId": runtime["runtimeId"]})
        self.assertEqual(by_id["runtimeId"], runtime["runtimeId"])
        self.assertIsNone(
            self.service.resolve_runtime(
                {
                    "runtimeId": runtime["runtimeId"],
                    "maafwRequirement": "maafw==9.9.9",
                }
            )
        )

    def test_failed_install_leaves_no_partial_runtime(self) -> None:
        def fail_install(
            environment_path: Path,
            requirements: tuple[str, ...] | list[str],
            identity: dict[str, Any],
        ) -> dict[str, Any]:
            del requirements, identity
            environment_path.mkdir(parents=True)
            (environment_path / "partial.txt").write_text(
                "partial",
                encoding="utf-8",
            )
            raise RuntimeError("injected install failure")

        failing_service = MaaFWRuntimePoolService(
            self.pool_root,
            installer=fail_install,
        )
        with self.assertRaisesRegex(RuntimeError, "injected install failure"):
            failing_service.ensure_runtime("maafw==4.3.0")

        self.assertEqual(list((self.pool_root / ".staging").iterdir()), [])
        self.assertEqual(failing_service.list_runtimes(), [])

    def test_references_pins_and_leases_block_deletion_until_reconciled(self) -> None:
        runtime = self.service.ensure_runtime("maafw==4.3.0")
        runtime_id = runtime["runtimeId"]

        reconciled = self.service.set_references(
            runtime_id,
            ["project:b@2", "project:a@1", "project:a@1"],
        )
        self.assertEqual(
            reconciled["references"],
            ["project:a@1", "project:b@2"],
        )
        self.assertEqual(self.service.delete(runtime_id)["blocked"], ["referenced"])

        self.service.reconcile_references(runtime_id, [])
        self.service.pin(runtime_id, True)
        self.assertEqual(self.service.delete(runtime_id)["blocked"], ["pinned"])

        self.service.pin(runtime_id, False)
        self.service.acquire_lease(runtime_id, "worker-1", owner="runner")
        self.assertEqual(self.service.delete(runtime_id)["blocked"], ["leased"])

        self.service.release_lease(runtime_id, "worker-1")
        deleted = self.service.delete(runtime_id)
        self.assertTrue(deleted["deleted"])
        self.assertFalse(Path(runtime["path"]).exists())

    def test_runner_environment_holds_lease_until_explicit_release(self) -> None:
        module_name = "_automas_maafw_runner_environment_contract"
        module_path = RUNNER_SOURCE / "environment.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        project_path = Path(self.temporary_directory.name) / "project"
        project_path.mkdir()
        (project_path / ".auto_mas_maafw_project.json").write_text(
            json.dumps(
                {
                    "runtime": {
                        "binding": {
                            "runtimeId": "maafw-runtime-" + ("0" * 24),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        environment = module.prepare_runner_environment(
            project_path,
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
            runtime_requirement="maafw==4.3.0",
            lease_ttl_seconds=None,
        )

        self.assertEqual(environment.runtime_pool_root, self.service.pool.root)
        self.assertTrue(environment.lease_id)
        self.assertEqual(
            self.service.delete(environment.runtime_id)["blocked"],
            ["leased"],
        )

        released = module.release_runner_environment(environment)
        self.assertEqual(released["activeLeaseIds"], [])
        self.assertTrue(self.service.delete(environment.runtime_id)["deleted"])

    def test_runner_collects_stale_runtime_after_current_lease_is_acquired(
        self,
    ) -> None:
        module_name = "_automas_maafw_runner_automatic_gc_contract"
        module_path = RUNNER_SOURCE / "environment.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        project_path = Path(self.temporary_directory.name) / "gc-project"
        project_path.mkdir()
        stale_packages = module.build_runner_packages(
            project_path,
            maafw_requirement="maafw==4.2.0",
        )
        stale = self.service.ensure_runtime({"requirements": stale_packages})
        self.service.touch(stale["runtimeId"], at="2000-01-01T00:00:00Z")

        logs: list[str] = []
        environment = module.prepare_runner_environment(
            project_path,
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
            runtime_requirement="maafw==4.3.0",
            send_log=logs.append,
        )
        try:
            self.assertFalse(Path(stale["path"]).exists())
            self.assertTrue(Path(environment.venv_path).exists())
            self.assertIn(stale["runtimeId"], "\n".join(logs))
            self.assertEqual(
                self.service.delete(environment.runtime_id)["blocked"],
                ["leased"],
            )
        finally:
            module.release_runner_environment(environment)

    def test_runner_gc_failure_warns_once_without_blocking_prepares(self) -> None:
        module_name = "_automas_maafw_runner_automatic_gc_failure_contract"
        module_path = RUNNER_SOURCE / "environment.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        project_path = Path(self.temporary_directory.name) / "gc-failure-project"
        project_path.mkdir()
        logs: list[str] = []
        with mock.patch.object(
            self.service.pool,
            "gc",
            side_effect=RuntimeError("injected gc failure"),
        ) as gc_mock:
            environment = module.prepare_runner_environment(
                project_path,
                runtime_pool=self.service.pool,
                runtime_installer=self._fake_installer,
                runtime_requirement="maafw==4.3.0",
                send_log=logs.append,
            )
            second_environment = module.prepare_runner_environment(
                project_path,
                runtime_pool=self.service.pool,
                runtime_installer=self._fake_installer,
                runtime_requirement="maafw==4.3.0",
                send_log=logs.append,
            )
        try:
            self.assertTrue(Path(environment.venv_path).exists())
            self.assertTrue(Path(second_environment.venv_path).exists())
            self.assertIn("injected gc failure", "\n".join(logs))
            self.assertEqual(gc_mock.call_count, 1)
        finally:
            module.release_runner_environment(second_environment)
            module.release_runner_environment(environment)

    def test_runner_logs_uv_cache_prune_unavailable_status(self) -> None:
        module_name = "_automas_maafw_runner_cache_gc_status_contract"
        module_path = RUNNER_SOURCE / "environment.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        project_path = Path(self.temporary_directory.name) / "gc-cache-status"
        project_path.mkdir()
        logs: list[str] = []
        original_gc = self.service.pool.gc

        def gc_with_unavailable_cache(**kwargs: Any) -> dict[str, Any]:
            result = original_gc(**kwargs)
            result["cachePrune"] = {
                "status": "unavailable",
                "error": "uv executable was not found",
            }
            return result

        with mock.patch.object(
            self.service.pool,
            "gc",
            side_effect=gc_with_unavailable_cache,
        ):
            environment = module.prepare_runner_environment(
                project_path,
                runtime_pool=self.service.pool,
                runtime_installer=self._fake_installer,
                runtime_requirement="maafw==4.3.0",
                send_log=logs.append,
            )
        try:
            output = "\n".join(logs)
            self.assertIn("status=unavailable", output)
            self.assertIn("uv executable was not found", output)
        finally:
            module.release_runner_environment(environment)

    def test_runner_prefers_recovered_exact_binding_over_original_range(self) -> None:
        module_name = "_automas_maafw_runner_recovered_binding_contract"
        module_path = RUNNER_SOURCE / "environment.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        project_path = Path(self.temporary_directory.name) / "range-project"
        project_path.mkdir()
        (project_path / "requirements.txt").write_text(
            "maafw>=5,<6\nrequests==2.34.2\n",
            encoding="utf-8",
        )
        exact_packages = module.build_runner_packages(
            project_path,
            maafw_requirement="maafw==5.11.1",
        )
        recovered = self.service.ensure_runtime(
            {"requirements": exact_packages}
        )
        (project_path / ".auto_mas_maafw_project.json").write_text(
            json.dumps(
                {
                    "runtime": {
                        "constraint": ">=5,<6",
                        "binding": {"runtimeId": recovered["runtimeId"]},
                    }
                }
            ),
            encoding="utf-8",
        )

        environment = module.prepare_runner_environment(
            project_path,
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
        )
        try:
            self.assertEqual(environment.runtime_id, recovered["runtimeId"])
            self.assertEqual(environment.maafw_requirement, "maafw==5.11.1")
        finally:
            module.release_runner_environment(environment)

    def test_managed_project_rejects_unbounded_latest_but_legacy_keeps_it(
        self,
    ) -> None:
        module_name = "_automas_maafw_runner_environment_routing_contract"
        module_path = RUNNER_SOURCE / "environment.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        managed_project = Path(self.temporary_directory.name) / "managed-unbounded"
        managed_project.mkdir()
        (managed_project / ".auto_mas_maafw_project.json").write_text(
            "{}",
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError):
            module.prepare_runner_environment(
                managed_project,
                runtime_pool=self.service.pool,
                runtime_installer=self._fake_installer,
            )
        self.assertEqual(self.install_calls, [])

        legacy_project = Path(self.temporary_directory.name) / "legacy-unbounded"
        legacy_project.mkdir()
        environment = module.prepare_runner_environment(
            legacy_project,
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
        )
        self.assertEqual(environment.maafw_requirement, "maafw")
        module.release_runner_environment(environment)

    def test_gc_supports_dry_run_grace_and_keep_latest(self) -> None:
        runtimes = [
            self.service.ensure_runtime(f"maafw==4.{minor}.0")
            for minor in (1, 2, 3)
        ]
        for index, runtime in enumerate(runtimes, start=1):
            self.service.touch(
                runtime["runtimeId"],
                at=f"2029-01-0{index}T00:00:00Z",
            )

        dry_run = self.service.collect_garbage(
            dry_run=True,
            grace_seconds=0,
            keep_latest=1,
            now="2030-01-01T00:00:00Z",
        )
        self.assertEqual(
            {item["runtimeId"] for item in dry_run["candidates"]},
            {runtimes[0]["runtimeId"], runtimes[1]["runtimeId"]},
        )
        self.assertEqual(dry_run["deleted"], [])
        self.assertTrue(all(Path(item["path"]).is_dir() for item in runtimes))

        collected = self.service.collect_garbage(
            dry_run=False,
            grace_seconds=0,
            keep_latest=1,
            now="2030-01-01T00:00:00Z",
        )
        self.assertEqual(
            set(collected["deleted"]),
            {runtimes[0]["runtimeId"], runtimes[1]["runtimeId"]},
        )
        self.assertTrue(Path(runtimes[2]["path"]).is_dir())

    def test_local_dependencies_and_unsafe_delete_targets_are_rejected(self) -> None:
        with self.assertRaises(MaaFWRuntimeIdentityError):
            self.service.ensure_runtime(
                {"requirements": ["maafw==4.3.0", "helper @ file:///tmp/helper"]}
            )
        with self.assertRaises(MaaFWRuntimeIdentityError):
            self.service.ensure_runtime(
                {"requirements": ["maafw==4.3.0", "-rrequirements.txt"]}
            )
        with self.assertRaises(TypeError):
            self.service.ensure_runtime(
                {"requirements": {"maafw": "4.3.0"}}
            )

        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(MaaFWRuntimePoolError):
            self.service.delete("../outside")
        self.assertTrue(sentinel.is_file())

    def test_only_opted_in_managed_python_agent_reuses_worker(
        self,
    ) -> None:
        module_name = "_automas_maafw_runner_shared_agent_contract"
        module_path = RUNNER_SOURCE / "shared_agent.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        managed_project = Path(self.temporary_directory.name) / "managed-project"
        managed_project.mkdir()
        (managed_project / ".auto_mas_maafw_project.json").write_text(
            json.dumps(
                {
                    "runtime": {
                        "sharedAgentDependenciesComplete": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        shared_python = Path(self.temporary_directory.name) / "runtime" / "python.exe"
        shared_python.parent.mkdir()
        shared_python.write_text("fake", encoding="utf-8")
        python_agent = SimpleNamespace(
            embedded=False,
            runtimeKind="isolated_venv",
            isolatedVenvPath="C:/project-scoped/venv",
            executable="C:/project-scoped/venv/Scripts/python.exe",
            executableExists=False,
            command=["C:/project-scoped/venv/Scripts/python.exe", "agent.py"],
            childArgs=["agent.py"],
            fallbackReason=None,
        )
        binary_agent = SimpleNamespace(
            embedded=False,
            runtimeKind="project_binary",
            isolatedVenvPath=None,
            executable="agent.exe",
            executableExists=True,
            command=["agent.exe"],
            childArgs=[],
            fallbackReason=None,
        )

        routed = module.route_managed_python_agents_to_shared_runtime(
            managed_project,
            [python_agent, binary_agent],
            python_executable=shared_python,
        )
        self.assertEqual(routed, [python_agent])
        self.assertEqual(python_agent.runtimeKind, "shared_runtime")
        self.assertEqual(python_agent.command[0], str(shared_python.resolve()))
        self.assertIsNone(python_agent.isolatedVenvPath)
        self.assertEqual(binary_agent.command, ["agent.exe"])

        incomplete_project = (
            Path(self.temporary_directory.name) / "managed-incomplete-project"
        )
        incomplete_project.mkdir()
        (incomplete_project / ".auto_mas_maafw_project.json").write_text(
            json.dumps(
                {
                    "runtime": {}
                }
            ),
            encoding="utf-8",
        )
        incomplete_agent = SimpleNamespace(
            embedded=False,
            runtimeKind="isolated_venv",
            isolatedVenvPath="C:/managed-incomplete/venv",
            executable="C:/managed-incomplete/venv/Scripts/python.exe",
            executableExists=False,
            command=["C:/managed-incomplete/venv/Scripts/python.exe", "agent.py"],
            childArgs=["agent.py"],
            fallbackReason=None,
        )
        self.assertEqual(
            module.route_managed_python_agents_to_shared_runtime(
                incomplete_project,
                [incomplete_agent],
                python_executable=shared_python,
            ),
            [],
        )
        self.assertEqual(incomplete_agent.runtimeKind, "isolated_venv")

        legacy_project = Path(self.temporary_directory.name) / "legacy-project"
        legacy_project.mkdir()
        legacy_agent = SimpleNamespace(
            embedded=False,
            runtimeKind="isolated_venv",
            isolatedVenvPath="C:/legacy/venv",
            executable="C:/legacy/venv/Scripts/python.exe",
            executableExists=False,
            command=["C:/legacy/venv/Scripts/python.exe", "agent.py"],
            childArgs=["agent.py"],
            fallbackReason=None,
        )
        self.assertEqual(
            module.route_managed_python_agents_to_shared_runtime(
                legacy_project,
                [legacy_agent],
                python_executable=shared_python,
            ),
            [],
        )
        self.assertEqual(legacy_agent.runtimeKind, "isolated_venv")

    def test_bootstrap_python_agent_isolated_plan_precedes_shared_opt_in(
        self,
    ) -> None:
        module_name = "_automas_maafw_runner_shared_agent_bootstrap_contract"
        module_path = RUNNER_SOURCE / "shared_agent.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        project = Path(self.temporary_directory.name) / "m9a-bootstrap"
        (project / "agent").mkdir(parents=True)
        (project / "agent" / "bootstrap.py").write_text(
            "print('bootstrap')\n",
            encoding="utf-8",
        )
        plans = build_maafw_agent_command_plans(
            project,
            {
                "child_exec": "python/python.exe",
                "child_args": ["-u", "agent/bootstrap.py"],
            },
            managed_env_root=Path(self.temporary_directory.name) / "agent-venvs",
        )
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.runtimeKind, "isolated_venv")
        self.assertEqual(plan.childArgs, ["-u", "agent/bootstrap.py"])

        shared_python = Path(self.temporary_directory.name) / "shared" / "python.exe"
        shared_python.parent.mkdir()
        shared_python.write_text("fake", encoding="utf-8")
        self.assertEqual(
            module.route_managed_python_agents_to_shared_runtime(
                project,
                plans,
                python_executable=shared_python,
            ),
            [],
        )
        self.assertEqual(plan.runtimeKind, "isolated_venv")

        (project / ".auto_mas_maafw_project.json").write_text(
            json.dumps(
                {
                    "runtime": {
                        "sharedAgentDependenciesComplete": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            module.route_managed_python_agents_to_shared_runtime(
                project,
                plans,
                python_executable=shared_python,
            ),
            [plan],
        )
        self.assertEqual(plan.runtimeKind, "shared_runtime")

        unsafe_project = Path(self.temporary_directory.name) / "unsafe-bootstrap"
        unsafe_project.mkdir()
        outside_entry = Path(self.temporary_directory.name) / "outside.py"
        outside_entry.write_text("print('outside')\n", encoding="utf-8")
        unsafe_plan = build_maafw_agent_command_plans(
            unsafe_project,
            {
                "child_exec": "python/python.exe",
                "child_args": ["-u", "../outside.py"],
            },
            managed_env_root=Path(self.temporary_directory.name) / "agent-venvs",
        )[0]
        self.assertEqual(unsafe_plan.runtimeKind, "external")


class MaaFWRuntimePoolStaticContractTest(unittest.TestCase):
    def test_plugin_distribution_and_service_contract(self) -> None:
        pyproject = tomllib.loads(
            (RUNTIME_POOL_PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = pyproject["project"]
        entry_points = project["entry-points"]["auto_mas.plugins"]

        self.assertEqual(project["name"], "automas-maafw-runtime-pool")
        self.assertEqual(project["version"], "0.1.4")
        self.assertEqual(
            entry_points["automas_maafw_runtime_pool"],
            "automas_maafw_runtime_pool.plugin:Plugin",
        )

        plugin_tree = ast.parse(
            (
                RUNTIME_POOL_SOURCE
                / "automas_maafw_runtime_pool"
                / "plugin.py"
            ).read_text(encoding="utf-8")
        )
        default_instance = next(
            ast.literal_eval(node.value)
            for node in plugin_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DEFAULT_INSTANCE"
                for target in node.targets
            )
        )
        self.assertEqual(default_instance["name"], "MaaFW Runtime Pool")
        self.assertTrue(default_instance["enabled"])

        service_source = (
            RUNTIME_POOL_SOURCE / "automas_maafw_runtime_pool" / "service.py"
        ).read_text(encoding="utf-8")
        installer_source = (
            RUNTIME_POOL_SOURCE / "automas_maafw_runtime_pool" / "installer.py"
        ).read_text(encoding="utf-8")
        for method_name in (
            "list_runtimes",
            "resolve_runtime",
            "ensure_runtime",
            "touch",
            "pin",
            "set_references",
            "reconcile_references",
            "acquire_lease",
            "release_lease",
            "delete",
            "collect_garbage",
        ):
            self.assertIn(f"def {method_name}(", service_source)
        self.assertIn('"freeze",', installer_source)
        self.assertIn('"--all",', installer_source)
        self.assertIn('"resolvedRequirements": resolved_requirements', installer_source)

    def test_runner_declares_pool_dependency_and_native_plugin_loading(self) -> None:
        pyproject = tomllib.loads(
            (RUNNER_PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(pyproject["project"]["version"], "0.3.3")
        self.assertIn(
            "automas-maafw-runtime-pool>=0.1.4",
            pyproject["project"]["dependencies"],
        )
        self.assertIn(
            "automas-maafw-agent-env>=0.1.2",
            pyproject["project"]["dependencies"],
        )
        self.assertIn(
            "automas-maafw-interface>=0.2.0",
            pyproject["project"]["dependencies"],
        )
        agent_env_pyproject = tomllib.loads(
            (AGENT_ENV_PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(agent_env_pyproject["project"]["version"], "0.1.2")

        environment_source = (RUNNER_SOURCE / "environment.py").read_text(
            encoding="utf-8"
        )
        runner_source = (RUNNER_SOURCE / "runner.py").read_text(encoding="utf-8")
        run_plan_source = (RUNNER_SOURCE / "run_plan.py").read_text(
            encoding="utf-8"
        )
        models_source = (RUNNER_SOURCE / "models.py").read_text(encoding="utf-8")
        agent_planner_source = (
            AGENT_ENV_SOURCE / "automas_maafw_agent_env" / "planner.py"
        ).read_text(encoding="utf-8")
        for source in (
            environment_source,
            runner_source,
            run_plan_source,
            models_source,
            agent_planner_source,
        ):
            ast.parse(source)

        self.assertIn("runtime_pool: MaaFWRuntimePool | None", environment_source)
        self.assertIn("runtime_id: str | None = None", environment_source)
        self.assertIn("maafw_requirement: str | None = None", environment_source)
        self.assertIn("runtime_pool_root: Path | None = None", environment_source)
        self.assertIn("lease_id: str | None = None", environment_source)
        self.assertIn("pool.acquire_lease(", environment_source)
        self.assertIn("def release_environment(", (
            RUNNER_SOURCE / "service.py"
        ).read_text(encoding="utf-8"))
        self.assertIn(".auto_mas_maafw_project.json", environment_source)
        self.assertIn("nativePluginPaths", models_source)
        self.assertIn("_build_native_plugin_paths", run_plan_source)
        self.assertIn("Tasker.load_plugin(path_info.resolved)", runner_source)
        self.assertIn("Path(sys.prefix)", runner_source)
        self.assertIn("SHARED_RUNTIME_KIND", runner_source)
        self.assertIn("json-with-comments", environment_source)
        self.assertIn("child_args=child_args", agent_planner_source)
        self.assertIn("_is_safe_existing_python_entry", agent_planner_source)


if __name__ == "__main__":
    unittest.main()
