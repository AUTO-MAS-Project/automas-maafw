from __future__ import annotations

import os
import platform
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


RUNTIME_INSTALL_TIMEOUT_SECONDS = 300
RUNTIME_AUDIT_TIMEOUT_SECONDS = 60


def install_python_runtime(
    environment_path: Path,
    requirements: Sequence[str],
    identity: dict[str, Any],
    *,
    cwd: str | Path | None = None,
    bootstrap_python: str | Path | None = None,
    send_log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Install a canonical requirement selector and audit its resolution."""

    del identity  # Identity was validated by the pool before installation.
    log = send_log or (lambda _: None)
    bootstrap = str(bootstrap_python or sys.executable)
    environment_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"[MaaFW Runtime Pool] 创建共享环境: {environment_path}")
    _run(
        [bootstrap, "-m", "venv", str(environment_path)],
        cwd=Path(cwd).resolve() if cwd is not None else Path.cwd(),
    )

    python_executable = _venv_python(environment_path)
    log(f"[MaaFW Runtime Pool] 安装依赖: {', '.join(requirements)}")
    _run(
        [
            str(python_executable),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
            "--quiet",
            *requirements,
        ],
        cwd=Path(cwd).resolve() if cwd is not None else Path.cwd(),
        env=_clean_install_environment(environment_path),
    )
    version = _installed_maafw_version(python_executable)
    resolved_requirements = _resolved_requirements(python_executable)
    return {
        "pythonExecutable": str(python_executable),
        "pythonVersion": platform.python_version(),
        "maafwVersion": version,
        "resolvedRequirements": resolved_requirements,
    }


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=RUNTIME_INSTALL_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"MaaFW runtime 安装超时: {command[:3]}") from exc
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").strip()
    raise RuntimeError(
        f"MaaFW runtime 安装失败 (exit={result.returncode}): {detail[:800]}"
    )


def _clean_install_environment(environment_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONPATH",
        "PIP_TARGET",
        "PIP_PREFIX",
        "PIP_USER",
    ):
        env.pop(name, None)
    scripts_dir = environment_path / ("Scripts" if os.name == "nt" else "bin")
    env["VIRTUAL_ENV"] = str(environment_path)
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = f"{scripts_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _installed_maafw_version(python_executable: Path) -> str | None:
    try:
        result = subprocess.run(
            [
                str(python_executable),
                "-c",
                "import importlib.metadata as m; print(m.version('maafw'))",
            ],
            capture_output=True,
            timeout=15,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_clean_install_environment(python_executable.parent.parent),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _resolved_requirements(python_executable: Path) -> list[str]:
    try:
        result = subprocess.run(
            [
                str(python_executable),
                "-m",
                "pip",
                "--disable-pip-version-check",
                "freeze",
                "--all",
            ],
            capture_output=True,
            timeout=RUNTIME_AUDIT_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_clean_install_environment(python_executable.parent.parent),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("MaaFW runtime resolved requirements 审计失败") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            "MaaFW runtime pip freeze --all 失败 "
            f"(exit={result.returncode}): {detail[:800]}"
        )
    return sorted(
        {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        },
        key=str.casefold,
    )


def _venv_python(environment_path: Path) -> Path:
    if os.name == "nt":
        return environment_path / "Scripts" / "python.exe"
    return environment_path / "bin" / "python"
