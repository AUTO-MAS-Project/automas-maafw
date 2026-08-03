from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .service import MaaFWProjectStoreService

if TYPE_CHECKING:
    from auto_mas_core import PluginContext


DEFAULT_INSTANCE = {
    "name": "MaaFW Project Store",
    "enabled": True,
    "config": {"Root": "", "RunRoot": ""},
}

schema = {
    "Root": {
        "type": "folder",
        "path_kind": "folder",
        "default": "",
        "title": "Project Store 根目录",
        "description": "留空使用 AUTO-MAS 工作目录下的 data/maafw_project_store；修改后需重启插件。",
    },
    "RunRoot": {
        "type": "folder",
        "path_kind": "folder",
        "default": "",
        "title": "MaaFW 脱壳运行目录",
        "description": "留空使用 AUTO-MAS 工作目录下的 data/maafw_project_runs；必须与 Project Store 分离，修改后需重启插件。",
    },
}


class Plugin:
    provides = ["maafw.project_store.v1"]

    def __init__(self, ctx: "PluginContext") -> None:
        self.ctx = ctx
        self.service = MaaFWProjectStoreService(
            _configured_value(ctx, "Root"),
            run_root=_configured_value(ctx, "RunRoot"),
        )

    async def on_start(self) -> None:
        self.ctx.set("maafw.project_store.v1", self.service)
        self.ctx.logger.info("maafw.project_store.v1 ready")

    async def on_stop(self, reason: str) -> None:
        self.ctx.logger.info(
            f"maafw.project_store.v1 stopped, reason={reason}"
        )


def _configured_value(ctx: Any, name: str) -> str | None:
    config = getattr(ctx, "config", None)
    if isinstance(config, Mapping):
        value = config.get(name)
    else:
        value = getattr(config, name, None)
    normalized = str(value or "").strip()
    return normalized or None
