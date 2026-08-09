from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core import Config
from app.models.task import ScriptItem, TaskExecuteBase, UserItem
from app.plugins import ScriptAdapterHooks, ScriptAdapterRuntime
from app.utils import get_logger
from automas_maafw_interface.loader import MaaFWInterfaceLoadError
from automas_maafw_interface.service import MaaFWInterfaceService
from automas_maafw_project_update.service import MaaFWProjectUpdateService

from .project_path import release_project_path, try_reserve_project_path
from .runtime_route import (
    RUNTIME_POOL_SERVICE,
    MaaFWRuntimePoolRoute,
    runtime_pool_route_from_service,
)
from .runner_task import MaaFWPluginAutoProxyTask
from .schema import build_source_config


logger = get_logger("MaaFW 插件适配")
_RUNTIME_POOL_ROUTE_KEY = "maafw_runtime_pool_route"
_MISSING_PROJECT_PATH = object()


def _global_update_value(key: str) -> Any:
    """Read a global update field from Config V2, then legacy Config.get."""

    setting = getattr(Config, "setting", None)
    updates = getattr(setting, "updates", None)
    v2_name = {
        "Source": "source",
        "Channel": "channel",
        "ProxyAddress": "proxy_address",
        "MirrorChyanCDK": "mirror_chyan_cdk",
    }.get(key)
    if updates is not None and v2_name is not None:
        value = getattr(updates, v2_name, None)
        if value is not None:
            return value

    getter = getattr(Config, "get", None)
    if not callable(getter):
        return None
    try:
        return getter("Update", key)
    except Exception:
        return None


def _global_proxy() -> Any:
    try:
        proxy = getattr(Config, "proxy", None)
    except Exception:
        proxy = None
    if proxy is not None:
        return proxy

    raw_proxy = str(_global_update_value("ProxyAddress") or "").strip()
    if not raw_proxy:
        return None
    if not raw_proxy.startswith(("http://", "https://", "socks5://", "socks4://")):
        raw_proxy = f"http://{raw_proxy}"
    try:
        import httpx

        return httpx.Proxy(raw_proxy)
    except Exception:
        return None


def _load_project_interface(project_path: Path) -> Any:
    if not project_path.exists():
        return _MISSING_PROJECT_PATH
    return MaaFWInterfaceService().load(project_path)


