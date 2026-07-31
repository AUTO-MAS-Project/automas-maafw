from __future__ import annotations

from .service import MaaFWProjectUpdateService
from .updater import (
    MaaFWProjectUpdateCandidate,
    MaaFWProjectUpdateDiscovery,
    MaaFWProjectUpdateError,
    MaaFWProjectUpdateResult,
    MaaFWUpdateProviderInfo,
    apply_maafw_project_update,
    check_maafw_project_update,
    discover_maafw_project_update,
    list_update_providers,
    update_maafw_project_if_needed,
)

__all__ = [
    "MaaFWProjectUpdateCandidate",
    "MaaFWProjectUpdateDiscovery",
    "MaaFWProjectUpdateError",
    "MaaFWProjectUpdateResult",
    "MaaFWProjectUpdateService",
    "MaaFWUpdateProviderInfo",
    "apply_maafw_project_update",
    "check_maafw_project_update",
    "discover_maafw_project_update",
    "list_update_providers",
    "update_maafw_project_if_needed",
]
