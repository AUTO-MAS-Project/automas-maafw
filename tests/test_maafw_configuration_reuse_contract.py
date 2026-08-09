from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SOURCE = ROOT / "packages" / "automas_script_maafw" / "src"
MODULE_ROOT = PACKAGE_SOURCE / "automas_script_maafw"
if str(PACKAGE_SOURCE) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SOURCE))

from automas_script_maafw.configuration_reuse import (  # noqa: E402
    MaaFWConfigurationReuseError,
    discover_configuration_sources,
    plan_external_configuration_import,
    plan_internal_user_copy,
    public_configuration_plan,
    stable_json_hash,
    user_records_hash,
)


INTERFACE = {
    "interface_version": 2,
    "name": "contract",
    "controller": [{"name": "adb", "type": "Adb"}],
    "resource": [{"name": "global", "path": ["resource"]}],
    "task": [
        {
            "name": "Daily",
            "label": "每日任务",
            "entry": "DailyEntry",
            "option": ["Mode", "Enabled", "Inputs"],
        },
        {"name": "Awards", "label": "领取奖励", "entry": "Awards"},
    ],
    "option": {
        "Mode": {
            "type": "select",
            "label": "模式",
            "cases": [{"name": "A"}, {"name": "B"}],
        },
        "Enabled": {
            "type": "switch",
            "cases": [{"name": "Yes"}, {"name": "No"}],
        },
        "Inputs": {
            "type": "input",
            "inputs": [{"name": "count"}, {"name": "label"}],
        },
    },
}


