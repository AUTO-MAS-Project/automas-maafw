from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = (
    ROOT / "packages" / "automas_script_maafw" / "src" / "automas_script_maafw"
)


class _Logger:
    def info(self, _message: str) -> None:
        return None


class _InterfaceService:
    def load(self, _path: Path, **_kwargs: Any) -> Any:
        return SimpleNamespace(controller=[object()], resource=[object()], task=[object()])


class _InterfaceLoadError(RuntimeError):
    pass


class _UserItem:
    def __init__(self, *, user_id: str, name: str, status: str) -> None:
        self.user_id = user_id
        self.name = name
        self.status = status


class _ScriptModel:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = copy.deepcopy(payload)

    def get(self, group: str, name: str) -> Any:
        value = self.payload.get(group)
        return value.get(name) if isinstance(value, dict) else None


class _ConfigBackend:
    """Minimal PluginData.Config persistence keyed by stable scriptId."""

    def __init__(self, forms: dict[str, dict[str, Any]]) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.save_calls: list[str] = []
        self.read_calls: list[str] = []
        for script_id, form in forms.items():
            self._write(script_id, form)

    def save_form(self, script_id: str, form: dict[str, Any]) -> None:
        self.save_calls.append(script_id)
        self._write(script_id, form)

    def load_model(self, script_id: str) -> _ScriptModel:
        self.read_calls.append(script_id)
        raw = self.records[script_id]["PluginData"]["Config"]
        return _ScriptModel(json.loads(raw))

    def _write(self, script_id: str, form: dict[str, Any]) -> None:
        normalized = copy.deepcopy(form)
        name = str(normalized.get("Info", {}).get("Name") or "MaaFW")
        self.records[script_id] = {
            "Info": {"Name": name},
            "PluginData": {
                "Config": json.dumps(normalized, ensure_ascii=False),
            },
        }


class _BoundStorage:
    def __init__(self, backend: _ConfigBackend, script_id: str) -> None:
        self.backend = backend
        self.script_id = script_id
        self.locked = False

    async def lock(self) -> None:
        self.locked = True

    async def unlock(self) -> None:
        self.locked = False

    async def load_user_collection(self) -> dict[Any, Any]:
        return {}

    async def read_script_data(self) -> dict[str, Any]:
        raw = self.backend.records[self.script_id]["PluginData"]["Config"]
        return json.loads(raw)


class _Runtime:
    def __init__(self, backend: _ConfigBackend, script_id: str) -> None:
        self.script_id = script_id
        self.mode = "AutoProxy"
        self.storage = _BoundStorage(backend, script_id)
        self.script_info = SimpleNamespace(
            script_id=script_id,
            user_list=[],
            log="",
        )
        self.script_config = None
        self.user_config = None
        self.emulator_manager = None
        self.extra: dict[str, Any] = {}

    async def build_script_model(self) -> _ScriptModel:
        return self.storage.backend.load_model(self.script_id)

    async def initialize_emulator_manager(self, _emulator_id: str) -> None:
        raise AssertionError("Emulator.Id='-' must not initialize an emulator")


def _module(name: str, **attributes: Any) -> types.ModuleType:
    value = types.ModuleType(name)
    for key, item in attributes.items():
        setattr(value, key, item)
    return value


def _load_adapter_module() -> types.ModuleType:
    class _ScriptAdapterHooks:
        pass

    package = _module("automas_script_maafw")
    package.__path__ = [str(MODULE_ROOT)]  # type: ignore[attr-defined]
    app_package = _module("app")
    app_package.__path__ = []  # type: ignore[attr-defined]
    app_models = _module("app.models")
    app_models.__path__ = []  # type: ignore[attr-defined]
    interface_package = _module("automas_maafw_interface")
    interface_package.__path__ = []  # type: ignore[attr-defined]
    update_package = _module("automas_maafw_project_update")
    update_package.__path__ = []  # type: ignore[attr-defined]
    stubs = {
        "app": app_package,
        "app.core": _module("app.core", Config=SimpleNamespace()),
        "app.models": app_models,
        "app.models.task": _module(
            "app.models.task",
            ScriptItem=object,
            TaskExecuteBase=object,
            UserItem=_UserItem,
        ),
        "app.plugins": _module(
            "app.plugins",
            ScriptAdapterHooks=_ScriptAdapterHooks,
            ScriptAdapterRuntime=object,
        ),
        "app.utils": _module("app.utils", get_logger=lambda _name: _Logger()),
        "automas_maafw_interface": interface_package,
        "automas_maafw_interface.loader": _module(
            "automas_maafw_interface.loader",
            MaaFWInterfaceLoadError=_InterfaceLoadError,
        ),
        "automas_maafw_interface.service": _module(
            "automas_maafw_interface.service",
            MaaFWInterfaceService=_InterfaceService,
        ),
        "automas_maafw_project_update": update_package,
        "automas_maafw_project_update.service": _module(
            "automas_maafw_project_update.service",
            MaaFWProjectUpdateService=object,
        ),
        "automas_script_maafw": package,
        "automas_script_maafw.runner_task": _module(
            "automas_script_maafw.runner_task",
            MaaFWPluginAutoProxyTask=object,
        ),
        "automas_script_maafw.schema": _module(
            "automas_script_maafw.schema",
            build_source_config=lambda _payload: None,
        ),
    }
    module_name = "automas_script_maafw._config_isolation_contract_adapter"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_ROOT / "adapter.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


