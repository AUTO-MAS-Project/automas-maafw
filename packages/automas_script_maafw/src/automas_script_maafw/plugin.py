from __future__ import annotations

from app.plugins import ScriptAdapterDefinition, ScriptAdapterPlugin

from .adapter import MaaFWAdapterHooks
from .configuration_controller import MaaFWConfigurationReuseController
from .registry import MaaFWRegistryService
from .schema import SCRIPT_GROUPS, USER_GROUPS


DEFAULT_INSTANCE = {
    "name": "MaaFW 脚本适配",
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

API_SERVICE = "maafw.api.v1"


class Plugin(ScriptAdapterPlugin):
    """MaaFW script adapter plugin."""

    provides = [
        "maafw.registry.v1",
        "maafw.configuration_reuse.v1",
        "maafw.api.v1",
    ]
    needs = ["maafw.runtime_pool.v1"]
    wants = [
        "emulator",
        "maafw.interface.v1",
        "maafw.project_update.v1",
        "maafw.agent_env.v1",
        "maafw.runner.v1",
    ]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.registry = MaaFWRegistryService()
        self.configuration_reuse = MaaFWConfigurationReuseController(
            ctx,
            self.registry,
        )
        # Import the transport controller lazily.  This keeps schema/adapter
        # discovery usable in lightweight tooling that does not mount the
        # host plugin server, while the normal lifecycle always registers the
        # routes through ``ctx.server``.
        self.api = None

    def build_script_adapters(self):
        return [
            ScriptAdapterDefinition(
                type_key="MaaFW",
                display_name="MaaFramework 项目",
                hooks_factory=MaaFWAdapterHooks,
                script_groups=SCRIPT_GROUPS,
                user_groups=USER_GROUPS,
                script_class_name="MaaFWPluginConfig",
                user_class_name="MaaFWPluginUserConfig",
                module="automas_script_maafw.schema",
                related_bindings={"EmulatorConfig": "EmulatorConfig"},
                supported_modes=("AutoProxy",),
                icon="MaaFW",
                icon_path="automas_script_maafw:assets/maafw.png",
                editor_kind="plugin:automas_script_maafw",
                legacy_config_class_name="MaaFWConfig",
                legacy_user_config_class_name="MaaFWUserConfig",
                is_builtin=False,
                metadata={
                    "framework": "maafw",
                    "source": "automas_script_maafw",
                    "create_group": "general",
                    "m9a_standalone": False,
                },
            )
        ]

    async def on_start(self) -> None:
        from .api import MaaFWApiController

        await super().on_start()
        self.ctx.set("maafw.registry.v1", self.registry)
        self.ctx.set("maafw.configuration_reuse.v1", self.configuration_reuse)
        self.api = MaaFWApiController(self.ctx)
        self.ctx.set(API_SERVICE, self.api)
        self.configuration_reuse.register_routes()
        self.api.register_routes()

    async def on_stop(self, reason: str) -> None:
        if self.api is not None:
            await self.api.close()
            self.api = None
        self.configuration_reuse.clear()
        self.ctx.set(API_SERVICE, None)
        self.ctx.set("maafw.configuration_reuse.v1", None)
        self.ctx.set("maafw.registry.v1", None)
        await super().on_stop(reason)
