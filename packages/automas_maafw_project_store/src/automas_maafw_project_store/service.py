from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform as host_platform
import re
import shutil
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

import json5


MANIFEST_FILE_NAME = ".auto_mas_maafw_project.json"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_STORE_DIR = Path("data") / "maafw_project_store"

_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

_DEPENDENCY_DIR_NAMES = {
    "agent",
    "agents",
    "lock",
    "locks",
    "plugins",
    "requirements",
}
_DEPENDENCY_FILE_PATTERNS = (
    "requirements*.txt",
    "constraints*.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "environment.yml",
    "environment.yaml",
    "conda-lock.yml",
    "conda-lock.yaml",
    "*.lock",
)

_EXCLUDED_DIRECTORY_REASONS = {
    ".git": "source-control",
    ".github": "source-control",
    ".idea": "editor-state",
    ".vscode": "editor-state",
    "ui": "ui-shell",
    "gui": "ui-shell",
    "frontend": "ui-shell",
    "web": "ui-shell",
    "webui": "ui-shell",
    "electron": "ui-shell",
    "mfaavalonia": "ui-shell",
    "mxu": "ui-shell",
    "mfw": "ui-shell",
    "maapicli": "ui-shell",
    "node_modules": "ui-runtime",
    "runtime": "embedded-runtime",
    "runtimes": "embedded-runtime",
    "python": "embedded-python",
    "python-runtime": "embedded-python",
    "python_runtime": "embedded-python",
    "python-embed": "embedded-python",
    "python_embed": "embedded-python",
    ".venv": "embedded-python",
    "venv": "embedded-python",
    "__pycache__": "cache",
    ".cache": "cache",
    "cache": "cache",
    ".pytest_cache": "cache",
    ".mypy_cache": "cache",
    ".ruff_cache": "cache",
    ".tox": "cache",
    ".nox": "cache",
    ".mas-update": "updater-shell",
    "update": "updater-shell",
    "updates": "updater-shell",
    "updater": "updater-shell",
    "temp": "temporary",
    "tmp": "temporary",
    ".tmp": "temporary",
    "backup": "temporary",
    "backups": "temporary",
    "build": "build-output",
    "dist": "build-output",
}
_EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyo", ".tmp", ".temp", ".log"}
_KNOWN_RUNTIME_FILE_NAMES = {
    "maaframework.dll",
    "maaframework.so",
    "maaframework.dylib",
    "maapicli",
    "maapicli.exe",
    "maatoolkit.dll",
    "python.exe",
    "pythonw.exe",
}
_KNOWN_RUNTIME_STEMS = {
    "maaframework",
    "maatoolkit",
    "maaadbcontrolunit",
    "maahttp",
}
_KNOWN_UI_SHELL_STEMS = {
    "mfaavalonia",
    "mxu",
    "mfw",
    "maapicli",
}
_SHELL_SUFFIXES = {".bat", ".cmd", ".exe", ".ps1", ".sh"}


class MaaFWProjectStoreError(RuntimeError):
    """Raised when an operation would violate the project-store contract."""


@dataclass
class _ProjectionPlan:
    source_root: Path
    interface_base: Path
    source_interface_path: Path
    interface_path: Path
    interface_data: dict[str, Any]
    rewritten_json: dict[Path, dict[str, Any]]
    copied_files: set[Path]
    copied_directories: set[Path]
    excluded_reasons: dict[str, str]
    agent_runtime: list[dict[str, Any]]
    required_python_abi: list[str]
    shared_agent_dependencies_complete: bool
    opaque_agent: bool
    conservative: bool
    warnings: list[str]


@dataclass(frozen=True)
class _ProjectionTargetMode:
    complete: bool
    allow_excluded_root: bool


@dataclass(frozen=True)
class _RequiredProjectionPath:
    path: Path
    label: str
    is_directory: bool
    agent_index: int | None = None
    agent_key: str | None = None
    python_entrypoint: bool = False
    allow_stripped_python_interpreter: bool = False


