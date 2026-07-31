from __future__ import annotations

import asyncio
import copy
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core import Config
from app.plugins import PluginHttpRequest

from .configuration_reuse import (
    MaaFWConfigurationReuseError,
    discover_configuration_sources,
    plan_external_configuration_import,
    plan_internal_user_copy,
    public_configuration_plan,
    stable_json_hash,
    user_records_hash,
)


INTERFACE_SERVICE = "maafw.interface.v1"
PLAN_TTL_SECONDS = 30 * 60
MAX_PENDING_PLANS = 128


class MaaFWConfigurationReuseController:
    """Plugin HTTP boundary for previewed, CAS-guarded configuration reuse."""

    def __init__(self, ctx: Any, registry: Any) -> None:
        self.ctx = ctx
        self.registry = registry
        self._plans: dict[str, dict[str, Any]] = {}
        self._script_locks: dict[str, asyncio.Lock] = {}

    def register_routes(self) -> None:
        self.ctx.server.http(
            "/maafw/config-reuse/sources",
            self.discover_sources,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw/config-reuse/plan/external",
            self.plan_external,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw/config-reuse/plan/copy",
            self.plan_copy,
            methods=("POST",),
        )
        self.ctx.server.http(
            "/maafw/config-reuse/apply",
            self.apply_plan,
            methods=("POST",),
        )

    def clear(self) -> None:
        self._plans.clear()
        self._script_locks.clear()

    async def discover_sources(self, request: PluginHttpRequest) -> dict[str, Any]:
        return await self._respond(
            lambda: self._discover_sources(_payload(request))
        )

    async def plan_external(self, request: PluginHttpRequest) -> dict[str, Any]:
        return await self._respond(
            lambda: self._plan_external(_payload(request))
        )

    async def plan_copy(self, request: PluginHttpRequest) -> dict[str, Any]:
        return await self._respond(
            lambda: self._plan_copy(_payload(request))
        )

    async def apply_plan(self, request: PluginHttpRequest) -> dict[str, Any]:
        return await self._respond(
            lambda: self._apply_plan(_payload(request))
        )

    async def _discover_sources(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        script_id = _required_text(payload, "scriptId", "脚本 ID")
        record = await _script_record(script_id)
        self._require_maafw_record(record)
        script_config = _record_config(record)
        project_path = str(_nested(script_config, "Info", "Path") or "").strip()
        source_path = str(payload.get("sourcePath") or project_path).strip()
        if not source_path:
            raise MaaFWConfigurationReuseError("请选择外部 MaaFW 配置文件或目录")
        sources = await self._discover_for_record(
            record,
            source_path,
            project_path=project_path,
        )
        return {"sources": sources, "count": len(sources)}

    async def _plan_external(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        script_id = _required_text(payload, "scriptId", "脚本 ID")
        source = _mapping(payload.get("source"))
        if not source:
            raise MaaFWConfigurationReuseError("请选择一份外部 MaaFW 配置")
        target = str(payload.get("target") or "new-user").strip()
        record = await _script_record(script_id)
        self._require_maafw_record(record)
        script_config = _record_config(record)
        users = await _user_records(script_id)
        if target == "project-and-first-user" and users:
            raise MaaFWConfigurationReuseError(
                "当前脚本已有用户，项目向导只能把外部配置导入为第一个用户"
            )

        pack_service = self._pack_service(record)
        pack_method = getattr(pack_service, "plan_configuration_import", None)
        if callable(pack_method):
            plan_value = await _call_variants(
                pack_method,
                (
                    (
                        (dict(source),),
                        {
                            "target": target,
                            "script_config": copy.deepcopy(script_config),
                            "user_config": None,
                        },
                    ),
                    (
                        (dict(source),),
                        {
                            "target": target,
                            "scriptConfig": copy.deepcopy(script_config),
                            "userConfig": None,
                        },
                    ),
                ),
                "生成 MaaFW pack 配置导入计划",
            )
            plan = _json_mapping(plan_value, "pack 配置导入计划")
            _validate_pack_plan(plan, target, source)
            plan["source"] = copy.deepcopy(dict(source))
        else:
            interface = await self._load_interface(script_config)
            plan = await asyncio.to_thread(
                plan_external_configuration_import,
                source,
                interface,
                target=target,
            )

        stored = self._store_plan(
            script_id,
            plan,
            script_hash=stable_json_hash(script_config),
            users_hash=user_records_hash(users),
            script_type=_record_type(record),
            source_user_id=None,
            source_user_hash=None,
        )
        return public_configuration_plan(stored)

    async def _plan_copy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        script_id = _required_text(payload, "scriptId", "脚本 ID")
        source_user_id = _required_text(payload, "sourceUserId", "来源用户 ID")
        record = await _script_record(script_id)
        self._require_maafw_record(record)
        script_config = _record_config(record)
        users = await _user_records(script_id)
        source_user = next(
            (item for item in users if _record_id(item) == source_user_id),
            None,
        )
        if source_user is None:
            raise MaaFWConfigurationReuseError("来源用户不存在，请刷新后重试")
        plan = plan_internal_user_copy(
            source_user,
            target_name=str(payload.get("targetName") or "").strip() or None,
        )
        stored = self._store_plan(
            script_id,
            plan,
            script_hash=stable_json_hash(script_config),
            users_hash=user_records_hash(users),
            script_type=_record_type(record),
            source_user_id=source_user_id,
            source_user_hash=stable_json_hash(source_user.get("config") or {}),
        )
        return public_configuration_plan(stored)

    async def _apply_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        script_id = _required_text(payload, "scriptId", "脚本 ID")
        plan_id = _required_text(payload, "planId", "计划 ID")
        self._purge_expired_plans()
        plan = self._plans.get(plan_id)
        if plan is None or str(plan.get("scriptId") or "") != script_id:
            raise MaaFWConfigurationReuseError("配置导入计划不存在或已过期，请重新预览")
        if plan.get("readyToApply") is not True:
            raise MaaFWConfigurationReuseError("配置导入计划仍有阻塞项，不能应用")

        lock = self._script_locks.setdefault(script_id, asyncio.Lock())
        async with lock:
            owner = f"maafw-config-reuse:{script_id}:{plan_id}"
            async with Config.script_config_transaction(script_id, owner=owner):
                result = await self._apply_in_transaction(script_id, plan)
        self._plans.pop(plan_id, None)
        return result

    async def _apply_in_transaction(
        self,
        script_id: str,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = await _script_record(script_id)
        self._require_maafw_record(record)
        if _record_type(record) != str(plan.get("scriptType") or ""):
            raise MaaFWConfigurationReuseError("脚本类型已变化，请重新预览")
        script_config = _record_config(record)
        users = await _user_records(script_id)
        if stable_json_hash(script_config) != str(plan.get("scriptHash") or ""):
            raise MaaFWConfigurationReuseError("脚本配置在预览后已变化，请重新预览")
        if user_records_hash(users) != str(plan.get("usersHash") or ""):
            raise MaaFWConfigurationReuseError("用户集合在预览后已变化，请重新预览")

        if str(plan.get("sourceFingerprint") or ""):
            await self._revalidate_external_source(record, plan)
        source_user_id = str(plan.get("sourceUserId") or "")
        if source_user_id:
            source_user = next(
                (item for item in users if _record_id(item) == source_user_id),
                None,
            )
            if source_user is None or stable_json_hash(
                source_user.get("config") or {}
            ) != str(plan.get("sourceUserHash") or ""):
                raise MaaFWConfigurationReuseError("来源用户在预览后已变化，请重新预览")

        target = str(plan.get("target") or "")
        if target == "project-and-first-user" and users:
            raise MaaFWConfigurationReuseError("当前脚本已存在用户，拒绝覆盖“第一个用户”语义")
        user_targets = plan.get("userTargetConfigs")
        if not isinstance(user_targets, list) or len(user_targets) != 1:
            raise MaaFWConfigurationReuseError("当前配置导入只允许创建一个用户")
        user_target = _mapping(user_targets[0])
        if not user_target:
            raise MaaFWConfigurationReuseError("配置导入计划缺少用户目标快照")

        created_user_id = ""
        script_write_attempted = False
        rollback_errors: list[str] = []
        try:
            created_uid, _created_user = await Config.add_user(script_id)
            created_user_id = str(created_uid)
            await Config.update_user(script_id, created_user_id, user_target)
            script_target = _mapping(plan.get("scriptTargetConfig"))
            if script_target:
                script_write_attempted = True
                await Config.update_script(script_id, script_target)
        except Exception as exc:
            if script_write_attempted:
                try:
                    await Config.update_script(script_id, script_config)
                except Exception as rollback_exc:
                    rollback_errors.append(f"脚本配置恢复失败：{rollback_exc}")
            if created_user_id:
                try:
                    await Config.del_user(script_id, created_user_id)
                except Exception as rollback_exc:
                    rollback_errors.append(f"新用户清理失败：{rollback_exc}")
            suffix = f"；{'；'.join(rollback_errors)}" if rollback_errors else ""
            raise MaaFWConfigurationReuseError(
                f"应用配置导入计划失败，已尝试恢复：{exc}{suffix}"
            ) from exc

        created_records = await _user_records(script_id, created_user_id)
        return {
            "applied": True,
            "planId": str(plan.get("planId") or ""),
            "target": target,
            "createdUser": created_records[0] if created_records else {
                "id": created_user_id,
            },
            "scriptUpdated": bool(_mapping(plan.get("scriptTargetConfig"))),
        }

    async def _revalidate_external_source(
        self,
        record: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> None:
        source = _mapping(plan.get("source"))
        source_path = str(source.get("path") or "").strip()
        if not source_path:
            raise MaaFWConfigurationReuseError("配置导入计划缺少来源路径")
        script_config = _record_config(record)
        project_path = str(_nested(script_config, "Info", "Path") or "").strip()
        sources = await self._discover_for_record(
            record,
            source_path,
            project_path=project_path,
        )
        source_id = str(source.get("sourceId") or "")
        refreshed = next(
            (item for item in sources if str(item.get("sourceId") or "") == source_id),
            None,
        )
        if refreshed is None or str(refreshed.get("fingerprint") or "") != str(
            plan.get("sourceFingerprint") or ""
        ):
            raise MaaFWConfigurationReuseError(
                "外部 MaaFW 配置在预览后已变化，请重新发现并确认"
            )

    async def _discover_for_record(
        self,
        record: Mapping[str, Any],
        source_path: str,
        *,
        project_path: str,
    ) -> list[dict[str, Any]]:
        pack_service = self._pack_service(record)
        method = getattr(pack_service, "discover_configuration_sources", None)
        if callable(method):
            project = {
                "path": project_path,
                "scriptType": _record_type(record),
                "scriptConfig": _record_config(record),
            }
            value = await _call_variants(
                method,
                (
                    ((project,), {"source_path": source_path}),
                    ((project,), {"sourcePath": source_path}),
                    ((project, source_path), {}),
                ),
                "发现 MaaFW pack 外部配置",
            )
            sources = _json_object_list(value, "pack 配置来源")
        else:
            sources = await asyncio.to_thread(
                discover_configuration_sources,
                source_path,
            )
        for source in sources:
            for field in ("sourceId", "label", "kind", "path", "fingerprint"):
                if not str(source.get(field) or "").strip():
                    raise MaaFWConfigurationReuseError(
                        f"配置来源缺少字段 {field}"
                    )
        return sources

    async def _load_interface(self, script_config: Mapping[str, Any]) -> dict[str, Any]:
        project_path = str(_nested(script_config, "Info", "Path") or "").strip()
        if not project_path:
            raise MaaFWConfigurationReuseError("脚本尚未绑定 MaaFW 项目目录")
        service = self.ctx.get(INTERFACE_SERVICE)
        if service is None:
            raise MaaFWConfigurationReuseError(f"缺少服务 {INTERFACE_SERVICE}")
        method = getattr(service, "load", None)
        if not callable(method):
            raise MaaFWConfigurationReuseError(f"{INTERFACE_SERVICE} 未提供 load")
        value = await _invoke(method, project_path)
        return _json_mapping(value, "ProjectInterface")

    def _pack_service(self, record: Mapping[str, Any]) -> Any:
        type_key = _record_type(record)
        pack = self.registry.get_project_pack(type_key.casefold())
        if not isinstance(pack, Mapping):
            return None
        service_key = str(pack.get("resource_service_key") or "").strip()
        return self.ctx.get(service_key) if service_key else None

    def _require_maafw_record(self, record: Mapping[str, Any]) -> None:
        type_key = _record_type(record)
        if type_key == "MaaFW":
            return
        if self.registry.get_project_pack(type_key.casefold()) is not None:
            return
        raise MaaFWConfigurationReuseError("当前脚本不是 MaaFW 项目或已注册 project pack")

    def _store_plan(
        self,
        script_id: str,
        plan: Mapping[str, Any],
        *,
        script_hash: str,
        users_hash: str,
        script_type: str,
        source_user_id: str | None,
        source_user_hash: str | None,
    ) -> dict[str, Any]:
        self._purge_expired_plans()
        if len(self._plans) >= MAX_PENDING_PLANS:
            oldest = min(
                self._plans,
                key=lambda key: float(self._plans[key].get("createdAtEpoch") or 0),
            )
            self._plans.pop(oldest, None)

        now = datetime.now(timezone.utc)
        plan_id = str(uuid.uuid4())
        stored = copy.deepcopy(dict(plan))
        stored.update(
            {
                "planId": plan_id,
                "scriptId": script_id,
                "scriptType": script_type,
                "scriptHash": script_hash,
                "usersHash": users_hash,
                "sourceUserId": source_user_id or "",
                "sourceUserHash": source_user_hash or "",
                "createdAtEpoch": time.time(),
                "expiresAt": (now + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat(),
            }
        )
        self._plans[plan_id] = stored
        return stored

    def _purge_expired_plans(self) -> None:
        threshold = time.time() - PLAN_TTL_SECONDS
        for plan_id, plan in tuple(self._plans.items()):
            if float(plan.get("createdAtEpoch") or 0) < threshold:
                self._plans.pop(plan_id, None)

    @staticmethod
    async def _respond(
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            data = await operation()
        except MaaFWConfigurationReuseError as exc:
            return {"code": 400, "status": "error", "message": str(exc), "data": None}
        except Exception as exc:
            return {
                "code": 500,
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
                "data": None,
            }
        return {"code": 200, "status": "success", "message": "", "data": data}


async def _script_record(script_id: str) -> dict[str, Any]:
    records = await Config.get_script_records(script_id)
    if len(records) != 1:
        raise MaaFWConfigurationReuseError("脚本不存在或记录不唯一")
    return _record_mapping(records[0])


async def _user_records(
    script_id: str,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    return [_record_mapping(item) for item in await Config.get_user_records(script_id, user_id)]


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
    raise MaaFWConfigurationReuseError("宿主记录不是 JSON object 或稳定 DTO")


def _record_id(record: Mapping[str, Any]) -> str:
    return str(record.get("id") or record.get("userId") or "")


def _record_type(record: Mapping[str, Any]) -> str:
    return str(record.get("type") or record.get("typeKey") or "").strip()


def _record_config(record: Mapping[str, Any]) -> dict[str, Any]:
    config = record.get("config")
    if not isinstance(config, Mapping):
        raise MaaFWConfigurationReuseError("宿主记录缺少 JSON config")
    return copy.deepcopy(dict(config))


def _validate_pack_plan(
    plan: Mapping[str, Any],
    target: str,
    source: Mapping[str, Any],
) -> None:
    if str(plan.get("target") or "") != target:
        raise MaaFWConfigurationReuseError("pack 配置计划返回了错误 target")
    if str(plan.get("sourceFingerprint") or "") != str(source.get("fingerprint") or ""):
        raise MaaFWConfigurationReuseError("pack 配置计划的来源 fingerprint 不一致")
    targets = plan.get("userTargetConfigs")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], Mapping):
        raise MaaFWConfigurationReuseError("pack 配置计划必须包含一个用户目标快照")
    if not isinstance(plan.get("scriptTargetConfig", {}), Mapping):
        raise MaaFWConfigurationReuseError("pack 配置计划 scriptTargetConfig 必须是 object")


async def _call_variants(
    method: Callable[..., Any],
    variants: Sequence[tuple[tuple[Any, ...], dict[str, Any]]],
    operation: str,
) -> Any:
    for args, kwargs in variants:
        try:
            inspect.signature(method).bind(*args, **kwargs)
        except (TypeError, ValueError):
            continue
        return await _invoke(method, *args, **kwargs)
    raise MaaFWConfigurationReuseError(f"{operation}失败：服务方法签名不兼容")


async def _invoke(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        if inspect.iscoroutinefunction(method):
            value = method(*args, **kwargs)
        else:
            value = await asyncio.to_thread(method, *args, **kwargs)
        if inspect.isawaitable(value):
            value = await value
        return value
    except MaaFWConfigurationReuseError:
        raise
    except Exception as exc:
        raise MaaFWConfigurationReuseError(str(exc)) from exc


def _json_mapping(value: Any, label: str) -> dict[str, Any]:
    payload = _record_mapping(value)
    try:
        # Enforce a real JSON-only cross-plugin boundary.
        return copy.deepcopy(json_round_trip(payload))
    except (TypeError, ValueError) as exc:
        raise MaaFWConfigurationReuseError(f"{label} 包含非 JSON 值") from exc


def _json_object_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise MaaFWConfigurationReuseError(f"{label} 必须是 JSON object array")
    return [_json_mapping(item, label) for item in value]


def json_round_trip(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _payload(request: PluginHttpRequest) -> dict[str, Any]:
    if isinstance(request.json, Mapping):
        return dict(request.json)
    if isinstance(request.query, Mapping):
        return dict(request.query)
    return {}


def _required_text(payload: Mapping[str, Any], key: str, label: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise MaaFWConfigurationReuseError(f"{label}不能为空（字段 {key}）")
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _nested(payload: Mapping[str, Any], group: str, name: str) -> Any:
    value = payload.get(group)
    return value.get(name) if isinstance(value, Mapping) else None


__all__ = [
    "INTERFACE_SERVICE",
    "MaaFWConfigurationReuseController",
]
