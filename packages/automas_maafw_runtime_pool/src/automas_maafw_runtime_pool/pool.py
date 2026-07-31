from __future__ import annotations

import copy
import json
import os
import platform
import re
import shutil
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cache import prune_uv_cache

from .identity import (
    RUNTIME_ID_PREFIX,
    build_runtime_identity,
    find_maafw_requirement,
    infer_exact_maafw_version,
    runtime_id_for_identity,
)


POOL_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
POOL_MARKER_NAME = ".auto_mas_maafw_runtime_pool.json"
RUNTIME_MANIFEST_NAME = "manifest.json"
RUNTIME_DIRECTORY_NAME = "runtimes"
STAGING_DIRECTORY_NAME = ".staging"
RUNTIME_ID_RE = re.compile(r"^maafw-runtime-[0-9a-f]{24}$")

RuntimeInstaller = Callable[
    [Path, Sequence[str], dict[str, Any]],
    Mapping[str, Any] | None,
]
RuntimeCachePruner = Callable[..., Mapping[str, Any]]

_LOCKS_GUARD = threading.Lock()
_POOL_LOCKS: dict[str, threading.RLock] = {}


class MaaFWRuntimePoolError(RuntimeError):
    """Raised when a managed MaaFW runtime pool operation is unsafe or invalid."""