class MaaFWProjectStoreService:
    """JSON-friendly implementation of ``maafw.project_store.v1``.

    Project payload files are immutable after import. The private manifest is
    management metadata and is updated atomically when runtime references,
    pins, bindings or last-used timestamps change.
    """

    def __init__(self, store_root: str | Path | None = None) -> None:
        requested_root = Path(store_root) if store_root is not None else Path.cwd() / DEFAULT_STORE_DIR
        absolute_root = Path(os.path.abspath(requested_root))
        _assert_existing_chain_has_no_reparse(absolute_root)
        absolute_root.mkdir(parents=True, exist_ok=True)
        _assert_not_reparse(absolute_root)
        self.root = absolute_root.resolve(strict=True)
        self._lock = RLock()
        self._projects_root.mkdir(parents=True, exist_ok=True)
        self._staging_root.mkdir(parents=True, exist_ok=True)

    @property
    def _projects_root(self) -> Path:
        return self.root / "projects"

    @property
    def _staging_root(self) -> Path:
        return self.root / ".staging"

    def import_project(
        self,
        source_path: str | Path,
        project_id: str,
        version: str,
        *,
        runtime_constraint: str | None = None,
        platform: str | None = None,
        arch: str | None = None,
        runtime_binding: dict[str, Any] | None = None,
        reference: str | None = None,
        pinned: bool = False,
        activate: bool = True,
    ) -> dict[str, Any]:
        """Import a local directory or unpacked release as an immutable version."""

        normalized_project_id = _validate_component(project_id, "project_id")
        normalized_version = _validate_component(version, "version")
        source_root = _canonical_source_directory(source_path, self.root)
        interface_base, source_interface_path = _discover_project_interface(source_root)
        interface_data = _read_json_object(source_interface_path)
        plan = _build_projection_plan(source_root, source_interface_path, interface_data)
        source_hash = _calculate_projected_source_hash(source_root, plan.copied_files)

        with self._lock:
            final_dir = self._version_dir(normalized_project_id, normalized_version)
            if final_dir.exists():
                existing = self._load_manifest(normalized_project_id, normalized_version)
                existing_hash = (
                    existing.get("source", {})
                    .get("hash", {})
                    .get("value")
                )
                if existing_hash != source_hash:
                    raise MaaFWProjectStoreError(
                        f"immutable project version already exists with different content: "
                        f"{normalized_project_id}@{normalized_version}"
                    )
                if activate:
                    self._write_current(normalized_project_id, normalized_version)
                return self.resolve_project(
                    normalized_project_id,
                    normalized_version,
                    touch=False,
                )

            final_dir.parent.mkdir(parents=True, exist_ok=True)
            _assert_path_chain_within_root(final_dir.parent, self.root)
            stage_dir = self._staging_root / (
                f"{normalized_project_id}.{normalized_version}.{uuid.uuid4().hex}"
            )
            data_dir = stage_dir / "data"
            imported_at = _format_timestamp()
            try:
                data_dir.mkdir(parents=True, exist_ok=False)
                cleared_hashes = _clear_native_resource_hashes(plan)
                _materialize_projection(plan, data_dir)
                warnings = list(plan.warnings)
                if cleared_hashes:
                    warnings.append(
                        "MaaFW native resource.hash values were cleared after projection; "
                        "recalculate hashes for the filtered tree before native hash validation."
                    )

                constraint = _resolve_runtime_constraint(
                    runtime_constraint,
                    plan.interface_data,
                    source_root,
                    interface_base,
                )
                if constraint is None:
                    warnings.append(
                        "No MaaFW runtime constraint was found in the ProjectInterface "
                        "or requirements files; managed routing must reject this version "
                        "until a constraint is supplied."
                    )
                manifest = {
                    "schemaVersion": MANIFEST_SCHEMA_VERSION,
                    "projectId": normalized_project_id,
                    "version": normalized_version,
                    "createdAt": imported_at,
                    "source": {
                        "path": str(source_root),
                        "projectPath": str(interface_base),
                        "interfacePath": source_interface_path.relative_to(source_root).as_posix(),
                        "version": _optional_string(plan.interface_data.get("version")),
                        "hash": {
                            "algorithm": "sha256",
                            "scope": "projected-source",
                            "value": source_hash,
                        },
                    },
                    "projectInterface": {
                        "path": plan.interface_path.as_posix(),
                        "resourceHashCleared": bool(cleared_hashes),
                        "clearedResources": cleared_hashes,
                    },
                    "runtimeConstraint": constraint,
                    "requiredPythonAbi": plan.required_python_abi,
                    "runtime": {
                        "constraint": constraint,
                        "platform": _optional_string(platform) or sys.platform,
                        "arch": _optional_string(arch) or host_platform.machine() or "unknown",
                        "agent": plan.agent_runtime,
                        "requiredPythonAbi": plan.required_python_abi,
                        "sharedAgentDependenciesComplete": (
                            plan.shared_agent_dependencies_complete
                        ),
                        "binding": _json_clone(runtime_binding),
                        "references": [reference.strip()] if reference and reference.strip() else [],
                        "leases": [],
                        "pinned": bool(pinned),
                        "lastUsedAt": imported_at if activate else None,
                    },
                    "projection": {
                        "copied": sorted(
                            _projection_output_path(plan, path).as_posix()
                            for path in plan.copied_files
                        ),
                        "copiedFromSource": sorted(
                            path.as_posix() for path in plan.copied_files
                        ),
                        "copiedDirectories": sorted(
                            {
                                _projection_output_path(plan, path).as_posix()
                                for path in plan.copied_directories
                                if path != Path(".")
                                and _projection_output_path(plan, path) != Path(".")
                            }
                        ),
                        "excluded": sorted(plan.excluded_reasons),
                        "excludedReasons": dict(sorted(plan.excluded_reasons.items())),
                    },
                    "flags": {
                        "opaqueAgent": plan.opaque_agent,
                        "conservative": plan.conservative,
                    },
                    "warnings": warnings,
                }
                _write_json(data_dir / MANIFEST_FILE_NAME, manifest)
                stage_dir.rename(final_dir)
            except FileExistsError as exc:
                if stage_dir.exists():
                    _safe_remove_tree(stage_dir, self.root)
                raise MaaFWProjectStoreError(
                    f"project version was created concurrently: "
                    f"{normalized_project_id}@{normalized_version}"
                ) from exc
            except Exception:
                if stage_dir.exists():
                    _safe_remove_tree(stage_dir, self.root)
                raise

            if activate:
                self._write_current(normalized_project_id, normalized_version)
            return self.resolve_project(
                normalized_project_id,
                normalized_version,
                touch=False,
            )

    def update_project(
        self,
        source_path: str | Path,
        project_id: str,
        version: str,
        *,
        runtime_constraint: str | None = None,
        platform: str | None = None,
        arch: str | None = None,
        runtime_binding: dict[str, Any] | None = None,
        reference: str | None = None,
        pinned: bool = False,
        activate: bool = True,
    ) -> dict[str, Any]:
        """Import an update as a new version; existing versions are never patched."""

        return self.import_project(
            source_path,
            project_id,
            version,
            runtime_constraint=runtime_constraint,
            platform=platform,
            arch=arch,
            runtime_binding=runtime_binding,
            reference=reference,
            pinned=pinned,
            activate=activate,
        )

    def resolve_project(
        self,
        project_id: str,
        version: str | None = None,
        *,
        touch: bool = True,
    ) -> dict[str, Any]:
        """Resolve a version to a runnable project directory.

        If ``version`` is omitted, the current pointer is preferred and the
        newest imported version is used as a tolerant fallback.
        """

        normalized_project_id = _validate_component(project_id, "project_id")
        with self._lock:
            resolved_version = self._resolve_version(normalized_project_id, version)
            manifest = self._load_manifest(normalized_project_id, resolved_version)
            if touch:
                manifest["runtime"]["lastUsedAt"] = _format_timestamp()
                self._write_manifest(normalized_project_id, resolved_version, manifest)
            return self._build_resolved_record(manifest)

    def list_versions(self, project_id: str) -> list[dict[str, Any]]:
        normalized_project_id = _validate_component(project_id, "project_id")
        with self._lock:
            current = self._read_current(normalized_project_id)
            records: list[dict[str, Any]] = []
            for version in self._iter_versions(normalized_project_id):
                manifest = self._load_manifest(normalized_project_id, version)
                record = self._build_resolved_record(manifest)
                runtime = manifest.get("runtime", {})
                record.update(
                    {
                        "current": version == current,
                        "pinned": bool(runtime.get("pinned")),
                        "references": list(runtime.get("references") or []),
                        "lastUsedAt": runtime.get("lastUsedAt"),
                    }
                )
                records.append(record)
            records.sort(
                key=lambda item: (
                    str(item["manifest"].get("createdAt") or ""),
                    str(item["version"]),
                ),
                reverse=True,
            )
            return records

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            result: list[dict[str, Any]] = []
            for project_id in self._iter_projects():
                versions = self.list_versions(project_id)
                result.append(
                    {
                        "projectId": project_id,
                        "currentVersion": self._read_current(project_id),
                        "versionCount": len(versions),
                        "versions": [item["version"] for item in versions],
                    }
                )
            return result

    def switch_version(self, project_id: str, version: str) -> dict[str, Any]:
        normalized_project_id = _validate_component(project_id, "project_id")
        normalized_version = _validate_component(version, "version")
        with self._lock:
            self._load_manifest(normalized_project_id, normalized_version)
            self._write_current(normalized_project_id, normalized_version)
            return self.resolve_project(
                normalized_project_id,
                normalized_version,
                touch=True,
            )

    def bind_runtime(
        self,
        project_id: str,
        version: str | None = None,
        *,
        binding: dict[str, Any] | None = None,
        reference: str | None = None,
        pinned: bool | None = None,
        touch: bool = True,
    ) -> dict[str, Any]:
        normalized_project_id = _validate_component(project_id, "project_id")
        with self._lock:
            resolved_version = self._resolve_version(normalized_project_id, version)
            manifest = self._load_manifest(normalized_project_id, resolved_version)
            runtime = manifest.setdefault("runtime", {})
            if binding is not None:
                runtime["binding"] = _json_clone(binding)
            if reference is not None:
                normalized_reference = reference.strip()
                if not normalized_reference:
                    raise MaaFWProjectStoreError("reference cannot be empty")
                references = [
                    item
                    for item in runtime.get("references") or []
                    if isinstance(item, str)
                ]
                if normalized_reference not in references:
                    references.append(normalized_reference)
                runtime["references"] = references
            if pinned is not None:
                runtime["pinned"] = bool(pinned)
            if touch:
                runtime["lastUsedAt"] = _format_timestamp()
            self._write_manifest(normalized_project_id, resolved_version, manifest)
            return self._build_resolved_record(manifest)

    def release_runtime(
        self,
        project_id: str,
        version: str | None = None,
        *,
        reference: str | None = None,
        clear_binding: bool = False,
        unpin: bool = False,
    ) -> dict[str, Any]:
        normalized_project_id = _validate_component(project_id, "project_id")
        with self._lock:
            resolved_version = self._resolve_version(normalized_project_id, version)
            manifest = self._load_manifest(normalized_project_id, resolved_version)
            runtime = manifest.setdefault("runtime", {})
            if reference is not None:
                runtime["references"] = [
                    item
                    for item in runtime.get("references") or []
                    if isinstance(item, str) and item != reference
                ]
            if clear_binding:
                runtime["binding"] = None
            if unpin:
                runtime["pinned"] = False
            self._write_manifest(normalized_project_id, resolved_version, manifest)
            return self._build_resolved_record(manifest)

    def set_references(
        self,
        project_id: str,
        version: str | None,
        references: list[str],
        *,
        touch: bool = False,
    ) -> dict[str, Any]:
        """Replace the complete reference set during host reconciliation."""

        if not isinstance(references, list):
            raise MaaFWProjectStoreError("references must be a string array")
        normalized_references: list[str] = []
        for reference in references:
            if not isinstance(reference, str) or not reference.strip():
                raise MaaFWProjectStoreError(
                    "references must contain non-empty strings"
                )
            normalized = reference.strip()
            if normalized not in normalized_references:
                normalized_references.append(normalized)

        normalized_project_id = _validate_component(project_id, "project_id")
        with self._lock:
            resolved_version = self._resolve_version(normalized_project_id, version)
            manifest = self._load_manifest(normalized_project_id, resolved_version)
            runtime = manifest.setdefault("runtime", {})
            runtime["references"] = normalized_references
            if touch:
                runtime["lastUsedAt"] = _format_timestamp()
            self._write_manifest(normalized_project_id, resolved_version, manifest)
            return self._build_resolved_record(manifest)

    def acquire_lease(
        self,
        project_id: str,
        version: str | None = None,
        *,
        owner: str,
        ttl_seconds: float = 5 * 60,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Acquire a renewable lease protecting a resolved dataPath from GC."""

        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise MaaFWProjectStoreError("lease owner cannot be empty")
        if ttl_seconds <= 0:
            raise MaaFWProjectStoreError("lease ttl_seconds must be positive")
        normalized_lease_id = str(lease_id or uuid.uuid4().hex).strip()
        if not normalized_lease_id:
            raise MaaFWProjectStoreError("lease_id cannot be empty")

        normalized_project_id = _validate_component(project_id, "project_id")
        with self._lock:
            resolved_version = self._resolve_version(normalized_project_id, version)
            manifest = self._load_manifest(normalized_project_id, resolved_version)
            runtime = manifest.setdefault("runtime", {})
            now_value = time.time()
            leases = [
                item
                for item in _active_leases(runtime.get("leases"), now_value)
                if item.get("leaseId") != normalized_lease_id
            ]
            lease = {
                "leaseId": normalized_lease_id,
                "owner": normalized_owner,
                "acquiredAt": _format_timestamp(now_value),
                "expiresAt": _format_timestamp(now_value + float(ttl_seconds)),
            }
            leases.append(lease)
            runtime["leases"] = leases
            runtime["lastUsedAt"] = _format_timestamp(now_value)
            self._write_manifest(normalized_project_id, resolved_version, manifest)
            result = self._build_resolved_record(manifest)
            result["lease"] = lease
            return result

    def release_lease(
        self,
        project_id: str,
        version: str | None = None,
        *,
        lease_id: str,
    ) -> dict[str, Any]:
        normalized_lease_id = str(lease_id or "").strip()
        if not normalized_lease_id:
            raise MaaFWProjectStoreError("lease_id cannot be empty")
        normalized_project_id = _validate_component(project_id, "project_id")
        with self._lock:
            resolved_version = self._resolve_version(normalized_project_id, version)
            manifest = self._load_manifest(normalized_project_id, resolved_version)
            runtime = manifest.setdefault("runtime", {})
            runtime["leases"] = [
                item
                for item in _active_leases(runtime.get("leases"), time.time())
                if item.get("leaseId") != normalized_lease_id
            ]
            self._write_manifest(normalized_project_id, resolved_version, manifest)
            return self._build_resolved_record(manifest)

    def delete_version(self, project_id: str, version: str) -> dict[str, Any]:
        normalized_project_id = _validate_component(project_id, "project_id")
        normalized_version = _validate_component(version, "version")
        with self._lock:
            manifest = self._load_manifest(normalized_project_id, normalized_version)
            protection = self._protection_reasons(
                normalized_project_id,
                normalized_version,
                manifest,
            )
            if protection:
                raise MaaFWProjectStoreError(
                    f"project version is protected: {normalized_project_id}@{normalized_version} "
                    f"({', '.join(protection)})"
                )
            version_dir = self._version_dir(normalized_project_id, normalized_version)
            reclaimed_bytes = _tree_size(version_dir)
            _safe_remove_tree(version_dir, self.root)
            self._prune_empty_project_directories(normalized_project_id)
            return {
                "deleted": True,
                "projectId": normalized_project_id,
                "version": normalized_version,
                "reclaimedBytes": reclaimed_bytes,
            }

    def collect_garbage(
        self,
        *,
        project_id: str | None = None,
        dry_run: bool = True,
        grace_seconds: float = 24 * 60 * 60,
        keep_latest: int = 1,
        now: float | None = None,
    ) -> dict[str, Any]:
        if grace_seconds < 0:
            raise MaaFWProjectStoreError("grace_seconds cannot be negative")
        if keep_latest < 0:
            raise MaaFWProjectStoreError("keep_latest cannot be negative")
        normalized_project_id = (
            _validate_component(project_id, "project_id")
            if project_id is not None
            else None
        )
        current_time = float(now if now is not None else time.time())

        with self._lock:
            project_ids = (
                [normalized_project_id]
                if normalized_project_id is not None
                else self._iter_projects()
            )
            candidates: list[dict[str, Any]] = []
            kept: list[dict[str, Any]] = []
            for candidate_project_id in project_ids:
                versions = self.list_versions(candidate_project_id)
                latest_versions = {
                    item["version"] for item in versions[:keep_latest]
                }
                for item in versions:
                    manifest = item["manifest"]
                    version_value = str(item["version"])
                    reasons = self._protection_reasons(
                        candidate_project_id,
                        version_value,
                        manifest,
                        now=current_time,
                    )
                    if not dry_run:
                        runtime = manifest.setdefault("runtime", {})
                        active_leases = _active_leases(
                            runtime.get("leases"),
                            current_time,
                        )
                        if active_leases != runtime.get("leases"):
                            runtime["leases"] = active_leases
                            self._write_manifest(
                                candidate_project_id,
                                version_value,
                                manifest,
                            )
                    if version_value in latest_versions:
                        reasons.append("keep-latest")
                    age_anchor = (
                        manifest.get("runtime", {}).get("lastUsedAt")
                        or manifest.get("createdAt")
                    )
                    age_seconds = max(
                        0.0,
                        current_time - _parse_timestamp(age_anchor),
                    )
                    if age_seconds < grace_seconds:
                        reasons.append("grace-period")

                    summary = {
                        "projectId": candidate_project_id,
                        "version": version_value,
                        "dataPath": item["dataPath"],
                        "ageSeconds": age_seconds,
                    }
                    if reasons:
                        summary["reasons"] = sorted(set(reasons))
                        kept.append(summary)
                    else:
                        summary["bytes"] = _tree_size(
                            self._version_dir(candidate_project_id, version_value)
                        )
                        candidates.append(summary)

            deleted: list[dict[str, Any]] = []
            reclaimed_bytes = 0
            if not dry_run:
                for candidate in candidates:
                    version_dir = self._version_dir(
                        candidate["projectId"],
                        candidate["version"],
                    )
                    _safe_remove_tree(version_dir, self.root)
                    reclaimed_bytes += int(candidate["bytes"])
                    deleted.append(dict(candidate))
                for candidate_project_id in project_ids:
                    self._prune_empty_project_directories(candidate_project_id)

            return {
                "dryRun": bool(dry_run),
                "graceSeconds": grace_seconds,
                "keepLatest": keep_latest,
                "candidates": candidates,
                "deleted": deleted,
                "kept": kept,
                "reclaimedBytes": reclaimed_bytes,
            }

    def _build_resolved_record(self, manifest: dict[str, Any]) -> dict[str, Any]:
        project_id = str(manifest["projectId"])
        version = str(manifest["version"])
        data_path = self._version_dir(project_id, version) / "data"
        interface_relative = Path(str(manifest["projectInterface"]["path"]))
        interface_path = (data_path / interface_relative).resolve(strict=True)
        _assert_within(interface_path, data_path)
        return {
            "dataPath": str(data_path.resolve(strict=True)),
            "projectId": project_id,
            "version": version,
            "runtimeConstraint": manifest.get("runtime", {}).get("constraint"),
            "manifestPath": str((data_path / MANIFEST_FILE_NAME).resolve(strict=True)),
            "projectInterfacePath": str(interface_path),
            "manifest": _json_clone(manifest),
        }

    def _project_dir(self, project_id: str) -> Path:
        return self._projects_root / _validate_component(project_id, "project_id")

    def _version_dir(self, project_id: str, version: str) -> Path:
        path = self._project_dir(project_id) / "versions" / _validate_component(version, "version")
        _assert_path_chain_within_root(path, self.root)
        return path

    def _manifest_path(self, project_id: str, version: str) -> Path:
        return self._version_dir(project_id, version) / "data" / MANIFEST_FILE_NAME

    def _load_manifest(self, project_id: str, version: str) -> dict[str, Any]:
        manifest_path = self._manifest_path(project_id, version)
        if not manifest_path.is_file():
            raise MaaFWProjectStoreError(
                f"project version does not exist: {project_id}@{version}"
            )
        _assert_path_chain_within_root(manifest_path, self.root)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise MaaFWProjectStoreError(
                f"cannot read project manifest: {project_id}@{version}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise MaaFWProjectStoreError("project manifest must be a JSON object")
        if payload.get("projectId") != project_id or payload.get("version") != version:
            raise MaaFWProjectStoreError(
                f"project manifest identity mismatch: {project_id}@{version}"
            )
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            raise MaaFWProjectStoreError("project manifest runtime must be a JSON object")
        return payload

    def _write_manifest(
        self,
        project_id: str,
        version: str,
        manifest: dict[str, Any],
    ) -> None:
        _write_json_atomic(
            self._manifest_path(project_id, version),
            manifest,
            self.root,
        )

    def _resolve_version(self, project_id: str, version: str | None) -> str:
        if version is not None:
            normalized = _validate_component(version, "version")
            self._load_manifest(project_id, normalized)
            return normalized
        current = self._read_current(project_id)
        if current is not None:
            return current
        versions = self._iter_versions(project_id)
        if not versions:
            raise MaaFWProjectStoreError(f"project does not exist: {project_id}")
        manifests = [(item, self._load_manifest(project_id, item)) for item in versions]
        manifests.sort(
            key=lambda item: str(item[1].get("createdAt") or ""),
            reverse=True,
        )
        return manifests[0][0]

    def _current_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "current.json"

    def _read_current(self, project_id: str) -> str | None:
        current_path = self._current_path(project_id)
        if not current_path.is_file():
            return None
        _assert_path_chain_within_root(current_path, self.root)
        try:
            payload = json.loads(current_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise MaaFWProjectStoreError(
                f"cannot read current pointer for {project_id}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
            raise MaaFWProjectStoreError(f"invalid current pointer for {project_id}")
        version = _validate_component(payload["version"], "version")
        if not self._manifest_path(project_id, version).is_file():
            raise MaaFWProjectStoreError(
                f"current pointer references missing version: {project_id}@{version}"
            )
        return version

    def _write_current(self, project_id: str, version: str) -> None:
        self._load_manifest(project_id, version)
        current_path = self._current_path(project_id)
        current_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            current_path,
            {
                "projectId": project_id,
                "version": version,
                "updatedAt": _format_timestamp(),
            },
            self.root,
        )

    def _iter_projects(self) -> list[str]:
        if not self._projects_root.exists():
            return []
        result: list[str] = []
        for child in self._projects_root.iterdir():
            _assert_not_reparse(child)
            if not child.is_dir():
                continue
            result.append(_validate_component(child.name, "project_id"))
        return sorted(result)

    def _iter_versions(self, project_id: str) -> list[str]:
        versions_root = self._project_dir(project_id) / "versions"
        if not versions_root.exists():
            return []
        _assert_path_chain_within_root(versions_root, self.root)
        result: list[str] = []
        for child in versions_root.iterdir():
            _assert_not_reparse(child)
            if not child.is_dir():
                continue
            version = _validate_component(child.name, "version")
            if self._manifest_path(project_id, version).is_file():
                result.append(version)
        return sorted(result)

    def _protection_reasons(
        self,
        project_id: str,
        version: str,
        manifest: dict[str, Any],
        *,
        now: float | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        if self._read_current(project_id) == version:
            reasons.append("current")
        runtime = manifest.get("runtime", {})
        if runtime.get("pinned"):
            reasons.append("pinned")
        if runtime.get("references"):
            reasons.append("referenced")
        if _active_leases(runtime.get("leases"), now if now is not None else time.time()):
            reasons.append("leased")
        return reasons

    def _prune_empty_project_directories(self, project_id: str) -> None:
        project_dir = self._project_dir(project_id)
        versions_dir = project_dir / "versions"
        if versions_dir.is_dir() and not any(versions_dir.iterdir()):
            versions_dir.rmdir()
        current_path = project_dir / "current.json"
        if project_dir.is_dir() and not current_path.exists() and not any(project_dir.iterdir()):
            project_dir.rmdir()


def _build_projection_plan(
    source_root: Path,
    source_interface_path: Path,
    interface_data: dict[str, Any],
) -> _ProjectionPlan:
    interface_base = source_interface_path.parent
    source_interface_relative = source_interface_path.relative_to(source_root)
    interface_path = _output_path_for_source(
        source_interface_relative,
        source_root,
        interface_base,
    )
    all_directories, all_files = _scan_safe_tree(source_root)
    target_modes: dict[Path, _ProjectionTargetMode] = {}
    required_paths: list[_RequiredProjectionPath] = []
    warnings: list[str] = []

    def add_target(
        relative_path: Path,
        *,
        complete: bool,
        required: bool,
        label: str,
        allow_excluded_root: bool = False,
        required_path: Path | None = None,
        agent_index: int | None = None,
        agent_key: str | None = None,
        python_entrypoint: bool = False,
        allow_stripped_python_interpreter: bool = False,
    ) -> None:
        normalized = _normalize_relative_path(
            relative_path.as_posix(),
            label,
            allow_root=True,
        )
        absolute = (source_root / normalized).resolve(strict=False)
        _assert_within(absolute, source_root)
        if not absolute.exists():
            if required:
                raise MaaFWProjectStoreError(f"required {label} path does not exist: {relative_path}")
            warnings.append(f"optional {label} path was not found and was not copied: {relative_path}")
            return
        previous = target_modes.get(
            normalized,
            _ProjectionTargetMode(complete=False, allow_excluded_root=False),
        )
        target_modes[normalized] = _ProjectionTargetMode(
            complete=bool(previous.complete or complete),
            allow_excluded_root=bool(
                previous.allow_excluded_root or allow_excluded_root
            ),
        )
        if required:
            exact_path = _normalize_relative_path(
                (required_path or normalized).as_posix(),
                f"required {label}",
                allow_root=True,
            )
            exact_absolute = (source_root / exact_path).resolve(strict=False)
            _assert_within(exact_absolute, source_root)
            if not exact_absolute.exists():
                raise MaaFWProjectStoreError(
                    f"required {label} path does not exist: {exact_path.as_posix()}"
                )
            required_paths.append(
                _RequiredProjectionPath(
                    path=exact_path,
                    label=label,
                    is_directory=exact_absolute.is_dir(),
                    agent_index=agent_index,
                    agent_key=agent_key,
                    python_entrypoint=python_entrypoint,
                    allow_stripped_python_interpreter=(
                        allow_stripped_python_interpreter
                    ),
                )
            )

    rewritten_json = _collect_and_rewrite_interfaces(
        source_root,
        interface_base,
        source_interface_path,
        interface_data,
        add_target,
    )

    for dependency_base in {source_root, interface_base}:
        for entry in dependency_base.iterdir():
            if entry.name.casefold() in _DEPENDENCY_DIR_NAMES and entry.is_dir():
                add_target(
                    entry.relative_to(source_root),
                    complete=False,
                    required=False,
                    label="agent dependency directory",
                )
            if entry.is_file() and any(
                fnmatch.fnmatchcase(entry.name, pattern)
                for pattern in _DEPENDENCY_FILE_PATTERNS
            ):
                add_target(
                    entry.relative_to(source_root),
                    complete=False,
                    required=False,
                    label="agent dependency file",
                )

    agent_runtime, agent_targets, opaque_agent = _inspect_agents(
        interface_data.get("agent"),
        interface_base,
        source_root,
        source_interface_relative.as_posix(),
    )
    for agent_target in agent_targets:
        add_target(
            agent_target,
            complete=False,
            required=False,
            label="agent dependency",
        )

    conservative = opaque_agent
    if conservative:
        target_modes[Path(".")] = _ProjectionTargetMode(
            complete=False,
            allow_excluded_root=False,
        )
        warnings.append(
            "Custom, Command or opaque agent detected; projection used conservative retention."
        )

    copied_files: set[Path] = set()
    copied_directories: set[Path] = {Path(".")}
    for target, mode in target_modes.items():
        target_absolute = source_root / target
        if target_absolute.is_file():
            if _target_exclusion_reason(
                target,
                target=target,
                mode=mode,
                target_is_directory=False,
            ) is None:
                copied_files.add(target)
                copied_directories.update(_relative_parents(target))
            continue

        for directory in all_directories:
            if not _is_relative_to(directory, target):
                continue
            if _target_exclusion_reason(
                directory,
                target=target,
                mode=mode,
                target_is_directory=True,
                is_directory=True,
            ) is None:
                copied_directories.add(directory)
                copied_directories.update(_relative_parents(directory))
        for file_path in all_files:
            if not _is_relative_to(file_path, target):
                continue
            if _target_exclusion_reason(
                file_path,
                target=target,
                mode=mode,
                target_is_directory=True,
            ) is None:
                copied_files.add(file_path)
                copied_directories.update(_relative_parents(file_path))

    _validate_required_projection_paths(
        required_paths,
        copied_files,
        copied_directories,
        agent_runtime,
        warnings,
    )
    for agent in agent_runtime:
        agent.pop("_projectionKey", None)

    excluded_reasons: dict[str, str] = {}
    for file_path in all_files - copied_files:
        excluded_reasons[file_path.as_posix()] = (
            _exclusion_reason(file_path) or "not-required-by-runtime-projection"
        )

    reserved_files = {
        path
        for path in copied_files
        if _projection_output_path_from_roots(
            path,
            source_root,
            interface_base,
        ).name == MANIFEST_FILE_NAME
    }
    for reserved in reserved_files:
        copied_files.remove(reserved)
        excluded_reasons[reserved.as_posix()] = "reserved-project-store-manifest"

    output_owners: dict[Path, Path] = {}
    for source_relative in copied_files:
        output_relative = _projection_output_path_from_roots(
            source_relative,
            source_root,
            interface_base,
        )
        owner = output_owners.setdefault(output_relative, source_relative)
        if owner != source_relative:
            raise MaaFWProjectStoreError(
                "projection path collision after promoting assets/: "
                f"{owner.as_posix()} and {source_relative.as_posix()} -> "
                f"{output_relative.as_posix()}"
            )

    required_python_abi = _detect_python_abi_tags(source_root, copied_files)
    for agent in agent_runtime:
        agent["abiTags"] = (
            list(required_python_abi)
            if agent.get("classification") == "python"
            else []
        )
    shared_agent_dependencies_complete = _shared_agent_dependencies_complete(
        source_root,
        interface_base,
        copied_files,
        agent_runtime,
    )

    return _ProjectionPlan(
        source_root=source_root,
        interface_base=interface_base,
        source_interface_path=source_interface_relative,
        interface_path=interface_path,
        interface_data=rewritten_json[source_interface_relative],
        rewritten_json=rewritten_json,
        copied_files=copied_files,
        copied_directories=copied_directories,
        excluded_reasons=excluded_reasons,
        agent_runtime=agent_runtime,
        required_python_abi=required_python_abi,
        shared_agent_dependencies_complete=shared_agent_dependencies_complete,
        opaque_agent=opaque_agent,
        conservative=conservative,
        warnings=warnings,
    )


def _target_exclusion_reason(
    path: Path,
    *,
    target: Path,
    mode: _ProjectionTargetMode,
    target_is_directory: bool,
    is_directory: bool = False,
) -> str | None:
    """Apply exclusions relative to an explicitly complete resource root.

    A ProjectInterface may legitimately name a resource directory ``runtime``,
    ``python`` or ``venv``.  Those names are unsafe as release-level defaults,
    but an explicit complete resource/attach-resource declaration is stronger
    evidence.  Only that target's prefix is ignored; exclusions inside it still
    remove known MaaFramework, Python and UI payloads.
    """

    if (
        not mode.complete
        or not mode.allow_excluded_root
        or target == Path(".")
    ):
        return _exclusion_reason(path, is_directory=is_directory)
    if target_is_directory:
        scoped_path = path.relative_to(target)
        return _exclusion_reason(scoped_path, is_directory=is_directory)
    return _exclusion_reason(Path(path.name), is_directory=is_directory)


def _validate_required_projection_paths(
    required_paths: Iterable[_RequiredProjectionPath],
    copied_files: set[Path],
    copied_directories: set[Path],
    agent_runtime: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    requirements = list(required_paths)
    python_entrypoints: dict[str, set[Path]] = {}
    for requirement in requirements:
        if (
            requirement.agent_key is not None
            and requirement.python_entrypoint
            and requirement.path in copied_files
        ):
            python_entrypoints.setdefault(requirement.agent_key, set()).add(
                requirement.path
            )

    runtime_agents = {
        str(agent.get("_projectionKey")): agent
        for agent in agent_runtime
        if agent.get("_projectionKey") is not None
    }

    for requirement in requirements:
        retained = (
            requirement.path in copied_directories
            if requirement.is_directory
            else requirement.path in copied_files
        )
        if retained:
            continue
        reason = _exclusion_reason(
            requirement.path,
            is_directory=requirement.is_directory,
        )
        if (
            requirement.allow_stripped_python_interpreter
            and not requirement.is_directory
            and _is_python_interpreter_path(requirement.path)
            and reason in {"embedded-python", "embedded-runtime"}
            and requirement.agent_index is not None
            and requirement.agent_key is not None
            and python_entrypoints.get(requirement.agent_key)
            and runtime_agents.get(requirement.agent_key, {}).get("classification")
            == "python"
        ):
            entrypoints = sorted(
                path.as_posix()
                for path in python_entrypoints[requirement.agent_key]
            )
            agent = runtime_agents[requirement.agent_key]
            agent["strippedInterpreter"] = {
                "sourcePath": requirement.path.as_posix(),
                "reason": reason,
                "retainedEntrypoints": entrypoints,
            }
            warnings.append(
                f"{requirement.label} embedded Python interpreter was stripped "
                f"({requirement.path.as_posix()}); retained Python entrypoint(s): "
                f"{', '.join(entrypoints)}."
            )
            continue
        raise MaaFWProjectStoreError(
            f"required {requirement.label} path was excluded by the "
            f"resource-only projection: {requirement.path.as_posix()} "
            f"({reason or 'not-retained'})"
        )


def _shared_agent_dependencies_complete(
    source_root: Path,
    interface_base: Path,
    copied_files: Iterable[Path],
    agent_runtime: Iterable[dict[str, Any]],
) -> bool:
    """Return true only for the runner's flat, root-requirements model."""

    if not any(agent.get("classification") == "python" for agent in agent_runtime):
        return False

    projected_files: dict[Path, Path] = {}
    for source_relative in copied_files:
        output_relative = _projection_output_path_from_roots(
            source_relative,
            source_root,
            interface_base,
        )
        projected_files[output_relative] = source_relative

    requirements_source = projected_files.get(Path("requirements.txt"))
    if requirements_source is None:
        return False

    for output_relative in projected_files:
        if output_relative == Path("requirements.txt"):
            continue
        name = output_relative.name.casefold()
        if any(
            fnmatch.fnmatchcase(name, pattern.casefold())
            for pattern in _DEPENDENCY_FILE_PATTERNS
        ):
            return False

    requirements_path = (source_root / requirements_source).resolve(strict=True)
    _assert_within(requirements_path, source_root)
    _assert_not_reparse(requirements_path)
    try:
        lines = requirements_path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return False
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if (
            line.startswith("-")
            or line.startswith((".", "/", "\\"))
            or line.endswith("\\")
            or re.search(r"\s--(?:hash|config-settings|global-option)\b", line)
            or re.search(r"@\s*(?:file:|https?://|git\+)", line, re.IGNORECASE)
            or line.casefold().startswith(("file:", "http://", "https://", "git+"))
            or Path(line.split(";", 1)[0].strip()).suffix.casefold()
            in {".whl", ".zip", ".gz", ".bz2"}
        ):
            return False
    return True


def _is_python_interpreter_path(path: Path) -> bool:
    return path.name.casefold() in {
        "python",
        "python.exe",
        "python3",
        "python3.exe",
        "pythonw",
        "pythonw.exe",
        "py",
        "py.exe",
    }


def _collect_and_rewrite_interfaces(
    source_root: Path,
    interface_base: Path,
    root_interface_path: Path,
    root_data: dict[str, Any],
    add_target: Any,
) -> dict[Path, dict[str, Any]]:
    rewritten: dict[Path, dict[str, Any]] = {}
    visiting: set[Path] = set()

    def visit(interface_file: Path, data: dict[str, Any]) -> None:
        source_relative = interface_file.relative_to(source_root)
        if source_relative in rewritten:
            return
        if source_relative in visiting:
            raise MaaFWProjectStoreError(
                f"cyclic ProjectInterface import: {source_relative.as_posix()}"
            )
        visiting.add(source_relative)
        add_target(
            source_relative,
            complete=True,
            required=True,
            label="ProjectInterface",
        )
        projected = _json_clone(data)

        raw_imports = data.get("import")
        if raw_imports is None:
            projected_imports: list[str] | None = None
        else:
            if not isinstance(raw_imports, list) or not all(
                isinstance(item, str) and item.strip() for item in raw_imports
            ):
                raise MaaFWProjectStoreError(
                    "ProjectInterface import must be a string array"
                )
            projected_imports = []
            for raw_import in raw_imports:
                imported_path, projected_path = _resolve_and_project_local_path(
                    raw_import,
                    interface_base,
                    source_root,
                    "ProjectInterface import",
                    required=True,
                )
                if not imported_path.is_file():
                    raise MaaFWProjectStoreError(
                        f"ProjectInterface import is not a file: {raw_import}"
                    )
                projected_imports.append(_format_project_path(projected_path))
                visit(imported_path, _read_json_object(imported_path))
            projected["import"] = projected_imports

        _rewrite_resource_paths(
            data,
            projected,
            interface_base,
            source_root,
            add_target,
        )
        _rewrite_controller_paths(
            data,
            projected,
            interface_base,
            source_root,
            add_target,
        )
        _rewrite_language_paths(
            data,
            projected,
            interface_base,
            source_root,
            add_target,
        )
        _rewrite_agent_paths(
            data,
            projected,
            interface_base,
            source_root,
            add_target,
            agent_scope=source_relative.as_posix(),
        )
        _rewrite_pretask_paths(
            data,
            projected,
            interface_base,
            source_root,
            add_target,
        )
        rewritten[source_relative] = projected
        visiting.remove(source_relative)

    visit(root_interface_path, root_data)
    return rewritten


def _rewrite_resource_paths(
    source: dict[str, Any],
    projected: dict[str, Any],
    interface_base: Path,
    source_root: Path,
    add_target: Any,
) -> None:
    source_resources = source.get("resource")
    projected_resources = projected.get("resource")
    if not isinstance(source_resources, list) or not isinstance(projected_resources, list):
        return
    for index, source_resource in enumerate(source_resources):
        if not isinstance(source_resource, dict) or index >= len(projected_resources):
            continue
        projected_resource = projected_resources[index]
        if not isinstance(projected_resource, dict):
            continue
        resource_name = _optional_string(source_resource.get("name")) or f"resource[{index}]"
        raw_paths = source_resource.get("path")
        if isinstance(raw_paths, str):
            source_values = [raw_paths]
            preserve_scalar = True
        elif isinstance(raw_paths, list):
            source_values = raw_paths
            preserve_scalar = False
        else:
            continue
        rewritten_paths: list[str] = []
        for raw_path in source_values:
            if not isinstance(raw_path, str):
                raise MaaFWProjectStoreError(
                    f"resource {resource_name} path must contain strings"
                )
            source_path, output_path = _resolve_and_project_local_path(
                raw_path,
                interface_base,
                source_root,
                f"resource {resource_name}",
                required=True,
                allow_root=True,
            )
            add_target(
                source_path.relative_to(source_root),
                complete=True,
                required=True,
                label=f"resource {resource_name}",
                allow_excluded_root=True,
            )
            rewritten_paths.append(_format_project_path(output_path))
        projected_resource["path"] = (
            rewritten_paths[0] if preserve_scalar else rewritten_paths
        )


def _rewrite_controller_paths(
    source: dict[str, Any],
    projected: dict[str, Any],
    interface_base: Path,
    source_root: Path,
    add_target: Any,
) -> None:
    source_controllers = source.get("controller")
    projected_controllers = projected.get("controller")
    if not isinstance(source_controllers, list) or not isinstance(projected_controllers, list):
        return
    for index, source_controller in enumerate(source_controllers):
        if not isinstance(source_controller, dict) or index >= len(projected_controllers):
            continue
        projected_controller = projected_controllers[index]
        if not isinstance(projected_controller, dict):
            continue
        key = (
            "attach_resource_path"
            if "attach_resource_path" in source_controller
            else "attachResourcePath"
        )
        raw_value = source_controller.get(key)
        if raw_value is None:
            continue
        if isinstance(raw_value, str):
            values = [raw_value]
            preserve_scalar = True
        elif isinstance(raw_value, list):
            values = raw_value
            preserve_scalar = False
        else:
            raise MaaFWProjectStoreError(
                f"controller[{index}].{key} must be a string array"
            )
        rewritten_paths: list[str] = []
        for raw_path in values:
            if not isinstance(raw_path, str):
                raise MaaFWProjectStoreError(
                    f"controller[{index}].{key} must contain strings"
                )
            source_path, output_path = _resolve_and_project_local_path(
                raw_path,
                interface_base,
                source_root,
                f"controller[{index}].{key}",
                required=True,
                allow_root=True,
            )
            add_target(
                source_path.relative_to(source_root),
                complete=True,
                required=True,
                label=f"controller[{index}].{key}",
                allow_excluded_root=True,
            )
            rewritten_paths.append(_format_project_path(output_path))
        projected_controller[key] = (
            rewritten_paths[0] if preserve_scalar else rewritten_paths
        )


def _rewrite_language_paths(
    source: dict[str, Any],
    projected: dict[str, Any],
    interface_base: Path,
    source_root: Path,
    add_target: Any,
) -> None:
    languages = source.get("languages")
    projected_languages = projected.get("languages")
    if not isinstance(languages, dict) or not isinstance(projected_languages, dict):
        return
    for language, raw_path in languages.items():
        if not isinstance(raw_path, str):
            raise MaaFWProjectStoreError(f"languages.{language} must be a string path")
        source_path, output_path = _resolve_and_project_local_path(
            raw_path,
            interface_base,
            source_root,
            f"languages.{language}",
            required=True,
        )
        if not source_path.is_file():
            raise MaaFWProjectStoreError(f"languages.{language} is not a file")
        add_target(
            source_path.relative_to(source_root),
            complete=True,
            required=True,
            label=f"languages.{language}",
        )
        projected_languages[language] = _format_project_path(output_path)


def _rewrite_agent_paths(
    source: dict[str, Any],
    projected: dict[str, Any],
    interface_base: Path,
    source_root: Path,
    add_target: Any,
    *,
    agent_scope: str,
) -> None:
    source_agents = source.get("agent")
    projected_agents = projected.get("agent")
    if isinstance(source_agents, dict):
        source_values = [source_agents]
        projected_values = [projected_agents] if isinstance(projected_agents, dict) else []
    elif isinstance(source_agents, list):
        source_values = source_agents
        projected_values = projected_agents if isinstance(projected_agents, list) else []
    else:
        return
    for index, source_agent in enumerate(source_values):
        if not isinstance(source_agent, dict) or index >= len(projected_values):
            continue
        projected_agent = projected_values[index]
        if not isinstance(projected_agent, dict):
            continue
        raw_child_exec = (
            _optional_string(source_agent.get("child_exec"))
            or _optional_string(source_agent.get("childExec"))
            or ""
        )
        raw_child_args = source_agent.get("child_args")
        if raw_child_args is None:
            raw_child_args = source_agent.get("childArgs")
        child_args = (
            [item for item in raw_child_args if isinstance(item, str)]
            if isinstance(raw_child_args, list)
            else []
        )
        declared_type = (
            _optional_string(source_agent.get("type"))
            or _optional_string(source_agent.get("kind"))
            or ""
        )
        classification, _opaque = _classify_agent(
            declared_type,
            raw_child_exec,
            child_args,
        )
        agent_key = f"{agent_scope}#{index}"
        for key in ("child_exec", "childExec"):
            raw_exec = source_agent.get(key)
            if not isinstance(raw_exec, str) or not _looks_like_local_path(raw_exec):
                continue
            source_path, output_path = _resolve_and_project_local_path(
                raw_exec,
                interface_base,
                source_root,
                f"agent[{index}].{key}",
                required=True,
            )
            add_target(
                _agent_retention_root(source_path, source_root),
                complete=False,
                required=True,
                label=f"agent[{index}].{key}",
                required_path=source_path.relative_to(source_root),
                agent_index=index,
                agent_key=agent_key,
                python_entrypoint=(
                    classification == "python"
                    and source_path.suffix.casefold() in {".py", ".pyw"}
                ),
                allow_stripped_python_interpreter=(
                    classification == "python"
                    and _is_python_interpreter_path(source_path)
                ),
            )
            projected_agent[key] = _format_project_path(output_path)
        for key in ("child_args", "childArgs"):
            raw_args = source_agent.get(key)
            projected_args = projected_agent.get(key)
            if not isinstance(raw_args, list) or not isinstance(projected_args, list):
                continue
            for argument_index, raw_arg in enumerate(raw_args):
                if not isinstance(raw_arg, str) or not _looks_like_local_path(raw_arg):
                    continue
                source_path, output_path = _resolve_and_project_local_path(
                    raw_arg,
                    interface_base,
                    source_root,
                    f"agent[{index}].{key}[{argument_index}]",
                    required=True,
                )
                add_target(
                    _agent_retention_root(source_path, source_root),
                    complete=False,
                    required=True,
                    label=f"agent[{index}].{key}[{argument_index}]",
                    required_path=source_path.relative_to(source_root),
                    agent_index=index,
                    agent_key=agent_key,
                    python_entrypoint=(
                        classification == "python"
                        and source_path.suffix.casefold() in {".py", ".pyw"}
                    ),
                )
                projected_args[argument_index] = _format_project_path(output_path)


def _rewrite_pretask_paths(
    source: dict[str, Any],
    projected: dict[str, Any],
    interface_base: Path,
    source_root: Path,
    add_target: Any,
) -> None:
    source_pretasks = source.get("pretask")
    projected_pretasks = projected.get("pretask")
    if isinstance(source_pretasks, dict):
        source_values = [source_pretasks]
        projected_values = [projected_pretasks] if isinstance(projected_pretasks, dict) else []
    elif isinstance(source_pretasks, list):
        source_values = source_pretasks
        projected_values = projected_pretasks if isinstance(projected_pretasks, list) else []
    else:
        return
    for index, source_pretask in enumerate(source_values):
        if not isinstance(source_pretask, dict) or index >= len(projected_values):
            continue
        raw_exec = source_pretask.get("exec")
        projected_pretask = projected_values[index]
        if (
            not isinstance(projected_pretask, dict)
            or not isinstance(raw_exec, str)
            or not _looks_like_local_path(raw_exec)
        ):
            continue
        source_path, output_path = _resolve_and_project_local_path(
            raw_exec,
            interface_base,
            source_root,
            f"pretask[{index}].exec",
            required=True,
        )
        add_target(
            _agent_retention_root(source_path, source_root),
            complete=False,
            required=True,
            label=f"pretask[{index}].exec",
            required_path=source_path.relative_to(source_root),
        )
        projected_pretask["exec"] = _format_project_path(output_path)


def _inspect_agents(
    raw_agents: Any,
    interface_base: Path,
    source_root: Path,
    agent_scope: str,
) -> tuple[list[dict[str, Any]], set[Path], bool]:
    agents = raw_agents if isinstance(raw_agents, list) else ([raw_agents] if isinstance(raw_agents, dict) else [])
    runtime: list[dict[str, Any]] = []
    targets: set[Path] = set()
    opaque_found = False

    for index, agent in enumerate(agents):
        child_exec = _optional_string(agent.get("child_exec")) or _optional_string(agent.get("childExec")) or ""
        child_args = agent.get("child_args")
        if child_args is None:
            child_args = agent.get("childArgs")
        args = [item for item in child_args or [] if isinstance(item, str)] if isinstance(child_args, list) else []
        declared_type = (
            _optional_string(agent.get("type"))
            or _optional_string(agent.get("kind"))
            or ""
        )
        classification, opaque = _classify_agent(declared_type, child_exec, args)
        opaque_found = opaque_found or opaque
        discovered_paths: list[str] = []
        for raw_candidate in [child_exec, *args]:
            candidate = _resolve_agent_candidate(
                interface_base,
                source_root,
                raw_candidate,
            )
            if candidate is None:
                continue
            relative = candidate.relative_to(source_root)
            projected_relative = _output_path_for_source(
                relative,
                source_root,
                interface_base,
            )
            discovered_paths.append(projected_relative.as_posix())
            targets.add(_agent_retention_root(candidate, source_root))
            if candidate.is_file():
                parent = relative.parent
                if parent == Path(".") and candidate.suffix.casefold() in {".py", ".pyw"}:
                    for sibling in source_root.glob("*.py*"):
                        if sibling.is_file():
                            targets.add(sibling.relative_to(source_root))
                    for folder_name in ("src", "lib", "modules", candidate.stem):
                        folder = source_root / folder_name
                        if folder.is_dir():
                            targets.add(folder.relative_to(source_root))

        runtime.append(
            {
                "index": index,
                "declaredType": declared_type or None,
                "childExec": child_exec or None,
                "classification": classification,
                "opaque": opaque,
                "projectPaths": discovered_paths,
                "_projectionKey": f"{agent_scope}#{index}",
            }
        )

    return runtime, targets, opaque_found


def _classify_agent(
    declared_type: str,
    child_exec: str,
    child_args: list[str],
) -> tuple[str, bool]:
    kind = declared_type.casefold()
    executable = Path(child_exec.replace("\\", "/")).name.casefold()
    suffixes = {Path(item.replace("\\", "/")).suffix.casefold() for item in child_args}
    if kind in {"custom", "command", "shell", "opaque"}:
        return kind or "opaque", True
    if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"} or ".py" in suffixes:
        return "python", False
    if executable in {"node", "node.exe", "deno", "deno.exe", "bun", "bun.exe"} or ".js" in suffixes or ".mjs" in suffixes:
        return "javascript", False
    if executable in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "sh", "bash"}:
        return "command", True
    if child_exec and ("/" in child_exec or "\\" in child_exec or Path(child_exec).suffix):
        return "native", False
    if child_exec:
        return "external", True
    return "opaque", True


