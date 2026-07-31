from __future__ import annotations

from .service import MaaFWProjectUpdateService
from .updater import (
    DOWNLOAD_MAX_BYTES,
    MaaFWDownloadedProjectPackage,
    MaaFWProjectUpdateCandidate,
    MaaFWProjectUpdateDiscovery,
    MaaFWProjectUpdateError,
    MaaFWProjectUpdateResult,
    MaaFWUpdateProviderInfo,
    apply_maafw_project_update,
    check_maafw_project_update,
    discover_maafw_project_update,
    download_maafw_project_package,
    list_update_providers,
    update_maafw_project_if_needed,
)

__all__ = [
    "DOWNLOAD_MAX_BYTES",
    "MaaFWDownloadedProjectPackage",
    "MaaFWProjectUpdateCandidate",
    "MaaFWProjectUpdateDiscovery",
    "MaaFWProjectUpdateError",
    "MaaFWProjectUpdateResult",
    "MaaFWProjectUpdateService",
    "MaaFWUpdateProviderInfo",
    "apply_maafw_project_update",
    "check_maafw_project_update",
    "discover_maafw_project_update",
    "download_maafw_project_package",
    "list_update_providers",
    "update_maafw_project_if_needed",
]