class MaaFWRuntimePool:
    def __init__(
        self,
        root: str | Path,
        *,
        installer: RuntimeInstaller | None = None,
        cache_pruner: RuntimeCachePruner | None = prune_uv_cache,
    ) -> None:
        self.root = Path(root).resolve()
        self.runtime_root = self.root / RUNTIME_DIRECTORY_NAME
        self.staging_root = self.root / STAGING_DIRECTORY_NAME
        self.installer = installer
        self.cache_pruner = cache_pruner
        self._lock = _pool_lock(self.root)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._initialize()
            items: list[dict[str, Any]] = []
            for path in self.runtime_root.glob(f"{RUNTIME_ID_PREFIX}*"):
                if not path.is_dir() or path.is_symlink():
                    continue
                try:
                    manifest = self._read_manifest(path.name)
                    item = self._augment_manifest(manifest)
                except MaaFWRuntimePoolError:
                    continue
                items.append(item)
            return sorted(
                items,
                key=lambda item: str(item.get("lastUsedAt") or ""),
                reverse=True,
            )

    def resolve(
        self,
        requirements: Iterable[str],
        *,
        touch: bool = False,
    ) -> dict[str, Any] | None:
        identity = build_runtime_identity(requirements)
        runtime_id = runtime_id_for_identity(identity)
        with self._lock:
            self._initialize()
            runtime_dir = self._runtime_dir(runtime_id)
            if not runtime_dir.is_dir():
                return None
            manifest = self._read_manifest(runtime_id, expected_identity=identity)
            if touch:
                manifest["lastUsedAt"] = _format_time(_utc_now())
                self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def get(
        self,
        runtime_id: str,
        *,
        touch: bool = False,
    ) -> dict[str, Any] | None:
        with self._lock:
            self._initialize()
            runtime_dir = self._runtime_dir(runtime_id)
            if not runtime_dir.is_dir():
                return None
            manifest = self._read_manifest(runtime_id)
            if touch:
                manifest["lastUsedAt"] = _format_time(_utc_now())
                self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def ensure(
        self,
        requirements: Iterable[str],
        *,
        installer: RuntimeInstaller | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = build_runtime_identity(requirements)
        canonical_requirements = tuple(identity["requirements"])
        runtime_id = runtime_id_for_identity(identity)
        install = installer or self.installer
        if install is None:
            raise MaaFWRuntimePoolError(
                "runtime does not exist and no installer was provided"
            )

        with self._lock:
            self._initialize()
            existing = self.resolve(canonical_requirements, touch=True)
            if existing is not None:
                return existing

            stage_dir = self.staging_root / f"{runtime_id}-{uuid.uuid4().hex}"
            self._validate_staging_path(stage_dir, runtime_id)
            stage_dir.mkdir(parents=True, exist_ok=False)
            environment_path = stage_dir / "environment"
            try:
                raw_install_result = install(
                    environment_path,
                    canonical_requirements,
                    copy.deepcopy(identity),
                )
                install_result = dict(raw_install_result or {})
                python_relative = self._resolve_installed_python(
                    stage_dir,
                    environment_path,
                    install_result,
                )
                now = _format_time(_utc_now())
                maafw_requirement = find_maafw_requirement(canonical_requirements)
                maafw_version = _optional_string(
                    install_result.pop("maafwVersion", None)
                    or install_result.pop("maafw_version", None)
                ) or infer_exact_maafw_version(maafw_requirement)
                python_patch_version = _optional_string(
                    install_result.pop("pythonVersion", None)
                    or install_result.pop("python_version", None)
                ) or platform.python_version()
                resolved_requirements = _normalize_string_list(
                    install_result.pop("resolvedRequirements", None)
                    or install_result.pop("resolved_requirements", None),
                    "resolvedRequirements",
                )
                manifest = {
                    "schemaVersion": MANIFEST_SCHEMA_VERSION,
                    "kind": "auto-mas-maafw-runtime",
                    "runtimeId": runtime_id,
                    "identity": identity,
                    "selectorRequirements": list(canonical_requirements),
                    # Compatibility alias retained for existing callers.
                    "requirements": list(canonical_requirements),
                    "resolvedRequirements": resolved_requirements,
                    "maafwRequirement": maafw_requirement,
                    "maafwVersion": maafw_version,
                    "pythonPatchVersion": python_patch_version,
                    "environmentRelativePath": "environment",
                    "pythonRelativePath": python_relative.as_posix(),
                    "createdAt": now,
                    "lastUsedAt": now,
                    "pinned": False,
                    "references": [],
                    "leases": {},
                    "metadata": _json_compatible(metadata or {}),
                    "installerMetadata": _json_compatible(install_result),
                }
                _write_json_atomic(stage_dir / RUNTIME_MANIFEST_NAME, manifest)

                runtime_dir = self._runtime_dir(runtime_id)
                if runtime_dir.exists():
                    self._remove_staging_dir(stage_dir, runtime_id)
                    return self._augment_manifest(
                        self._read_manifest(runtime_id, expected_identity=identity)
                    )
                try:
                    stage_dir.replace(runtime_dir)
                except OSError:
                    if not runtime_dir.is_dir():
                        raise
                    self._remove_staging_dir(stage_dir, runtime_id)
                return self._augment_manifest(
                    self._read_manifest(runtime_id, expected_identity=identity)
                )
            except Exception:
                if stage_dir.exists():
                    self._remove_staging_dir(stage_dir, runtime_id)
                raise

    def touch(
        self,
        runtime_id: str,
        *,
        at: str | datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            manifest = self._read_manifest(runtime_id)
            manifest["lastUsedAt"] = _format_time(_parse_time(at) if at else _utc_now())
            self._prune_expired_leases(manifest, _utc_now())
            self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def pin(self, runtime_id: str, pinned: bool = True) -> dict[str, Any]:
        with self._lock:
            manifest = self._read_manifest(runtime_id)
            manifest["pinned"] = bool(pinned)
            self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def add_reference(self, runtime_id: str, reference: str) -> dict[str, Any]:
        normalized = _required_token(reference, "reference")
        with self._lock:
            manifest = self._read_manifest(runtime_id)
            references = {
                str(item) for item in manifest.get("references", []) if str(item)
            }
            references.add(normalized)
            manifest["references"] = sorted(references)
            self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def remove_reference(self, runtime_id: str, reference: str) -> dict[str, Any]:
        normalized = _required_token(reference, "reference")
        with self._lock:
            manifest = self._read_manifest(runtime_id)
            manifest["references"] = sorted(
                str(item)
                for item in manifest.get("references", [])
                if str(item) and str(item) != normalized
            )
            self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def set_references(
        self,
        runtime_id: str,
        references: Iterable[str],
    ) -> dict[str, Any]:
        """Replace the declared reference set for one runtime.

        References describe durable project/version ownership. Callers should
        periodically reconcile this complete set from their authoritative
        manifests so removed projects cannot leave stale GC blockers behind.
        Active processes belong in ``leases`` instead.
        """

        if isinstance(references, str):
            raw_references: Iterable[str] = (references,)
        else:
            raw_references = references
        normalized = sorted(
            {
                _required_token(reference, "reference")
                for reference in raw_references
            }
        )
        with self._lock:
            manifest = self._read_manifest(runtime_id)
            manifest["references"] = normalized
            self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def acquire_lease(
        self,
        runtime_id: str,
        lease_id: str,
        *,
        owner: str = "",
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        normalized = _required_token(lease_id, "lease_id")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise MaaFWRuntimePoolError("lease ttl_seconds must be positive")
        with self._lock:
            manifest = self._read_manifest(runtime_id)
            now = _utc_now()
            self._prune_expired_leases(manifest, now)
            leases = dict(manifest.get("leases") or {})
            leases[normalized] = {
                "owner": str(owner or ""),
                "acquiredAt": _format_time(now),
                "expiresAt": (
                    _format_time(now + timedelta(seconds=float(ttl_seconds)))
                    if ttl_seconds is not None
                    else None
                ),
            }
            manifest["leases"] = leases
            manifest["lastUsedAt"] = _format_time(now)
            self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def release_lease(self, runtime_id: str, lease_id: str) -> dict[str, Any]:
        normalized = _required_token(lease_id, "lease_id")
        with self._lock:
            manifest = self._read_manifest(runtime_id)
            leases = dict(manifest.get("leases") or {})
            leases.pop(normalized, None)
            manifest["leases"] = leases
            self._write_manifest(runtime_id, manifest)
            return self._augment_manifest(manifest)

    def delete(self, runtime_id: str) -> dict[str, Any]:
        with self._lock:
            manifest = self._read_manifest(runtime_id)
            now = _utc_now()
            self._prune_expired_leases(manifest, now)
            blocked = self._deletion_blockers(manifest, now)
            if blocked:
                self._write_manifest(runtime_id, manifest)
                return {
                    "runtimeId": runtime_id,
                    "deleted": False,
                    "blocked": blocked,
                }
            self._remove_runtime_dir(runtime_id)
            return {"runtimeId": runtime_id, "deleted": True, "blocked": []}

    def gc(
        self,
        *,
        dry_run: bool = True,
        grace_seconds: float = 7 * 24 * 60 * 60,
        keep_latest: int = 1,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        if grace_seconds < 0:
            raise MaaFWRuntimePoolError("gc grace_seconds cannot be negative")
        if keep_latest < 0:
            raise MaaFWRuntimePoolError("gc keep_latest cannot be negative")
        reference_time = _parse_time(now) if now is not None else _utc_now()
        cutoff = reference_time - timedelta(seconds=float(grace_seconds))

        with self._lock:
            runtimes = self.list()
            keep_ids = {
                str(item["runtimeId"])
                for item in runtimes[:keep_latest]
            }
            candidates: list[dict[str, Any]] = []
            kept: list[dict[str, Any]] = []
            deleted: list[str] = []
            errors: list[dict[str, str]] = []
            for item in runtimes:
                runtime_id = str(item["runtimeId"])
                reasons: list[str] = []
                if runtime_id in keep_ids:
                    reasons.append("keep_latest")
                reasons.extend(self._deletion_blockers(item, reference_time))
                last_used = _parse_time(item.get("lastUsedAt"))
                if last_used > cutoff:
                    reasons.append("grace_period")
                if reasons:
                    kept.append({"runtimeId": runtime_id, "reasons": sorted(set(reasons))})
                    continue

                candidate = {
                    "runtimeId": runtime_id,
                    "path": item["path"],
                    "lastUsedAt": item.get("lastUsedAt"),
                    "sizeBytes": item.get("sizeBytes", 0),
                }
                candidates.append(candidate)
                if dry_run:
                    continue
                try:
                    self._remove_runtime_dir(runtime_id)
                    deleted.append(runtime_id)
                except Exception as exc:
                    errors.append({"runtimeId": runtime_id, "error": str(exc)})

            cache_prune = self._prune_cache(dry_run=bool(dry_run))
            return {
                "dryRun": bool(dry_run),
                "graceSeconds": float(grace_seconds),
                "keepLatest": int(keep_latest),
                "candidates": candidates,
                "deleted": deleted,
                "kept": kept,
                "errors": errors,
                "cachePrune": cache_prune,
            }

    def _prune_cache(self, *, dry_run: bool) -> dict[str, Any]:
        if self.cache_pruner is None:
            return {
                "kind": "uv",
                "scope": "pool",
                "dryRun": dry_run,
                "attempted": False,
                "status": "disabled",
                "error": "runtime pool cache pruner is disabled",
            }
        try:
            return dict(self.cache_pruner(self.root, dry_run=dry_run))
        except Exception as exc:
            return {
                "kind": "uv",
                "scope": "pool",
                "dryRun": dry_run,
                "attempted": False,
                "status": "error",
                "error": f"runtime pool cache prune failed: {exc}",
            }

    def _initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        marker_path = self.root / POOL_MARKER_NAME
        if marker_path.is_file():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise MaaFWRuntimePoolError(f"runtime pool marker is invalid: {exc}") from exc
            if marker.get("schemaVersion") != POOL_SCHEMA_VERSION:
                raise MaaFWRuntimePoolError("runtime pool marker version is unsupported")
            return
        _write_json_atomic(
            marker_path,
            {
                "schemaVersion": POOL_SCHEMA_VERSION,
                "kind": "auto-mas-maafw-runtime-pool",
            },
        )

    def _runtime_dir(self, runtime_id: str) -> Path:
        _validate_runtime_id(runtime_id)
        return self.runtime_root / runtime_id

    def _read_manifest(
        self,
        runtime_id: str,
        *,
        expected_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._initialize()
        runtime_dir = self._runtime_dir(runtime_id)
        self._validate_managed_runtime_dir(runtime_dir, runtime_id, require_manifest=False)
        manifest_path = runtime_dir / RUNTIME_MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MaaFWRuntimePoolError(f"runtime manifest not found: {runtime_id}") from exc
        except Exception as exc:
            raise MaaFWRuntimePoolError(f"runtime manifest is invalid: {runtime_id}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise MaaFWRuntimePoolError(f"runtime manifest must be an object: {runtime_id}")
        if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
            raise MaaFWRuntimePoolError(f"runtime manifest version is unsupported: {runtime_id}")
        if manifest.get("kind") != "auto-mas-maafw-runtime":
            raise MaaFWRuntimePoolError(f"runtime manifest kind is invalid: {runtime_id}")
        if manifest.get("runtimeId") != runtime_id:
            raise MaaFWRuntimePoolError(f"runtime manifest identity mismatch: {runtime_id}")
        identity = manifest.get("identity")
        if not isinstance(identity, dict) or runtime_id_for_identity(identity) != runtime_id:
            raise MaaFWRuntimePoolError(
                f"runtime manifest selector identity is invalid: {runtime_id}"
            )
        identity_requirements = _normalize_string_list(
            identity.get("requirements"),
            "identity.requirements",
        )
        selector_requirements = _normalize_string_list(
            manifest.get("selectorRequirements", manifest.get("requirements")),
            "selectorRequirements",
        )
        compatibility_requirements = _normalize_string_list(
            manifest.get("requirements"),
            "requirements",
        )
        if (
            selector_requirements != identity_requirements
            or compatibility_requirements != identity_requirements
        ):
            raise MaaFWRuntimePoolError(
                f"runtime manifest selector requirements mismatch: {runtime_id}"
            )
        _normalize_string_list(
            manifest.get("resolvedRequirements", []),
            "resolvedRequirements",
        )
        if expected_identity is not None and manifest.get("identity") != expected_identity:
            raise MaaFWRuntimePoolError(
                f"runtime requirement selector mismatch: {runtime_id}"
            )
        self._validate_managed_runtime_dir(runtime_dir, runtime_id, require_manifest=True)
        return manifest

    def _write_manifest(self, runtime_id: str, manifest: dict[str, Any]) -> None:
        runtime_dir = self._runtime_dir(runtime_id)
        self._validate_managed_runtime_dir(runtime_dir, runtime_id, require_manifest=True)
        _write_json_atomic(runtime_dir / RUNTIME_MANIFEST_NAME, manifest)

    def _augment_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(manifest)
        runtime_id = str(payload["runtimeId"])
        runtime_dir = self._runtime_dir(runtime_id).resolve()
        environment_relative = Path(str(payload["environmentRelativePath"]))
        python_relative = Path(str(payload["pythonRelativePath"]))
        environment_path = (runtime_dir / environment_relative).resolve()
        python_executable = (runtime_dir / python_relative).resolve()
        if not _is_within(environment_path, runtime_dir) or not _is_within(
            python_executable,
            runtime_dir,
        ):
            raise MaaFWRuntimePoolError(f"runtime manifest contains unsafe paths: {runtime_id}")
        if not environment_path.is_dir() or not python_executable.is_file():
            raise MaaFWRuntimePoolError(
                f"runtime environment is incomplete: {runtime_id}"
            )
        now = _utc_now()
        payload["path"] = str(runtime_dir)
        payload["environmentPath"] = str(environment_path)
        payload["venvPath"] = str(environment_path)
        payload["pythonExecutable"] = str(python_executable)
        selector_requirements = list(
            payload.get("selectorRequirements")
            or payload.get("requirements")
            or []
        )
        payload["selectorRequirements"] = selector_requirements
        payload["resolvedRequirements"] = list(
            payload.get("resolvedRequirements") or []
        )
        payload["packages"] = selector_requirements
        payload["sizeBytes"] = _directory_size(runtime_dir)
        payload["activeLeaseIds"] = self._active_lease_ids(payload, now)
        return payload

    def _resolve_installed_python(
        self,
        stage_dir: Path,
        environment_path: Path,
        install_result: dict[str, Any],
    ) -> Path:
        raw_value = (
            install_result.pop("pythonExecutable", None)
            or install_result.pop("python_executable", None)
        )
        if raw_value:
            candidate = Path(str(raw_value))
            if not candidate.is_absolute():
                candidate = environment_path / candidate
        else:
            candidate = _venv_python(environment_path)
        resolved = candidate.resolve()
        if not _is_within(resolved, stage_dir.resolve()) or not resolved.is_file():
            raise MaaFWRuntimePoolError(
                f"runtime installer did not create a managed Python executable: {candidate}"
            )
        return resolved.relative_to(stage_dir.resolve())

    def _deletion_blockers(
        self,
        manifest: Mapping[str, Any],
        now: datetime,
    ) -> list[str]:
        blockers: list[str] = []
        if bool(manifest.get("pinned")):
            blockers.append("pinned")
        if [item for item in manifest.get("references", []) if str(item)]:
            blockers.append("referenced")
        if self._active_lease_ids(manifest, now):
            blockers.append("leased")
        return blockers

    def _active_lease_ids(
        self,
        manifest: Mapping[str, Any],
        now: datetime,
    ) -> list[str]:
        leases = manifest.get("leases")
        if not isinstance(leases, Mapping):
            return []
        active: list[str] = []
        for lease_id, payload in leases.items():
            if not isinstance(payload, Mapping):
                continue
            expires_at = payload.get("expiresAt")
            if expires_at is None or _parse_time(expires_at) > now:
                active.append(str(lease_id))
        return sorted(active)

    def _prune_expired_leases(self, manifest: dict[str, Any], now: datetime) -> None:
        leases = manifest.get("leases")
        if not isinstance(leases, Mapping):
            manifest["leases"] = {}
            return
        manifest["leases"] = {
            str(lease_id): dict(payload)
            for lease_id, payload in leases.items()
            if isinstance(payload, Mapping)
            and (
                payload.get("expiresAt") is None
                or _parse_time(payload.get("expiresAt")) > now
            )
        }

    def _remove_runtime_dir(self, runtime_id: str) -> None:
        runtime_dir = self._runtime_dir(runtime_id)
        self._validate_managed_runtime_dir(runtime_dir, runtime_id, require_manifest=True)
        shutil.rmtree(runtime_dir)

    def _remove_staging_dir(self, path: Path, runtime_id: str) -> None:
        self._validate_staging_path(path, runtime_id)
        if path.is_symlink():
            raise MaaFWRuntimePoolError(f"refusing to delete staging symlink: {path}")
        shutil.rmtree(path, ignore_errors=True)

    def _validate_staging_path(self, path: Path, runtime_id: str) -> None:
        _validate_runtime_id(runtime_id)
        resolved = path.resolve()
        if resolved.parent != self.staging_root.resolve():
            raise MaaFWRuntimePoolError(f"staging path escapes runtime pool: {path}")
        if not resolved.name.startswith(f"{runtime_id}-"):
            raise MaaFWRuntimePoolError(f"staging path does not match runtime: {path}")

    def _validate_managed_runtime_dir(
        self,
        path: Path,
        runtime_id: str,
        *,
        require_manifest: bool,
    ) -> None:
        _validate_runtime_id(runtime_id)
        resolved = path.resolve()
        if resolved.parent != self.runtime_root.resolve() or resolved.name != runtime_id:
            raise MaaFWRuntimePoolError(f"runtime path escapes managed pool: {path}")
        if path.is_symlink():
            raise MaaFWRuntimePoolError(f"managed runtime cannot be a symlink: {path}")
        if require_manifest and not (resolved / RUNTIME_MANIFEST_NAME).is_file():
            raise MaaFWRuntimePoolError(f"managed runtime has no manifest: {runtime_id}")


def _pool_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root.resolve()))
    with _LOCKS_GUARD:
        return _POOL_LOCKS.setdefault(key, threading.RLock())


def _validate_runtime_id(runtime_id: str) -> None:
    if not RUNTIME_ID_RE.fullmatch(str(runtime_id or "")):
        raise MaaFWRuntimePoolError(f"invalid managed runtime id: {runtime_id}")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_compatible(item) for item in value]
    return str(value)


def _required_token(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise MaaFWRuntimePoolError(f"{field_name} cannot be empty")
    return normalized


def _optional_string(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[Any] = value.splitlines()
    elif isinstance(value, Mapping):
        raise MaaFWRuntimePoolError(f"{field_name} must be a string list")
    elif isinstance(value, Iterable):
        items = value
    else:
        raise MaaFWRuntimePoolError(f"{field_name} must be a string list")
    normalized: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise MaaFWRuntimePoolError(f"{field_name} must contain strings")
        token = item.strip()
        if token:
            normalized.add(token)
    return sorted(normalized, key=str.casefold)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        return datetime.fromtimestamp(0, timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _venv_python(environment_path: Path) -> Path:
    if os.name == "nt":
        return environment_path / "Scripts" / "python.exe"
    return environment_path / "bin" / "python"


def _is_within(path: Path, base_dir: Path) -> bool:
    try:
        path.relative_to(base_dir)
        return True
    except ValueError:
        return False


def _directory_size(path: Path) -> int:
    total = 0
    for directory, directory_names, file_names in os.walk(path):
        directory_path = Path(directory)
        directory_names[:] = [
            name
            for name in directory_names
            if not (directory_path / name).is_symlink()
        ]
        for name in file_names:
            file_path = directory_path / name
            try:
                if not file_path.is_symlink():
                    total += file_path.stat().st_size
            except OSError:
                continue
    return total