def _resolve_agent_candidate(
    interface_base: Path,
    source_root: Path,
    raw_value: str,
) -> Path | None:
    if not _looks_like_local_path(raw_value):
        return None
    try:
        candidate, _ = _resolve_and_project_local_path(
            raw_value,
            interface_base,
            source_root,
            "agent path",
            required=False,
        )
    except MaaFWProjectStoreError:
        return None
    return candidate if candidate.exists() else None


def _materialize_projection(plan: _ProjectionPlan, data_dir: Path) -> None:
    for relative_directory in sorted(
        plan.copied_directories,
        key=lambda path: (len(path.parts), path.as_posix()),
    ):
        if relative_directory == Path("."):
            continue
        output_directory = _projection_output_path(plan, relative_directory)
        if output_directory == Path("."):
            continue
        destination = (data_dir / output_directory).resolve(strict=False)
        _assert_within(destination, data_dir)
        destination.mkdir(parents=True, exist_ok=True)

    for relative_file in sorted(plan.copied_files, key=lambda path: path.as_posix()):
        source = (plan.source_root / relative_file).resolve(strict=True)
        _assert_within(source, plan.source_root)
        _assert_not_reparse(source)
        output_file = _projection_output_path(plan, relative_file)
        destination = (data_dir / output_file).resolve(strict=False)
        _assert_within(destination, data_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for source_relative, payload in plan.rewritten_json.items():
        output_file = _projection_output_path(plan, source_relative)
        if source_relative not in plan.copied_files:
            raise MaaFWProjectStoreError(
                f"rewritten ProjectInterface file was not projected: {source_relative}"
            )
        _write_json(data_dir / output_file, payload)


def _clear_native_resource_hashes(
    plan: _ProjectionPlan,
) -> list[dict[str, str]]:
    cleared: list[dict[str, str]] = []
    for source_relative, projected in plan.rewritten_json.items():
        resources = projected.get("resource")
        if not isinstance(resources, list):
            continue
        output_file = _projection_output_path(plan, source_relative).as_posix()
        for index, resource in enumerate(resources):
            if not isinstance(resource, dict) or "hash" not in resource:
                continue
            resource.pop("hash", None)
            cleared.append(
                {
                    "file": output_file,
                    "resource": (
                        _optional_string(resource.get("name"))
                        or f"resource[{index}]"
                    ),
                }
            )
    return cleared


def _discover_project_interface(source_root: Path) -> tuple[Path, Path]:
    candidates = (
        (source_root, source_root / "interface.json"),
        (source_root, source_root / "interface.jsonc"),
        (source_root / "assets", source_root / "assets" / "interface.json"),
        (source_root / "assets", source_root / "assets" / "interface.jsonc"),
    )
    for project_root, interface_path in candidates:
        if interface_path.is_file():
            _assert_not_reparse(interface_path)
            _assert_within(interface_path.resolve(strict=True), source_root)
            return project_root.resolve(strict=True), interface_path.resolve(strict=True)
    raise MaaFWProjectStoreError(
        "interface.json or interface.jsonc was not found at the release root or assets/"
    )


def _scan_safe_tree(root: Path) -> tuple[set[Path], set[Path]]:
    directories: set[Path] = {Path(".")}
    files: set[Path] = set()
    for current_raw, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_raw)
        _assert_not_reparse(current)
        _assert_within(current.resolve(strict=True), root)
        for directory_name in list(directory_names):
            directory = current / directory_name
            _assert_not_reparse(directory)
            directories.add(directory.relative_to(root))
        for file_name in file_names:
            file_path = current / file_name
            _assert_not_reparse(file_path)
            if not file_path.is_file():
                raise MaaFWProjectStoreError(f"unsupported source entry: {file_path}")
            files.add(file_path.relative_to(root))
    return directories, files


