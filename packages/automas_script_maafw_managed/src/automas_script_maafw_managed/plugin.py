from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata as importlib_metadata
import inspect
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import Config
from app.plugins import (
    PluginHttpRequest,
    ScriptAdapterDefinition,
    ScriptAdapterPlugin,
)
from automas_script_maafw.project_path import (
    release_project_path,
    try_reserve_project_path,
)

from .adapter import MaaFWManagedAdapterHooks
from .schema import SCRIPT_GROUPS, USER_GROUPS
from .services import (
    INTERFACE_SERVICE,
    PROJECT_STORE_SERVICE,
    PROJECT_UPDATE_SERVICE,
    RUNTIME_POOL_SERVICE,
    ManagedServiceError,
    ManagedServiceGateway,
    managed_project_identity,
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

_PENDING_KIND = "maafw.managed-upgrade-pending"
_USER_PENDING_KIND = "maafw.managed-user-upgrade-pending"
_CONVERSION_KIND = "maafw.managed-conversion"
_CONVERSION_API_VERSION = "maafw-managed.v1"
_DISTRIBUTION_NAME = "automas-script-maafw-managed"
_DISTRIBUTION_VERSION_FALLBACK = "0.2.0"
_SOURCE_TYPE = "MaaFW"
_TARGET_TYPE = "MaaFWManaged"
_PROGRESS_EVENT_TYPE = "maafw.managed.progress"
_PROGRESS_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_PROGRESS_MAX_STATES = 128
_PROGRESS_MAX_LOGS = 80
_PROGRESS_TERMINAL_STATES = frozenset({"success", "error"})
_INTERRUPTED_STATES = {
    "applying",
    "committing",
    "recovery_required",
    "rollback_failed",
}
_JSON_OBJECT_FIELDS = frozenset(
    (str(getattr(group, "key", "")), str(getattr(field, "name", "")))
    for group in (*SCRIPT_GROUPS, *USER_GROUPS)
    for field in getattr(group, "fields", ())
    if getattr(field, "field_type", None) == "json"
    and getattr(field, "json_type", "object") != "array"
)


class Plugin(ScriptAdapterPlugin):
    """Declarative adapter for resource-only, versioned MaaFW projects."""

    needs = [
        PROJECT_STORE_SERVICE,
        RUNTIME_POOL_SERVICE,
        PROJECT_UPDATE_SERVICE,
        INTERFACE_SERVICE,
    ]
    wants = [
        "emulator",
        "maafw.agent_env.v1",
        "maafw.runner.v1",
        "maafw.registry.v1",
    ]

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._upgrade_locks: dict[str, asyncio.Lock] = {}
        self._progress_states: dict[str, dict[str, Any]] = {}
        self._progress_lock = asyncio.Lock()
        self._progress_tasks: set[asyncio.Task[None]] = set()
        self._progress_context: ContextVar[dict[str, str] | None] = (
            ContextVar(
                f"maafw_managed_progress_{id(self)}",
                default=None,
            )
        )

    def _upgrade_lock(self, script_id: str) -> asyncio.Lock:
        lock = self._upgrade_locks.get(script_id)
        if lock is None:
            lock = asyncio.Lock()
            self._upgrade_locks[script_id] = lock
        return lock

    async def _run_upgrade_locked(
        self,
        script_id: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        async with self._gateway().resource_transaction():
            async with self._upgrade_lock(script_id):
                return await operation()

    async def _run_resource_locked(
        self,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        async with self._gateway().resource_transaction():
            return await operation()

    @staticmethod
    async def _run_config_transaction(
        script_id: str,
        owner: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        async with Config.script_config_transaction(
            script_id,
            owner=owner,
        ):
            return await operation()

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
                editor_kind="plugin:automas_script_maafw",
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
                    "creatable": False,
                    "create_mode": "convert-only",
                    "editor_reuse_type": "MaaFW",
                    "m9a_standalone": False,
                },
            )
        ]

    async def on_start(self) -> None:
        await super().on_start()
        self.ctx.server.http(
            "/maafw-managed/capabilities",
            self._capabilities,
            methods=("GET", "POST"),
        )
        self.ctx.server.http(
            "/maafw-managed/progress",
            self._read_progress,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/convert",
            self._convert_project,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/remote/check",
            self._check_remote_project,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/remote/import",
            self._import_remote_project,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/remote/upgrade",
            self._upgrade_remote_project,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/import",
            self._import_project,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/upgrade-local",
            self._upgrade_project,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/upgrade-apply",
            self._apply_upgrade,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/upgrade-cancel",
            self._cancel_upgrade,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/switch",
            self._switch_version,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw-managed/projects/list",
            self._list_projects,
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
        await self._recover_interrupted_upgrades()
        await self._repair_upgrade_artifacts_on_start()

    async def _capabilities(
        self,
        _request: PluginHttpRequest,
    ) -> dict[str, Any]:
        return {
            "code": 200,
            "status": "success",
            "data": _managed_capabilities(),
        }

    async def _read_progress(
        self,
        request: PluginHttpRequest,
    ) -> dict[str, Any]:
        payload = _payload(request)
        try:
            script_id = _required_script_id(payload)
            operation_id = _required_progress_id(payload)
        except ManagedServiceError as exc:
            return {
                "code": 400,
                "status": "error",
                "message": str(exc),
            }
        async with self._progress_lock:
            state = self._progress_states.get(operation_id)
            if state is None or state.get("scriptId") != script_id:
                return {
                    "code": 404,
                    "status": "error",
                    "message": "未找到与该脚本匹配的 MaaFW 托管操作进度",
                }
            snapshot = _json_clone(state)
        return {
            "code": 200,
            "status": "success",
            "data": snapshot,
        }

    async def _begin_progress(
        self,
        payload: Mapping[str, Any],
        operation: str,
        message: str,
    ) -> dict[str, str]:
        script_id = _required_script_id(payload)
        operation_id = _required_progress_id(payload, required=False)
        if not operation_id:
            operation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        state: dict[str, Any] = {
            "operationId": operation_id,
            "scriptId": script_id,
            "operation": operation,
            "status": "running",
            "stage": "queued",
            "message": message,
            "percent": 0,
            "downloadedBytes": None,
            "totalBytes": None,
            "logs": [message] if message else [],
            "createdAt": now,
            "updatedAt": now,
        }
        async with self._progress_lock:
            if operation_id in self._progress_states:
                raise ManagedServiceError(
                    "progressId 已被使用；每次操作必须使用新的 progressId"
                )
            if len(self._progress_states) >= _PROGRESS_MAX_STATES:
                terminal_id = next(
                    (
                        key
                        for key, item in self._progress_states.items()
                        if item.get("status") in _PROGRESS_TERMINAL_STATES
                    ),
                    None,
                )
                if terminal_id is None:
                    raise ManagedServiceError(
                        "MaaFW 托管操作过多，请等待正在执行的操作完成"
                    )
                self._progress_states.pop(terminal_id)
            self._progress_states[operation_id] = state
        return {
            "operationId": operation_id,
            "scriptId": script_id,
            "operation": operation,
        }

    async def _progress_stage(
        self,
        stage: str,
        message: str,
        *,
        percent: float | int | None = None,
        downloaded_bytes: int | None = None,
        total_bytes: int | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        progress = dict(context or self._progress_context.get() or {})
        operation_id = str(progress.get("operationId") or "")
        script_id = str(progress.get("scriptId") or "")
        if not operation_id or not script_id:
            return
        async with self._progress_lock:
            state = self._progress_states.get(operation_id)
            if (
                state is None
                or state.get("scriptId") != script_id
                or state.get("status") in _PROGRESS_TERMINAL_STATES
            ):
                return
            next_percent = _progress_number(percent)
            current_percent = _progress_number(state.get("percent"))
            if (
                next_percent is not None
                and current_percent is not None
                and next_percent < current_percent
            ):
                return
            state["stage"] = str(stage or state.get("stage") or "running")
            if message:
                state["message"] = message
                logs = list(state.get("logs") or [])
                if not logs or logs[-1] != message:
                    logs.append(message)
                state["logs"] = logs[-_PROGRESS_MAX_LOGS:]
            if next_percent is not None:
                state["percent"] = min(99.0, max(0.0, next_percent))
            if downloaded_bytes is not None:
                state["downloadedBytes"] = max(0, int(downloaded_bytes))
            if total_bytes is not None:
                state["totalBytes"] = max(0, int(total_bytes))
            state["updatedAt"] = datetime.now(timezone.utc).isoformat()
            snapshot = _json_clone(state)
        await self._publish_progress(snapshot)

    async def _finish_progress(
        self,
        progress: Mapping[str, str],
        status: str,
        message: str,
    ) -> None:
        if status not in _PROGRESS_TERMINAL_STATES:
            raise ValueError(f"invalid progress terminal state: {status}")
        operation_id = str(progress.get("operationId") or "")
        script_id = str(progress.get("scriptId") or "")
        async with self._progress_lock:
            state = self._progress_states.get(operation_id)
            if (
                state is None
                or state.get("scriptId") != script_id
                or state.get("status") in _PROGRESS_TERMINAL_STATES
            ):
                return
            state["status"] = status
            state["stage"] = "completed" if status == "success" else "failed"
            state["message"] = message
            if status == "success":
                state["percent"] = 100
            logs = list(state.get("logs") or [])
            if message and (not logs or logs[-1] != message):
                logs.append(message)
            state["logs"] = logs[-_PROGRESS_MAX_LOGS:]
            state["updatedAt"] = datetime.now(timezone.utc).isoformat()
            snapshot = _json_clone(state)
        await self._publish_progress(snapshot)

    @staticmethod
    async def _publish_progress(state: Mapping[str, Any]) -> None:
        try:
            from app.core.ws import Publisher
        except Exception:
            return
        identifiers = {
            str(state.get("scriptId") or ""),
            str(state.get("operationId") or ""),
        }
        for identifier in identifiers:
            if not identifier:
                continue
            try:
                await Publisher.send(
                    id=identifier,
                    type=_PROGRESS_EVENT_TYPE,
                    data=state,
                )
            except Exception:
                # Progress is observational.  A disconnected or older host
                # must never abort a resource/configuration transaction.
                continue

    def _download_progress_callback(
        self,
        *,
        start_percent: float = 15.0,
        end_percent: float = 65.0,
    ) -> Callable[[Mapping[str, Any]], None] | None:
        progress = self._progress_context.get()
        if progress is None:
            return None
        captured = dict(progress)
        loop = asyncio.get_running_loop()

        def report(event: Mapping[str, Any]) -> None:
            if not isinstance(event, Mapping):
                return
            raw = dict(event)
            raw_percent = _progress_number(raw.get("percent"))
            mapped_percent = start_percent
            if raw_percent is not None:
                mapped_percent += (
                    min(100.0, max(0.0, raw_percent))
                    * (end_percent - start_percent)
                    / 100.0
                )
            stage = str(raw.get("stage") or "downloading")
            message = str(raw.get("message") or "").strip()
            if not message:
                message = (
                    "正在校验远程 MaaFW 资源包"
                    if stage == "validating"
                    else "正在下载远程 MaaFW 资源包"
                )
            downloaded = _progress_int(
                raw.get("downloadedBytes", raw.get("downloaded_bytes"))
            )
            total = _progress_int(
                raw.get("totalBytes", raw.get("total_bytes"))
            )

            def schedule() -> None:
                task = asyncio.create_task(
                    self._progress_stage(
                        f"download:{stage}",
                        message,
                        percent=mapped_percent,
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                        context=captured,
                    )
                )
                self._progress_tasks.add(task)
                task.add_done_callback(self._progress_tasks.discard)

            try:
                loop.call_soon_threadsafe(schedule)
            except RuntimeError:
                return

        return report

    async def _flush_progress_updates(self) -> None:
        # A synchronous downloader callback may arrive from another thread.
        # Yield once so call_soon_threadsafe can materialize its task, then
        # drain only the plugin's observational progress tasks.
        await asyncio.sleep(0)
        pending = tuple(self._progress_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    async def _wait_for_progress_task(task: asyncio.Task[Any]) -> bool:
        """Wait for a protected operation while remembering request cancellation."""

        cancellation_requested = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancellation_requested = True
            except Exception:
                # The caller inspects task.result() after the task is done so
                # it can derive the authoritative progress terminal state.
                pass
        return cancellation_requested

    async def _respond_with_progress(
        self,
        payload: Mapping[str, Any],
        operation_name: str,
        initial_message: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> dict[str, Any]:
        try:
            progress = await self._begin_progress(
                payload,
                operation_name,
                initial_message,
            )
        except ManagedServiceError as exc:
            return {
                "code": 400,
                "status": "error",
                "message": str(exc),
            }

        token = self._progress_context.set(progress)
        terminal_status = "success"
        terminal_message = "操作已完成"
        cancellation_requested = False

        async def run_operation() -> Any:
            await self._progress_stage(
                "running",
                initial_message,
                percent=5,
            )
            return await operation()

        operation_task = asyncio.create_task(run_operation())
        try:
            cancellation_requested = await self._wait_for_progress_task(
                operation_task
            )
            data = operation_task.result()
            response = {
                "code": 200,
                "status": "success",
                "data": data,
            }
        except asyncio.CancelledError:
            terminal_status = "error"
            terminal_message = "操作已取消"
            cancellation_requested = True
        except ManagedServiceError as exc:
            terminal_status = "error"
            terminal_message = str(exc)
            response = {
                "code": 400,
                "status": "error",
                "message": terminal_message,
            }
        except Exception as exc:
            terminal_status = "error"
            terminal_message = f"托管 MaaFW 动作执行失败：{exc}"
            response = {
                "code": 500,
                "status": "error",
                "message": terminal_message,
            }
        finally:
            self._progress_context.reset(token)
            finish_task = asyncio.create_task(
                self._finish_progress(
                    progress,
                    terminal_status,
                    terminal_message,
                )
            )
            cancellation_requested = (
                await self._wait_for_progress_task(finish_task)
                or cancellation_requested
            )
            try:
                finish_task.result()
            except asyncio.CancelledError:
                cancellation_requested = True
        if cancellation_requested:
            raise asyncio.CancelledError
        return response

    async def _convert_project(
        self,
        request: PluginHttpRequest,
    ) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_with_progress(
            payload,
            "convert",
            "正在转换为托管项目",
            lambda: self._convert_project_request(payload),
        )

    async def _convert_project_request(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        script_id = _required_script_id(payload)
        await self._progress_stage(
            "locking",
            "正在锁定项目资源与脚本配置",
            percent=5,
        )
        snapshot_reader = getattr(
            Config,
            "get_plugin_script_type_conversion_snapshot",
            None,
        )
        converter = getattr(Config, "convert_plugin_script_type", None)
        if not callable(snapshot_reader) or not callable(converter):
            raise ManagedServiceError(
                "当前 AUTO-MAS 宿主不支持原子脚本类型转换；"
                "请升级到同时提供 "
                "Config.get_plugin_script_type_conversion_snapshot() 和 "
                "Config.convert_plugin_script_type() 的版本。"
            )

        await self._progress_stage(
            "snapshot",
            "正在读取脚本与用户配置快照",
            percent=10,
        )
        async with Config.script_config_transaction(
            script_id,
            owner=f"maafw-managed-convert-snapshot:{script_id}",
        ):
            records = await Config.get_script_records(script_id)
            if len(records) != 1:
                raise ManagedServiceError(
                    f"scriptId {script_id} 不是唯一脚本"
                )
            record = records[0]
            source_type = str(
                _record_field(record, "type") or ""
            ).strip()
            if source_type == _TARGET_TYPE:
                await self._progress_stage(
                    "validating",
                    "正在核验已完成的托管转换",
                    percent=90,
                )
                return await _committed_conversion_response(
                    record,
                    script_id,
                )
            if source_type != _SOURCE_TYPE:
                raise ManagedServiceError(
                    f"scriptId {script_id} 不是普通 MaaFW 脚本，拒绝转换"
                )

            raw_snapshot = snapshot_reader(script_id)
            if inspect.isawaitable(raw_snapshot):
                raw_snapshot = await raw_snapshot
            snapshot = _validate_conversion_snapshot(
                raw_snapshot,
                script_id,
            )
            if _snapshot_script_type(snapshot) != _SOURCE_TYPE:
                raise ManagedServiceError(
                    "宿主转换快照中的 source type 不是 MaaFW，拒绝导入资源"
                )

            source_config = _conversion_form_config(
                _record_config(record, f"脚本 {script_id}"),
                virtual_name="script_name",
            )
            source_info = _mapping(source_config.get("Info"))
            source_path = str(source_info.get("Path") or "").strip()
            if not source_path:
                raise ManagedServiceError(
                    "普通 MaaFW 脚本没有配置 Info.Path"
                )
            if not Path(source_path).is_dir():
                raise ManagedServiceError(
                    f"普通 MaaFW 项目目录不存在或不可读：{source_path}"
                )

            user_records = await Config.get_user_records(script_id)
            target_user_configs = _conversion_user_configs(
                snapshot,
                user_records,
            )

        project_reservation = await try_reserve_project_path(source_path)
        if project_reservation is None:
            raise ManagedServiceError(
                "MaaFW 项目正在运行、准备或更新，暂不能转换；请稍后重试"
            )
        try:
            gateway = self._gateway()
            async with gateway.resource_transaction():
                async with self._upgrade_lock(script_id):
                    current_snapshot = snapshot_reader(script_id)
                    if inspect.isawaitable(current_snapshot):
                        current_snapshot = await current_snapshot
                    current_snapshot = _validate_conversion_snapshot(
                        current_snapshot,
                        script_id,
                    )
                    if current_snapshot != snapshot:
                        raise ManagedServiceError(
                            "脚本或用户配置在资源导入前已变化，请刷新后重试"
                        )
                    return await self._convert_project_locked(
                        script_id,
                        payload,
                        snapshot=snapshot,
                        source_config=source_config,
                        source_path=source_path,
                        target_user_configs=target_user_configs,
                        snapshot_reader=snapshot_reader,
                        converter=converter,
                        gateway=gateway,
                    )
        finally:
            await release_project_path(project_reservation)

    async def _convert_project_locked(
        self,
        script_id: str,
        payload: Mapping[str, Any],
        *,
        snapshot: Mapping[str, Any],
        source_config: Mapping[str, Any],
        source_path: str,
        target_user_configs: Mapping[str, Mapping[str, Any]],
        snapshot_reader: Callable[..., Any],
        converter: Callable[..., Any],
        gateway: ManagedServiceGateway,
    ) -> dict[str, Any]:
        source_info = _mapping(source_config.get("Info"))
        await self._progress_stage(
            "project-inspection",
            "正在识别 MaaFW 项目身份",
            percent=20,
        )
        interface = await gateway.load_interface(source_path)
        project_id = _conversion_project_id(
            payload,
            source_info,
            interface,
            source_path,
        )
        version = str(
            payload.get("version") or interface.get("version") or ""
        ).strip()
        reference = f"maafw-script:{script_id}"
        import_payload = {
            "sourcePath": source_path,
            "projectId": project_id,
            "version": version,
            "activate": False,
            "runtimeConstraint": str(
                payload.get("runtimeConstraint") or ""
            ).strip(),
            "projectReference": reference,
        }
        await self._progress_stage(
            "project-import",
            "正在导入不可变项目资源",
            percent=35,
        )
        project = await gateway.import_project(import_payload)
        imported_project_id = str(project.get("projectId") or "").strip()
        imported_version = str(project.get("version") or "").strip()
        if not imported_project_id or not imported_version:
            raise ManagedServiceError(
                "Project Store 导入结果缺少 projectId/version"
            )
        if imported_project_id != project_id:
            raise ManagedServiceError(
                "Project Store 返回的 projectId 与转换请求不一致"
            )
        # Existing immutable versions may predate this script reference.  The
        # idempotent add makes the stable reference explicit for both new and
        # reused versions.
        await gateway.add_project_reference(
            imported_project_id,
            imported_version,
            reference,
        )
        await self._progress_stage(
            "project-referenced",
            "项目资源已导入并建立稳定引用",
            percent=60,
        )

        source_fingerprint = _json_hash(snapshot)
        operation_id = _conversion_operation_id(
            script_id,
            source_fingerprint,
            imported_project_id,
            imported_version,
            str(
                project.get("runtimeConstraint")
                or import_payload.get("runtimeConstraint")
                or ""
            ),
        )
        journal = {
            "schemaVersion": 1,
            "kind": _CONVERSION_KIND,
            "operationId": operation_id,
            "scriptId": script_id,
            "sourceType": _SOURCE_TYPE,
            "targetType": _TARGET_TYPE,
            "sourceFingerprint": source_fingerprint,
            "projectReference": reference,
            "projectId": imported_project_id,
            "version": imported_version,
            "state": "project_imported",
            "userIds": list(target_user_configs),
        }
        committed_marker = {
            **journal,
            "state": "committed",
        }
        target_script_config = _merge_conversion_target(
            source_config,
            _project_form_update(
                project,
                import_payload,
                status="普通 MaaFW 项目已原地转换为托管项目",
            ),
        )
        target_script_config.setdefault("Managed", {})[
            "ConversionJournal"
        ] = committed_marker

        await self._progress_stage(
            "config-commit",
            "正在原子提交脚本类型与全部用户配置",
            percent=75,
        )
        try:
            async with Config.script_config_transaction(
                script_id,
                owner=f"maafw-managed-convert-commit:{script_id}",
            ):
                host_result = converter(
                    script_id,
                    source_type=_SOURCE_TYPE,
                    target_type=_TARGET_TYPE,
                    expected_snapshot=snapshot,
                    target_script_config=target_script_config,
                    target_user_configs=target_user_configs,
                    journal=journal,
                )
                if inspect.isawaitable(host_result):
                    host_result = await host_result
                if not isinstance(host_result, Mapping):
                    raise ManagedServiceError(
                        "宿主转换原语返回值不是 JSON object"
                    )
        except Exception as exc:
            if getattr(exc, "conversion_state", None) == "source_changed":
                await gateway.release_project_reference(
                    imported_project_id,
                    imported_version,
                    reference,
                )
                raise ManagedServiceError(
                    "转换失败：脚本或用户配置已变化，配置未提交且已释放项目引用；"
                    "请刷新后重试"
                ) from exc
            commit_state = await _conversion_commit_state(
                script_id,
                operation_id,
                expected_snapshot=snapshot,
                snapshot_reader=snapshot_reader,
            )
            if commit_state == "committed":
                return await self._validated_conversion_response(
                    script_id,
                    project,
                    source_config,
                    target_user_configs,
                    host_result={
                        "converted": True,
                        "recovered": True,
                        "warning": str(exc),
                    },
                )
            if commit_state == "source":
                await gateway.release_project_reference(
                    imported_project_id,
                    imported_version,
                    reference,
                )
                raise ManagedServiceError(
                    f"转换失败，脚本配置未提交，已释放项目引用：{exc}"
                ) from exc
            raise ManagedServiceError(
                "转换结果不确定，已保留项目引用和 durable journal 供宿主恢复；"
                f"请勿删除资源并重试同一转换：{exc}"
            ) from exc

        return await self._validated_conversion_response(
            script_id,
            project,
            source_config,
            target_user_configs,
            host_result=dict(host_result),
        )

    async def _validated_conversion_response(
        self,
        script_id: str,
        project: Mapping[str, Any],
        source_config: Mapping[str, Any],
        source_user_configs: Mapping[str, Mapping[str, Any]],
        *,
        host_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        await self._progress_stage(
            "validating",
            "正在核验脚本、用户顺序与运行历史",
            percent=90,
        )
        records = await Config.get_script_records(script_id)
        if len(records) != 1 or _record_field(records[0], "type") != _TARGET_TYPE:
            raise ManagedServiceError(
                "宿主转换返回成功，但脚本尚未成为 MaaFWManaged"
            )
        converted_config = _conversion_form_config(
            _record_config(records[0], f"脚本 {script_id}"),
            virtual_name="script_name",
        )
        _assert_conversion_script_preserved(source_config, converted_config)
        users = await Config.get_user_records(script_id)
        converted_user_ids = [
            str(_record_field(user, "id") or "").strip()
            for user in users
        ]
        if converted_user_ids != list(source_user_configs):
            raise ManagedServiceError(
                "转换后用户 UUID 或顺序发生变化，宿主必须恢复 journal"
            )
        for user in users:
            user_id = str(_record_field(user, "id") or "").strip()
            converted_user = _conversion_form_config(
                _record_config(user, f"用户 {user_id}"),
                virtual_name="user_name",
            )
            if _upgrade_source_config(converted_user) != _upgrade_source_config(
                source_user_configs[user_id]
            ):
                raise ManagedServiceError(
                    f"转换后用户 {user_id} 配置或运行历史发生变化"
                )
        return {
            "converted": bool(host_result.get("converted", True)),
            "idempotent": bool(host_result.get("idempotent", False)),
            "recovered": bool(host_result.get("recovered", False)),
            "scriptId": script_id,
            "fromType": _SOURCE_TYPE,
            "toType": _TARGET_TYPE,
            "project": dict(project),
            "userIds": converted_user_ids,
            "host": dict(host_result),
        }

    async def _check_remote_project(
        self,
        request: PluginHttpRequest,
    ) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._check_remote_project_locked(script_id, payload),
            ),
        )

    async def _check_remote_project_locked(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        discovery = await self._discover_remote_project(script_id, payload)
        public_discovery = _public_remote_discovery(discovery)

        async def persist() -> dict[str, Any]:
            await self._persist_remote_result(
                script_id,
                public_discovery,
                status=str(discovery.get("message") or "远程资源检查完成"),
            )
            return public_discovery

        return await self._run_config_transaction(
            script_id,
            f"maafw-remote-check:{script_id}",
            persist,
        )

    async def _import_remote_project(
        self,
        request: PluginHttpRequest,
    ) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._download_and_import_remote(
                    script_id,
                    payload,
                    initial=True,
                ),
            ),
            progress_operation="import-remote",
            progress_message="正在下载并导入远程资源",
        )

    async def _upgrade_remote_project(
        self,
        request: PluginHttpRequest,
    ) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._download_and_import_remote(
                    script_id,
                    payload,
                    initial=False,
                ),
            ),
            progress_operation="upgrade-remote",
            progress_message="正在下载资源并生成升级计划",
        )

    async def _download_and_import_remote(
        self,
        script_id: str,
        payload: Mapping[str, Any],
        *,
        initial: bool,
    ) -> dict[str, Any]:
        await self._progress_stage(
            "remote-discovery",
            "正在发现可安装的远程资源版本",
            percent=5,
        )
        discovery = await self._discover_remote_project(script_id, payload)
        candidate = _mapping(discovery.get("candidate"))
        if discovery.get("installable") is not True or not candidate:
            reason = str(
                discovery.get("unavailableReason")
                or discovery.get("message")
                or "远程来源没有可下载候选"
            )
            raise ManagedServiceError(reason)

        download_root = Path.cwd() / "data" / "maafw-managed" / "downloads"
        await self._progress_stage(
            "download:starting",
            "正在下载远程 MaaFW 资源包",
            percent=15,
        )
        downloaded = await self._gateway().download_remote_package(
            download_root,
            candidate,
            progress=self._download_progress_callback(),
        )
        await self._flush_progress_updates()
        await self._progress_stage(
            "download:validated",
            "远程 MaaFW 资源包已下载并校验",
            percent=65,
            downloaded_bytes=_progress_int(downloaded.get("size")),
            total_bytes=_progress_int(downloaded.get("size")),
        )
        imported_payload = dict(payload)
        imported_payload.update(
            {
                "sourcePath": "",
                "sourceArchive": str(downloaded.get("path") or ""),
                "version": str(candidate.get("version") or "").strip(),
            }
        )

        async def persist() -> dict[str, Any]:
            await self._progress_stage(
                "project-import",
                "正在导入不可变项目资源",
                percent=70,
            )
            if initial:
                result = await self._import_initial_project(
                    script_id,
                    imported_payload,
                )
                status = "远程资源已下载、校验并导入"
            else:
                result = await self._stage_and_persist_upgrade(
                    script_id,
                    imported_payload,
                    existing_version=False,
                )
                status = "远程资源已下载并生成待确认升级计划"
            await self._persist_remote_result(
                script_id,
                _public_remote_discovery(discovery),
                status=status,
                downloaded=downloaded,
            )
            await self._progress_stage(
                "config-persisted",
                "远程资源状态已写入脚本配置",
                percent=92,
            )
            return {**result, "download": downloaded}

        return await self._run_config_transaction(
            script_id,
            (
                f"maafw-remote-import:{script_id}"
                if initial
                else f"maafw-remote-stage:{script_id}"
            ),
            persist,
        )

    async def _discover_remote_project(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = await _managed_script_record(script_id)
        config = _record_config(record, "脚本")
        managed = _mapping(config.get("Managed"))
        project_id, version = managed_project_identity(managed)
        gateway = self._gateway()
        if project_id and version:
            project = await gateway.resolve_project(project_id, version)
            interface = await gateway.load_interface(_project_data_path(project))
            current_version = str(project.get("version") or version)
            mode = "upgrade"
        else:
            requested_project_id = str(payload.get("projectId") or "").strip()
            if not requested_project_id:
                raise ManagedServiceError(
                    "首次远程导入前请填写 ImportProjectId"
                )
            interface = _remote_probe_interface(payload, requested_project_id)
            current_version = "0.0.0"
            mode = "initial"

        discovery = await gateway.discover_remote_update(
            interface,
            current_version=current_version,
            source_config=_remote_source_config(payload),
        )
        if discovery is None:
            return {
                "mode": mode,
                "currentVersion": current_version,
                "latestVersion": current_version if mode == "upgrade" else "",
                "updateAvailable": False,
                "installable": False,
                "candidate": None,
                "unavailableReason": "",
                "message": (
                    "当前资源已是远程来源可用的最新版本"
                    if mode == "upgrade"
                    else "远程来源未发现可导入版本"
                ),
            }

        candidate = _mapping(discovery.get("candidate"))
        installable = bool(candidate) and bool(
            str(
                candidate.get("download_url")
                or candidate.get("downloadUrl")
                or ""
            ).strip()
        )
        latest_version = str(discovery.get("version") or "").strip()
        unavailable_reason = str(
            discovery.get("unavailable_reason")
            or discovery.get("unavailableReason")
            or ""
        ).strip()
        return {
            "mode": mode,
            "currentVersion": current_version,
            "latestVersion": latest_version,
            "updateAvailable": True,
            "installable": installable,
            "candidate": candidate or None,
            "unavailableReason": unavailable_reason,
            "message": (
                f"发现可下载远程资源 {latest_version}"
                if installable
                else f"发现远程版本 {latest_version}，但没有可下载资源："
                f"{unavailable_reason or '来源未返回下载地址'}"
            ),
        }

    @staticmethod
    async def _persist_remote_result(
        script_id: str,
        discovery: Mapping[str, Any],
        *,
        status: str,
        downloaded: Mapping[str, Any] | None = None,
    ) -> None:
        update: dict[str, Any] = {
            "LatestVersion": str(discovery.get("latestVersion") or ""),
            "Installable": discovery.get("installable") is True,
            "Status": status,
            "Discovery": dict(discovery),
        }
        if downloaded is not None:
            update["LastDownload"] = dict(downloaded)
        await Config.update_script(
            script_id,
            {"ManagedRemote": update},
        )

    async def _import_project(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._run_config_transaction(
                    script_id,
                    f"maafw-resource-import:{script_id}",
                    lambda: self._import_initial_project(script_id, payload),
                ),
            ),
            progress_operation="import-local",
            progress_message="正在导入本地项目资源",
        )

    async def _upgrade_project(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._run_config_transaction(
                    script_id,
                    f"maafw-resource-stage:{script_id}",
                    lambda: self._stage_and_persist_upgrade(
                        script_id,
                        payload,
                        existing_version=False,
                    ),
                ),
            ),
            progress_operation="upgrade-local",
            progress_message="正在导入资源并生成升级计划",
        )

    async def _apply_upgrade(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._apply_pending_upgrade_transaction(
                    script_id,
                    payload,
                ),
            ),
            progress_operation="apply-upgrade",
            progress_message="正在校验并应用升级计划",
        )

    async def _cancel_upgrade(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._cancel_pending_upgrade_transaction(script_id),
            ),
            progress_operation="cancel-upgrade",
            progress_message="正在取消待确认升级",
        )

    async def _switch_version(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._run_config_transaction(
                    script_id,
                    f"maafw-resource-stage-existing:{script_id}",
                    lambda: self._stage_and_persist_upgrade(
                        script_id,
                        payload,
                        existing_version=True,
                    ),
                ),
            ),
            progress_operation="switch-version",
            progress_message="正在生成版本切换计划",
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

    async def _upgrade_local_with_plan(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        await self._progress_stage(
            "upgrade-validation",
            "正在校验当前项目与目标版本",
            percent=15,
        )
        record = await _managed_script_record(script_id)
        managed = _mapping(_record_config(record, "脚本").get("Managed"))
        project_id, current_version = _bound_upgrade_target(managed, payload)
        plan_id = uuid.uuid4().hex
        pending_reference = f"maafw-upgrade:{script_id}:{plan_id}"
        request = dict(payload)
        request["projectReference"] = pending_reference
        request["currentVersion"] = current_version
        await self._progress_stage(
            "project-import",
            "正在导入不可变升级资源",
            percent=40,
        )
        result = await self._gateway().upgrade_project(request)
        if str(_mapping(result.get("project")).get("projectId") or "") != project_id:
            raise ManagedServiceError("Project Store 返回了不同项目的升级版本")
        await self._progress_stage(
            "upgrade-plan",
            "资源已导入，正在生成逐用户升级计划",
            percent=70,
        )
        return await self._attach_upgrade_plan(
            script_id,
            result,
            plan_id=plan_id,
            pending_reference=pending_reference,
        )

    async def _import_initial_project(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        await self._progress_stage(
            "import-validation",
            "正在校验首次导入绑定",
            percent=15,
        )
        record = await _managed_script_record(script_id)
        config = _record_config(record, "脚本")
        managed = _mapping(config.get("Managed"))
        bound_project_id, bound_version = managed_project_identity(managed)
        if (
            bound_project_id
            or bound_version
            or str(_mapping(config.get("Info")).get("Path") or "").strip()
        ):
            raise ManagedServiceError(
                "当前脚本已经绑定资源；请使用“本地升级”生成配置计划，"
                "不能用首次导入绕过升级事务。"
            )
        configured_project_id = str(
            managed.get("ImportProjectId") or ""
        ).strip()
        requested_project_id = str(payload.get("projectId") or "").strip()
        if not requested_project_id:
            raise ManagedServiceError("请填写首次导入项目 ID")
        if (
            configured_project_id
            and configured_project_id != requested_project_id
        ):
            raise ManagedServiceError(
                "首次导入的项目 ID 与脚本中的 ImportProjectId 不一致"
            )
        request = _with_project_reference(payload, script_id)
        await self._progress_stage(
            "project-import",
            "正在导入不可变项目资源",
            percent=40,
        )
        result = await self._gateway().import_project(request)
        try:
            await self._progress_stage(
                "config-persist",
                "正在写入项目绑定与可用版本",
                percent=80,
            )
            await self._persist_project(
                script_id,
                result,
                payload,
                status="资源版本已导入并激活",
            )
        except Exception:
            project_id = str(result.get("projectId") or "").strip()
            version = str(result.get("version") or "").strip()
            reference = str(request.get("projectReference") or "").strip()
            if project_id and version and reference:
                try:
                    await self._gateway().release_project_reference(
                        project_id,
                        version,
                        reference,
                    )
                except Exception:
                    pass
            raise
        return result

    async def _stage_and_persist_upgrade(
        self,
        script_id: str,
        payload: Mapping[str, Any],
        *,
        existing_version: bool,
    ) -> dict[str, Any]:
        result = (
            await self._stage_existing_version(script_id, payload)
            if existing_version
            else await self._upgrade_local_with_plan(script_id, payload)
        )
        await self._progress_stage(
            "plan-persist",
            "正在持久化待确认升级计划",
            percent=85,
        )
        await self._persist_upgrade_result(script_id, result, payload)
        return result

    async def _stage_existing_version(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        await self._progress_stage(
            "version-validation",
            "正在校验已安装目标版本",
            percent=20,
        )
        record = await _managed_script_record(script_id)
        managed = _mapping(_record_config(record, "脚本").get("Managed"))
        project_id, current_version = _bound_upgrade_target(managed, payload)
        target_version = str(payload.get("version") or "").strip()
        if not target_version or target_version == current_version:
            raise ManagedServiceError("请选择不同于当前绑定的已安装目标版本")
        gateway = self._gateway()
        previous_project = await gateway.resolve_project(project_id, current_version)
        project = await gateway.resolve_project(project_id, target_version)
        plan_id = uuid.uuid4().hex
        pending_reference = f"maafw-upgrade:{script_id}:{plan_id}"
        await gateway.add_project_reference(
            project_id,
            target_version,
            pending_reference,
        )
        await self._progress_stage(
            "upgrade-plan",
            "目标版本已引用，正在生成切换计划",
            percent=70,
        )
        return await self._attach_upgrade_plan(
            script_id,
            {
                "updated": False,
                "activated": False,
                "currentVersion": current_version,
                "latestVersion": target_version,
                "previousProject": previous_project,
                "project": project,
            },
            plan_id=plan_id,
            pending_reference=pending_reference,
        )

    async def _attach_upgrade_plan(
        self,
        script_id: str,
        raw_result: Mapping[str, Any],
        *,
        plan_id: str,
        pending_reference: str,
    ) -> dict[str, Any]:
        result = dict(raw_result)
        try:
            plan = await self._build_pack_upgrade_plan(
                script_id,
                result,
                plan_id=plan_id,
                pending_reference=pending_reference,
            )
        except Exception as exc:
            result["upgradePlanError"] = str(exc)
            plan = _failed_upgrade_envelope(
                script_id,
                result,
                plan_id=plan_id,
                pending_reference=pending_reference,
                error=str(exc),
            )
        result["_upgradePlanInternal"] = plan
        result["upgradePlan"] = _public_upgrade_plan(plan)
        return result

    async def _build_pack_upgrade_plan(
        self,
        script_id: str,
        result: Mapping[str, Any],
        *,
        plan_id: str,
        pending_reference: str,
    ) -> dict[str, Any]:
        project = _mapping(result.get("project"))
        previous_project = _mapping(result.get("previousProject"))
        project_id = str(project.get("projectId") or "").strip()
        if not project_id:
            raise ManagedServiceError("待升级资源缺少 projectId")

        registry = self.ctx.get("maafw.registry.v1")
        get_pack = getattr(registry, "get_project_pack", None)
        if not callable(get_pack):
            raise ManagedServiceError("maafw.registry.v1 未提供 project pack 查询")
        definition = get_pack(project_id)
        definition_data = _mapping(definition)
        service_key = str(
            definition_data.get("resource_service_key")
            or definition_data.get("resourceServiceKey")
            or ""
        ).strip()
        if not service_key:
            raise ManagedServiceError(
                f"项目 pack {project_id} 未声明 resource_service_key"
            )
        service = self.ctx.get(service_key)
        planner = getattr(service, "plan_resource_upgrade", None)
        if not callable(planner):
            raise ManagedServiceError(
                f"{service_key} 未实现 plan_resource_upgrade"
            )

        records = await Config.get_script_records(script_id)
        if len(records) != 1:
            raise ManagedServiceError(
                f"生成资源升级计划时无法唯一解析脚本 {script_id}"
            )
        old_path = _project_data_path(previous_project)
        new_path = _project_data_path(project)
        from_version = str(previous_project.get("version") or "").strip()
        to_version = str(project.get("version") or "").strip()
        if not from_version or not to_version:
            raise ManagedServiceError("升级项目缺少来源或目标版本")
        scopes = [
            await self._plan_upgrade_record(
                planner,
                service_key,
                project_id,
                from_version,
                to_version,
                old_path,
                new_path,
                scope="script",
                record=records[0],
            )
        ]
        try:
            user_records = await Config.get_user_records(script_id)
        except Exception as exc:
            raise ManagedServiceError(
                f"读取脚本 {script_id} 的用户配置失败：{exc}"
            ) from exc
        for record in user_records:
            scopes.append(
                await self._plan_upgrade_record(
                    planner,
                    service_key,
                    project_id,
                    from_version,
                    to_version,
                    old_path,
                    new_path,
                    scope="user",
                    record=record,
                )
            )

        errors: list[dict[str, Any]] = []
        manual_actions: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for scope in scopes:
            scope_data = {
                "scope": scope.get("scope"),
                "recordId": scope.get("recordId"),
                "name": scope.get("name"),
            }
            error = str(scope.get("error") or "").strip()
            if error:
                errors.append({**scope_data, "message": error})
            scope_plan = _mapping(scope.get("plan"))
            for action in scope_plan.get("manualActions") or []:
                if isinstance(action, Mapping):
                    manual_actions.append({**scope_data, "action": dict(action)})
            for warning in scope_plan.get("warnings") or []:
                if isinstance(warning, str) and warning.strip():
                    warnings.append({**scope_data, "message": warning.strip()})

        mode = str(
            definition_data.get("resource_upgrade_mode")
            or definition_data.get("resourceUpgradeMode")
            or "plan-only"
        ).strip()
        ready = (
            not errors
            and not manual_actions
            and all(
                _mapping(scope.get("plan")).get("readyToApply") is True
                and _mapping(scope.get("plan")).get("lossless") is True
                for scope in scopes
            )
        )
        state = "plan_error" if errors else "ready" if ready else "blocked"
        confirmation_token = f"{project_id}@{to_version}#{plan_id}"
        return {
            "schemaVersion": 1,
            "kind": _PENDING_KIND,
            "planId": plan_id,
            "state": state,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "scriptId": script_id,
            "project": {
                "projectId": project_id,
                "fromVersion": from_version,
                "toVersion": to_version,
                "fromHash": _project_source_hash(previous_project),
                "toHash": _project_source_hash(project),
                "pendingReference": pending_reference,
            },
            "planner": {
                "serviceKey": service_key,
                "method": "plan_resource_upgrade",
                "mode": mode,
            },
            "script": scopes[0],
            "users": scopes[1:],
            "userIds": [str(scope.get("recordId") or "") for scope in scopes[1:]],
            "planCount": len(scopes),
            "errors": errors,
            "manualActions": manual_actions,
            "warnings": warnings,
            "lossless": all(
                _mapping(scope.get("plan")).get("lossless") is True
                for scope in scopes
                if not scope.get("error")
            )
            and not errors,
            "readyToApply": ready,
            "confirmationToken": confirmation_token,
        }

    async def _plan_upgrade_record(
        self,
        planner: Callable[..., Any],
        service_key: str,
        project_id: str,
        from_version: str,
        to_version: str,
        old_path: str,
        new_path: str,
        *,
        scope: str,
        record: Any,
    ) -> dict[str, Any]:
        record_id = str(_record_field(record, "id") or "").strip()
        name = str(_record_field(record, "name") or "")
        raw_config = _record_field(record, "config")
        if not record_id or not isinstance(raw_config, Mapping):
            return {
                "scope": scope,
                "recordId": record_id,
                "name": name,
                "error": "记录缺少稳定 ID 或 JSON 配置",
            }
        source_config = _upgrade_source_config(raw_config)
        try:
            raw_plan = await self._invoke_pack_planner(
                planner,
                service_key,
                old_path,
                new_path,
                source_config,
            )
            plan = _validate_pack_upgrade_plan(
                raw_plan,
                service_key=service_key,
                project_id=project_id,
                from_version=from_version,
                to_version=to_version,
            )
            target_config = _upgrade_source_config(
                _required_plan_config(plan, f"{scope} {record_id}")
            )
        except Exception as exc:
            return {
                "scope": scope,
                "recordId": record_id,
                "name": name,
                "sourceHash": _json_hash(source_config),
                "sourceConfig": source_config,
                "error": str(exc),
            }
        return {
            "scope": scope,
            "recordId": record_id,
            "name": name,
            "sourceHash": _json_hash(source_config),
            "targetHash": _json_hash(target_config),
            "sourceConfig": source_config,
            "targetConfig": target_config,
            "plan": plan,
        }

    @staticmethod
    async def _invoke_pack_planner(
        planner: Callable[..., Any],
        service_key: str,
        old_path: str,
        new_path: str,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        if inspect.iscoroutinefunction(planner):
            plan = await planner(old_path, new_path, dict(config))
        else:
            plan = await asyncio.to_thread(
                planner,
                old_path,
                new_path,
                dict(config),
            )
            if inspect.isawaitable(plan):
                plan = await plan
        if not isinstance(plan, Mapping):
            raise ManagedServiceError(
                f"{service_key}.plan_resource_upgrade 必须返回 JSON object"
            )
        return dict(plan)

    async def _apply_pending_upgrade(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        await self._progress_stage(
            "upgrade-validation",
            "正在校验升级计划、配置 CAS 与项目版本",
            percent=15,
        )
        _record, current_script, pending = await self._load_pending_upgrade(script_id)
        state = str(pending.get("state") or "")
        if state in _INTERRUPTED_STATES:
            await self._rollback_pending_upgrade(script_id, pending)
            raise ManagedServiceError(
                "检测到上次升级在提交中中断，已恢复旧版本与旧配置；"
                "请检查状态后再次应用。"
            )
        if state != "ready" or pending.get("readyToApply") is not True:
            raise ManagedServiceError(
                "当前升级计划不是可应用状态；人工动作或规划错误未解决。"
            )
        plan_id = str(pending.get("planId") or "").strip()
        if str(payload.get("planId") or "").strip() != plan_id:
            raise ManagedServiceError("页面中的 planId 已过期，请刷新后重试")
        expected = str(pending.get("confirmationToken") or "").strip()
        if str(payload.get("confirmation") or "").strip() != expected:
            raise ManagedServiceError(f"应用升级前请完整输入确认令牌 {expected}")

        project_info = _mapping(pending.get("project"))
        project_id = str(project_info.get("projectId") or "").strip()
        from_version = str(project_info.get("fromVersion") or "").strip()
        to_version = str(project_info.get("toVersion") or "").strip()
        gateway = self._gateway()
        previous_project = await gateway.resolve_project(project_id, from_version)
        project = await gateway.resolve_project(project_id, to_version)
        user_journals = await self._load_user_upgrade_journals(
            script_id,
            pending,
        )
        try:
            _assert_pending_fresh(
                current_script,
                pending,
                user_journals,
                previous_project,
                project,
            )
        except ManagedServiceError as exc:
            await self._set_upgrade_state(
                script_id,
                pending,
                "stale",
                error=str(exc),
            )
            raise
        script_entry = _mapping(pending.get("script"))
        script_target = script_entry.get("targetConfig")
        if not isinstance(script_target, Mapping):
            raise ManagedServiceError("持久化脚本计划缺少 targetConfig")
        public_plan = _public_upgrade_plan(pending)
        applied_users: list[str] = []
        await self._progress_stage(
            "upgrade-prepare",
            "升级计划已通过校验，正在进入可恢复提交阶段",
            percent=35,
        )
        await self._set_upgrade_state(script_id, pending, "applying")
        _applying_record, applying_script, _applying_pending = (
            await self._load_pending_upgrade(script_id)
        )
        user_journals = await self._load_user_upgrade_journals(
            script_id,
            pending,
        )
        try:
            _assert_pending_fresh(
                applying_script,
                pending,
                user_journals,
                previous_project,
                project,
            )
        except ManagedServiceError as exc:
            await self._set_upgrade_state(
                script_id,
                pending,
                "stale",
                error=str(exc),
            )
            raise
        try:
            await self._progress_stage(
                "config-apply",
                "正在应用脚本与用户配置计划",
                percent=50,
            )
            await Config.update_script(
                script_id,
                _atomic_json_field_update(script_target),
            )
            for user_id, journal in user_journals.items():
                target_config = journal.get("targetConfig")
                if not isinstance(target_config, Mapping):
                    raise ManagedServiceError(
                        f"用户 {user_id} 持久化计划缺少 targetConfig"
                    )
                await Config.update_user(
                    script_id,
                    user_id,
                    _atomic_json_field_update(target_config),
                )
                applied_users.append(user_id)
            await self._set_upgrade_state(script_id, pending, "committing")
            await self._progress_stage(
                "version-switch",
                "配置已应用，正在切换项目资源版本",
                percent=75,
            )
            activated = await gateway.switch_version(
                {"projectId": project_id, "version": to_version}
            )
            final_update = _project_form_update(
                activated,
                payload,
                status=f"已应用配置计划并切换到资源版本 {to_version}",
            )
            final_update["Managed"].update(
                {
                    **_cleared_pending_upgrade(),
                    "UpgradePlan": public_plan,
                    "UpgradeReady": True,
                    "UpgradePlanStatus": "配置计划已应用",
                    "LastUpgrade": {
                        "planId": plan_id,
                        "fromVersion": from_version,
                        "toVersion": to_version,
                        "appliedAt": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )
            await Config.update_script(script_id, final_update)
        except Exception as exc:
            try:
                await self._progress_stage(
                    "rollback",
                    "升级提交失败，正在恢复旧版本与旧配置",
                    percent=80,
                )
                await self._rollback_pending_upgrade(
                    script_id,
                    pending,
                    user_journals=user_journals,
                )
            except Exception as rollback_exc:
                raise ManagedServiceError(
                    f"应用资源升级失败且回滚不完整：{exc}；{rollback_exc}"
                ) from exc
            raise ManagedServiceError(
                f"应用资源升级失败，已恢复旧版本与旧配置：{exc}"
            ) from exc

        warnings: list[str] = []
        await self._progress_stage(
            "reconcile",
            "资源版本已切换，正在对账引用并收尾",
            percent=92,
        )
        try:
            await self._refresh_project_versions_and_references(
                script_id,
                project_id,
            )
        except Exception as exc:
            warnings.append(f"引用对账失败：{exc}")
        try:
            await self._clear_user_upgrade_journals(script_id, user_journals)
        except Exception as exc:
            warnings.append(f"用户升级 journal 清理失败：{exc}")
        if warnings:
            try:
                await Config.update_script(
                    script_id,
                    {
                        "Managed": {
                            "Status": (
                                f"资源已切换到 {to_version}，"
                                f"但收尾需要重试：{'；'.join(warnings)}"
                            )
                        }
                    },
                )
            except Exception as exc:
                warnings.append(f"状态提示写入失败：{exc}")
        return {
            "applied": True,
            "project": project,
            "previousProject": previous_project,
            "upgradePlan": public_plan,
            "appliedUsers": applied_users,
            "warnings": warnings,
        }

    async def _apply_pending_upgrade_transaction(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan_id = str(payload.get("planId") or "").strip()
        owner = f"maafw-resource-upgrade:{script_id}:{plan_id or 'unknown'}"
        async with Config.script_config_transaction(
            script_id,
            owner=owner,
        ):
            return await self._apply_pending_upgrade(script_id, payload)

    async def _cancel_pending_upgrade(self, script_id: str) -> dict[str, Any]:
        await self._progress_stage(
            "cancel-validation",
            "正在读取并校验待确认升级计划",
            percent=20,
        )
        _record, _config, pending = await self._load_pending_upgrade(script_id)
        if str(pending.get("state") or "") in _INTERRUPTED_STATES:
            await self._rollback_pending_upgrade(script_id, pending)
            _record, _config, pending = await self._load_pending_upgrade(script_id)
        project = _mapping(pending.get("project"))
        project_id = str(project.get("projectId") or "").strip()
        pending_version = str(project.get("toVersion") or "").strip()
        pending_reference = str(project.get("pendingReference") or "").strip()
        user_journals = await self._load_user_upgrade_journals(
            script_id,
            pending,
            allow_missing=True,
        )
        await self._progress_stage(
            "cancel-config",
            "正在清除脚本与用户升级 journal",
            percent=55,
        )
        await self._clear_user_upgrade_journals(script_id, user_journals)
        await Config.update_script(
            script_id,
            {
                "Managed": {
                    **_cleared_pending_upgrade(),
                    "Status": (
                        f"已取消切换到 {pending_version}；"
                        "导入的不可变版本仍保留，可稍后重新选择或删除"
                    ),
                }
            },
        )
        try:
            await self._progress_stage(
                "cancel-reconcile",
                "升级计划已取消，正在对账资源引用",
                percent=85,
            )
            await self._refresh_project_versions_and_references(
                script_id,
                project_id,
            )
        except Exception:
            await self._gateway().release_project_reference(
                project_id,
                pending_version,
                pending_reference,
            )
        return {
            "cancelled": True,
            "projectId": project_id,
            "version": pending_version,
        }

    async def _cancel_pending_upgrade_transaction(
        self,
        script_id: str,
    ) -> dict[str, Any]:
        owner = f"maafw-resource-cancel:{script_id}"
        async with Config.script_config_transaction(
            script_id,
            owner=owner,
        ):
            return await self._cancel_pending_upgrade(script_id)

    async def _load_pending_upgrade(
        self,
        script_id: str,
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        record = await _managed_script_record(script_id)
        config = _record_config(record, "脚本")
        managed = _mapping(config.get("Managed"))
        pending = _mapping(managed.get("PendingUpgrade"))
        if (
            pending.get("schemaVersion") != 1
            or pending.get("kind") != _PENDING_KIND
            or str(pending.get("scriptId") or "") != script_id
            or not str(pending.get("planId") or "").strip()
        ):
            raise ManagedServiceError("当前脚本没有有效的待确认升级 journal")
        return record, config, pending

    async def _recover_interrupted_upgrades(self) -> None:
        try:
            records = await Config.get_script_records()
        except Exception as exc:
            self.ctx.logger.warning(f"无法扫描 MaaFW 升级 journal：{exc}")
            return
        for record in records:
            try:
                if _record_field(record, "type") != "MaaFWManaged":
                    continue
                script_id = str(_record_field(record, "id") or "").strip()
                if not script_id:
                    continue
                config = _record_config(record, f"脚本 {script_id}")
                pending = _mapping(
                    _mapping(config.get("Managed")).get("PendingUpgrade")
                )
                if str(pending.get("state") or "") not in _INTERRUPTED_STATES:
                    continue
                candidate_plan_id = str(pending.get("planId") or "").strip()
                if not candidate_plan_id:
                    continue
                async with self._gateway().resource_transaction():
                    async with self._upgrade_lock(script_id):
                        async with Config.script_config_transaction(
                            script_id,
                            owner=f"maafw-resource-recovery:{script_id}",
                        ):
                            try:
                                _record, _config, fresh_pending = (
                                    await self._load_pending_upgrade(script_id)
                                )
                            except ManagedServiceError:
                                continue
                            if (
                                str(fresh_pending.get("planId") or "").strip()
                                != candidate_plan_id
                                or str(fresh_pending.get("state") or "")
                                not in _INTERRUPTED_STATES
                            ):
                                continue
                            await self._rollback_pending_upgrade(
                                script_id,
                                fresh_pending,
                            )
            except Exception as exc:
                self.ctx.logger.error(
                    "恢复 MaaFW 升级事务失败："
                    f"{str(_record_field(record, 'id') or '<unknown>')}：{exc}"
                )

    async def _repair_upgrade_artifacts_on_start(self) -> None:
        try:
            records = await Config.get_script_records()
        except Exception as exc:
            self.ctx.logger.warning(f"无法扫描 MaaFW 升级残留：{exc}")
            return
        for record in records:
            try:
                if _record_field(record, "type") != "MaaFWManaged":
                    continue
                script_id = str(_record_field(record, "id") or "").strip()
                if not script_id:
                    continue
                async with self._gateway().resource_transaction():
                    async with self._upgrade_lock(script_id):
                        async with Config.script_config_transaction(
                            script_id,
                            owner=f"maafw-resource-repair:{script_id}",
                        ):
                            fresh_record = await _managed_script_record(script_id)
                            config = _record_config(
                                fresh_record,
                                f"脚本 {script_id}",
                            )
                            pending = _mapping(
                                _mapping(config.get("Managed")).get(
                                    "PendingUpgrade"
                                )
                            )
                            active_plan_id = (
                                str(pending.get("planId") or "").strip()
                                if pending.get("schemaVersion") == 1
                                and pending.get("kind") == _PENDING_KIND
                                else ""
                            )
                            users = await Config.get_user_records(script_id)
                            for user in users:
                                user_id = str(
                                    _record_field(user, "id") or ""
                                ).strip()
                                if not user_id:
                                    continue
                                user_config = _record_config(
                                    user,
                                    f"用户 {user_id}",
                                )
                                journal = _mapping(
                                    _mapping(
                                        user_config.get("ManagedUpgrade")
                                    ).get("PendingPlan")
                                )
                                if journal and (
                                    journal.get("schemaVersion") != 1
                                    or journal.get("kind") != _USER_PENDING_KIND
                                    or str(journal.get("planId") or "").strip()
                                    != active_plan_id
                                ):
                                    await Config.update_user(
                                        script_id,
                                        user_id,
                                        {
                                            "ManagedUpgrade": {
                                                "PendingPlan": "{}"
                                            }
                                        },
                                    )
            except Exception as exc:
                self.ctx.logger.warning(
                    "清理 MaaFW 升级残留失败："
                    f"{str(_record_field(record, 'id') or '<unknown>')}：{exc}"
                )
        try:
            gateway = self._gateway()
            async with gateway.resource_transaction():
                async with Config.script_config_write_scope(None):
                    await gateway.reconcile_project_references(
                        await _managed_script_record_dtos()
                    )
        except Exception as exc:
            self.ctx.logger.warning(f"MaaFW 项目引用启动对账失败：{exc}")

    async def _load_user_upgrade_journals(
        self,
        script_id: str,
        pending: Mapping[str, Any],
        *,
        allow_missing: bool = False,
    ) -> dict[str, dict[str, Any]]:
        records = await Config.get_user_records(script_id)
        by_id = {
            str(_record_field(record, "id") or ""): record
            for record in records
        }
        expected_ids = [
            str(item)
            for item in pending.get("userIds") or []
            if str(item).strip()
        ]
        if not allow_missing and set(by_id) != set(expected_ids):
            raise ManagedServiceError(
                "用户集合在规划后发生变化，计划已过期；请取消后重新生成。"
            )
        journals: dict[str, dict[str, Any]] = {}
        for user_id in expected_ids:
            record = by_id.get(user_id)
            if record is None:
                if allow_missing:
                    continue
                raise ManagedServiceError(f"升级计划中的用户 {user_id} 已不存在")
            config = _record_config(record, f"用户 {user_id}")
            journal = _mapping(
                _mapping(config.get("ManagedUpgrade")).get("PendingPlan")
            )
            if (
                journal.get("schemaVersion") != 1
                or journal.get("kind") != _USER_PENDING_KIND
                or journal.get("planId") != pending.get("planId")
                or journal.get("recordId") != user_id
            ):
                if allow_missing:
                    continue
                raise ManagedServiceError(f"用户 {user_id} 的升级 journal 缺失或过期")
            journal["_currentConfig"] = config
            journals[user_id] = journal
        return journals

    async def _set_upgrade_state(
        self,
        script_id: str,
        pending: Mapping[str, Any],
        state: str,
        *,
        error: str = "",
    ) -> dict[str, Any]:
        updated = dict(pending)
        updated["state"] = state
        updated["updatedAt"] = datetime.now(timezone.utc).isoformat()
        if error:
            updated["recoveryError"] = error
        else:
            updated.pop("recoveryError", None)
        await Config.update_script(
            script_id,
            {
                "Managed": {
                    "PendingUpgrade": json.dumps(
                        updated,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "UpgradePlanStatus": state,
                }
            },
        )
        return updated

    async def _rollback_pending_upgrade(
        self,
        script_id: str,
        pending: Mapping[str, Any],
        *,
        user_journals: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        project = _mapping(pending.get("project"))
        project_id = str(project.get("projectId") or "").strip()
        from_version = str(project.get("fromVersion") or "").strip()
        if user_journals is not None:
            journals = dict(user_journals)
        else:
            try:
                journals = await self._load_user_upgrade_journals(
                    script_id,
                    pending,
                    allow_missing=False,
                )
            except Exception as exc:
                try:
                    await self._set_upgrade_state(
                        script_id,
                        pending,
                        "recovery_required",
                        error=str(exc),
                    )
                except Exception:
                    pass
                raise ManagedServiceError(
                    "升级恢复缺少完整用户 journal，已保持运行阻断状态："
                    f"{exc}"
                ) from exc
        errors: list[str] = []
        try:
            await self._gateway().switch_version(
                {"projectId": project_id, "version": from_version}
            )
        except Exception as exc:
            errors.append(f"资源版本：{exc}")
        for user_id, journal in journals.items():
            source = journal.get("sourceConfig")
            if not isinstance(source, Mapping):
                errors.append(f"用户 {user_id} 缺少 sourceConfig")
                continue
            try:
                await Config.update_user(
                    script_id,
                    user_id,
                    _atomic_json_field_update(source),
                )
            except Exception as exc:
                errors.append(f"用户 {user_id}：{exc}")
        script_source = _mapping(pending.get("script")).get("sourceConfig")
        if not isinstance(script_source, Mapping):
            errors.append("脚本缺少 sourceConfig")
        else:
            try:
                await Config.update_script(
                    script_id,
                    _atomic_json_field_update(script_source),
                )
            except Exception as exc:
                errors.append(f"脚本：{exc}")
        if errors:
            await self._set_upgrade_state(
                script_id,
                pending,
                "rollback_failed",
                error="；".join(errors),
            )
            raise ManagedServiceError("升级恢复失败：" + "；".join(errors))
        await self._set_upgrade_state(script_id, pending, "ready")
        await Config.update_script(
            script_id,
            {
                "Managed": {
                    "Status": "已从中断的升级事务恢复旧版本与旧配置",
                }
            },
        )

    @staticmethod
    async def _clear_user_upgrade_journals(
        script_id: str,
        journals: Mapping[str, Mapping[str, Any]],
    ) -> None:
        for user_id in journals:
            await Config.update_user(
                script_id,
                user_id,
                {"ManagedUpgrade": {"PendingPlan": "{}"}},
            )

    async def _list_projects(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._gateway().list_projects(),
            after_success=lambda script_id, data: Config.update_script(
                script_id,
                {
                    "Managed": {
                        "AvailableProjects": data,
                        "Status": "托管资源总览已刷新",
                    }
                },
            ),
        )

    async def _delete_version(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._delete_version_transaction(
                    script_id,
                    payload,
                ),
            ),
            progress_operation="delete-version",
            progress_message="正在删除项目版本",
        )

    async def _install_runtime(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda script_id: self._run_upgrade_locked(
                script_id,
                lambda: self._install_runtime_transaction(script_id, payload),
            ),
            progress_operation="install-runtime",
            progress_message="正在安装或复用共享运行时",
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
            lambda _script_id: self._run_resource_locked(
                lambda: self._gateway().delete_runtime(payload)
            ),
            after_success=lambda script_id, data: self._persist_runtime_delete(
                script_id,
                data,
            ),
            progress_operation="delete-runtime",
            progress_message="正在删除共享运行时",
        )

    async def _pin(self, request: PluginHttpRequest) -> dict[str, Any]:
        payload = _payload(request)
        return await self._respond_for_script(
            payload,
            lambda _script_id: self._run_resource_locked(
                lambda: self._gateway().pin(payload)
            ),
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
            progress_operation="pin",
            progress_message=(
                "正在固定资源"
                if _as_bool(payload.get("pinned"), True)
                else "正在取消固定"
            ),
        )

    async def _collect_garbage(
        self,
        request: PluginHttpRequest,
    ) -> dict[str, Any]:
        payload = _payload(request)
        dry_run = _as_bool(payload.get("dryRun"), True)

        async def collect(_script_id: str) -> dict[str, Any]:
            if (
                not dry_run
                and str(payload.get("confirmation") or "")
                != "DELETE UNUSED"
            ):
                raise ManagedServiceError(
                    "实际回收前请在确认字段中输入 DELETE UNUSED"
                )
            return await self._collect_garbage_with_script_references(
                payload,
                dry_run=dry_run,
            )

        return await self._respond_for_script(
            payload,
            collect,
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
            progress_operation="gc-preview" if dry_run else "gc-apply",
            progress_message=(
                "正在预览空间回收"
                if dry_run
                else "正在回收过期资源"
            ),
        )

    async def _resolve_and_bind_runtime(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        request = dict(payload)
        request["projectReference"] = f"maafw-script:{script_id}"
        await self._progress_stage(
            "runtime-resolve",
            "正在解析项目依赖并安装或复用共享运行时",
            percent=25,
        )
        resolution = await self._gateway().resolve_execution(request)
        project = _mapping(resolution.get("project"))
        expected_project_id = str(request.get("projectId") or "").strip()
        expected_version = str(request.get("version") or "").strip()
        if str(project.get("projectId") or "").strip() != expected_project_id:
            raise ManagedServiceError("运行时解析返回了不同的项目 ID")
        if str(project.get("version") or "").strip() != expected_version:
            raise ManagedServiceError("运行时解析返回了不同的项目版本")
        binding = _mapping(resolution.get("runtime"))
        await self._progress_stage(
            "runtime-bind",
            "共享运行时已就绪，正在绑定项目资源",
            percent=75,
        )
        resolution["project"] = await self._gateway().bind_project_runtime(
            expected_project_id,
            expected_version,
            binding,
            project_reference=request["projectReference"],
        )
        return resolution

    async def _install_runtime_transaction(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        async with Config.script_config_transaction(
            script_id,
            owner=f"maafw-runtime-install:{script_id}",
        ):
            await self._progress_stage(
                "runtime-validation",
                "正在校验项目绑定与运行时约束",
                percent=10,
            )
            record = await _managed_script_record(script_id)
            config = _record_config(record, f"脚本 {script_id}")
            request = _runtime_install_request(config, payload)
            resolution = await self._resolve_and_bind_runtime(
                script_id,
                request,
            )
            await self._progress_stage(
                "runtime-persist",
                "正在写入共享运行时绑定",
                percent=90,
            )
            await self._persist_resolution(script_id, resolution, request)
            return resolution

    async def _delete_version_transaction(
        self,
        script_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        async with Config.script_config_transaction(
            script_id,
            owner=f"maafw-resource-delete:{script_id}",
        ):
            result = await self._gateway().delete_version(payload)
            await self._persist_project_delete(script_id, result, payload)
            return result

    async def _collect_garbage_with_script_references(
        self,
        payload: Mapping[str, Any],
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        gateway = self._gateway()
        async with gateway.resource_transaction():
            async with Config.script_config_write_scope(None):
                script_records = await _managed_script_record_dtos()
                return await gateway.collect_garbage(
                    dry_run=dry_run,
                    grace_days=_as_int(payload.get("graceDays"), 30),
                    keep_latest=_as_int(payload.get("keepLatest"), 2),
                    project_id=(
                        str(payload.get("projectId") or "").strip() or None
                    ),
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
        progress_operation: str | None = None,
        progress_message: str = "",
    ) -> dict[str, Any]:
        async def validated_operation() -> Any:
            script_id = await self._require_managed_script(payload)
            data = await operation(script_id)
            if after_success is not None:
                await after_success(script_id, data)
            return data

        if progress_operation:
            return await self._respond_with_progress(
                payload,
                progress_operation,
                progress_message or "正在执行 MaaFW 托管资源操作",
                validated_operation,
            )
        return await self._respond(validated_operation)

    @staticmethod
    async def _require_managed_script(payload: Mapping[str, Any]) -> str:
        script_id = _required_script_id(payload)
        try:
            records = await Config.get_script_records(script_id)
        except Exception as exc:
            raise ManagedServiceError(f"无法读取脚本 {script_id}：{exc}") from exc
        if (
            len(records) != 1
            or _record_field(records[0], "type") != "MaaFWManaged"
        ):
            raise ManagedServiceError(
                f"scriptId {script_id} 不是 MaaFWManaged 脚本，拒绝跨类型写入"
            )
        return script_id

    async def _persist_project(
        self,
        script_id: str,
        project: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        status: str,
    ) -> None:
        update = _project_form_update(project, payload, status=status)
        update["Managed"].update(_cleared_pending_upgrade())
        await Config.update_script(
            script_id,
            update,
        )
        await self._refresh_project_versions_and_references(
            script_id,
            str(project.get("projectId") or payload.get("projectId") or ""),
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

    async def _persist_project_delete(
        self,
        script_id: str,
        result: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        project_id = str(payload.get("projectId") or "").strip()
        await Config.update_script(
            script_id,
            {
                "Managed": {
                    "DeleteConfirmation": "",
                    "Status": f"已删除资源版本 {result.get('version') or ''}",
                }
            },
        )
        await self._refresh_project_versions_and_references(script_id, project_id)

    async def _persist_upgrade_result(
        self,
        script_id: str,
        result: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        del payload
        mutable_result = result if isinstance(result, dict) else dict(result)
        internal = _mapping(mutable_result.pop("_upgradePlanInternal", None))
        if internal.get("kind") != _PENDING_KIND:
            raise ManagedServiceError("升级规划没有生成可持久化 journal")
        project = _mapping(internal.get("project"))
        pending_version = str(project.get("toVersion") or "").strip()
        project_id = str(project.get("projectId") or "").strip()
        pending_reference = str(project.get("pendingReference") or "").strip()
        public_plan = _public_upgrade_plan(internal)
        durable = _durable_pending_upgrade(internal)
        written_users: list[str] = []
        try:
            for raw_user in internal.get("users") or []:
                user = _mapping(raw_user)
                user_id = str(user.get("recordId") or "").strip()
                if not user_id:
                    raise ManagedServiceError("用户升级计划缺少 recordId")
                journal = _user_upgrade_journal(internal, user)
                await Config.update_user(
                    script_id,
                    user_id,
                    {
                        "ManagedUpgrade": {
                            "PendingPlan": json.dumps(
                                journal,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        }
                    },
                )
                written_users.append(user_id)
        except Exception:
            await self._discard_partial_upgrade_persistence(
                script_id,
                written_users,
                project_id=project_id,
                version=pending_version,
                reference=pending_reference,
            )
            raise

        plan_error = str(mutable_result.get("upgradePlanError") or "").strip()
        state = str(internal.get("state") or "plan_error")
        pending_version = str(
            project.get("toVersion")
            or mutable_result.get("latestVersion")
            or ""
        ).strip()
        if plan_error:
            status = (
                f"资源版本 {pending_version} 已导入但未激活；"
                f"配置升级计划生成失败：{plan_error}"
            )
        elif state == "blocked":
            status = (
                f"资源版本 {pending_version} 已导入但未激活；"
                "升级计划仍有人工动作，旧资源继续生效"
            )
        elif state == "ready":
            status = (
                f"资源版本 {pending_version} 已导入但未激活；"
                "脚本及全部用户配置计划已生成，等待明确应用"
            )
        else:
            status = (
                f"资源版本 {pending_version} 已导入但未激活；"
                "脚本或用户配置规划失败，旧资源继续生效"
            )
        update = {
            "Managed": {
                "PendingVersion": pending_version,
                "PendingPlanId": str(internal.get("planId") or ""),
                "UpgradeToken": str(internal.get("confirmationToken") or ""),
                "PendingUpgrade": durable,
                "UpgradeConfirmation": "",
                "UpgradePlan": public_plan,
                "UpgradeReady": state == "ready",
                "UpgradePlanStatus": (
                    plan_error
                    or {
                        "ready": "脚本及全部用户计划已固化，等待确认",
                        "blocked": "存在人工动作，旧资源继续生效",
                        "plan_error": "规划失败，旧资源继续生效",
                    }.get(state, state)
                ),
                "Status": status,
            },
        }
        try:
            await Config.update_script(
                script_id,
                update,
            )
        except Exception:
            await self._discard_partial_upgrade_persistence(
                script_id,
                written_users,
                project_id=project_id,
                version=pending_version,
                reference=pending_reference,
            )
            raise
        await self._refresh_project_versions_and_references(
            script_id,
            project_id,
        )

    async def _discard_partial_upgrade_persistence(
        self,
        script_id: str,
        user_ids: list[str],
        *,
        project_id: str,
        version: str,
        reference: str,
    ) -> None:
        for user_id in user_ids:
            try:
                await Config.update_user(
                    script_id,
                    user_id,
                    {"ManagedUpgrade": {"PendingPlan": "{}"}},
                )
            except Exception:
                pass
        try:
            await Config.update_script(
                script_id,
                {
                    "Managed": {
                        **_cleared_pending_upgrade(),
                        "Status": "升级计划持久化失败，旧资源继续生效",
                    }
                },
            )
        except Exception:
            pass
        if project_id and version and reference:
            try:
                await self._gateway().release_project_reference(
                    project_id,
                    version,
                    reference,
                )
            except Exception:
                pass

    async def _refresh_project_versions_and_references(
        self,
        script_id: str,
        project_id: str,
    ) -> None:
        normalized_project_id = project_id.strip()
        if not normalized_project_id:
            return
        gateway = self._gateway()
        async with gateway.resource_transaction():
            await gateway.reconcile_project_references(
                await _managed_script_record_dtos()
            )
            versions = await gateway.list_versions(normalized_project_id)
        await Config.update_script(
            script_id,
            {"Managed": {"AvailableVersions": versions}},
        )


def _payload(request: PluginHttpRequest) -> dict[str, Any]:
    if isinstance(request.json, Mapping):
        return dict(request.json)
    if isinstance(request.query, Mapping):
        return dict(request.query)
    return {}


def _managed_capabilities() -> dict[str, Any]:
    snapshot_available = callable(
        getattr(Config, "get_plugin_script_type_conversion_snapshot", None)
    )
    conversion_available = callable(
        getattr(Config, "convert_plugin_script_type", None)
    )
    in_place_conversion = snapshot_available and conversion_available
    return {
        "apiVersion": _CONVERSION_API_VERSION,
        "distributionVersion": _distribution_version(),
        "features": {
            "singleEntry": True,
            "inPlaceConversion": in_place_conversion,
            "conversionRecovery": in_place_conversion,
            "projectOverview": True,
            "localImport": True,
            "remoteImport": True,
            "upgradePlans": True,
            "runtimeManagement": True,
            "pinning": True,
            "garbageCollection": True,
            "operationProgress": True,
        },
        "hostApis": {
            "conversionSnapshot": snapshot_available,
            "atomicTypeConversion": conversion_available,
        },
    }


def _distribution_version() -> str:
    try:
        return importlib_metadata.version(_DISTRIBUTION_NAME)
    except importlib_metadata.PackageNotFoundError:
        return _DISTRIBUTION_VERSION_FALLBACK


def _required_script_id(payload: Mapping[str, Any]) -> str:
    script_id = str(
        payload.get("scriptId") or payload.get("script_id") or ""
    ).strip()
    if not script_id:
        raise ManagedServiceError("动作请求缺少 scriptId")
    try:
        return str(uuid.UUID(script_id))
    except ValueError as exc:
        raise ManagedServiceError("scriptId 不是有效 UUID") from exc


def _required_progress_id(
    payload: Mapping[str, Any],
    *,
    required: bool = True,
) -> str:
    operation_value = str(payload.get("operationId") or "").strip()
    progress_value = str(payload.get("progressId") or "").strip()
    if operation_value and progress_value and operation_value != progress_value:
        raise ManagedServiceError("operationId 与 progressId 不一致")
    operation_id = operation_value or progress_value
    if not operation_id:
        if required:
            raise ManagedServiceError("动作请求缺少 operationId/progressId")
        return ""
    if _PROGRESS_ID_PATTERN.fullmatch(operation_id) is None:
        raise ManagedServiceError(
            "progressId 只能包含字母、数字、点、下划线、冒号和连字符，"
            "且长度不能超过 200"
        )
    return operation_id


def _progress_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _progress_int(value: Any) -> int | None:
    number = _progress_number(value)
    if number is None:
        return None
    return max(0, int(number))


def _conversion_operation_id(
    script_id: str,
    source_fingerprint: str,
    project_id: str,
    version: str,
    runtime_constraint: str,
) -> str:
    target_identity = json.dumps(
        {
            "projectId": str(project_id).strip(),
            "version": str(version).strip(),
            "runtimeConstraint": str(runtime_constraint).strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                f"{_CONVERSION_KIND}:{script_id}:{source_fingerprint}:"
                f"{target_identity}"
            ),
        )
    )


def _validate_conversion_snapshot(
    value: Any,
    script_id: str,
) -> dict[str, Any]:
    snapshot = _json_clone(value)
    if not isinstance(snapshot, dict):
        raise ManagedServiceError("宿主转换快照不是 JSON object")
    script = snapshot.get("script")
    users = snapshot.get("users")
    user_order = snapshot.get("userOrder")
    if not isinstance(script, Mapping):
        raise ManagedServiceError("宿主转换快照缺少 script")
    if str(script.get("id") or "").strip() != script_id:
        raise ManagedServiceError("宿主转换快照 scriptId 不一致")
    if not isinstance(script.get("config"), Mapping):
        raise ManagedServiceError("宿主转换快照 script.config 不是 JSON object")
    if not isinstance(users, Mapping) or not isinstance(user_order, list):
        raise ManagedServiceError("宿主转换快照缺少 users/userOrder")
    normalized_order = [str(item).strip() for item in user_order]
    if any(not item for item in normalized_order):
        raise ManagedServiceError("宿主转换快照包含空 userId")
    if len(normalized_order) != len(set(normalized_order)):
        raise ManagedServiceError("宿主转换快照包含重复 userId")
    normalized_users = {str(key).strip(): item for key, item in users.items()}
    if normalized_order != list(normalized_users):
        raise ManagedServiceError(
            "宿主转换快照 users 必须按 userOrder 顺序提供"
        )
    for user_id in normalized_order:
        user = normalized_users[user_id]
        if not isinstance(user, Mapping):
            raise ManagedServiceError(
                f"宿主转换快照用户 {user_id} 不是 JSON object"
            )
        if str(user.get("id") or "").strip() != user_id:
            raise ManagedServiceError(
                f"宿主转换快照用户 {user_id} 身份不一致"
            )
        if str(user.get("type") or "").strip() != str(
            script.get("type") or ""
        ).strip():
            raise ManagedServiceError(
                f"宿主转换快照用户 {user_id} 与脚本类型不一致"
            )
        if not isinstance(user.get("config"), Mapping):
            raise ManagedServiceError(
                f"宿主转换快照用户 {user_id} config 不是 JSON object"
            )
    snapshot["userOrder"] = normalized_order
    snapshot["users"] = normalized_users
    return snapshot


def _snapshot_script_type(snapshot: Mapping[str, Any]) -> str:
    return str(_mapping(snapshot.get("script")).get("type") or "").strip()


def _conversion_form_config(
    config: Mapping[str, Any],
    *,
    virtual_name: str,
) -> dict[str, Any]:
    result = _json_clone(config)
    if not isinstance(result, dict):
        raise ManagedServiceError("转换配置不是 JSON object")
    result.pop(virtual_name, None)
    return result


def _conversion_user_configs(
    snapshot: Mapping[str, Any],
    records: list[Any],
) -> dict[str, dict[str, Any]]:
    user_order = [str(item) for item in snapshot.get("userOrder") or []]
    record_ids = [
        str(_record_field(record, "id") or "").strip()
        for record in records
    ]
    if record_ids != user_order:
        raise ManagedServiceError(
            "脚本用户 UUID/order 与宿主原始快照不一致，拒绝转换"
        )
    result: dict[str, dict[str, Any]] = {}
    for record, user_id in zip(records, user_order, strict=True):
        if str(_record_field(record, "type") or "").strip() != _SOURCE_TYPE:
            raise ManagedServiceError(
                f"用户 {user_id} 不是 MaaFW 配置，拒绝混合类型转换"
            )
        result[user_id] = _conversion_form_config(
            _record_config(record, f"用户 {user_id}"),
            virtual_name="user_name",
        )
    return result


def _conversion_project_id(
    payload: Mapping[str, Any],
    source_info: Mapping[str, Any],
    interface: Mapping[str, Any],
    source_path: str,
) -> str:
    project_id = str(
        payload.get("projectId")
        or interface.get("name")
        or source_info.get("ProjectLabel")
        or Path(source_path).name
        or ""
    ).strip()
    if not project_id:
        raise ManagedServiceError(
            "无法从请求、ProjectInterface 或项目路径确定 projectId"
        )
    return project_id


def _merge_conversion_target(
    source: Mapping[str, Any],
    update: Mapping[str, Any],
) -> dict[str, Any]:
    result = _conversion_form_config(source, virtual_name="script_name")

    def merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
        for key, value in patch.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = _json_clone(value)

    merge(result, update)
    return result


def _assert_conversion_script_preserved(
    source: Mapping[str, Any],
    converted: Mapping[str, Any],
) -> None:
    source_base = _upgrade_source_config(source)
    converted_base = _upgrade_source_config(converted)
    for value in (source_base, converted_base):
        info = value.get("Info")
        if isinstance(info, dict):
            # A managed project authoritatively binds these two fields to its
            # immutable Project Store identity.  Every other ordinary MaaFW
            # setting must survive byte-for-byte at the JSON value level.
            info.pop("Path", None)
            info.pop("ProjectLabel", None)
    if converted_base != source_base:
        raise ManagedServiceError(
            "转换后普通 MaaFW 脚本配置发生了非资源身份变更"
        )


async def _committed_conversion_response(
    record: Any,
    script_id: str,
) -> dict[str, Any]:
    config = _record_config(record, f"脚本 {script_id}")
    managed = _mapping(config.get("Managed"))
    journal = _mapping(managed.get("ConversionJournal"))
    if (
        journal.get("schemaVersion") != 1
        or journal.get("kind") != _CONVERSION_KIND
        or journal.get("state") != "committed"
        or str(journal.get("scriptId") or "") != script_id
    ):
        raise ManagedServiceError(
            f"scriptId {script_id} 已是 MaaFWManaged，且没有可验证的转换 marker"
        )
    users = await Config.get_user_records(script_id)
    return {
        "converted": False,
        "idempotent": True,
        "recovered": False,
        "scriptId": script_id,
        "fromType": _SOURCE_TYPE,
        "toType": _TARGET_TYPE,
        "project": {
            "projectId": str(managed.get("ProjectId") or ""),
            "version": str(managed.get("Version") or ""),
            "dataPath": str(_mapping(config.get("Info")).get("Path") or ""),
            "manifest": _mapping(managed.get("ProjectManifest")),
        },
        "userIds": [str(_record_field(user, "id") or "") for user in users],
        "host": {"idempotent": True, "journal": journal},
    }


async def _conversion_commit_state(
    script_id: str,
    operation_id: str,
    *,
    expected_snapshot: Mapping[str, Any],
    snapshot_reader: Callable[..., Any],
) -> str:
    try:
        records = await Config.get_script_records(script_id)
    except Exception:
        return "unknown"
    if len(records) != 1:
        return "unknown"
    record = records[0]
    record_type = str(_record_field(record, "type") or "").strip()
    if record_type == _TARGET_TYPE:
        try:
            config = _record_config(record, f"脚本 {script_id}")
            journal = _mapping(
                _mapping(config.get("Managed")).get("ConversionJournal")
            )
        except ManagedServiceError:
            return "unknown"
        if (
            journal.get("schemaVersion") == 1
            and journal.get("kind") == _CONVERSION_KIND
            and journal.get("operationId") == operation_id
            and journal.get("state") == "committed"
        ):
            return "committed"
        return "unknown"
    if record_type != _SOURCE_TYPE:
        return "unknown"
    try:
        current = snapshot_reader(script_id)
        if inspect.isawaitable(current):
            current = await current
        current_snapshot = _validate_conversion_snapshot(current, script_id)
    except Exception:
        return "unknown"
    return "source" if current_snapshot == expected_snapshot else "unknown"


def _remote_source_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    source = str(payload.get("source") or "").strip().casefold()
    if source in {"github", "github_release"}:
        return {
            "source": "github_release",
            "repo": str(payload.get("githubRepo") or "").strip(),
            "tag": str(payload.get("githubTag") or "").strip(),
            "asset_pattern": str(
                payload.get("githubAssetPattern") or r"\.zip$"
            ).strip(),
            "channel": str(payload.get("channel") or "stable").strip(),
        }
    if source in {"mirrorchyan", "mirror_chyan", "mirror酱"}:
        return {
            "source": "mirrorchyan",
            "cdk": str(payload.get("mirrorChyanCDK") or "").strip(),
            "channel": str(payload.get("channel") or "stable").strip(),
        }
    raise ManagedServiceError("远程来源必须是 MirrorChyan 或 GitHub")


def _remote_probe_interface(
    payload: Mapping[str, Any],
    project_id: str,
) -> dict[str, Any]:
    source_config = _remote_source_config(payload)
    interface: dict[str, Any] = {
        "interface_version": 2,
        "name": project_id,
        "version": "0.0.0",
    }
    if source_config["source"] == "mirrorchyan":
        rid = str(payload.get("mirrorChyanRid") or project_id).strip()
        if not rid:
            raise ManagedServiceError("首次 MirrorChyan 导入需要 RID")
        interface["mirrorchyan_rid"] = rid
        interface["mirrorchyan_multiplatform"] = True
    else:
        repo = str(payload.get("githubRepo") or "").strip()
        if not repo:
            raise ManagedServiceError("首次 GitHub 导入需要 owner/repository")
        interface["github"] = repo
    return interface


def _public_remote_discovery(discovery: Mapping[str, Any]) -> dict[str, Any]:
    """Remove ephemeral or credential-bearing download URLs before persistence."""

    result = _json_clone(discovery)
    if not isinstance(result, dict):
        raise ManagedServiceError("远程发现结果必须是 JSON object")
    candidate = _mapping(result.get("candidate"))
    if candidate:
        had_url = bool(
            str(
                candidate.pop("download_url", None)
                or candidate.pop("downloadUrl", None)
                or ""
            ).strip()
        )
        candidate["downloadAvailable"] = had_url
        result["candidate"] = candidate
    return result


def _with_project_reference(
    payload: Mapping[str, Any],
    script_id: str,
    *,
    pending: bool = False,
) -> dict[str, Any]:
    result = dict(payload)
    prefix = "maafw-upgrade" if pending else "maafw-script"
    result["projectReference"] = f"{prefix}:{script_id}"
    return result


async def _managed_script_record(script_id: str) -> Any:
    try:
        records = await Config.get_script_records(script_id)
    except Exception as exc:
        raise ManagedServiceError(f"无法读取脚本 {script_id}：{exc}") from exc
    if len(records) != 1 or _record_field(records[0], "type") != "MaaFWManaged":
        raise ManagedServiceError(
            f"scriptId {script_id} 不是唯一的 MaaFWManaged 脚本"
        )
    return records[0]


def _record_field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _record_config(record: Any, label: str) -> dict[str, Any]:
    config = _record_field(record, "config")
    if not isinstance(config, Mapping):
        raise ManagedServiceError(f"{label}配置不是 JSON object")
    return dict(config)


def _bound_upgrade_target(
    managed: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    bound_project_id, current_version = managed_project_identity(managed)
    requested_project_id = str(payload.get("projectId") or "").strip()
    if (
        not bound_project_id
        or not requested_project_id
        or requested_project_id != bound_project_id
    ):
        raise ManagedServiceError(
            "升级项目 ID 必须与当前脚本绑定的 ProjectId 完全一致"
        )
    if not current_version:
        raise ManagedServiceError("当前脚本尚未绑定资源版本，不能执行升级")
    pending = _mapping(managed.get("PendingUpgrade"))
    if pending or str(managed.get("PendingVersion") or "").strip():
        raise ManagedServiceError("已有待确认升级；请先应用或取消该计划")
    return bound_project_id, current_version


def _runtime_install_request(
    config: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    managed = _mapping(config.get("Managed"))
    project_id, version = managed_project_identity(managed)
    authoritative = {
        "projectId": project_id,
        "version": version,
        "runtimeConstraint": str(
            managed.get("RuntimeConstraint") or ""
        ).strip(),
    }
    if not authoritative["projectId"] or not authoritative["version"]:
        raise ManagedServiceError(
            "当前脚本尚未绑定资源版本，不能安装共享运行时"
        )
    for key in ("projectId", "version", "runtimeConstraint"):
        requested = str(payload.get(key) or "").strip()
        if requested and requested != authoritative[key]:
            raise ManagedServiceError(
                "页面中的资源或运行时配置已过期，请保存并刷新后重试"
            )
    request = dict(payload)
    request.update(authoritative)
    return request


def _upgrade_source_config(config: Mapping[str, Any]) -> dict[str, Any]:
    value = _json_clone(config)
    if not isinstance(value, dict):
        raise ManagedServiceError("升级配置必须是 JSON object")
    for key in ("Managed", "ManagedRuntime", "ManagedActions", "ManagedUpgrade"):
        value.pop(key, None)
    return value


def _atomic_json_field_update(config: Mapping[str, Any]) -> dict[str, Any]:
    """Encode object-valued form fields so the host replaces them atomically.

    Plugin script configuration is shaped as ``group -> field -> value``.
    The host recursively merges mapping payloads before validating them, which
    would retain keys removed by a resource-upgrade plan or rollback snapshot.
    JSON object fields accept their encoded form and are parsed by the normal
    configuration codec after the merge.
    """

    value = _json_clone(config)
    if not isinstance(value, dict):
        raise ManagedServiceError("升级配置必须是 JSON object")
    for group_name, fields in value.items():
        if not isinstance(fields, dict):
            continue
        for name, field_value in tuple(fields.items()):
            if (
                (group_name, name) in _JSON_OBJECT_FIELDS
                and isinstance(field_value, dict)
            ):
                fields[name] = json.dumps(
                    field_value,
                    ensure_ascii=False,
                    sort_keys=True,
                )
    return value


def _json_clone(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
    except (TypeError, ValueError) as exc:
        raise ManagedServiceError("升级 journal 只接受 JSON 值") from exc


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_pack_upgrade_plan(
    raw_plan: Mapping[str, Any],
    *,
    service_key: str,
    project_id: str,
    from_version: str,
    to_version: str,
) -> dict[str, Any]:
    plan = _json_clone(raw_plan)
    if not isinstance(plan, dict):
        raise ManagedServiceError(f"{service_key} 计划不是 JSON object")
    expected = {
        "schemaVersion": 1,
        "kind": "maafw.resource-upgrade-plan",
        "projectId": project_id,
        "fromVersion": from_version,
        "toVersion": to_version,
    }
    for key, value in expected.items():
        if plan.get(key) != value:
            raise ManagedServiceError(
                f"{service_key} 计划字段 {key}={plan.get(key)!r}，"
                f"预期 {value!r}"
            )
    if not isinstance(plan.get("config"), Mapping):
        raise ManagedServiceError(f"{service_key} 计划缺少 JSON config")
    if not isinstance(plan.get("readyToApply"), bool):
        raise ManagedServiceError(f"{service_key} 计划 readyToApply 必须是 boolean")
    if plan.get("lossless") is not True:
        raise ManagedServiceError(f"{service_key} 计划未声明 lossless=true")
    if not isinstance(plan.get("manualActions"), list):
        raise ManagedServiceError(f"{service_key} 计划 manualActions 必须是 array")
    if not isinstance(plan.get("warnings"), list):
        raise ManagedServiceError(f"{service_key} 计划 warnings 必须是 array")
    return plan


def _project_source_hash(project: Mapping[str, Any]) -> str:
    manifest = _mapping(project.get("manifest"))
    source = _mapping(manifest.get("source"))
    source_hash = _mapping(source.get("hash"))
    value = str(source_hash.get("value") or "").strip()
    if not value:
        raise ManagedServiceError("Project Store 资源清单缺少 projected-source hash")
    return value


def _failed_upgrade_envelope(
    script_id: str,
    result: Mapping[str, Any],
    *,
    plan_id: str,
    pending_reference: str,
    error: str,
) -> dict[str, Any]:
    project = _mapping(result.get("project"))
    previous = _mapping(result.get("previousProject"))
    project_id = str(project.get("projectId") or "").strip()
    from_version = str(previous.get("version") or "").strip()
    to_version = str(project.get("version") or "").strip()
    return {
        "schemaVersion": 1,
        "kind": _PENDING_KIND,
        "planId": plan_id,
        "state": "plan_error",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "scriptId": script_id,
        "project": {
            "projectId": project_id,
            "fromVersion": from_version,
            "toVersion": to_version,
            "fromHash": _project_source_hash(previous),
            "toHash": _project_source_hash(project),
            "pendingReference": pending_reference,
        },
        "planner": {},
        "script": {},
        "users": [],
        "userIds": [],
        "planCount": 0,
        "errors": [{"scope": "upgrade", "message": error}],
        "manualActions": [],
        "warnings": [],
        "lossless": False,
        "readyToApply": False,
        "confirmationToken": f"{project_id}@{to_version}#{plan_id}",
    }


def _public_upgrade_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    result = _json_clone(plan)
    if not isinstance(result, dict):
        return {}
    script = _mapping(result.get("script"))
    script.pop("sourceConfig", None)
    script.pop("targetConfig", None)
    script_plan = _mapping(script.get("plan"))
    script_plan.pop("config", None)
    script["plan"] = script_plan
    result["script"] = script
    users: list[dict[str, Any]] = []
    for raw_user in result.get("users") or []:
        if not isinstance(raw_user, Mapping):
            continue
        user = dict(raw_user)
        user.pop("sourceConfig", None)
        user.pop("targetConfig", None)
        user_plan = _mapping(user.get("plan"))
        user_plan.pop("config", None)
        user["plan"] = user_plan
        users.append(user)
    result["users"] = users
    return result


def _durable_pending_upgrade(plan: Mapping[str, Any]) -> dict[str, Any]:
    result = _json_clone(plan)
    if not isinstance(result, dict):
        raise ManagedServiceError("升级 journal 不是 JSON object")
    script = _mapping(result.get("script"))
    script_plan = _mapping(script.get("plan"))
    script_plan.pop("config", None)
    script["plan"] = script_plan
    result["script"] = script
    result["users"] = [
        {
            "scope": user.get("scope"),
            "recordId": user.get("recordId"),
            "name": user.get("name"),
            "sourceHash": user.get("sourceHash"),
            "targetHash": user.get("targetHash"),
            "error": user.get("error"),
            "plan": _public_record_plan(_mapping(user.get("plan"))),
        }
        for user in result.get("users") or []
        if isinstance(user, Mapping)
    ]
    return result


def _user_upgrade_journal(
    pending: Mapping[str, Any],
    user: Mapping[str, Any],
) -> dict[str, Any]:
    source = user.get("sourceConfig")
    target = user.get("targetConfig")
    if not isinstance(source, Mapping):
        source = {}
    if not isinstance(target, Mapping):
        target = {}
    return {
        "schemaVersion": 1,
        "kind": _USER_PENDING_KIND,
        "planId": pending.get("planId"),
        "recordId": user.get("recordId"),
        "sourceHash": user.get("sourceHash"),
        "targetHash": user.get("targetHash"),
        "sourceConfig": dict(source),
        "targetConfig": dict(target),
        "plan": _public_record_plan(_mapping(user.get("plan"))),
        "error": user.get("error"),
    }


def _public_record_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    result = _json_clone(plan)
    if not isinstance(result, dict):
        return {}
    result.pop("config", None)
    return result


def _assert_pending_fresh(
    current_script: Mapping[str, Any],
    pending: Mapping[str, Any],
    user_journals: Mapping[str, Mapping[str, Any]],
    previous_project: Mapping[str, Any],
    project: Mapping[str, Any],
) -> None:
    script = _mapping(pending.get("script"))
    current_source = _upgrade_source_config(current_script)
    if _json_hash(current_source) != script.get("sourceHash"):
        raise ManagedServiceError(
            "脚本配置在规划后发生变化，计划已过期；请取消后重新生成。"
        )
    target = script.get("targetConfig")
    if not isinstance(target, Mapping) or _json_hash(target) != script.get("targetHash"):
        raise ManagedServiceError("持久化脚本 targetConfig 哈希不匹配")
    project_info = _mapping(pending.get("project"))
    current_managed = _mapping(current_script.get("Managed"))
    current_info = _mapping(current_script.get("Info"))
    project_id = str(project_info.get("projectId") or "").strip()
    from_version = str(project_info.get("fromVersion") or "").strip()
    bound_project_id, bound_version = managed_project_identity(current_managed)
    if bound_project_id != project_id:
        raise ManagedServiceError("脚本绑定的 ProjectId 与升级计划来源不一致")
    if bound_version != from_version:
        raise ManagedServiceError("脚本绑定的资源版本已变化，升级计划已过期")
    previous_path = _project_data_path(previous_project)
    current_path = str(current_info.get("Path") or "").strip()
    if not current_path or not _same_local_path(current_path, previous_path):
        raise ManagedServiceError("脚本绑定的资源路径与升级计划来源不一致")
    if _project_source_hash(previous_project) != project_info.get("fromHash"):
        raise ManagedServiceError("来源资源内容哈希与计划不一致")
    if _project_source_hash(project) != project_info.get("toHash"):
        raise ManagedServiceError("目标资源内容哈希与计划不一致")
    for user_id, journal in user_journals.items():
        current = journal.get("_currentConfig")
        if not isinstance(current, Mapping):
            raise ManagedServiceError(f"用户 {user_id} 当前配置缺失")
        if _json_hash(_upgrade_source_config(current)) != journal.get("sourceHash"):
            raise ManagedServiceError(
                f"用户 {user_id} 配置在规划后发生变化，计划已过期"
            )
        target_config = journal.get("targetConfig")
        if (
            not isinstance(target_config, Mapping)
            or _json_hash(target_config) != journal.get("targetHash")
        ):
            raise ManagedServiceError(f"用户 {user_id} targetConfig 哈希不匹配")


def _required_plan_config(plan: Mapping[str, Any], label: str) -> dict[str, Any]:
    config = plan.get("config")
    if not isinstance(config, Mapping):
        raise ManagedServiceError(f"{label}升级计划缺少可应用的 config")
    return dict(config)


def _cleared_pending_upgrade() -> dict[str, Any]:
    return {
        "ImportVersion": "",
        "TargetVersion": "",
        "PendingVersion": "",
        "PendingPlanId": "",
        "UpgradeToken": "",
        # The host recursively merges mapping payloads. An encoded empty object
        # replaces the old JSON field atomically instead of preserving its keys.
        "PendingUpgrade": "{}",
        "UpgradeConfirmation": "",
        "UpgradeReady": False,
        "UpgradePlanStatus": "尚未生成",
        "UpgradePlan": "{}",
    }


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
        "ImportProjectId": "",
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
    if "sourcePath" in payload:
        managed["SourcePath"] = str(payload.get("sourcePath") or "")
    if "sourceArchive" in payload:
        managed["SourceArchive"] = str(payload.get("sourceArchive") or "")
    managed.update(_project_capability_form(project))
    update: dict[str, Any] = {"Managed": managed}
    if project_path:
        update["Info"] = {
            "Path": project_path,
            "ProjectLabel": "@".join(
                item for item in (project_id, version) if item
            ),
        }
    return update


def _project_capability_form(project: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _mapping(project.get("manifest"))
    summary = _mapping(project.get("summary"))
    capabilities = _mapping(
        summary.get("capabilities", manifest.get("capabilities"))
    )
    counts = _mapping(capabilities.get("counts"))
    source = _mapping(manifest.get("source"))
    size = _mapping(summary.get("size", manifest.get("size")))
    shells = summary.get("shells", manifest.get("shells", {}))
    agents = summary.get("agents", manifest.get("agents", []))
    return {
        "ResourceVersion": str(
            project.get("version")
            or source.get("interfaceVersion")
            or source.get("version")
            or ""
        ),
        "InterfaceVersion": str(
            summary.get("interfaceVersion")
            or source.get("interfaceVersion")
            or source.get("version")
            or ""
        ),
        "ResourceCount": _as_int(
            counts.get("resources", capabilities.get("resourceCount")),
            0,
        ),
        "TaskCount": _as_int(
            counts.get("tasks", capabilities.get("taskCount")),
            0,
        ),
        "AgentCount": _as_int(
            summary.get(
                "agentCount",
                counts.get("agents", capabilities.get("agentCount")),
            ),
            len(agents) if isinstance(agents, list) else 0,
        ),
        "Agents": agents if isinstance(agents, list) else [],
        "Shells": shells if isinstance(shells, Mapping) else {},
        "Capabilities": capabilities,
        "SourceSizeBytes": _as_int(
            size.get(
                "sourceTreeBytes",
                source.get("treeSizeBytes", source.get("inputSizeBytes")),
            ),
            0,
        ),
        "ManagedSizeBytes": _as_int(
            size.get("projectedBytes"),
            0,
        ),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _project_data_path(project: Mapping[str, Any]) -> str:
    for key in ("dataPath", "projectPath", "path"):
        value = str(project.get(key) or "").strip()
        if value:
            return value
    raise ManagedServiceError("资源存储返回值缺少项目目录")


def _same_local_path(left: str, right: str) -> bool:
    try:
        return str(Path(left).resolve(strict=False)).casefold() == str(
            Path(right).resolve(strict=False)
        ).casefold()
    except (OSError, RuntimeError):
        return left.strip().casefold() == right.strip().casefold()


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
