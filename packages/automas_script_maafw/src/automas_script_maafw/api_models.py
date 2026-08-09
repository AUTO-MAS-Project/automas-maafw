from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class MaaFWProjectUpdateIn(_ApiModel):
    scriptId: str
    # Do not let Pydantic coerce strings such as ``"false"`` or ``"0"``.
    # This payload crosses the host/plugin boundary and truthy coercion would
    # turn a dry-run request into a mutating update.
    apply: StrictBool = False

    @field_validator("scriptId")
    @classmethod
    def _script_id_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("scriptId 不能为空")
        return value


class MaaFWProjectUpdateData(_ApiModel):
    checked: StrictBool = False
    updated: StrictBool = False
    updateAvailable: StrictBool = False
    installable: StrictBool = False
    currentVersion: str = ""
    latestVersion: str | None = None
    source: str | None = None
    providerErrorCode: int | None = None
    logs: list[str] = Field(default_factory=list)


class MaaFWProjectUpdateOut(_ApiModel):
    code: int = 200
    status: str = "success"
    message: str = ""
    data: MaaFWProjectUpdateData | None = None


class MaaFWAgentEnvPrepareIn(_ApiModel):
    path: str
    scriptId: str | None = None

    @field_validator("path")
    @classmethod
    def _path_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("path 不能为空")
        return value

    @field_validator("scriptId")
    @classmethod
    def _optional_script_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class MaaFWAgentEnvInfo(_ApiModel):
    childExec: str = ""
    executable: str = ""
    runtimeKind: str | None = None
    isolatedVenvPath: str | None = None
    fallbackReason: str | None = None


class MaaFWAgentEnvPrepareData(_ApiModel):
    path: str
    agentCount: int = 0
    agents: list[MaaFWAgentEnvInfo] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    runtimeId: str | None = None
    poolId: str | None = None
    pythonExecutable: str | None = None
    venvPath: str | None = None


class MaaFWAgentEnvPrepareOut(_ApiModel):
    code: int = 200
    status: str = "success"
    message: str = ""
    data: MaaFWAgentEnvPrepareData | None = None


def model_json(model: BaseModel) -> dict[str, Any]:
    """Return a JSON-compatible response payload for the plugin gateway."""

    return model.model_dump(mode="json", by_alias=True)


__all__ = [
    "MaaFWAgentEnvInfo",
    "MaaFWAgentEnvPrepareData",
    "MaaFWAgentEnvPrepareIn",
    "MaaFWAgentEnvPrepareOut",
    "MaaFWProjectUpdateData",
    "MaaFWProjectUpdateIn",
    "MaaFWProjectUpdateOut",
    "model_json",
]
