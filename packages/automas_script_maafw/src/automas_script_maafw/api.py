from __future__ import annotations

import asyncio
import copy
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from app.core import Config
from app.plugins import PluginHttpRequest, PluginWebSocketSession

from .agent_env_state import (
    invalidate_maafw_agent_env_state,
    load_maafw_agent_env_state,
    save_maafw_agent_env_state,
)
from .api_models import (
    MaaFWAgentEnvInfo,
    MaaFWAgentEnvPrepareData,
    MaaFWAgentEnvPrepareIn,
    MaaFWAgentEnvPrepareOut,
    MaaFWProjectUpdateData,
    MaaFWProjectUpdateIn,
    MaaFWProjectUpdateOut,
    model_json,
)
from .project_path import release_project_path, try_reserve_project_path
from .schema import build_source_config


INTERFACE_SERVICE = "maafw.interface.v1"
PROJECT_UPDATE_SERVICE = "maafw.project_update.v1"
AGENT_ENV_SERVICE = "maafw.agent_env.v1"
RUNNER_SERVICE = "maafw.runner.v1"
RUNTIME_POOL_SERVICE = "maafw.runtime_pool.v1"
API_SERVICE = "maafw.api.v1"

PROJECT_UPDATE_PROGRESS = "maafw.project-update.progress"
ENV_PREPARE_PROGRESS = "maafw.env-prepare.progress"

class MaaFWApiError(RuntimeError):
    """A user-facing failure at the ordinary MaaFW API boundary."""


def _track_http_operation(method: Callable[..., Awaitable[dict[str, Any]]]) -> Any:
    """Keep plugin shutdown behind an in-flight project mutation."""

    @wraps(method)
    async def wrapped(
        self: "MaaFWApiController",
        request: PluginHttpRequest,
    ) -> dict[str, Any]:
        if self._draining:
            return {
                "code": 503,
                "status": "error",
                "message": "MaaFW 插件正在停止，暂不接受新请求",
                "data": None,
            }
        task = asyncio.current_task()
        if task is not None:
            self._operations.add(task)
        try:
            return await method(self, request)
        finally:
            if task is not None:
                self._operations.discard(task)

    return wrapped


