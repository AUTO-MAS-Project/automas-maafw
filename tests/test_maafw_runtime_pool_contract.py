from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from types import SimpleNamespace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_POOL_PACKAGE = ROOT / "packages" / "automas_maafw_runtime_pool"
RUNTIME_POOL_SOURCE = RUNTIME_POOL_PACKAGE / "src"
RUNNER_PACKAGE = ROOT / "packages" / "automas_maafw_runner"
RUNNER_PACKAGE_SOURCE = RUNNER_PACKAGE / "src"
RUNNER_SOURCE = RUNNER_PACKAGE / "src" / "automas_maafw_runner"
AGENT_ENV_PACKAGE = ROOT / "packages" / "automas_maafw_agent_env"
AGENT_ENV_SOURCE = AGENT_ENV_PACKAGE / "src"
INTERFACE_SOURCE = ROOT / "packages" / "automas_maafw_interface" / "src"

for source_path in (
    RUNTIME_POOL_SOURCE,
    AGENT_ENV_SOURCE,
    INTERFACE_SOURCE,
    RUNNER_PACKAGE_SOURCE,
):
    if str(source_path) not in sys.path:
        sys.path.insert(0, str(source_path))

from automas_maafw_runtime_pool import (  # noqa: E402
    MaaFWRuntimeIdentityError,
    MaaFWRuntimePoolError,
    MaaFWRuntimePoolService,
    POOL_MARKER_NAME,
    POOL_SCHEMA_VERSION,
    build_runtime_id,
    build_runtime_identity,
)
from automas_maafw_runtime_pool import installer as runtime_installer  # noqa: E402
from automas_maafw_runtime_pool import pool as runtime_pool  # noqa: E402
from automas_maafw_agent_env.planner import (  # noqa: E402
    MaaFWAgentEnvError,
    build_maafw_agent_command_plans,
)
from automas_maafw_agent_env.env import (  # noqa: E402
    prepare_agent_envs,
    write_agent_compat_shims,
)
from automas_maafw_agent_env.models import MaaFWAgentCommandPlan  # noqa: E402
from automas_maafw_runner.service import MaaFWRunnerService  # noqa: E402


class MaaFWRuntimePoolContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.pool_root = Path(self.temporary_directory.name) / "pool"
        self.install_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.fake_python_identities: dict[str, dict[str, str]] = {}
        self.real_python_probe = runtime_pool.probe_python_identity
        self.python_probe_patch = mock.patch.object(
            runtime_pool,
            "probe_python_identity",
            side_effect=self._probe_fake_python,
        )
        self.python_probe_patch.start()
        self.service = MaaFWRuntimePoolService(
            self.pool_root,
            installer=self._fake_installer,
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
        self.install_calls.append((tuple(requirements), identity))
        scripts_dir = environment_path / ("Scripts" if os.name == "nt" else "bin")
        scripts_dir.mkdir(parents=True, exist_ok=False)
        python_name = "python.exe" if os.name == "nt" else "python"
        python_executable = scripts_dir / python_name
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

    def test_uv_discovery_honors_host_configured_executable(self) -> None:
        configured_uv = self.pool_root.parent / "host-tools" / "uv.exe"
        configured_uv.parent.mkdir(parents=True)
        configured_uv.write_bytes(b"test uv")

        with mock.patch.dict(
            os.environ,
            {"AUTO_MAS_UV_EXE": str(configured_uv)},
        ):
            resolved = runtime_installer._find_uv_executable(sys.executable)

        self.assertEqual(resolved, str(configured_uv.resolve()))

    def test_runtime_install_honors_host_configured_index(self) -> None:
        index_url = "https://mirrors.aliyun.com/pypi/simple/"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "AUTO_MAS_UV_INDEX_URL": index_url,
                    "UV_INDEX_URL": "",
                    "UV_DEFAULT_INDEX": "",
                },
            ),
            mock.patch.object(runtime_installer, "_run") as run,
        ):
            runtime_installer._install_requirements_with_uv(
                "C:/tools/uv.exe",
                self.pool_root / "environment" / "Scripts" / "python.exe",
                ["maafw==5.12.2"],
                cache_dir=self.pool_root / "cache" / "uv",
                link_mode="hardlink",
                cwd=self.pool_root,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--index-url") + 1], index_url)

    def test_runtime_install_preserves_direct_uv_index_configuration(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "AUTO_MAS_UV_INDEX_URL": "https://host.invalid/simple/",
                    "UV_INDEX_URL": "https://user.invalid/simple/",
                },
            ),
            mock.patch.object(runtime_installer, "_run") as run,
        ):
            runtime_installer._install_requirements_with_uv(
                "C:/tools/uv.exe",
                self.pool_root / "environment" / "Scripts" / "python.exe",
                ["maafw==5.12.2"],
                cache_dir=self.pool_root / "cache" / "uv",
                link_mode="hardlink",
                cwd=self.pool_root,
            )

        self.assertNotIn("--index-url", run.call_args.args[0])

    def test_root_marker_identity_is_stable_and_json_friendly(self) -> None:
        first = self.service.storage_info()
        marker = json.loads(
            (self.pool_root / POOL_MARKER_NAME).read_text(encoding="utf-8")
        )

        self.assertEqual(marker["schemaVersion"], POOL_SCHEMA_VERSION)
        self.assertEqual(marker["kind"], "auto-mas-maafw-runtime-pool")
        self.assertEqual(first["poolId"], marker["poolId"])
        self.assertEqual(first["root"], str(self.pool_root.resolve()))
        self.assertFalse(first["isDefault"])
        self.assertEqual(first["rootIdentity"], self.service.rootIdentity)

        reopened = MaaFWRuntimePoolService(
            self.pool_root,
            installer=self._fake_installer,
        )
        self.assertEqual(reopened.storage_info()["poolId"], first["poolId"])

    def test_configured_root_rejects_unknown_non_empty_directory(self) -> None:
        unknown = Path(self.temporary_directory.name) / "unknown-pool"
        unknown.mkdir()
        sentinel = unknown / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(
            MaaFWRuntimePoolError,
            "non-empty directory without a valid runtime-pool marker",
        ):
            MaaFWRuntimePoolService(unknown, installer=self._fake_installer)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertFalse((unknown / POOL_MARKER_NAME).exists())

    def test_legacy_marker_is_upgraded_in_place(self) -> None:
        legacy = Path(self.temporary_directory.name) / "legacy-pool"
        legacy.mkdir()
        marker_path = legacy / POOL_MARKER_NAME
        marker_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "kind": "auto-mas-maafw-runtime-pool",
                }
            ),
            encoding="utf-8",
        )

        migrated = MaaFWRuntimePoolService(legacy, installer=self._fake_installer)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))

        self.assertEqual(marker["schemaVersion"], POOL_SCHEMA_VERSION)
        self.assertEqual(marker["poolId"], migrated.storage_info()["poolId"])

    def test_legacy_default_layout_is_adopted_without_moving_content(self) -> None:
        working_directory = Path(self.temporary_directory.name) / "legacy-host"
        legacy_root = working_directory / "config" / "maafw_runtime_pool"
        (legacy_root / "runtimes").mkdir(parents=True)
        (legacy_root / ".staging").mkdir()
        (legacy_root / "cache").mkdir()
        sentinel = legacy_root / "cache" / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")

        with mock.patch(
            "automas_maafw_runtime_pool.service.Path.cwd",
            return_value=working_directory,
        ), mock.patch(
            "automas_maafw_runtime_pool.pool.Path.cwd",
            return_value=working_directory,
        ):
            adopted = MaaFWRuntimePoolService(installer=self._fake_installer)

        self.assertTrue((legacy_root / POOL_MARKER_NAME).is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertTrue(adopted.storage_info()["isDefault"])

    def test_invalid_marker_fails_closed(self) -> None:
        invalid = Path(self.temporary_directory.name) / "invalid-pool-marker"
        invalid.mkdir()
        (invalid / POOL_MARKER_NAME).write_text(
            json.dumps(
                {
                    "schemaVersion": POOL_SCHEMA_VERSION,
                    "kind": "not-a-runtime-pool",
                    "poolId": "00000000-0000-0000-0000-000000000000",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MaaFWRuntimePoolError, "marker kind is invalid"):
            MaaFWRuntimePoolService(invalid, installer=self._fake_installer)

    def test_plugin_uses_root_from_context_config(self) -> None:
        from automas_maafw_runtime_pool.plugin import Plugin

        configured = Path(self.temporary_directory.name) / "configured-pool"
        context = type("Context", (), {"config": {"Root": str(configured)}})()

        plugin = Plugin(context)

        self.assertEqual(plugin.service.pool.root, configured.resolve())

    def test_configured_root_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(MaaFWRuntimePoolError, "absolute path"):
            MaaFWRuntimePoolService("relative-pool", installer=self._fake_installer)

    def test_runtime_dtos_include_pool_identity_and_inventory_reports_corruption(
        self,
    ) -> None:
        runtime = self.service.ensure_runtime({"requirements": ["maafw==4.3.0"]})
        self.assertEqual(runtime["poolId"], self.service.storage_info()["poolId"])
        corrupt = self.pool_root / "runtimes" / "maafw-runtime-corrupt"
        corrupt.mkdir()
        (corrupt / "manifest.json").write_text("{}", encoding="utf-8")

        snapshot = self.service.inventory()

        self.assertFalse(snapshot["complete"])
        self.assertEqual(len(snapshot["items"]), 1)
        self.assertEqual(snapshot["items"][0]["poolId"], runtime["poolId"])
        self.assertEqual(snapshot["errors"][0]["runtimeId"], corrupt.name)
        self.assertEqual(
            snapshot["rootIdentity"]["poolId"],
            runtime["poolId"],
        )
        preview = self.service.collect_garbage(
            dry_run=True,
            grace_seconds=0,
            keep_latest=0,
            now="2030-01-01T00:00:00Z",
        )
        self.assertFalse(preview["complete"])
        self.assertTrue(preview["inventoryErrors"])
        self.assertEqual(preview["deleted"], [])
        self.assertEqual(
            preview["cachePrune"]["status"],
            "skipped-incomplete-inventory",
        )
        with self.assertRaisesRegex(
            MaaFWRuntimePoolError,
            "inventory is incomplete",
        ):
            self.service.collect_garbage(
                dry_run=False,
                grace_seconds=0,
                keep_latest=0,
                now="2030-01-01T00:00:00Z",
            )
        self.assertTrue(Path(runtime["path"]).is_dir())

    def test_corrupt_selector_manifest_is_stale_and_quarantined_on_ensure(
        self,
    ) -> None:
        runtime = self.service.ensure_runtime("maafw==4.3.0")
        runtime_id = runtime["runtimeId"]
        runtime_dir = Path(runtime["path"])
        sentinel = runtime_dir / "keep-during-recovery.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        manifest_path = runtime_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["pinned"] = True
        manifest["references"] = ["project:protected"]
        manifest["leases"] = {
            "runner-1": {
                "owner": "runner",
                "acquiredAt": "2029-01-01T00:00:00Z",
                "expiresAt": None,
            }
        }
        manifest["selectorRequirements"] = ["maafw==9.9.9"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.assertIsNone(self.service.resolve_runtime({"runtimeId": runtime_id}))

        restored = self.service.ensure_runtime(
            {
                "runtimeId": runtime_id,
                "requirements": runtime["selectorRequirements"],
            }
        )

        self.assertEqual(restored["runtimeId"], runtime_id)
        self.assertEqual(len(self.install_calls), 2)
        quarantine = list(
            (self.pool_root / ".staging").glob(f"{runtime_id}-quarantine-*")
        )
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(
            (quarantine[0] / "keep-during-recovery.txt").read_text(
                encoding="utf-8"
            ),
            "preserve",
        )
        self.assertEqual(
            json.loads((Path(restored["path"]) / "manifest.json").read_text())[
                "pinned"
            ],
            True,
        )
        restored_manifest = json.loads(
            (Path(restored["path"]) / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(restored_manifest["references"], ["project:protected"])
        self.assertEqual(restored_manifest["leases"]["runner-1"]["owner"], "runner")

    def test_missing_python_is_stale_and_quarantined_on_ensure(self) -> None:
        runtime = self.service.ensure_runtime("maafw==4.3.0")
        runtime_id = runtime["runtimeId"]
        runtime_dir = Path(runtime["path"])
        python_executable = Path(runtime["pythonExecutable"])
        detached_python = Path(self.temporary_directory.name) / "detached-python.exe"
        python_executable.replace(detached_python)

        self.assertIsNone(self.service.resolve_runtime({"runtimeId": runtime_id}))

        restored = self.service.ensure_runtime(
            {
                "runtimeId": runtime_id,
                "requirements": runtime["selectorRequirements"],
            }
        )

        self.assertEqual(restored["runtimeId"], runtime_id)
        self.assertTrue(Path(restored["pythonExecutable"]).is_file())
        quarantine = list(
            (self.pool_root / ".staging").glob(f"{runtime_id}-quarantine-*")
        )
        self.assertEqual(len(quarantine), 1)
        relative_python = python_executable.relative_to(runtime_dir)
        self.assertFalse((quarantine[0] / relative_python).exists())
        self.assertTrue(detached_python.is_file())

    def test_stale_recovery_is_serialized_for_one_runtime_id(self) -> None:
        runtime = self.service.ensure_runtime("maafw==4.3.0")
        runtime_id = runtime["runtimeId"]
        manifest_path = Path(runtime["path"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["selectorRequirements"] = ["maafw==9.9.9"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        request = {
            "runtimeId": runtime_id,
            "requirements": runtime["selectorRequirements"],
        }

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(self.service.ensure_runtime, request)
                for _ in range(4)
            ]
            restored = [future.result(timeout=5) for future in futures]

        self.assertEqual({item["runtimeId"] for item in restored}, {runtime_id})
        self.assertEqual(len(self.install_calls), 2)
        self.assertEqual(
            len(
                list(
                    (self.pool_root / ".staging").glob(
                        f"{runtime_id}-quarantine-*"
                    )
                )
            ),
            1,
        )

    def test_stale_recovery_refuses_malformed_protection_metadata(self) -> None:
        runtime = self.service.ensure_runtime("maafw==4.3.0")
        runtime_id = runtime["runtimeId"]
        runtime_dir = Path(runtime["path"])
        manifest_path = runtime_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["selectorRequirements"] = ["maafw==9.9.9"]
        manifest["leases"] = {"active": "malformed-but-protected"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            MaaFWRuntimePoolError,
            "lease entry is invalid",
        ):
            self.service.ensure_runtime(
                {
                    "runtimeId": runtime_id,
                    "requirements": runtime["selectorRequirements"],
                }
            )

        self.assertTrue(runtime_dir.is_dir())
        self.assertEqual(
            list((self.pool_root / ".staging").glob(f"{runtime_id}-quarantine-*")),
            [],
        )

    def test_stale_resolution_does_not_swallow_root_marker_errors(self) -> None:
        runtime = self.service.ensure_runtime("maafw==4.3.0")
        marker_path = self.pool_root / POOL_MARKER_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["kind"] = "not-a-runtime-pool"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        with self.assertRaisesRegex(MaaFWRuntimePoolError, "marker kind is invalid"):
            self.service.resolve_runtime({"runtimeId": runtime["runtimeId"]})

    def test_gc_refuses_runtime_manifest_with_malformed_lease(self) -> None:
        runtime = self.service.ensure_runtime({"requirements": ["maafw==4.3.0"]})
        manifest_path = Path(runtime["path"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["leases"] = {
            "possibly-active": {
                "owner": "runner",
                "expiresAt": "",
            }
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        preview = self.service.collect_garbage(
            dry_run=True,
            grace_seconds=0,
            keep_latest=0,
            now="2030-01-01T00:00:00Z",
        )
        self.assertFalse(preview["complete"])
        self.assertTrue(preview["inventoryErrors"])
        self.assertEqual(preview["deleted"], [])
        with self.assertRaisesRegex(
            MaaFWRuntimePoolError,
            "inventory is incomplete",
        ):
            self.service.collect_garbage(
                dry_run=False,
                grace_seconds=0,
                keep_latest=0,
                now="2030-01-01T00:00:00Z",
            )
        self.assertTrue(Path(runtime["path"]).is_dir())

    def test_reparse_root_is_rejected_when_supported(self) -> None:
        target = Path(self.temporary_directory.name) / "runtime-reparse-target"
        target.mkdir()
        link = Path(self.temporary_directory.name) / "runtime-reparse-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaisesRegex(MaaFWRuntimePoolError, "reparse points"):
            MaaFWRuntimePoolService(link, installer=self._fake_installer)

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
        self.assertRegex(identity["pythonVersion"], r"^\d+\.\d+\.\d+$")
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

    def test_project_python_health_does_not_require_pip(self) -> None:
        project_path = Path(self.temporary_directory.name) / "project-python-agent"
        project_path.mkdir()
        plan = MaaFWAgentCommandPlan(
            childExec="python/python.exe",
            executable=sys.executable,
            executableExists=True,
            runtimeKind="project_python",
            command=[sys.executable, "-u", "agent/main.py", "<socket_id>"],
            childArgs=["-u", "agent/main.py"],
            cwd=str(project_path),
        )
        logs: list[str] = []
        progress_events: list[dict[str, object]] = []
        with (
            mock.patch(
                "automas_maafw_agent_env.env._check_project_python_health",
                return_value=True,
            ) as health_check,
            mock.patch(
                "automas_maafw_agent_env.env._check_pip_health",
                side_effect=AssertionError("project Python must not probe pip"),
            ) as pip_check,
        ):
            result = prepare_agent_envs(
                project_path,
                [plan],
                send_log=logs.append,
                progress=progress_events.append,
            )

        health_check.assert_called_once()
        pip_check.assert_not_called()
        self.assertEqual(result.preparedVenvs, [])
        self.assertTrue(any("project_python" in line for line in logs))
        self.assertEqual(progress_events[-1]["status"], "ready")
        self.assertEqual(progress_events[-1]["percent"], 100.0)
        self.assertEqual(progress_events[-1]["completed"], 1)
        self.assertTrue(
            all(
                {"stage", "status", "message", "percent", "completed", "total"}
                <= event.keys()
                for event in progress_events
            )
        )

    def test_shared_runtime_agent_requires_existing_python(self) -> None:
        project_path = Path(self.temporary_directory.name) / "missing-shared-runtime"
        project_path.mkdir()
        missing_python = (
            Path(self.temporary_directory.name)
            / "missing-runtime"
            / "Scripts"
            / "python.exe"
        )
        plan = MaaFWAgentCommandPlan(
            childExec="python/python.exe",
            executable=str(missing_python),
            executableExists=False,
            runtimeKind="shared_runtime",
            command=[str(missing_python), "agent.py"],
            childArgs=["agent.py"],
            cwd=str(project_path),
        )

        with self.assertRaisesRegex(
            MaaFWAgentEnvError,
            "共享 MaaFW runtime Python 不存在",
        ):
            prepare_agent_envs(project_path, [plan])

    def test_agent_compat_shim_write_is_atomic_and_idempotent(self) -> None:
        runtime_path = Path(self.temporary_directory.name) / "shim-runtime"
        shim_dir = runtime_path / ".auto_mas_shims"
        shim_dir.mkdir(parents=True)
        shim_path = shim_dir / "sitecustomize.py"
        shim_path.write_text("old shim\n", encoding="utf-8")

        with (
            mock.patch.object(
                Path,
                "replace",
                side_effect=OSError("replace failed"),
            ),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            write_agent_compat_shims(runtime_path)

        self.assertEqual(shim_path.read_text(encoding="utf-8"), "old shim\n")
        self.assertEqual(list(shim_dir.glob("sitecustomize.py.tmp-*")), [])

        returned_shim_dir = write_agent_compat_shims(runtime_path)
        content = shim_path.read_text(encoding="utf-8")
        self.assertEqual(returned_shim_dir, shim_dir)
        self.assertIn("_patch_legacy_maafw_resource", content)

        with mock.patch.object(
            Path,
            "write_text",
            side_effect=AssertionError("identical shim must not be rewritten"),
        ):
            self.assertEqual(write_agent_compat_shims(runtime_path), shim_dir)
        self.assertEqual(shim_path.read_text(encoding="utf-8"), content)

    def test_isolated_agent_venv_preparation_is_serialized_by_path(self) -> None:
        project_path = Path(self.temporary_directory.name) / "isolated-agent-project"
        project_path.mkdir()
        venv_path = Path(self.temporary_directory.name) / "maafw_agent_venvs" / "shared"
        plan = MaaFWAgentCommandPlan(
            childExec="python/python.exe",
            executable=str(venv_path / "Scripts" / "python.exe"),
            executableExists=False,
            runtimeKind="isolated_venv",
            isolatedVenvPath=str(venv_path),
            command=[str(venv_path / "Scripts" / "python.exe"), "agent.py"],
            childArgs=["agent.py"],
            cwd=str(project_path),
        )
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0

        def fake_prepare(*_args: Any, **_kwargs: Any) -> Path:
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.05)
            with state_lock:
                active -= 1
            return venv_path

        with (
            mock.patch(
                "automas_maafw_agent_env.env._prepare_isolated_venv_env",
                side_effect=fake_prepare,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [
                executor.submit(prepare_agent_envs, project_path, [plan])
                for _ in range(2)
            ]
            for future in futures:
                future.result(timeout=5)

        self.assertEqual(maximum_active, 1)

    def test_project_preflight_and_first_run_share_runtime_identity(self) -> None:
        project_path = Path(self.temporary_directory.name) / "prewarm-project"
        project_path.mkdir()
        (project_path / "requirements.txt").write_text(
            "maafw==4.3.0\nrequests==2.34.2\n",
            encoding="utf-8",
        )
        (project_path / "interface.json").write_text(
            '{"interface_version":2,"name":"demo","version":"1.0.0"}',
            encoding="utf-8",
        )
        runner = MaaFWRunnerService()
        first_progress: list[dict[str, Any]] = []

        first = runner.prepare_project_environment(
            project_path,
            None,
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
            runtime_requirement="maafw==4.3.0",
            progress=first_progress.append,
        )
        runtime_id = first["runtime"]["runtimeId"]
        self.assertEqual(len(self.install_calls), 1)
        self.assertEqual(self.service.pool.get(runtime_id)["activeLeaseIds"], [])
        first_stages = [event["stage"] for event in first_progress]
        self.assertTrue(
            {
                "resolving",
                "runtime_check",
                "creating_runtime",
                "installing_runtime",
                "runtime_ready",
                "preparing_agents",
                "completed",
            }.issubset(first_stages)
        )
        self.assertEqual(first_progress[-1]["status"], "ready")
        self.assertEqual(first_progress[-1]["percent"], 100.0)
        self.assertTrue(
            all(
                {"stage", "status", "message"} <= event.keys()
                for event in first_progress
            )
        )

        # Project metadata/version alone is not part of the canonical runtime
        # identity and must not rebuild an unchanged dependency environment.
        (project_path / "interface.json").write_text(
            '{"interface_version":2,"name":"demo","version":"1.0.0"}',
            encoding="utf-8",
        )
        second_progress: list[dict[str, Any]] = []
        second = runner.prepare_project_environment(
            project_path,
            None,
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
            runtime_requirement="maafw==4.3.0",
            progress=second_progress.append,
        )
        self.assertEqual(second["runtime"]["runtimeId"], runtime_id)
        self.assertEqual(len(self.install_calls), 1)
        second_stages = [event["stage"] for event in second_progress]
        self.assertNotIn("creating_runtime", second_stages)
        self.assertNotIn("installing_runtime", second_stages)
        self.assertTrue(
            any(
                event["stage"] == "runtime_ready"
                and event["status"] == "reused"
                for event in second_progress
            )
        )

        execution = runner.prepare_environment(
            project_path,
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
            runtime_requirement="maafw==4.3.0",
        )
        try:
            self.assertEqual(execution.runtime_id, runtime_id)
            self.assertEqual(len(self.install_calls), 1)
            self.assertTrue(execution.lease_id)
        finally:
            runner.release_environment(execution, runtime_pool=self.service.pool)

    def test_managed_explicit_selector_ignores_checkout_route_and_requirements(
        self,
    ) -> None:
        project_path = Path(self.temporary_directory.name) / "managed-selector"
        project_path.mkdir()
        (project_path / ".auto_mas_maafw_project.json").write_text(
            "{malformed checkout sidecar",
            encoding="utf-8",
        )
        (project_path / "requirements.txt").write_text(
            "maafw==99.0.0\ncheckout-only==1.0\n",
            encoding="utf-8",
        )
        selector = [
            "maafw==4.3.0",
            "json-with-comments",
            "requests==2.34.2",
        ]
        runtime = self.service.ensure_runtime({"requirements": selector})
        self.assertEqual(runtime["runtimeId"], build_runtime_id(selector))
        runner = MaaFWRunnerService()

        environment = runner.prepare_environment(
            project_path,
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
            runtime_requirement=runtime["maafwRequirement"],
            runtime_requirements=runtime["selectorRequirements"],
            runtime_id=runtime["runtimeId"],
            runtime_pool_id=runtime["poolId"],
        )
        try:
            self.assertEqual(environment.runtime_id, runtime["runtimeId"])
            self.assertEqual(
                environment.python_executable,
                Path(runtime["pythonExecutable"]),
            )
            self.assertEqual(environment.runtime_pool_id, runtime["poolId"])
            self.assertEqual(len(self.install_calls), 1)
        finally:
            runner.release_environment(
                environment,
                runtime_pool=self.service.pool,
            )

        with self.assertRaisesRegex(RuntimeError, "Pool 身份不匹配"):
            runner.prepare_environment(
                project_path,
                runtime_pool=self.service.pool,
                runtime_requirements=runtime["selectorRequirements"],
                runtime_id=runtime["runtimeId"],
                runtime_pool_id="00000000-0000-0000-0000-000000000000",
            )

    def test_managed_preflight_routes_python_agent_to_exact_shared_runtime(
        self,
    ) -> None:
        project_path = Path(self.temporary_directory.name) / "managed-agent"
        project_path.mkdir()
        (project_path / "agent.py").write_text("pass\n", encoding="utf-8")
        selector = ["maafw==4.3.0", "json-with-comments"]
        runtime = self.service.ensure_runtime({"requirements": selector})
        runner = MaaFWRunnerService()

        result = runner.prepare_project_environment(
            project_path,
            {
                "agent": {
                    "child_exec": "python",
                    "child_args": ["agent.py"],
                }
            },
            runtime_pool=self.service.pool,
            runtime_installer=self._fake_installer,
            runtime_requirement=runtime["maafwRequirement"],
            runtime_requirements=runtime["selectorRequirements"],
            runtime_id=runtime["runtimeId"],
            runtime_pool_id=runtime["poolId"],
            agent_env_root=Path(self.temporary_directory.name) / "agent-venvs",
            install_agent_dependencies=False,
            managed_shared_agent_dependencies_complete=True,
            managed_python_agent_indexes=[0],
        )

        self.assertEqual(result["runtime"]["runtimeId"], runtime["runtimeId"])
        self.assertEqual(result["runtime"]["poolId"], runtime["poolId"])
        self.assertEqual(len(self.install_calls), 1)
        plans = result["agents"]["plans"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["runtimeKind"], "shared_runtime")
        self.assertEqual(plans[0]["executable"], runtime["pythonExecutable"])
        self.assertEqual(result["agents"]["preparedVenvs"], [])
        self.assertEqual(result["agents"]["skipped"], [])
        self.assertTrue(
            any(
                "共享 MaaFW runtime Python 已就绪" in message
                for message in result["agents"]["messages"]
            )
        )

    def test_managed_cp313_binding_is_not_recomputed_from_host_python(self) -> None:
        project_path = Path(self.temporary_directory.name) / "managed-cp313"
        project_path.mkdir()
        selector = ["maafw==5.12.2", "json-with-comments"]
        host_identity = build_runtime_identity(selector)
        cp313 = {
            "implementation": "cpython",
            "cacheTag": "cpython-313",
            "soabi": "cp313-win_amd64",
            "version": "3.13.14",
            "shortVersion": "3.13",
            "platform": host_identity["platform"],
            "architecture": host_identity["architecture"],
        }
        with (
            mock.patch.object(
                self.service.pool,
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
            runtime = self.service.ensure_runtime(
                {
                    "requirements": selector,
                    "python": {
                        "implementation": "cpython",
                        "constraint": "==3.13.*",
                    },
                }
            )

        runner = MaaFWRunnerService()
        with mock.patch.object(
            runtime_pool,
            "probe_python_identity",
            return_value=cp313,
        ):
            environment = runner.prepare_environment(
                project_path,
                runtime_pool=self.service.pool,
                runtime_installer=self._fake_installer,
                runtime_requirement=runtime["maafwRequirement"],
                runtime_requirements=runtime["selectorRequirements"],
                runtime_id=runtime["runtimeId"],
                runtime_pool_id=runtime["poolId"],
                runtime_python_constraint="==3.13.*",
            )
        try:
            self.assertEqual(environment.runtime_id, runtime["runtimeId"])
            self.assertEqual(environment.python_constraint, "==3.13.*")
            self.assertEqual(
                runtime["identity"]["pythonVersion"],
                "3.13.14",
            )
            if sys.version_info[:2] != (3, 13):
                self.assertNotEqual(runtime["runtimeId"], build_runtime_id(selector))
            self.assertEqual(len(self.install_calls), 1)
        finally:
            runner.release_environment(
                environment,
                runtime_pool=self.service.pool,
            )

    def test_project_preflight_failure_reports_and_releases_lease(self) -> None:
        project_path = Path(self.temporary_directory.name) / "failed-prewarm-project"
        project_path.mkdir()
        (project_path / "requirements.txt").write_text(
            "maafw==4.3.0\n",
            encoding="utf-8",
        )
        progress_events: list[dict[str, Any]] = []
        runner = MaaFWRunnerService()

        with (
            mock.patch(
                "automas_maafw_runner.service.prepare_agent_envs",
                side_effect=RuntimeError("agent preparation failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "agent preparation failed"),
        ):
            runner.prepare_project_environment(
                project_path,
                None,
                runtime_pool=self.service.pool,
                runtime_installer=self._fake_installer,
                runtime_requirement="maafw==4.3.0",
                progress=progress_events.append,
            )

        runtimes = self.service.pool.list()
        self.assertEqual(len(runtimes), 1)
        self.assertEqual(runtimes[0]["activeLeaseIds"], [])
        self.assertEqual(progress_events[-1]["stage"], "failed")
        self.assertEqual(progress_events[-1]["status"], "failed")
        self.assertIn("agent preparation failed", progress_events[-1]["message"])
        self.assertNotIn("percent", progress_events[-1])

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

    def test_authoritative_shared_agent_flag_overrides_checkout_manifest(
        self,
    ) -> None:
        from automas_maafw_runner.shared_agent import (
            route_managed_python_agents_to_shared_runtime,
        )

        project = Path(self.temporary_directory.name) / "managed-authoritative-flag"
        project.mkdir()
        manifest_path = project / ".auto_mas_maafw_project.json"
        shared_python = Path(self.temporary_directory.name) / "shared" / "python.exe"
        shared_python.parent.mkdir()
        shared_python.write_text("fake", encoding="utf-8")

        def agent() -> SimpleNamespace:
            return SimpleNamespace(
                embedded=False,
                runtimeKind="isolated_venv",
                isolatedVenvPath="C:/managed/venv",
                executable="C:/managed/venv/Scripts/python.exe",
                executableExists=False,
                command=["C:/managed/venv/Scripts/python.exe", "agent.py"],
                childArgs=["agent.py"],
                fallbackReason=None,
            )

        manifest_path.write_text(
            json.dumps(
                {"runtime": {"sharedAgentDependenciesComplete": False}}
            ),
            encoding="utf-8",
        )
        trusted_true = agent()
        self.assertEqual(
            route_managed_python_agents_to_shared_runtime(
                project,
                [trusted_true],
                python_executable=shared_python,
                dependencies_complete=True,
            ),
            [trusted_true],
        )
        self.assertEqual(trusted_true.runtimeKind, "shared_runtime")

        manifest_path.write_text(
            json.dumps(
                {"runtime": {"sharedAgentDependenciesComplete": True}}
            ),
            encoding="utf-8",
        )
        trusted_false = agent()
        self.assertEqual(
            route_managed_python_agents_to_shared_runtime(
                project,
                [trusted_false],
                python_executable=shared_python,
                dependencies_complete=False,
            ),
            [],
        )
        self.assertEqual(trusted_false.runtimeKind, "isolated_venv")

        manifest_path.write_text("{malformed checkout sidecar", encoding="utf-8")
        trusted_over_malformed_checkout = agent()
        self.assertEqual(
            route_managed_python_agents_to_shared_runtime(
                project,
                [trusted_over_malformed_checkout],
                python_executable=shared_python,
                dependencies_complete=True,
            ),
            [trusted_over_malformed_checkout],
        )

    def test_stripped_bare_python_route_is_indexed_and_project_local(
        self,
    ) -> None:
        from automas_maafw_runner.shared_agent import (
            route_managed_python_agents_to_shared_runtime,
        )

        project = Path(self.temporary_directory.name) / "stripped-python"
        (project / "agent").mkdir(parents=True)
        (project / "agent" / "bootstrap.pyw").write_text(
            "print('agent')\n",
            encoding="utf-8",
        )
        outside_entry = Path(self.temporary_directory.name) / "outside.py"
        outside_entry.write_text("print('outside')\n", encoding="utf-8")
        shared_python = Path(self.temporary_directory.name) / "shared" / "python.exe"
        shared_python.parent.mkdir()
        shared_python.write_text("fake", encoding="utf-8")

        def external_agent(
            child_exec: str = "python",
            child_args: list[str] | None = None,
            *,
            embedded: bool = False,
        ) -> SimpleNamespace:
            args = child_args or ["-u", "agent/bootstrap.pyw"]
            return SimpleNamespace(
                childExec=child_exec,
                childArgs=args,
                embedded=embedded,
                runtimeKind="external",
                isolatedVenvPath=None,
                executable=child_exec,
                executableExists=None,
                command=[child_exec, *args, "<socket_id>"],
                fallbackReason=None,
            )

        stripped = external_agent()
        explicit_external = external_agent("C:/tools/python.exe")
        no_entrypoint = external_agent(child_args=["-m", "agent.bootstrap"])
        outside = external_agent(child_args=["-u", "../outside.py"])
        embedded = external_agent(embedded=True)
        routed = route_managed_python_agents_to_shared_runtime(
            project,
            [stripped, explicit_external, no_entrypoint, outside, embedded],
            python_executable=shared_python,
            dependencies_complete=True,
            managed_python_agent_indexes=[0],
        )

        self.assertEqual(routed, [stripped])
        self.assertEqual(stripped.runtimeKind, "shared_runtime")
        self.assertEqual(stripped.command[0], str(shared_python.resolve()))
        for untouched in (explicit_external, no_entrypoint, outside, embedded):
            self.assertEqual(untouched.runtimeKind, "external")
            self.assertNotEqual(untouched.command[0], str(shared_python.resolve()))

        unindexed = external_agent()
        self.assertEqual(
            route_managed_python_agents_to_shared_runtime(
                project,
                [unindexed],
                python_executable=shared_python,
                dependencies_complete=True,
                managed_python_agent_indexes=[],
            ),
            [],
        )

        ordinary_project = Path(self.temporary_directory.name) / "ordinary"
        (ordinary_project / "agent").mkdir(parents=True)
        (ordinary_project / "agent" / "bootstrap.pyw").write_text(
            "print('ordinary')\n",
            encoding="utf-8",
        )
        ordinary = external_agent()
        self.assertEqual(
            route_managed_python_agents_to_shared_runtime(
                ordinary_project,
                [ordinary],
                python_executable=shared_python,
            ),
            [],
        )
        self.assertEqual(ordinary.runtimeKind, "external")

        with self.assertRaisesRegex(
            RuntimeError,
            "trusted Store projection",
        ):
            route_managed_python_agents_to_shared_runtime(
                project,
                [explicit_external],
                python_executable=shared_python,
                dependencies_complete=True,
                managed_python_agent_indexes=[0],
            )
        inline_code = external_agent(
            child_args=["-cprint('not a file entrypoint')", "agent/bootstrap.pyw"]
        )
        with self.assertRaisesRegex(RuntimeError, "trusted Store projection"):
            route_managed_python_agents_to_shared_runtime(
                project,
                [inline_code],
                python_executable=shared_python,
                dependencies_complete=True,
                managed_python_agent_indexes=[0],
            )
        with self.assertRaisesRegex(RuntimeError, "outside the current interface"):
            route_managed_python_agents_to_shared_runtime(
                project,
                [stripped],
                python_executable=shared_python,
                dependencies_complete=True,
                managed_python_agent_indexes=[1],
            )

        (project / ".auto_mas_maafw_project.json").write_text(
            json.dumps(
                {
                    "runtime": {
                        "sharedAgentDependenciesComplete": True,
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
            ),
            encoding="utf-8",
        )
        manifest_routed = external_agent()
        self.assertEqual(
            route_managed_python_agents_to_shared_runtime(
                project,
                [manifest_routed],
                python_executable=shared_python,
            ),
            [manifest_routed],
        )

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
    def test_worker_and_preflight_use_the_same_shared_agent_router(self) -> None:
        runner_source = (RUNNER_SOURCE / "runner.py").read_text(encoding="utf-8")
        service_source = (RUNNER_SOURCE / "service.py").read_text(encoding="utf-8")
        for source in (runner_source, service_source):
            tree = ast.parse(source)
            route_calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id
                == "route_managed_python_agents_to_shared_runtime"
            ]
            self.assertEqual(len(route_calls), 1)
            keywords = {keyword.arg for keyword in route_calls[0].keywords}
            self.assertIn("dependencies_complete", keywords)
            self.assertIn("managed_python_agent_indexes", keywords)

    def test_plugin_distribution_and_service_contract(self) -> None:
        pyproject = tomllib.loads(
            (RUNTIME_POOL_PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = pyproject["project"]
        entry_points = project["entry-points"]["auto_mas.plugins"]

        self.assertEqual(project["name"], "automas-maafw-runtime-pool")
        self.assertEqual(project["version"], "0.2.0")
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
        self.assertEqual(pyproject["project"]["version"], "0.4.0")
        self.assertIn(
            "automas-maafw-runtime-pool>=0.2.0",
            pyproject["project"]["dependencies"],
        )
        self.assertIn(
            "automas-maafw-agent-env>=0.1.4",
            pyproject["project"]["dependencies"],
        )
        self.assertIn(
            "automas-maafw-interface>=0.2.0",
            pyproject["project"]["dependencies"],
        )
        agent_env_pyproject = tomllib.loads(
            (AGENT_ENV_PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(agent_env_pyproject["project"]["version"], "0.1.4")

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
        self.assertIn('"ADB 连接方式: "', runner_source)
        self.assertIn('"ADB 连接测速: ', runner_source)
        self.assertIn('"ADB controller 传入候选集合: "', runner_source)
        self.assertIn('"ADB controller 最终连接: "', runner_source)
        self.assertIn(
            '"MaaFW Python binding 未公开测速后实际选中的单项方法，"',
            runner_source,
        )


if __name__ == "__main__":
    unittest.main()
