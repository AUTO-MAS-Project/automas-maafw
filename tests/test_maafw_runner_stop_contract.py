from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT / "packages" / "automas_maafw_agent_env" / "src",
    ROOT / "packages" / "automas_maafw_interface" / "src",
    ROOT / "packages" / "automas_maafw_runtime_pool" / "src",
    ROOT / "packages" / "automas_maafw_runner" / "src",
)
for source_root in reversed(SOURCE_ROOTS):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from automas_maafw_runner.models import (  # noqa: E402
    MaaFWDeviceConfig,
    MaaFWResourceBundlePlan,
    MaaFWRunPlan,
    MaaFWRunnerJobPayload,
)
from automas_maafw_runner.plugin import Plugin as MaaFWRunnerPlugin  # noqa: E402
from automas_maafw_runner.service import MaaFWRunnerService  # noqa: E402
from automas_maafw_runner.worker_registry import MaaFWWorkerRegistry  # noqa: E402


class _FakeLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)


class _FakeContext:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.logger = _FakeLogger()

    def set(self, key: str, value: object) -> None:
        self.values[key] = value


class _FakeAsyncProcess:
    def __init__(self, *, exit_on_terminate: bool = True) -> None:
        self.returncode: int | None = None
        self.exit_on_terminate = exit_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.exit_on_terminate:
            self.returncode = 15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = 9

    async def wait(self) -> int | None:
        if self.returncode is None:
            await asyncio.sleep(60)
        return self.returncode


class _FakeSyncProcess:
    def __init__(self, result_payload: dict[str, object]) -> None:
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.stdout = iter(
            [json.dumps({"type": "result", "data": result_payload}) + "\n"]
        )

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 15


class MaaFWRunnerStopContractTest(unittest.TestCase):
    def test_plugin_stop_terminates_registered_workers_and_rejects_late_worker(self) -> None:
        asyncio.run(self._plugin_stop_lifecycle())

    async def _plugin_stop_lifecycle(self) -> None:
        registry = MaaFWWorkerRegistry()
        context = _FakeContext()
        plugin = MaaFWRunnerPlugin(context)
        plugin.service = MaaFWRunnerService(worker_registry=registry)

        await plugin.on_start()
        first = _FakeAsyncProcess()
        second = _FakeAsyncProcess()
        self.assertIsNotNone(plugin.service.register_worker(first))
        self.assertIsNotNone(plugin.service.register_worker(second))
        self.assertEqual(registry.active_count, 2)

        await plugin.on_stop("plugin-reload")

        self.assertEqual(first.terminate_calls, 1)
        self.assertEqual(second.terminate_calls, 1)
        self.assertEqual(registry.active_count, 0)
        self.assertFalse(registry.accepting_workers)
        self.assertIsNone(context.values["maafw.runner.v1"])
        self.assertIn("workers=2", context.logger.messages[-1])

        late = _FakeAsyncProcess()
        self.assertIsNone(plugin.service.register_worker(late))
        self.assertEqual(late.terminate_calls, 1)
        self.assertEqual(registry.active_count, 0)

        await plugin.on_start()
        self.assertTrue(registry.accepting_workers)
        self.assertIsNotNone(plugin.service.register_worker(_FakeAsyncProcess()))

    def test_shutdown_escalates_to_kill_for_uncooperative_worker(self) -> None:
        async def shutdown() -> None:
            registry = MaaFWWorkerRegistry()
            worker = _FakeAsyncProcess(exit_on_terminate=False)
            self.assertIsNotNone(registry.register(worker))

            report = await registry.shutdown_all(graceful_timeout_seconds=0.001)

            self.assertEqual(report.requested, 1)
            self.assertEqual(report.terminated, 0)
            self.assertEqual(report.killed, 1)
            self.assertEqual(worker.terminate_calls, 1)
            self.assertEqual(worker.kill_calls, 1)
            self.assertEqual(registry.active_count, 0)

        asyncio.run(shutdown())

    def test_service_worker_path_unregisters_after_normal_completion(self) -> None:
        registry = MaaFWWorkerRegistry()
        service = MaaFWRunnerService(worker_registry=registry)
        payload = self._payload()
        expected = {
            "success": True,
            "projectName": "demo",
            "controllerName": "adb",
            "resourceName": "base",
            "completedTasks": ["Start"],
        }
        process = _FakeSyncProcess(expected)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "automas_maafw_runner.service.subprocess.Popen",
                return_value=process,
            ):
                result = service.run_worker(
                    payload,
                    work_dir=temporary_directory,
                    worker_command=["mock-worker"],
                )

        self.assertTrue(result.success)
        self.assertEqual(process.terminate_calls, 0)
        self.assertEqual(registry.active_count, 0)

    def test_script_adapter_path_registers_and_unregisters_worker(self) -> None:
        source = (
            ROOT
            / "packages"
            / "automas_script_maafw"
            / "src"
            / "automas_script_maafw"
            / "runner_task.py"
        ).read_text(encoding="utf-8")

        self.assertIn("worker_id = service.register_worker(process)", source)
        self.assertGreaterEqual(source.count("service.unregister_worker(worker_id)"), 2)
        self.assertIn(
            'native_debug_log_path = self.project_path / "debug" / "maafw.log"',
            source,
        )
        self.assertIn('f"{local_started_at.strftime(\'%H-%M-%S\')}.maafw.log"', source)
        self.assertIn('native_debug_log_file.seek(start_offset)', source)
        self.assertIn('write_framework_log("worker-stderr", line)', source)
        self.assertIn("MaaFW 框架调试日志已保存", source)
        self.assertIn("await self._save_user_logs()", source)

        script_project = tomllib.loads(
            (
                ROOT
                / "packages"
                / "automas_script_maafw"
                / "pyproject.toml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(script_project["project"]["version"], "0.1.13")
        self.assertIn(
            "automas-maafw-runner>=0.4.0",
            script_project["project"]["dependencies"],
        )
        self.assertIn(
            "automas-maafw-agent-env>=0.1.4",
            script_project["project"]["dependencies"],
        )
        self.assertIn(
            "automas-maafw-project-update>=0.2.3",
            script_project["project"]["dependencies"],
        )
        self.assertIn(
            "automas-maafw-runtime-pool>=0.2.0",
            script_project["project"]["dependencies"],
        )

    @staticmethod
    def _payload() -> MaaFWRunnerJobPayload:
        return MaaFWRunnerJobPayload(
            plan=MaaFWRunPlan(
                path="C:/mock-project",
                projectName="demo",
                controllerName="adb",
                controllerType="Adb",
                resourceName="base",
                resource=MaaFWResourceBundlePlan(name="base"),
            ),
            deviceConfig=MaaFWDeviceConfig(type="Adb", address="127.0.0.1:5555"),
        )