async def _run_to_thread_with_cancellation_drain(
    function: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Do not release a project reservation while a worker is still mutating."""

    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation_requested = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancellation_requested = True
        except BaseException:
            break
    try:
        result = worker.result()
    except BaseException:
        if cancellation_requested:
            raise asyncio.CancelledError
        raise
    if cancellation_requested:
        raise asyncio.CancelledError
    return result


async def _await_with_cancellation_drain(awaitable: Awaitable[Any]) -> Any:
    """Drain an async provider operation before propagating caller cancellation."""

    task = asyncio.ensure_future(awaitable)
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_requested = True
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException:
        if cancellation_requested:
            raise asyncio.CancelledError
        raise
    if cancellation_requested:
        raise asyncio.CancelledError
    return result


async def _invoke_provider(
    service: Any,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    method = getattr(service, method_name, None)
    if not callable(method):
        raise MaaFWApiError(f"服务未提供 {method_name}()")
    if inspect.iscoroutinefunction(method):
        return await _await_with_cancellation_drain(method(*args, **kwargs))
    result = await _run_to_thread_with_cancellation_drain(method, *args, **kwargs)
    if inspect.isawaitable(result):
        return await _await_with_cancellation_drain(result)
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _record_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            payload = model_dump(mode="json", by_alias=True)
        except TypeError:
            payload = model_dump()
        if isinstance(payload, Mapping):
            return copy.deepcopy(dict(payload))
    if hasattr(value, "__dict__"):
        return {
            key: copy.deepcopy(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    raise MaaFWApiError("宿主脚本记录不是 JSON object 或稳定 DTO")


def _record_type(record: Mapping[str, Any]) -> str:
    outer = str(record.get("type") or record.get("typeKey") or "").strip()
    if outer.casefold() in {
        "plugin",
        "pluginscript",
        "pluginscriptconfig",
        "plugin-script-config",
        "plugin_script_config",
    }:
        config = _mapping(record.get("config"))
        plugin_data = _mapping(config.get("PluginData"))
        raw_plugin_config = plugin_data.get("Config")
        if isinstance(raw_plugin_config, Mapping):
            plugin_config = _mapping(raw_plugin_config)
        elif isinstance(raw_plugin_config, str) and raw_plugin_config.strip():
            try:
                decoded = json.loads(raw_plugin_config)
            except json.JSONDecodeError:
                decoded = {}
            plugin_config = _mapping(decoded)
        else:
            plugin_config = {}
        meta = _mapping(
            config.get("Meta")
            or config.get("meta")
            or plugin_config.get("Meta")
            or plugin_config.get("meta")
        )
        return str(
            record.get("pluginTypeKey")
            or record.get("plugin_type_key")
            or record.get("PluginTypeKey")
            or meta.get("PluginTypeKey")
            or meta.get("pluginTypeKey")
            or outer
        ).strip()
    return outer


def _record_config(record: Mapping[str, Any]) -> dict[str, Any]:
    config = record.get("config")
    if not isinstance(config, Mapping):
        raise MaaFWApiError("宿主脚本记录缺少 JSON config")
    return copy.deepcopy(dict(config))


def _value(value: Any, key: str, default: Any = None) -> Any:
    """Read one field from a stable JSON object or a provider DTO."""

    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _script_id_from_request(request: PluginHttpRequest) -> str:
    payload = _payload(request)
    return str(payload.get("scriptId") or "").strip()


def _payload(request: PluginHttpRequest) -> dict[str, Any]:
    if isinstance(request.json, Mapping):
        return dict(request.json)
    if isinstance(request.query, Mapping):
        return dict(request.query)
    return {}


def _header(request: PluginHttpRequest, name: str) -> str:
    expected = name.casefold()
    for key, value in request.headers.items():
        if str(key).casefold() == expected:
            return str(value or "").strip()
    return ""


async def _script_record(script_id: str) -> dict[str, Any]:
    records = await _invoke_provider(Config, "get_script_records", script_id)
    if not isinstance(records, list) or len(records) != 1:
        raise KeyError(script_id)
    return _record_mapping(records[0])


def _resolve_script_form(record: Mapping[str, Any]) -> dict[str, Any]:
    config = _record_config(record)
    plugin_data = _mapping(config.get("PluginData"))
    raw = plugin_data.get("Config")
    if isinstance(raw, Mapping):
        return copy.deepcopy(dict(raw))
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MaaFWApiError(f"插件 MaaFW 配置无法解码: {exc}") from exc
        if isinstance(decoded, Mapping):
            return copy.deepcopy(dict(decoded))
    return config


def _is_managed_record(record: Mapping[str, Any]) -> bool:
    if _record_type(record).casefold() == "maafwmanaged":
        return True
    config = _record_config(record)
    managed = _mapping(config.get("Managed"))
    if bool(managed.get("ProjectId") or managed.get("ProjectManifest")):
        return True
    # A project-store pack is intentionally opaque to this ordinary-directory
    # transport.  Resolve the generic script metadata when available instead
    # of importing or naming a concrete managed plugin.
    try:
        from app.core.script_types import script_type_registry

        provider = script_type_registry.get(_record_type(record))
        metadata = getattr(provider, "metadata", None)
        resource_model = str(
            metadata.get("resource_model") if isinstance(metadata, Mapping) else ""
        ).strip().casefold()
        if resource_model in {"project-store", "project_store"}:
            return True
    except Exception:
        pass
    return False


def _runtime_pool_route(service: Any) -> tuple[Path, str]:
    storage_info = getattr(service, "storage_info", None)
    if not callable(storage_info):
        raise MaaFWApiError(
            f"插件服务 {RUNTIME_POOL_SERVICE} 未加载或不支持 storage_info()"
        )
    payload = storage_info()
    if not isinstance(payload, Mapping):
        raise MaaFWApiError("MaaFW Runtime Pool storage_info 必须返回对象")
    raw_root = payload.get("root")
    pool_id = payload.get("poolId")
    identity = _mapping(payload.get("rootIdentity"))
    identity_pool_id = identity.get("poolId")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (raw_root, pool_id, identity_pool_id)
    ):
        raise MaaFWApiError("MaaFW Runtime Pool storage_info 缺少 root/poolId/rootIdentity")
    raw_root = raw_root.strip()
    pool_id = pool_id.strip()
    identity_pool_id = identity_pool_id.strip()
    if identity_pool_id != pool_id:
        raise MaaFWApiError(
            "MaaFW Runtime Pool storage_info 的 poolId 与 rootIdentity 不一致"
        )
    root = Path(raw_root)
    if not root.is_absolute():
        raise MaaFWApiError("MaaFW Runtime Pool root 必须是绝对路径")
    return root.resolve(), pool_id


def _runtime_pool_entry(service: Any, runtime_id: str) -> dict[str, str] | None:
    resolve_runtime = getattr(service, "resolve_runtime", None)
    if not callable(resolve_runtime):
        raise MaaFWApiError(
            f"插件服务 {RUNTIME_POOL_SERVICE} 未加载或不支持 resolve_runtime()"
        )
    payload = resolve_runtime({"runtimeId": runtime_id, "touch": False})
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise MaaFWApiError("MaaFW Runtime Pool resolve_runtime 必须返回对象")
    identity: dict[str, str] = {}
    for key in ("runtimeId", "poolId", "pythonExecutable", "venvPath"):
        raw_value = payload.get(key)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise MaaFWApiError(f"MaaFW Runtime Pool 条目缺少字符串字段 {key}")
        normalized = raw_value.strip()
        if key in {"pythonExecutable", "venvPath"} and not Path(normalized).is_absolute():
            raise MaaFWApiError(
                f"MaaFW Runtime Pool 条目的 {key} 必须是绝对路径"
            )
        identity[key] = normalized
    return identity


def _build_agent_info_items(agent_plans: Any) -> list[MaaFWAgentEnvInfo]:
    if not isinstance(agent_plans, list):
        return []

    def value(agent: Any, key: str) -> Any:
        if isinstance(agent, Mapping):
            return agent.get(key)
        return getattr(agent, key, None)

    return [
        MaaFWAgentEnvInfo(
            childExec=str(value(agent, "childExec") or ""),
            executable=str(value(agent, "executable") or ""),
            runtimeKind=value(agent, "runtimeKind"),
            isolatedVenvPath=value(agent, "isolatedVenvPath"),
            fallbackReason=value(agent, "fallbackReason"),
        )
        for agent in agent_plans
    ]


def _runtime_cache_fields(result: Any) -> dict[str, str]:
    runtime = _mapping(_value(result, "runtime"))
    values: dict[str, str] = {}
    for key in ("runtimeId", "poolId", "pythonExecutable", "venvPath"):
        value = str(runtime.get(key) or "").strip()
        if value:
            values[key] = value
    return values


def _cached_runtime_is_current(data: Mapping[str, Any], service: Any) -> bool:
    runtime_id = str(data.get("runtimeId") or "").strip()
    cached_pool_id = str(data.get("poolId") or "").strip()
    if not runtime_id or not cached_pool_id:
        return False
    try:
        _, pool_id = _runtime_pool_route(service)
        entry = _runtime_pool_entry(service, runtime_id)
        if entry is None:
            return False
        return (
            cached_pool_id == pool_id
            and entry["runtimeId"] == runtime_id
            and entry["poolId"] == cached_pool_id
            and Path(entry["pythonExecutable"]).resolve()
            == Path(str(data.get("pythonExecutable"))).resolve()
            and Path(entry["venvPath"]).resolve()
            == Path(str(data.get("venvPath"))).resolve()
        )
    except Exception:
        return False


class MaaFWApiController:
    """HTTP/WS controller for ordinary MaaFW project operations."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._sessions: set[PluginWebSocketSession] = set()
        self._progress_tasks: set[asyncio.Task[Any]] = set()
        self._operations: set[asyncio.Task[Any]] = set()
        self._draining = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def register_routes(self) -> None:
        self.ctx.server.http(
            "/maafw/project/update",
            self.project_update,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw/agent-env/prepare",
            self.prepare_agent_env,
            methods=("POST",),
        )
        self.ctx.server.websocket(
            "/maafw/progress",
            self._on_progress_message,
            on_connect=self._on_progress_connect,
            on_disconnect=self._on_progress_disconnect,
        )

    async def close(self) -> None:
        self._draining = True
        current = asyncio.current_task()
        operations = tuple(task for task in self._operations if task is not current)
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        pending = tuple(self._progress_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._progress_tasks.clear()
        self._sessions.clear()

    async def _on_progress_connect(self, session: PluginWebSocketSession) -> None:
        self._sessions.add(session)

    async def _on_progress_disconnect(self, session: PluginWebSocketSession) -> None:
        self._sessions.discard(session)

    async def _on_progress_message(
        self,
        _message: Any,
        session: PluginWebSocketSession,
    ) -> None:
        # The channel is server-push. Keep a small ping/pong affordance for
        # callers that probe the connection before an operation starts.
        if isinstance(_message, Mapping) and str(_message.get("type") or "").casefold() == "ping":
            await session.send_json({"type": "pong"})

    def _set_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        self._loop = loop
        return loop

    def _progress_callback(
        self,
        event_type: str,
        script_id: str,
        *,
        project_path: str | None = None,
    ) -> Callable[[Mapping[str, Any]], None]:
        # Provider callbacks may run in a worker thread (Runner's prepare
        # method is synchronous), so never call get_running_loop() there.
        loop = self._loop or asyncio.get_running_loop()

        def publish(progress: Mapping[str, Any]) -> None:
            data = {"scriptId": script_id}
            if project_path is not None:
                data["project_path"] = project_path
            data.update(dict(progress))

            def schedule() -> None:
                task = asyncio.create_task(self._broadcast(event_type, script_id, data))
                self._progress_tasks.add(task)
                task.add_done_callback(self._progress_tasks.discard)

            loop.call_soon_threadsafe(schedule)

        return publish

    async def _broadcast(
        self,
        event_type: str,
        script_id: str,
        data: Mapping[str, Any],
    ) -> None:
        message = {"id": script_id, "type": event_type, "data": dict(data)}
        stale: list[PluginWebSocketSession] = []
        for session in tuple(self._sessions):
            try:
                await session.send_json(message)
            except Exception:
                stale.append(session)
        for session in stale:
            self._sessions.discard(session)

    @_track_http_operation
    async def project_update(self, request: PluginHttpRequest) -> dict[str, Any]:
        self._set_loop()
        try:
            payload = MaaFWProjectUpdateIn.model_validate(_payload(request))
        except Exception as exc:
            return model_json(
                MaaFWProjectUpdateOut(
                    code=400,
                    status="error",
                    message=f"请求参数无效: {exc}",
                    data=MaaFWProjectUpdateData(),
                )
            )

        logs: list[str] = []
        current_version = ""
        terminal_published = False
        deferred_provider_terminal: dict[str, Any] | None = None
        reservation_key: str | None = None

        def append_log(message: str) -> None:
            timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
            for line in str(message).splitlines() or [""]:
                logs.append(f"[{timestamp}] {line}")

        def publish(progress: Mapping[str, Any]) -> None:
            nonlocal deferred_provider_terminal, terminal_published
            stage = str(progress.get("stage") or "")
            # The project-update provider reports its own successful terminal
            # event before the optional Runner prewarm starts. Defer that
            # event until we know whether prewarm follows, then publish it as
            # a non-terminal resource-applied milestone when needed. Failure
            # events remain terminal immediately so cancellation/error paths
            # still have exactly one terminal notification.
            if (
                payload.apply
                and stage == "completed"
                and bool(progress.get("final"))
            ):
                deferred_provider_terminal = dict(progress)
                return
            if stage == "failed" or bool(progress.get("final")):
                terminal_published = True
            self._progress_callback(
                PROJECT_UPDATE_PROGRESS,
                payload.scriptId,
            )(progress)

        try:
            uuid.UUID(payload.scriptId)
            record = await _script_record(payload.scriptId)
            if _record_type(record).casefold() != "maafw":
                return model_json(
                    MaaFWProjectUpdateOut(
                        code=400,
                        status="error",
                        message="指定脚本不是 MaaFW 项目",
                        data=MaaFWProjectUpdateData(logs=logs),
                    )
                )
            if _is_managed_record(record):
                return model_json(
                    MaaFWProjectUpdateOut(
                        code=409,
                        status="error",
                        message="该项目使用插件管理的版本化资源，不能通过普通目录更新接口修改",
                        data=MaaFWProjectUpdateData(logs=logs),
                    )
                )
            script_form = _resolve_script_form(record)
            info_group = _mapping(script_form.get("Info"))
            update_group = _mapping(script_form.get("Update"))
            project_path_raw = str(info_group.get("Path") or "").strip()
            if not project_path_raw:
                append_log("请先配置 MaaFW 项目目录")
                return model_json(
                    MaaFWProjectUpdateOut(
                        code=400,
                        status="error",
                        message="请先配置 MaaFW 项目目录",
                        data=MaaFWProjectUpdateData(logs=logs),
                    )
                )

            project_path = Path(project_path_raw).resolve()
            reservation_key = await try_reserve_project_path(project_path)
            if reservation_key is None:
                message = "MaaFW 项目正在运行或更新，请稍后重试"
                append_log(message)
                return model_json(
                    MaaFWProjectUpdateOut(
                        code=409,
                        status="error",
                        message=message,
                        data=MaaFWProjectUpdateData(logs=logs),
                    )
                )

            interface_service = self.ctx.get(INTERFACE_SERVICE)
            update_service = self.ctx.get(PROJECT_UPDATE_SERVICE)
            runner_service = self.ctx.get(RUNNER_SERVICE)
            runtime_pool = self.ctx.get(RUNTIME_POOL_SERVICE)
            if interface_service is None or update_service is None:
                raise MaaFWApiError("MaaFW interface/project update 服务尚未加载")
            interface_model = await _invoke_provider(interface_service, "load", project_path)
            # Interface providers may return either their native Pydantic
            # model or the JSON-compatible mapping promised by the service
            # contract.  Read through the same boundary helper in both cases.
            current_version = str(_value(interface_model, "version", "") or "")

            local_cdk = str(update_group.get("MirrorChyanCDK") or "").strip()
            global_cdk = str(_global_config("MirrorChyanCDK") or "").strip()
            mirror_cdk = local_cdk or global_cdk
            channel = str(
                update_group.get("Channel") or _global_config("Channel") or "stable"
            ).strip()
            source_config = build_source_config(script_form) or {}
            configured_source = str(source_config.get("source") or "").strip().casefold()
            if not configured_source:
                global_source = str(_global_config("Source") or "").strip().casefold()
                if global_source in {"github", "github_release", "github release"}:
                    source_config["source"] = "github_release"
                elif global_source in {"mirrorchyan", "mirror_chyan", "mirror酱"}:
                    source_config["source"] = "mirrorchyan"
            if mirror_cdk and not str(source_config.get("cdk") or "").strip():
                source_config["cdk"] = mirror_cdk
            if channel and not str(source_config.get("channel") or "").strip():
                source_config["channel"] = channel

            proxy = getattr(Config, "proxy", None)
            if not payload.apply:
                append_log("开始检查 MaaFW 项目更新（尚不安装）")
                publish(
                    {
                        "stage": "checking",
                        "status": "running",
                        "message": "正在检查 MaaFW 项目更新",
                        "percent": 5.0,
                        "phase": "checking",
                        "final": False,
                    }
                )
                discovery = await _invoke_provider(
                    update_service,
                    "discover_update",
                    interface_model,
                    current_version=current_version,
                    project_path=project_path,
                    source_config=source_config,
                    proxy=proxy,
                    send_log=append_log,
                )
                if discovery is None:
                    message = f"MaaFW 项目已是最新版本: {current_version}"
                    append_log(message)
                    publish(
                        {
                            "stage": "completed",
                            "status": "no_update",
                            "message": message,
                            "percent": 100.0,
                            "phase": "checking",
                            "final": True,
                        }
                    )
                    return model_json(
                        MaaFWProjectUpdateOut(
                            message=message,
                            data=MaaFWProjectUpdateData(
                                checked=True,
                                currentVersion=current_version,
                                logs=logs,
                            ),
                        )
                    )
                installable = bool(_value(discovery, "installable", False))
                version = str(_value(discovery, "version", "") or "")
                message = f"发现 MaaFW 项目更新 {current_version} -> {version}"
                if installable:
                    message += "，请再次点击“开始更新”"
                else:
                    message += "，当前来源未提供可安装包"
                append_log(message)
                publish(
                    {
                        "stage": "completed",
                        "status": "version_discovered",
                        "message": message,
                        "version": version,
                        "metadata_source": _value(discovery, "source"),
                        "percent": 100.0,
                        "phase": "checking",
                        "final": True,
                    }
                )
                return model_json(
                    MaaFWProjectUpdateOut(
                        message=message,
                        data=MaaFWProjectUpdateData(
                            checked=True,
                            updateAvailable=True,
                            installable=installable,
                            currentVersion=current_version,
                            latestVersion=version or None,
                            source=_value(discovery, "source"),
                            logs=logs,
                        ),
                    )
                )

            update_result = await _invoke_provider(
                update_service,
                "update_if_needed",
                project_path,
                interface_model,
                mirror_cdk=mirror_cdk,
                channel=channel,
                proxy=proxy,
                send_log=append_log,
                source_config=source_config,
                progress=publish,
            )
            environment_warning = ""
            if bool(_value(update_result, "updated", False)):
                await _run_to_thread_with_cancellation_drain(
                    invalidate_maafw_agent_env_state,
                    payload.scriptId,
                )
                append_log("[完成] MaaFW 项目资源已更新")
                if deferred_provider_terminal is not None:
                    applied_progress = dict(deferred_provider_terminal)
                    applied_progress.update(
                        {
                            "phase": "resource_applied",
                            "final": False,
                        }
                    )
                    deferred_provider_terminal = None
                    self._progress_callback(
                        PROJECT_UPDATE_PROGRESS,
                        payload.scriptId,
                    )(applied_progress)
                refreshed_interface = await _invoke_provider(
                    interface_service,
                    "load",
                    project_path,
                    force_reload=True,
                )
                publish(
                    {
                        "stage": "preparing_environment",
                        "status": "running",
                        "message": "正在准备 MaaFW 实际运行环境",
                        "percent": 90.0,
                        "phase": "resource_applied",
                        "final": False,
                    }
                )
                if runner_service is None or runtime_pool is None:
                    environment_warning = "项目资源已更新，但运行环境预热服务尚未加载"
                    append_log(f"[警告] {environment_warning}")
                else:
                    try:
                        runtime_root, runtime_pool_id = (
                            await _run_to_thread_with_cancellation_drain(
                                _runtime_pool_route,
                                runtime_pool,
                            )
                        )
                        prepared_result = await _invoke_provider(
                            runner_service,
                            "prepare_project_environment",
                            project_path,
                            refreshed_interface,
                            runtime_pool_root=runtime_root,
                            runtime_pool_id=runtime_pool_id,
                            send_log=append_log,
                            progress=publish,
                        )
                        prepared_agents = _mapping(_value(prepared_result, "agents"))
                        prepared_plans = prepared_agents.get("plans", [])
                        prepared_data = MaaFWAgentEnvPrepareData(
                            path=str(project_path),
                            agentCount=(
                                len(prepared_plans)
                                if isinstance(prepared_plans, list)
                                else 0
                            ),
                            agents=_build_agent_info_items(prepared_plans),
                            logs=list(logs),
                            **_runtime_cache_fields(prepared_result),
                        )
                        expected_fingerprint = str(
                            _value(prepared_result, "projectFingerprint", "") or ""
                        ).strip()
                        try:
                            saved = await _run_to_thread_with_cancellation_drain(
                                save_maafw_agent_env_state,
                                payload.scriptId,
                                project_path,
                                model_json(prepared_data),
                                expected_fingerprint=expected_fingerprint,
                            )
                        except Exception as exc:
                            saved = False
                            environment_warning = f"项目资源已更新，但运行环境缓存写入失败: {exc}"
                            append_log(f"[警告] {environment_warning}")
                        if not saved:
                            if not environment_warning:
                                environment_warning = (
                                    "MaaFW 运行环境身份或项目输入已变化，已丢弃旧预热缓存"
                                )
                                append_log(f"[警告] {environment_warning}")
                    except Exception as exc:
                        environment_warning = f"项目资源已更新，但运行环境预热未完成: {type(exc).__name__}: {exc}"
                        append_log(f"[警告] {environment_warning}")

            status = "warning" if environment_warning else "success"
            message = environment_warning or str(_value(update_result, "message", "") or "")
            if deferred_provider_terminal is not None:
                # No Runner prewarm was needed (for example, no update). The
                # provider's terminal event is the complete operation result.
                terminal_progress = deferred_provider_terminal
                deferred_provider_terminal = None
                terminal_published = True
                self._progress_callback(
                    PROJECT_UPDATE_PROGRESS,
                    payload.scriptId,
                )(terminal_progress)
            elif not terminal_published:
                self._progress_callback(
                    PROJECT_UPDATE_PROGRESS,
                    payload.scriptId,
                )(
                    {
                        "stage": "completed",
                        "status": "updated_with_environment_warning" if environment_warning else (
                            "updated" if bool(_value(update_result, "updated", False)) else "no_update"
                        ),
                        "message": message or "MaaFW project update completed",
                        "percent": 100.0,
                        "phase": "finalizing",
                        "final": True,
                    }
                )
            return model_json(
                MaaFWProjectUpdateOut(
                    status=status,
                    message=message,
                    data=MaaFWProjectUpdateData(
                        checked=bool(_value(update_result, "checked", False)),
                        updated=bool(_value(update_result, "updated", False)),
                        updateAvailable=bool(_value(update_result, "update_available", False)),
                        installable=bool(_value(update_result, "installable", False)),
                        currentVersion=current_version
                        or str(_value(update_result, "current_version", "") or ""),
                        latestVersion=_value(update_result, "latest_version"),
                        source=_value(update_result, "source"),
                        logs=logs,
                    ),
                )
            )
        except KeyError:
            append_log("脚本不存在或已被删除")
            if not terminal_published:
                publish(
                    {
                        "stage": "failed",
                        "status": "not_found",
                        "message": logs[-1],
                        "final": True,
                    }
                )
            return model_json(
                MaaFWProjectUpdateOut(
                    code=404,
                    status="error",
                    message="脚本不存在或已被删除",
                    data=MaaFWProjectUpdateData(currentVersion=current_version, logs=logs),
                )
            )
        except ValueError as exc:
            append_log(f"MaaFW 项目更新失败: {exc}")
            if not terminal_published:
                publish({"stage": "failed", "status": "failed", "message": str(exc), "final": True})
            return model_json(
                MaaFWProjectUpdateOut(
                    code=400,
                    status="error",
                    message=str(exc),
                    data=MaaFWProjectUpdateData(currentVersion=current_version, logs=logs),
                )
            )
        except Exception as exc:
            append_log(f"MaaFW 项目更新失败: {type(exc).__name__}: {exc}")
            if not terminal_published:
                publish(
                    {
                        "stage": "failed",
                        "status": "failed",
                        "message": str(exc),
                        "final": True,
                    }
                )
            provider_error_code = getattr(exc, "provider_error_code", None)
            return model_json(
                MaaFWProjectUpdateOut(
                    code=422 if provider_error_code is not None else 500,
                    status="error",
                    message=f"MaaFW 项目更新失败: {type(exc).__name__}: {exc}",
                    data=MaaFWProjectUpdateData(
                        currentVersion=current_version,
                        providerErrorCode=provider_error_code,
                        logs=logs,
                    ),
                )
            )
        finally:
            await release_project_path(reservation_key)

    @_track_http_operation
    async def prepare_agent_env(self, request: PluginHttpRequest) -> dict[str, Any]:
        self._set_loop()
        raw_payload = _payload(request)
        try:
            payload = MaaFWAgentEnvPrepareIn.model_validate(raw_payload)
        except Exception as exc:
            return model_json(
                MaaFWAgentEnvPrepareOut(
                    code=400,
                    status="error",
                    message=f"请求参数无效: {exc}",
                    data=MaaFWAgentEnvPrepareData(path=str(raw_payload.get("path") or "")),
                )
            )

        logs: list[str] = []
        root_path: Path | None = None
        reservation_key: str | None = None
        body_script_id = str(payload.scriptId or "").strip()
        header_progress_id = _header(request, "X-MaaFW-Progress-Id")
        if len(body_script_id) > 128 or len(header_progress_id) > 128:
            return model_json(
                MaaFWAgentEnvPrepareOut(
                    code=400,
                    status="error",
                    message="MaaFW scriptId/progressId 长度不能超过 128 个字符",
                    data=MaaFWAgentEnvPrepareData(path=str(Path(payload.path))),
                )
            )
        if body_script_id and header_progress_id and body_script_id != header_progress_id:
            return model_json(
                MaaFWAgentEnvPrepareOut(
                    code=400,
                    status="error",
                    message="请求体 scriptId 与 X-MaaFW-Progress-Id 不一致",
                    data=MaaFWAgentEnvPrepareData(path=str(Path(payload.path))),
                )
            )
        script_id = body_script_id or header_progress_id
        terminal_published = False

        def append_log(message: str) -> None:
            timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
            for line in str(message).splitlines() or [""]:
                logs.append(f"[{timestamp}] {line}")

        def publish(progress: Mapping[str, Any]) -> None:
            nonlocal terminal_published
            if str(progress.get("stage") or "") in {"completed", "failed"}:
                terminal_published = True
            if script_id:
                self._progress_callback(
                    ENV_PREPARE_PROGRESS,
                    script_id,
                    project_path=str(root_path or payload.path),
                )(progress)

        try:
            runtime_pool = self.ctx.get(RUNTIME_POOL_SERVICE)
            if script_id and runtime_pool is not None:
                cached_data = await _run_to_thread_with_cancellation_drain(
                    load_maafw_agent_env_state,
                    script_id,
                    payload.path,
                )
                if cached_data is not None and await _run_to_thread_with_cancellation_drain(
                    _cached_runtime_is_current,
                    cached_data,
                    runtime_pool,
                ):
                    root_path = Path(str(cached_data["path"])).resolve()
                    data = MaaFWAgentEnvPrepareData.model_validate(cached_data)
                    publish(
                        {
                            "stage": "completed",
                            "status": "ready",
                            "message": "复用已准备的 MaaFW 运行环境",
                            "percent": 100.0,
                            "final": True,
                        }
                    )
                    return model_json(
                        MaaFWAgentEnvPrepareOut(
                            message="复用已准备的 MaaFW 运行环境",
                            data=data,
                        )
                    )
            if _header(request, "X-MaaFW-Cache-Only") == "1":
                return model_json(
                    MaaFWAgentEnvPrepareOut(
                        code=404,
                        status="not_ready",
                        message="MaaFW 运行环境尚未准备",
                        data=None,
                    )
                )

            root_path = Path(payload.path).resolve()
            reservation_key = await try_reserve_project_path(root_path)
            if reservation_key is None:
                message = "MaaFW 项目正在运行、更新或准备环境，请稍后重试"
                publish({"stage": "failed", "status": "busy", "message": message, "final": True})
                return model_json(
                    MaaFWAgentEnvPrepareOut(
                        code=409,
                        status="error",
                        message=message,
                        data=MaaFWAgentEnvPrepareData(path=str(root_path)),
                    )
                )

            interface_service = self.ctx.get(INTERFACE_SERVICE)
            runner_service = self.ctx.get(RUNNER_SERVICE)
            if interface_service is None or runner_service is None or runtime_pool is None:
                raise MaaFWApiError("MaaFW interface/runner/runtime pool 服务尚未加载")
            publish(
                {
                    "stage": "resolving",
                    "status": "running",
                    "message": "正在解析 MaaFW 项目运行环境",
                    "percent": 2.0,
                }
            )
            interface = await _invoke_provider(interface_service, "load", root_path)
            runtime_pool_root, runtime_pool_id = await _run_to_thread_with_cancellation_drain(
                _runtime_pool_route,
                runtime_pool,
            )
            prepare_result = await _invoke_provider(
                runner_service,
                "prepare_project_environment",
                root_path,
                interface,
                runtime_pool_root=runtime_pool_root,
                runtime_pool_id=runtime_pool_id,
                send_log=append_log,
                progress=publish,
            )
            agent_result = _mapping(_value(prepare_result, "agents"))
            raw_plans = agent_result.get("plans", [])
            plans = raw_plans if isinstance(raw_plans, list) else []
            data = MaaFWAgentEnvPrepareData(
                path=str(root_path),
                agentCount=len(plans),
                agents=_build_agent_info_items(plans),
                logs=logs,
                **_runtime_cache_fields(prepare_result),
            )
            expected_fingerprint = str(
                _value(prepare_result, "projectFingerprint", "") or ""
            ).strip()
            if script_id:
                try:
                    saved = await _run_to_thread_with_cancellation_drain(
                        save_maafw_agent_env_state,
                        script_id,
                        root_path,
                        model_json(data),
                        expected_fingerprint=expected_fingerprint,
                    )
                except Exception as exc:
                    saved = False
                    append_log(f"[警告] MaaFW 运行环境缓存写入失败: {exc}")
                if not saved:
                    logs.append("[警告] MaaFW 项目输入已在预热期间变化，已丢弃旧预热缓存")
            data.logs = list(logs)
            publish(
                {
                    "stage": "completed",
                    "status": "ready",
                    "message": "MaaFW 项目运行环境准备完成",
                    "percent": 100.0,
                    "final": True,
                }
            )
            return model_json(
                MaaFWAgentEnvPrepareOut(
                    message=f"MaaFW runtime env prepared, agent count: {data.agentCount}",
                    data=data,
                )
            )
        except ValueError as exc:
            if not terminal_published:
                publish({"stage": "failed", "status": "failed", "message": str(exc), "final": True})
            return model_json(
                MaaFWAgentEnvPrepareOut(
                    code=400,
                    status="error",
                    message=str(exc),
                    data=MaaFWAgentEnvPrepareData(
                        path=str(root_path or Path(payload.path)),
                        logs=logs,
                    ),
                )
            )
        except Exception as exc:
            if not terminal_published:
                publish(
                    {
                        "stage": "failed",
                        "status": "failed",
                        "message": f"{type(exc).__name__}: {exc}",
                        "final": True,
                    }
                )
            return model_json(
                MaaFWAgentEnvPrepareOut(
                    code=500,
                    status="error",
                    message=f"{type(exc).__name__}: {exc}",
                    data=MaaFWAgentEnvPrepareData(
                        path=str(root_path or Path(payload.path)),
                        logs=logs,
                    ),
                )
            )
        finally:
            await release_project_path(reservation_key)


def _global_config(key: str) -> Any:
    getter = getattr(Config, "get", None)
    if not callable(getter):
        return None
    try:
        return getter("Update", key)
    except Exception:
        return None


__all__ = [
    "AGENT_ENV_SERVICE",
    "API_SERVICE",
    "ENV_PREPARE_PROGRESS",
    "INTERFACE_SERVICE",
    "MaaFWApiController",
    "MaaFWApiError",
    "PROJECT_UPDATE_PROGRESS",
    "PROJECT_UPDATE_SERVICE",
    "RUNTIME_POOL_SERVICE",
    "RUNNER_SERVICE",
    "_cached_runtime_is_current",
    "_runtime_pool_entry",
    "_runtime_pool_route",
    "_run_to_thread_with_cancellation_drain",
]
