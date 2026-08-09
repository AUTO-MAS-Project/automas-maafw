from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for package_name in (
    "automas_maafw_interface",
    "automas_maafw_agent_env",
    "automas_maafw_runtime_pool",
    "automas_maafw_runner",
):
    package_src = ROOT / "packages" / package_name / "src"
    if str(package_src) not in sys.path:
        sys.path.insert(0, str(package_src))

from automas_maafw_interface.loader import (  # noqa: E402
    MaaFWInterfaceLoadError,
    load_interface_model,
)
from automas_maafw_interface.models import MaaFWInterface  # noqa: E402
from automas_maafw_interface.preview import build_interface_preview_data  # noqa: E402
from automas_maafw_interface.task_config import (  # noqa: E402
    build_interface_preset_snapshot,
    normalize_task_options_by_task,
)
from automas_maafw_runner.hotkey import MaaFWHotkeyError, resolve_hotkey  # noqa: E402
from automas_maafw_runner.pipeline_override import (  # noqa: E402
    MaaFWPipelineOverrideBuilder,
)


def _interface_payload(*, controller_type: str = "Win32") -> dict:
    return {
        "interface_version": 2,
        "name": "pi-28-contract",
        "controller": [
            {
                "name": "main",
                "type": controller_type,
            }
        ],
        "resource": [{"name": "default", "path": ["resource"]}],
        "task": [
            {
                "name": "run",
                "entry": "Run",
                "option": ["battle_keys"],
            }
        ],
        "option": {
            "battle_keys": {
                "type": "hotkey",
                "hotkeys": [
                    {
                        "name": "Combo",
                        "label": "组合键",
                        "default": "Ctrl+Shift+A",
                    }
                ],
                "pipeline_override": {
                    "CtrlDown": {"key": "{Combo}.modifier1"},
                    "ShiftDown": {"key": "{Combo}.modifier2"},
                    "Primary": {"key": "{Combo}"},
                },
            }
        },
        "setting": [
            {
                "name": "controls",
                "label": "控制",
                "option": ["battle_keys"],
            }
        ],
        "preset": [
            {
                "name": "default",
                "task": [
                    {
                        "name": "run",
                        "option": {
                            "battle_keys": {"Combo": "Alt+F4"},
                        },
                    }
                ],
            }
        ],
    }


class MaaFWProjectInterface28ContractTest(unittest.TestCase):
    def test_setting_imports_append_in_protocol_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            payload = _interface_payload()
            payload["import"] = ["extra.json"]
            (root / "interface.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "extra.json").write_text(
                json.dumps(
                    {
                        "setting": [
                            {
                                "name": "advanced",
                                "option": ["battle_keys"],
                                "default_expand": False,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            interface = load_interface_model(root)

            self.assertEqual(
                [setting.name for setting in interface.setting or []],
                ["controls", "advanced"],
            )
            self.assertEqual(interface.option["battle_keys"].type, "hotkey")

    def test_setting_rejects_unknown_option_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            payload = _interface_payload()
            payload["setting"][0]["option"] = ["missing"]
            (root / "interface.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                MaaFWInterfaceLoadError,
                "setting controls 引用了不存在的选项",
            ):
                load_interface_model(root)

    def test_preview_preserves_settings_and_hotkey_fields(self) -> None:
        interface = MaaFWInterface.model_validate(_interface_payload())

        preview = build_interface_preview_data(ROOT, interface)

        self.assertEqual(preview.settings[0]["name"], "controls")
        self.assertEqual(preview.settings[0]["option"], ["battle_keys"])
        hotkeys = next(
            option["hotkeys"]
            for option in preview.options
            if option["name"] == "battle_keys"
        )
        self.assertEqual(hotkeys[0]["default"], "Ctrl+Shift+A")

    def test_hotkey_defaults_and_presets_use_readable_strings(self) -> None:
        interface = MaaFWInterface.model_validate(_interface_payload())

        normalized = normalize_task_options_by_task(None, ["run"], interface)
        preset = build_interface_preset_snapshot(interface, interface.preset[0])

        self.assertEqual(
            normalized["run"]["battle_keys"],
            {"Combo": "Ctrl+Shift+A"},
        )
        self.assertEqual(
            preset.taskOptions["run"]["battle_keys"],
            {"Combo": "Alt+F4"},
        )

    def test_win32_hotkey_pipeline_uses_virtual_key_integers(self) -> None:
        interface = MaaFWInterface.model_validate(_interface_payload())
        builder = MaaFWPipelineOverrideBuilder(
            interface,
            controller_names={"main"},
            resource_name="default",
        )

        override = builder.build_task_pipeline_override(
            "run",
            {"battle_keys": {"Combo": "Ctrl+Shift+A"}},
        )

        self.assertEqual(override["CtrlDown"]["key"], 0x11)
        self.assertEqual(override["ShiftDown"]["key"], 0x10)
        self.assertEqual(override["Primary"]["key"], 0x41)

    def test_adb_hotkey_pipeline_uses_android_keyevent_codes(self) -> None:
        interface = MaaFWInterface.model_validate(
            _interface_payload(controller_type="Adb")
        )
        builder = MaaFWPipelineOverrideBuilder(
            interface,
            controller_names={"main"},
            resource_name="default",
        )

        override = builder.build_task_pipeline_override(
            "run",
            {"battle_keys": {"Combo": "Ctrl+Shift+A"}},
        )

        self.assertEqual(override["CtrlDown"]["key"], 113)
        self.assertEqual(override["ShiftDown"]["key"], 59)
        self.assertEqual(override["Primary"]["key"], 29)

    def test_missing_referenced_modifier_is_rejected(self) -> None:
        interface = MaaFWInterface.model_validate(_interface_payload())
        builder = MaaFWPipelineOverrideBuilder(
            interface,
            controller_names={"main"},
            resource_name="default",
        )

        with self.assertRaisesRegex(MaaFWHotkeyError, "不包含所需修饰键"):
            builder.build_task_pipeline_override(
                "run",
                {"battle_keys": {"Combo": "A"}},
            )

    def test_common_keyboard_event_aliases_are_supported(self) -> None:
        resolved = resolve_hotkey("Control+ArrowUp", "Win32")

        self.assertEqual(resolved.modifiers, (0x11,))
        self.assertEqual(resolved.primary, 0x26)


if __name__ == "__main__":
    unittest.main()