class ScriptMaaFWConfigIsolationContractTest(unittest.TestCase):
    def test_first_save_is_read_on_first_run_without_touching_other_script(self) -> None:
        module = _load_adapter_module()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_a = root / "project-a"
            project_b = root / "project-b"
            project_a.mkdir()
            project_b.mkdir()
            default_a = self._form("A default", "", project_label="default-a")
            configured_a = self._form("A configured", str(project_a), project_label="a")
            configured_b = self._form("B configured", str(project_b), project_label="b")
            backend = _ConfigBackend({"script-a": default_a, "script-b": configured_b})
            untouched_b = copy.deepcopy(backend.records["script-b"])

            backend.save_form("script-a", configured_a)
            runtime_a = _Runtime(backend, "script-a")
            hooks = module.MaaFWAdapterHooks()

            self.assertEqual(asyncio.run(hooks.check(runtime_a)), "Pass")
            asyncio.run(hooks.prepare(runtime_a))

            self.assertEqual(runtime_a.script_config.get("Info", "Path"), str(project_a))
            self.assertEqual(runtime_a.script_config.get("Info", "ProjectLabel"), "a")
            self.assertEqual(backend.save_calls, ["script-a"])
            self.assertEqual(backend.read_calls, ["script-a", "script-a"])
            self.assertEqual(backend.records["script-b"], untouched_b)

            runtime_b = _Runtime(backend, "script-b")
            asyncio.run(hooks.prepare(runtime_b))
            self.assertEqual(runtime_b.script_config.get("Info", "Path"), str(project_b))
            self.assertEqual(runtime_b.script_config.get("Info", "ProjectLabel"), "b")
            self.assertEqual(backend.records["script-b"], untouched_b)

    def test_busy_external_project_path_skips_update_before_storage_mutation(
        self,
    ) -> None:
        module = _load_adapter_module()
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_path = Path(temporary_directory) / "project"
            project_path.mkdir()
            backend = _ConfigBackend(
                {
                    "script-a": self._form(
                        "A",
                        str(project_path),
                        project_label="a",
                    )
                }
            )
            runtime = _Runtime(backend, "script-a")
            script_config = backend.load_model("script-a")
            script_config.payload["Update"]["IfAutoUpdate"] = True
            hooks = module.MaaFWAdapterHooks()

            with mock.patch.object(
                module,
                "try_reserve_project_path",
                new=mock.AsyncMock(return_value=None),
            ):
                asyncio.run(
                    hooks._update_project_before_run(
                        runtime,
                        script_config,
                    )
                )

            self.assertIn(
                "正在运行或更新",
                "".join(runtime.extra["maafw_project_update_logs"]),
            )

    def test_post_update_prewarm_failure_is_reported_as_warning(self) -> None:
        module = _load_adapter_module()

        class _Updater:
            async def update_if_needed(self, *_args: Any, **_kwargs: Any) -> Any:
                return SimpleNamespace(updated=True)

        with tempfile.TemporaryDirectory() as temporary_directory:
            project_path = Path(temporary_directory) / "project"
            project_path.mkdir()
            backend = _ConfigBackend(
                {
                    "script-a": self._form(
                        "A",
                        str(project_path),
                        project_label="a",
                    )
                }
            )
            runtime = _Runtime(backend, "script-a")
            script_config = backend.load_model("script-a")
            script_config.payload["Update"]["IfAutoUpdate"] = True
            hooks = module.MaaFWAdapterHooks()
            module.MaaFWProjectUpdateService = _Updater
            module.Config = SimpleNamespace(
                get=lambda *_args: "",
                proxy=None,
            )

            with (
                mock.patch.object(
                    module,
                    "try_reserve_project_path",
                    new=mock.AsyncMock(return_value="reserved"),
                ),
                mock.patch.object(
                    module,
                    "release_project_path",
                    new=mock.AsyncMock(),
                ),
                mock.patch.object(
                    module,
                    "_prepare_maafw_agent_python_envs",
                    side_effect=RuntimeError("prewarm boom"),
                ),
            ):
                asyncio.run(
                    hooks._update_project_before_run(
                        runtime,
                        script_config,
                    )
                )

            logs = "".join(runtime.extra["maafw_project_update_logs"])
            self.assertIn("项目资源已更新，但运行环境预热未完成", logs)
            self.assertIn("prewarm boom", logs)
            self.assertNotIn("项目更新失败，继续使用当前目录", logs)

    @staticmethod
    def _form(name: str, path: str, *, project_label: str) -> dict[str, Any]:
        return {
            "Info": {
                "Name": name,
                "ProjectLabel": project_label,
                "Path": path,
            },
            "Emulator": {"Id": "-", "Index": "-"},
            "Update": {"IfAutoUpdate": False},
            "Run": {"RunTimeLimit": 30},
        }


if __name__ == "__main__":
    unittest.main()
