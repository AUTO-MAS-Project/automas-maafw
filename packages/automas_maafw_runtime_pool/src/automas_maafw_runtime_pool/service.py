from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .identity import build_runtime_id
from .installer import install_python_runtime
from .pool import MaaFWRuntimePool, MaaFWRuntimePoolError, RuntimeInstaller


class MaaFWRuntimePoolService:
    """JSON-friendly `maafw.runtime_pool.v1` service."""

    def __init__(
        self,
        pool_root: str | Path | None = None,
        *,
        installer: RuntimeInstaller | None = install_python_runtime,
    ) -> None:
        root = pool_root or (Path.cwd() / "config" / "maafw_runtime_pool")
        self.pool = MaaFWRuntimePool(root, installer=installer)

    def list(self) -> list[dict[str, Any]]:
        return self.pool.list()

    def list_runtimes(self) -> list[dict[str, Any]]:
        return self.list()

    def resolve(
        self,
        requirements: Iterable[str],
        *,
        touch: bool = False,
    ) -> dict[str, Any] | None:
        return self.pool.resolve(requirements, touch=touch)

    def ensure(
        self,
        requirements: Iterable[str],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.pool.ensure(requirements, metadata=metadata)

    def resolve_runtime(
        self,
        request: str | Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if isinstance(request, Mapping):
            runtime_id = str(request.get("runtimeId") or "").strip()
            if runtime_id:
                resolved = self.pool.get(
                    runtime_id,
                    touch=bool(request.get("touch", False)),
                )
                if resolved is not None and _request_contains_requirements(request):
                    requirements, _, _ = _normalize_runtime_request(request)
                    expected = self.pool.resolve(requirements)
                    if expected is None or expected["runtimeId"] != runtime_id:
                        return None
                return resolved
        requirements, _, touch = _normalize_runtime_request(request)
        return self.resolve(requirements, touch=touch)

    def ensure_runtime(
        self,
        request: str | Mapping[str, Any],
    ) -> dict[str, Any]:
        requested_runtime_id = ""
        if isinstance(request, Mapping):
            requested_runtime_id = str(request.get("runtimeId") or "").strip()
            if requested_runtime_id and not _request_contains_requirements(request):
                existing = self.pool.get(
                    requested_runtime_id,
                    touch=bool(request.get("touch", False)),
                )
                if existing is None:
                    raise MaaFWRuntimePoolError(
                        "cannot ensure an unknown runtimeId without requirements"
                    )
                return existing
        requirements, metadata, _ = _normalize_runtime_request(request)
        computed_runtime_id = build_runtime_id(requirements)
        if requested_runtime_id and computed_runtime_id != requested_runtime_id:
            raise MaaFWRuntimePoolError(
                "requested runtimeId does not match the requirement selector: "
                f"requested={requested_runtime_id}, "
                f"computed={computed_runtime_id}"
            )
        return self.ensure(requirements, metadata=metadata)

    def touch(
        self,
        runtime_id: str,
        *,
        at: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self.pool.touch(runtime_id, at=at)

    def pin(self, runtime_id: str, pinned: bool = True) -> dict[str, Any]:
        return self.pool.pin(runtime_id, pinned)

    def add_reference(self, runtime_id: str, reference: str) -> dict[str, Any]:
        return self.pool.add_reference(runtime_id, reference)

    def remove_reference(self, runtime_id: str, reference: str) -> dict[str, Any]:
        return self.pool.remove_reference(runtime_id, reference)

    def set_references(
        self,
        runtime_id: str,
        references: Iterable[str],
    ) -> dict[str, Any]:
        return self.pool.set_references(runtime_id, references)

    def reconcile_references(
        self,
        runtime_id: str,
        references: Iterable[str],
    ) -> dict[str, Any]:
        return self.set_references(runtime_id, references)

    def acquire_lease(
        self,
        runtime_id: str,
        lease_id: str,
        *,
        owner: str = "",
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self.pool.acquire_lease(
            runtime_id,
            lease_id,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )

    def release_lease(self, runtime_id: str, lease_id: str) -> dict[str, Any]:
        return self.pool.release_lease(runtime_id, lease_id)

    def delete(self, runtime_id: str) -> dict[str, Any]:
        return self.pool.delete(runtime_id)

    def gc(
        self,
        *,
        dry_run: bool = True,
        grace_seconds: float = 7 * 24 * 60 * 60,
        keep_latest: int = 1,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self.pool.gc(
            dry_run=dry_run,
            grace_seconds=grace_seconds,
            keep_latest=keep_latest,
            now=now,
        )

    def collect_garbage(
        self,
        *,
        dry_run: bool = True,
        grace_seconds: float = 7 * 24 * 60 * 60,
        keep_latest: int = 1,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self.gc(
            dry_run=dry_run,
            grace_seconds=grace_seconds,
            keep_latest=keep_latest,
            now=now,
        )


def _normalize_runtime_request(
    request: str | Mapping[str, Any],
) -> tuple[list[str], dict[str, Any], bool]:
    if isinstance(request, str):
        value = request.strip()
        if not value:
            raise ValueError("runtime requirement cannot be empty")
        return [value], {}, False
    if not isinstance(request, Mapping):
        raise TypeError("runtime request must be a requirement string or mapping")

    raw_requirements = request.get("requirements", request.get("packages"))
    if raw_requirements is None:
        raw_requirement = request.get(
            "maafwRequirement",
            request.get("requirement"),
        )
        raw_requirements = [raw_requirement] if isinstance(raw_requirement, str) else []
    if isinstance(raw_requirements, str):
        requirements = [raw_requirements]
    elif isinstance(raw_requirements, Mapping):
        raise TypeError("runtime request requirements must be a string or list")
    elif isinstance(raw_requirements, Iterable) and not isinstance(
        raw_requirements,
        (bytes, bytearray),
    ):
        requirements = list(raw_requirements)
        if not all(isinstance(item, str) for item in requirements):
            raise TypeError("runtime request requirements must contain strings")
    else:
        raise TypeError("runtime request requirements must be a string or list")

    metadata_value = request.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    return requirements, metadata, bool(request.get("touch", False))


def _request_contains_requirements(request: Mapping[str, Any]) -> bool:
    return any(
        key in request
        for key in (
            "requirements",
            "packages",
            "maafwRequirement",
            "requirement",
        )
    )
