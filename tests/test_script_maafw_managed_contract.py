from __future__ import annotations

import asyncio
import ast
import importlib.util
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "packages" / "automas_script_maafw_managed"
MODULE_ROOT = PACKAGE_ROOT / "src" / "automas_script_maafw_managed"
BASE_MODULE_ROOT = ROOT / "packages" / "automas_script_maafw" / "src" / "automas_script_maafw"


class ScriptMaaFWManagedContractTest(unittest.TestCase):
    def test_distribution_and_entry_point_contract(self) -> None:
        project = tomllib.loads(
            (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(project["project"]["name"], "automas-script-maafw-managed")
        self.assertEqual(
            project["project"]["entry-points"]["auto_mas.plugins"],
            {
                "automas_script_maafw_managed": (
                    "automas_script_maafw_managed.plugin:Plugin"
                )
            },
        )
        dependencies = project["project"]["dependencies"]
        self.assertIn("automas-script-maafw>=0.1.7", dependencies)
        self.assertIn("automas-maafw-runner>=0.3.0", dependencies)
        self.assertIn("automas-maafw-interface>=0.2.0", dependencies)
        self.assertIn("automas-maafw-project-update>=0.1.0", dependencies)
        self.assertIn("automas-maafw-project-store>=0.1.0", dependencies)
        self.assertIn("automas-maafw-runtime-pool>=0.1.0", dependencies)

    def test_adapter_registration_is_declarative_and_reuses_icon(self) -> None:
        tree = ast.parse((MODULE_ROOT / "plugin.py").read_text(encoding="utf-8"))
        definition = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ScriptAdapterDefinition"
            and self._keyword_literal(node, "type_key") == "MaaFWManaged"
        )
        self.assertEqual(
            self._keyword_literal(definition, "display_name"),
            "托管 MaaFW 项目",
        )
        self.assertEqual(
            self._keyword_literal(definition, "icon_path"),
            "automas_script_maafw:assets/maafw.png",
        )
        self.assertEqual(
            self._keyword_literal(definition, "editor_kind"),
            "schema",
        )
        self.assertEqual(
            ast.unparse(self._keyword(definition, "hooks_factory").value),
            "MaaFWManagedAdapterHooks",
        )
        metadata = self._keyword_literal(definition, "metadata")
        self.assertTrue(metadata["declarative"])
        self.assertEqual(metadata["resource_model"], "project-store")

    def test_hooks_delegate_to_existing_maafw_runner(self) -> None:
        source = (MODULE_ROOT / "adapter.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        hook_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "MaaFWManagedAdapterHooks"
        )
        self.assertEqual([ast.unparse(base) for base in hook_class.bases], ["MaaFWAdapterHooks"])
        self.assertIn(
            "from automas_script_maafw.runner_task import MaaFWPluginAutoProxyTask",
            source,
        )
        run_method = next(
            node
            for node in hook_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "run_auto_proxy"
        )
        run_source = ast.unparse(run_method)
        self.assertIn("super().run_auto_proxy(runtime)", run_source)
        self.assertIn("MaaFWPluginAutoProxyTask", run_source)
        self.assertNotIn("create_subprocess_exec", source)
        self.assertNotIn("MaaFWRunnerService()", source)

    def test_execution_resolves_project_then_shared_runtime_and_binds_it(self) -> None:
        service_source = (MODULE_ROOT / "services.py").read_text(encoding="utf-8")
        service_tree = ast.parse(service_source)
        gateway = next(
            node
            for node in service_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ManagedServiceGateway"
        )
        resolve = next(
            node
            for node in gateway.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "resolve_execution"
        )
        resolve_source = ast.unparse(resolve)
        project_index = resolve_source.index("self.resolve_project")
        runtime_index = resolve_source.index("self.resolve_runtime")
        ensure_index = resolve_source.index("self.ensure_runtime")
        self.assertLess(project_index, runtime_index)
        self.assertLess(runtime_index, ensure_index)
        self.assertIn('PROJECT_STORE_SERVICE = "maafw.project_store.v1"', service_source)
        self.assertIn('RUNTIME_POOL_SERVICE = "maafw.runtime_pool.v1"', service_source)
        self.assertNotIn("pydantic", service_source)

        adapter_source = (MODULE_ROOT / "adapter.py").read_text(encoding="utf-8")
        self.assertIn("bind_project_runtime", adapter_source)
        self.assertIn("acquire_runtime_lease", adapter_source)
        self.assertIn("release_runtime_lease", adapter_source)
        self.assertIn("acquire_project_lease", adapter_source)
        self.assertIn("release_project_lease", adapter_source)
        self.assertIn("ttl_seconds", adapter_source)
        self.assertIn("_MINIMUM_LEASE_TTL_SECONDS = 24 * 60 * 60", adapter_source)
        self.assertIn("max(_MINIMUM_LEASE_TTL_SECONDS, requested)", adapter_source)
        self.assertIn('"Path": resolution["projectPath"]', adapter_source)
        self.assertIn('"RuntimeBinding": dict(runtime_binding)', adapter_source)

        self.assertIn("_validate_python_abi(project, runtime)", service_source)
        self.assertIn("_validate_platform_arch(project, runtime)", service_source)
        self.assertIn("拒绝创建未约束的 MaaFW 运行时", service_source)
        self.assertIn("add_reference", service_source)
        self.assertIn("reconcile_runtime_references", service_source)
        self.assertNotIn("clear_binding", service_source)

    def test_managed_update_and_reference_lifecycle_are_isolated(self) -> None:
        adapter_source = (MODULE_ROOT / "adapter.py").read_text(encoding="utf-8")
        adapter_tree = ast.parse(adapter_source)
        hook_class = next(
            node
            for node in adapter_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "MaaFWManagedAdapterHooks"
        )
        update_override = next(
            node
            for node in hook_class.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_update_project_before_run"
        )
        self.assertNotIn("super()", ast.unparse(update_override))
        self.assertNotIn("update_if_needed", ast.unparse(update_override))
        self.assertIn('f"maafw-script:{_script_id(runtime)}"', adapter_source)

        service_source = (MODULE_ROOT / "services.py").read_text(encoding="utf-8")
        self.assertIn("project_reference: str | None = None", service_source)
        self.assertIn('"reference": stable_project_reference', service_source)
        self.assertIn("reconcile_project_references", service_source)
        self.assertIn('if not item.startswith("maafw-script:")', service_source)
        self.assertIn('f"maafw-script:{script_id}"', service_source)

        plugin_source = (MODULE_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("await Config.get_script_records()", plugin_source)
        self.assertIn("script_records=script_records", plugin_source)
        self.assertIn("_resolve_and_bind_runtime", plugin_source)
        self.assertIn("project_reference=request[\"projectReference\"]", plugin_source)

    def test_missing_bound_runtime_is_rebuilt_from_exact_recorded_version(self) -> None:
        source = (MODULE_ROOT / "services.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        gateway = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ManagedServiceGateway"
        )
        resolve = next(
            node
            for node in gateway.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "resolve_execution"
        )
        resolve_source = ast.unparse(resolve)
        self.assertIn("manifest_binding.get('maafwVersion')", resolve_source)
        self.assertIn("runtime_request.pop('runtimeId', None)", resolve_source)
        self.assertIn("_runner_requirements(project_path, bound_maafw_version)", resolve_source)
        self.assertIn("recovered_binding", resolve_source)
        self.assertIn("await self.bind_project_runtime", resolve_source)

    def test_gateway_dynamically_recovers_exact_runtime_and_reconciles_refs(self) -> None:
        services = self._load_services_module()

        class ProjectStore:
            def __init__(self) -> None:
                self.bound: dict[str, object] = {}
                self.project = {
                    "projectId": "demo",
                    "version": "1.0",
                    "dataPath": "C:/immutable/demo/1.0",
                    "runtimeConstraint": ">=4,<5",
                    "manifest": {
                        "runtime": {
                            "constraint": ">=4,<5",
                            "platform": "linux",
                            "arch": "AMD64",
                            "binding": {
                                "runtimeId": "missing-runtime",
                                "maafwVersion": "4.3.0",
                            },
                            "references": [],
                        }
                    },
                }

            def resolve_project(
                self,
                project_id: str,
                version: str | None,
                *,
                touch: bool = True,
            ):
                del project_id, version, touch
                return self.project

            def bind_runtime(
                self,
                project_id: str,
                version: str | None,
                *,
                binding,
                reference: str | None = None,
                touch: bool = True,
            ):
                self.bound = {
                    "projectId": project_id,
                    "version": version,
                    "binding": dict(binding),
                    "reference": reference,
                    "touch": touch,
                }
                runtime = self.project["manifest"]["runtime"]
                runtime["binding"] = dict(binding)
                runtime["references"] = [reference] if reference else []
                return self.project

        class RuntimePool:
            def __init__(self) -> None:
                self.ensure_request: dict[str, object] = {}
                self.added: list[tuple[str, str]] = []

            def resolve_runtime(self, request):
                del request
                return None

            def ensure_runtime(self, request):
                self.ensure_request = dict(request)
                return {
                    "runtimeId": "recovered-runtime",
                    "pythonExecutable": "C:/runtime/python.exe",
                    "maafwVersion": "4.3.0",
                    "identity": {
                        "pythonAbi": "cpython:cpython-312:cp312",
                        "platform": "linux-x86_64",
                        "architecture": "x86_64",
                    },
                }

            def add_reference(self, runtime_id: str, reference: str):
                self.added.append((runtime_id, reference))
                return {"runtimeId": runtime_id, "references": [reference]}

            def remove_reference(self, runtime_id: str, reference: str):
                return {"runtimeId": runtime_id, "references": [], "removed": reference}

        project_store = ProjectStore()
        runtime_pool = RuntimePool()
        gateway = services.ManagedServiceGateway(project_store, runtime_pool)
        original_runner_requirements = services._runner_requirements
        services._runner_requirements = (
            lambda _path, constraint: [services._maafw_requirement(constraint)]
        )
        try:
            resolution = asyncio.run(
                gateway.resolve_execution(
                    {
                        "projectId": "demo",
                        "version": "1.0",
                        "projectReference": "maafw-script:script-one",
                    }
                )
            )
        finally:
            services._runner_requirements = original_runner_requirements

        self.assertNotIn("runtimeId", runtime_pool.ensure_request)
        self.assertEqual(
            runtime_pool.ensure_request["requirements"],
            ["maafw==4.3.0"],
        )
        self.assertEqual(project_store.bound["reference"], "maafw-script:script-one")
        self.assertEqual(
            resolution["project"]["manifest"]["runtime"]["binding"]["runtimeId"],
            "recovered-runtime",
        )

        services._validate_platform_arch(
            {"manifest": {"runtime": {"platform": "win32", "arch": "AMD64"}}},
            {"identity": {"platform": "win-amd64", "architecture": "x86_64"}},
        )
        with self.assertRaisesRegex(services.ManagedServiceError, "架构"):
            services._validate_platform_arch(
                {"manifest": {"runtime": {"platform": "linux", "arch": "arm64"}}},
                {
                    "identity": {
                        "platform": "linux-x86_64",
                        "architecture": "x86_64",
                    }
                },
            )

    def test_gateway_dynamically_removes_stale_script_references(self) -> None:
        services = self._load_services_module()

        class ProjectStore:
            def __init__(self) -> None:
                self.references = {
                    "1.0": ["maafw-script:deleted", "external:keeper"],
                    "2.0": [],
                }

            def list_projects(self):
                return [{"projectId": "demo"}]

            def list_versions(self, project_id: str):
                del project_id
                return [
                    {"version": version, "references": references}
                    for version, references in self.references.items()
                ]

            def set_references(self, project_id: str, version: str, references):
                del project_id
                self.references[version] = list(references)
                return {"version": version, "references": list(references)}

        project_store = ProjectStore()
        gateway = services.ManagedServiceGateway(project_store, object())
        asyncio.run(
            gateway.reconcile_project_references(
                [
                    {
                        "id": "active",
                        "type": "MaaFWManaged",
                        "config": {
                            "Managed": {"ProjectId": "demo", "Version": "2.0"}
                        },
                    },
                    {
                        "id": "other",
                        "type": "General",
                        "config": {
                            "Managed": {"ProjectId": "demo", "Version": "1.0"}
                        },
                    },
                ]
            )
        )
        self.assertEqual(project_store.references["1.0"], ["external:keeper"])
        self.assertEqual(project_store.references["2.0"], ["maafw-script:active"])

    def test_schema_exposes_managed_lifecycle_without_a_custom_frontend(self) -> None:
        schema_source = (MODULE_ROOT / "schema.py").read_text(encoding="utf-8")
        for field_name in (
            "ProjectId",
            "Version",
            "AvailableVersions",
            "Channel",
            "RuntimeConstraint",
            "Status",
            "ImportProject",
            "CheckUpdate",
            "UpdateLatest",
            "SwitchVersion",
            "ListVersions",
            "DeleteVersion",
            "InstallRuntime",
            "ListRuntimes",
            "DeleteRuntime",
            "TargetRuntimeId",
            "PinResources",
            "PreviewGC",
            "RunGC",
            "AutoGC",
            "GCGraceDays",
            "KeepLatest",
        ):
            self.assertIn(f'"{field_name}"', schema_source)
        for route in (
            "/plugin/maafw-managed/import",
            "/plugin/maafw-managed/check-update",
            "/plugin/maafw-managed/update",
            "/plugin/maafw-managed/switch",
            "/plugin/maafw-managed/versions/list",
            "/plugin/maafw-managed/delete",
            "/plugin/maafw-managed/runtime/install",
            "/plugin/maafw-managed/runtime/list",
            "/plugin/maafw-managed/runtime/delete",
            "/plugin/maafw-managed/pin",
            "/plugin/maafw-managed/gc",
        ):
            self.assertIn(route, schema_source)

        base_schema = (BASE_MODULE_ROOT / "schema.py").read_text(encoding="utf-8")
        self.assertIn('PluginField.json("TaskSnapshot"', base_schema)
        self.assertIn("USER_GROUPS = tuple(MAAFW_USER_GROUPS)", schema_source)
        self.assertFalse(any(PACKAGE_ROOT.rglob("*.vue")))
        self.assertFalse((PACKAGE_ROOT / "package.json").exists())

    def test_update_action_stages_an_immutable_copy_and_persists_results(self) -> None:
        services_source = (MODULE_ROOT / "services.py").read_text(encoding="utf-8")
        self.assertIn("TemporaryDirectory", services_source)
        self.assertIn("shutil.copytree", services_source)
        self.assertIn('("apply_update",)', services_source)
        self.assertIn('("update_project", "import_project")', services_source)

        plugin_source = (MODULE_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("Config.update_script", plugin_source)
        self.assertIn('records[0].type != "MaaFWManaged"', plugin_source)
        self.assertIn("_persist_update_result", plugin_source)
        self.assertIn('result.get("deleted") is not True', services_source)
        self.assertIn("_persist_runtime_delete", plugin_source)

    def test_managed_adapter_does_not_squat_the_legacy_maafw_config_names(
        self,
    ) -> None:
        """MaaFWManaged 不得与 MaaFW 共用 legacy 配置类名。

        宿主 script_types.register 会无条件把 legacy_config_class_name 映射到
        provider（后注册者静默覆盖先注册者），而 unregister 只按 legacy 名 pop，
        因此停用其中一个插件会打断另一个仍在加载的插件。MaaFWManaged 是 v6 新增
        类型，没有 r6 遗留配置需要兼容，不应声明 legacy 名。
        """

        managed = self._adapter_definition(MODULE_ROOT / "plugin.py", "MaaFWManaged")
        base = self._adapter_definition(BASE_MODULE_ROOT / "plugin.py", "MaaFW")

        self.assertEqual(
            self._keyword_literal(base, "legacy_config_class_name"),
            "MaaFWConfig",
        )
        self.assertEqual(
            self._keyword_literal(base, "legacy_user_config_class_name"),
            "MaaFWUserConfig",
        )
        for name in ("legacy_config_class_name", "legacy_user_config_class_name"):
            with self.subTest(keyword=name):
                self.assertIsNone(
                    next(
                        (item for item in managed.keywords if item.arg == name),
                        None,
                    ),
                    f"MaaFWManaged 不应声明 {name}",
                )
        # 显式类名仍在，宿主 _class_names() 不会回落到 legacy 名。
        self.assertEqual(
            self._keyword_literal(managed, "script_class_name"),
            "MaaFWManagedPluginConfig",
        )
        self.assertEqual(
            self._keyword_literal(managed, "user_class_name"),
            "MaaFWManagedPluginUserConfig",
        )

    def test_all_modules_are_parseable_without_importing_the_host(self) -> None:
        for path in MODULE_ROOT.glob("*.py"):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"))

    @staticmethod
    def _adapter_definition(plugin_path: Path, type_key: str) -> ast.Call:
        tree = ast.parse(plugin_path.read_text(encoding="utf-8"))
        return next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ScriptAdapterDefinition"
            and next(
                (
                    ast.literal_eval(item.value)
                    for item in node.keywords
                    if item.arg == "type_key"
                ),
                None,
            )
            == type_key
        )

    @staticmethod
    def _keyword(call: ast.Call, name: str) -> ast.keyword:
        return next(item for item in call.keywords if item.arg == name)

    @classmethod
    def _keyword_literal(cls, call: ast.Call, name: str):
        return ast.literal_eval(cls._keyword(call, name).value)

    @staticmethod
    def _load_services_module():
        source_roots = (
            ROOT / "packages" / "automas_maafw_agent_env" / "src",
            ROOT / "packages" / "automas_maafw_runtime_pool" / "src",
            ROOT / "packages" / "automas_maafw_runner" / "src",
        )
        for source_root in reversed(source_roots):
            if str(source_root) not in sys.path:
                sys.path.insert(0, str(source_root))
        module_name = "_automas_script_maafw_managed_services_unit_contract"
        existing = sys.modules.get(module_name)
        if existing is not None:
            return existing
        module_path = MODULE_ROOT / "services.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


if __name__ == "__main__":
    unittest.main()