class MaaFWConfigurationReuseMappingTest(unittest.TestCase):
    def test_direct_multi_config_file_expands_listed_configs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_root = Path(temporary_directory) / "config"
            configs = config_root / "configs"
            configs.mkdir(parents=True)
            source = configs / "c_first.json"
            source.write_text(
                json.dumps(
                    {
                        "InstanceName": "配置一",
                        "TaskItems": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            multi_config = config_root / "multi_config.json"
            multi_config.write_text(
                json.dumps({"config_list": ["first"]}),
                encoding="utf-8",
            )

            sources = discover_configuration_sources(multi_config)

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["label"], "配置一")
            self.assertEqual(Path(sources[0]["path"]), source.resolve())

    def test_mfaa_v1_discovers_and_maps_script_and_first_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_dir = root / "config" / "instances"
            config_dir.mkdir(parents=True)
            source = config_dir / "first.json"
            source.write_text(
                json.dumps(
                    {
                        "InstanceName": "配置一",
                        "SoftwarePath": "C:/Games/Demo/game.exe",
                        "WaitSoftwareTime": 45,
                        "CurrentControllerName": "adb",
                        "Resource": "global",
                        "AdbDevice": {
                            "Name": "模拟器",
                            "AdbPath": "C:/adb.exe",
                            "AdbSerial": "127.0.0.1:5555",
                            "ScreencapMethods": 8,
                            # MFAAvalonia persists MaaFramework uint64 masks;
                            # this is MaaAdbInputMethodEnum.Default (-9).
                            "InputMethods": 18446744073709551607,
                        },
                        "TaskItems": [
                            {
                                "name": "每日任务",
                                "entry": "DailyEntry",
                                "default_check": True,
                                "option": [
                                    {"name": "模式", "index": 1},
                                    {"name": "Enabled", "index": 0},
                                    {
                                        "name": "Inputs",
                                        "data": {"count": "3", "label": "demo"},
                                    },
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            sources = discover_configuration_sources(root)
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["kind"], "mfaa-v1")
            plan = plan_external_configuration_import(
                sources[0],
                INTERFACE,
                target="project-and-first-user",
            )

            self.assertEqual(plan["scriptTargetConfig"]["Info"]["Controller"], "adb")
            self.assertEqual(plan["scriptTargetConfig"]["Info"]["Resource"], "global")
            self.assertEqual(
                plan["scriptTargetConfig"]["Game"]["Path"],
                "C:/Games/Demo/game.exe",
            )
            self.assertEqual(
                plan["scriptTargetConfig"]["Device"]["AdbInputMethods"],
                -9,
            )
            self.assertEqual(plan["summary"]["enabledTaskCount"], 1)
            snapshot = json.loads(
                plan["userTargetConfigs"][0]["Task"]["TaskSnapshot"]
            )
            self.assertEqual(snapshot["taskOrder"], ["Daily"])
            self.assertEqual(
                snapshot["taskOptions"]["Daily"],
                {
                    "Mode": "B",
                    "Enabled": "Yes",
                    "Inputs": {"count": "3", "label": "demo"},
                },
            )
            self.assertEqual(plan["manualActions"][0]["kind"], "emulator-selection")

    def test_mfaa_v2_maps_selected_android_controller_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "config.json"
            source.write_text(
                json.dumps(
                    {
                        "name": "安卓配置",
                        "tasks": [
                            {
                                "name": "控制器",
                                "is_checked": True,
                                "task_option": {
                                    "controller_type": "adb",
                                    "adb": {
                                        "adb_path": "C:/platform-tools/adb.exe",
                                        "address": "127.0.0.1:5555",
                                        "screencap_methods": 8,
                                        "input_methods": 4,
                                        "emulator_path": "C:/Emulator/player.exe",
                                        "device_index": 2,
                                        "name": "外部模拟器",
                                    },
                                },
                            },
                            {
                                "name": "资源",
                                "is_checked": True,
                                "task_option": {"resource": "global"},
                            },
                            {
                                "name": "每日任务",
                                "is_checked": True,
                                "task_option": {"模式": "A"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            discovered = discover_configuration_sources(source)[0]
            self.assertTrue(discovered["summary"]["hasAdbDevice"])
            plan = plan_external_configuration_import(
                discovered,
                INTERFACE,
                target="project-and-first-user",
            )

            self.assertEqual(plan["scriptTargetConfig"]["Info"]["Controller"], "adb")
            self.assertEqual(plan["scriptTargetConfig"]["Info"]["Resource"], "global")
            self.assertEqual(
                plan["scriptTargetConfig"]["Device"],
                {
                    "AdbPath": "C:/platform-tools/adb.exe",
                    "AdbAddress": "127.0.0.1:5555",
                    "AdbScreencapMethods": 8,
                    "AdbInputMethods": 4,
                },
            )
            self.assertTrue(
                any(
                    item["kind"] == "emulator-selection"
                    for item in plan["manualActions"]
                )
            )

    def test_mfaa_v2_maps_selected_desktop_game_and_control_methods(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "config.json"
            source.write_text(
                json.dumps(
                    {
                        "name": "桌面配置",
                        "tasks": [
                            {
                                "name": "控制器",
                                "is_checked": True,
                                "task_option": {
                                    "controller_type": "desktop",
                                    "desktop": {
                                        "program_path": "C:/Games/Demo/game.exe",
                                        "program_params": "--server demo",
                                        "wait_time": 30,
                                        "hwnd": "123456",
                                        "window_name": "Demo",
                                        "win32_screencap_methods": 8,
                                        "mouse_input_methods": 4,
                                        "keyboard_input_methods": 2,
                                    },
                                },
                            },
                            {
                                "name": "资源",
                                "is_checked": True,
                                "task_option": {"resource": "global"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            discovered = discover_configuration_sources(source)[0]
            self.assertTrue(discovered["summary"]["hasGamePath"])
            plan = plan_external_configuration_import(
                discovered,
                INTERFACE,
                target="project-and-first-user",
            )

            self.assertEqual(
                plan["scriptTargetConfig"]["Game"],
                {
                    "Path": "C:/Games/Demo/game.exe",
                    "Arguments": "--server demo",
                    "WaitTime": 30,
                },
            )
            self.assertEqual(
                plan["scriptTargetConfig"]["Device"],
                {
                    "Win32ScreencapMethod": 8,
                    "Win32MouseMethod": 4,
                    "Win32KeyboardMethod": 2,
                },
            )
            self.assertTrue(
                any(item["kind"] == "desktop-window" for item in plan["manualActions"])
            )
            self.assertNotIn("HWnd", plan["scriptTargetConfig"]["Device"])

    def test_mxu_instances_and_new_user_preserve_script_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_dir = root / "config"
            config_dir.mkdir()
            source = config_dir / "mxu-demo.json"
            source.write_text(
                json.dumps(
                    {
                        "version": "1",
                        "instances": [
                            {
                                "id": "one",
                                "name": "用户一",
                                "controllerName": "adb",
                                "resourceName": "global",
                                "savedDevice": {"adbDeviceName": "127.0.0.1:5555"},
                                "tasks": [
                                    {
                                        "taskName": "Daily",
                                        "enabled": True,
                                        "optionValues": {
                                            "Mode": {"type": "select", "caseName": "A"},
                                            "Enabled": {"type": "switch", "value": True},
                                            "Inputs": {
                                                "type": "input",
                                                "values": ["9", "copied"],
                                            },
                                        },
                                    }
                                ],
                                "preActions": [],
                            },
                            {
                                "id": "two",
                                "name": "用户二",
                                "controllerName": "adb",
                                "resourceName": "global",
                                "savedDevice": {},
                                "tasks": [],
                                "preActions": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            sources = discover_configuration_sources(root)
            self.assertEqual([item["label"] for item in sources], ["用户一", "用户二"])
            plan = plan_external_configuration_import(
                sources[0],
                INTERFACE,
                target="new-user",
            )

            self.assertEqual(plan["scriptTargetConfig"], {})
            self.assertTrue(
                any(
                    item["kind"] == "script-fields-preserved"
                    for item in plan["manualActions"]
                )
            )
            snapshot = json.loads(
                plan["userTargetConfigs"][0]["Task"]["TaskSnapshot"]
            )
            self.assertEqual(snapshot["taskOptions"]["Daily"]["Enabled"], "Yes")
            self.assertEqual(
                snapshot["taskOptions"]["Daily"]["Inputs"],
                {"count": "9", "label": "copied"},
            )

    def test_fingerprint_change_rejects_stale_external_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "config.json"
            source.write_text(
                json.dumps({"InstanceName": "一", "TaskItems": []}),
                encoding="utf-8",
            )
            discovered = discover_configuration_sources(source)[0]
            source.write_text(
                json.dumps({"InstanceName": "二", "TaskItems": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MaaFWConfigurationReuseError,
                "预览后已发生变化",
            ):
                plan_external_configuration_import(
                    discovered,
                    INTERFACE,
                    target="new-user",
                )

    def test_internal_copy_resets_runtime_state_and_hides_target_payload(self) -> None:
        plan = plan_internal_user_copy(
            {
                "id": "source",
                "name": "主账号",
                "config": {
                    "Info": {"Name": "主账号", "Status": True},
                    "Task": {"TaskSnapshot": '{"taskOrder":["Daily"]}'},
                    "Data": {"ProxyTimes": 99, "LastProxyStatus": "running"},
                    "Notify": {"Enabled": True},
                    "ManagedUpgrade": {"PendingPlan": {"secret": True}},
                },
            }
        )
        copied = plan["userTargetConfigs"][0]
        self.assertEqual(copied["Info"]["Name"], "主账号 - 副本")
        self.assertEqual(copied["Data"]["ProxyTimes"], 0)
        self.assertNotIn("ManagedUpgrade", copied)
        plan["planId"] = "plan"
        public = public_configuration_plan(plan)
        self.assertNotIn("userTargetConfigs", public)
        self.assertNotIn("scriptTargetConfig", public)


class MaaFWConfigurationReuseControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.script_id = str(uuid.uuid4())
        self.source_user_id = str(uuid.uuid4())
        self.config = self._fake_config()
        self.controller_module = self._load_controller(self.config)
        self.controller = self.controller_module.MaaFWConfigurationReuseController(
            SimpleNamespace(get=lambda _key: None, server=SimpleNamespace(http=lambda *_a, **_k: None)),
            SimpleNamespace(get_project_pack=lambda _key: None),
        )

    def test_plugin_container_record_resolves_authoritative_adapter_type_key(self) -> None:
        record_type = self.controller_module._record_type

        self.assertEqual(
            record_type({"type": "Plugin", "PluginTypeKey": "M9A"}),
            "M9A",
        )
        self.assertEqual(
            record_type(
                {
                    "type": "PluginScriptConfig",
                    "config": {"Meta": {"PluginTypeKey": "M9A"}},
                }
            ),
            "M9A",
        )
        self.assertEqual(record_type({"type": "M9A"}), "M9A")

    def test_m9a_host_provider_falls_back_when_project_pack_registry_is_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.config.scripts[self.script_id] = {
                "id": self.script_id,
                "type": "M9A",
                "name": "M9A",
                "config": {"Info": {"Name": "M9A", "Path": str(root)}},
            }

            interface_path = root / "interface.json"
            interface_path.write_text(
                json.dumps(INTERFACE, ensure_ascii=False),
                encoding="utf-8",
            )
            source = root / "config" / "instances" / "profile.json"
            source.parent.mkdir(parents=True)
            source.write_text(
                json.dumps(
                    {
                        "InstanceName": "M9A 配置",
                        "TaskItems": [],
                        "AdbDevice": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class InterfaceService:
                @staticmethod
                def load(project_path):
                    return json.loads(
                        (Path(project_path) / "interface.json").read_text(
                            encoding="utf-8"
                        )
                    )

            self.controller.ctx = SimpleNamespace(
                get=lambda key: (
                    InterfaceService()
                    if key == self.controller_module.INTERFACE_SERVICE
                    else None
                ),
                server=SimpleNamespace(http=lambda *_a, **_k: None),
            )
            # Model the real stale-HMR state: the MaaFW-local project-pack
            # registry is empty, while the host provider that decoded the live
            # ScriptRecord still explicitly declares framework=maafw.
            self.controller.registry = SimpleNamespace(
                get_project_pack=lambda _key: None
            )

            class HostScriptTypeRegistry:
                @staticmethod
                def get(key):
                    if key != "M9A":
                        raise KeyError(key)
                    return SimpleNamespace(metadata={"framework": "maafw"})

            host_registry = HostScriptTypeRegistry()
            script_types = types.ModuleType("app.core.script_types")
            script_types.script_type_registry = host_registry

            with patch.dict(
                sys.modules,
                {"app.core.script_types": script_types},
            ):
                for selected_path in (root, source):
                    discovered = asyncio.run(
                        self.controller._discover_sources(
                            {
                                "scriptId": self.script_id,
                                "sourcePath": str(selected_path),
                            }
                        )
                    )
                    self.assertEqual(discovered["count"], 1)
                    self.assertEqual(discovered["sources"][0]["kind"], "mfaa-v1")
                    self.assertEqual(
                        discovered["sources"][0]["label"],
                        "M9A 配置",
                    )

                planned = asyncio.run(
                    self.controller._plan_external(
                        {
                            "scriptId": self.script_id,
                            "source": discovered["sources"][0],
                            "target": "new-user",
                        }
                    )
                )

            self.assertEqual(planned["target"], "new-user")
            self.assertEqual(planned["preview"]["format"], "mfaa-v1")

    def test_non_maafw_host_provider_does_not_bypass_missing_project_pack(self) -> None:
        self.config.scripts[self.script_id]["type"] = "OtherAdapter"
        host_registry = SimpleNamespace(
            get=lambda _key: SimpleNamespace(
                metadata={"framework": "script_adapter"}
            )
        )
        script_types = types.ModuleType("app.core.script_types")
        script_types.script_type_registry = host_registry

        with patch.dict(sys.modules, {"app.core.script_types": script_types}):
            with self.assertRaisesRegex(
                MaaFWConfigurationReuseError,
                "不是 MaaFW 项目",
            ):
                asyncio.run(
                    self.controller._discover_sources(
                        {
                            "scriptId": self.script_id,
                            "sourcePath": "C:/external",
                        }
                    )
                )

    def test_internal_copy_is_planned_then_applied_in_one_host_transaction(self) -> None:
        planned = asyncio.run(
            self.controller._plan_copy(
                {"scriptId": self.script_id, "sourceUserId": self.source_user_id}
            )
        )
        self.assertNotIn("userTargetConfigs", planned)
        applied = asyncio.run(
            self.controller._apply_plan(
                {"scriptId": self.script_id, "planId": planned["planId"]}
            )
        )

        self.assertTrue(applied["applied"])
        created_id = applied["createdUser"]["id"]
        self.assertNotEqual(created_id, self.source_user_id)
        self.assertEqual(
            self.config.users[created_id]["config"]["Info"]["Name"],
            "来源用户 - 副本",
        )
        self.assertEqual(self.config.users[created_id]["config"]["Data"]["ProxyTimes"], 0)
        self.assertEqual(self.config.transactions, [self.script_id])
        self.assertEqual(
            self.config.events[:2],
            [("add_user", created_id), ("update_user", created_id)],
        )

    def test_changed_source_user_rejects_old_copy_plan(self) -> None:
        planned = asyncio.run(
            self.controller._plan_copy(
                {"scriptId": self.script_id, "sourceUserId": self.source_user_id}
            )
        )
        self.config.users[self.source_user_id]["config"]["Info"]["Name"] = "已变化"
        with self.assertRaisesRegex(MaaFWConfigurationReuseError, "用户集合"):
            asyncio.run(
                self.controller._apply_plan(
                    {"scriptId": self.script_id, "planId": planned["planId"]}
                )
            )
        self.assertEqual(set(self.config.users), {self.source_user_id})

    def test_failed_user_write_removes_half_created_user(self) -> None:
        planned = asyncio.run(
            self.controller._plan_copy(
                {"scriptId": self.script_id, "sourceUserId": self.source_user_id}
            )
        )
        self.config.fail_user_write = True
        with self.assertRaisesRegex(MaaFWConfigurationReuseError, "已尝试恢复"):
            asyncio.run(
                self.controller._apply_plan(
                    {"scriptId": self.script_id, "planId": planned["planId"]}
                )
            )
        self.assertEqual(set(self.config.users), {self.source_user_id})
        self.assertEqual(self.config.events[-1][0], "del_user")

    def test_configuration_boundary_rejects_truthy_script_updated(self) -> None:
        with self.assertRaisesRegex(
            MaaFWConfigurationReuseError,
            "scriptUpdated.*boolean",
        ):
            self.controller_module._strict_bool("true", "scriptUpdated")

    def _fake_config(self):
        script_id = self.script_id
        source_user_id = self.source_user_id

        class FakeConfig:
            scripts = {
                script_id: {
                    "id": script_id,
                    "type": "MaaFW",
                    "config": {"Info": {"Name": "脚本", "Path": "C:/project"}},
                }
            }
            users = {
                source_user_id: {
                    "id": source_user_id,
                    "script_id": script_id,
                    "type": "MaaFW",
                    "name": "来源用户",
                    "config": {
                        "Info": {"Name": "来源用户", "Status": True},
                        "Task": {"SelectedPreset": "", "TaskSnapshot": "{}"},
                        "Data": {"ProxyTimes": 3},
                        "Notify": {"Enabled": False},
                    },
                }
            }
            transactions: list[str] = []
            events: list[tuple[str, str]] = []
            fail_user_write = False

            @classmethod
            async def get_script_records(cls, requested_id):
                record = cls.scripts.get(requested_id)
                return [copy.deepcopy(record)] if record else []

            @classmethod
            async def get_user_records(cls, requested_script_id, requested_user_id=None):
                records = [
                    value
                    for value in cls.users.values()
                    if value["script_id"] == requested_script_id
                    and (requested_user_id is None or value["id"] == requested_user_id)
                ]
                return copy.deepcopy(records)

            @classmethod
            @asynccontextmanager
            async def script_config_transaction(cls, requested_id, *, owner):
                self.assertTrue(owner.startswith("maafw-config-reuse:"))
                cls.transactions.append(requested_id)
                yield owner

            @classmethod
            async def add_user(cls, requested_script_id):
                new_id = str(uuid.uuid4())
                cls.users[new_id] = {
                    "id": new_id,
                    "script_id": requested_script_id,
                    "type": "MaaFW",
                    "name": "新用户",
                    "config": {},
                }
                cls.events.append(("add_user", new_id))
                return uuid.UUID(new_id), object()

            @classmethod
            async def update_user(cls, _requested_script_id, user_id, update):
                cls.events.append(("update_user", user_id))
                if cls.fail_user_write:
                    raise RuntimeError("injected user write failure")
                cls.users[user_id]["config"] = copy.deepcopy(update)
                cls.users[user_id]["name"] = str(
                    update.get("Info", {}).get("Name") or "新用户"
                )

            @classmethod
            async def update_script(cls, requested_id, update):
                cls.events.append(("update_script", requested_id))
                cls.scripts[requested_id]["config"] = copy.deepcopy(update)

            @classmethod
            async def del_user(cls, _requested_script_id, user_id):
                cls.events.append(("del_user", user_id))
                cls.users.pop(user_id, None)

        return FakeConfig

    @staticmethod
    def _load_controller(config):
        app = types.ModuleType("app")
        app.__path__ = []  # type: ignore[attr-defined]
        app_core = types.ModuleType("app.core")
        app_core.Config = config
        app_plugins = types.ModuleType("app.plugins")
        app_plugins.PluginHttpRequest = object
        app.core = app_core
        app.plugins = app_plugins
        stubs = {"app": app, "app.core": app_core, "app.plugins": app_plugins}
        module_name = f"automas_script_maafw._configuration_controller_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(
            module_name,
            MODULE_ROOT / "configuration_controller.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "automas_script_maafw"
        previous = {name: sys.modules.get(name) for name in stubs}
        try:
            sys.modules.update(stubs)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
        return module


if __name__ == "__main__":
    unittest.main()
