from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.core import Config
from app.plugins import (
    PluginHttpRequest,
    ScriptAdapterDefinition,
    ScriptAdapterPlugin,
)

from .adapter import MaaFWManagedAdapterHooks
from .schema import SCRIPT_GROUPS, USER_GROUPS
from .services import (
    PROJECT_STORE_SERVICE,
    PROJECT_UPDATE_SERVICE,
    RUNTIME_POOL_SERVICE,
    INTERFACE_SERVICE,
    ManagedServiceError,
    ManagedServiceGateway,
)


DEFAULT_INSTANCE = {
    "name": "托管 MaaFW 项目脚本适配",
    "enabled": True,
    "config": {},
}

schema = {
    "__no_plugin_config__": {
        "type": "boolean",
        "default": True,
        "hidden": True,
        "configurable": False,
        "title": "No plugin-level configuration",
    },
}


class Plugin(ScriptAdapterPlugin):
    """Declarative adapter for resource-only, versioned MaaFW projects."""

    needs = [PROJECT_STORE_SERVICE, RUNTIME_POOL_SERVICE]
    wants = [
        "emulator",
        "maafw.interface.v1",
        "maafw.project_update.v1",
        "maafw.agent_env.v1",
        "maafw.runner.v1",
        "maafw.registry.v1",
    ]

    def build_script_adapters(self):
        return [
            ScriptAdapterDefinition(
                type_key="MaaFWManaged",
                display_name="托管 MaaFW 项目",
                hooks_factory=MaaFWManagedAdapterHooks,
                script_groups=SCRIPT_GROUPS,
                user_groups=USER_GROUPS,
                script_class_name="MaaFWManagedPluginConfig",
                user_class_name="MaaFWManagedPluginUserConfig",
                module="automas_script_maafw_managed.schema",
                related_bindings={"EmulatorConfig": "EmulatorConfig"},
                supported_modes=("AutoProxy",),
                icon="MaaFW",
                icon_path="automas_script_maafw:assets/maafw.png",
                editor_kind="schema",
                # MaaFWManaged 是 v6 新增类型，没有 r6 遗留配置需要兼容。
                # 声明 legacy MaaFWConfig/MaaFWUserConfig 会与 automas_script_maafw
                # 抢占宿主注册表里同一个 legacy 键：后注册者静默覆盖先注册者，
                # 且停用其中一个会把另一个仍在使用的 legacy 映射一起 pop 掉。
                is_builtin=False,
                metadata={
                    "framework": "maafw",
                    "source": "automas_script_maafw_managed",
                    "resource_model": "project-store",
                    "declarative": True,
                    "create_group": "general",
                    "m9a_standalone": False,
                },
            )
        ]

    async def on_start(self) -> None:
        await super().on_start()
        self.ctx.server.http(
            "/maafw-managed/import",
            self._import_project,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/check-update",
            self._check_update,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/update",
            self._update_project,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/switch",
            self._switch_version,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/versions/list",
            self._list_versions,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/delete",
            self._delete_version,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/runtime/install",
            self._install_runtime,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/runtime/list",
            self._list_runtimes,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/runtime/delete",
            self._delete_runtime,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/pin",
            self._pin,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/gc",
            self._collect_garbage,
            methods=("POST",),
        )

    async def _import_project(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().import_project(payload),
            after_success=lambda script_id, data: self._persist_project(
                script_id,
                data,
                payload,
                status="资源版本已导入并激活",
            ),
        )

    async def _check_update(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().check_update(payload),
            after_success=lambda script_id, data: self._persist_check_result(
                script_id,
                data,
            ),
        )

    async def _update_project(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().update_project(payload),
            after_success=lambda script_id, data: self._persist_update_result(
                script_id,
                data,
                payload,
            ),
        )

    async def _switch_version(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().switch_version(payload),
            after_success=lambda script_id, data: self._persist_project(
                script_id,
                data,
                payload,
                status="已切换 MaaFW 资源版本",
            ),
        )

    async def _list_versions(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().list_versions(
                str(payload.get("projectId") or "")
            ),
            after_success=lambda script_id, data: Config.update_script(
                script_id,
                {
                    "Managed": {
                        "AvailableVersions": data,
                        "Status": "项目版本列表已刷新",
                    }
                },
            ),
        )

    async def _delete_version(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().delete_version(payload),
            after_success=lambda script_id, data: Config.update_script(
                script_id,
                {
                    "Managed": {
                        "Version": "",
                        "Status": f"已删除资源版本 {data.get('version') or ''}",
                    }
                },
            ),
        )

    async def _install_runtime(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._resolve_and_bind_runtime(script_id, payload),
            after_success=lambda script_id, data: self._persist_resolution(
                script_id,
                data,
                payload,
            ),
        )

    async def _list_runtimes(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().list_runtimes(),
            after_success=lambda script_id, data: Config.update_script(
                script_id,
                {
                    "ManagedRuntime": {"AvailableRuntimes": data},
                    "Managed": {"Status": "共享运行时列表已刷新"},
                },
            ),
        )

    async def _delete_runtime(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().delete_runtime(payload),
            after_success=lambda script_id, data: self._persist_runtime_delete(
                script_id,
                data,
            ),
        )

    async def _pin(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().pin(payload),
            after_success=lambda script_id, data: Config.update_script(
                script_id,
                {
                    "Managed": {
                        "Status": (
                            "项目与运行时已固定"
                            if _as_bool(payload.get("pinned"), True)
                            else "项目与运行时已取消固定"
                        )
                    }
                },
            ),
        )

    async def _collect_garbage(
        self,
        request: PluginHttpRequest,
    ) -> dict[str, Any]:
        payload = _payload(request)
        dry_run = _as_bool(payload.get("dryRun"), True)
        if not dry_run and str(payload.get("confirmation") or "") != "DELETE UNUSED":
            return {
                "code": 400,
                "status": "error",
                "message": "实际回收前请在确认字段中输入 DELETE UNUSED",
            }
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._collect_garbage_with_script_references(
                payload,
                dry_run=dry_run,
            ),
            after_success=lambda script_id, data: Config.update_script(
                script_id,
                {
                    "Managed": {
                        "Status": (
                            "空间回收预览已完成"
                            if dry_run
                            else "过期项目与运行时已回收"
                        )
                    }
                },
            ),
        )

    async def _resolve_and_bind_runtime(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        request = dict(payload)
        request["projectReference"] = f"maafw-script:{script_id}"
        resolution = await self._gateway().resolve_execution(request)
        project = _mapping(resolution.get("project"))
        binding = _mapping(resolution.get("runtime"))
        resolution["project"] = await self._gateway().bind_project_runtime(
            str(project.get("projectId") or payload.get("projectId") or ""),
            str(project.get("version") or payload.get("version") or "") or None,
            binding,
            project_reference=request["projectReference"],
        )
        return resolution

    async def _collect_garbage_with_script_references(
        self,
        payload: Mapping[str, Any],
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        script_records = await _managed_script_record_dtos()
        return await self._gateway().collect_garbage(
            dry_run=dry_run,
            grace_days=_as_int(payload.get("graceDays"), 30),
            keep_latest=_as_int(payload.get("keepLatest"), 2),
            project_id=str(payload.get("projectId") or "").strip() or None,
            script_records=script_records,
        )

    def _gateway(self) -> ManagedServiceGateway:
        return ManagedServiceGateway(
            self.ctx.get(PROJECT_STORE_SERVICE),
            self.ctx.get(RUNTIME_POOL_SERVICE),
            self.ctx.get(PROJECT_UPDATE_SERVICE),
            self.ctx.get(INTERFACE_SERVICE),
        )

    @staticmethod
    async def _respond(
        operation: Callable[[], Awaitable[Any]],
        *,
        after_success: Callable[[Any], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        try:
            data = await operation()
            if after_success is not None:
                await after_success(data)
        except ManagedServiceError as exc:
            return {
                "code": 400,
                "status": "error",
                "message": str(exc),
            }
        except Exception as exc:
            return {
                "code": 500,
                "status": "error",
                "message": f"托管 MaaFW 动作执行失败：{exc}",
            }
        return {
            "code": 200,
            "status": "success",
            "data": data,
        }

    async def _respond_for_script(
        self,
        payload: Mapping[str, Any],
        operation: Callable[[str], Awaitable[Any]],
        *,
        after_success: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        async def validated_operation() -> Any:
            script_id = await self._require_managed_script(payload)
            data = await operation(script_id)
            if after_success is not None:
                await after_success(script_id, data)
            return data

        return await self._respond(validated_operation)

    @staticmethod
    async def _require_managed_script(payload: Mapping[str, Any]) -> str:
        script_id = str(payload.get("scriptId") or payload.get("script_id") or "").strip()
        if not script_id:
            raise ManagedServiceError("动作请求缺少 scriptId")
        try:
            records = await Config.get_script_records(script_id)
        except Exception as exc:
            raise ManagedServiceError(f"无法读取脚本 {script_id}：{exc}") from exc
        if len(records) != 1 or records[0].type != "MaaFWManaged":
            raise ManagedServiceError(
                f"scriptId {script_id} 不是 MaaFWManaged 脚本，拒绝跨类型写入"
            )
        return script_id

    @staticmethod
    async def _persist_project(
        script_id: str,
        project: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        status: str,
    ) -> None:
        await Config.update_script(
            script_id,
            _project_form_update(project, payload, status=status),
        )

    @staticmethod
    async def _persist_resolution(
        script_id: str,
        resolution: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        project = _mapping(resolution.get("project"))
        runtime = _mapping(resolution.get("runtime"))
        update = _project_form_update(
            project,
            payload,
            status=f"共享运行时已就绪 · {runtime.get('runtimeId') or ''}",
        )
        update["ManagedRuntime"] = {
            "RuntimeId": str(runtime.get("runtimeId") or ""),
            "PythonExecutable": str(runtime.get("pythonExecutable") or ""),
            "VenvPath": str(runtime.get("venvPath") or ""),
            "RuntimeBinding": runtime,
        }
        await Config.update_script(script_id, update)

    async def _persist_runtime_delete(
        self,
        script_id: str,
        result: Mapping[str, Any],
    ) -> None:
        runtimes = await self._gateway().list_runtimes()
        await Config.update_script(
            script_id,
            {
                "ManagedRuntime": {
                    "AvailableRuntimes": runtimes,
                    "TargetRuntimeId": "",
                    "RuntimeDeleteConfirmation": "",
                },
                "Managed": {
                    "Status": f"已删除共享运行时 {result.get('runtimeId') or ''}"
                },
            },
        )

    @staticmethod
    async def _persist_check_result(
        script_id: str,
        result: Mapping[str, Any],
    ) -> None:
        candidate = _mapping(result.get("candidate"))
        status = (
            f"发现可用更新 {candidate.get('version') or ''}"
            if result.get("updateAvailable")
            else "当前 MaaFW 资源已是最新版本"
        )
        await Config.update_script(script_id, {"Managed": {"Status": status}})

    @staticmethod
    async def _persist_update_result(
        script_id: str,
        result: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        project = _mapping(result.get("project"))
        status = (
            f"已更新到不可变资源版本 {result.get('latestVersion') or ''}"
            if result.get("updated")
            else "当前 MaaFW 资源已是最新版本"
        )
        await Config.update_script(
            script_id,
            _project_form_update(project, payload, status=status),
        )


def _payload(request: PluginHttpRequest) -> dict[str, Any]:
    if isinstance(request.json, Mapping):
        return dict(request.json)
    if isinstance(request.query, Mapping):
        return dict(request.query)
    return {}


def _project_form_update(
    project: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    project_id = str(project.get("projectId") or payload.get("projectId") or "").strip()
    version = str(project.get("version") or payload.get("version") or "").strip()
    project_path = ""
    for key in ("dataPath", "projectPath", "path"):
        value = str(project.get(key) or "").strip()
        if value:
            project_path = value
            break
    managed = {
        "ProjectId": project_id,
        "Version": version,
        "RuntimeConstraint": str(
            project.get("runtimeConstraint")
            or payload.get("runtimeConstraint")
            or ""
        ),
        "Status": status,
        "ProjectManifest": _mapping(project.get("manifest")),
    }
    if "channel" in payload:
        managed["Channel"] = str(payload.get("channel") or "stable")
    if "sourcePath" in payload:
        managed["SourcePath"] = str(payload.get("sourcePath") or "")
    raw_source_config = payload.get("sourceConfig")
    if isinstance(raw_source_config, Mapping):
        source_config = dict(raw_source_config)
        managed.update({
            "UpdateSource": str(source_config.get("source") or "auto"),
            "MirrorChyanCDK": str(source_config.get("mirror_cdk") or ""),
            "GitHubRepo": str(source_config.get("repo") or ""),
            "GitHubTag": str(source_config.get("tag") or ""),
            "GitHubAssetPattern": str(source_config.get("asset_pattern") or ""),
            "GitHubToken": str(source_config.get("token") or ""),
        })
    update: dict[str, Any] = {"Managed": managed}
    if project_path:
        update["Info"] = {
            "Path": project_path,
            "ProjectLabel": "@".join(
                item for item in (project_id, version) if item
            ),
        }
    return update


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


async def _managed_script_record_dtos() -> list[dict[str, Any]]:
    try:
        records = await Config.get_script_records()
    except Exception as exc:
        raise ManagedServiceError(f"无法读取脚本引用：{exc}") from exc
    return [
        {
            "id": (
                record.get("id")
                if isinstance(record, Mapping)
                else getattr(record, "id", None)
            ),
            "type": (
                record.get("type")
                if isinstance(record, Mapping)
                else getattr(record, "type", None)
            ),
            "config": (
                record.get("config")
                if isinstance(record, Mapping)
                else getattr(record, "config", None)
            ),
        }
        for record in records
    ]


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