def _calculate_projected_source_hash(root: Path, files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(files, key=lambda path: path.as_posix()):
        source = (root / relative_path).resolve(strict=True)
        _assert_within(source, root)
        _assert_not_reparse(source)
        encoded_path = relative_path.as_posix().encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        with source.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _detect_python_abi_tags(root: Path, files: Iterable[Path]) -> list[str]:
    tags: set[str] = set()
    compact_pattern = re.compile(
        r"(?<![A-Za-z0-9])(?P<tag>cp\d{2,3}(?:-[A-Za-z0-9_]+)?)",
        flags=re.IGNORECASE,
    )
    cpython_pattern = re.compile(
        r"(?<![A-Za-z0-9])(?P<tag>cpython-\d{2,3}(?:-[A-Za-z0-9_]+)+)",
        flags=re.IGNORECASE,
    )
    for relative_path in files:
        if relative_path.suffix.casefold() not in {".pyd", ".so"}:
            continue
        source = (root / relative_path).resolve(strict=True)
        _assert_within(source, root)
        for pattern in (compact_pattern, cpython_pattern):
            match = pattern.search(relative_path.name)
            if match is not None:
                tags.add(match.group("tag").casefold())
    return sorted(tags)


def _resolve_runtime_constraint(
    explicit: str | None,
    interface_data: dict[str, Any],
    source_root: Path,
    interface_base: Path,
) -> str | None:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    for key in (
        "maafw_constraint",
        "maafwConstraint",
        "maafw_version",
        "maafwVersion",
        "maa_framework_version",
        "maaFrameworkVersion",
    ):
        value = _optional_string(interface_data.get(key))
        if value:
            return value
    runtime = interface_data.get("runtime")
    if isinstance(runtime, dict):
        for key in ("maafw", "constraint", "version"):
            value = _optional_string(runtime.get(key))
            if value:
                return value
    return _read_maafw_requirement_constraint(source_root, interface_base)


def _read_maafw_requirement_constraint(
    source_root: Path,
    interface_base: Path,
) -> str | None:
    candidates: list[tuple[bool, str]] = []
    requirement_files = {
        path.resolve(strict=True)
        for base in {source_root, interface_base}
        for path in base.glob("requirements*.txt")
        if path.is_file()
    }
    requirement_pattern = re.compile(
        r"^\s*(?:maafw|maa[-_]framework)(?:\[[^\]]+\])?\s*"
        r"(?P<constraint>(?:===|==|~=|>=|<=|!=|>|<).+?)?\s*$",
        flags=re.IGNORECASE,
    )
    for requirement_file in sorted(requirement_files, key=str):
        _assert_not_reparse(requirement_file)
        try:
            lines = requirement_file.read_text(encoding="utf-8-sig").splitlines()
        except Exception as exc:
            raise MaaFWProjectStoreError(
                f"cannot read MaaFW requirements file {requirement_file}: {exc}"
            ) from exc
        for raw_line in lines:
            line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
            if not line or line.startswith(("-", "--")):
                continue
            match = requirement_pattern.fullmatch(line)
            if match is None:
                continue
            constraint = (match.group("constraint") or "").strip()
            if not constraint:
                continue
            exact = constraint.startswith(("===", "=="))
            candidates.append((exact, constraint))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
    return candidates[0][1]


def _resolve_and_project_local_path(
    raw_path: str,
    interface_base: Path,
    source_root: Path,
    field_name: str,
    *,
    required: bool,
    allow_root: bool = False,
) -> tuple[Path, Path]:
    value = str(raw_path).strip().strip('"').strip("'").replace("\\", "/")
    value = value.replace("${PROJECT_DIR}", "{PROJECT_DIR}")
    if value.startswith("{PROJECT_DIR}"):
        value = value[len("{PROJECT_DIR}") :].lstrip("/")
    if not value and not allow_root:
        raise MaaFWProjectStoreError(f"{field_name} cannot be empty")
    candidate_path = Path(value or ".")
    if candidate_path.is_absolute() or candidate_path.drive or candidate_path.root:
        raise MaaFWProjectStoreError(
            f"{field_name} must stay inside the unpacked release: {raw_path}"
        )
    source_path = (interface_base / candidate_path).resolve(strict=False)
    _assert_within(source_path, source_root)
    if required and not source_path.exists():
        raise MaaFWProjectStoreError(
            f"{field_name} path does not exist in the unpacked release: {raw_path}"
        )
    if source_path.exists():
        _assert_existing_chain_has_no_reparse(source_path)
    source_relative = source_path.relative_to(source_root)
    output_path = _projection_output_path_from_roots(
        source_relative,
        source_root,
        interface_base,
    )
    return source_path, output_path


def _output_path_for_source(
    source_relative: Path,
    source_root: Path,
    interface_base: Path,
) -> Path:
    return _projection_output_path_from_roots(
        source_relative,
        source_root,
        interface_base,
    )


def _projection_output_path(plan: _ProjectionPlan, source_relative: Path) -> Path:
    return _projection_output_path_from_roots(
        source_relative,
        plan.source_root,
        plan.interface_base,
    )


def _projection_output_path_from_roots(
    source_relative: Path,
    source_root: Path,
    interface_base: Path,
) -> Path:
    absolute_source = (source_root / source_relative).resolve(strict=False)
    _assert_within(absolute_source, source_root)
    try:
        relative_to_interface = absolute_source.relative_to(interface_base)
        return relative_to_interface if relative_to_interface.parts else Path(".")
    except ValueError:
        relative_to_release = absolute_source.relative_to(source_root)
        return relative_to_release if relative_to_release.parts else Path(".")


def _format_project_path(path: Path) -> str:
    if path == Path(".") or not path.parts:
        return "."
    return f"./{path.as_posix()}"


def _looks_like_local_path(value: str) -> bool:
    normalized = value.strip().strip('"').strip("'")
    if not normalized or normalized.startswith(("-", "http://", "https://")):
        return False
    if normalized.startswith(("{PROJECT_DIR}", "${PROJECT_DIR}", "./", "../", ".\\", "..\\")):
        return True
    if "/" in normalized or "\\" in normalized:
        return True
    return Path(normalized).suffix.casefold() in {
        ".py",
        ".pyw",
        ".js",
        ".mjs",
        ".cjs",
        ".exe",
        ".cmd",
        ".bat",
        ".ps1",
        ".sh",
    }


def _agent_retention_root(source_path: Path, source_root: Path) -> Path:
    source_relative = source_path.relative_to(source_root)
    if source_path.is_dir():
        return source_relative
    parent = source_relative.parent
    return parent if parent != Path(".") else source_relative


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json5.load(file)
    except Exception as exc:
        raise MaaFWProjectStoreError(f"cannot parse ProjectInterface file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MaaFWProjectStoreError(f"ProjectInterface file must contain a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: dict[str, Any], store_root: Path) -> None:
    _assert_path_chain_within_root(path, store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_path_chain_within_root(path.parent, store_root)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_json(temp_path, payload)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _canonical_source_directory(source_path: str | Path, store_root: Path) -> Path:
    raw_path = Path(source_path)
    _assert_existing_chain_has_no_reparse(Path(os.path.abspath(raw_path)))
    try:
        source = raw_path.resolve(strict=True)
    except OSError as exc:
        raise MaaFWProjectStoreError(f"source directory does not exist: {source_path}") from exc
    if not source.is_dir():
        raise MaaFWProjectStoreError(f"source path is not a directory: {source_path}")
    _assert_not_reparse(source)
    if _is_within(source, store_root) or _is_within(store_root, source):
        raise MaaFWProjectStoreError("source directory and project store must not contain each other")
    return source


def _normalize_relative_path(
    raw_path: str,
    field_name: str,
    *,
    allow_root: bool = False,
) -> Path:
    value = str(raw_path).strip().replace("\\", "/")
    value = value.replace("${PROJECT_DIR}", "{PROJECT_DIR}")
    if value.startswith("{PROJECT_DIR}"):
        value = value[len("{PROJECT_DIR}") :].lstrip("/")
    candidate = Path(value)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise MaaFWProjectStoreError(f"{field_name} must be project-relative: {raw_path}")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise MaaFWProjectStoreError(f"{field_name} escapes the project root: {raw_path}")
    if not parts:
        if allow_root:
            return Path(".")
        raise MaaFWProjectStoreError(f"{field_name} cannot be empty")
    return Path(*parts)


def _validate_component(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _COMPONENT_PATTERN.fullmatch(normalized):
        raise MaaFWProjectStoreError(
            f"{field_name} must use only letters, digits, '.', '_', '+', or '-'"
        )
    if normalized.endswith((".", " ")):
        raise MaaFWProjectStoreError(f"{field_name} has an unsafe trailing character")
    if normalized.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise MaaFWProjectStoreError(f"{field_name} uses a reserved Windows name")
    return normalized


def _exclusion_reason(path: Path, *, is_directory: bool = False) -> str | None:
    parts = path.parts if is_directory else path.parts[:-1]
    for part in parts:
        normalized_part = part.casefold()
        reason = _EXCLUDED_DIRECTORY_REASONS.get(normalized_part)
        if reason:
            return reason
        part_family = normalized_part.split(".", 1)[0]
        if part_family in _KNOWN_UI_SHELL_STEMS:
            return "ui-shell"
        if part_family in _KNOWN_RUNTIME_STEMS:
            return "embedded-runtime"
    name = path.name.casefold()
    if name == MANIFEST_FILE_NAME.casefold():
        return "reserved-project-store-manifest"
    if not is_directory:
        suffix = path.suffix.casefold()
        shell_family = name.split(".", 1)[0]
        if shell_family in _KNOWN_UI_SHELL_STEMS:
            return "ui-shell"
        if shell_family in _KNOWN_RUNTIME_STEMS:
            return "embedded-runtime"
        if suffix in _EXCLUDED_FILE_SUFFIXES:
            return "cache-or-temporary"
        if name in _KNOWN_RUNTIME_FILE_NAMES or (
            name.startswith("python") and suffix in {".dll", ".exe", ".so", ".dylib"}
        ):
            return "embedded-runtime"
        if suffix in _SHELL_SUFFIXES and (
            "update" in path.stem.casefold()
            or "updater" in path.stem.casefold()
            or path.stem.casefold().endswith(("gui", "ui", "launcher"))
        ):
            return "ui-or-updater-shell"
    return None


def _assert_existing_chain_has_no_reparse(path: Path) -> None:
    existing: list[Path] = []
    current = path
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(existing):
        _assert_not_reparse(item)


def _assert_not_reparse(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    if path.is_symlink() or bool(file_attributes & reparse_flag):
        raise MaaFWProjectStoreError(f"reparse points are not allowed: {path}")


def _assert_path_chain_within_root(path: Path, root: Path) -> None:
    absolute = Path(os.path.abspath(path))
    root_absolute = root.resolve(strict=True)
    try:
        absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise MaaFWProjectStoreError(f"path escapes project store: {path}") from exc
    current = root_absolute
    _assert_not_reparse(current)
    for part in absolute.relative_to(root_absolute).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            _assert_not_reparse(current)


def _assert_within(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise MaaFWProjectStoreError(f"path escapes allowed root: {path}") from exc


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _safe_remove_tree(path: Path, store_root: Path) -> None:
    _assert_path_chain_within_root(path, store_root)
    resolved_root = store_root.resolve(strict=True)
    resolved_path = path.resolve(strict=False)
    if resolved_path == resolved_root:
        raise MaaFWProjectStoreError("refusing to delete the project-store root")
    if not path.exists():
        return
    _assert_not_reparse(path)
    for current_raw, directory_names, file_names in os.walk(path, followlinks=False):
        current = Path(current_raw)
        _assert_not_reparse(current)
        for name in [*directory_names, *file_names]:
            _assert_not_reparse(current / name)
    shutil.rmtree(path)


def _tree_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for current_raw, directory_names, file_names in os.walk(path, followlinks=False):
        current = Path(current_raw)
        _assert_not_reparse(current)
        for directory_name in directory_names:
            _assert_not_reparse(current / directory_name)
        for file_name in file_names:
            file_path = current / file_name
            _assert_not_reparse(file_path)
            total += file_path.stat().st_size
    return total


def _relative_parents(path: Path) -> set[Path]:
    parents: set[Path] = {Path(".")}
    current = path.parent
    while current != Path("."):
        parents.add(current)
        current = current.parent
    return parents


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _object_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _string_or_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _json_clone(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise MaaFWProjectStoreError("service values must be JSON-compatible") from exc


def _format_timestamp(timestamp: float | None = None) -> str:
    value = datetime.fromtimestamp(
        timestamp if timestamp is not None else time.time(),
        tz=timezone.utc,
    )
    return value.isoformat().replace("+00:00", "Z")


def _active_leases(value: Any, now: float) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    active: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        lease_id = item.get("leaseId")
        owner = item.get("owner")
        expires_at = item.get("expiresAt")
        if (
            not isinstance(lease_id, str)
            or not lease_id
            or not isinstance(owner, str)
            or not owner
            or _parse_timestamp(expires_at) <= now
        ):
            continue
        active.append(_json_clone(item))
    return active


def _parse_timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
