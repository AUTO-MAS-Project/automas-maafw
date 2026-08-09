from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .service import MaaFWRuntimePoolService

if TYPE_CHECKING:
    from auto_mas_core import PluginContext


DEFAULT_INSTANCE = {
    "name": "MaaFW Runtime Pool",
    "enabled": True,
    "config": {"Root": ""},
}

schema = {
    "Root": {
        "type": "folder",
        "path_kind": "folder",
        "default": "",
        "title": "Runtime Pool 根目录",
        "description": "留空使用 AUTO-MAS 工作目录下的 config/maafw_runtime_pool；修改后需重启插件。",
    },
}


class Plugin:
    provides = ["maafw.runtime_pool.v1"]

    def __init__(self, ctx: "PluginContext") -> None:
        self.ctx = ctx
        self.service = MaaFWRuntimePoolService(_configured_root(ctx))

    async def on_start(self) -> None:
        self.ctx.set("maafw.runtime_pool.v1", self.service)
        self.ctx.logger.info("maafw.runtime_pool.v1 ready")

    async def on_stop(self, reason: str) -> None:
        self.ctx.set("maafw.runtime_pool.v1", None)
        self.ctx.logger.info(f"maafw.runtime_pool.v1 stopped, reason={reason}")


def _configured_root(ctx: Any) -> str | None:
    config = getattr(ctx, "config", None)
    if isinstance(config, Mapping):
        value = config.get("Root")
    else:
        value = getattr(config, "Root", None)
    normalized = str(value or "").strip()
    return normalized or None
