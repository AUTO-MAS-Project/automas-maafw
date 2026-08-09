from __future__ import annotations

import os
import platform
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_POOL_SOURCE = ROOT / "packages" / "automas_maafw_runtime_pool" / "src"
if str(RUNTIME_POOL_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_POOL_SOURCE))

from automas_maafw_runtime_pool import (  # noqa: E402
    MaaFWRuntimePoolError,
    MaaFWRuntimePoolService,
    build_runtime_id,
    build_runtime_identity,
)
from automas_maafw_runtime_pool import installer as runtime_installer  # noqa: E402
from automas_maafw_runtime_pool import pool as runtime_pool  # noqa: E402


def _python_probe(minor: str) -> dict[str, str]:
    compact = minor.replace(".", "")
    return {
        "implementation": "cpython",
        "cacheTag": f"cpython-{compact}",
        "soabi": f"cp{compact}-win_amd64",
        "version": f"{minor}.14",
        "shortVersion": minor,
        "platform": "win-amd64",
        "architecture": "AMD64",
    }


class MaaFWRuntimePoolMultiAbiContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.pool_root = Path(self.temporary_directory.name) / "pool"
        self.install_identities: list[dict[str, Any]] = []
        self.service = MaaFWRuntimePoolService(
            self.pool_root,
            installer=self._fake_installer,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _fake_installer(
        self,
        environment_path: Path,
        requirements: list[str] | tuple[str, ...],
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        del requirements
        self.install_identities.append(identity)
        scripts = environment_path / ("Scripts" if os.name == "nt" else "bin")
        scripts.mkdir(parents=True)
        executable = scripts / ("python.exe" if os.name == "nt" else "python")
        executable.write_text("fake", encoding="utf-8")
        return {
            "pythonExecutable": str(executable),
            "pythonVersion": str(identity["pythonVersion"]),
        }

    def test_default_identity_matches_same_probed_interpreter(self) -> None:
        requirements = ["maafw==5.10.4", "json5==0.14.0"]
        legacy_identity = {
            "schemaVersion": 1,
            "requirements": ["json5==0.14.0", "maafw==5.10.4"],
            "pythonAbi": (
                f"{getattr(sys.implementation, 'name', 'python')}:"
                f"{getattr(sys.implementation, 'cache_tag', None) or 'unknown'}:"
                f"{str(sysconfig.get_config_var('SOABI') or 'unknown')}"
            ),
            "pythonVersion": platform.python_version(),
            "platform": sysconfig.get_platform() or sys.platform,
            "architecture": platform.machine() or "unknown",
        }

        self.assertEqual(build_runtime_identity(requirements), legacy_identity)
        self.assertEqual(
            build_runtime_id(requirements),
            build_runtime_id(requirements, python_identity=legacy_identity | {
                "implementation": legacy_identity["pythonAbi"].split(":", 1)[0],
                "cacheTag": legacy_identity["pythonAbi"].split(":", 2)[1],
                "soabi": legacy_identity["pythonAbi"].split(":", 2)[2],
                "shortVersion": legacy_identity["pythonVersion"],
            }),
        )

    def test_explicit_cp313_identity_isolated_from_host_default(self) -> None:
        requirements = ["maafw==5.10.4"]
        cp313 = _python_probe("3.13")
        identity = build_runtime_identity(requirements, python_identity=cp313)

        self.assertEqual(identity["pythonVersion"], "3.13.14")
        self.assertEqual(identity["pythonAbi"], "cpython:cpython-313:cp313-win_amd64")
        if sys.version_info[:2] != (3, 13):
            self.assertNotEqual(
                build_runtime_id(requirements, python_identity=cp313),
                build_runtime_id(requirements),
            )

    def test_explicit_patch_versions_have_distinct_runtime_ids(self) -> None:
        requirements = ["maafw==5.10.4"]
        cp31314 = _python_probe("3.13")
        cp31313 = dict(cp31314, version="3.13.13")

        self.assertNotEqual(
            build_runtime_id(requirements, python_identity=cp31314),
            build_runtime_id(requirements, python_identity=cp31313),
        )

    def test_bootstrap_python_is_forwarded_to_kwargs_installer(self) -> None:
        received: dict[str, Any] = {}

        def installer(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            received.update(kwargs)
            return {}

        bound = runtime_pool._bind_bootstrap_python(
            installer,
            self.pool_root / "python" / "python.exe",
        )
        bound(Path("environment"), (), {})

        self.assertEqual(
            received["bootstrap_python"],
            str((self.pool_root / "python" / "python.exe").resolve()),
        )

    def test_service_routes_explicit_python_identity_into_manifest(self) -> None:
        cp313 = _python_probe("3.13")
        selected = {
            "executable": "C:/pool/python/cpython-3.13/python.exe",
            "identity": cp313,
            "source": "pool-managed",
            "constraint": ">=3.13,<3.14",
        }
        with (
            mock.patch.object(
                self.service.pool,
                "resolve_python",
                return_value=selected,
            ) as resolve_python,
            mock.patch.object(
                runtime_pool,
                "probe_python_identity",
                return_value=cp313,
            ),
        ):
            runtime = self.service.ensure_runtime(
                {
                    "requirements": ["maafw==5.10.4"],
                    "python": {
                        "implementation": "cpython",
                        "constraint": ">=3.13,<3.14",
                    },
                }
            )

        resolve_python.assert_called_once_with(
            {
                "implementation": "cpython",
                "constraint": ">=3.13,<3.14",
            },
            allow_install=True,
        )
        self.assertEqual(runtime["identity"]["pythonVersion"], "3.13.14")
        self.assertEqual(runtime["runtimeId"], build_runtime_id(
            ["maafw==5.10.4"],
            python_identity=cp313,
        ))
        self.assertEqual(self.install_identities, [runtime["identity"]])

    def test_runtime_id_request_is_validated_from_manifest_without_resolution(self) -> None:
        cp313 = _python_probe("3.13")
        with (
            mock.patch.object(
                self.service.pool,
                "resolve_python",
                return_value={
                    "executable": "C:/pool/python/cpython-3.13/python.exe",
                    "identity": cp313,
                },
            ),
            mock.patch.object(
                runtime_pool,
                "probe_python_identity",
                return_value=cp313,
            ),
        ):
            runtime = self.service.ensure_runtime(
                {
                    "requirements": ["maafw==5.10.4"],
                    "python": {"constraint": ">=3.13,<3.14"},
                }
            )

        with (
            mock.patch.object(
                self.service.pool,
                "resolve_python",
                side_effect=AssertionError(
                    "runtimeId validation must not resolve Python"
                ),
            ),
            mock.patch.object(
                runtime_pool,
                "probe_python_identity",
                return_value=cp313,
            ),
        ):
            matched = self.service.resolve_runtime(
                {
                    "runtimeId": runtime["runtimeId"],
                    "requirements": ["maafw==5.10.4"],
                    "python": {"constraint": ">=3.13,<3.14"},
                }
            )
            rejected = self.service.resolve_runtime(
                {
                    "runtimeId": runtime["runtimeId"],
                    "python": {"constraint": ">=3.12,<3.13"},
                }
            )

        self.assertEqual(matched["runtimeId"], runtime["runtimeId"])
        self.assertIsNone(rejected)
        with (
            mock.patch.object(
                runtime_pool,
                "probe_python_identity",
                return_value=cp313,
            ),
            self.assertRaisesRegex(
                MaaFWRuntimePoolError,
                "does not match the runtime manifest",
            ),
        ):
            self.service.ensure_runtime(
                {
                    "runtimeId": runtime["runtimeId"],
                    "python": {"constraint": ">=3.12,<3.13"},
                }
            )

        drifted = dict(cp313, version="3.13.13")
        with mock.patch.object(
            runtime_pool,
            "probe_python_identity",
            return_value=drifted,
        ):
            with self.assertRaisesRegex(
                MaaFWRuntimePoolError,
                "does not match the selected ABI",
            ):
                self.service.resolve_runtime(
                    {"runtimeId": runtime["runtimeId"]}
                )

    def test_explicit_python_runtime_rejects_installer_abi_mismatch(self) -> None:
        cp313 = _python_probe("3.13")
        cp312 = _python_probe("3.12")
        selected = {
            "executable": "C:/pool/python/cpython-3.13/python.exe",
            "identity": cp313,
            "source": "pool-managed",
            "constraint": ">=3.13,<3.14",
        }
        with (
            mock.patch.object(
                self.service.pool,
                "resolve_python",
                return_value=selected,
            ),
            mock.patch.object(
                runtime_pool,
                "probe_python_identity",
                return_value=cp312,
            ),
        ):
            with self.assertRaisesRegex(
                MaaFWRuntimePoolError,
                "does not match the selected ABI",
            ):
                self.service.ensure_runtime(
                    {
                        "requirements": ["maafw==5.10.4"],
                        "python": {
                            "implementation": "cpython",
                            "constraint": ">=3.13,<3.14",
                        },
                    }
                )

        self.assertEqual(list((self.pool_root / "runtimes").glob("*")), [])

    def test_resolve_does_not_install_missing_pool_python(self) -> None:
        host = _python_probe("3.12")
        with (
            mock.patch.object(
                runtime_installer,
                "probe_python_identity",
                return_value=host,
            ),
            mock.patch.object(
                runtime_installer,
                "_find_uv_executable",
                return_value="C:/tools/uv.exe",
            ),
            mock.patch.object(
                runtime_installer,
                "_find_pool_managed_python",
                return_value=None,
            ),
            mock.patch.object(
                runtime_installer,
                "_install_pool_managed_python",
            ) as install_python,
        ):
            selected = runtime_installer.resolve_python_interpreter(
                self.pool_root,
                {"implementation": "cpython", "constraint": ">=3.13,<3.14"},
                allow_install=False,
            )

        self.assertIsNone(selected)
        install_python.assert_not_called()

    def test_ensure_installs_and_reprobes_pool_local_target(self) -> None:
        host = _python_probe("3.12")
        cp313 = _python_probe("3.13")
        managed = self.pool_root / "python" / "cpython-3.13" / "python.exe"

        def probe(path: str | Path) -> dict[str, str]:
            return host if Path(path).resolve() == Path(sys.executable).resolve() else cp313

        with (
            mock.patch.object(
                runtime_installer,
                "probe_python_identity",
                side_effect=probe,
            ),
            mock.patch.object(
                runtime_installer,
                "_find_uv_executable",
                return_value="C:/tools/uv.exe",
            ),
            mock.patch.object(
                runtime_installer,
                "_find_pool_managed_python",
                side_effect=[None, managed],
            ),
            mock.patch.object(
                runtime_installer,
                "_install_pool_managed_python",
            ) as install_python,
        ):
            selected = runtime_installer.resolve_python_interpreter(
                self.pool_root,
                {"implementation": "cpython", "constraint": ">=3.13,<3.14"},
                allow_install=True,
            )

        install_python.assert_called_once()
        self.assertEqual(selected["executable"], str(managed))
        self.assertEqual(selected["identity"]["shortVersion"], "3.13")

    def test_exact_patch_constraint_uses_exact_uv_request(self) -> None:
        host = _python_probe("3.12")
        cp313 = _python_probe("3.13")
        managed = self.pool_root / "python" / "cpython-3.13.14" / "python.exe"
        find_targets: list[str] = []
        install_targets: list[str] = []

        def find_python(
            _uv: str,
            target: str,
            **_kwargs: Any,
        ) -> Path | None:
            find_targets.append(target)
            return managed if len(find_targets) == 2 else None

        def install_python(
            _uv: str,
            target: str,
            **_kwargs: Any,
        ) -> None:
            install_targets.append(target)

        def probe(path: str | Path) -> dict[str, str]:
            return host if Path(path).resolve() == Path(sys.executable).resolve() else cp313

        with (
            mock.patch.object(
                runtime_installer,
                "probe_python_identity",
                side_effect=probe,
            ),
            mock.patch.object(
                runtime_installer,
                "_find_uv_executable",
                return_value="C:/tools/uv.exe",
            ),
            mock.patch.object(
                runtime_installer,
                "_find_pool_managed_python",
                side_effect=find_python,
            ),
            mock.patch.object(
                runtime_installer,
                "_install_pool_managed_python",
                side_effect=install_python,
            ),
        ):
            selected = runtime_installer.resolve_python_interpreter(
                self.pool_root,
                {"implementation": "cpython", "constraint": "==3.13.14"},
                allow_install=True,
            )

        self.assertEqual(find_targets, ["3.13.14", "3.13.14"])
        self.assertEqual(install_targets, ["3.13.14"])
        self.assertEqual(selected["identity"]["version"], "3.13.14")

    def test_patch_bounded_range_uses_compatible_uv_catalog_version(self) -> None:
        host = _python_probe("3.12")
        cp313 = dict(_python_probe("3.13"), version="3.13.13")
        managed = self.pool_root / "python" / "cpython-3.13.13" / "python.exe"
        find_targets: list[str] = []
        install_targets: list[str] = []

        def find_python(
            _uv: str,
            target: str,
            **_kwargs: Any,
        ) -> Path | None:
            find_targets.append(target)
            return managed if target == "3.13.13" else None

        def probe(path: str | Path) -> dict[str, str]:
            return host if Path(path).resolve() == Path(sys.executable).resolve() else cp313

        with (
            mock.patch.object(
                runtime_installer,
                "probe_python_identity",
                side_effect=probe,
            ),
            mock.patch.object(
                runtime_installer,
                "_find_uv_executable",
                return_value="C:/tools/uv.exe",
            ),
            mock.patch.object(
                runtime_installer,
                "_find_pool_managed_python",
                side_effect=find_python,
            ),
            mock.patch.object(
                runtime_installer,
                "_select_uv_python_version",
                return_value="3.13.13",
            ) as select_version,
            mock.patch.object(
                runtime_installer,
                "_install_pool_managed_python",
                side_effect=lambda _uv, target, **_kwargs: install_targets.append(
                    target
                ),
            ),
        ):
            selected = runtime_installer.resolve_python_interpreter(
                self.pool_root,
                {
                    "implementation": "cpython",
                    "constraint": ">=3.13.10,<3.13.14",
                },
                allow_install=True,
            )

        self.assertEqual(find_targets, ["3.13", "3.13.13"])
        self.assertEqual(install_targets, ["3.13.13"])
        select_version.assert_called_once()
        self.assertEqual(selected["identity"]["version"], "3.13.13")

    def test_uv_lookup_is_pool_local_and_forbids_downloads(self) -> None:
        python_root = self.pool_root / "python"
        cache_dir = self.pool_root / "cache" / "uv"
        managed = python_root / "cpython-3.13" / "python.exe"
        managed.parent.mkdir(parents=True)
        managed.write_text("fake", encoding="utf-8")
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{managed}\n",
            stderr="",
        )

        with mock.patch.object(
            runtime_installer.subprocess,
            "run",
            return_value=completed,
        ) as run:
            resolved = runtime_installer._find_pool_managed_python(
                "C:/tools/uv.exe",
                "3.13",
                pool_root=self.pool_root,
                python_root=python_root,
                cache_dir=cache_dir,
            )

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(resolved, managed.resolve())
        self.assertIn("--managed-python", command)
        self.assertIn("--no-python-downloads", command)
        self.assertEqual(
            environment["UV_PYTHON_INSTALL_DIR"],
            str(python_root.resolve()),
        )

    def test_uv_lookup_canonicalizes_pool_paths_before_boundary_check(
        self,
    ) -> None:
        python_root = self.pool_root / "python"
        cache_dir = self.pool_root / "cache" / "uv"
        managed = python_root / "cpython-3.13" / "python.exe"
        managed.parent.mkdir(parents=True)
        managed.write_text("fake", encoding="utf-8")
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{managed}\n",
            stderr="",
        )

        with mock.patch.object(
            runtime_installer.subprocess,
            "run",
            return_value=completed,
        ) as run:
            resolved = runtime_installer._find_pool_managed_python(
                "C:/tools/uv.exe",
                "3.13",
                pool_root=self.pool_root / "nested" / "..",
                python_root=python_root / "nested" / "..",
                cache_dir=cache_dir / "nested" / "..",
            )

        self.assertEqual(resolved, managed.resolve())
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["cwd"], self.pool_root.resolve())
        self.assertEqual(
            kwargs["env"]["UV_PYTHON_INSTALL_DIR"],
            str(python_root.resolve()),
        )
        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("--cache-dir") + 1],
            str(cache_dir.resolve()),
        )

    def test_uv_lookup_rejects_a_result_outside_the_pool_python_root(self) -> None:
        python_root = self.pool_root / "python"
        cache_dir = self.pool_root / "cache" / "uv"
        outside = self.pool_root.parent / "outside-python.exe"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{outside}\n",
            stderr="",
        )

        with (
            mock.patch.object(
                runtime_installer.subprocess,
                "run",
                return_value=completed,
            ),
            self.assertRaisesRegex(RuntimeError, "outside the runtime pool"),
        ):
            runtime_installer._find_pool_managed_python(
                "C:/tools/uv.exe",
                "3.13",
                pool_root=self.pool_root,
                python_root=python_root,
                cache_dir=cache_dir,
            )

    def test_uv_install_targets_pool_private_python_directory(self) -> None:
        python_root = self.pool_root / "python"
        cache_dir = self.pool_root / "cache" / "uv"
        commands: list[list[str]] = []

        def record(command: list[str], **_kwargs: Any) -> None:
            commands.append(command)

        with mock.patch.object(runtime_installer, "_run", side_effect=record):
            runtime_installer._install_pool_managed_python(
                "C:/tools/uv.exe",
                "3.13",
                pool_root=self.pool_root,
                python_root=python_root,
                cache_dir=cache_dir,
            )

        self.assertEqual(commands[0][0:4], [
            "C:/tools/uv.exe",
            "python",
            "install",
            "cpython-3.13",
        ])
        self.assertEqual(
            commands[0][commands[0].index("--install-dir") + 1],
            str(python_root.resolve()),
        )
        self.assertIn("--no-bin", commands[0])
        self.assertIn("--no-registry", commands[0])


if __name__ == "__main__":
    unittest.main()
