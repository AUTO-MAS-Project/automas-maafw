from __future__ import annotations

import asyncio
import ast
import copy
import importlib.util
import json
import sys
import tomllib
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace


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
        self.assertEqual(project["project"]["version"], "0.2.0")
        self.assertEqual(
            project["project"]["entry-points"]["auto_mas.plugins"],
            {
                "automas_script_maafw_managed": (
                    "automas_script_maafw_managed.plugin:Plugin"
                )
            },
        )
        dependencies = project["project"]["dependencies"]
        self.assertIn("automas-script-maafw>=0.1.9", dependencies)
        self.assertIn("automas-maafw-runner>=0.3.3", dependencies)
        self.assertIn("automas-maafw-project-store>=0.2.0", dependencies)
        self.assertIn("automas-maafw-runtime-pool>=0.1.4", dependencies)
        self.assertIn("automas-maafw-project-update>=0.2.0", dependencies)

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
        self.assertNotIn("clear_binding", resolve_source)

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
        self.assertIn('"maafw-script:", "maafw-upgrade:"', service_source)
        self.assertIn('f"maafw-script:{script_id}"', service_source)
        self.assertIn('f"maafw-upgrade:{script_id}:"', service_source)

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
                    "3.0": ["maafw-upgrade:active:stale"],
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
                            "Managed": {
                                "ProjectId": "tampered",
                                "Version": "9.9",
                                "ProjectManifest": {
                                    "projectId": "demo",
                                    "version": "2.0",
                                },
                                "PendingUpgrade": {
                                    "project": {
                                        "projectId": "demo",
                                        "toVersion": "3.0",
                                        "pendingReference": (
                                            "maafw-upgrade:active:plan-one"
                                        ),
                                    }
                                },
                            }
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
        self.assertEqual(
            project_store.references["3.0"],
            ["maafw-upgrade:active:plan-one"],
        )

    def test_local_upgrade_uses_the_selected_artifact_and_keeps_constraints(self) -> None:
        services = self._load_services_module()

        class ProjectStore:
            def __init__(self) -> None:
                self.request: dict[str, object] = {}

            def resolve_project(
                self,
                project_id: str,
                version: str | None,
                *,
                touch: bool = True,
            ):
                del project_id, version, touch
                return {
                    "projectId": "demo",
                    "version": "1.0",
                    "dataPath": "C:/managed/demo/1.0",
                    "runtimeConstraint": "==5.10.4",
                    "manifest": {"runtime": {"constraint": "==5.10.4"}},
                }

            def update_project(
                self,
                source_path: str,
                project_id: str,
                version: str | None,
                *,
                runtime_constraint: str | None,
                activate: bool,
                pinned: bool,
                reference: str | None,
            ):
                self.request = {
                    "sourcePath": source_path,
                    "projectId": project_id,
                    "version": version,
                    "runtimeConstraint": runtime_constraint,
                    "activate": activate,
                    "pinned": pinned,
                    "reference": reference,
                }
                return {
                    "projectId": project_id,
                    "version": version,
                    "dataPath": f"C:/managed/{project_id}/{version}",
                    "runtimeConstraint": runtime_constraint,
                    "manifest": {},
                }

        project_store = ProjectStore()
        gateway = services.ManagedServiceGateway(project_store, object())
        result = asyncio.run(
            gateway.upgrade_project(
                {
                    "sourcePath": "C:/ignored-folder",
                    "sourceArchive": "C:/downloads/m9a.zip",
                    "projectId": "demo",
                    "version": "2.0",
                    "projectReference": "maafw-upgrade:script-one:plan-one",
                }
            )
        )

        self.assertTrue(result["updated"])
        self.assertFalse(result["activated"])
        self.assertEqual(result["currentVersion"], "1.0")
        self.assertEqual(result["latestVersion"], "2.0")
        self.assertEqual(project_store.request["sourcePath"], "C:/downloads/m9a.zip")
        self.assertEqual(project_store.request["runtimeConstraint"], "==5.10.4")
        self.assertFalse(project_store.request["activate"])
        self.assertEqual(
            project_store.request["reference"],
            "maafw-upgrade:script-one:plan-one",
        )

    def test_schema_exposes_managed_lifecycle_without_a_custom_frontend(self) -> None:
        schema_source = (MODULE_ROOT / "schema.py").read_text(encoding="utf-8")
        for field_name in (
            "ImportProjectId",
            "ProjectId",
            "Version",
            "ImportVersion",
            "TargetVersion",
            "AvailableProjects",
            "AvailableVersions",
            "RuntimeConstraint",
            "SourcePath",
            "SourceArchive",
            "ResourceVersion",
            "InterfaceVersion",
            "ResourceCount",
            "TaskCount",
            "AgentCount",
            "Agents",
            "Shells",
            "Capabilities",
            "SourceSizeBytes",
            "ManagedSizeBytes",
            "UpgradeReady",
            "PendingPlanId",
            "UpgradeToken",
            "PendingUpgrade",
            "UpgradePlanStatus",
            "UpgradePlan",
            "Status",
            "ImportProject",
            "UpgradeLocal",
            "ApplyUpgrade",
            "CancelUpgrade",
            "ListProjects",
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
            "Source",
            "Channel",
            "MirrorChyanRID",
            "MirrorChyanCDK",
            "GitHubRepo",
            "GitHubTag",
            "GitHubAssetPattern",
            "LatestVersion",
            "Installable",
            "Discovery",
            "LastDownload",
            "CheckRemote",
            "ImportRemote",
            "UpgradeRemote",
        ):
            self.assertIn(f'"{field_name}"', schema_source)
        for route in (
            "/plugin/maafw-managed/import",
            "/plugin/maafw-managed/upgrade-local",
            "/plugin/maafw-managed/upgrade-apply",
            "/plugin/maafw-managed/upgrade-cancel",
            "/plugin/maafw-managed/projects/list",
            "/plugin/maafw-managed/switch",
            "/plugin/maafw-managed/versions/list",
            "/plugin/maafw-managed/delete",
            "/plugin/maafw-managed/runtime/install",
            "/plugin/maafw-managed/runtime/list",
            "/plugin/maafw-managed/runtime/delete",
            "/plugin/maafw-managed/pin",
            "/plugin/maafw-managed/gc",
            "/plugin/maafw-managed/remote/check",
            "/plugin/maafw-managed/remote/import",
            "/plugin/maafw-managed/remote/upgrade",
        ):
            self.assertIn(route, schema_source)
        self.assertNotIn("/plugin/maafw-managed/check-update", schema_source)
        self.assertNotIn("/plugin/maafw-managed/update", schema_source)
        action_nodes = {
            str(node.args[0].value): ast.unparse(node)
            for node in ast.walk(ast.parse(schema_source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "button"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        self.assertIn(
            "formModel.Managed.ImportProjectId",
            action_nodes["ImportProject"],
        )
        self.assertNotIn(
            "formModel.Managed.ProjectId",
            action_nodes["ImportProject"],
        )
        self.assertIn(
            "formModel.Managed.ProjectId",
            action_nodes["UpgradeLocal"],
        )
        self.assertNotIn(
            "formModel.Managed.ImportProjectId",
            action_nodes["UpgradeLocal"],
        )

        base_schema = (BASE_MODULE_ROOT / "schema.py").read_text(encoding="utf-8")
        self.assertIn('PluginField.json("TaskSnapshot"', base_schema)
        self.assertIn(
            "USER_GROUPS = (*MAAFW_USER_GROUPS, USER_UPGRADE_GROUP)",
            schema_source,
        )
        self.assertFalse(any(PACKAGE_ROOT.rglob("*.vue")))
        self.assertFalse((PACKAGE_ROOT / "package.json").exists())

    def test_local_upgrade_imports_an_immutable_version_and_persists_results(self) -> None:
        services_source = (MODULE_ROOT / "services.py").read_text(encoding="utf-8")
        self.assertIn('("update_project", "import_project")', services_source)
        self.assertIn("sourceArchive", services_source)
        self.assertNotIn("TemporaryDirectory", services_source)
        self.assertIn('PROJECT_UPDATE_SERVICE = "maafw.project_update.v1"', services_source)
        self.assertIn('INTERFACE_SERVICE = "maafw.interface.v1"', services_source)
        self.assertIn("download_remote_package", services_source)

        plugin_source = (MODULE_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("Config.update_script", plugin_source)
        self.assertIn('_record_field(records[0], "type") != "MaaFWManaged"', plugin_source)
        self.assertIn("_persist_upgrade_result", plugin_source)
        self.assertIn("plan_resource_upgrade", plugin_source)
        self.assertIn("await Config.get_user_records(script_id)", plugin_source)
        self.assertIn("_assert_pending_fresh", plugin_source)
        self.assertIn("_rollback_pending_upgrade", plugin_source)
        self.assertIn('definition_data.get("resource_service_key")', plugin_source)
        self.assertIn("reconcile_project_references", plugin_source)
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


class ManagedUpgradeStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = self._load_plugin_module()
        self.module._JSON_OBJECT_FIELDS = frozenset(
            {
                ("Task", "TaskSnapshot"),
                ("Managed", "PendingUpgrade"),
                ("Managed", "UpgradePlan"),
                ("ManagedUpgrade", "PendingPlan"),
            }
        )
        self.script_id = "script-one"
        self.old_project = self._project("1.0", "old-hash")
        self.new_project = self._project("2.0", "new-hash")

    def test_runtime_install_request_rejects_stale_binding(self) -> None:
        config = {
            "Managed": {
                "ProjectId": "tampered",
                "Version": "9.9",
                "ProjectManifest": {
                    "projectId": "m9a",
                    "version": "2.0",
                },
                "RuntimeConstraint": "==5.10.4",
            }
        }
        request = self.module._runtime_install_request(
            config,
            {
                "projectId": "m9a",
                "version": "2.0",
                "runtimeConstraint": "==5.10.4",
            },
        )
        self.assertEqual(request["version"], "2.0")

        with self.assertRaisesRegex(
            self.module.ManagedServiceError,
            "页面中的资源或运行时配置已过期",
        ):
            self.module._runtime_install_request(
                config,
                {
                    "projectId": "m9a",
                    "version": "1.0",
                    "runtimeConstraint": "==5.10.4",
                },
            )

    def test_remote_discovery_drops_ephemeral_download_url_before_persistence(self) -> None:
        public = self.module._public_remote_discovery(
            {
                "latestVersion": "2.0.0",
                "installable": True,
                "candidate": {
                    "source": "mirrorchyan",
                    "version": "2.0.0",
                    "download_url": "https://download.example/pkg.zip?cdk=secret",
                    "sha256": "a" * 64,
                },
            }
        )

        self.assertNotIn("download_url", public["candidate"])
        self.assertNotIn("downloadUrl", public["candidate"])
        self.assertTrue(public["candidate"]["downloadAvailable"])
        self.assertEqual(public["candidate"]["sha256"], "a" * 64)

    def test_plans_and_persists_every_user_without_switching(self) -> None:
        config = self._fake_config(manual_user=True)
        self.module.Config = config
        plugin = self.module.Plugin(self._context())
        plugin._refresh_project_versions_and_references = self._no_refresh
        plan = asyncio.run(
            plugin._build_pack_upgrade_plan(
                self.script_id,
                {
                    "previousProject": self.old_project,
                    "project": self.new_project,
                },
                plan_id="plan-one",
                pending_reference="maafw-upgrade:script-one:plan-one",
            )
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["userIds"], ["user-one", "user-two"])
        self.assertEqual(plan["planCount"], 3)
        self.assertFalse(plan["readyToApply"])
        self.assertEqual(len(plan["manualActions"]), 1)

        result = {
            "project": self.new_project,
            "previousProject": self.old_project,
            "latestVersion": "2.0",
            "_upgradePlanInternal": plan,
            "upgradePlan": self.module._public_upgrade_plan(plan),
        }
        asyncio.run(
            plugin._persist_upgrade_result(self.script_id, result, {})
        )
        managed = config.script.config["Managed"]
        self.assertEqual(managed["Version"], "1.0")
        self.assertEqual(config.script.config["Info"]["Path"], "C:/store/m9a/1.0")
        self.assertEqual(managed["PendingVersion"], "2.0")
        self.assertEqual(managed["PendingUpgrade"]["state"], "blocked")
        self.assertEqual(
            config.users[0].config["ManagedUpgrade"]["PendingPlan"]["recordId"],
            "user-one",
        )
        self.assertNotIn(
            "sourceConfig",
            managed["PendingUpgrade"]["users"][0],
        )

    def test_ready_plan_is_cas_applied_then_switches(self) -> None:
        config = self._fake_config(manual_user=False)
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh
        pending = asyncio.run(
            plugin._build_pack_upgrade_plan(
                self.script_id,
                {
                    "previousProject": self.old_project,
                    "project": self.new_project,
                },
                plan_id="plan-ready",
                pending_reference="maafw-upgrade:script-one:plan-ready",
            )
        )
        self.assertEqual(pending["state"], "ready")
        result = {
            "project": self.new_project,
            "previousProject": self.old_project,
            "latestVersion": "2.0",
            "_upgradePlanInternal": pending,
            "upgradePlan": self.module._public_upgrade_plan(pending),
        }
        asyncio.run(plugin._persist_upgrade_result(self.script_id, result, {}))
        config.events.clear()
        applied = asyncio.run(
            plugin._apply_pending_upgrade_transaction(
                self.script_id,
                {
                    "planId": "plan-ready",
                    "confirmation": pending["confirmationToken"],
                },
            )
        )
        self.assertTrue(applied["applied"])
        self.assertEqual(gateway.switches, ["2.0"])
        self.assertLess(
            config.events.index("user:user-two:target"),
            config.events.index("switch:2.0"),
        )
        self.assertEqual(config.script.config["Managed"]["Version"], "2.0")
        self.assertEqual(config.script.config["Info"]["Path"], "C:/store/m9a/2.0")
        self.assertEqual(
            config.users[0].config["Task"]["TaskSnapshot"]["migratedTo"],
            "2.0",
        )
        self.assertEqual(
            config.users[0].config["ManagedUpgrade"]["PendingPlan"],
            {},
        )
        self.assertEqual(
            config.transactions,
            [("enter", "plan-ready"), ("exit", "plan-ready")],
        )

    def test_changed_user_config_invalidates_plan_before_switch(self) -> None:
        config = self._fake_config(manual_user=False)
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh
        pending = asyncio.run(
            plugin._build_pack_upgrade_plan(
                self.script_id,
                {
                    "previousProject": self.old_project,
                    "project": self.new_project,
                },
                plan_id="plan-stale",
                pending_reference="maafw-upgrade:script-one:plan-stale",
            )
        )
        result = {
            "project": self.new_project,
            "previousProject": self.old_project,
            "latestVersion": "2.0",
            "_upgradePlanInternal": pending,
            "upgradePlan": self.module._public_upgrade_plan(pending),
        }
        asyncio.run(plugin._persist_upgrade_result(self.script_id, result, {}))
        config.users[0].config["Task"]["SelectedPreset"] = "changed-after-plan"
        with self.assertRaisesRegex(
            self.module.ManagedServiceError,
            "配置在规划后发生变化",
        ):
            asyncio.run(
                plugin._apply_pending_upgrade(
                    self.script_id,
                    {
                        "planId": "plan-stale",
                        "confirmation": pending["confirmationToken"],
                    },
                )
            )
        self.assertEqual(gateway.switches, [])
        self.assertEqual(config.script.config["Managed"]["Version"], "1.0")
        self.assertEqual(
            config.script.config["Managed"]["PendingUpgrade"]["state"],
            "stale",
        )

    def test_interrupted_apply_restores_exact_json_snapshots(self) -> None:
        config = self._fake_config(manual_user=False)
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh
        pending = asyncio.run(
            plugin._build_pack_upgrade_plan(
                self.script_id,
                {
                    "previousProject": self.old_project,
                    "project": self.new_project,
                },
                plan_id="plan-recover",
                pending_reference="maafw-upgrade:script-one:plan-recover",
            )
        )
        result = {
            "project": self.new_project,
            "previousProject": self.old_project,
            "latestVersion": "2.0",
            "_upgradePlanInternal": pending,
            "upgradePlan": self.module._public_upgrade_plan(pending),
        }
        asyncio.run(plugin._persist_upgrade_result(self.script_id, result, {}))
        durable = config.script.config["Managed"]["PendingUpgrade"]
        asyncio.run(
            config.update_script(
                self.script_id,
                self.module._atomic_json_field_update(
                    durable["script"]["targetConfig"]
                ),
            )
        )
        for user in durable["users"]:
            journal = next(
                record.config["ManagedUpgrade"]["PendingPlan"]
                for record in config.users
                if record.id == user["recordId"]
            )
            asyncio.run(
                config.update_user(
                    self.script_id,
                    user["recordId"],
                    self.module._atomic_json_field_update(
                        journal["targetConfig"]
                    ),
                )
            )
        asyncio.run(plugin._set_upgrade_state(self.script_id, durable, "applying"))

        with self.assertRaisesRegex(
            self.module.ManagedServiceError,
            "已恢复旧版本与旧配置",
        ):
            asyncio.run(
                plugin._apply_pending_upgrade(
                    self.script_id,
                    {
                        "planId": "plan-recover",
                        "confirmation": pending["confirmationToken"],
                    },
                )
            )

        self.assertEqual(gateway.switches, ["1.0"])
        self.assertEqual(
            config.users[0].config["Task"]["TaskSnapshot"],
            {"value": 1},
        )
        self.assertEqual(
            config.script.config["Managed"]["PendingUpgrade"]["state"],
            "ready",
        )

    def test_bound_script_cannot_bypass_upgrade_with_initial_import(self) -> None:
        config = self._fake_config(manual_user=False)
        config.script.config["Managed"].update(
            {
                "ProjectId": "",
                "Version": "",
                "ProjectManifest": {
                    "projectId": "m9a",
                    "version": "1.0",
                },
            }
        )
        config.script.config["Info"]["Path"] = ""
        self.module.Config = config
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: self.fail("bound import must not reach gateway")
        with self.assertRaisesRegex(
            self.module.ManagedServiceError,
            "不能用首次导入绕过升级事务",
        ):
            asyncio.run(
                plugin._import_initial_project(
                    self.script_id,
                    {"projectId": "m9a", "sourcePath": "C:/candidate"},
                )
            )

    def test_preconfigured_project_id_allows_first_import(self) -> None:
        config = self._fake_config(manual_user=False)
        config.script.config["Managed"]["ProjectId"] = ""
        config.script.config["Managed"]["Version"] = ""
        config.script.config["Managed"]["ImportProjectId"] = "m9a"
        config.script.config["Info"]["Path"] = ""
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh

        result = asyncio.run(
            plugin._import_initial_project(
                self.script_id,
                {"projectId": "m9a", "sourcePath": "C:/candidate"},
            )
        )

        self.assertEqual(result["version"], "2.0")
        self.assertEqual(config.script.config["Managed"]["Version"], "2.0")
        self.assertEqual(config.script.config["Managed"]["ImportProjectId"], "")
        self.assertEqual(config.script.config["Info"]["Path"], "C:/store/m9a/2.0")

    def test_script_journal_failure_cleans_users_and_reference(self) -> None:
        config = self._fake_config(manual_user=False)
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh
        pending = asyncio.run(
            plugin._build_pack_upgrade_plan(
                self.script_id,
                {
                    "previousProject": self.old_project,
                    "project": self.new_project,
                },
                plan_id="plan-write-fail",
                pending_reference=(
                    "maafw-upgrade:script-one:plan-write-fail"
                ),
            )
        )
        original_update_script = config.update_script

        async def fail_pending_script_write(script_id, update):
            candidate = update.get("Managed", {}).get("PendingUpgrade")
            if isinstance(candidate, dict) and candidate.get("kind"):
                raise RuntimeError("script is locked")
            await original_update_script(script_id, update)

        config.update_script = staticmethod(fail_pending_script_write)
        result = {
            "project": self.new_project,
            "previousProject": self.old_project,
            "latestVersion": "2.0",
            "_upgradePlanInternal": pending,
            "upgradePlan": self.module._public_upgrade_plan(pending),
        }
        with self.assertRaisesRegex(RuntimeError, "script is locked"):
            asyncio.run(plugin._persist_upgrade_result(self.script_id, result, {}))

        self.assertEqual(
            config.users[0].config["ManagedUpgrade"]["PendingPlan"],
            {},
        )
        self.assertEqual(
            gateway.releases,
            [
                (
                    "m9a",
                    "2.0",
                    "maafw-upgrade:script-one:plan-write-fail",
                )
            ],
        )

    def test_recovery_requires_every_user_journal(self) -> None:
        config = self._fake_config(manual_user=False)
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh
        pending = asyncio.run(
            plugin._build_pack_upgrade_plan(
                self.script_id,
                {
                    "previousProject": self.old_project,
                    "project": self.new_project,
                },
                plan_id="plan-missing-user",
                pending_reference=(
                    "maafw-upgrade:script-one:plan-missing-user"
                ),
            )
        )
        result = {
            "project": self.new_project,
            "previousProject": self.old_project,
            "latestVersion": "2.0",
            "_upgradePlanInternal": pending,
            "upgradePlan": self.module._public_upgrade_plan(pending),
        }
        asyncio.run(plugin._persist_upgrade_result(self.script_id, result, {}))
        durable = config.script.config["Managed"]["PendingUpgrade"]
        asyncio.run(plugin._set_upgrade_state(self.script_id, durable, "applying"))
        config.users[0].config["ManagedUpgrade"]["PendingPlan"] = {}

        with self.assertRaisesRegex(
            self.module.ManagedServiceError,
            "缺少完整用户 journal",
        ):
            asyncio.run(plugin._rollback_pending_upgrade(self.script_id, durable))

        self.assertEqual(gateway.switches, [])
        self.assertEqual(
            config.script.config["Managed"]["PendingUpgrade"]["state"],
            "recovery_required",
        )

    def test_startup_sweeps_user_journal_without_script_envelope(self) -> None:
        config = self._fake_config(manual_user=False)
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        pending = asyncio.run(
            plugin._build_pack_upgrade_plan(
                self.script_id,
                {
                    "previousProject": self.old_project,
                    "project": self.new_project,
                },
                plan_id="plan-orphan",
                pending_reference="maafw-upgrade:script-one:plan-orphan",
            )
        )
        result = {
            "project": self.new_project,
            "previousProject": self.old_project,
            "latestVersion": "2.0",
            "_upgradePlanInternal": pending,
            "upgradePlan": self.module._public_upgrade_plan(pending),
        }
        plugin._refresh_project_versions_and_references = self._no_refresh
        asyncio.run(plugin._persist_upgrade_result(self.script_id, result, {}))
        config.script.config["Managed"]["PendingUpgrade"] = {}

        asyncio.run(plugin._repair_upgrade_artifacts_on_start())

        self.assertEqual(
            config.users[0].config["ManagedUpgrade"]["PendingPlan"],
            {},
        )
        self.assertEqual(gateway.reconciliations, 1)

    def _fake_config(self, *, manual_user: bool):
        script = SimpleNamespace(
            id=self.script_id,
            type="MaaFWManaged",
            name="M9A managed",
            config={
                "Info": {
                    "Path": "C:/store/m9a/1.0",
                    "Resource": "Official",
                },
                "Managed": {
                    "ProjectId": "m9a",
                    "Version": "1.0",
                    "PendingVersion": "",
                    "PendingUpgrade": {},
                },
            },
        )
        users = [
            SimpleNamespace(
                id="user-one",
                type="MaaFWManaged",
                name="one",
                config={
                    "Task": {
                        "TaskSnapshot": {"value": 1},
                        "SelectedPreset": "Daily",
                    }
                },
            ),
            SimpleNamespace(
                id="user-two",
                type="MaaFWManaged",
                name="two",
                config={
                    "Task": {
                        "TaskSnapshot": {"value": 2},
                        "SelectedPreset": "Daily",
                    },
                    "NeedsManual": manual_user,
                },
            ),
        ]

        class FakeConfig:
            events: list[str] = []
            transactions: list[tuple[str, str]] = []

            @classmethod
            def script_config_transaction(cls, script_id, *, owner):
                assert script_id == script.id
                plan_id = str(owner).rsplit(":", 1)[-1]

                class Transaction:
                    async def __aenter__(self):
                        cls.transactions.append(("enter", plan_id))

                    async def __aexit__(self, exc_type, exc, traceback):
                        del exc_type, exc, traceback
                        cls.transactions.append(("exit", plan_id))

                return Transaction()

            @classmethod
            @asynccontextmanager
            async def script_config_write_scope(cls, script_id):
                del cls
                assert script_id is None
                yield

            @classmethod
            async def get_script_records(cls, script_id=None):
                return [script] if script_id in (None, script.id) else []

            @classmethod
            async def get_user_records(cls, script_id, user_id=None):
                assert script_id == script.id
                return (
                    users
                    if user_id is None
                    else [item for item in users if item.id == user_id]
                )

            @classmethod
            async def update_script(cls, script_id, update):
                assert script_id == script.id
                _deep_merge_form(script.config, copy.deepcopy(dict(update)))
                cls.events.append("script:update")

            @classmethod
            async def update_user(cls, script_id, user_id, update):
                assert script_id == script.id
                user = next(item for item in users if item.id == user_id)
                payload = copy.deepcopy(dict(update))
                _deep_merge_form(user.config, payload)
                snapshot = user.config.get("Task", {}).get("TaskSnapshot", {})
                phase = (
                    "target"
                    if isinstance(snapshot, dict)
                    and snapshot.get("migratedTo") == "2.0"
                    else "update"
                )
                cls.events.append(f"user:{user_id}:{phase}")

        FakeConfig.script = script
        FakeConfig.users = users
        FakeConfig.transactions = []
        return FakeConfig

    def _context(self):
        class Registry:
            @staticmethod
            def get_project_pack(project_id):
                return {
                    "key": project_id,
                    "resource_service_key": "maafw.pack.m9a.v1",
                    "resource_upgrade_mode": "plan-only",
                }

        class Pack:
            @staticmethod
            def plan_resource_upgrade(old_path, new_path, config):
                del old_path, new_path
                target = copy.deepcopy(config)
                task = target.get("Task")
                if isinstance(task, dict) and isinstance(
                    task.get("TaskSnapshot"), dict
                ):
                    task["TaskSnapshot"]["migratedTo"] = "2.0"
                manual = bool(target.pop("NeedsManual", False))
                return {
                    "schemaVersion": 1,
                    "kind": "maafw.resource-upgrade-plan",
                    "projectId": "m9a",
                    "fromVersion": "1.0",
                    "toVersion": "2.0",
                    "config": target,
                    "manualActions": (
                        [{"kind": "manual-test"}] if manual else []
                    ),
                    "warnings": [],
                    "lossless": True,
                    "readyToApply": not manual,
                }

        services = {
            "maafw.registry.v1": Registry(),
            "maafw.pack.m9a.v1": Pack(),
        }
        return SimpleNamespace(
            get=lambda key: services.get(key),
            logger=SimpleNamespace(
                warning=lambda *_args, **_kwargs: None,
                error=lambda *_args, **_kwargs: None,
            ),
        )

    @staticmethod
    async def _no_refresh(_script_id, _project_id):
        return None

    @staticmethod
    def _project(version: str, source_hash: str) -> dict:
        return {
            "projectId": "m9a",
            "version": version,
            "dataPath": f"C:/store/m9a/{version}",
            "runtimeConstraint": "==5.10.4",
            "manifest": {
                "source": {
                    "hash": {
                        "algorithm": "sha256",
                        "scope": "projected-source",
                        "value": source_hash,
                    }
                }
            },
        }

    class _Gateway:
        def __init__(self, old_project, new_project, events):
            self.old_project = old_project
            self.new_project = new_project
            self.events = events
            self.switches: list[str] = []
            self.releases: list[tuple[str, str, str]] = []
            self.reconciliations = 0

        @asynccontextmanager
        async def resource_transaction(self):
            yield

        async def import_project(self, payload):
            assert payload["projectId"] == "m9a"
            return self.new_project

        async def resolve_project(self, project_id, version):
            assert project_id == "m9a"
            return self.old_project if version == "1.0" else self.new_project

        async def switch_version(self, payload):
            version = payload["version"]
            self.switches.append(version)
            self.events.append(f"switch:{version}")
            return self.old_project if version == "1.0" else self.new_project

        async def release_project_reference(self, project_id, version, reference):
            self.releases.append((project_id, version, reference))

        async def reconcile_project_references(self, _records):
            self.reconciliations += 1

    @staticmethod
    def _load_plugin_module():
        package_name = "_automas_script_maafw_managed_contract_package"
        module_name = f"{package_name}.plugin_contract"
        existing = sys.modules.get(module_name)
        if existing is not None:
            return existing
        package = types.ModuleType(package_name)
        package.__path__ = [str(MODULE_ROOT)]
        sys.modules[package_name] = package

        app = types.ModuleType("app")
        app_core = types.ModuleType("app.core")
        app_plugins = types.ModuleType("app.plugins")
        app_core.Config = object()

        class ScriptAdapterPlugin:
            def __init__(self, ctx):
                self.ctx = ctx

            async def on_start(self):
                return None

        class ScriptAdapterDefinition:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        app_plugins.ScriptAdapterPlugin = ScriptAdapterPlugin
        app_plugins.ScriptAdapterDefinition = ScriptAdapterDefinition
        app_plugins.PluginHttpRequest = object
        app.core = app_core
        app.plugins = app_plugins
        sys.modules.setdefault("app", app)
        sys.modules.setdefault("app.core", app_core)
        sys.modules.setdefault("app.plugins", app_plugins)

        adapter = types.ModuleType(f"{package_name}.adapter")
        adapter.MaaFWManagedAdapterHooks = type(
            "MaaFWManagedAdapterHooks",
            (),
            {},
        )
        schema = types.ModuleType(f"{package_name}.schema")
        schema.SCRIPT_GROUPS = ()
        schema.USER_GROUPS = ()
        services_source = ScriptMaaFWManagedContractTest._load_services_module()
        services = types.ModuleType(f"{package_name}.services")
        for name in (
            "PROJECT_STORE_SERVICE",
            "RUNTIME_POOL_SERVICE",
            "PROJECT_UPDATE_SERVICE",
            "INTERFACE_SERVICE",
            "ManagedServiceError",
            "ManagedServiceGateway",
            "managed_project_identity",
        ):
            setattr(services, name, getattr(services_source, name))
        sys.modules[adapter.__name__] = adapter
        sys.modules[schema.__name__] = schema
        sys.modules[services.__name__] = services

        spec = importlib.util.spec_from_file_location(
            module_name,
            MODULE_ROOT / "plugin.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = package_name
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


def _deep_merge(target: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _deep_merge_form(target: dict, update: dict) -> None:
    """Mirror the host deep merge followed by JSON-field decoding."""

    _deep_merge(target, update)
    for group_name, fields in update.items():
        if not isinstance(fields, dict):
            continue
        target_group = target.get(group_name)
        if not isinstance(target_group, dict):
            continue
        for field_name, raw_value in fields.items():
            if not isinstance(raw_value, str):
                continue
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, (dict, list)):
                target_group[field_name] = parsed


if __name__ == "__main__":
    unittest.main()
