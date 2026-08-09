from __future__ import annotations

from typing import TYPE_CHECKING

from .service import MaaFWRunnerService

if TYPE_CHECKING:
    from auto_mas_core import PluginContext


DEFAULT_INSTANCE = {
    "name": "MaaFW Runner",
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


class Plugin:
    provides = ["maafw.runner.v1"]

    def __init__(self, ctx: "PluginContext") -> None:
        self.ctx = ctx
        self.service = MaaFWRunnerService()

    async def on_start(self) -> None:
        self.service.reopen_worker_registry()
        self.ctx.set("maafw.runner.v1", self.service)
        self.ctx.logger.info("maafw.runner.v1 ready")

    async def on_stop(self, reason: str) -> None:
        report = await self.service.shutdown_workers()
        self.ctx.set("maafw.runner.v1", None)
        suffix = f", shutdown_errors={len(report.errors)}" if report.errors else ""
        self.ctx.logger.info(
            "maafw.runner.v1 stopped, "
            f"reason={reason}, workers={report.requested}, "
            f"terminated={report.terminated}, killed={report.killed}{suffix}"
        )
