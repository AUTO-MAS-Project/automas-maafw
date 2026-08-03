from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_POOL_SOURCE = ROOT / "packages" / "automas_maafw_runtime_pool" / "src"

if str(RUNTIME_POOL_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_POOL_SOURCE))

from automas_maafw_runtime_pool import (  # noqa: E402
    MaaFWRuntimePoolService,
    prune_uv_cache,
)
from automas_maafw_runtime_pool import cache as runtime_cache  # noqa: E402
from automas_maafw_runtime_pool import pool as runtime_pool  # noqa: E402


class MaaFWRuntimeCachePruneContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.pool_root = Path(self.temporary_directory.name) / "pool"
        self.cache_path = self.pool_root / "cache" / "uv"
        self.cache_path.mkdir(parents=True)
        self.cached_file = self.cache_path / "archive.whl"
        self.cached_file.write_bytes(b"cached-wheel")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_dry_run_audits_without_invoking_uv(self) -> None:
        with (
            mock.patch.object(
                runtime_cache,
                "_find_uv_executable",
                return_value="C:/portable/uv.exe",
            ),
            mock.patch.object(
                runtime_cache,
                "_uv_version",
                return_value="0.11.26",
            ),
            mock.patch.object(runtime_cache.subprocess, "run") as run_mock,
        ):
            result = prune_uv_cache(self.pool_root, dry_run=True)

        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["attempted"])
        self.assertFalse(result["previewExact"])
        self.assertEqual(result["before"]["fileCount"], 1)
        self.assertEqual(result["before"]["sizeBytes"], len(b"cached-wheel"))
        self.assertTrue(self.cached_file.is_file())
        self.assertEqual(
            result["command"][:3],
            ["C:/portable/uv.exe", "cache", "prune"],
        )
        run_mock.assert_not_called()

    def test_real_prune_delegates_to_uv_and_reports_before_after(self) -> None:
        def emulate_uv(command: list[str], **kwargs: Any) -> SimpleNamespace:
            self.assertEqual(command[:3], ["C:/portable/uv.exe", "cache", "prune"])
            self.assertEqual(kwargs["cwd"], self.pool_root.resolve())
            self.assertEqual(kwargs["env"]["UV_CACHE_DIR"], str(self.cache_path))
            self.cached_file.unlink()
            return SimpleNamespace(
                returncode=0,
                stdout="Removed 1 file",
                stderr="",
            )

        with (
            mock.patch.object(
                runtime_cache,
                "_find_uv_executable",
                return_value="C:/portable/uv.exe",
            ),
            mock.patch.object(
                runtime_cache,
                "_uv_version",
                return_value="0.11.26",
            ),
            mock.patch.object(
                runtime_cache.subprocess,
                "run",
                side_effect=emulate_uv,
            ) as run_mock,
        ):
            result = prune_uv_cache(self.pool_root, dry_run=False)

        self.assertEqual(result["status"], "pruned")
        self.assertTrue(result["attempted"])
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["removedFiles"], 1)
        self.assertEqual(result["removedBytes"], len(b"cached-wheel"))
        self.assertEqual(result["after"]["fileCount"], 0)
        self.assertEqual(run_mock.call_count, 1)

    def test_missing_uv_returns_explicit_unavailable_status(self) -> None:
        with (
            mock.patch.object(
                runtime_cache,
                "_find_uv_executable",
                return_value=None,
            ),
            mock.patch.object(runtime_cache.subprocess, "run") as run_mock,
        ):
            result = prune_uv_cache(self.pool_root, dry_run=False)

        self.assertEqual(result["status"], "unavailable")
        self.assertIn("not found", result["error"])
        self.assertFalse(result["attempted"])
        self.assertTrue(self.cached_file.is_file())
        run_mock.assert_not_called()

    def test_nonzero_uv_exit_is_auditable_and_does_not_delete_directly(self) -> None:
        with (
            mock.patch.object(
                runtime_cache,
                "_find_uv_executable",
                return_value="C:/portable/uv.exe",
            ),
            mock.patch.object(
                runtime_cache,
                "_uv_version",
                return_value="0.11.26",
            ),
            mock.patch.object(
                runtime_cache.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=2,
                    stdout="",
                    stderr="cache is busy",
                ),
            ),
        ):
            result = prune_uv_cache(self.pool_root, dry_run=False)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["exitCode"], 2)
        self.assertIn("cache is busy", result["error"])
        self.assertTrue(self.cached_file.is_file())


class MaaFWRuntimeCacheGcIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.pool_root = Path(self.temporary_directory.name) / "pool"
        self.prune_calls: list[dict[str, Any]] = []
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
            cache_pruner=self._record_prune,
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
        del requirements
        scripts_dir = environment_path / ("Scripts" if os.name == "nt" else "bin")
        scripts_dir.mkdir(parents=True)
        python_name = "python.exe" if os.name == "nt" else "python"
        python_executable = scripts_dir / python_name
        python_executable.write_text("fake", encoding="utf-8")
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
        return {"pythonExecutable": str(python_executable)}

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

    def _record_prune(
        self,
        pool_root: Path,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        runtime_count = len(list((pool_root / "runtimes").iterdir()))
        call = {"dryRun": dry_run, "runtimeCount": runtime_count}
        self.prune_calls.append(call)
        return {
            "kind": "uv",
            "scope": "pool",
            "dryRun": dry_run,
            "attempted": not dry_run,
            "status": "preview" if dry_run else "pruned",
        }

    def test_gc_previews_cache_and_prunes_only_after_runtime_collection(self) -> None:
        runtime = self.service.ensure_runtime("maafw==4.3.0")
        self.service.touch(runtime["runtimeId"], at="2000-01-01T00:00:00Z")

        preview = self.service.collect_garbage(
            dry_run=True,
            grace_seconds=0,
            keep_latest=0,
            now="2030-01-01T00:00:00Z",
        )
        self.assertTrue(Path(runtime["path"]).is_dir())
        self.assertEqual(preview["cachePrune"]["status"], "preview")
        self.assertEqual(self.prune_calls[-1], {"dryRun": True, "runtimeCount": 1})

        collected = self.service.collect_garbage(
            dry_run=False,
            grace_seconds=0,
            keep_latest=0,
            now="2030-01-01T00:00:00Z",
        )
        self.assertFalse(Path(runtime["path"]).exists())
        self.assertEqual(collected["cachePrune"]["status"], "pruned")
        self.assertEqual(
            self.prune_calls[-1],
            {"dryRun": False, "runtimeCount": 0},
        )


if __name__ == "__main__":
    unittest.main()
