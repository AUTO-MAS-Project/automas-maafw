from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "packages" / "automas_script_maafw" / "src"


class _Config:
    @classmethod
    async def get_script_records(cls, _script_id: str):
        return []

    proxy = None


class _PluginField:
    @classmethod
    def group(cls, *_args, **_kwargs):
        return None

    @classmethod
    def string(cls, *_args, **_kwargs):
        return None

    @classmethod
    def folder(cls, *_args, **_kwargs):
        return None

    @classmethod
    def related_id(cls, *_args, **_kwargs):
        return None

    @classmethod
    def file(cls, *_args, **_kwargs):
        return None

    @classmethod
    def number(cls, *_args, **_kwargs):
        return None

    @classmethod
    def boolean(cls, *_args, **_kwargs):
        return None

    @classmethod
    def select(cls, *_args, **_kwargs):
        return None

    @classmethod
    def json(cls, *_args, **_kwargs):
        return None

    @classmethod
    def datetime(cls, *_args, **_kwargs):
        return None


def _load_api_module():
    app = types.ModuleType("app")
    app.__path__ = []
    app_core = types.ModuleType("app.core")
    app_core.Config = _Config
    app_plugins = types.ModuleType("app.plugins")
    app_plugins.__path__ = []
    app_plugins.PluginHttpRequest = object
    app_plugins.PluginWebSocketSession = object
    app_fields = types.ModuleType("app.plugins.fields")
    app_fields.PluginField = _PluginField
    app.core = app_core
    app.plugins = app_plugins
    app_plugins.fields = app_fields
    stubs = {
        "app": app,
        "app.core": app_core,
        "app.plugins": app_plugins,
        "app.plugins.fields": app_fields,
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.path.insert(0, str(SOURCE_ROOT))
    try:
        sys.modules.update(stubs)
        return importlib.import_module("automas_script_maafw.api"), previous
    except Exception:
        sys.path.remove(str(SOURCE_ROOT))
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        raise


class MaaFWApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api, cls._previous_stubs = _load_api_module()

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("automas_script_maafw.api", None)
        sys.path.remove(str(SOURCE_ROOT))
        for name, value in cls._previous_stubs.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    @staticmethod
    def _request(payload: dict, *, headers: dict[str, str] | None = None):
        return SimpleNamespace(json=payload, query={}, headers=headers or {})

    @staticmethod
    def _record(
        project_path: Path | None,
        *,
        record_type: str = "MaaFW",
        managed: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        config: dict = {}
        if managed:
            config["Managed"] = {"ProjectId": "managed-project"}
        script_form: dict = {"Info": {}}
        if project_path is not None:
            script_form["Info"]["Path"] = str(project_path)
        config["PluginData"] = {"Config": script_form}
        record = {"type": record_type, "config": config}
        if metadata is not None:
            record["metadata"] = metadata
        return record

    def _controller(self, *, services: dict | None = None):
        context = SimpleNamespace(get=lambda key: (services or {}).get(key))
        controller = self.api.MaaFWApiController(context)
        events: list[dict] = []

        def callback(_event_type, script_id, *, project_path=None):
            def publish(progress):
                value = dict(progress)
                value["scriptId"] = script_id
                if project_path is not None:
                    value["project_path"] = project_path
                events.append(value)

            return publish

        controller._progress_callback = callback
        return controller, events

    def test_prepare_rejects_managed_and_path_mismatch_with_one_terminal_event(self):
        async def exercise():
            requested = Path.cwd().resolve() / "ordinary-project"
            record = self._record(requested, managed=True)
            with patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)):
                controller, events = self._controller()
                response = await controller.prepare_agent_env(
                    self._request({"path": str(requested), "scriptId": "script-1"})
                )
            return response, events

        response, events = asyncio.run(exercise())
        self.assertEqual(response["code"], 409)
        self.assertEqual([event["status"] for event in events], ["managed"])
        self.assertTrue(events[0]["final"])

        async def mismatch():
            configured = Path.cwd().resolve() / "configured-project"
            requested = Path.cwd().resolve() / "other-project"
            record = self._record(configured)
            with patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)):
                controller, mismatch_events = self._controller()
                mismatch_response = await controller.prepare_agent_env(
                    self._request({"path": str(requested), "scriptId": "script-2"})
                )
            return mismatch_response, mismatch_events

        response, events = asyncio.run(mismatch())
        self.assertEqual(response["code"], 400)
        self.assertEqual([event["status"] for event in events], ["path_mismatch"])
        self.assertTrue(events[0]["final"])

    def test_prepare_requires_absolute_path_and_cache_only_miss_is_terminal(self):
        async def exercise():
            controller, relative_events = self._controller()
            relative_response = await controller.prepare_agent_env(
                self._request({"path": "relative-project"})
            )

            project = Path.cwd().resolve() / "cache-project"
            record = self._record(project)
            order: list[str] = []

            async def reserve(_path):
                order.append("reserve")
                return "reservation"

            def load(_script_id, _path):
                order.append("load")
                return None

            services = {
                self.api.RUNTIME_POOL_SERVICE: SimpleNamespace(
                    storage_info=lambda: {
                        "root": str(project),
                        "poolId": "pool",
                        "rootIdentity": {"poolId": "pool"},
                    }
                )
            }
            with (
                patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)),
                patch.object(self.api, "try_reserve_project_path", new=reserve),
                patch.object(self.api, "release_project_path", new=AsyncMock()),
                patch.object(self.api, "load_maafw_agent_env_state", side_effect=load),
            ):
                controller, cache_events = self._controller(services=services)
                cache_response = await controller.prepare_agent_env(
                    self._request(
                        {"path": str(project), "scriptId": "script-cache"},
                        headers={"X-MaaFW-Cache-Only": "1"},
                    )
                )
            return relative_response, relative_events, cache_response, cache_events, order

        relative_response, relative_events, cache_response, cache_events, order = asyncio.run(
            exercise()
        )
        self.assertEqual(relative_response["code"], 400)
        self.assertEqual(relative_events, [])
        self.assertEqual(cache_response["code"], 404)
        self.assertEqual(order, ["reserve", "load"])
        self.assertEqual([event["status"] for event in cache_events], ["not_ready"])
        self.assertTrue(cache_events[0]["final"])

    def test_prepare_accepts_custom_type_declaring_maafw_framework(self):
        async def exercise():
            project = Path.cwd().resolve() / "custom-maafw-project"
            record = self._record(
                project,
                record_type="M9A",
                metadata={
                    "framework": "maafw",
                    "editor_kind": "plugin:automas_script_maafw",
                },
            )
            services = {
                self.api.RUNTIME_POOL_SERVICE: SimpleNamespace(
                    storage_info=lambda: {
                        "root": str(project),
                        "poolId": "pool",
                        "rootIdentity": {"poolId": "pool"},
                    }
                )
            }
            registry_module = types.ModuleType("app.core.script_types")
            registry_module.script_type_registry = SimpleNamespace(
                get=lambda _type_key: SimpleNamespace(metadata={"framework": "maafw"})
            )
            with (
                patch.dict(sys.modules, {"app.core.script_types": registry_module}),
                patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)),
                patch.object(self.api, "try_reserve_project_path", new=AsyncMock(return_value="reservation")),
                patch.object(self.api, "release_project_path", new=AsyncMock()),
                patch.object(self.api, "load_maafw_agent_env_state", side_effect=lambda *_args: None),
            ):
                controller, events = self._controller(services=services)
                response = await controller.prepare_agent_env(
                    self._request(
                        {"path": str(project), "scriptId": "custom-maafw-script"},
                        headers={"X-MaaFW-Cache-Only": "1"},
                    )
                )
            return response, events

        response, events = asyncio.run(exercise())
        self.assertEqual(response["code"], 404)
        self.assertEqual(response["status"], "not_ready")
        self.assertEqual([event["status"] for event in events], ["not_ready"])

    def test_project_update_accepts_custom_type_declaring_maafw_framework(self):
        async def exercise():
            project = Path.cwd().resolve() / "custom-maafw-update-project"
            record = self._record(
                project,
                record_type="M9A",
                metadata={
                    "framework": "maafw",
                    "editor_kind": "plugin:automas_script_maafw",
                },
            )

            async def invoke(_service, method_name, *_args, **kwargs):
                if method_name == "load":
                    return {"version": "1.0.0"}
                return None

            services = {
                self.api.INTERFACE_SERVICE: object(),
                self.api.PROJECT_UPDATE_SERVICE: object(),
            }
            registry_module = types.ModuleType("app.core.script_types")
            registry_module.script_type_registry = SimpleNamespace(
                get=lambda _type_key: SimpleNamespace(metadata={"framework": "maafw"})
            )
            with (
                patch.dict(sys.modules, {"app.core.script_types": registry_module}),
                patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)),
                patch.object(self.api, "try_reserve_project_path", new=AsyncMock(return_value="reservation")),
                patch.object(self.api, "release_project_path", new=AsyncMock()),
                patch.object(self.api, "_invoke_provider", side_effect=invoke),
            ):
                controller, events = self._controller(services=services)
                response = await controller.project_update(
                    self._request(
                        {
                            "scriptId": "00000000-0000-4000-8000-000000000003",
                            "apply": False,
                        }
                    )
                )
            return response, events

        response, events = asyncio.run(exercise())
        self.assertEqual(response["code"], 200)
        self.assertEqual(response["data"]["checked"], True)
        self.assertEqual([event["status"] for event in events], ["running", "no_update"])

    def test_non_maafw_framework_is_rejected_even_for_custom_type(self):
        async def exercise():
            project = Path.cwd().resolve() / "non-maafw-project"
            record = self._record(
                project,
                record_type="M9A",
                metadata={"framework": "script_adapter", "editor_kind": "plugin:other"},
            )
            registry_module = types.ModuleType("app.core.script_types")
            registry_module.script_type_registry = SimpleNamespace(
                get=lambda _type_key: SimpleNamespace(
                    metadata={"framework": "script_adapter"}
                )
            )
            with (
                patch.dict(sys.modules, {"app.core.script_types": registry_module}),
                patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)),
            ):
                controller, events = self._controller()
                response = await controller.project_update(
                    self._request(
                        {
                            "scriptId": "00000000-0000-4000-8000-000000000004",
                            "apply": False,
                        }
                    )
                )
            return response, events

        response, events = asyncio.run(exercise())
        self.assertEqual(response["code"], 400)
        self.assertEqual(response["data"]["logs"], [])
        self.assertEqual([event["status"] for event in events], ["not_maafw"])

    def test_exact_maafw_registry_error_stays_ordinary(self):
        record = self._record(Path.cwd().resolve() / "native-maafw-project")
        registry_module = types.ModuleType("app.core.script_types")
        registry_module.script_type_registry = SimpleNamespace(
            get=Mock(side_effect=RuntimeError("native registry unavailable"))
        )
        with patch.dict(sys.modules, {"app.core.script_types": registry_module}):
            self.assertTrue(self.api._is_maafw_record(record))
            self.assertFalse(self.api._is_managed_record(record))

    def test_custom_maafw_registry_error_fails_closed_as_managed(self):
        record = self._record(
            Path.cwd().resolve() / "custom-maafw-project",
            record_type="M9A",
        )
        registry_module = types.ModuleType("app.core.script_types")
        registry_module.script_type_registry = SimpleNamespace(
            get=Mock(
                side_effect=[
                    SimpleNamespace(metadata={"framework": "maafw"}),
                    RuntimeError("registry disappeared"),
                ]
            )
        )
        with patch.dict(sys.modules, {"app.core.script_types": registry_module}):
            self.assertTrue(self.api._is_maafw_record(record))
            self.assertTrue(self.api._is_managed_record(record))

    def test_prepare_busy_emits_one_terminal_failure(self):
        async def exercise():
            project = Path.cwd().resolve() / "busy-project"
            record = self._record(project)
            with (
                patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)),
                patch.object(
                    self.api,
                    "try_reserve_project_path",
                    new=AsyncMock(return_value=None),
                ),
            ):
                controller, events = self._controller()
                response = await controller.prepare_agent_env(
                    self._request({"path": str(project), "scriptId": "busy-script"})
                )
            return response, events

        response, events = asyncio.run(exercise())
        self.assertEqual(response["code"], 409)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "busy")

    def test_project_update_cancellation_emits_exactly_one_cancelled_terminal(self):
        events_holder: list[list[dict]] = []

        async def exercise():
            project = Path.cwd().resolve() / "cancel-project"
            record = self._record(project)
            services = {
                self.api.INTERFACE_SERVICE: object(),
                self.api.PROJECT_UPDATE_SERVICE: object(),
            }
            with (
                patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)),
                patch.object(
                    self.api,
                    "_invoke_provider",
                    new=AsyncMock(side_effect=asyncio.CancelledError()),
                ),
            ):
                controller, events = self._controller(services=services)
                events_holder.append(events)
                response = await controller.project_update(
                    self._request(
                        {
                            "scriptId": "00000000-0000-4000-8000-000000000001",
                            "apply": False,
                        }
                    )
                )
            return response, events

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(exercise())
        # The endpoint re-raises cancellation so the gateway can cancel the
        # caller, but the callback was still emitted exactly once before that.
        self.assertEqual(len(events_holder), 1)
        self.assertEqual(len(events_holder[0]), 1)
        self.assertEqual(events_holder[0][0]["status"], "cancelled")
        self.assertTrue(events_holder[0][0]["final"])

    def test_project_update_deferred_provider_terminal_is_flushed_once(self):
        async def exercise():
            project = Path.cwd().resolve() / "deferred-project"
            record = self._record(project)

            async def invoke(_service, method_name, *_args, **kwargs):
                if method_name == "load":
                    return {"version": "1.0.0"}
                kwargs["progress"](
                    {
                        "stage": "completed",
                        "status": "updated",
                        "message": "资源检查完成",
                        "final": True,
                    }
                )
                return {
                    "checked": True,
                    "updated": False,
                    "update_available": False,
                    "installable": False,
                    "message": "无需更新",
                }

            services = {
                self.api.INTERFACE_SERVICE: object(),
                self.api.PROJECT_UPDATE_SERVICE: object(),
            }
            with (
                patch.object(self.api, "_script_record", new=AsyncMock(return_value=record)),
                patch.object(self.api, "_invoke_provider", side_effect=invoke),
            ):
                controller, events = self._controller(services=services)
                response = await controller.project_update(
                    self._request(
                        {
                            "scriptId": "00000000-0000-4000-8000-000000000002",
                            "apply": True,
                        }
                    )
                )
            return response, events

        response, events = asyncio.run(exercise())
        self.assertEqual(response["code"], 200)
        terminal = [event for event in events if event.get("final") is True]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["status"], "updated")

    def test_close_closes_each_session_with_going_away_code(self):
        class Session:
            def __init__(self):
                self.calls = []

            async def close(self, *, code=1000, reason=""):
                self.calls.append((code, reason))

        async def exercise():
            controller, _events = self._controller()
            first = Session()
            second = Session()
            await controller._on_progress_connect(first)
            await controller._on_progress_connect(second)
            await controller.close()
            return first, second

        first, second = asyncio.run(exercise())
        self.assertEqual(first.calls[0][0], 1001)
        self.assertEqual(second.calls[0][0], 1001)

    def test_progress_connect_during_shutdown_is_closed_without_registration(self):
        class Session:
            def __init__(self):
                self.calls = []

            async def close(self, *, code=1000, reason=""):
                self.calls.append((code, reason))

        async def exercise():
            controller, _events = self._controller()
            controller._draining = True
            session = Session()
            await controller._on_progress_connect(session)
            return session, controller._sessions

        session, sessions = asyncio.run(exercise())
        self.assertEqual(session.calls, [(1001, "MaaFW 插件正在停止")])
        self.assertNotIn(session, sessions)

    def test_progress_broadcast_strips_sensitive_transport_fields(self):
        class Session:
            def __init__(self):
                self.messages = []

            async def send_json(self, message):
                self.messages.append(message)

        async def exercise():
            controller, _events = self._controller()
            session = Session()
            await controller._on_progress_connect(session)
            await controller._broadcast(
                self.api.PROJECT_UPDATE_PROGRESS,
                "script-1",
                {
                    "scriptId": "spoofed",
                    "cdk": "secret-cdk",
                    "proxy": "http://secret",
                    "source_config": {"token": "secret-token"},
                    "project_path": "C:/private/ordinary-project",
                    "message": "safe",
                },
            )
            return session

        session = asyncio.run(exercise())
        data = session.messages[0]["data"]
        self.assertEqual(data["scriptId"], "script-1")
        self.assertNotIn("cdk", data)
        self.assertNotIn("proxy", data)
        self.assertNotIn("source_config", data)
        self.assertNotIn("project_path", data)
        self.assertEqual(data["message"], "safe")

    def test_boundary_booleans_do_not_accept_truthy_strings(self):
        with self.assertRaises(ValidationError):
            self.api.MaaFWProjectUpdateIn.model_validate(
                {"scriptId": "script", "apply": "false"}
            )
        with self.assertRaises(ValueError):
            self.api._strict_bool("true", "provider.updated")


if __name__ == "__main__":
    unittest.main()
