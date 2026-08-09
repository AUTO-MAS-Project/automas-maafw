from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT / "packages" / "automas_maafw_agent_env" / "src",
    ROOT / "packages" / "automas_maafw_interface" / "src",
    ROOT / "packages" / "automas_maafw_runner" / "src",
)
for source_root in reversed(SOURCE_ROOTS):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

SCRIPT_PACKAGE = (
    ROOT / "packages" / "automas_script_maafw" / "src" / "automas_script_maafw"
)
MANAGED_PACKAGE = (
    ROOT
    / "packages"
    / "automas_script_maafw_managed"
    / "src"
    / "automas_script_maafw_managed"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_environment_module():
    script_package = types.ModuleType("automas_script_maafw")
    script_package.__path__ = [str(SCRIPT_PACKAGE)]
    sys.modules["automas_script_maafw"] = script_package
    _load_module(
        "automas_script_maafw.runtime_route",
        SCRIPT_PACKAGE / "runtime_route.py",
    )

    managed_package = types.ModuleType("automas_script_maafw_managed")
    managed_package.__path__ = [str(MANAGED_PACKAGE)]
    sys.modules["automas_script_maafw_managed"] = managed_package
    _load_module(
        "automas_script_maafw_managed.services",
        MANAGED_PACKAGE / "services.py",
    )
    return _load_module(
        "automas_script_maafw_managed.environment_service",
        MANAGED_PACKAGE / "environment_service.py",
    )


ENVIRONMENT = _load_environment_module()
ManagedServiceError = ENVIRONMENT.ManagedServiceError
ManagedServiceGateway = ENVIRONMENT.ManagedServiceGateway


class _FakeConfig:
    def __init__(
        self,
        record: dict[str, Any],
        events: list[str],
        *,
        fail_update: bool = False,
    ) -> None:
        self.record = record
        self.events = events
        self.fail_update = fail_update
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def get_script_records(self, script_id: str) -> list[dict[str, Any]]:
        self.events.append(f"read:{script_id}")
        return [self.record]

    @asynccontextmanager
    async def script_config_transaction(self, script_id: str, *, owner: str):
        self.events.append(f"config-enter:{script_id}:{owner}")
        try:
            yield
        finally:
            self.events.append("config-exit")

    async def update_script(
        self,
        script_id: str,
        update: dict[str, Any],
    ) -> None:
        self.events.append("persist")
        if self.fail_update:
            raise RuntimeError("synthetic config persistence failure")
        self.updates.append((script_id, update))


class _FakeGateway:
    def __init__(
        self,
        events: list[str],
        project_path: Path,
        *,
        invalid_runtime: bool = False,
        fail_rollback: bool = False,
    ) -> None:
        self.events = events
        self.project_path = project_path
        self.fail_rollback = fail_rollback
        self.runtime = {
            "runtimeId": "runtime-one",
            "poolId": "pool-one",
            "pythonExecutable": "C:/pool/runtime/python.exe",
            "venvPath": "C:/pool/runtime",
            "maafwRequirement": "maafw==5.10.4",
            "selectorRequirements": [
                "json5==0.12.1",
                "maafw==5.10.4",
            ],
        }
        if invalid_runtime:
            self.runtime.pop("selectorRequirements")

    @asynccontextmanager
    async def resource_transaction(self):
        self.events.append("resource-enter")
        try:
            yield
        finally:
            self.events.append("resource-exit")

    async def resolve_execution(self, request: dict[str, Any]) -> dict[str, Any]:
        self.events.append("resolve")
        self.request = request
        deferred = request.get("deferRuntimeBinding") is True
        if not deferred:
            # The real gateway may bind while recovering or first creating a
            # runtime.  Model that side effect so the environment service must
            # explicitly request the atomic, deferred-binding path.
            self.events.append("resolve-bind-side-effect")
        return {
            "project": {
                "projectId": "M9A",
                "version": "4.5.4",
                "storeId": "store-one",
                "manifest": {
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
                },
            },
            "runtime": dict(self.runtime),
            "projectPath": str(self.project_path),
            "runtimeConstraint": "maafw==5.10.4",
            "checkout": {
                "runRootId": "run-root-one",
                "payloadHash": "a" * 64,
            },
            "bindingPersistenceDeferred": deferred,
        }

    async def bind_project_runtime(
        self,
        project_id: str,
        version: str,
        runtime: dict[str, Any],
        *,
        project_reference: str,
    ) -> dict[str, Any]:
        self.events.append("bind")
        self.binding_call = (
            project_id,
            version,
            runtime,
            project_reference,
        )
        return {
            "projectId": project_id,
            "version": version,
            "storeId": "store-one",
            "manifest": {
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
                    "binding": dict(runtime),
                }
            },
        }

    async def bind_project_runtime_reversible(
        self,
        project_id: str,
        version: str,
        runtime: dict[str, Any],
        *,
        project_reference: str,
    ) -> dict[str, Any]:
        project = await self.bind_project_runtime(
            project_id,
            version,
            runtime,
            project_reference=project_reference,
        )
        return {
            "project": project,
            "rollback": {
                "apiVersion": "maafw-managed-runtime-binding-rollback.v1",
                "projectId": project_id,
                "version": version,
            },
        }

    async def rollback_project_runtime_binding(
        self,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        self.events.append("rollback")
        self.rollback_receipt = dict(receipt)
        if self.fail_rollback:
            raise RuntimeError("synthetic rollback failure")
        return {"restored": True}

    async def runtime_storage_info(self) -> dict[str, Any]:
        self.events.append("storage")
        return {
            "available": True,
            "root": "C:/pool",
            "poolId": "pool-one",
            "rootIdentity": {"poolId": "pool-one"},
        }

    async def load_interface(self, project_path: str) -> dict[str, Any]:
        self.events.append("interface")
        self.interface_path = project_path
        return {"interface_version": 2, "agent": []}


class _FakeRunner:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.call: tuple[Any, ...] | None = None
        self.kwargs: dict[str, Any] | None = None

    def prepare_project_environment(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.events.append("prepare")
        self.call = args
        self.kwargs = kwargs
        if self.fail:
            raise RuntimeError("prepare exploded")
        return {"status": "ready", "runtime": {"runtimeId": "runtime-one"}}


def _managed_record() -> dict[str, Any]:
    return {
        "id": "script-one",
        "type": "MaaFWManaged",
        "config": {
            "Managed": {
                "ProjectId": "M9A",
                "Version": "4.5.4",
                "StoreId": "store-one",
                "RuntimeConstraint": "maafw==5.10.4",
                "ProjectManifest": {
                    "source": {
                        "hash": {
                            "algorithm": "sha256",
                            "scope": "tree",
                            "value": "b" * 64,
                        }
                    }
                },
            }
        },
    }


class MaaFWManagedEnvironmentContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_defer_flag_prevents_recovery_binding(self) -> None:
        events: list[str] = []
        project_store = types.SimpleNamespace(checkout_project=lambda: None)
        gateway = ManagedServiceGateway(project_store, object())

        async def resolve_project(
            project_id: str,
            version: str | None,
        ) -> dict[str, Any]:
            return {
                "projectId": project_id,
                "version": version,
                "dataPath": "C:/store/M9A/4.5.4/data",
                "manifest": {"runtime": {}},
            }

        async def checkout_project(
            project_id: str,
            version: str | None,
            script_id: str,
        ) -> dict[str, Any]:
            return {
                "projectId": project_id,
                "version": version,
                "scriptId": script_id,
                "dataPath": "C:/run/script-one/data",
                "runRootId": "run-root-one",
                "payloadHash": "a" * 64,
            }

        async def resolve_runtime(_request: dict[str, Any]):
            return None

        async def ensure_runtime(_request: dict[str, Any]):
            events.append("ensure")
            return {
                "runtimeId": "runtime-one",
                "pythonExecutable": "C:/pool/runtime/python.exe",
            }

        async def bind_project_runtime(*_args: Any, **_kwargs: Any):
            events.append("bind")
            raise AssertionError("deferred resolve must not bind")

        gateway.resolve_project = resolve_project  # type: ignore[method-assign]
        gateway.checkout_project = checkout_project  # type: ignore[method-assign]
        gateway.resolve_runtime = resolve_runtime  # type: ignore[method-assign]
        gateway.ensure_runtime = ensure_runtime  # type: ignore[method-assign]
        gateway.bind_project_runtime = bind_project_runtime  # type: ignore[method-assign]
        services_module = sys.modules[ManagedServiceGateway.__module__]
        with mock.patch.object(
            services_module,
            "_runner_requirements",
            return_value=["maafw==5.10.4"],
        ):
            resolution = await gateway.resolve_execution(
                {
                    "projectId": "M9A",
                    "version": "4.5.4",
                    "runtimeConstraint": "maafw==5.10.4",
                    "scriptId": "script-one",
                    "deferRuntimeBinding": True,
                }
            )

        self.assertEqual(events, ["ensure"])
        self.assertTrue(resolution["bindingPersistenceDeferred"])
        self.assertEqual(resolution["runtime"]["runtimeId"], "runtime-one")

    async def test_recovery_states_fail_closed_before_environment_resolution(
        self,
    ) -> None:
        self.assertEqual(
            ManagedServiceGateway.UPGRADE_BLOCKING_STATES,
            frozenset(
                {
                    "applying",
                    "committing",
                    "recovery_required",
                    "rollback_failed",
                }
            ),
        )
        self.assertNotIn(
            "planned",
            ManagedServiceGateway.UPGRADE_BLOCKING_STATES,
        )
        self.assertNotIn(
            "rollback-required",
            ManagedServiceGateway.UPGRADE_BLOCKING_STATES,
        )

        for state in ("recovery_required", "rollback_failed"):
            with self.subTest(state=state):
                events: list[str] = []
                record = _managed_record()
                record["config"]["Managed"]["PendingUpgrade"] = {
                    "state": state,
                }
                config = _FakeConfig(record, events)
                gateway = _FakeGateway(events, Path("C:/unused"))
                runner = _FakeRunner(events)

                async def reserve(_path: str | Path):
                    events.append("reserve")
                    return "reservation"

                service = ENVIRONMENT.MaaFWManagedEnvironmentService(
                    config=config,
                    gateway_provider=lambda: gateway,
                    runner_provider=lambda: runner,
                    reserve_project_path=reserve,
                    release_project_path=lambda _reservation: None,
                )
                with self.assertRaisesRegex(ManagedServiceError, state):
                    await service.prepare_script_environment(
                        "script-one",
                        None,
                    )

                self.assertNotIn("resolve", events)
                self.assertNotIn("persist", events)
                self.assertNotIn("prepare", events)
                self.assertNotIn("reserve", events)

    async def test_uses_authoritative_checkout_and_full_runtime_selector(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_path = Path(temporary_directory) / "checkout"
            project_path.mkdir()
            config = _FakeConfig(_managed_record(), events)
            gateway = _FakeGateway(events, project_path)
            runner = _FakeRunner(events)

            async def reserve(path: str | Path):
                events.append(f"reserve:{Path(path).name}")
                return "reservation-one"

            async def release(reservation: Any) -> None:
                events.append(f"release:{reservation}")

            service = ENVIRONMENT.MaaFWManagedEnvironmentService(
                config=config,
                gateway_provider=lambda: gateway,
                runner_provider=lambda: runner,
                reserve_project_path=reserve,
                release_project_path=release,
                import_paths_provider=lambda: ["C:/plugins"],
            )
            logs: list[str] = []
            result = await service.prepare_script_environment(
                "script-one",
                "C:/stale/untrusted/project",
                send_log=logs.append,
                progress=lambda _event: None,
            )

        self.assertEqual(result["projectPath"], str(project_path))
        self.assertEqual(result["prepareResult"]["status"], "ready")
        self.assertEqual(runner.call[0], str(project_path))
        self.assertEqual(gateway.interface_path, str(project_path))
        self.assertEqual(
            runner.kwargs["runtime_requirements"],
            ("json5==0.12.1", "maafw==5.10.4"),
        )
        self.assertEqual(runner.kwargs["runtime_requirement"], "maafw==5.10.4")
        self.assertEqual(runner.kwargs["runtime_id"], "runtime-one")
        self.assertEqual(runner.kwargs["runtime_pool_id"], "pool-one")
        self.assertTrue(
            runner.kwargs["managed_shared_agent_dependencies_complete"]
        )
        self.assertEqual(runner.kwargs["managed_python_agent_indexes"], (0,))
        self.assertEqual(runner.kwargs["import_paths"], ["C:/plugins"])
        self.assertIn("权威 checkout", logs[0])
        persisted = config.updates[0][1]
        self.assertEqual(persisted["Info"]["Path"], str(project_path))
        self.assertEqual(
            persisted["ManagedRuntime"]["RuntimeBinding"]["runtimeId"],
            "runtime-one",
        )
        self.assertLess(events.index("resource-enter"), events.index("resolve"))
        self.assertLess(events.index("config-enter:script-one:maafw-managed-environment:script-one"), events.index("resolve"))
        self.assertLess(events.index("reserve:checkout"), events.index("bind"))
        self.assertTrue(gateway.request["deferRuntimeBinding"])
        self.assertNotIn("resolve-bind-side-effect", events)
        self.assertLess(events.index("prepare"), events.index("bind"))
        self.assertLess(events.index("bind"), events.index("persist"))
        self.assertLess(events.index("release:reservation-one"), events.index("config-exit"))
        self.assertLess(events.index("config-exit"), events.index("resource-exit"))

    async def test_non_managed_script_returns_none_without_managed_services(self) -> None:
        events: list[str] = []
        record = _managed_record()
        record["type"] = "MaaFW"
        config = _FakeConfig(record, events)
        gateway_requested = False

        def gateway_provider():
            nonlocal gateway_requested
            gateway_requested = True
            raise AssertionError("gateway must not be requested")

        service = ENVIRONMENT.MaaFWManagedEnvironmentService(
            config=config,
            gateway_provider=gateway_provider,
            runner_provider=lambda: None,
            reserve_project_path=lambda _path: None,
            release_project_path=lambda _reservation: None,
        )
        result = await service.prepare_script_environment(
            "script-one",
            "C:/ordinary",
        )
        self.assertIsNone(result)
        self.assertFalse(gateway_requested)

    async def test_incomplete_runtime_dto_fails_closed_before_reservation(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = _FakeConfig(_managed_record(), events)
            gateway = _FakeGateway(
                events,
                Path(temporary_directory),
                invalid_runtime=True,
            )
            runner = _FakeRunner(events)

            async def reserve(_path: str | Path):
                events.append("reserve")
                return "reservation"

            service = ENVIRONMENT.MaaFWManagedEnvironmentService(
                config=config,
                gateway_provider=lambda: gateway,
                runner_provider=lambda: runner,
                reserve_project_path=reserve,
                release_project_path=lambda _reservation: None,
            )
            with self.assertRaisesRegex(
                ManagedServiceError,
                "selectorRequirements/packages",
            ):
                await service.prepare_script_environment(
                    "script-one",
                    None,
                )
        self.assertNotIn("reserve", events)
        self.assertNotIn("prepare", events)
        self.assertFalse(config.updates)

    async def test_runner_failure_releases_path_before_transactions_exit(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = _FakeConfig(_managed_record(), events)
            gateway = _FakeGateway(events, Path(temporary_directory))
            runner = _FakeRunner(events, fail=True)

            async def reserve(_path: str | Path):
                events.append("reserve")
                return "reservation"

            async def release(_reservation: Any) -> None:
                events.append("release")

            service = ENVIRONMENT.MaaFWManagedEnvironmentService(
                config=config,
                gateway_provider=lambda: gateway,
                runner_provider=lambda: runner,
                reserve_project_path=reserve,
                release_project_path=release,
            )
            with self.assertRaisesRegex(RuntimeError, "prepare exploded"):
                await service.prepare_script_environment(
                    "script-one",
                    None,
                )
        self.assertIn("release", events)
        self.assertTrue(gateway.request["deferRuntimeBinding"])
        self.assertNotIn("resolve-bind-side-effect", events)
        self.assertNotIn("bind", events)
        self.assertNotIn("persist", events)
        self.assertFalse(config.updates)
        self.assertLess(events.index("release"), events.index("config-exit"))
        self.assertLess(events.index("config-exit"), events.index("resource-exit"))

    async def test_config_failure_rolls_back_runtime_binding_before_unlock(
        self,
    ) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = _FakeConfig(
                _managed_record(),
                events,
                fail_update=True,
            )
            gateway = _FakeGateway(events, Path(temporary_directory))
            runner = _FakeRunner(events)

            async def reserve(_path: str | Path):
                events.append("reserve")
                return "reservation"

            async def release(_reservation: Any) -> None:
                events.append("release")

            service = ENVIRONMENT.MaaFWManagedEnvironmentService(
                config=config,
                gateway_provider=lambda: gateway,
                runner_provider=lambda: runner,
                reserve_project_path=reserve,
                release_project_path=release,
            )
            with self.assertRaisesRegex(
                ManagedServiceError,
                "synthetic config persistence failure",
            ):
                await service.prepare_script_environment(
                    "script-one",
                    None,
                )

        self.assertEqual(gateway.rollback_receipt["projectId"], "M9A")
        self.assertFalse(config.updates)
        self.assertLess(events.index("prepare"), events.index("bind"))
        self.assertLess(events.index("bind"), events.index("persist"))
        self.assertLess(events.index("persist"), events.index("rollback"))
        self.assertLess(events.index("rollback"), events.index("release"))
        self.assertLess(events.index("release"), events.index("config-exit"))
        self.assertLess(events.index("config-exit"), events.index("resource-exit"))

    async def test_rollback_failure_is_noted_on_config_failure(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = _FakeConfig(
                _managed_record(),
                events,
                fail_update=True,
            )
            gateway = _FakeGateway(
                events,
                Path(temporary_directory),
                fail_rollback=True,
            )
            runner = _FakeRunner(events)

            async def reserve(_path: str | Path):
                return "reservation"

            async def release(_reservation: Any) -> None:
                return None

            service = ENVIRONMENT.MaaFWManagedEnvironmentService(
                config=config,
                gateway_provider=lambda: gateway,
                runner_provider=lambda: runner,
                reserve_project_path=reserve,
                release_project_path=release,
            )
            with self.assertRaises(ManagedServiceError) as raised:
                await service.prepare_script_environment(
                    "script-one",
                    None,
                )

        self.assertIn("synthetic config persistence failure", str(raised.exception))
        self.assertTrue(
            any(
                "runtime binding 补偿未完成" in note
                and "synthetic rollback failure" in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )
        self.assertIn("rollback", events)


if __name__ == "__main__":
    unittest.main()
