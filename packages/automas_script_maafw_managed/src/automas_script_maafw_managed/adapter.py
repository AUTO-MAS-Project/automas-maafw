from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from app.core import Config
from app.plugins import ScriptAdapterRuntime
from automas_script_maafw.adapter import MaaFWAdapterHooks
from automas_script_maafw.runtime_route import managed_execution_route
from automas_script_maafw.runner_task import MaaFWPluginAutoProxyTask

from .environment_service import MANAGED_ENVIRONMENT_SERVICE
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
_CHECKOUT_LEASE_KEY = "maafw_managed_checkout_lease"
_PREWARM_PROJECT_LEASE_KEY = "maafw_managed_auto_update_prewarm_lease"
_POLICY_KEY = "maafw_managed_gc_policy"
_MINIMUM_LEASE_TTL_SECONDS = 24 * 60 * 60


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
        # Managed updates own their own resource/config transaction, so they
        # must finish before execution locks and leases are acquired below.
        await self._run_managed_auto_update(runtime)

        # 持住宿主写门直到基础 prepare 锁住脚本配置。升级事务只能完整发生在
        # 本批次之前，或等待到运行锁建立后明确失败，不能插入解析与绑定之间。
        async with self._gateway(runtime).resource_transaction():
            async with runtime.storage.write_transaction():
                resolution = await self._resolve_and_inject(runtime)
                await self._acquire_runtime_lease(runtime, resolution)
                await self._acquire_project_lease(runtime, resolution)
                await self._acquire_checkout_lease(runtime, resolution)
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
        """The Managed update transaction already ran before execution locks."""

        del script_config

    async def _run_managed_auto_update(
        self,
        runtime: ScriptAdapterRuntime,
    ) -> None:
        script_data = await runtime.storage.read_script_data()
        update = _mapping(script_data.get("Update"))
        managed_remote = _mapping(script_data.get("ManagedRemote"))
        if update.get("IfAutoUpdate", True) is not True:
            self._emit_log(runtime, "MaaFW 托管项目运行前自动更新已关闭")
            return

        # The manager's global source/CDK are authoritative for bound Managed
        # projects.  ManagedRemote retains legacy import metadata, but it is
        # not a per-project provider override.  The project channel remains
        # the one per-project remote setting.
        config_get = getattr(Config, "get", None)
        global_config_available = callable(config_get)
        # Bound Managed projects use the manager's global provider.  The
        # source/CDK fields retained in ManagedRemote are legacy import
        # metadata, not per-project overrides; using them here would make a
        # stale remote import silently undo the current global setting.
        script_cdk = ""
        if global_config_available:
            try:
                source = str(config_get("Update", "Source") or "").strip()
            except Exception:
                self._emit_log(
                    runtime,
                    "无法读取 AUTO-MAS 全局更新来源，跳过 MaaFW 托管项目运行前更新",
                )
                return
            if source.casefold() not in {
                "mirrorchyan",
                "mirror_chyan",
                "mirror酱",
                "github",
                "github_release",
            }:
                self._emit_log(
                    runtime,
                    "AUTO-MAS 全局更新来源不支持 MaaFW 托管远程资源，"
                    "跳过运行前更新；请在项目管理页保存 MirrorChyan 或 GitHub",
                )
                return
        else:
            # Keep old host-test/legacy environments usable when the global
            # Config API is absent.
            source = str(update.get("Source") or "").strip()
            if not source:
                source = str(managed_remote.get("Source") or "").strip()
            if source.casefold() not in {
                "mirrorchyan",
                "mirror_chyan",
                "mirror酱",
                "github",
                "github_release",
            }:
                # MaaFW package metadata is keyed by the interface RID. Keep
                # the historical default only when the old host has no global
                # Config API at all.
                source = "MirrorChyan"
            script_cdk = str(
                update.get("MirrorChyanCDK")
                or managed_remote.get("MirrorChyanCDK")
                or ""
            ).strip()
        global_cdk = ""
        if (
            global_config_available
            and source.casefold() in {"mirrorchyan", "mirror_chyan", "mirror酱"}
        ):
            try:
                global_cdk = str(
                    config_get("Update", "MirrorChyanCDK") or ""
                ).strip()
            except Exception:
                self._emit_log(
                    runtime,
                    "无法读取 AUTO-MAS 全局 MirrorChyan CDK，"
                    "跳过 MaaFW 托管项目运行前更新",
                )
                return
        channel = str(managed_remote.get("Channel") or "").strip().casefold()
        if channel not in {"stable", "beta"}:
            channel = "stable"
        if source.casefold() in {"mirrorchyan", "mirror_chyan", "mirror酱"} and not (
            script_cdk or global_cdk
        ):
            self._emit_log(
                runtime,
                "MaaFW 托管项目未配置 MirrorChyan CDK，跳过运行前更新；"
                "可改用 GitHub 或配置 AUTO-MAS 全局 CDK",
            )
            return

        managed = _mapping(script_data.get("Managed"))
        project_id, _version = managed_project_identity(managed)
        payload = {
            "projectId": project_id,
            "source": source,
            "channel": channel,
        }
        update_applied = False
        try:
            service = runtime.get_service(MANAGED_ENVIRONMENT_SERVICE)
            updater = getattr(service, "update_script_before_run", None)
            if not callable(updater):
                raise ManagedServiceError(
                    "maafw.managed.environment.v1 未提供 update_script_before_run()"
                )
            result = await updater(
                _script_id(runtime),
                payload,
                send_log=lambda message: self._emit_log(runtime, message),
            )
            if not isinstance(result, Mapping):
                raise ManagedServiceError("运行前更新服务返回值不是 JSON object")
            update_applied = result.get("updated") is True
            if update_applied:
                transition_lease = _mapping(result.get("_prewarmProjectLease"))
                if transition_lease:
                    runtime.extra[_PREWARM_PROJECT_LEASE_KEY] = dict(
                        transition_lease
                    )
                try:
                    prepare = getattr(service, "prepare_script_environment", None)
                    if not callable(prepare):
                        raise ManagedServiceError(
                            "maafw.managed.environment.v1 未提供 prepare_script_environment()"
                        )
                    await prepare(
                        _script_id(runtime),
                        None,
                        send_log=lambda message: self._emit_log(runtime, message),
                    )
                finally:
                    await self._release_prewarm_project_lease(runtime)
                collect = getattr(service, "collect_unreferenced_resources", None)
                if callable(collect):
                    try:
                        await collect()
                    except Exception as exc:
                        self._emit_log(
                            runtime,
                            f"MaaFW 新版本已就绪，旧版本回收暂未完成：{exc}",
                        )
            runtime.extra.pop(_RESOLUTION_KEY, None)
        except Exception as exc:
            if update_applied:
                # The binding has already switched at this point.  Starting
                # anyway after prewarm failure would run an unverified project
                # or runtime and could make the old transition lease eligible
                # for GC.  Fail closed; the caller can retry preparation or
                # explicitly recover the pending resource state.
                message = (
                    "MaaFW 托管项目已切换但新环境预热失败，"
                    "已阻止本次运行；请重试准备环境或检查资源状态："
                    f"{exc}"
                )
                self._emit_log(runtime, message)
                raise ManagedServiceError(message) from exc
            self._emit_log(
                runtime,
                "MaaFW 托管项目运行前更新或新版本预热未完整完成；"
                f"继续按当前已绑定版本启动：{exc}",
            )

    def run_auto_proxy(self, runtime: ScriptAdapterRuntime) -> MaaFWPluginAutoProxyTask:
        task = super().run_auto_proxy(runtime)
        if not isinstance(task, MaaFWPluginAutoProxyTask):
            raise RuntimeError("MaaFW 托管适配器未获得 MaaFWPluginAutoProxyTask")
        resolution = runtime.extra.get(_RESOLUTION_KEY)
        if not isinstance(resolution, Mapping):
            raise RuntimeError("MaaFW 托管执行缺少已解析的 Project Store resolution")
        project = resolution.get("project")
        runtime_binding = resolution.get("runtime")
        if not isinstance(project, Mapping) or not isinstance(runtime_binding, Mapping):
            raise RuntimeError("MaaFW 托管 resolution 缺少 project/runtime DTO")
        task.maafw_managed_execution = True
        task.maafw_managed_project = dict(project)
        task.maafw_managed_runtime_binding = dict(runtime_binding)
        task.maafw_managed_route = managed_execution_route(
            managed_execution=True,
            project=task.maafw_managed_project,
            runtime_binding=task.maafw_managed_runtime_binding,
            expected_pool_id=task.maafw_runtime_pool_id,
        )
        return task

    async def finalize(self, runtime: ScriptAdapterRuntime) -> None:
        try:
            await super().finalize(runtime)
        finally:
            async with self._gateway(runtime).resource_transaction():
                await self._release_prewarm_project_lease(runtime)
                await self._release_checkout_lease(runtime)
                await self._release_project_lease(runtime)
                await self._release_runtime_lease(runtime)
            await self._auto_collect_garbage(runtime)

    async def on_crash(self, runtime: ScriptAdapterRuntime, error: Exception) -> None:
        async with self._gateway(runtime).resource_transaction():
            await self._release_prewarm_project_lease(runtime)
            await self._release_checkout_lease(runtime)
            await self._release_project_lease(runtime)
            await self._release_runtime_lease(runtime)
        await self._auto_collect_garbage(runtime)
        await super().on_crash(runtime, error)

    async def _resolve_and_inject(
        self,
        runtime: ScriptAdapterRuntime,
    ) -> dict[str, Any]:
        script_data = await runtime.storage.read_script_data()
        managed = _mapping(script_data.get("Managed"))
        pending_upgrade = _mapping(managed.get("PendingUpgrade"))
        upgrade_state = str(pending_upgrade.get("state") or "").strip()
        if upgrade_state in ManagedServiceGateway.UPGRADE_BLOCKING_STATES:
            raise ManagedServiceError(
                f"资源升级事务处于 {upgrade_state}，"
                "恢复完成前拒绝启动新任务"
            )
        run_config = _mapping(script_data.get("Run"))
        project_id, version = managed_project_identity(managed)
        request = {
            "projectId": project_id,
            "version": version,
            "channel": managed.get("Channel"),
            "runtimeConstraint": managed.get("RuntimeConstraint"),
            "projectReference": _script_reference(runtime),
            "scriptId": _script_id(runtime),
            "expectedStoreId": managed.get("StoreId"),
            "expectedProjectManifest": managed.get("ProjectManifest"),
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
                    "StoreId": str(project.get("storeId") or ""),
                    "RunRootId": str(
                        _mapping(resolution.get("checkout")).get("runRootId")
                        or ""
                    ),
                    "Version": version,
                    "RuntimeConstraint": resolution.get("runtimeConstraint") or "",
                    "Status": f"就绪 · {project_label} · {runtime_id}",
                    "ProjectManifest": dict(project.get("manifest") or {}),
                },
                "ManagedRuntime": {
                    "RuntimeId": runtime_id,
                    "PoolId": str(runtime_binding.get("poolId") or ""),
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
        intent = {
            "runtimeId": runtime_id,
            "leaseId": lease_id,
        }
        # Register the intent before crossing the service boundary.  A sync
        # service mutation may commit in its worker thread and then re-raise a
        # pending cancellation; finalize still needs the exact lease id in that
        # case.
        runtime.extra[_LEASE_KEY] = intent
        try:
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
        except BaseException:
            # release_lease is idempotent when the acquire did not commit.
            await self._release_runtime_lease(runtime)
            raise

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
        intent = {
            "projectId": project_id,
            "version": version,
            "leaseId": lease_id,
        }
        runtime.extra[_PROJECT_LEASE_KEY] = intent
        try:
            await self._gateway(runtime).acquire_project_lease(
                project_id,
                version,
                lease_id,
                owner=_runtime_owner(runtime),
                ttl_seconds=ttl_seconds,
            )
        except BaseException:
            await self._release_project_lease(runtime)
            raise

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

    async def _acquire_checkout_lease(
        self,
        runtime: ScriptAdapterRuntime,
        resolution: Mapping[str, Any],
    ) -> None:
        if runtime.extra.get(_CHECKOUT_LEASE_KEY):
            return
        checkout = _mapping(resolution.get("checkout"))
        runtime_lease = runtime.extra.get(_LEASE_KEY)
        checkout_id = str(checkout.get("checkoutId") or "").strip()
        script_id = _script_id(runtime)
        lease_id = (
            str(runtime_lease.get("leaseId") or "")
            if isinstance(runtime_lease, Mapping)
            else ""
        )
        if not checkout_id or not lease_id:
            raise ManagedServiceError("checkout lease 缺少 checkoutId 或 leaseId")
        ttl_seconds = max(
            float(_MINIMUM_LEASE_TTL_SECONDS),
            float(
                _mapping(runtime.extra.get(_POLICY_KEY)).get(
                    "leaseTtlSeconds",
                    _MINIMUM_LEASE_TTL_SECONDS,
                )
            ),
        )
        intent = {
            "checkoutId": checkout_id,
            "scriptId": script_id,
            "leaseId": lease_id,
        }
        runtime.extra[_CHECKOUT_LEASE_KEY] = intent
        try:
            await self._gateway(runtime).acquire_checkout_lease(
                checkout_id,
                script_id,
                lease_id,
                owner=_runtime_owner(runtime),
                ttl_seconds=ttl_seconds,
            )
        except BaseException:
            await self._release_checkout_lease(runtime)
            raise

    async def _release_checkout_lease(self, runtime: ScriptAdapterRuntime) -> None:
        lease = runtime.extra.get(_CHECKOUT_LEASE_KEY)
        if not isinstance(lease, Mapping):
            return
        checkout_id = str(lease.get("checkoutId") or "")
        script_id = str(lease.get("scriptId") or "")
        lease_id = str(lease.get("leaseId") or "")
        if not checkout_id or not script_id or not lease_id:
            return
        try:
            await self._gateway(runtime).release_checkout_lease(
                checkout_id,
                script_id,
                lease_id,
            )
        except ManagedServiceError as exc:
            self._emit_log(runtime, f"释放 MaaFW checkout lease 失败：{exc}")
            return
        if runtime.extra.get(_CHECKOUT_LEASE_KEY) is lease:
            runtime.extra.pop(_CHECKOUT_LEASE_KEY, None)

    async def _release_project_lease(self, runtime: ScriptAdapterRuntime) -> None:
        lease = runtime.extra.get(_PROJECT_LEASE_KEY)
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
            return
        if runtime.extra.get(_PROJECT_LEASE_KEY) is lease:
            runtime.extra.pop(_PROJECT_LEASE_KEY, None)

    async def _release_prewarm_project_lease(
        self,
        runtime: ScriptAdapterRuntime,
    ) -> None:
        lease = runtime.extra.get(_PREWARM_PROJECT_LEASE_KEY)
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
            self._emit_log(runtime, f"释放 MaaFW 自动更新过渡 lease 失败：{exc}")
            return
        if runtime.extra.get(_PREWARM_PROJECT_LEASE_KEY) is lease:
            runtime.extra.pop(_PREWARM_PROJECT_LEASE_KEY, None)

    async def _release_runtime_lease(self, runtime: ScriptAdapterRuntime) -> None:
        lease = runtime.extra.get(_LEASE_KEY)
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
            return
        if runtime.extra.get(_LEASE_KEY) is lease:
            runtime.extra.pop(_LEASE_KEY, None)

    async def _auto_collect_garbage(self, runtime: ScriptAdapterRuntime) -> None:
        try:
            gateway = self._gateway(runtime)
            async with gateway.resource_transaction():
                async with Config.script_config_write_scope(None):
                    script_records = await _managed_script_record_dtos()
                    result = await gateway.collect_garbage(
                        dry_run=False,
                        grace_days=0,
                        keep_latest=0,
                        project_id=None,
                        script_records=script_records,
                    )
            project_store = _mapping(result.get("projectStore"))
            checkout_gc = _mapping(
                project_store.get("checkoutGarbageCollection")
            )
            runtime_pool = _mapping(result.get("runtimePool"))
            project_count = _list_count(project_store.get("deleted"))
            checkout_count = _list_count(checkout_gc.get("deleted"))
            runtime_count = _list_count(runtime_pool.get("deleted"))
            if project_count or checkout_count or runtime_count:
                self._emit_log(
                    runtime,
                    "MaaFW 无引用资源回收完成："
                    f"项目版本 {project_count}，运行副本 {checkout_count}，"
                    f"共享运行时 {runtime_count}",
                )
        except Exception as exc:
            self._emit_log(runtime, f"MaaFW 无引用资源回收失败：{exc}")

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


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


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
