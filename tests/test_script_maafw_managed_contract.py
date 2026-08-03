from __future__ import annotations

import asyncio
import ast
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
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
        self.assertEqual(project["project"]["version"], "0.2.1")
        self.assertEqual(
            project["project"]["entry-points"]["auto_mas.plugins"],
            {
                "automas_script_maafw_managed": (
                    "automas_script_maafw_managed.plugin:Plugin"
                )
            },
        )
        dependencies = project["project"]["dependencies"]
        self.assertIn("automas-script-maafw>=0.1.10", dependencies)
        self.assertIn("automas-maafw-runner>=0.3.4", dependencies)
        self.assertIn("automas-maafw-project-store>=0.2.1", dependencies)
        self.assertIn("automas-maafw-runtime-pool>=0.1.5", dependencies)
        self.assertIn("automas-maafw-project-update>=0.2.2", dependencies)

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
            "plugin:automas_script_maafw",
        )
        self.assertEqual(
            ast.unparse(self._keyword(definition, "hooks_factory").value),
            "MaaFWManagedAdapterHooks",
        )
        metadata = self._keyword_literal(definition, "metadata")
        self.assertTrue(metadata["declarative"])
        self.assertEqual(metadata["resource_model"], "project-store")
        self.assertFalse(metadata["creatable"])
        self.assertEqual(metadata["create_mode"], "convert-only")
        self.assertEqual(metadata["editor_reuse_type"], "MaaFW")

    def test_single_entry_backend_routes_are_registered(self) -> None:
        source = (MODULE_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn('"/maafw-managed/capabilities"', source)
        self.assertIn('"/maafw-managed/progress"', source)
        self.assertIn('"/maafw-managed/operations/active"', source)
        self.assertIn('"/maafw-managed/convert"', source)
        self.assertIn("get_plugin_script_type_conversion_snapshot", source)
        self.assertIn("convert_plugin_script_type", source)
        self.assertNotIn("Config.add_script", source)
        self.assertNotIn("Config.del_script", source)

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

    def test_initial_import_honors_activate_override_and_defaults_true(self) -> None:
        services = self._load_services_module()

        class ProjectStore:
            def __init__(self) -> None:
                self.activations: list[bool] = []

            def import_project(
                self,
                source_path,
                project_id,
                version,
                *,
                runtime_constraint,
                activate,
                pinned,
                reference,
            ):
                del source_path, runtime_constraint, pinned, reference
                self.activations.append(activate)
                return {
                    "projectId": project_id,
                    "version": version,
                    "dataPath": str(ROOT),
                    "manifest": {},
                }

        async def scenario():
            project_store = ProjectStore()
            gateway = services.ManagedServiceGateway(project_store, object())
            base = {
                "sourcePath": str(ROOT),
                "projectId": "demo",
                "version": "1.0",
                "projectReference": "maafw-script:script-one",
            }
            await gateway.import_project(base)
            await gateway.import_project({**base, "version": "2.0", "activate": False})
            return project_store.activations

        self.assertEqual(asyncio.run(scenario()), [True, False])

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

            def checkout_project(
                self,
                project_id: str,
                version: str | None,
                script_id: str,
            ):
                return {
                    "checkoutId": "maafw-checkout-" + "1" * 32,
                    "dataPath": "C:/mutable/demo/1.0",
                    "projectId": project_id,
                    "version": version,
                    "scriptId": script_id,
                    "runRootId": "11111111-1111-4111-8111-111111111111",
                    "payloadHash": "a" * 64,
                }

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
                self.runtime: dict[str, object] | None = None

            def resolve_runtime(self, request):
                if (
                    self.runtime is not None
                    and request.get("runtimeId") == self.runtime.get("runtimeId")
                ):
                    return dict(self.runtime)
                return None

            def ensure_runtime(self, request):
                self.ensure_request = dict(request)
                self.runtime = {
                    "runtimeId": "recovered-runtime",
                    "pythonExecutable": "C:/runtime/python.exe",
                    "maafwVersion": "4.3.0",
                    "references": [],
                    "identity": {
                        "pythonAbi": "cpython:cpython-312:cp312",
                        "platform": "linux-x86_64",
                        "architecture": "x86_64",
                    },
                }
                return dict(self.runtime)

            def add_reference(self, runtime_id: str, reference: str):
                self.added.append((runtime_id, reference))
                assert self.runtime is not None
                self.runtime["references"] = [reference]
                return dict(self.runtime)

            def remove_reference(self, runtime_id: str, reference: str):
                assert self.runtime is not None
                self.runtime["references"] = []
                return {**self.runtime, "removed": reference}

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
                        "scriptId": "script-one",
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
            "ConversionJournal",
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


def _load_managed_adapter_module():
    package_name = "_automas_script_maafw_managed_adapter_contract"
    module_name = f"{package_name}.adapter"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    services = ScriptMaaFWManagedContractTest._load_services_module()
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE_ROOT)]
    sys.modules[package_name] = package
    sys.modules[f"{package_name}.services"] = services

    sentinel = object()
    stub_names = (
        "app",
        "app.core",
        "app.plugins",
        "automas_script_maafw",
        "automas_script_maafw.adapter",
        "automas_script_maafw.runner_task",
    )
    previous = {name: sys.modules.get(name, sentinel) for name in stub_names}

    class MaaFWAdapterHooks:
        async def finalize(self, runtime):
            del runtime

        async def on_crash(self, runtime, error):
            del runtime, error

        @staticmethod
        def _emit_log(runtime, message):
            runtime.extra.setdefault("test_logs", []).append(message)

    app = types.ModuleType("app")
    app_core = types.ModuleType("app.core")
    app_plugins = types.ModuleType("app.plugins")
    app_core.Config = object()
    app_plugins.ScriptAdapterRuntime = object
    app.core = app_core
    app.plugins = app_plugins

    base_package = types.ModuleType("automas_script_maafw")
    base_adapter = types.ModuleType("automas_script_maafw.adapter")
    base_runner = types.ModuleType("automas_script_maafw.runner_task")
    base_adapter.MaaFWAdapterHooks = MaaFWAdapterHooks
    base_runner.MaaFWPluginAutoProxyTask = type("MaaFWPluginAutoProxyTask", (), {})
    base_package.adapter = base_adapter
    base_package.runner_task = base_runner

    sys.modules.update(
        {
            "app": app,
            "app.core": app_core,
            "app.plugins": app_plugins,
            "automas_script_maafw": base_package,
            "automas_script_maafw.adapter": base_adapter,
            "automas_script_maafw.runner_task": base_runner,
        }
    )
    try:
        spec = importlib.util.spec_from_file_location(module_name, MODULE_ROOT / "adapter.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class ManagedAdapterLeaseLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = _load_managed_adapter_module()
        self.services = ScriptMaaFWManagedContractTest._load_services_module()

    def _runtime(self, gateway):
        return SimpleNamespace(
            extra={self.adapter._GATEWAY_KEY: gateway},
            script_info=SimpleNamespace(script_id="script-one", uid="script-one"),
        )

    def test_cancelled_acquires_compensate_committed_lease_intents(self) -> None:
        adapter = self.adapter
        services = self.services

        class CommitThenCancelGateway(services.ManagedServiceGateway):
            def __init__(self) -> None:
                self.active_runtime: set[tuple[str, str]] = set()
                self.active_project: set[tuple[str, str, str]] = set()
                self.active_checkout: set[tuple[str, str, str]] = set()

            async def acquire_runtime_lease(self, runtime_id, lease_id, **_kwargs):
                self.active_runtime.add((runtime_id, lease_id))
                raise asyncio.CancelledError

            async def release_runtime_lease(self, runtime_id, lease_id):
                self.active_runtime.discard((runtime_id, lease_id))
                return {}

            async def acquire_project_lease(
                self,
                project_id,
                version,
                lease_id,
                **_kwargs,
            ):
                self.active_project.add((project_id, version, lease_id))
                raise asyncio.CancelledError

            async def release_project_lease(self, project_id, version, lease_id):
                self.active_project.discard((project_id, version, lease_id))
                return {}

            async def acquire_checkout_lease(
                self,
                checkout_id,
                script_id,
                lease_id,
                **_kwargs,
            ):
                self.active_checkout.add((checkout_id, script_id, lease_id))
                raise asyncio.CancelledError

            async def release_checkout_lease(self, checkout_id, script_id, lease_id):
                self.active_checkout.discard((checkout_id, script_id, lease_id))
                return {}

        async def expect_cancelled(awaitable):
            with self.assertRaises(asyncio.CancelledError):
                await awaitable

        async def scenario() -> None:
            gateway = CommitThenCancelGateway()
            hooks = adapter.MaaFWManagedAdapterHooks()

            runtime = self._runtime(gateway)
            await expect_cancelled(
                hooks._acquire_runtime_lease(
                    runtime,
                    {"runtime": {"runtimeId": "runtime-one"}},
                )
            )
            self.assertEqual(gateway.active_runtime, set())
            self.assertNotIn(adapter._LEASE_KEY, runtime.extra)

            runtime = self._runtime(gateway)
            runtime.extra[adapter._LEASE_KEY] = {
                "runtimeId": "runtime-one",
                "leaseId": "lease-one",
            }
            await expect_cancelled(
                hooks._acquire_project_lease(
                    runtime,
                    {"project": {"projectId": "demo", "version": "1.0"}},
                )
            )
            self.assertEqual(gateway.active_project, set())
            self.assertNotIn(adapter._PROJECT_LEASE_KEY, runtime.extra)

            runtime = self._runtime(gateway)
            runtime.extra[adapter._LEASE_KEY] = {
                "runtimeId": "runtime-one",
                "leaseId": "lease-one",
            }
            await expect_cancelled(
                hooks._acquire_checkout_lease(
                    runtime,
                    {"checkout": {"checkoutId": "checkout-one"}},
                )
            )
            self.assertEqual(gateway.active_checkout, set())
            self.assertNotIn(adapter._CHECKOUT_LEASE_KEY, runtime.extra)

        asyncio.run(scenario())

    def test_sync_runtime_acquire_cancel_after_commit_is_compensated(self) -> None:
        adapter = self.adapter
        services = self.services
        started = threading.Event()
        finish = threading.Event()

        class RuntimePool:
            def __init__(self) -> None:
                self.active: set[tuple[str, str]] = set()
                self.release_calls = 0

            def acquire_lease(self, runtime_id, lease_id, **_kwargs):
                started.set()
                if not finish.wait(timeout=2):
                    raise TimeoutError("test acquire worker was not released")
                self.active.add((runtime_id, lease_id))
                return {"runtimeId": runtime_id, "activeLeaseIds": [lease_id]}

            def release_lease(self, runtime_id, lease_id):
                self.release_calls += 1
                self.active.discard((runtime_id, lease_id))
                return {"runtimeId": runtime_id, "activeLeaseIds": []}

        async def scenario() -> None:
            runtime_pool = RuntimePool()
            gateway = services.ManagedServiceGateway(object(), runtime_pool)
            hooks = adapter.MaaFWManagedAdapterHooks()
            runtime = self._runtime(gateway)
            acquire = asyncio.create_task(
                hooks._acquire_runtime_lease(
                    runtime,
                    {"runtime": {"runtimeId": "runtime-one"}},
                )
            )
            while not started.is_set():
                await asyncio.sleep(0)
            acquire.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await acquire
            self.assertEqual(runtime_pool.active, set())
            self.assertEqual(runtime_pool.release_calls, 1)
            self.assertNotIn(adapter._LEASE_KEY, runtime.extra)

        try:
            asyncio.run(scenario())
        finally:
            finish.set()

    def test_failed_releases_remain_for_next_teardown_retry(self) -> None:
        adapter = self.adapter
        services = self.services

        class RetryGateway(services.ManagedServiceGateway):
            def __init__(self) -> None:
                self.calls = {"checkout": 0, "project": 0, "runtime": 0}

            @asynccontextmanager
            async def resource_transaction(self):
                yield

            async def _release(self, name):
                self.calls[name] += 1
                if self.calls[name] == 1:
                    raise services.ManagedServiceError(f"{name} transient failure")
                return {}

            async def release_checkout_lease(self, *_args):
                return await self._release("checkout")

            async def release_project_lease(self, *_args):
                return await self._release("project")

            async def release_runtime_lease(self, *_args):
                return await self._release("runtime")

        async def scenario() -> None:
            gateway = RetryGateway()
            hooks = adapter.MaaFWManagedAdapterHooks()
            runtime = self._runtime(gateway)
            runtime.extra.update(
                {
                    adapter._CHECKOUT_LEASE_KEY: {
                        "checkoutId": "checkout-one",
                        "scriptId": "script-one",
                        "leaseId": "lease-one",
                    },
                    adapter._PROJECT_LEASE_KEY: {
                        "projectId": "demo",
                        "version": "1.0",
                        "leaseId": "lease-one",
                    },
                    adapter._LEASE_KEY: {
                        "runtimeId": "runtime-one",
                        "leaseId": "lease-one",
                    },
                }
            )

            await hooks.on_crash(runtime, RuntimeError("run failed"))
            self.assertIn(adapter._CHECKOUT_LEASE_KEY, runtime.extra)
            self.assertIn(adapter._PROJECT_LEASE_KEY, runtime.extra)
            self.assertIn(adapter._LEASE_KEY, runtime.extra)

            await hooks.finalize(runtime)
            self.assertNotIn(adapter._CHECKOUT_LEASE_KEY, runtime.extra)
            self.assertNotIn(adapter._PROJECT_LEASE_KEY, runtime.extra)
            self.assertNotIn(adapter._LEASE_KEY, runtime.extra)
            self.assertEqual(
                gateway.calls,
                {"checkout": 2, "project": 2, "runtime": 2},
            )

        asyncio.run(scenario())


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
                "mirrorChyanCDK": "synthetic-discovery-cdk",
                "candidate": {
                    "source": "mirrorchyan",
                    "version": "2.0.0",
                    "download_url": "https://download.example/pkg.zip?cdk=secret",
                    "sourceConfig": {"cdk": "synthetic-candidate-cdk"},
                    "sha256": "a" * 64,
                },
            }
        )

        self.assertNotIn("download_url", public["candidate"])
        self.assertNotIn("downloadUrl", public["candidate"])
        self.assertTrue(public["candidate"]["downloadAvailable"])
        self.assertEqual(public["candidate"]["sha256"], "a" * 64)
        serialized = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("synthetic-discovery-cdk", serialized)
        self.assertNotIn("synthetic-candidate-cdk", serialized)

    def test_remote_source_config_inherits_host_cdk_only_for_mirror(self) -> None:
        original_config = self.module.Config

        class HostConfig:
            calls = []

            @classmethod
            def get(cls, group, name):
                cls.calls.append((group, name))
                return "synthetic-host-cdk"

        self.module.Config = HostConfig
        try:
            mirror = self.module._remote_source_config(
                {"source": "MirrorChyan", "channel": "stable"}
            )
            github = self.module._remote_source_config(
                {"source": "GitHub", "githubRepo": "owner/project"}
            )
        finally:
            self.module.Config = original_config

        self.assertEqual(mirror["cdk"], "synthetic-host-cdk")
        self.assertEqual(HostConfig.calls, [("Update", "MirrorChyanCDK")])
        self.assertNotIn("cdk", github)

    def test_remote_source_config_explicit_cdk_overrides_host(self) -> None:
        original_config = self.module.Config

        class HostConfig:
            @staticmethod
            def get(_group, _name):
                raise AssertionError("显式 CDK 不应读取宿主全局配置")

        self.module.Config = HostConfig
        try:
            source = self.module._remote_source_config(
                {
                    "source": "MirrorChyan",
                    "mirrorChyanCDK": "synthetic-request-cdk",
                }
            )
        finally:
            self.module.Config = original_config

        self.assertEqual(source["cdk"], "synthetic-request-cdk")

    def test_mirror_check_allows_blank_cdk_but_install_requires_url(self) -> None:
        original_config = self.module.Config

        class HostConfig:
            @staticmethod
            def get(_group, _name):
                return ""

        self.module.Config = HostConfig
        try:
            source = self.module._remote_source_config({"source": "MirrorChyan"})
        finally:
            self.module.Config = original_config
        self.assertEqual(source["cdk"], "")

        plugin = self.module.Plugin(self._context())

        async def discover(_script_id, _payload):
            return {
                "installable": False,
                "candidate": None,
                "unavailableReason": "MirrorChyan 未返回下载地址",
            }

        plugin._discover_remote_project = discover
        with self.assertRaisesRegex(self.module.ManagedServiceError, "未返回下载地址"):
            asyncio.run(
                plugin._download_and_import_remote(
                    "11111111-1111-4111-8111-111111111111",
                    {"source": "MirrorChyan", "mirrorChyanRid": "demo"},
                    initial=True,
                )
            )

    def test_remote_initial_import_releases_package_after_config_commit(self) -> None:
        config = self._fake_config(manual_user=False)
        config.script.config["Managed"].update(
            {
                "ProjectId": "",
                "Version": "",
                "ImportProjectId": "m9a",
            }
        )
        config.script.config["Info"]["Path"] = ""
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        context = self._context()
        plugin = self.module.Plugin(context)
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh
        release_observations: list[dict] = []

        async def discover(_script_id, _payload):
            return {
                "latestVersion": "2.0",
                "installable": True,
                "candidate": {
                    "source": "github_release",
                    "version": "2.0",
                    "download_url": "https://example.invalid/m9a.zip",
                },
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            package_path = Path(temporary_directory) / "remote.zip"
            content = b"remote initial package"
            package_path.write_bytes(content)
            downloaded = {
                "source": "github_release",
                "version": "2.0",
                "path": str(package_path),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }

            async def download(_root, _candidate, *, progress=None):
                del progress
                return dict(downloaded)

            async def release(_root, package):
                release_observations.append(
                    {
                        "version": config.script.config["Managed"]["Version"],
                        "remote": copy.deepcopy(
                            config.script.config.get("ManagedRemote", {})
                        ),
                        "transaction": config.transactions[-1],
                        "package": dict(package),
                    }
                )
                package_path.unlink()
                return {"released": True, "retained": False}

            gateway.download_remote_package = download
            gateway.release_remote_package = release
            plugin._discover_remote_project = discover

            result = asyncio.run(
                plugin._download_and_import_remote(
                    self.script_id,
                    {"projectId": "m9a", "source": "github_release"},
                    initial=True,
                )
            )

            self.assertFalse(package_path.exists())

        self.assertEqual(release_observations[0]["version"], "2.0")
        self.assertEqual(release_observations[0]["transaction"][0], "exit")
        self.assertTrue(
            release_observations[0]["remote"]["LastDownload"]["retained"]
        )
        self.assertEqual(config.script.config["Managed"]["SourceArchive"], "")
        last_download = config.script.config["ManagedRemote"]["LastDownload"]
        self.assertFalse(last_download["retained"])
        self.assertEqual(last_download["cleanupStatus"], "released")
        self.assertFalse(result["download"]["retained"])
        self.assertNotIn("path", result["download"])
        self.assertNotIn("path", last_download)

    def test_remote_cleanup_failure_keeps_import_success_and_reports_retained(
        self,
    ) -> None:
        config = self._fake_config(manual_user=False)
        config.script.config["Managed"].update(
            {
                "ProjectId": "",
                "Version": "",
                "ImportProjectId": "m9a",
            }
        )
        config.script.config["Info"]["Path"] = ""
        self.module.Config = config
        warnings: list[str] = []
        context = self._context()
        context.logger.warning = lambda message: warnings.append(str(message))
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        plugin = self.module.Plugin(context)
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh

        async def discover(_script_id, _payload):
            return {
                "latestVersion": "2.0",
                "installable": True,
                "candidate": {
                    "source": "github_release",
                    "version": "2.0",
                    "download_url": "https://example.invalid/m9a.zip",
                },
            }

        async def download(_root, _candidate, *, progress=None):
            del progress
            return {
                "source": "github_release",
                "version": "2.0",
                "path": "C:/managed-downloads/transient.zip",
                "size": 10,
                "sha256": "d" * 64,
            }

        async def fail_release(_root, _package):
            raise RuntimeError("synthetic cleanup failure")

        gateway.download_remote_package = download
        gateway.release_remote_package = fail_release
        plugin._discover_remote_project = discover

        result = asyncio.run(
            plugin._download_and_import_remote(
                self.script_id,
                {"projectId": "m9a", "source": "github_release"},
                initial=True,
            )
        )

        self.assertEqual(config.script.config["Managed"]["Version"], "2.0")
        self.assertEqual(config.script.config["Managed"]["SourceArchive"], "")
        self.assertTrue(result["download"]["retained"])
        self.assertEqual(result["download"]["cleanupStatus"], "cleanup-failed")
        self.assertNotIn("path", result["download"])
        last_download = config.script.config["ManagedRemote"]["LastDownload"]
        self.assertTrue(last_download["retained"])
        self.assertEqual(last_download["cleanupStatus"], "cleanup-failed")
        self.assertNotIn("path", last_download)
        self.assertTrue(any("释放临时下载包失败" in item for item in warnings))

    def test_remote_postcommit_cleanup_finishes_before_cancellation_reraises(
        self,
    ) -> None:
        config = self._fake_config(manual_user=False)
        config.script.config["Managed"].update(
            {
                "ProjectId": "",
                "Version": "",
                "ImportProjectId": "m9a",
            }
        )
        config.script.config["Info"]["Path"] = ""
        self.module.Config = config
        gateway = self._Gateway(self.old_project, self.new_project, config.events)
        context = self._context()
        plugin = self.module.Plugin(context)
        plugin._gateway = lambda: gateway
        plugin._refresh_project_versions_and_references = self._no_refresh

        async def discover(_script_id, _payload):
            return {
                "latestVersion": "2.0",
                "installable": True,
                "candidate": {
                    "source": "github_release",
                    "version": "2.0",
                    "download_url": "https://example.invalid/m9a.zip",
                },
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            package_path = Path(temporary_directory) / "remote.zip"
            content = b"postcommit cancellation package"
            package_path.write_bytes(content)
            downloaded = {
                "source": "github_release",
                "version": "2.0",
                "path": str(package_path),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }

            async def scenario():
                release_started = asyncio.Event()
                finish_release = asyncio.Event()

                async def download(_root, _candidate, *, progress=None):
                    del progress
                    return dict(downloaded)

                async def release(_root, _package):
                    self.assertEqual(
                        config.script.config["Managed"]["Version"],
                        "2.0",
                    )
                    release_started.set()
                    await finish_release.wait()
                    package_path.unlink()
                    return {"released": True, "retained": False}

                gateway.download_remote_package = download
                gateway.release_remote_package = release
                plugin._discover_remote_project = discover
                task = asyncio.create_task(
                    plugin._download_and_import_remote(
                        self.script_id,
                        {"projectId": "m9a", "source": "github_release"},
                        initial=True,
                    )
                )
                await release_started.wait()
                task.cancel()
                await asyncio.sleep(0)
                pending_after_cancel = not task.done()
                finish_release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                return pending_after_cancel

            pending_after_cancel = asyncio.run(scenario())
            self.assertTrue(pending_after_cancel)
            self.assertFalse(package_path.exists())

        self.assertEqual(config.script.config["Managed"]["Version"], "2.0")
        last_download = config.script.config["ManagedRemote"]["LastDownload"]
        self.assertFalse(last_download["retained"])
        self.assertEqual(last_download["cleanupStatus"], "released")
        self.assertNotIn("path", last_download)

    def test_remote_upgrade_apply_and_cancel_do_not_depend_on_released_package(
        self,
    ) -> None:
        for decision in ("apply", "cancel"):
            with self.subTest(decision=decision):
                config = self._fake_config(manual_user=False)
                self.module.Config = config
                gateway = self._Gateway(
                    self.old_project,
                    self.new_project,
                    config.events,
                )
                context = self._context()
                plugin = self.module.Plugin(context)
                plugin._gateway = lambda: gateway
                plugin._refresh_project_versions_and_references = self._no_refresh

                async def discover(_script_id, _payload):
                    return {
                        "latestVersion": "2.0",
                        "installable": True,
                        "candidate": {
                            "source": "github_release",
                            "version": "2.0",
                            "download_url": "https://example.invalid/m9a.zip",
                        },
                    }

                with tempfile.TemporaryDirectory() as temporary_directory:
                    package_path = Path(temporary_directory) / "upgrade.zip"
                    content = b"remote upgrade package"
                    package_path.write_bytes(content)
                    downloaded = {
                        "source": "github_release",
                        "version": "2.0",
                        "path": str(package_path),
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }

                    async def download(_root, _candidate, *, progress=None):
                        del progress
                        return dict(downloaded)

                    async def release(_root, _package):
                        package_path.unlink()
                        return {"released": True, "retained": False}

                    async def upgrade(payload):
                        self.assertEqual(payload["sourceArchive"], str(package_path))
                        self.assertTrue(package_path.is_file())
                        return {
                            "updated": True,
                            "activated": False,
                            "currentVersion": "1.0",
                            "latestVersion": "2.0",
                            "previousProject": self.old_project,
                            "project": self.new_project,
                        }

                    gateway.download_remote_package = download
                    gateway.release_remote_package = release
                    gateway.upgrade_project = upgrade
                    plugin._discover_remote_project = discover
                    staged = asyncio.run(
                        plugin._download_and_import_remote(
                            self.script_id,
                            {
                                "projectId": "m9a",
                                "source": "github_release",
                            },
                            initial=False,
                        )
                    )

                    self.assertFalse(package_path.exists())
                    pending = copy.deepcopy(
                        config.script.config["Managed"]["PendingUpgrade"]
                    )
                    serialized = json.dumps(
                        {
                            "pending": pending,
                            "remote": config.script.config["ManagedRemote"],
                            "result": staged,
                        },
                        ensure_ascii=False,
                    )
                    self.assertNotIn(str(package_path), serialized)

                    restarted = self.module.Plugin(context)
                    restarted._gateway = lambda: gateway
                    restarted._refresh_project_versions_and_references = (
                        self._no_refresh
                    )
                    if decision == "apply":
                        decided = asyncio.run(
                            restarted._apply_pending_upgrade_transaction(
                                self.script_id,
                                {
                                    "planId": pending["planId"],
                                    "confirmation": pending["confirmationToken"],
                                },
                            )
                        )
                        self.assertTrue(decided["applied"])
                        self.assertEqual(
                            config.script.config["Managed"]["Version"],
                            "2.0",
                        )
                    else:
                        decided = asyncio.run(
                            restarted._cancel_pending_upgrade_transaction(
                                self.script_id
                            )
                        )
                        self.assertTrue(decided["cancelled"])
                        self.assertEqual(
                            config.script.config["Managed"]["Version"],
                            "1.0",
                        )

    def test_remote_secrets_are_redacted_from_http_progress_and_persistence(self) -> None:
        plugin = self.module.Plugin(self._context())
        script_id = "11111111-1111-4111-8111-111111111111"
        operation_id = f"{script_id}:import-remote:redaction"
        secrets = {
            "synthetic-path-token",
            "synthetic-query-cdk",
            "synthetic-bearer-token",
            "synthetic-secret-value",
        }
        failure = (
            "download failed: "
            "https://mirrorchyan.com/api/resources/download/synthetic-path-token"
            "?cdk=synthetic-query-cdk; "
            "Authorization: Bearer synthetic-bearer-token; "
            "secret=synthetic-secret-value"
        )
        published = []
        persisted = []

        class FakePublisher:
            @staticmethod
            async def send(*, id, type, data):
                published.append((id, type, copy.deepcopy(dict(data))))
                return True

        class PersistConfig:
            @staticmethod
            async def update_script(received_script_id, update):
                persisted.append((received_script_id, copy.deepcopy(update)))

        async def fail():
            raise self.module.ManagedServiceError(failure)

        ws_module = types.ModuleType("app.core.ws")
        ws_module.Publisher = FakePublisher
        previous_ws = sys.modules.get("app.core.ws")
        original_config = self.module.Config
        sys.modules["app.core.ws"] = ws_module
        self.module.Config = PersistConfig

        async def scenario():
            response = await plugin._respond_with_progress(
                {"scriptId": script_id, "progressId": operation_id},
                "import-remote",
                "正在导入远程资源",
                fail,
            )
            progress = await plugin._read_progress(
                SimpleNamespace(
                    json={"scriptId": script_id, "operationId": operation_id},
                    query={},
                )
            )
            plain_response = await plugin._respond(fail)
            await plugin._persist_remote_result(
                script_id,
                {
                    "latestVersion": "2.0.0",
                    "installable": True,
                    "cdk": "synthetic-query-cdk",
                    "candidate": {
                        "download_url": failure,
                        "secret": "synthetic-secret-value",
                    },
                },
                status="远程检查完成",
                downloaded={
                    "source": "mirrorchyan",
                    "version": "2.0.0",
                    "path": "C:/safe/package.zip",
                    "size": 10,
                    "token": "synthetic-bearer-token",
                },
            )
            return response, progress, plain_response

        try:
            response, progress, plain_response = asyncio.run(scenario())
        finally:
            self.module.Config = original_config
            if previous_ws is None:
                sys.modules.pop("app.core.ws", None)
            else:
                sys.modules["app.core.ws"] = previous_ws

        self.assertEqual(response["code"], 400, response)
        self.assertEqual(progress["code"], 200, progress)
        self.assertEqual(plain_response["code"], 400, plain_response)
        serialized = json.dumps(
            [response, progress, plain_response, published, persisted],
            ensure_ascii=False,
        )
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertIn("***", serialized)
        self.assertNotIn("C:/safe/package.zip", serialized)

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
                self.stopped_reasons = []

            async def on_start(self):
                return None

            async def on_stop(self, reason):
                self.stopped_reasons.append(reason)

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


class ManagedInPlaceConversionTest(unittest.TestCase):
    script_id = "11111111-1111-4111-8111-111111111111"
    user_ids = (
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    )

    def setUp(self) -> None:
        self.module = ManagedUpgradeStateMachineTest._load_plugin_module()

    def test_capabilities_fail_closed_without_host_primitives(self) -> None:
        class UnsupportedConfig:
            pass

        self.module.Config = UnsupportedConfig
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: self.fail("fail-closed path must not import")
        capabilities = asyncio.run(plugin._capabilities(None))
        self.assertEqual(capabilities["code"], 200)
        self.assertEqual(
            capabilities["data"]["apiVersion"],
            "maafw-managed.v1",
        )
        self.assertIsInstance(
            capabilities["data"]["distributionVersion"],
            str,
        )
        self.assertFalse(
            capabilities["data"]["features"]["inPlaceConversion"]
        )
        self.assertTrue(capabilities["data"]["features"]["pinning"])
        self.assertTrue(
            capabilities["data"]["features"]["garbageCollection"]
        )
        self.assertTrue(
            capabilities["data"]["features"]["operationProgress"]
        )
        self.assertTrue(
            capabilities["data"]["features"]["activeOperationLookup"]
        )
        self.assertTrue(
            capabilities["data"]["features"]["serverMutationExclusion"]
        )
        self.assertTrue(capabilities["data"]["serverEpoch"])

        response = asyncio.run(
            plugin._convert_project(self._request({"scriptId": self.script_id}))
        )
        self.assertEqual(response["code"], 400)
        self.assertIn("原子脚本类型转换", response["message"])

    def test_conversion_operation_id_includes_target_identity(self) -> None:
        build = self.module._conversion_operation_id
        first = build(self.script_id, "source-hash", "m9a", "1.0", "==5.10.4")
        retry = build(self.script_id, "source-hash", "m9a", "1.0", "==5.10.4")
        different_version = build(
            self.script_id,
            "source-hash",
            "m9a",
            "2.0",
            "==5.10.4",
        )
        different_project = build(
            self.script_id,
            "source-hash",
            "other",
            "1.0",
            "==5.10.4",
        )
        different_runtime = build(
            self.script_id,
            "source-hash",
            "m9a",
            "1.0",
            "==5.11.0",
        )

        self.assertEqual(first, retry)
        self.assertEqual(len({first, different_version, different_project, different_runtime}), 4)

    def test_conversion_fails_fast_when_source_project_is_busy(self) -> None:
        config = self._fake_config()
        gateway = self._Gateway(config.events)
        self.module.Config = config
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        original_reserve = self.module.try_reserve_project_path
        original_release = self.module.release_project_path
        release_calls: list[str | None] = []

        async def reserve(path):
            self.assertEqual(path, str(ROOT))
            return None

        async def release(key):
            release_calls.append(key)

        self.module.try_reserve_project_path = reserve
        self.module.release_project_path = release
        try:
            response = asyncio.run(
                plugin._convert_project(
                    self._request({"scriptId": self.script_id})
                )
            )
        finally:
            self.module.try_reserve_project_path = original_reserve
            self.module.release_project_path = original_release

        self.assertEqual(response["code"], 400, response)
        self.assertIn("项目正在运行、准备或更新", response["message"])
        self.assertEqual(gateway.imports, 0)
        self.assertEqual(release_calls, [])

    def test_conversion_releases_source_path_after_resource_transaction(self) -> None:
        config = self._fake_config()
        gateway = self._Gateway(config.events)
        self.module.Config = config
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway
        original_reserve = self.module.try_reserve_project_path
        original_release = self.module.release_project_path

        async def reserve(path):
            self.assertEqual(path, str(ROOT))
            config.events.append("path:reserve")
            return "reserved-project"

        async def release(key):
            self.assertEqual(key, "reserved-project")
            config.events.append("path:release")

        self.module.try_reserve_project_path = reserve
        self.module.release_project_path = release
        try:
            response = asyncio.run(
                plugin._convert_project(
                    self._request({"scriptId": self.script_id})
                )
            )
        finally:
            self.module.try_reserve_project_path = original_reserve
            self.module.release_project_path = original_release

        self.assertEqual(response["code"], 200, response)
        self.assertLess(
            config.events.index("resource:exit"),
            config.events.index("path:release"),
        )

    def test_conversion_preserves_ids_order_config_and_stable_reference(self) -> None:
        config = self._fake_config()
        gateway = self._Gateway(config.events)
        self.module.Config = config
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway

        @asynccontextmanager
        async def upgrade_lock():
            config.events.append("upgrade:enter")
            try:
                yield
            finally:
                config.events.append("upgrade:exit")

        plugin._upgrade_lock = lambda _script_id: upgrade_lock()
        original_user_objects = list(config.users)
        original_user_configs = [copy.deepcopy(user.config) for user in config.users]

        response = asyncio.run(
            plugin._convert_project(self._request({"scriptId": self.script_id}))
        )

        self.assertEqual(response["code"], 200, response)
        data = response["data"]
        self.assertTrue(data["converted"])
        self.assertEqual(data["scriptId"], self.script_id)
        self.assertEqual(data["fromType"], "MaaFW")
        self.assertEqual(data["toType"], "MaaFWManaged")
        self.assertEqual(data["userIds"], list(self.user_ids))
        self.assertEqual(config.script.id, self.script_id)
        self.assertEqual(config.script.type, "MaaFWManaged")
        self.assertEqual(config.users, original_user_objects)
        self.assertEqual([user.id for user in config.users], list(self.user_ids))
        for source, converted in zip(
            original_user_configs,
            (user.config for user in config.users),
            strict=True,
        ):
            self.assertEqual(
                self.module._upgrade_source_config(converted),
                self.module._upgrade_source_config(source),
            )
        self.assertEqual(
            config.script.config["Info"]["Path"],
            "C:/store/m9a/1.0",
        )
        marker = config.script.config["Managed"]["ConversionJournal"]
        self.assertEqual(marker["state"], "committed")
        self.assertEqual(marker["scriptId"], self.script_id)
        self.assertEqual(
            gateway.references,
            [("m9a", "1.0", f"maafw-script:{self.script_id}")],
        )
        self.assertLess(
            config.events.index("config:snapshot:exit"),
            config.events.index("resource:enter"),
        )
        self.assertLess(
            config.events.index("resource:enter"),
            config.events.index("upgrade:enter"),
        )
        self.assertLess(
            config.events.index("upgrade:enter"),
            config.events.index("config:commit:enter"),
        )
        self.assertLess(
            config.events.index("config:commit:enter"),
            config.events.index("convert"),
        )
        self.assertLess(
            config.events.index("convert"),
            config.events.index("config:commit:exit"),
        )
        self.assertLess(
            config.events.index("config:commit:exit"),
            config.events.index("upgrade:exit"),
        )
        self.assertLess(
            config.events.index("upgrade:exit"),
            config.events.index("resource:exit"),
        )

        second = asyncio.run(
            plugin._convert_project(self._request({"scriptId": self.script_id}))
        )
        self.assertEqual(second["code"], 200, second)
        self.assertTrue(second["data"]["idempotent"])
        self.assertEqual(gateway.imports, 1)

    def test_precommit_failure_releases_project_reference(self) -> None:
        config = self._fake_config(conversion_mode="fail")
        gateway = self._Gateway(config.events)
        self.module.Config = config
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway

        response = asyncio.run(
            plugin._convert_project(self._request({"scriptId": self.script_id}))
        )

        self.assertEqual(response["code"], 400, response)
        self.assertIn("已释放项目引用", response["message"])
        self.assertEqual(config.script.type, "MaaFW")
        self.assertEqual(
            gateway.releases,
            [("m9a", "1.0", f"maafw-script:{self.script_id}")],
        )

    def test_uncertain_commit_keeps_reference_for_recovery(self) -> None:
        config = self._fake_config(conversion_mode="uncertain")
        gateway = self._Gateway(config.events)
        self.module.Config = config
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway

        response = asyncio.run(
            plugin._convert_project(self._request({"scriptId": self.script_id}))
        )

        self.assertEqual(response["code"], 400, response)
        self.assertIn("已保留项目引用", response["message"])
        self.assertEqual(gateway.releases, [])

    def test_source_changed_cas_releases_inactive_project_reference(self) -> None:
        config = self._fake_config(conversion_mode="source_changed")
        gateway = self._Gateway(config.events)
        self.module.Config = config
        plugin = self.module.Plugin(self._context())
        plugin._gateway = lambda: gateway

        response = asyncio.run(
            plugin._convert_project(self._request({"scriptId": self.script_id}))
        )

        self.assertEqual(response["code"], 400, response)
        self.assertIn("配置未提交且已释放项目引用", response["message"])
        self.assertEqual(config.script.type, "MaaFW")
        self.assertEqual(gateway.current_version, "existing")
        self.assertFalse(gateway.import_payloads[0]["activate"])
        self.assertEqual(
            gateway.releases,
            [("m9a", "1.0", f"maafw-script:{self.script_id}")],
        )

    def test_progress_is_pollable_isolated_and_terminal_once(self) -> None:
        plugin = self.module.Plugin(self._context())
        operation_id = f"{self.script_id}:import-local:1:1"
        other_script_id = "44444444-4444-4444-8444-444444444444"
        published: list[tuple[str, str, dict]] = []

        class FakePublisher:
            @staticmethod
            async def send(*, id, type, data):
                published.append((id, type, copy.deepcopy(dict(data))))
                return True

        ws_module = types.ModuleType("app.core.ws")
        ws_module.Publisher = FakePublisher
        previous_ws = sys.modules.get("app.core.ws")
        sys.modules["app.core.ws"] = ws_module

        async def scenario():
            async def operation():
                callback = plugin._download_progress_callback()
                assert callback is not None
                callback(
                    {
                        "stage": "downloading",
                        "downloaded_bytes": 12,
                        "total_bytes": 24,
                        "percent": 50,
                    }
                )
                await plugin._flush_progress_updates()
                await plugin._progress_stage(
                    "project-import",
                    "正在导入不可变项目资源",
                    percent=70,
                )
                return {"imported": True}

            response = await plugin._respond_with_progress(
                {
                    "scriptId": self.script_id,
                    "progressId": operation_id,
                },
                "import-local",
                "正在导入本地项目资源",
                operation,
            )
            progress = await plugin._read_progress(
                self._request(
                    {
                        "scriptId": self.script_id,
                        "operationId": operation_id,
                    }
                )
            )
            isolated = await plugin._read_progress(
                self._request(
                    {
                        "scriptId": other_script_id,
                        "operationId": operation_id,
                    }
                )
            )
            duplicate = await plugin._respond_with_progress(
                {
                    "scriptId": self.script_id,
                    "progressId": operation_id,
                },
                "import-local",
                "不得覆盖旧操作",
                operation,
            )
            invalid = await plugin._respond_with_progress(
                {
                    "scriptId": self.script_id,
                    "progressId": "../not-safe",
                },
                "import-local",
                "非法进度 ID",
                operation,
            )
            await plugin._finish_progress(
                {
                    "scriptId": self.script_id,
                    "operationId": operation_id,
                },
                "error",
                "不得覆盖 success 终态",
            )
            return response, progress, isolated, duplicate, invalid

        try:
            response, progress, isolated, duplicate, invalid = asyncio.run(
                scenario()
            )
        finally:
            if previous_ws is None:
                sys.modules.pop("app.core.ws", None)
            else:
                sys.modules["app.core.ws"] = previous_ws

        self.assertEqual(response["code"], 200, response)
        self.assertEqual(progress["code"], 200, progress)
        state = progress["data"]
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["percent"], 100)
        self.assertEqual(state["operation"], "import-local")
        self.assertEqual(state["downloadedBytes"], 12)
        self.assertEqual(state["totalBytes"], 24)
        self.assertEqual(isolated["code"], 404)
        self.assertEqual(duplicate["code"], 400)
        self.assertEqual(invalid["code"], 400)
        self.assertEqual(
            plugin._progress_states[operation_id]["status"],
            "success",
        )
        terminal = [
            item
            for item in published
            if item[2].get("operationId") == operation_id
            and item[2].get("status") in {"success", "error"}
        ]
        self.assertEqual({item[0] for item in terminal}, {self.script_id, operation_id})
        self.assertTrue(all(item[1] == "maafw.managed.progress" for item in terminal))
        self.assertTrue(all(item[2]["status"] == "success" for item in terminal))

    def test_active_operation_is_queryable_and_same_script_conflicts(self) -> None:
        plugin = self.module.Plugin(self._context())
        first_operation_id = f"{self.script_id}:import-local:active:1"
        second_operation_id = f"{self.script_id}:pin:active:2"

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()
            second_called = False

            async def first_operation():
                started.set()
                await release.wait()
                return {"imported": True}

            async def second_operation():
                nonlocal second_called
                second_called = True
                return {"pinned": True}

            first_task = asyncio.create_task(
                plugin._respond_with_progress(
                    {
                        "scriptId": self.script_id,
                        "progressId": first_operation_id,
                    },
                    "import-local",
                    "正在导入本地项目资源",
                    first_operation,
                )
            )
            await started.wait()
            active = await plugin._read_active_operation(
                self._request({"scriptId": self.script_id})
            )
            conflict = await plugin._respond_with_progress(
                {
                    "scriptId": self.script_id,
                    "progressId": second_operation_id,
                },
                "pin",
                "正在固定资源",
                second_operation,
            )
            remote_check_conflict = await plugin._check_remote_project(
                self._request(
                    {
                        "scriptId": self.script_id,
                        "progressId": f"{self.script_id}:remote-check:active:3",
                        "source": "GitHub",
                    }
                )
            )
            release.set()
            first = await first_task
            inactive = await plugin._read_active_operation(
                self._request({"scriptId": self.script_id})
            )
            return (
                active,
                conflict,
                remote_check_conflict,
                first,
                inactive,
                second_called,
            )

        active, conflict, remote_check_conflict, first, inactive, second_called = (
            asyncio.run(scenario())
        )

        self.assertEqual(active["code"], 200, active)
        active_data = active["data"]
        self.assertEqual(active_data["serverEpoch"], plugin._server_epoch)
        self.assertEqual(
            active_data["activeOperation"]["operationId"],
            first_operation_id,
        )
        self.assertEqual(
            active_data["activeOperation"]["serverEpoch"],
            plugin._server_epoch,
        )
        self.assertEqual(conflict["code"], 409, conflict)
        self.assertEqual(remote_check_conflict["code"], 409, remote_check_conflict)
        self.assertEqual(
            conflict["data"]["activeOperation"]["operationId"],
            first_operation_id,
        )
        self.assertFalse(second_called)
        self.assertEqual(first["code"], 200, first)
        self.assertIsNone(inactive["data"]["activeOperation"])

    def test_progress_capacity_never_evicts_an_active_operation(self) -> None:
        plugin = self.module.Plugin(self._context())
        script_two = "44444444-4444-4444-8444-444444444444"
        script_three = "55555555-5555-4555-8555-555555555555"
        terminal_id = f"{self.script_id}:terminal"
        active_id = f"{script_two}:active"
        replacement_id = f"{script_three}:replacement"
        original_limit = self.module._PROGRESS_MAX_STATES
        self.module._PROGRESS_MAX_STATES = 2

        async def scenario():
            terminal = await plugin._begin_progress(
                {"scriptId": self.script_id, "progressId": terminal_id},
                "pin",
                "terminal",
            )
            await plugin._finish_progress(terminal, "success", "done")
            active = await plugin._begin_progress(
                {"scriptId": script_two, "progressId": active_id},
                "import-local",
                "active",
            )
            replacement = await plugin._begin_progress(
                {"scriptId": script_three, "progressId": replacement_id},
                "gc-apply",
                "replacement",
            )
            snapshot = copy.deepcopy(plugin._progress_states)
            await plugin._finish_progress(active, "success", "done")
            await plugin._finish_progress(replacement, "success", "done")
            return snapshot

        try:
            states = asyncio.run(scenario())
        finally:
            self.module._PROGRESS_MAX_STATES = original_limit

        self.assertNotIn(terminal_id, states)
        self.assertIn(active_id, states)
        self.assertEqual(states[active_id]["status"], "running")
        self.assertIn(replacement_id, states)

    def test_gc_excludes_requesting_script_but_keeps_other_active_scripts(
        self,
    ) -> None:
        plugin = self.module.Plugin(self._context())
        other_script_id = "44444444-4444-4444-8444-444444444444"
        plugin._active_operations = {
            self.script_id: "requesting-gc",
            other_script_id: "other-operation",
        }
        received: list[dict[str, object]] = []

        class Gateway:
            @asynccontextmanager
            async def resource_transaction(self):
                yield

            async def collect_garbage(self, **kwargs):
                received.append(dict(kwargs))
                return {"collected": True}

        class Config:
            @classmethod
            @asynccontextmanager
            async def script_config_write_scope(cls, script_id):
                del cls
                self.assertIsNone(script_id)
                yield

        async def script_records():
            return []

        original_config = self.module.Config
        original_script_records = self.module._managed_script_record_dtos
        self.module.Config = Config
        self.module._managed_script_record_dtos = script_records
        plugin._gateway = Gateway
        try:
            result = asyncio.run(
                plugin._collect_garbage_with_script_references(
                    {},
                    dry_run=False,
                    requesting_script_id=self.script_id,
                )
            )
        finally:
            self.module.Config = original_config
            self.module._managed_script_record_dtos = original_script_records

        self.assertEqual(result, {"collected": True})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["active_script_ids"], [other_script_id])
        self.assertTrue(received[0]["checkout_gc_confirmed"])

    def test_on_stop_drains_active_operation_and_rejects_new_mutation(self) -> None:
        plugin = self.module.Plugin(self._context())
        operation_id = f"{self.script_id}:install-runtime:drain"

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()

            async def operation():
                started.set()
                await release.wait()
                return {"installed": True}

            request_task = asyncio.create_task(
                plugin._respond_with_progress(
                    {
                        "scriptId": self.script_id,
                        "progressId": operation_id,
                    },
                    "install-runtime",
                    "正在安装共享运行时",
                    operation,
                )
            )
            await started.wait()
            stop_task = asyncio.create_task(plugin.on_stop("test-drain"))
            await asyncio.sleep(0)
            rejected = await plugin._respond_with_progress(
                {
                    "scriptId": self.script_id,
                    "progressId": f"{self.script_id}:pin:after-drain",
                },
                "pin",
                "正在固定资源",
                lambda: asyncio.sleep(0),
            )
            stop_pending = not stop_task.done()
            release.set()
            response = await request_task
            await stop_task
            active = await plugin._read_active_operation(
                self._request({"scriptId": self.script_id})
            )
            return rejected, stop_pending, response, active

        rejected, stop_pending, response, active = asyncio.run(scenario())

        self.assertEqual(rejected["code"], 503, rejected)
        self.assertTrue(stop_pending)
        self.assertEqual(response["code"], 200, response)
        self.assertIsNone(active["data"]["activeOperation"])
        self.assertTrue(active["data"]["draining"])
        self.assertEqual(plugin.stopped_reasons, ["test-drain"])

    def test_on_stop_drains_non_progress_request_and_rejects_new_request(
        self,
    ) -> None:
        plugin = self.module.Plugin(self._context())

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()

            async def operation():
                started.set()
                await release.wait()
                return {"inventoried": True}

            request_task = asyncio.create_task(plugin._respond(operation))
            await started.wait()
            stop_task = asyncio.create_task(plugin.on_stop("test-read-drain"))
            await asyncio.sleep(0)
            rejected = await plugin._respond(lambda: asyncio.sleep(0))
            stop_pending = not stop_task.done()
            release.set()
            response = await request_task
            await stop_task
            return rejected, stop_pending, response

        rejected, stop_pending, response = asyncio.run(scenario())

        self.assertEqual(rejected["code"], 503, rejected)
        self.assertTrue(stop_pending)
        self.assertEqual(response["code"], 200, response)
        self.assertEqual(response["data"], {"inventoried": True})
        self.assertEqual(plugin.stopped_reasons, ["test-read-drain"])

    def test_failure_terminal_is_published_after_lock_exit(self) -> None:
        plugin = self.module.Plugin(self._context())
        operation_id = f"{self.script_id}:upgrade-local:1:2"
        events: list[str] = []

        class FakePublisher:
            @staticmethod
            async def send(*, id, type, data):
                del id, type
                if data.get("status") in {"success", "error"}:
                    events.append(f"terminal:{data['status']}")
                return True

        ws_module = types.ModuleType("app.core.ws")
        ws_module.Publisher = FakePublisher
        previous_ws = sys.modules.get("app.core.ws")
        sys.modules["app.core.ws"] = ws_module

        @asynccontextmanager
        async def locked_operation():
            events.append("lock:enter")
            try:
                yield
            finally:
                events.append("lock:exit")

        async def operation():
            async with locked_operation():
                raise self.module.ManagedServiceError("预期失败")

        try:
            response = asyncio.run(
                plugin._respond_with_progress(
                    {
                        "scriptId": self.script_id,
                        "progressId": operation_id,
                    },
                    "upgrade-local",
                    "正在导入升级资源",
                    operation,
                )
            )
        finally:
            if previous_ws is None:
                sys.modules.pop("app.core.ws", None)
            else:
                sys.modules["app.core.ws"] = previous_ws

        self.assertEqual(response["code"], 400, response)
        self.assertLess(events.index("lock:exit"), events.index("terminal:error"))
        self.assertEqual(events.count("terminal:error"), 2)

    def test_request_cancellation_waits_for_unlock_and_records_success(self) -> None:
        plugin = self.module.Plugin(self._context())
        operation_id = f"{self.script_id}:install-runtime:1:3"
        events: list[str] = []

        class FakePublisher:
            @staticmethod
            async def send(*, id, type, data):
                del id, type
                if data.get("status") in {"success", "error"}:
                    events.append(f"terminal:{data['status']}")
                return True

        ws_module = types.ModuleType("app.core.ws")
        ws_module.Publisher = FakePublisher
        previous_ws = sys.modules.get("app.core.ws")
        sys.modules["app.core.ws"] = ws_module

        @asynccontextmanager
        async def locked_operation():
            events.append("lock:enter")
            try:
                yield
            finally:
                events.append("lock:exit")

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()

            async def operation():
                async with locked_operation():
                    started.set()
                    await release.wait()
                    events.append("mutation:done")
                    return {"installed": True}

            request_task = asyncio.create_task(
                plugin._respond_with_progress(
                    {
                        "scriptId": self.script_id,
                        "progressId": operation_id,
                    },
                    "install-runtime",
                    "正在安装共享运行时",
                    operation,
                )
            )
            await started.wait()
            request_task.cancel()
            await asyncio.sleep(0)
            progress_during = await plugin._read_progress(
                self._request(
                    {
                        "scriptId": self.script_id,
                        "operationId": operation_id,
                    }
                )
            )
            pending_after_cancel = not request_task.done()
            release.set()
            reraised = False
            try:
                await request_task
            except asyncio.CancelledError:
                reraised = True
            progress = await plugin._read_progress(
                self._request(
                    {
                        "scriptId": self.script_id,
                        "operationId": operation_id,
                    }
                )
            )
            return reraised, pending_after_cancel, progress_during, progress

        try:
            reraised, pending_after_cancel, progress_during, progress = (
                asyncio.run(scenario())
            )
        finally:
            if previous_ws is None:
                sys.modules.pop("app.core.ws", None)
            else:
                sys.modules["app.core.ws"] = previous_ws

        self.assertTrue(reraised)
        self.assertTrue(pending_after_cancel)
        self.assertEqual(progress_during["data"]["status"], "running")
        self.assertNotIn("lock:exit", events[: events.index("mutation:done")])
        self.assertEqual(progress["code"], 200, progress)
        self.assertEqual(progress["data"]["status"], "success")
        self.assertLess(events.index("mutation:done"), events.index("lock:exit"))
        self.assertLess(events.index("lock:exit"), events.index("terminal:success"))
        self.assertEqual(events.count("terminal:success"), 2)

    def test_sync_mutation_cancellation_keeps_lock_until_worker_finishes(self) -> None:
        services = ScriptMaaFWManagedContractTest._load_services_module()
        release = threading.Event()
        events: list[str] = []

        async def scenario():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()

            def mutation():
                events.append("mutation:start")
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=2):
                    raise TimeoutError("test worker was not released")
                events.append("mutation:done")
                return {"mutated": True}

            @asynccontextmanager
            async def locked_operation():
                events.append("lock:enter")
                try:
                    yield
                finally:
                    events.append("lock:exit")

            async def invoke():
                async with locked_operation():
                    return await services._invoke(
                        mutation,
                        (),
                        {},
                        "测试同步变更",
                    )

            request_task = asyncio.create_task(invoke())
            await asyncio.wait_for(started.wait(), timeout=1)
            request_task.cancel()
            await asyncio.sleep(0)
            pending_after_cancel = not request_task.done()
            lock_held_after_cancel = "lock:exit" not in events
            release.set()
            reraised = False
            try:
                await request_task
            except asyncio.CancelledError:
                reraised = True
            return reraised, pending_after_cancel, lock_held_after_cancel

        reraised, pending_after_cancel, lock_held_after_cancel = asyncio.run(
            scenario()
        )

        self.assertTrue(reraised)
        self.assertTrue(pending_after_cancel)
        self.assertTrue(lock_held_after_cancel)
        self.assertLess(events.index("mutation:done"), events.index("lock:exit"))

    def test_running_publish_cancellation_cannot_leak_begin_state(self) -> None:
        plugin = self.module.Plugin(self._context())
        operation_id = f"{self.script_id}:import-local:1:4"
        operation_called = False

        async def publish(state):
            if state.get("status") == "running":
                raise asyncio.CancelledError

        plugin._publish_progress = publish

        async def operation():
            nonlocal operation_called
            operation_called = True
            return {"imported": True}

        async def scenario():
            reraised = False
            try:
                await plugin._respond_with_progress(
                    {
                        "scriptId": self.script_id,
                        "progressId": operation_id,
                    },
                    "import-local",
                    "正在导入本地项目资源",
                    operation,
                )
            except asyncio.CancelledError:
                reraised = True
            return reraised, copy.deepcopy(plugin._progress_states[operation_id])

        reraised, progress = asyncio.run(scenario())

        self.assertTrue(reraised)
        self.assertFalse(operation_called)
        self.assertEqual(progress["status"], "error")
        self.assertEqual(progress["message"], "操作已取消")

    def test_success_terminal_survives_cancellation_during_publish(self) -> None:
        plugin = self.module.Plugin(self._context())
        operation_id = f"{self.script_id}:pin:1:5"

        async def scenario():
            terminal_publish_started = asyncio.Event()
            release_terminal_publish = asyncio.Event()

            async def publish(state):
                if state.get("status") == "success":
                    terminal_publish_started.set()
                    await release_terminal_publish.wait()

            plugin._publish_progress = publish

            async def operation():
                return {"pinned": True}

            request_task = asyncio.create_task(
                plugin._respond_with_progress(
                    {
                        "scriptId": self.script_id,
                        "progressId": operation_id,
                    },
                    "pin",
                    "正在固定资源",
                    operation,
                )
            )
            await terminal_publish_started.wait()
            decided = copy.deepcopy(plugin._progress_states[operation_id])
            request_task.cancel()
            await asyncio.sleep(0)
            pending_after_cancel = not request_task.done()
            release_terminal_publish.set()
            reraised = False
            try:
                await request_task
            except asyncio.CancelledError:
                reraised = True
            final = copy.deepcopy(plugin._progress_states[operation_id])
            return reraised, pending_after_cancel, decided, final

        reraised, pending_after_cancel, decided, final = asyncio.run(scenario())

        self.assertTrue(reraised)
        self.assertTrue(pending_after_cancel)
        self.assertEqual(decided["status"], "success")
        self.assertEqual(decided["percent"], 100)
        self.assertEqual(final["status"], "success")
        self.assertNotEqual(final["message"], "操作已取消")

    def test_remote_download_forwards_stream_progress_callback(self) -> None:
        services = ScriptMaaFWManagedContractTest._load_services_module()
        received: list[dict] = []
        callback_events: list[dict] = []

        class ProjectUpdate:
            async def download_package(
                self,
                download_root,
                candidate,
                *,
                progress=None,
            ):
                received.append(
                    {
                        "downloadRoot": download_root,
                        "candidate": candidate,
                        "progress": progress,
                    }
                )
                assert progress is not None
                progress(
                    {
                        "stage": "downloading",
                        "downloaded_bytes": 12,
                        "total_bytes": 24,
                        "percent": 50,
                    }
                )
                return {
                    "path": str(ROOT / "pyproject.toml"),
                    "size": 24,
                }

        gateway = services.ManagedServiceGateway(
            object(),
            object(),
            ProjectUpdate(),
        )

        def marker(event):
            callback_events.append(dict(event))

        result = asyncio.run(
            gateway.download_remote_package(
                ROOT,
                {"source": "github", "version": "1.0"},
                progress=marker,
            )
        )

        self.assertEqual(result["size"], 24)
        self.assertIs(received[0]["progress"], marker)
        self.assertEqual(callback_events[0]["downloaded_bytes"], 12)

    def test_remote_release_forwards_exact_root_and_package(self) -> None:
        services = ScriptMaaFWManagedContractTest._load_services_module()
        received: list[dict] = []
        package = {
            "source": "github_release",
            "version": "1.0",
            "path": str(ROOT / "pyproject.toml"),
            "size": 24,
            "sha256": "a" * 64,
        }

        class ProjectUpdate:
            async def release_download_package(self, download_root, received_package):
                received.append(
                    {
                        "downloadRoot": download_root,
                        "package": dict(received_package),
                    }
                )
                return {"released": True, "retained": False}

        gateway = services.ManagedServiceGateway(
            object(),
            object(),
            ProjectUpdate(),
        )
        result = asyncio.run(gateway.release_remote_package(ROOT, package))

        self.assertEqual(result, {"released": True, "retained": False})
        self.assertEqual(received[0]["downloadRoot"], ROOT)
        self.assertEqual(received[0]["package"], package)

    def _fake_config(self, *, conversion_mode: str = "success"):
        script = SimpleNamespace(
            id=self.script_id,
            type="MaaFW",
            name="M9A ordinary",
            config={
                "Info": {
                    "Name": "M9A ordinary",
                    "Path": str(ROOT),
                    "ProjectLabel": "m9a",
                    "Controller": "ADB",
                    "Resource": "Official",
                },
                "Run": {"RunTimesLimit": 3},
            },
        )
        users = [
            SimpleNamespace(
                id=self.user_ids[0],
                type="MaaFW",
                name="one",
                config={
                    "Info": {"Name": "one", "Status": True},
                    "Task": {"TaskSnapshot": {"daily": True}},
                    "Data": {
                        "LastProxyStatus": "成功",
                        "PeriodTaskRecords": {"daily": "2026-08-02"},
                    },
                },
            ),
            SimpleNamespace(
                id=self.user_ids[1],
                type="MaaFW",
                name="two",
                config={
                    "Info": {"Name": "two", "Status": False},
                    "Task": {"TaskSnapshot": {"weekly": True}},
                    "Data": {
                        "LastProxyStatus": "失败",
                        "PeriodTaskRecords": {"weekly": "2026-W31"},
                    },
                },
            ),
        ]

        def snapshot():
            return {
                "script": {
                    "id": script.id,
                    "type": script.type,
                    "name": script.name,
                    "config": copy.deepcopy(script.config),
                },
                "userOrder": [user.id for user in users],
                "users": {
                    user.id: {
                        "id": user.id,
                        "type": user.type,
                        "name": user.name,
                        "config": copy.deepcopy(user.config),
                    }
                    for user in users
                },
            }

        initial_snapshot = snapshot()

        class FakeConfig:
            events: list[str] = []

            @classmethod
            def script_config_transaction(cls, script_id, *, owner):
                assert script_id == script.id
                expected_prefix = f"maafw-managed-convert-"
                assert owner.startswith(expected_prefix)
                assert owner.endswith(f":{script.id}")
                phase = (
                    "snapshot"
                    if "-snapshot:" in owner
                    else "commit"
                    if "-commit:" in owner
                    else ""
                )
                assert phase

                class Transaction:
                    async def __aenter__(self):
                        cls.events.append(f"config:{phase}:enter")

                    async def __aexit__(self, exc_type, exc, traceback):
                        del exc_type, exc, traceback
                        cls.events.append(f"config:{phase}:exit")

                return Transaction()

            @classmethod
            async def get_script_records(cls, script_id=None):
                return [script] if script_id in (None, script.id) else []

            @classmethod
            async def get_user_records(cls, script_id, user_id=None):
                assert script_id == script.id
                return (
                    users
                    if user_id is None
                    else [user for user in users if user.id == user_id]
                )

            @classmethod
            async def get_plugin_script_type_conversion_snapshot(cls, script_id):
                assert script_id == script.id
                return snapshot()

            @classmethod
            async def convert_plugin_script_type(
                cls,
                script_id,
                *,
                source_type,
                target_type,
                expected_snapshot,
                target_script_config,
                target_user_configs,
                journal,
            ):
                assert script_id == script.id
                assert source_type == "MaaFW"
                assert target_type == "MaaFWManaged"
                assert expected_snapshot == initial_snapshot
                assert journal["sourceFingerprint"] == self.module._json_hash(
                    expected_snapshot
                )
                cls.events.append("convert")
                if conversion_mode == "fail":
                    raise RuntimeError("host rejected before commit")
                if conversion_mode == "source_changed":
                    error = RuntimeError("host source-current CAS rejected")
                    error.conversion_state = "source_changed"
                    raise error
                if conversion_mode == "uncertain":
                    script.type = target_type
                    raise RuntimeError("host interrupted after replace")
                script.type = target_type
                script.config = copy.deepcopy(dict(target_script_config))
                for user in users:
                    user.type = target_type
                    user.config = copy.deepcopy(target_user_configs[user.id])
                return {
                    "converted": True,
                    "idempotent": False,
                    "recovered": False,
                }

        FakeConfig.script = script
        FakeConfig.users = users
        FakeConfig.snapshot = initial_snapshot
        return FakeConfig

    def _context(self):
        return SimpleNamespace(
            get=lambda _key: None,
            logger=SimpleNamespace(
                warning=lambda *_args, **_kwargs: None,
                error=lambda *_args, **_kwargs: None,
            ),
        )

    @staticmethod
    def _request(payload):
        return SimpleNamespace(json=payload, query={})

    class _Gateway:
        def __init__(self, events):
            self.events = events
            self.references: list[tuple[str, str, str]] = []
            self.releases: list[tuple[str, str, str]] = []
            self.import_payloads: list[dict] = []
            self.current_version = "existing"
            self.imports = 0

        @asynccontextmanager
        async def resource_transaction(self):
            self.events.append("resource:enter")
            try:
                yield
            finally:
                self.events.append("resource:exit")

        async def load_interface(self, source_path):
            assert source_path == str(ROOT)
            return {"name": "m9a", "version": "1.0"}

        async def import_project(self, payload):
            self.imports += 1
            self.import_payloads.append(copy.deepcopy(dict(payload)))
            assert payload["projectReference"].startswith("maafw-script:")
            if payload.get("activate", True):
                self.current_version = "1.0"
            return {
                "projectId": "m9a",
                "version": "1.0",
                "dataPath": "C:/store/m9a/1.0",
                "runtimeConstraint": "==5.10.4",
                "manifest": {"projectId": "m9a", "version": "1.0"},
            }

        async def add_project_reference(self, project_id, version, reference):
            self.references.append((project_id, version, reference))

        async def release_project_reference(self, project_id, version, reference):
            self.releases.append((project_id, version, reference))


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
