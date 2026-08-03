from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


RUNTIME_INSTALL_TIMEOUT_SECONDS = 300
RUNTIME_AUDIT_TIMEOUT_SECONDS = 60
VENV_PROBE_TIMEOUT_SECONDS = 30
# uv 兜底可能需要下载 managed Python，给足余量。
UV_VENV_TIMEOUT_SECONDS = 300
UV_CACHE_RELATIVE_PATH = Path("cache") / "uv"
UV_LINK_MODE = "hardlink"
RUNTIME_POOL_STAGING_DIRECTORY_NAME = ".staging"

_IDENTITY_PROBE_SCRIPT = (
    "import json,sys,sysconfig;"
    "print(json.dumps({"
    "'implementation': getattr(sys.implementation, 'name', 'python'),"
    "'cacheTag': getattr(sys.implementation, 'cache_tag', None) or 'unknown',"
    "'soabi': str(sysconfig.get_config_var('SOABI') or 'unknown'),"
    "'version': '.'.join(str(part) for part in sys.version_info[:3]),"
    "'shortVersion': f'{sys.version_info.major}.{sys.version_info.minor}',"
    "}))"
)


def install_python_runtime(
    environment_path: Path,
    requirements: Sequence[str],
    identity: dict[str, Any],
    *,
    cwd: str | Path | None = None,
    bootstrap_python: str | Path | None = None,
    send_log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Install one isolated selector environment with a pool-local uv cache.

    Every canonical requirement set still owns an independent venv.  When uv
    is available, package downloads and unpacked wheels are shared through a
    cache located beside the pool's ``runtimes``/``.staging`` directories, and
    uv hardlinks cached package files into each venv.  A complete Python may
    fall back to stdlib ``venv`` + pip when uv is unavailable; an embeddable
    Python without ``venv``/``ensurepip`` cannot.
    """

    log = send_log or (lambda _: None)
    bootstrap = str(bootstrap_python or sys.executable)
    resolved_cwd = Path(cwd).resolve() if cwd is not None else Path.cwd()
    pool_root = _runtime_pool_root(environment_path)
    uv_cache_dir = (pool_root / UV_CACHE_RELATIVE_PATH).resolve()
    uv_executable = _find_uv_executable(bootstrap)
    if uv_executable is not None:
        uv_cache_dir.mkdir(parents=True, exist_ok=True)
    environment_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"[MaaFW Runtime Pool] 创建共享环境: {environment_path}")
    environment_installer = _create_environment(
        environment_path,
        bootstrap,
        uv_executable=uv_executable,
        uv_cache_dir=uv_cache_dir,
        cwd=resolved_cwd,
        log=log,
    )

    python_executable = _venv_python(environment_path)
    # 兜底路径可能换了解释器，manifest 里的 identity 取自宿主进程，
    # 必须在落盘前对账，避免声明的 ABI 与实际 runtime 不一致。
    probe = _verify_runtime_identity(python_executable, identity)
    log(f"[MaaFW Runtime Pool] 安装依赖: {', '.join(requirements)}")
    if uv_executable is not None:
        _install_requirements_with_uv(
            uv_executable,
            python_executable,
            requirements,
            cache_dir=uv_cache_dir,
            link_mode=UV_LINK_MODE,
            cwd=resolved_cwd,
        )
        dependency_installer = "uv-pip"
        resolved_requirements = _resolved_requirements_with_uv(
            uv_executable,
            python_executable,
            cache_dir=uv_cache_dir,
        )
    else:
        _install_requirements_with_pip(
            python_executable,
            requirements,
            cwd=resolved_cwd,
        )
        dependency_installer = "pip"
        resolved_requirements = _resolved_requirements(python_executable)
    version = _installed_maafw_version(python_executable)
    installer_name = "uv" if uv_executable is not None else "pip"
    installer_metadata = {
        "installer": {
            "name": installer_name,
            "version": (
                _uv_version(uv_executable)
                if uv_executable is not None
                else _pip_version(python_executable)
            ),
            "executable": (
                str(Path(uv_executable).resolve())
                if uv_executable is not None
                else str(python_executable)
            ),
            "environment": environment_installer,
            "dependencies": dependency_installer,
        },
        "cache": {
            "kind": "uv" if uv_executable is not None else "pip-default",
            "scope": "pool" if uv_executable is not None else "external",
            "shared": uv_executable is not None,
            "path": str(uv_cache_dir) if uv_executable is not None else None,
            "relativeToPool": (
                UV_CACHE_RELATIVE_PATH.as_posix()
                if uv_executable is not None
                else None
            ),
        },
        "link": {
            "mode": UV_LINK_MODE if uv_executable is not None else "pip-default",
        },
    }
    return {
        "pythonExecutable": str(python_executable),
        "pythonVersion": probe.get("version") or platform.python_version(),
        "maafwVersion": version,
        "resolvedRequirements": resolved_requirements,
        **installer_metadata,
    }


def _create_environment(
    environment_path: Path,
    bootstrap: str,
    *,
    uv_executable: str | None,
    uv_cache_dir: Path,
    cwd: Path,
    log: Callable[[str], None],
) -> str:
    """创建共享 runtime venv；引导解释器缺 venv 模块时回退到 uv。

    绿色免安装包随附的 environment/python 是 embeddable 发行版
    （python3xx._pth，不含 Lib/venv），`python.exe -m venv` 会直接报
    "No module named venv"。必须先探测，不合格再走 uv 兜底。
    """

    if _python_supports_venv(bootstrap):
        command = [bootstrap, "-m", "venv"]
        if uv_executable is not None:
            # uv installs and audits dependencies itself; avoid seeding a
            # private pip/setuptools copy into every selector environment.
            command.append("--without-pip")
        command.append(str(environment_path))
        _run(
            command,
            cwd=cwd,
            env=_clean_process_environment(),
        )
        return "stdlib-venv"
    log(
        "[MaaFW Runtime Pool] 引导 Python 缺少 venv 模块"
        "（便携版常见 embeddable 发行版），改用 uv 创建共享环境"
    )
    if uv_executable is None:
        raise RuntimeError(
            "MaaFW runtime 安装失败：引导 Python 不含 venv 模块"
            "（便携版常见 embeddable 发行版），且未找到 uv 兜底。"
            "请提供完整 Python 或在 environment/python/Scripts 下放置 uv。"
        )
    _create_environment_with_uv(
        environment_path,
        bootstrap=bootstrap,
        uv_executable=uv_executable,
        uv_cache_dir=uv_cache_dir,
        cwd=cwd,
        log=log,
    )
    return "uv-venv"


def _create_environment_with_uv(
    environment_path: Path,
    *,
    cwd: Path,
    bootstrap: str,
    uv_executable: str,
    uv_cache_dir: Path,
    log: Callable[[str], None],
) -> None:
    # runtime identity 的 pythonAbi 取自宿主进程，兜底解释器必须同 major.minor，
    # 否则会造出 manifest 声明与实际不符的 runtime。
    target_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    log(
        f"[MaaFW Runtime Pool] uv 创建共享环境 (python {target_version}): "
        f"{environment_path}"
    )
    _run(
        [
            uv_executable,
            "venv",
            "--python",
            target_version,
            "--cache-dir",
            str(uv_cache_dir),
            "--link-mode",
            UV_LINK_MODE,
            str(environment_path),
        ],
        cwd=cwd,
        env=_uv_environment(uv_cache_dir, UV_LINK_MODE),
        timeout=UV_VENV_TIMEOUT_SECONDS,
    )


def _python_supports_venv(python: str) -> bool:
    """探测解释器是否带 venv/ensurepip 标准库。"""

    try:
        result = subprocess.run(
            [python, "-c", "import venv, ensurepip"],
            capture_output=True,
            timeout=VENV_PROBE_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _find_uv_executable(bootstrap: str) -> str | None:
    configured_uv = os.environ.get("AUTO_MAS_UV_EXE")
    if configured_uv:
        configured_path = Path(configured_uv)
        if configured_path.is_file():
            return str(configured_path.resolve())

    bootstrap_path = Path(bootstrap).resolve()
    bootstrap_candidates = (
        bootstrap_path.parent / "uv.exe",
        bootstrap_path.parent / "Scripts" / "uv.exe",
    )
    for bootstrap_uv in bootstrap_candidates:
        if bootstrap_uv.is_file():
            return str(bootstrap_uv)

    portable_uv = Path.cwd() / "environment" / "python" / "Scripts" / "uv.exe"
    if portable_uv.is_file():
        return str(portable_uv)
    return shutil.which("uv")


def _runtime_pool_root(environment_path: Path) -> Path:
    """Infer the owning pool root from ``.staging/<id>/environment``."""

    resolved = environment_path.resolve()
    stage_dir = resolved.parent
    staging_root = stage_dir.parent
    if staging_root.name == RUNTIME_POOL_STAGING_DIRECTORY_NAME:
        return staging_root.parent
    # Direct installer calls (including contract tests) have no pool object.
    # Keep their cache beside the staging parent rather than in process-global
    # uv state.
    return staging_root


def _install_requirements_with_uv(
    uv_executable: str,
    python_executable: Path,
    requirements: Sequence[str],
    *,
    cache_dir: Path,
    link_mode: str,
    cwd: Path,
) -> None:
    _run(
        [
            uv_executable,
            "pip",
            "install",
            "--python",
            str(python_executable),
            "--cache-dir",
            str(cache_dir),
            "--link-mode",
            link_mode,
            "--upgrade",
            "--quiet",
            *requirements,
        ],
        cwd=cwd,
        env=_uv_install_environment(
            python_executable.parent.parent,
            cache_dir,
            link_mode,
        ),
    )


def _install_requirements_with_pip(
    python_executable: Path,
    requirements: Sequence[str],
    *,
    cwd: Path,
) -> None:
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
        cwd=cwd,
        env=_clean_install_environment(python_executable.parent.parent),
    )


def _probe_python_identity(python_executable: Path) -> dict[str, str]:
    try:
        result = subprocess.run(
            [str(python_executable), "-c", _IDENTITY_PROBE_SCRIPT],
            capture_output=True,
            timeout=VENV_PROBE_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"MaaFW runtime ABI 探测失败：无法执行 {python_executable}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"MaaFW runtime ABI 探测失败 (exit={result.returncode}): {detail[:400]}"
        )
    try:
        payload = json.loads(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("MaaFW runtime ABI 探测返回值不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MaaFW runtime ABI 探测返回值不是 JSON object")
    return {str(key): str(value) for key, value in payload.items()}


def _verify_runtime_identity(
    python_executable: Path,
    identity: dict[str, Any] | None,
) -> dict[str, str]:
    """对账新建 runtime 的实际 ABI 与 pool 声明的 identity。"""

    probe = _probe_python_identity(python_executable)
    if not identity:
        return probe
    actual_abi = (
        f"{probe.get('implementation', 'python')}:"
        f"{probe.get('cacheTag', 'unknown')}:"
        f"{probe.get('soabi', 'unknown')}"
    )
    expected_abi = str(identity.get("pythonAbi") or "").strip()
    if expected_abi and expected_abi != actual_abi:
        raise RuntimeError(
            "MaaFW runtime ABI 与 identity 声明不一致："
            f"expected={expected_abi}, actual={actual_abi}"
        )
    expected_version = str(identity.get("pythonVersion") or "").strip()
    actual_version = probe.get("shortVersion", "")
    if expected_version and expected_version != actual_version:
        raise RuntimeError(
            "MaaFW runtime Python 版本与 identity 声明不一致："
            f"expected={expected_version}, actual={actual_version}"
        )
    return probe


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = RUNTIME_INSTALL_TIMEOUT_SECONDS,
) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
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


def _clean_process_environment() -> dict[str, str]:
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
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _clean_install_environment(environment_path: Path) -> dict[str, str]:
    env = _clean_process_environment()
    scripts_dir = environment_path / ("Scripts" if os.name == "nt" else "bin")
    env["VIRTUAL_ENV"] = str(environment_path)
    env["PATH"] = f"{scripts_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _uv_environment(cache_dir: Path, link_mode: str) -> dict[str, str]:
    env = _clean_process_environment()
    env["UV_CACHE_DIR"] = str(cache_dir)
    env["UV_LINK_MODE"] = link_mode
    return env


def _uv_install_environment(
    environment_path: Path,
    cache_dir: Path,
    link_mode: str,
) -> dict[str, str]:
    env = _clean_install_environment(environment_path)
    env["UV_CACHE_DIR"] = str(cache_dir)
    env["UV_LINK_MODE"] = link_mode
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


def _resolved_requirements_with_uv(
    uv_executable: str,
    python_executable: Path,
    *,
    cache_dir: Path,
) -> list[str]:
    try:
        result = subprocess.run(
            [
                uv_executable,
                "pip",
                "freeze",
                "--python",
                str(python_executable),
                "--cache-dir",
                str(cache_dir),
            ],
            capture_output=True,
            timeout=RUNTIME_AUDIT_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_uv_install_environment(
                python_executable.parent.parent,
                cache_dir,
                UV_LINK_MODE,
            ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("MaaFW runtime uv resolved requirements 审计失败") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            "MaaFW runtime uv pip freeze 失败 "
            f"(exit={result.returncode}): {detail[:800]}"
        )
    return _normalized_freeze_lines(result.stdout)


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
    return _normalized_freeze_lines(result.stdout)


def _normalized_freeze_lines(output: str) -> list[str]:
    return sorted(
        {
            line.strip()
            for line in output.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        },
        key=str.casefold,
    )


def _uv_version(uv_executable: str) -> str | None:
    return _command_version([uv_executable, "--version"], prefix="uv ")


def _pip_version(python_executable: Path) -> str | None:
    return _command_version(
        [str(python_executable), "-m", "pip", "--version"],
        prefix="pip ",
        env=_clean_install_environment(python_executable.parent.parent),
    )


def _command_version(
    command: list[str],
    *,
    prefix: str,
    env: dict[str, str] | None = None,
) -> str | None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=15,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if value.casefold().startswith(prefix.casefold()):
        value = value[len(prefix):].strip()
    return value or None


def _venv_python(environment_path: Path) -> Path:
    if os.name == "nt":
        return environment_path / "Scripts" / "python.exe"
    return environment_path / "bin" / "python"
