from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from automas_maafw_runner.environment import (
    build_runner_packages,
    requirement_distribution_name,
)


PROJECT_STORE_SERVICE = "maafw.project_store.v1"
RUNTIME_POOL_SERVICE = "maafw.runtime_pool.v1"
PROJECT_UPDATE_SERVICE = "maafw.project_update.v1"
INTERFACE_SERVICE = "maafw.interface.v1"


class ManagedServiceError(RuntimeError):
    """A concise, user-facing managed-resource contract failure."""


class ManagedServiceGateway:
    """Keep service-version tolerance at one JSON-friendly boundary."""

    def __init__(
        self,
        project_store: Any,
        runtime_pool: Any,
        project_update: Any = None,
        interface_service: Any = None,
    ) -> None:
        if project_store is None:
            raise ManagedServiceError(f"缺少服务 {PROJECT_STORE_SERVICE}")
        if runtime_pool is None:
            raise ManagedServiceError(f"缺少服务 {RUNTIME_POOL_SERVICE}")
        self.project_store = project_store
        self.runtime_pool = runtime_pool
        self.project_update = project_update
        self.interface_service = interface_service

    @asynccontextmanager
    async def resource_transaction(self) -> AsyncIterator[None]:
        """Serialize project reference reconciliation and destructive GC."""

        transaction = getattr(
            self.project_store,
            "resource_lifecycle_transaction",
            None,
        )
        if not callable(transaction):
            raise ManagedServiceError(
                "maafw.project_store.v1 未提供资源生命周期事务"
            )
        try:
            async with transaction():
                yield
        except ManagedServiceError:
            raise
        except Exception as exc:
            raise ManagedServiceError(f"MaaFW 资源事务失败：{exc}") from exc

    async def resolve_execution(self, request: Mapping[str, Any]) -> dict[str, Any]:
        project_id = _required_text(request, "projectId", "项目 ID")
        requested_version = _optional_text(request.get("version"))
        project = await self.resolve_project(project_id, requested_version)
        project_path = _project_path(project)

        constraint = (
            _optional_text(request.get("runtimeConstraint"))
            or _optional_text(project.get("runtimeConstraint"))
            or _manifest_runtime_constraint(project.get("manifest"))
        )
        manifest_binding = _manifest_runtime_binding(project.get("manifest"))
        bound_runtime_id = _optional_text(manifest_binding.get("runtimeId"))
        if not constraint and not bound_runtime_id:
            raise ManagedServiceError(
                "项目未声明 runtimeConstraint，资源清单也没有已绑定 runtimeId；"
                "拒绝创建未约束的 MaaFW 运行时。请先声明版本约束或绑定已验证运行时。"
            )
        requirements = (
            _runner_requirements(project_path, constraint)
            if constraint
            else []
        )
        runtime_request = {
            "touch": True,
            "metadata": {
                "component": "automas-script-maafw-managed",
                "projectId": project_id,
                "version": str(project.get("version") or requested_version or ""),
                "channel": _optional_text(request.get("channel")),
                "projectPath": project_path,
            },
        }
        if requirements:
            runtime_request["requirements"] = requirements
        if bound_runtime_id:
            runtime_request["runtimeId"] = bound_runtime_id
        runtime = await self.resolve_runtime(runtime_request)
        recovered_binding = False
        if runtime is None:
            bound_maafw_version = _optional_text(
                manifest_binding.get("maafwVersion")
            )
            if bound_runtime_id and bound_maafw_version:
                runtime_request = dict(runtime_request)
                runtime_request.pop("runtimeId", None)
                runtime_request["requirements"] = _runner_requirements(
                    project_path,
                    bound_maafw_version,
                )
                runtime_request["metadata"] = {
                    **dict(runtime_request.get("metadata") or {}),
                    "recoveredFromRuntimeId": bound_runtime_id,
                    "recoveredMaaFWVersion": bound_maafw_version,
                }
                runtime = await self.resolve_runtime(runtime_request)
                if runtime is None:
                    runtime = await self.ensure_runtime(runtime_request)
                recovered_binding = True
            elif not constraint:
                raise ManagedServiceError(
                    f"资源清单绑定的运行时 {bound_runtime_id} 不存在；"
                    "且项目没有 runtimeConstraint，无法安全重建。"
                )
            else:
                # Stale runtimeId 不匹配当前 constraint 计算出的 selector；
                # 清理后用 constraint 重建运行时，并标记需要回写新绑定。
                runtime_request = dict(runtime_request)
                runtime_request.pop("runtimeId", None)
                runtime_request["metadata"] = {
                    **dict(runtime_request.get("metadata") or {}),
                    "recoveredFromRuntimeId": bound_runtime_id,
                }
                runtime = await self.ensure_runtime(runtime_request)
                recovered_binding = True

        runtime_id = _optional_text(runtime.get("runtimeId"))
        python_executable = _optional_text(runtime.get("pythonExecutable"))
        if not runtime_id or not python_executable:
            raise ManagedServiceError(
                "运行时服务返回值缺少 runtimeId 或 pythonExecutable"
            )
        _validate_python_abi(project, runtime)
        _validate_platform_arch(project, runtime)
        if recovered_binding:
            project = await self.bind_project_runtime(
                project_id,
                str(project.get("version") or requested_version or "") or None,
                runtime,
                project_reference=_optional_text(request.get("projectReference")),
            )
        return {
            "project": project,
            "runtime": runtime,
            "projectPath": project_path,
            "runtimeConstraint": constraint or "",
            "runtimeRequest": runtime_request,
        }

    async def resolve_project(
        self,
        project_id: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "projectId": project_id,
            "version": version,
            "touch": True,
        }
        value = await _call_variants(
            self.project_store,
            ("resolve_project", "resolve"),
            (
                ((project_id, version), {"touch": True}),
                ((project_id, version), {}),
                ((project_id,), {"version": version, "touch": True}),
                ((payload,), {}),
            ),
            operation="解析 MaaFW 项目",
        )
        project = _as_dict(value, "project_store resolve")
        _project_path(project)
        return project

    async def resolve_runtime(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        method = getattr(self.runtime_pool, "resolve_runtime", None)
        if callable(method):
            value = await _invoke(method, (dict(request),), {}, "解析 MaaFW 运行时")
        else:
            if request.get("runtimeId") and not request.get("requirements"):
                raise ManagedServiceError(
                    "当前运行时服务缺少 resolve_runtime(request)，"
                    "无法按已绑定 runtimeId 安全解析"
                )
            value = await _call_variants(
                self.runtime_pool,
                ("resolve",),
                (
                    ((list(request.get("requirements") or []),), {"touch": True}),
                    ((list(request.get("requirements") or []),), {}),
                ),
                operation="解析 MaaFW 运行时",
            )
        if value is None:
            return None
        return _as_dict(value, "runtime_pool resolve")

    async def ensure_runtime(self, request: Mapping[str, Any]) -> dict[str, Any]:
        method = getattr(self.runtime_pool, "ensure_runtime", None)
        if callable(method):
            value = await _invoke(method, (dict(request),), {}, "安装 MaaFW 运行时")
        else:
            value = await _call_variants(
                self.runtime_pool,
                ("ensure",),
                (
                    (
                        (list(request.get("requirements") or []),),
                        {"metadata": dict(request.get("metadata") or {})},
                    ),
                    ((list(request.get("requirements") or []),), {}),
                ),
                operation="安装 MaaFW 运行时",
            )
        return _as_dict(value, "runtime_pool ensure")

    async def list_runtimes(self) -> list[dict[str, Any]]:
        value = await _call_variants(
            self.runtime_pool,
            ("list_runtimes", "list"),
            (((), {}),),
            operation="列出 MaaFW 共享运行时",
        )
        return _as_dict_list(value, "runtime_pool list")

    async def list_versions(self, project_id: str) -> list[dict[str, Any]]:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ManagedServiceError("列出版本需要项目 ID")
        value = await _call_variants(
            self.project_store,
            ("list_versions",),
            (
                ((normalized_project_id,), {}),
                ((({"projectId": normalized_project_id}),), {}),
            ),
            operation=f"列出 MaaFW 项目 {normalized_project_id} 的版本",
        )
        return _as_dict_list(value, "project_store list versions")

    async def list_projects(self) -> list[dict[str, Any]]:
        value = await _call_variants(
            self.project_store,
            ("list_projects", "list"),
            (((), {}),),
            operation="列出 MaaFW 托管资源",
        )
        return _as_dict_list(value, "project_store list projects")

    async def load_interface(self, project_path: str) -> dict[str, Any]:
        if self.interface_service is None:
            raise ManagedServiceError(f"缺少服务 {INTERFACE_SERVICE}")
        value = await _call_variants(
            self.interface_service,
            ("load",),
            (
                ((project_path,), {"force_reload": False}),
                ((project_path,), {}),
            ),
            operation="读取托管 MaaFW ProjectInterface",
        )
        return _as_dict(value, "maafw.interface.v1 load")

    async def discover_remote_update(
        self,
        interface: Mapping[str, Any],
        *,
        current_version: str,
        source_config: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if self.project_update is None:
            raise ManagedServiceError(f"缺少服务 {PROJECT_UPDATE_SERVICE}")
        method = getattr(self.project_update, "discover_update", None)
        if not callable(method):
            raise ManagedServiceError(
                f"{PROJECT_UPDATE_SERVICE} 未提供 discover_update"
            )
        value = await _invoke(
            method,
            (dict(interface),),
            {
                "current_version": current_version,
                "source_config": dict(source_config),
            },
            "发现 MaaFW 远程资源",
        )
        if value is None:
            return None
        return _as_dict(value, "maafw.project_update.v1 discover_update")

    async def download_remote_package(
        self,
        download_root: str | Path,
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.project_update is None:
            raise ManagedServiceError(f"缺少服务 {PROJECT_UPDATE_SERVICE}")
        method = getattr(self.project_update, "download_package", None)
        if not callable(method):
            raise ManagedServiceError(
                f"{PROJECT_UPDATE_SERVICE} 未提供 download_package；"
                "请升级 automas-maafw-project-update"
            )
        value = await _invoke(
            method,
            (Path(download_root), dict(candidate)),
            {},
            "下载 MaaFW 远程资源包",
        )
        package = _as_dict(value, "maafw.project_update.v1 download_package")
        path = _required_text(package, "path", "远程下载包路径")
        if not Path(path).is_file():
            raise ManagedServiceError("远程下载服务未返回可读取的本地 ZIP")
        return package

    async def delete_runtime(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        runtime_id = _required_text(payload, "runtimeId", "运行时 ID")
        if _optional_text(payload.get("confirmation")) != runtime_id:
            raise ManagedServiceError(
                f"删除前请在确认字段中完整输入运行时 ID：{runtime_id}"
            )
        value = await _call_variants(
            self.runtime_pool,
            ("delete", "delete_runtime"),
            (
                ((runtime_id,), {}),
                ((({"runtimeId": runtime_id}),), {}),
            ),
            operation="删除 MaaFW 共享运行时",
        )
        result = _as_dict(value, "runtime_pool delete")
        if result.get("deleted") is not True:
            blocked = result.get("blocked") or ["unknown"]
            raise ManagedServiceError(
                f"运行时 {runtime_id} 未删除，保护原因：{blocked}。"
                "请先解除固定、项目引用或活动 lease。"
            )
        return result

    async def import_project(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        source_path = _local_source_path(payload)
        project_id = _required_text(payload, "projectId", "项目 ID")
        version = _optional_text(payload.get("version"))
        runtime_constraint = _optional_text(payload.get("runtimeConstraint"))
        project_reference = _project_script_reference(
            _optional_text(payload.get("projectReference"))
        )
        request = {
            "sourcePath": source_path,
            "projectId": project_id,
            "version": version,
            "runtimeConstraint": runtime_constraint,
            "activate": True,
            "pinned": False,
            "reference": project_reference,
        }
        value = await _call_variants(
            self.project_store,
            ("import_project", "import_version", "import_release"),
            (
                (
                    (source_path, project_id, version),
                    {
                        "runtime_constraint": runtime_constraint,
                        "activate": True,
                        "pinned": False,
                        "reference": project_reference,
                    },
                ),
                ((request,), {}),
            ),
            operation="导入 MaaFW 项目",
        )
        return _as_dict(value, "project_store import")

    async def upgrade_project(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Import a caller-supplied folder/ZIP as a new immutable version.

        Remote downloads are deliberately handled by Project Update before
        this method is called.  The imported version remains inactive until
        the host has generated and explicitly applied every script/user
        configuration plan.
        """

        source_path = _local_source_path(payload)
        project_id = _required_text(payload, "projectId", "项目 ID")
        version = _optional_text(payload.get("version"))
        current = await self.resolve_project(
            project_id,
            _optional_text(payload.get("currentVersion")),
        )
        current_version = _optional_text(current.get("version"))
        runtime_constraint = (
            _optional_text(payload.get("runtimeConstraint"))
            or _optional_text(current.get("runtimeConstraint"))
            or _manifest_runtime_constraint(current.get("manifest"))
        )
        project_reference = _project_script_reference(
            _optional_text(payload.get("projectReference"))
        )
        request = {
            "sourcePath": source_path,
            "projectId": project_id,
            "version": version,
            "runtimeConstraint": runtime_constraint,
            "activate": False,
            "pinned": False,
            "reference": project_reference,
        }
        imported = await _call_variants(
            self.project_store,
            ("update_project", "import_project"),
            (
                (
                    (source_path, project_id, version),
                    {
                        "runtime_constraint": runtime_constraint,
                        "activate": False,
                        "pinned": False,
                        "reference": project_reference,
                    },
                ),
                ((request,), {}),
            ),
            operation="从本地文件夹或 ZIP 导入 MaaFW 新资源版本",
        )
        project = _as_dict(imported, "project_store local upgrade")
        imported_version = _required_text(project, "version", "导入后的资源版本")
        if current_version and imported_version == current_version:
            raise ManagedServiceError(
                "本地升级必须导入不同于当前版本的新资源；"
                f"当前版本与导入版本均为 {current_version}"
            )
        return {
            "updated": True,
            "activated": False,
            "currentVersion": current_version,
            "latestVersion": imported_version,
            "sourcePath": source_path,
            "previousProject": current,
            "project": project,
        }

    async def release_project_reference(
        self,
        project_id: str,
        version: str,
        reference: str,
    ) -> dict[str, Any]:
        value = await _call_variants(
            self.project_store,
            ("release_runtime", "release_reference"),
            (
                (
                    (project_id, version),
                    {
                        "reference": reference,
                        "clear_binding": False,
                    },
                ),
                (
                    (
                        {
                            "projectId": project_id,
                            "version": version,
                            "reference": reference,
                            "clearBinding": False,
                        },
                    ),
                    {},
                ),
            ),
            operation=f"释放 MaaFW 项目引用 {project_id}@{version}",
        )
        return _as_dict(value, "project_store release reference")

    async def add_project_reference(
        self,
        project_id: str,
        version: str,
        reference: str,
    ) -> dict[str, Any]:
        normalized_reference = _project_script_reference(reference)
        value = await _call_variants(
            self.project_store,
            ("bind_runtime", "add_reference"),
            (
                (
                    (project_id, version),
                    {
                        "reference": normalized_reference,
                        "touch": True,
                    },
                ),
                (
                    (
                        {
                            "projectId": project_id,
                            "version": version,
                            "reference": normalized_reference,
                        },
                    ),
                    {},
                ),
            ),
            operation=f"保护 MaaFW 待确认项目版本 {project_id}@{version}",
        )
        return _as_dict(value, "project_store add reference")

    async def switch_version(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        project_id = _required_text(payload, "projectId", "项目 ID")
        version = _required_text(payload, "version", "版本")
        value = await _call_variants(
            self.project_store,
            ("switch_version", "activate_version", "switch"),
            (
                ((project_id, version), {}),
                (({"projectId": project_id, "version": version},), {}),
            ),
            operation="切换 MaaFW 项目版本",
        )
        return _as_dict(value, "project_store switch")

    async def delete_version(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        project_id = _required_text(payload, "projectId", "项目 ID")
        version = _required_text(payload, "version", "版本")
        expected_confirmation = f"{project_id}@{version}"
        if _optional_text(payload.get("confirmation")) != expected_confirmation:
            raise ManagedServiceError(
                f"删除前请在确认字段中输入 {expected_confirmation}"
            )
        project = await self.resolve_project(project_id, version)
        bound_runtime_id = _manifest_runtime_id(project.get("manifest"))
        value = await _call_variants(
            self.project_store,
            ("delete_version", "delete"),
            (
                ((project_id, version), {}),
                (({"projectId": project_id, "version": version},), {}),
            ),
            operation="删除 MaaFW 项目版本",
        )
        result = _as_dict(value, "project_store delete")
        if bound_runtime_id:
            reference = _project_runtime_reference(project_id, version)
            try:
                await _call_variants(
                    self.runtime_pool,
                    ("remove_reference",),
                    (((bound_runtime_id, reference), {}),),
                    operation="清理已删除项目的运行时引用",
                )
            except ManagedServiceError as exc:
                result["referenceCleanupWarning"] = str(exc)
        return result

    async def pin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pinned = bool(payload.get("pinned", True))
        result: dict[str, Any] = {}
        runtime_id = _optional_text(payload.get("runtimeId"))
        if runtime_id:
            value = await _call_variants(
                self.runtime_pool,
                ("pin", "pin_runtime"),
                (
                    ((runtime_id, pinned), {}),
                    (({"runtimeId": runtime_id, "pinned": pinned},), {}),
                ),
                operation="固定 MaaFW 运行时",
            )
            result["runtime"] = _as_dict(value, "runtime_pool pin")

        project_id = _optional_text(payload.get("projectId"))
        version = _optional_text(payload.get("version"))
        bind = getattr(self.project_store, "bind_runtime", None)
        if project_id and callable(bind):
            value = await _call_variants(
                self.project_store,
                ("bind_runtime",),
                (
                    ((project_id, version), {"pinned": pinned}),
                    (
                        (
                            {
                                "projectId": project_id,
                                "version": version,
                                "pinned": pinned,
                            },
                        ),
                        {},
                    ),
                ),
                operation="固定 MaaFW 项目版本",
            )
            result["project"] = _as_dict(value, "project_store pin")

        if not result:
            raise ManagedServiceError("固定操作需要 projectId 或 runtimeId")
        return result

    async def acquire_runtime_lease(
        self,
        runtime_id: str,
        lease_id: str,
        *,
        owner: str,
        ttl_seconds: float,
    ) -> dict[str, Any] | None:
        method = getattr(self.runtime_pool, "acquire_lease", None)
        if not callable(method):
            return None
        value = await _call_variants(
            self.runtime_pool,
            ("acquire_lease",),
            (
                (
                    (runtime_id, lease_id),
                    {"owner": owner, "ttl_seconds": ttl_seconds},
                ),
                (
                    (
                        {
                            "runtimeId": runtime_id,
                            "leaseId": lease_id,
                            "owner": owner,
                            "ttlSeconds": ttl_seconds,
                        },
                    ),
                    {},
                ),
            ),
            operation="锁定 MaaFW 运行时",
        )
        return _as_dict(value, "runtime_pool acquire lease")

    async def bind_project_runtime(
        self,
        project_id: str,
        version: str | None,
        binding: Mapping[str, Any],
        *,
        project_reference: str | None = None,
    ) -> dict[str, Any]:
        runtime_id = _required_text(binding, "runtimeId", "运行时 ID")
        resolved_version = _optional_text(version)
        if not resolved_version:
            raise ManagedServiceError("绑定运行时前必须解析出项目版本")
        reference = _project_runtime_reference(project_id, resolved_version)
        stable_project_reference = _project_script_reference(project_reference)
        previous_project = await self.resolve_project(project_id, resolved_version)
        previous_runtime_id = _manifest_runtime_id(previous_project.get("manifest"))
        await _call_variants(
            self.runtime_pool,
            ("add_reference",),
            (
                ((runtime_id, reference), {}),
                ((({"runtimeId": runtime_id, "reference": reference}),), {}),
            ),
            operation="记录 MaaFW 项目运行时引用",
        )
        value = await _call_variants(
            self.project_store,
            ("bind_runtime", "bind_project_runtime"),
            (
                (
                    (project_id, resolved_version),
                    {
                        "binding": dict(binding),
                        "reference": stable_project_reference,
                        "touch": True,
                    },
                ),
                (
                    (
                        {
                            "projectId": project_id,
                            "version": resolved_version,
                            "binding": dict(binding),
                            "reference": stable_project_reference,
                            "touch": True,
                        },
                    ),
                    {},
                ),
            ),
            operation="绑定 MaaFW 项目运行时",
        )
        result = _as_dict(value, "project_store bind runtime")
        if previous_runtime_id and previous_runtime_id != runtime_id:
            try:
                await _call_variants(
                    self.runtime_pool,
                    ("remove_reference",),
                    (((previous_runtime_id, reference), {}),),
                    operation="清理已替换的 MaaFW 运行时引用",
                )
            except ManagedServiceError as exc:
                result["referenceCleanupWarning"] = str(exc)
        return result

    async def acquire_project_lease(
        self,
        project_id: str,
        version: str | None,
        lease_id: str,
        *,
        owner: str,
        ttl_seconds: float,
    ) -> dict[str, Any]:
        value = await _call_variants(
            self.project_store,
            ("acquire_lease", "acquire_project_lease"),
            (
                (
                    (project_id, version),
                    {
                        "owner": owner,
                        "ttl_seconds": ttl_seconds,
                        "lease_id": lease_id,
                    },
                ),
                (
                    (
                        {
                            "projectId": project_id,
                            "version": version,
                            "owner": owner,
                            "ttlSeconds": ttl_seconds,
                            "leaseId": lease_id,
                        },
                    ),
                    {},
                ),
            ),
            operation="锁定 MaaFW 项目版本",
        )
        return _as_dict(value, "project_store acquire lease")

    async def release_project_lease(
        self,
        project_id: str,
        version: str | None,
        lease_id: str,
    ) -> dict[str, Any]:
        value = await _call_variants(
            self.project_store,
            ("release_lease", "release_project_lease"),
            (
                ((project_id, version), {"lease_id": lease_id}),
                (
                    (
                        {
                            "projectId": project_id,
                            "version": version,
                            "leaseId": lease_id,
                        },
                    ),
                    {},
                ),
            ),
            operation="释放 MaaFW 项目版本",
        )
        return _as_dict(value, "project_store release lease")

    async def release_runtime_lease(
        self,
        runtime_id: str,
        lease_id: str,
    ) -> dict[str, Any] | None:
        method = getattr(self.runtime_pool, "release_lease", None)
        if not callable(method):
            return None
        value = await _call_variants(
            self.runtime_pool,
            ("release_lease",),
            (
                ((runtime_id, lease_id), {}),
                ((({"runtimeId": runtime_id, "leaseId": lease_id}),), {}),
            ),
            operation="释放 MaaFW 运行时",
        )
        return _as_dict(value, "runtime_pool release lease")

    async def collect_garbage(
        self,
        *,
        dry_run: bool,
        grace_days: int,
        keep_latest: int,
        project_id: str | None = None,
        script_records: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        grace_seconds = max(0, int(grace_days)) * 24 * 60 * 60
        keep = max(0, int(keep_latest))
        project_kwargs = {
            "dry_run": bool(dry_run),
            "grace_seconds": grace_seconds,
            "keep_latest": keep,
        }
        if project_id:
            project_kwargs["project_id"] = project_id
        project_reconciliation = None
        if script_records is not None:
            project_reconciliation = await self.reconcile_project_references(
                script_records
            )
        project_result = await _call_variants(
            self.project_store,
            ("collect_garbage", "gc"),
            (
                ((), project_kwargs),
                (
                    (
                        {
                            "dryRun": bool(dry_run),
                            "graceSeconds": grace_seconds,
                            "keepLatest": keep,
                            "projectId": project_id,
                        },
                    ),
                    {},
                ),
            ),
            operation="回收 MaaFW 项目资源",
        )
        reconciliation = None
        if not dry_run:
            reconciliation = await self.reconcile_runtime_references()
        runtime_result = await _call_variants(
            self.runtime_pool,
            ("collect_garbage", "gc"),
            (
                (
                    (),
                    {
                        "dry_run": bool(dry_run),
                        "grace_seconds": grace_seconds,
                        "keep_latest": keep,
                    },
                ),
                (
                    (
                        {
                            "dryRun": bool(dry_run),
                            "graceSeconds": grace_seconds,
                            "keepLatest": keep,
                        },
                    ),
                    {},
                ),
            ),
            operation="回收 MaaFW 运行时",
        )
        return {
            "dryRun": bool(dry_run),
            "projectReferenceReconciliation": project_reconciliation,
            "projectStore": _json_value(project_result),
            "referenceReconciliation": reconciliation,
            "runtimePool": _json_value(runtime_result),
        }

    async def reconcile_project_references(
        self,
        script_records: Sequence[Any],
    ) -> dict[str, Any]:
        expected: dict[tuple[str, str], set[str]] = {}
        for raw_record in script_records:
            record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
            if _optional_text(record.get("type")) != "MaaFWManaged":
                continue
            script_id = _optional_text(record.get("id"))
            config = record.get("config")
            config_data = dict(config) if isinstance(config, Mapping) else {}
            managed = config_data.get("Managed")
            managed_data = dict(managed) if isinstance(managed, Mapping) else {}
            project_id, version = managed_project_identity(managed_data)
            if not script_id or not project_id or not version:
                continue
            expected.setdefault((project_id, version), set()).add(
                f"maafw-script:{script_id}"
            )
            pending = managed_data.get("PendingUpgrade")
            pending_data = dict(pending) if isinstance(pending, Mapping) else {}
            pending_project = pending_data.get("project")
            pending_project_data = (
                dict(pending_project)
                if isinstance(pending_project, Mapping)
                else {}
            )
            pending_version = _optional_text(
                pending_project_data.get("toVersion")
            )
            pending_reference = _optional_text(
                pending_project_data.get("pendingReference")
            )
            pending_project_id = _optional_text(
                pending_project_data.get("projectId")
            )
            if (
                pending_project_id == project_id
                and pending_version
                and pending_reference
                and pending_reference.startswith(
                    f"maafw-upgrade:{script_id}:"
                )
            ):
                expected.setdefault((project_id, pending_version), set()).add(
                    pending_reference
                )

        projects_value = await _call_variants(
            self.project_store,
            ("list_projects", "list"),
            (((), {}),),
            operation="列出 MaaFW 托管项目",
        )
        projects = _as_dict_list(projects_value, "project_store list projects")
        updated: list[dict[str, Any]] = []
        for project in projects:
            project_id = _optional_text(project.get("projectId"))
            if not project_id:
                continue
            versions_value = await _call_variants(
                self.project_store,
                ("list_versions",),
                (((project_id,), {}),),
                operation=f"列出 MaaFW 项目 {project_id} 的版本",
            )
            versions = _as_dict_list(
                versions_value,
                "project_store list versions",
            )
            for version_record in versions:
                version = _optional_text(version_record.get("version"))
                if not version:
                    continue
                current_references = {
                    str(item).strip()
                    for item in version_record.get("references") or []
                    if isinstance(item, str) and item.strip()
                }
                external_references = {
                    item
                    for item in current_references
                    if not item.startswith(
                        ("maafw-script:", "maafw-upgrade:")
                    )
                }
                references = sorted(
                    external_references | expected.get((project_id, version), set())
                )
                value = await _call_variants(
                    self.project_store,
                    ("set_references",),
                    (
                        ((project_id, version, references), {}),
                        (
                            (
                                {
                                    "projectId": project_id,
                                    "version": version,
                                    "references": references,
                                },
                            ),
                            {},
                        ),
                    ),
                    operation=f"对账 MaaFW 项目引用 {project_id}@{version}",
                )
                updated.append(_as_dict(value, "project_store set references"))
        return {
            "scriptCount": len(script_records),
            "updated": updated,
        }

    async def reconcile_runtime_references(self) -> dict[str, Any]:
        projects_value = await _call_variants(
            self.project_store,
            ("list_projects", "list"),
            (((), {}),),
            operation="列出 MaaFW 托管项目",
        )
        projects = _as_dict_list(projects_value, "project_store list projects")
        expected: dict[str, set[str]] = {}
        for project in projects:
            project_id = _optional_text(project.get("projectId"))
            if not project_id:
                continue
            versions_value = await _call_variants(
                self.project_store,
                ("list_versions",),
                (((project_id,), {}),),
                operation=f"列出 MaaFW 项目 {project_id} 的版本",
            )
            versions = _as_dict_list(
                versions_value,
                "project_store list versions",
            )
            for version_record in versions:
                version = _optional_text(version_record.get("version"))
                runtime_id = _manifest_runtime_id(version_record.get("manifest"))
                if not version or not runtime_id:
                    continue
                expected.setdefault(runtime_id, set()).add(
                    _project_runtime_reference(project_id, version)
                )

        runtimes_value = await _call_variants(
            self.runtime_pool,
            ("list_runtimes", "list"),
            (((), {}),),
            operation="列出 MaaFW 共享运行时",
        )
        runtimes = _as_dict_list(runtimes_value, "runtime_pool list")
        updated: list[dict[str, Any]] = []
        for runtime in runtimes:
            runtime_id = _optional_text(runtime.get("runtimeId"))
            if not runtime_id:
                continue
            current_references = [
                str(item)
                for item in runtime.get("references") or []
                if isinstance(item, str) and item.strip()
            ]
            external_references = {
                item
                for item in current_references
                if not item.startswith("maafw-project:")
            }
            references = sorted(
                external_references | expected.get(runtime_id, set())
            )
            value = await _call_variants(
                self.runtime_pool,
                ("reconcile_references", "set_references"),
                (
                    ((runtime_id, references), {}),
                    (
                        (
                            {
                                "runtimeId": runtime_id,
                                "references": references,
                            },
                        ),
                        {},
                    ),
                ),
                operation=f"对账 MaaFW 运行时引用 {runtime_id}",
            )
            updated.append(_as_dict(value, "runtime_pool reconcile references"))
        return {
            "runtimeCount": len(runtimes),
            "updated": updated,
        }


def _runner_requirements(project_path: str, constraint: str | None) -> list[str]:
    requirements = list(build_runner_packages(Path(project_path)))
    if not constraint:
        return requirements
    requirements = [
        item
        for item in requirements
        if requirement_distribution_name(item) != "maafw"
    ]
    requirements.insert(0, _maafw_requirement(constraint))
    return requirements


def _maafw_requirement(constraint: str) -> str:
    value = constraint.strip()
    if not value:
        return "maafw"
    if requirement_distribution_name(value) == "maafw":
        return value
    if value.startswith("v") and value[1:2].isdigit():
        return f"maafw=={value[1:]}"
    if value[0].isdigit():
        return f"maafw=={value}"
    return f"maafw{value}"


def _project_path(project: Mapping[str, Any]) -> str:
    for key in ("dataPath", "projectPath", "path"):
        value = _optional_text(project.get(key))
        if value:
            return value
    raise ManagedServiceError(
        "项目存储服务返回值缺少 dataPath/projectPath/path"
    )


def _manifest_runtime_constraint(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    direct = _optional_text(value.get("runtimeConstraint"))
    if direct:
        return direct
    runtime = value.get("runtime")
    if isinstance(runtime, Mapping):
        return _optional_text(runtime.get("constraint"))
    return None


def _manifest_runtime_id(value: Any) -> str | None:
    return _optional_text(_manifest_runtime_binding(value).get("runtimeId"))


def _manifest_runtime_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping):
        return {}
    binding = runtime.get("binding")
    if not isinstance(binding, Mapping):
        return {}
    return dict(binding)


def _project_runtime_reference(project_id: str, version: str) -> str:
    return f"maafw-project:{project_id}@{version}"


def _project_script_reference(value: str | None) -> str | None:
    reference = _optional_text(value)
    if reference is None:
        return None
    prefixes = ("maafw-script:", "maafw-upgrade:")
    prefix = next(
        (item for item in prefixes if reference.startswith(item)),
        None,
    )
    if prefix is None or not reference[len(prefix):].strip():
        raise ManagedServiceError(
            "项目引用必须使用稳定格式 maafw-script:<scriptId> "
            "或 maafw-upgrade:<scriptId>"
        )
    return reference


def _local_source_path(payload: Mapping[str, Any]) -> str:
    """Resolve the selected local artifact without invoking a downloader."""

    archive = _optional_text(payload.get("sourceArchive"))
    directory = _optional_text(payload.get("sourcePath"))
    source = archive or directory
    if source is None:
        raise ManagedServiceError("请选择待导入的本地 ZIP 或资源目录")
    return source


def _validate_python_abi(
    project: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> None:
    required = _required_python_abi_tags(project.get("manifest"))
    if not required:
        return
    identity = runtime.get("identity")
    identity_data = dict(identity) if isinstance(identity, Mapping) else {}
    runtime_abi = str(
        identity_data.get("pythonAbi")
        or runtime.get("pythonAbi")
        or identity_data.get("cacheTag")
        or runtime.get("cacheTag")
        or ""
    ).strip()
    normalized_runtime = runtime_abi.casefold().replace("_", "-")
    compatible = any(
        _abi_tag_matches(tag, normalized_runtime)
        for tag in required
    )
    if compatible:
        return
    raise ManagedServiceError(
        "项目内 Python agent 的 ABI 与共享运行时不兼容："
        f"项目要求 {sorted(required)}，运行时 identity.pythonAbi={runtime_abi or 'missing'}。"
        "请使用匹配的 Python 运行时重新导入/安装，不能静默跨 ABI 执行。"
    )


def _validate_platform_arch(
    project: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> None:
    manifest = project.get("manifest")
    manifest_data = dict(manifest) if isinstance(manifest, Mapping) else {}
    project_runtime = manifest_data.get("runtime")
    project_runtime_data = (
        dict(project_runtime) if isinstance(project_runtime, Mapping) else {}
    )
    required_platform = _optional_text(
        project_runtime_data.get("platform") or manifest_data.get("platform")
    )
    required_arch = _optional_text(
        project_runtime_data.get("arch")
        or project_runtime_data.get("architecture")
        or manifest_data.get("arch")
        or manifest_data.get("architecture")
    )
    identity = runtime.get("identity")
    identity_data = dict(identity) if isinstance(identity, Mapping) else {}
    runtime_platform = _optional_text(
        identity_data.get("platform") or runtime.get("platform")
    )
    runtime_arch = _optional_text(
        identity_data.get("architecture")
        or identity_data.get("arch")
        or runtime.get("architecture")
        or runtime.get("arch")
    )
    if required_platform and (
        not runtime_platform
        or _platform_family(required_platform) != _platform_family(runtime_platform)
    ):
        raise ManagedServiceError(
            "项目平台与共享运行时不兼容："
            f"项目要求 {required_platform}，运行时 identity.platform="
            f"{runtime_platform or 'missing'}。"
        )
    if required_arch and (
        not runtime_arch
        or _normalized_arch(required_arch) != _normalized_arch(runtime_arch)
    ):
        raise ManagedServiceError(
            "项目架构与共享运行时不兼容："
            f"项目要求 {required_arch}，运行时 identity.architecture="
            f"{runtime_arch or 'missing'}。"
        )


def _platform_family(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    if normalized.startswith(("win", "mingw", "cygwin")):
        return "windows"
    if normalized.startswith("linux"):
        return "linux"
    if normalized.startswith(("darwin", "mac", "osx")):
        return "darwin"
    return normalized.split("-", 1)[0]


def _normalized_arch(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "").replace("_", "")
    if normalized in {"amd64", "x8664", "x64"}:
        return "x86_64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    if normalized in {"x86", "i386", "i486", "i586", "i686"}:
        return "x86"
    return normalized


def _required_python_abi_tags(manifest: Any) -> set[str]:
    if not isinstance(manifest, Mapping):
        return set()
    runtime = manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        return set()
    values: list[Any] = [runtime.get("requiredPythonAbi"), runtime.get("abiTags")]
    agents = runtime.get("agent")
    if isinstance(agents, Sequence) and not isinstance(agents, (str, bytes, bytearray)):
        for agent in agents:
            if isinstance(agent, Mapping):
                values.extend((agent.get("requiredPythonAbi"), agent.get("abiTags")))
    result: set[str] = set()
    for value in values:
        if isinstance(value, str) and value.strip():
            result.add(value.strip().casefold().replace("_", "-"))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            result.update(
                str(item).strip().casefold().replace("_", "-")
                for item in value
                if str(item).strip()
            )
    return result


def _abi_tag_matches(required: str, runtime_abi: str) -> bool:
    if not runtime_abi:
        return False
    if required in runtime_abi:
        return True
    # A compact extension tag (cp312) corresponds to cache tag cpython-312.
    if required.startswith("cp") and required[2:].isdigit():
        return f"cpython-{required[2:]}" in runtime_abi
    if required.startswith("cpython-"):
        parts = required.split("-", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            if f"cpython-{parts[1]}" not in runtime_abi:
                return False
            return len(parts) == 2 or parts[2] in runtime_abi
    return False


async def _call_variants(
    service: Any,
    names: Sequence[str],
    variants: Sequence[tuple[tuple[Any, ...], dict[str, Any]]],
    *,
    operation: str,
) -> Any:
    found = False
    for name in names:
        method = getattr(service, name, None)
        if not callable(method):
            continue
        found = True
        for args, kwargs in variants:
            if not _signature_accepts(method, args, kwargs):
                continue
            return await _invoke(method, args, kwargs, operation)
    if not found:
        raise ManagedServiceError(
            f"{operation}失败：服务未实现 {', '.join(names)}"
        )
    raise ManagedServiceError(f"{operation}失败：服务方法签名不兼容")


def _is_async_callable(method: Any) -> bool:
    target = method
    while isinstance(target, functools.partial):
        target = target.func
    if inspect.iscoroutinefunction(target):
        return True
    call = getattr(target, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


async def _invoke(
    method: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    operation: str,
) -> Any:
    """在事件循环外执行同步服务方法。

    project_store / runtime_pool 的服务方法都是同步 def，内部会做 venv 创建、
    pip install（各 300s 超时）、整树 sha256+copytree、runtime 目录遍历。
    托管适配器与 11 个托管 HTTP 动作全部在宿主事件循环上调用它们，
    直接内联执行会把整个后端卡死数十秒到十分钟，所以同步方法一律走线程池。
    """

    try:
        if _is_async_callable(method):
            value = method(*args, **kwargs)
        else:
            value = await asyncio.to_thread(method, *args, **kwargs)
        if inspect.isawaitable(value):
            value = await value
        return value
    except ManagedServiceError:
        raise
    except Exception as exc:
        raise ManagedServiceError(f"{operation}失败：{exc}") from exc


def _signature_accepts(
    method: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> bool:
    try:
        inspect.signature(method).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return False
    return True


def _as_dict(value: Any, source: str) -> dict[str, Any]:
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        raise ManagedServiceError(f"{source} 必须返回 JSON object")
    return normalized


def _as_dict_list(value: Any, source: str) -> list[dict[str, Any]]:
    normalized = _json_value(value)
    if not isinstance(normalized, list) or not all(
        isinstance(item, dict) for item in normalized
    ):
        raise ManagedServiceError(f"{source} 必须返回 JSON object array")
    return normalized


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return _json_value(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_value(model_dump(mode="json", by_alias=True))
        except TypeError:
            return _json_value(model_dump())
    raise ManagedServiceError(
        f"服务返回了非 JSON/DTO 值：{type(value).__name__}"
    )


def managed_project_identity(managed: Mapping[str, Any]) -> tuple[str, str]:
    """Return the immutable Project Store identity when a manifest is bound."""
    manifest_value = managed.get("ProjectManifest")
    manifest = (
        dict(manifest_value) if isinstance(manifest_value, Mapping) else {}
    )
    manifest_project_id = _optional_text(manifest.get("projectId"))
    manifest_version = _optional_text(manifest.get("version"))
    if manifest_project_id and manifest_version:
        return manifest_project_id, manifest_version
    return (
        _optional_text(managed.get("ProjectId")) or "",
        _optional_text(managed.get("Version")) or "",
    )


def _required_text(
    payload: Mapping[str, Any],
    key: str,
    label: str,
) -> str:
    value = _optional_text(payload.get(key))
    if value:
        return value
    raise ManagedServiceError(f"{label}不能为空（字段 {key}）")


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
