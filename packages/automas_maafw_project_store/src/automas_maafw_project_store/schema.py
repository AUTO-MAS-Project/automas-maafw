from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    """Host-level plugin instance configuration."""

    model_config = ConfigDict(extra="allow")

    Root: str = Field(
        "",
        title="Project Store 根目录",
        description=(
            "留空使用 AUTO-MAS 工作目录下的 data/maafw_project_store；"
            "只接受专用的绝对目录，修改后需重载插件。"
        ),
        json_schema_extra={
            "x-auto-mas-plugin-field": True,
            "type": "folder",
            "path_kind": "folder",
            "placeholder": "留空使用默认 Project Store 目录",
            "size": "large",
        },
    )
    RunRoot: str = Field(
        "",
        title="MaaFW 脱壳运行目录",
        description=(
            "留空使用 AUTO-MAS 工作目录下的 data/maafw_project_runs；"
            "必须与 Project Store 使用不同目录树，修改后需重载插件。"
        ),
        json_schema_extra={
            "x-auto-mas-plugin-field": True,
            "type": "folder",
            "path_kind": "folder",
            "placeholder": "留空使用默认脱壳运行目录",
            "size": "large",
        },
    )
