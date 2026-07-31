from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT / "packages" / "automas_maafw_interface" / "src",
    ROOT / "packages" / "automas_maafw_project_update" / "src",
)
for source_root in reversed(SOURCE_ROOTS):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from automas_maafw_interface.models import MaaFWInterface  # noqa: E402
from automas_maafw_project_update import (  # noqa: E402
    MaaFWDownloadedProjectPackage,
    MaaFWProjectUpdateCandidate,
    MaaFWProjectUpdateError,
    MaaFWProjectUpdateResult,
    MaaFWProjectUpdateService,
    check_maafw_project_update,
    discover_maafw_project_update,
    update_maafw_project_if_needed,
)
from automas_maafw_project_update import service as service_module  # noqa: E402
from automas_maafw_project_update import updater as updater_module  # noqa: E402


class _FakeAsyncClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    async def get(self, url: str, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


class MaaFWProjectUpdateProviderContractTest(unittest.TestCase):
    def test_distribution_version_marks_provider_contract_fix(self) -> None:
        project = tomllib.loads(
            (
                ROOT
                / "packages"
                / "automas_maafw_project_update"
                / "pyproject.toml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(project["project"]["version"], "0.2.0")

    def test_result_positional_contract_keeps_existing_field_order(self) -> None:
        result = MaaFWProjectUpdateResult(
            True,
            False,
            "1.0.0",
            "2.0.0",
            "mirrorchyan",
            "discovered",
        )

        self.assertEqual(result.latest_version, "2.0.0")
        self.assertEqual(result.source, "mirrorchyan")
        self.assertEqual(result.message, "discovered")
        self.assertFalse(result.update_available)
        self.assertFalse(result.installable)

    def test_mirrorchyan_version_without_url_is_discovery_not_candidate(self) -> None:
        response = httpx.Response(
            200,
            json={"code": 0, "data": {"version_name": "2.0.0"}},
        )
        interface = self._interface(mirrorchyan_rid="demo")

        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=_FakeAsyncClient(response),
        ):
            discovery = asyncio.run(
                discover_maafw_project_update(
                    interface,
                    current_version="1.0.0",
                    source_config={"source": "mirrorchyan"},
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None
        self.assertEqual(discovery.version, "2.0.0")
        self.assertFalse(discovery.installable)
        self.assertIsNone(discovery.candidate)
        self.assertIn("without a download URL", discovery.unavailable_reason)

        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=_FakeAsyncClient(response),
        ):
            with self.assertRaisesRegex(
                MaaFWProjectUpdateError,
                "no installable update candidate",
            ):
                asyncio.run(
                    check_maafw_project_update(
                        interface,
                        current_version="1.0.0",
                        source_config={"source": "mirrorchyan"},
                    )
                )

    def test_update_if_needed_reports_non_installable_version_without_apply(self) -> None:
        response = httpx.Response(
            200,
            json={"code": 0, "data": {"version_name": "2.0.0"}},
        )
        logs: list[str] = []
        apply_mock = AsyncMock()
        with (
            patch.object(
                updater_module.httpx,
                "AsyncClient",
                return_value=_FakeAsyncClient(response),
            ),
            patch.object(
                updater_module,
                "apply_maafw_project_update",
                apply_mock,
            ),
        ):
            result = asyncio.run(
                update_maafw_project_if_needed(
                    Path("C:/project"),
                    self._interface(mirrorchyan_rid="demo"),
                    source_config={"source": "mirrorchyan"},
                    send_log=logs.append,
                )
            )

        apply_mock.assert_not_awaited()
        self.assertTrue(result.checked)
        self.assertFalse(result.updated)
        self.assertTrue(result.update_available)
        self.assertFalse(result.installable)
        self.assertEqual(result.latest_version, "2.0.0")
        self.assertTrue(any("not installable" in message for message in logs))

    def test_mirrorchyan_url_creates_installable_candidate(self) -> None:
        response = httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "version_name": "2.0.0",
                    "url": "https://example.invalid/project.zip",
                    "sha256": "a" * 64,
                },
            },
        )
        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=_FakeAsyncClient(response),
        ):
            discovery = asyncio.run(
                discover_maafw_project_update(
                    self._interface(mirrorchyan_rid="demo"),
                    current_version="1.0.0",
                    source_config={"source": "mirrorchyan"},
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None and discovery.candidate is not None
        self.assertTrue(discovery.installable)
        self.assertEqual(
            discovery.candidate.download_url,
            "https://example.invalid/project.zip",
        )

    def test_github_release_without_asset_or_zipball_is_not_installable(self) -> None:
        response = httpx.Response(
            200,
            json={"tag_name": "2.0.0", "assets": []},
        )
        client = _FakeAsyncClient(response)
        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=client,
        ):
            discovery = asyncio.run(
                discover_maafw_project_update(
                    self._interface(),
                    current_version="1.0.0",
                    source_config={
                        "source": "github_release",
                        "github_repo": "owner/project",
                        "github_tag": "v2.0.0",
                        "github_token": "secret",
                        "github_asset_pattern": r"\.msix$",
                    },
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None
        self.assertFalse(discovery.installable)
        self.assertIsNone(discovery.candidate)
        self.assertIn("no matching asset", discovery.unavailable_reason)
        self.assertEqual(
            client.requests[0][0],
            "https://api.github.com/repos/owner/project/releases/tags/v2.0.0",
        )
        self.assertEqual(
            client.requests[0][1]["headers"]["Authorization"],
            "Bearer secret",
        )

    def test_service_apply_accepts_json_candidate_dto(self) -> None:
        apply_mock = AsyncMock()
        service = MaaFWProjectUpdateService()
        with patch.object(
            service_module,
            "apply_maafw_project_update",
            apply_mock,
        ):
            asyncio.run(
                service.apply_update(
                    Path("C:/project"),
                    {
                        "source": "github_release",
                        "version": "2.0.0",
                        "downloadUrl": " https://example.invalid/project.zip ",
                    },
                )
            )

        apply_mock.assert_awaited_once()
        candidate = apply_mock.await_args.args[1]
        self.assertIsInstance(candidate, MaaFWProjectUpdateCandidate)
        self.assertEqual(candidate.version, "2.0.0")
        self.assertEqual(
            candidate.download_url,
            "https://example.invalid/project.zip",
        )

    def test_service_download_returns_json_only_validated_package(self) -> None:
        download_mock = AsyncMock(
            return_value=MaaFWDownloadedProjectPackage(
                source="github_release",
                version="2.0.0",
                path="C:/managed-downloads/project.zip",
                size=1024,
                sha256="a" * 64,
            )
        )
        service = MaaFWProjectUpdateService()
        with patch.object(
            service_module,
            "download_maafw_project_package",
            download_mock,
        ):
            result = asyncio.run(
                service.download_package(
                    Path("C:/managed-downloads"),
                    {
                        "source": "github_release",
                        "version": "2.0.0",
                        "downloadUrl": "https://example.invalid/project.zip",
                    },
                )
            )

        self.assertEqual(
            result,
            {
                "source": "github_release",
                "version": "2.0.0",
                "path": "C:/managed-downloads/project.zip",
                "size": 1024,
                "sha256": "a" * 64,
            },
        )
        candidate = download_mock.await_args.args[1]
        self.assertIsInstance(candidate, MaaFWProjectUpdateCandidate)

    def test_remote_package_rejects_non_https_and_private_literal_hosts(self) -> None:
        with self.assertRaisesRegex(MaaFWProjectUpdateError, "must use HTTPS"):
            updater_module._validate_download_url(
                "http://example.invalid/project.zip"
            )
        with self.assertRaisesRegex(MaaFWProjectUpdateError, "private address"):
            updater_module._validate_download_url(
                "https://127.0.0.1/project.zip"
            )

    def test_managed_downloads_publish_to_a_content_addressed_cache(self) -> None:
        async def fake_download(
            temp_path,
            package_path,
            _download_url,
            **_kwargs,
        ):
            with zipfile.ZipFile(temp_path, "w") as archive:
                archive.writestr("interface.json", '{"version":"2.0.0"}')
            return updater_module._validate_and_publish_download(
                temp_path,
                package_path,
                None,
            )

        candidate = MaaFWProjectUpdateCandidate(
            source="github_release",
            version="2.0.0",
            download_url="https://example.invalid/project.zip",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(
                updater_module,
                "_download_candidate_to_paths",
                side_effect=fake_download,
            ):
                first = asyncio.run(
                    updater_module.download_maafw_project_package(
                        Path(temporary_directory),
                        candidate,
                    )
                )
                second = asyncio.run(
                    updater_module.download_maafw_project_package(
                        Path(temporary_directory),
                        candidate,
                    )
                )

            self.assertEqual(first.path, second.path)
            self.assertEqual(Path(first.path).name, f"{first.sha256}.zip")
            self.assertTrue(Path(first.path).is_file())

    def test_archive_apply_does_not_block_the_event_loop(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_apply(*_args) -> None:
            started.set()
            release.wait(timeout=2)

        async def exercise() -> float:
            with patch.object(
                updater_module,
                "_apply_update_package_sync",
                side_effect=blocking_apply,
            ):
                task = asyncio.create_task(
                    updater_module._apply_update_package(
                        Path("C:/project"),
                        Path("C:/project/update.zip"),
                        lambda _message: None,
                    )
                )
                timer = threading.Timer(1.0, release.set)
                timer.start()
                try:
                    before = time.perf_counter()
                    while not started.is_set():
                        await asyncio.sleep(0)
                    await asyncio.sleep(0.01)
                    elapsed = time.perf_counter() - before
                    release.set()
                    await task
                    return elapsed
                finally:
                    release.set()
                    timer.cancel()

        self.assertLess(asyncio.run(exercise()), 0.5)

    @staticmethod
    def _interface(**updates) -> MaaFWInterface:
        return MaaFWInterface(
            interface_version=2,
            name="provider-contract",
            version="1.0.0",
            **updates,
        )


if __name__ == "__main__":
    unittest.main()
