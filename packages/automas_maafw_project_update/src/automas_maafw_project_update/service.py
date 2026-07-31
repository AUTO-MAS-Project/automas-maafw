from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from automas_maafw_interface.models import MaaFWInterface

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


class MaaFWProjectUpdateService:
    """maafw.project_update.v1 service."""

    def list_providers(self) -> list[MaaFWUpdateProviderInfo]:
        return list_update_providers()

    async def discover_update(
        self,
        interface: MaaFWInterface | dict[str, Any],
        *,
        current_version: str | None = None,
        source_config: dict[str, Any] | None = None,
        proxy: httpx.Proxy | None = None,
        send_log: Any = None,
    ) -> MaaFWProjectUpdateDiscovery | None:
        return await discover_maafw_project_update(
            self._coerce_interface(interface),
            current_version=current_version,
            source_config=source_config,
            proxy=proxy,
            send_log=send_log,
        )

    async def check_update(
        self,
        interface: MaaFWInterface | dict[str, Any],
        *,
        current_version: str | None = None,
        source_config: dict[str, Any] | None = None,
        proxy: httpx.Proxy | None = None,
        send_log: Any = None,
    ) -> MaaFWProjectUpdateCandidate | None:
        return await check_maafw_project_update(
            self._coerce_interface(interface),
            current_version=current_version,
            source_config=source_config,
            proxy=proxy,
            send_log=send_log,
        )

    async def apply_update(
        self,
        project_path: str | Path,
        candidate: MaaFWProjectUpdateCandidate | Mapping[str, Any],
        *,
        proxy: httpx.Proxy | None = None,
        send_log: Any = None,
    ) -> None:
        await apply_maafw_project_update(
            Path(project_path).resolve(),
            self._coerce_candidate(candidate),
            proxy=proxy,
            send_log=send_log,
        )

    async def download_package(
        self,
        download_root: str | Path,
        candidate: MaaFWProjectUpdateCandidate | Mapping[str, Any],
        *,
        proxy: httpx.Proxy | None = None,
        send_log: Any = None,
        max_download_bytes: int = DOWNLOAD_MAX_BYTES,
    ) -> dict[str, Any]:
        """Download a validated ZIP for an immutable-store consumer."""

        downloaded = await download_maafw_project_package(
            Path(download_root).resolve(),
            self._coerce_candidate(candidate),
            proxy=proxy,
            send_log=send_log,
            max_download_bytes=max_download_bytes,
        )
        return self._downloaded_package_dict(downloaded)

    async def update_if_needed(
        self,
        project_path: str | Path,
        interface: MaaFWInterface | dict[str, Any],
        *,
        mirror_cdk: str = "",
        channel: str = "stable",
        proxy: httpx.Proxy | None = None,
        send_log: Any = None,
        source_config: dict[str, Any] | None = None,
    ) -> MaaFWProjectUpdateResult:
        return await update_maafw_project_if_needed(
            Path(project_path).resolve(),
            self._coerce_interface(interface),
            mirror_cdk=mirror_cdk,
            channel=channel,
            proxy=proxy,
            send_log=send_log,
            source_config=source_config,
        )

    @staticmethod
    def _coerce_candidate(
        candidate: MaaFWProjectUpdateCandidate | Mapping[str, Any],
    ) -> MaaFWProjectUpdateCandidate:
        if isinstance(candidate, MaaFWProjectUpdateCandidate):
            return candidate
        if hasattr(candidate, "model_dump"):
            data = candidate.model_dump(mode="json", by_alias=True)
        elif isinstance(candidate, Mapping):
            data = dict(candidate)
        else:
            raise MaaFWProjectUpdateError(
                "MaaFW update candidate must be a JSON object or stable DTO"
            )

        source = str(data.get("source") or "").strip()
        version = str(data.get("version") or "").strip()
        if not source or not version:
            raise MaaFWProjectUpdateError(
                "MaaFW update candidate is missing source or version"
            )
        return MaaFWProjectUpdateCandidate(
            source=source,
            version=version,
            download_url=str(
                data.get("download_url") or data.get("downloadUrl") or ""
            ).strip()
            or None,
            sha256=str(data.get("sha256") or "").strip() or None,
        )

    @staticmethod
    def _downloaded_package_dict(
        package: MaaFWDownloadedProjectPackage,
    ) -> dict[str, Any]:
        return {
            "source": package.source,
            "version": package.version,
            "path": package.path,
            "size": package.size,
            "sha256": package.sha256,
        }

    @staticmethod
    def _coerce_interface(interface: MaaFWInterface | dict[str, Any]) -> MaaFWInterface:
        if isinstance(interface, MaaFWInterface):
            return interface
        if hasattr(interface, "model_dump"):
            return MaaFWInterface.model_validate(
                interface.model_dump(mode="json", by_alias=True)
            )
        return MaaFWInterface.model_validate(interface)
