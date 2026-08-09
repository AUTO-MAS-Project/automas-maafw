from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

from pydantic import BaseModel


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

NON_SCRIPT_SCHEMA_PACKAGES = (
    "automas_maafw_agent_env",
    "automas_maafw_controller_adb",
    "automas_maafw_controller_win32",
    "automas_maafw_interface",
    "automas_maafw_project_update",
    "automas_maafw_runner",
)

ROOT_CONFIG_SCHEMA_PACKAGES = (
    "automas_maafw_project_store",
    "automas_maafw_runtime_pool",
)

SCRIPT_SCHEMA_PACKAGES = (
    "automas_script_maafw",
    "automas_script_maafw_managed",
)


class MaaFWPluginSchemaContractTest(unittest.TestCase):
    def test_non_script_plugins_expose_empty_config_model(self) -> None:
        for package_name in NON_SCRIPT_SCHEMA_PACKAGES:
            with self.subTest(package_name=package_name):
                module = importlib.import_module(f"{package_name}.schema")
                config_model = getattr(module, "Config", None)

                self.assertIsInstance(config_model, type)
                self.assertTrue(issubclass(config_model, BaseModel))
                self.assertEqual(config_model.model_json_schema()["properties"], {})
                self.assertEqual(config_model.model_validate({}).model_dump(), {})

    def test_script_plugin_schema_declares_host_config_model(self) -> None:
        for package_name in SCRIPT_SCHEMA_PACKAGES:
            with self.subTest(package_name=package_name):
                schema_path = (
                    REPOSITORY_ROOT
                    / "packages"
                    / package_name
                    / "src"
                    / package_name
                    / "schema.py"
                )
                tree = ast.parse(schema_path.read_text(encoding="utf-8"))
                config_classes = [
                    node
                    for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "Config"
                ]

                self.assertEqual(len(config_classes), 1)
                self.assertTrue(
                    any(
                        isinstance(base, ast.Name) and base.id == "BaseModel"
                        for base in config_classes[0].bases
                    )
                )

    def test_storage_plugins_expose_restart_scoped_root_config(self) -> None:
        for package_name in ROOT_CONFIG_SCHEMA_PACKAGES:
            with self.subTest(package_name=package_name):
                module = importlib.import_module(f"{package_name}.schema")
                config_model = getattr(module, "Config")
                properties = config_model.model_json_schema()["properties"]

                expected = (
                    {"Root", "RunRoot"}
                    if package_name == "automas_maafw_project_store"
                    else {"Root"}
                )
                self.assertEqual(set(properties), expected)
                self.assertEqual(properties["Root"]["default"], "")
                for field_name in expected:
                    field = config_model.model_fields[field_name]
                    self.assertEqual(field.json_schema_extra["type"], "folder")
                    self.assertEqual(field.json_schema_extra["path_kind"], "folder")
                    self.assertTrue(
                        field.json_schema_extra["x-auto-mas-plugin-field"]
                    )
                self.assertEqual(
                    config_model.model_validate({}).model_dump(),
                    {name: "" for name in expected},
                )


if __name__ == "__main__":
    unittest.main()
