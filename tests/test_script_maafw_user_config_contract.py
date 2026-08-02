from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "packages" / "automas_script_maafw" / "src" / "automas_script_maafw"


class ScriptMaaFWUserConfigContractTest(unittest.TestCase):
    @staticmethod
    def _load_source_config_builder():
        tree = ast.parse((MODULE_ROOT / "schema.py").read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "build_source_config"
        )
        namespace: dict[str, object] = {"Any": object}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                str(MODULE_ROOT / "schema.py"),
                "exec",
            ),
            namespace,
        )
        return namespace["build_source_config"]

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

    def test_automatic_update_source_keeps_github_fallback_hints(self) -> None:
        build_source_config = self._load_source_config_builder()
        result = build_source_config(
            {
                "Update": {
                    "Source": "",
                    "Channel": "beta",
                    "MirrorChyanCDK": "local-cdk",
                    "GitHubRepo": "owner/project",
                    "GitHubTag": "v1.2.3",
                    "GitHubAssetPattern": r"win-x64\.zip$",
                }
            }
        )

        self.assertEqual(
            result,
            {
                "cdk": "local-cdk",
                "channel": "beta",
                "repo": "owner/project",
                "tag": "v1.2.3",
                "asset_pattern": r"win-x64\.zip$",
            },
        )
        self.assertNotIn("source", result)
        self.assertIsNone(build_source_config({"Update": {"Source": ""}}))


if __name__ == "__main__":
    unittest.main()
