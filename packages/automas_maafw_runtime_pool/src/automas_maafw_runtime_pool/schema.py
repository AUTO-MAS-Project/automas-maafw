from pydantic import BaseModel, ConfigDict


class Config(BaseModel):
    """Host-level plugin instance configuration."""

    model_config = ConfigDict(extra="allow")