async def _await_mutating_to_thread(
    func: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Finish a mutating worker before propagating task cancellation."""

    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancellation_requested = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancellation_requested = True
        except Exception:
            # The terminal result is re-read below after the worker exits.
            pass

    if cancellation_requested:
        try:
            worker.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        raise asyncio.CancelledError
    return worker.result()


class MaaFWAdapterHooks(ScriptAdapterHooks):
    """MaaFW script adapter backed by PluginScriptConfig."""

    async def check(self, runtime: ScriptAdapterRuntime) -> str:
        if runtime.mode != "AutoProxy":
            return "MaaFW 插件当前仅支持 AutoProxy 模式"

        script_config = await runtime.build_script_model()
        raw_project_path = str(script_config.get("Info", "Path") or "").strip()
        if not raw_project_path:
            return "请设置 MaaFW 项目路径"

        project_path = Path(raw_project_path)
        try:
            interface = await asyncio.to_thread(
                _load_project_interface,
                project_path,
            )
        except MaaFWInterfaceLoadError as exc:
            return f"无法读取 MaaFW interface，请检查项目路径: {exc}"
        if interface is _MISSING_PROJECT_PATH:
            return "请设置 MaaFW 项目路径"

        if not interface.controller:
            return "MaaFW interface 未声明 controller，请检查项目目录"
        if not interface.resource:
            return "MaaFW interface 未声明 resource，请检查项目目录"
        if not interface.task:
            return "MaaFW interface 未声明 task，请检查项目目录"

        emulator_id = script_config.get("Emulator", "Id")
        emulator_index = script_config.get("Emulator", "Index")
        if emulator_id != "-" and emulator_index in ("", "-"):
            return "请在 MaaFW 脚本配置中选择模拟器实例"
        return "Pass"

    async def prepare(self, runtime: ScriptAdapterRuntime) -> None:
        await asyncio.to_thread(self._runtime_pool_route, runtime)
        await runtime.storage.lock()

        script_config = await runtime.build_script_model()
        user_config = await runtime.storage.load_user_collection()

        runtime.script_config = script_config
        runtime.user_config = user_config
        runtime.extra["maafw_project_update_logs"] = []

        await self._update_project_before_run(runtime, script_config)

        emulator_manager = None
        emulator_id = script_config.get("Emulator", "Id")
        if emulator_id != "-":
            emulator_manager = await runtime.initialize_emulator_manager(emulator_id)
        runtime.emulator_manager = emulator_manager

        runtime.script_info.user_list = [
            UserItem(
                user_id=str(uid),
                name=config.get("Info", "Name"),
                status="等待",
            )
            for uid, config in user_config.items()
            if config.get("Info", "Status")
            and config.get("Info", "RemainedDay") != 0
        ]
        self._emit_log(
            runtime,
            f"MaaFW 插件用户列表加载完成，已筛选用户数: {len(runtime.script_info.user_list)}",
        )

    def run_auto_proxy(self, runtime: ScriptAdapterRuntime) -> TaskExecuteBase:
        if runtime.script_config is None or runtime.user_config is None:
            raise RuntimeError("MaaFW 插件配置尚未准备")
        runtime_pool = self._runtime_pool_route(runtime)
        task = MaaFWPluginAutoProxyTask(
            runtime.script_info,
            runtime.script_config,
            runtime.user_config,
            runtime.emulator_manager,
            list(runtime.extra.get("maafw_project_update_logs") or []),
        )
        task.maafw_runtime_pool_root = runtime_pool.root
        task.maafw_runtime_pool_id = runtime_pool.pool_id
        return task

    async def finalize(self, runtime: ScriptAdapterRuntime) -> None:
        previous_status = runtime.script_info.status
        try:
            if runtime.user_config is not None and runtime.mode == "AutoProxy":
                # 先取得宿主写事务，再解锁并整批写回。这样资源升级事务无法
                # 插入 unlock 与多用户保存之间，也不会用旧运行快照覆盖升级结果。
                write_transaction = getattr(runtime.storage, "write_transaction", None)
                if callable(write_transaction):
                    async with write_transaction():
                        await runtime.storage.unlock()
                        await runtime.storage.save_user_models(runtime.user_config)
                else:
                    # 较旧宿主的 ScriptConfigStore 没有事务上下文；保留
                    # unlock-then-write 顺序，让插件在缺少可选能力时仍能收尾。
                    await runtime.storage.unlock()
                    await runtime.storage.save_user_models(runtime.user_config)
        finally:
            await runtime.storage.unlock()

        if previous_status == "异常" or runtime.check_result not in ("-", "", "Pass"):
            runtime.script_info.status = "异常"
            return

        error_user = [u.name for u in runtime.script_info.user_list if u.status == "异常"]
        over_user = [u.name for u in runtime.script_info.user_list if u.status == "完成"]
        if error_user:
            runtime.script_info.status = "异常"
        elif over_user:
            runtime.script_info.status = "完成"
        else:
            runtime.script_info.status = "跳过"

    async def on_crash(self, runtime: ScriptAdapterRuntime, error: Exception) -> None:
        await super().on_crash(runtime, error)

    async def _update_project_before_run(
        self,
        runtime: ScriptAdapterRuntime,
        script_config: Any,
    ) -> None:
        if not script_config.get("Update", "IfAutoUpdate"):
            self._emit_log(runtime, "MaaFW 项目运行前自动更新已关闭")
            return

        project_path = Path(script_config.get("Info", "Path")).resolve()
        project_reservation = await try_reserve_project_path(project_path)
        if project_reservation is None:
            self._emit_log(
                runtime,
                "同一路径 MaaFW 项目正在运行或更新，跳过本次运行前自动更新",
            )
            return
        try:
            try:
                interface_model = await asyncio.to_thread(
                    MaaFWInterfaceService().load,
                    project_path,
                )
            except MaaFWInterfaceLoadError as exc:
                self._emit_log(
                    runtime,
                    f"MaaFW 项目更新跳过，interface 读取失败: {exc}",
                )
                return

            script_data = await runtime.storage.read_script_data()
            source_config = build_source_config(script_data)
            effective_source_config = dict(source_config or {})
            configured_source = str(
                effective_source_config.get("source") or ""
            ).strip().casefold()
            if not configured_source:
                global_source = str(_global_update_value("Source") or "").strip().casefold()
                if global_source in {
                    "github",
                    "github_release",
                    "github release",
                }:
                    effective_source_config["source"] = "github_release"
                elif global_source in {
                    "mirrorchyan",
                    "mirror_chyan",
                    "mirror酱",
                }:
                    effective_source_config["source"] = "mirrorchyan"
            local_mirror_cdk = str(
                script_config.get("Update", "MirrorChyanCDK") or ""
            ).strip()
            global_mirror_cdk = str(
                _global_update_value("MirrorChyanCDK") or ""
            ).strip()
            mirror_cdk = local_mirror_cdk or global_mirror_cdk
            channel = script_config.get("Update", "Channel") or _global_update_value(
                "Channel"
            )
            try:
                update_result = await MaaFWProjectUpdateService().update_if_needed(
                    project_path,
                    interface_model,
                    mirror_cdk=mirror_cdk,
                    channel=channel,
                    proxy=_global_proxy(),
                    send_log=lambda message: self._emit_log(runtime, message),
                    source_config=effective_source_config,
                )
            except Exception as exc:
                self._emit_log(
                    runtime,
                    f"MaaFW 项目更新失败，继续使用当前目录: {exc}",
                )
                return

            if update_result.updated:
                try:
                    refreshed_interface = await asyncio.to_thread(
                        MaaFWInterfaceService().load,
                        project_path,
                        force_reload=True,
                    )
                except Exception as exc:
                    self._emit_log(
                        runtime,
                        "MaaFW 项目资源已更新，但新版 interface 重新读取失败；"
                        f"本次运行继续尝试当前目录: {exc}",
                    )
                    return

                self._emit_log(
                    runtime,
                    "MaaFW project updated, preparing agent Python env",
                )
                agent_prepare_logs: list[str] = []
                runtime_pool = await asyncio.to_thread(
                    self._runtime_pool_route,
                    runtime,
                )
                try:
                    await _await_mutating_to_thread(
                        _prepare_maafw_agent_python_envs,
                        project_path,
                        refreshed_interface,
                        runtime_pool_root=runtime_pool.root,
                        runtime_pool_id=runtime_pool.pool_id,
                        send_log=agent_prepare_logs.append,
                    )
                except Exception as exc:
                    self._emit_log(
                        runtime,
                        "MaaFW 项目资源已更新，但运行环境预热未完成；"
                        f"本次运行继续尝试当前目录: {exc}",
                    )
                finally:
                    for log_line in agent_prepare_logs:
                        self._emit_log(runtime, log_line)
        finally:
            await release_project_path(project_reservation)

    @staticmethod
    def _emit_log(runtime: ScriptAdapterRuntime, message: str) -> None:
        """把 adapter 关键操作日志同时写入后端日志与 UI 通道。

        UI 侧由 task_manager 广播 script_info.log（task.log 事件），因此这里
        除了 logger.info，还要追加到共享缓冲并刷新 script_info.log，
        否则用户在脚本管理页看不到插件适配阶段的进度。
        """
        logger.info(message)
        logs = runtime.extra.setdefault("maafw_project_update_logs", [])
        if isinstance(logs, list):
            logs.extend(_format_update_log_lines(message))
            runtime.script_info.log = "".join(logs[-80:])

    @staticmethod
    def _runtime_pool_route(runtime: ScriptAdapterRuntime) -> MaaFWRuntimePoolRoute:
        cached = runtime.extra.get(_RUNTIME_POOL_ROUTE_KEY)
        if isinstance(cached, MaaFWRuntimePoolRoute):
            return cached
        if cached is not None:
            raise RuntimeError("MaaFW Runtime Pool 缓存路由类型无效")
        route = runtime_pool_route_from_service(
            runtime.get_service(RUNTIME_POOL_SERVICE)
        )
        runtime.extra[_RUNTIME_POOL_ROUTE_KEY] = route
        return route


def _format_update_log_lines(message: str) -> list[str]:
    now = datetime.now().strftime("%H:%M:%S")
    return [f"[{now}] {line}\n" for line in str(message).splitlines() or [""]]


def _prepare_maafw_agent_python_envs(
    project_path: Path,
    interface_model: Any,
    *,
    runtime_pool_root: Path,
    runtime_pool_id: str,
    send_log: Any = None,
) -> None:
    from automas_maafw_runner.service import MaaFWRunnerService

    MaaFWRunnerService().prepare_project_environment(
        project_path,
        interface_model,
        runtime_pool_root=runtime_pool_root,
        runtime_pool_id=runtime_pool_id,
        send_log=send_log,
    )


def build_legacy_script_item(script_item: ScriptItem) -> ScriptItem:
    return script_item
