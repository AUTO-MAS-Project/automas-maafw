from __future__ import annotations

import ast
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "packages" / "automas_script_maafw"
MODULE_ROOT = PACKAGE_ROOT / "src" / "automas_script_maafw"
ICON_PATH = "automas_script_maafw:assets/maafw.png"


class ScriptMaaFWIconContractTest(unittest.TestCase):
    def test_adapter_declares_packaged_icon(self) -> None:
        tree = ast.parse((MODULE_ROOT / "plugin.py").read_text(encoding="utf-8"))
        adapter_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ScriptAdapterDefinition"
        ]
        maafw_adapter = next(
            call
            for call in adapter_calls
            if self._keyword_value(call, "type_key") == "MaaFW"
        )

        self.assertEqual(self._keyword_value(maafw_adapter, "icon_path"), ICON_PATH)
        self.assertEqual(
            self._keyword_value(maafw_adapter, "metadata")["create_group"],
            "general",
        )

    def test_icon_is_included_as_package_data(self) -> None:
        pyproject = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = pyproject["tool"]["setuptools"]["package-data"]["automas_script_maafw"]

        self.assertIn("assets/*.png", package_data)
        self.assertTrue((MODULE_ROOT / "assets" / "maafw.png").is_file())

    @staticmethod
    def _keyword_value(call: ast.Call, name: str) -> object:
        keyword = next(item for item in call.keywords if item.arg == name)
        return ast.literal_eval(keyword.value)


if __name__ == "__main__":
    unittest.main()
