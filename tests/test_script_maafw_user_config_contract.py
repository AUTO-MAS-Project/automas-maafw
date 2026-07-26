from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "packages" / "automas_script_maafw" / "src" / "automas_script_maafw"


class ScriptMaaFWUserConfigContractTest(unittest.TestCase):
    def test_runtime_uses_native_script_config_store(self) -> None:
        adapter_source = (MODULE_ROOT / "adapter.py").read_text(encoding="utf-8")
        runner_source = (MODULE_ROOT / "runner_task.py").read_text(encoding="utf-8")

        for source in (adapter_source, runner_source):
            self.assertNotIn("app.models.ConfigBase", source)
            self.assertNotIn("app.models.config", source)
            self.assertNotIn("MultipleConfig", source)

        self.assertIn("await runtime.build_script_model()", adapter_source)
        self.assertIn("await runtime.storage.load_user_collection()", adapter_source)

    def test_user_schema_has_no_controller_or_resource_overrides(self) -> None:
        source = (MODULE_ROOT / "schema.py").read_text(encoding="utf-8")
        user_schema_source = source[source.index("USER_GROUPS =") : source.index("def build_source_config")]

        self.assertNotIn('PluginField.string("Controller"', user_schema_source)
        self.assertNotIn('PluginField.string("Resource"', user_schema_source)
        self.assertNotIn('PluginField.group(\n        "Device"', user_schema_source)

    def test_runner_uses_script_level_controller_configuration_only(self) -> None:
        tree = ast.parse((MODULE_ROOT / "runner_task.py").read_text(encoding="utf-8"))
        methods = {
            node.name: ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        for method_name in (
            "_select_controller_name",
            "_select_resource_name",
            "_resolve_window_handle",
            "_wait_for_desktop_game_ready",
        ):
            self.assertNotIn("self.cur_user_config.get", methods[method_name])


if __name__ == "__main__":
    unittest.main()
