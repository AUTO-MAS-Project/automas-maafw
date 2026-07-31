from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from app.core import Config
from app.plugins import ScriptAdapterRuntime
from automas_script_maafw.adapter import MaaFWAdapterHooks
from automas_script_maafw.runner_task import MaaFWPluginAutoProxyTask

from .services import (
    PROJECT_STORE_SERVICE,
    RUNTIME_POOL_SERVICE,
    ManagedServiceError,
    ManagedServiceGateway,
    managed_project_identity,
)


_RESOLUTION_KEY = "maafw_managed_resolution"
_GATEWAY_KEY = "maafw_managed_gateway"
_LEASE_KEY = "maafw_managed_runtime_lease"
_PROJECT_LEASE_KEY = "maafw_managed_project_lease"
_POLICY_KEY = "maafw_managed_gc_policy"
_MINIMUM_LEASE_TTL_SECONDS = 24 * 60 * 60
_UPGRADE_BLOCKING_STATES = {
    "applying",
    "committing",
    "recovery_required",
    "rollback_failed",
}


class MaaFWManagedAdapterHooks(MaaFWAdapterHooks):
    """Resolve immutable project resources, then delegate to the MaaFW adapter."""

    async def check(self, runtime: ScriptAdapterRuntime) -> str:
        if runtime.mode != "AutoProxy":
            return await super().check(runtime)
        async with self._gateway(runtime).resource_transaction():
            async with runtime.storage.write_transaction():
                try:
                    await self._resolve_and_inject(runtime)
                except ManagedServiceError as exc:
                    await self._write_failure_status(runtime, str(exc))
                    return f"托管 MaaFW 项目不可用：{exc}"
                return await super().check(runtime)

    async def prepare(self, runtime: ScriptAdapterRuntime) -> None:
        # 持住宿主写门直到基础 prepare 锁住脚本配置。升级事务只能完整发生在
        # 本批次之前，或等待到运行锁建立后明确失败，不能插入解析与绑定之间。
        async with self._gateway(runtime).resource_transaction():
            async with runtime.storage.write_transaction():
                resolution = await self._resolve_and_inject(runtime)
                await self._acquire_runtime_lease(runtime, resolution)
                await self._acquire_project_lease(runtime, resolution)
                await self._bind_project_runtime(runtime, resolution)
                await super().prepare(runtime)

        # The legacy MaaFW config intentionally ignores new declarative groups.
        # Attach JSON DTOs explicitly so runner revisions can consume the binding
        # without teaching the legacy ConfigBase model about service classes.
        if runtime.script_config is not None:
            setattr(
                runtime.script_config,
                "maafw_managed_project",
                dict(resolution["project"]),
            )
            setattr(
                runtime.script_config,
                "maafw_managed_runtime_binding",
                dict(resolution["runtime"]),
            )

    async def _update_project_before_run(
        self,
        runtime: ScriptAdapterRuntime,
        script_config: Any,
    ) -> None:
        """Never let the legacy adapter mutate an immutable managed project."""

        del script_config
        self._emit_log(runtime, "托管 MaaFW 项目只通过不可变版本动作更新")

    def run_auto_proxy(self, runtime: ScriptAdapterRuntime) -> MaaFWPluginAutoProxyTask:
        task = super().run_auto_proxy(runtime)
        if not isinstance(task, MaaFWPluginAutoProxyTask):
            raise RuntimeError("MaaFW 托管适配器未获得 MaaFWPluginAutoProxyTask")
        resolution = runtime.extra.get(_RESOLUTION_KEY)
        if isinstance(resolution, Mapping):
            task.maafw_managed_project = dict(resolution.get("project") or {})
            task.maafw_managed_runtime_binding = dict(resolution.get("runtime") or {})
        return task

    async def finalize(self, runtime: ScriptAdapterRuntime) -> None:
        try:
            await super().finalize(runtime)
        finally:
            async with self._gateway(runtime).resource_transaction():
                await self._release_project_lease(runtime)
                await self._release_runtime_lease(runtime)
                await self._auto_collect_garbage(runtime)

    async def on_crash(self, runtime: ScriptAdapterRuntime, error: Exception) -> None:
        async with self._gateway(runtime).resource_transaction():
            await self._release_project_lease(runtime)
            await self._release_runtime_lease(runtime)
        await super().on_crash(runtime, error)

    async def _resolve_and_inject(
        self,
        runtime: ScriptAdapterRuntime,
    ) -> dict[str, Any]:
        script_data = await runtime.storage.read_script_data()
        managed = _mapping(script_data.get("Managed"))
        pending_upgrade = _mapping(managed.get("PendingUpgrade"))
        upgrade_state = str(pending_upgrade.get("state") or "").strip()
        if upgrade_state in _UPGRADE_BLOCKING_STATES:
            raise ManagedServiceError(
                f"资源升级事务处于 {upgrade_state}，"
                "恢复完成前拒绝启动新任务"
            )
        managed_runtime = _mapping(script_data.get("ManagedRuntime"))
        run_config = _mapping(script_data.get("Run"))
        project_id, version = managed_project_identity(managed)
        request = {
            "projectId": project_id,
            "version": version,
            "channel": managed.get("Channel"),
            "runtimeConstraint": managed.get("RuntimeConstraint"),
            "projectReference": _script_reference(runtime),
        }
        gateway = self._gateway(runtime)
        resolution = await gateway.resolve_execution(request)
        project = resolution["project"]
        runtime_binding = resolution["runtime"]
        project_id = str(project.get("projectId") or managed.get("ProjectId") or "")
        version = str(project.get("version") or managed.get("Version") or "")
        runtime_id = str(runtime_binding.get("runtimeId") or "")
        project_label = "@".join(item for item in (project_id, version) if item)

        await runtime.storage.update_script_data(
            {
                "Info": {
                    "Path": resolution["projectPath"],
                    "ProjectLabel": project_label,
                },
                "Managed": {
                    "ImportProjectId": "",
                    "ProjectId": project_id,
                    "Version": version,
                    "RuntimeConstraint": resolution.get("runtimeConstraint") or "",
                    "Status": f"就绪 · {project_label} · {runtime_id}",
                    "ProjectManifest": dict(project.get("manifest") or {}),
                },
                "ManagedRuntime": {
                    "RuntimeId": runtime_id,
                    "PythonExecutable": str(
                        runtime_binding.get("pythonExecutable") or ""
                    ),
                    "VenvPath": str(runtime_binding.get("venvPath") or ""),
                    "RuntimeBinding": dict(runtime_binding),
                },
            }
        )
        runtime.extra[_RESOLUTION_KEY] = resolution
        runtime.extra["maafw_managed_project"] = dict(project)
        runtime.extra["maafw_runtime_binding"] = dict(runtime_binding)
        runtime.extra[_POLICY_KEY] = {
            "autoGC": bool(managed_runtime.get("AutoGC", False)),
            "graceDays": _as_int(managed_runtime.get("GCGraceDays"), 30),
            "keepLatest": _as_int(managed_runtime.get("KeepLatest"), 2),
            "projectId": project_id,
            "leaseTtlSeconds": _lease_ttl_seconds(run_config.get("RunTimeLimit")),
        }
        return resolution

    def _gateway(self, runtime: ScriptAdapterRuntime) -> ManagedServiceGateway:
        cached = runtime.extra.get(_GATEWAY_KEY)
        if isinstance(cached, ManagedServiceGateway):
            return cached
        gateway = ManagedServiceGateway(
            runtime.get_service(PROJECT_STORE_SERVICE),
            runtime.get_service(RUNTIME_POOL_SERVICE),
        )
        runtime.extra[_GATEWAY_KEY] = gateway
        return gateway

    async def _acquire_runtime_lease(
        self,
        runtime: ScriptAdapterRuntime,
        resolution: Mapping[str, Any],
    ) -> None:
        if runtime.extra.get(_LEASE_KEY):
            return
        binding = _mapping(resolution.get("runtime"))
        runtime_id = str(binding.get("runtimeId") or "").strip()
        if not runtime_id:
            raise ManagedServiceError("运行时绑定缺少 runtimeId")
        lease_id = f"maafw-managed-run-{uuid.uuid4().hex}"
        owner = _runtime_owner(runtime)
        await self._gateway(runtime).acquire_runtime_lease(
            runtime_id,
            lease_id,
            owner=owner,
            ttl_seconds=max(
                float(_MINIMUM_LEASE_TTL_SECONDS),
                float(
                    _mapping(runtime.extra.get(_POLICY_KEY)).get(
                        "leaseTtlSeconds",
                        _MINIMUM_LEASE_TTL_SECONDS,
                    )
                ),
            ),
        )
        runtime.extra[_LEASE_KEY] = {
            "runtimeId": runtime_id,
            "leaseId": lease_id,
        }

    async def _acquire_project_lease(
        self,
        runtime: ScriptAdapterRuntime,
        resolution: Mapping[str, Any],
    ) -> None:
        if runtime.extra.get(_PROJECT_LEASE_KEY):
            return
        project = _mapping(resolution.get("project"))
        runtime_lease = runtime.extra.get(_LEASE_KEY)
        project_id = str(project.get("projectId") or "").strip()
        version = str(project.get("version") or "").strip() or None
        lease_id = (
            str(runtime_lease.get("leaseId") or "")
            if isinstance(runtime_lease, Mapping)
            else ""
        )
        if not project_id or not lease_id:
            raise ManagedServiceError("项目 lease 缺少 projectId 或 leaseId")
        ttl_seconds = max(
            float(_MINIMUM_LEASE_TTL_SECONDS),
            float(
                _mapping(runtime.extra.get(_POLICY_KEY)).get(
                    "leaseTtlSeconds",
                    _MINIMUM_LEASE_TTL_SECONDS,
                )
            ),
        )
        await self._gateway(runtime).acquire_project_lease(
            project_id,
            version,
            lease_id,
            owner=_runtime_owner(runtime),
            ttl_seconds=ttl_seconds,
        )
        runtime.extra[_PROJECT_LEASE_KEY] = {
            "projectId": project_id,
            "version": version,
            "leaseId": lease_id,
        }

    async def _bind_project_runtime(
        self,
        runtime: ScriptAdapterRuntime,
        resolution: dict[str, Any],
    ) -> None:
        project = _mapping(resolution.get("project"))
        binding = _mapping(resolution.get("runtime"))
        project_id = str(project.get("projectId") or "").strip()
        version = str(project.get("version") or "").strip() or None
        if not project_id:
            raise ManagedServiceError("项目运行时绑定缺少 projectId")
        bound_project = await self._gateway(runtime).bind_project_runtime(
            project_id,
            version,
            binding,
            project_reference=_script_reference(runtime),
        )
        resolution["project"] = bound_project
        runtime.extra["maafw_managed_project"] = dict(bound_project)
        await runtime.storage.update_script_data(
            {
                "Managed": {
                    "ProjectManifest": dict(bound_project.get("manifest") or {}),
                }
            }
        )

    async def _release_project_lease(self, runtime: ScriptAdapterRuntime) -> None:
        lease = runtime.extra.pop(_PROJECT_LEASE_KEY, None)
        if not isinstance(lease, Mapping):
            return
        project_id = str(lease.get("projectId") or "")
        version = str(lease.get("version") or "") or None
        lease_id = str(lease.get("leaseId") or "")
        if not project_id or not lease_id:
            return
        try:
            await self._gateway(runtime).release_project_lease(
                project_id,
                version,
                lease_id,
            )
        except ManagedServiceError as exc:
            self._emit_log(runtime, f"释放 MaaFW 项目 lease 失败：{exc}")

    async def _release_runtime_lease(self, runtime: ScriptAdapterRuntime) -> None:
        lease = runtime.extra.pop(_LEASE_KEY, None)
        if not isinstance(lease, Mapping):
            return
        runtime_id = str(lease.get("runtimeId") or "")
        lease_id = str(lease.get("leaseId") or "")
        if not runtime_id or not lease_id:
            return
        try:
            await self._gateway(runtime).release_runtime_lease(runtime_id, lease_id)
        except ManagedServiceError as exc:
            self._emit_log(runtime, f"释放共享 MaaFW 运行时 lease 失败：{exc}")

    async def _auto_collect_garbage(self, runtime: ScriptAdapterRuntime) -> None:
        policy = runtime.extra.get(_POLICY_KEY)
        if not isinstance(policy, Mapping) or not policy.get("autoGC"):
            return
        try:
            gateway = self._gateway(runtime)
            async with gateway.resource_transaction():
                async with Config.script_config_write_scope(None):
                    script_records = await _managed_script_record_dtos()
                    result = await gateway.collect_garbage(
                        dry_run=False,
                        grace_days=_as_int(policy.get("graceDays"), 30),
                        keep_latest=_as_int(policy.get("keepLatest"), 2),
                        project_id=str(policy.get("projectId") or "") or None,
                        script_records=script_records,
                    )
            self._emit_log(runtime, f"MaaFW 过期资源回收完成：{result}")
        except ManagedServiceError as exc:
            self._emit_log(runtime, f"MaaFW 过期资源回收失败：{exc}")

    @staticmethod
    async def _write_failure_status(
        runtime: ScriptAdapterRuntime,
        message: str,
    ) -> None:
        try:
            await runtime.storage.update_script_data(
                {"Managed": {"Status": f"不可用 · {message}"}}
            )
        except Exception:
            # The actionable check result remains visible even if storage is locked.
            return


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _runtime_owner(runtime: ScriptAdapterRuntime) -> str:
    return f"MaaFWManaged:{_script_id(runtime)}"


def _script_id(runtime: ScriptAdapterRuntime) -> str:
    script_info = runtime.script_info
    for name in ("script_id", "uid"):
        value = str(getattr(script_info, name, None) or "").strip()
        if value:
            return value
    raise ManagedServiceError("托管 MaaFW 运行上下文缺少稳定 scriptId")


def _script_reference(runtime: ScriptAdapterRuntime) -> str:
    return f"maafw-script:{_script_id(runtime)}"


async def _managed_script_record_dtos() -> list[dict[str, Any]]:
    try:
        records = await Config.get_script_records()
    except Exception as exc:
        raise ManagedServiceError(f"无法读取脚本引用：{exc}") from exc
    result: list[dict[str, Any]] = []
    for record in records:
        if isinstance(record, Mapping):
            value = dict(record)
        else:
            value = {
                "id": getattr(record, "id", None),
                "type": getattr(record, "type", None),
                "config": getattr(record, "config", None),
            }
        result.append(value)
    return result


def _lease_ttl_seconds(value: Any) -> int:
    run_minutes = _as_int(value, 0)
    requested = run_minutes * 60 + 10 * 60 if run_minutes > 0 else 0
    return max(_MINIMUM_LEASE_TTL_SECONDS, requested)
