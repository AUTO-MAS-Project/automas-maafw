from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    """Host-level plugin instance configuration."""

    model_config = ConfigDict(extra="allow")

    Root: str = Field(
        "",
        title="Runtime Pool 根目录",
        description=(
            "留空使用 AUTO-MAS 工作目录下的 config/maafw_runtime_pool；"
            "只接受专用的绝对目录，修改后需重载插件。"
        ),
        json_schema_extra={
            "x-auto-mas-plugin-field": True,
            "type": "folder",
            "path_kind": "folder",
            "placeholder": "留空使用默认 Runtime Pool 目录",
            "size": "large",
        },
    )
