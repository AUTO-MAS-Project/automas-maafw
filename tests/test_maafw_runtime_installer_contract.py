"""共享 runtime 安装器的引导契约。

绿色免安装包随附的 environment/python 是 embeddable 发行版（python3xx._pth，
不含 Lib/venv），`python.exe -m venv` 会直接 "No module named venv"。安装器
必须先探测引导解释器，不合格时用 uv 兜底，并在落盘前对账实际 ABI 与 pool
声明的 identity。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_POOL_SOURCE = ROOT / "packages" / "automas_maafw_runtime_pool" / "src"

if str(RUNTIME_POOL_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_POOL_SOURCE))

from automas_maafw_runtime_pool import installer as runtime_installer  # noqa: E402
from automas_maafw_runtime_pool.identity import (  # noqa: E402
    build_runtime_identity,
)


HOST_SHORT_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"


def _probe_for(identity: dict[str, Any]) -> dict[str, str]:
    implementation, cache_tag, soabi = str(identity["pythonAbi"]).split(":", 2)
    return {
        "implementation": implementation,
        "cacheTag": cache_tag,
        "soabi": soabi,
        "version": f"{identity['pythonVersion']}.99",
        "shortVersion": str(identity["pythonVersion"]),
    }


class MaaFWRuntimeInstallerBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.environment_path = (
            Path(self.temporary_directory.name) / "stage" / "environment"
        )
        self.uv_cache_dir = (
            Path(self.temporary_directory.name)
            / runtime_installer.UV_CACHE_RELATIVE_PATH
        ).resolve()
        self.requirements = ("maafw==5.8.1",)
        self.identity = build_runtime_identity(self.requirements)
        self.commands: list[list[str]] = []
        self.logs: list[str] = []

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _record_run(self, command: list[str], **_kwargs: Any) -> None:
        self.commands.append(list(command))

    def _install(
        self,
        *,
        supports_venv: bool,
        uv_executable: str | None = None,
        probe: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with (
            mock.patch.object(runtime_installer, "_run", self._record_run),
            mock.patch.object(
                runtime_installer,
                "_python_supports_venv",
                return_value=supports_venv,
            ),
            mock.patch.object(
                runtime_installer,
                "_find_uv_executable",
                return_value=uv_executable,
            ),
            mock.patch.object(
                runtime_installer,
                "_probe_python_identity",
                return_value=probe if probe is not None else _probe_for(self.identity),
            ),
            mock.patch.object(
                runtime_installer,
                "_installed_maafw_version",
                return_value="5.8.1",
            ),
            mock.patch.object(
                runtime_installer,
                "_resolved_requirements",
                return_value=["maafw==5.8.1"],
            ),
            mock.patch.object(
                runtime_installer,
                "_resolved_requirements_with_uv",
                return_value=["maafw==5.8.1"],
            ),
            mock.patch.object(
                runtime_installer,
                "_uv_version",
                return_value="0.11.26",
            ),
            mock.patch.object(
                runtime_installer,
                "_pip_version",
                return_value="25.1.1",
            ),
        ):
            return runtime_installer.install_python_runtime(
                self.environment_path,
                self.requirements,
                dict(self.identity),
                cwd=self.temporary_directory.name,
                bootstrap_python="C:/portable/python.exe",
                send_log=self.logs.append,
            )

    def test_complete_bootstrap_python_still_uses_stdlib_venv(self) -> None:
        result = self._install(supports_venv=True)

        self.assertEqual(
            self.commands[0],
            [
                "C:/portable/python.exe",
                "-m",
                "venv",
                str(self.environment_path),
            ],
        )
        self.assertEqual(result["maafwVersion"], "5.8.1")
        self.assertEqual(result["resolvedRequirements"], ["maafw==5.8.1"])
        self.assertEqual(result["installer"]["name"], "pip")
        self.assertEqual(result["installer"]["environment"], "stdlib-venv")
        self.assertFalse(result["cache"]["shared"])

    def test_complete_python_uses_uv_for_dependencies_when_available(self) -> None:
        result = self._install(
            supports_venv=True,
            uv_executable="C:/portable/uv.exe",
        )

        self.assertEqual(
            self.commands[0],
            [
                "C:/portable/python.exe",
                "-m",
                "venv",
                "--without-pip",
                str(self.environment_path),
            ],
        )
        self.assertEqual(
            self.commands[1],
            [
                "C:/portable/uv.exe",
                "pip",
                "install",
                "--python",
                str(runtime_installer._venv_python(self.environment_path)),
                "--cache-dir",
                str(self.uv_cache_dir),
                "--link-mode",
                runtime_installer.UV_LINK_MODE,
                "--upgrade",
                "--quiet",
                "maafw==5.8.1",
            ],
        )
        self.assertEqual(result["installer"]["name"], "uv")
        self.assertEqual(result["installer"]["dependencies"], "uv-pip")
        self.assertEqual(result["cache"]["scope"], "pool")
        self.assertEqual(result["cache"]["path"], str(self.uv_cache_dir))
        self.assertEqual(
            result["cache"]["relativeToPool"],
            runtime_installer.UV_CACHE_RELATIVE_PATH.as_posix(),
        )
        self.assertEqual(
            result["link"]["mode"],
            runtime_installer.UV_LINK_MODE,
        )

    def test_embeddable_bootstrap_python_falls_back_to_uv(self) -> None:
        result = self._install(
            supports_venv=False,
            uv_executable="C:/portable/uv.exe",
        )

        self.assertEqual(
            self.commands[0],
            [
                "C:/portable/uv.exe",
                "venv",
                "--python",
                HOST_SHORT_VERSION,
                "--cache-dir",
                str(self.uv_cache_dir),
                "--link-mode",
                runtime_installer.UV_LINK_MODE,
                str(self.environment_path),
            ],
        )
        self.assertEqual(result["installer"]["environment"], "uv-venv")
        self.assertTrue(
            any("uv" in message for message in self.logs),
            self.logs,
        )

    def test_missing_uv_fallback_reports_an_actionable_error(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            self._install(supports_venv=False, uv_executable=None)

        message = str(raised.exception)
        self.assertIn("venv", message)
        self.assertIn("uv", message)
        self.assertEqual(self.commands, [])

    def test_runtime_python_version_comes_from_the_created_environment(self) -> None:
        result = self._install(supports_venv=True)

        self.assertEqual(result["pythonVersion"], f"{HOST_SHORT_VERSION}.99")

    def test_abi_mismatch_between_identity_and_runtime_is_rejected(self) -> None:
        drifted = _probe_for(self.identity)
        drifted["cacheTag"] = "cpython-999"

        with self.assertRaises(RuntimeError) as raised:
            self._install(
                supports_venv=False,
                uv_executable="C:/portable/uv.exe",
                probe=drifted,
            )

        self.assertIn("ABI", str(raised.exception))
        # uv pip install 必须没有发生：只创建了环境就中止。
        self.assertEqual(len(self.commands), 1)

    def test_python_version_mismatch_between_identity_and_runtime_is_rejected(
        self,
    ) -> None:
        drifted = _probe_for(self.identity)
        drifted["shortVersion"] = "2.7"

        with self.assertRaises(RuntimeError) as raised:
            self._install(
                supports_venv=False,
                uv_executable="C:/portable/uv.exe",
                probe=drifted,
            )

        self.assertIn("2.7", str(raised.exception))


class MaaFWRuntimeInstallerProbeTest(unittest.TestCase):
    def test_uv_lookup_prefers_bootstrap_python_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bootstrap = root / "portable" / "python.exe"
            uv_executable = bootstrap.parent / "Scripts" / "uv.exe"
            bootstrap.parent.mkdir(parents=True)
            uv_executable.parent.mkdir()
            bootstrap.write_text("fake python", encoding="utf-8")
            uv_executable.write_text("fake uv", encoding="utf-8")

            with mock.patch.object(
                runtime_installer.shutil,
                "which",
                return_value=None,
            ):
                resolved = runtime_installer._find_uv_executable(str(bootstrap))

        self.assertEqual(resolved, str(uv_executable.resolve()))

    def test_venv_probe_accepts_the_current_interpreter(self) -> None:
        self.assertTrue(runtime_installer._python_supports_venv(sys.executable))

    def test_venv_probe_rejects_an_unusable_interpreter(self) -> None:
        missing = str(
            Path(tempfile.gettempdir()) / "automas-maafw-missing-python.exe"
        )
        self.assertFalse(runtime_installer._python_supports_venv(missing))

    def test_identity_probe_reports_the_running_abi(self) -> None:
        probe = runtime_installer._probe_python_identity(Path(sys.executable))

        identity = build_runtime_identity(("maafw",))
        self.assertEqual(
            f"{probe['implementation']}:{probe['cacheTag']}:{probe['soabi']}",
            identity["pythonAbi"],
        )
        self.assertEqual(probe["shortVersion"], identity["pythonVersion"])


if __name__ == "__main__":
    unittest.main()
