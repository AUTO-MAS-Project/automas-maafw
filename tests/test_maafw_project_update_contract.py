from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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
    apply_maafw_project_update,
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


class _FakeSequentialAsyncClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    async def get(self, url: str, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses[len(self.requests) - 1]


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.status_code = 200
        self.headers = {"content-length": str(sum(map(len, chunks)))}
        self.url = httpx.URL("https://example.invalid/project.zip")
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    async def aiter_bytes(self, *, chunk_size: int):
        del chunk_size
        for chunk in self._chunks:
            yield chunk


class _FakeStreamClient:
    def __init__(self, chunks: list[bytes]) -> None:
        self.response = _FakeStreamResponse(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def stream(self, method: str, url: str, **kwargs):
        del method, url, kwargs
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
        self.assertEqual(project["project"]["version"], "0.2.3")

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
                    source_config={"source": "mirrorchyan", "cdk": "test-cdk"},
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
                        source_config={"source": "mirrorchyan", "cdk": "test-cdk"},
                    )
                )

    def test_update_if_needed_reports_non_installable_version_without_apply(self) -> None:
        response = httpx.Response(
            200,
            json={"code": 0, "data": {"version_name": "2.0.0"}},
        )
        logs: list[str] = []
        events: list[dict] = []
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
                    source_config={"source": "mirrorchyan", "cdk": "test-cdk"},
                    send_log=logs.append,
                    progress=events.append,
                )
            )

        apply_mock.assert_not_awaited()
        self.assertTrue(result.checked)
        self.assertFalse(result.updated)
        self.assertTrue(result.update_available)
        self.assertFalse(result.installable)
        self.assertEqual(result.latest_version, "2.0.0")
        self.assertTrue(any("not installable" in message for message in logs))
        terminal = [
            event for event in events if event["stage"] in {"completed", "failed"}
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["stage"], "completed")
        self.assertTrue(terminal[0]["final"])

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
                    source_config={"source": "mirrorchyan", "cdk": "test-cdk"},
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None and discovery.candidate is not None
        self.assertTrue(discovery.installable)
        self.assertEqual(
            discovery.candidate.download_url,
            "https://example.invalid/project.zip",
        )

    def test_blank_script_cdk_inherits_non_empty_host_cdk(self) -> None:
        response = httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "version_name": "2.0.0",
                    "url": "https://example.invalid/project.zip",
                },
            },
        )
        client = _FakeAsyncClient(response)
        with (
            patch.object(
                updater_module.httpx,
                "AsyncClient",
                return_value=client,
            ),
            patch.object(
                updater_module,
                "apply_maafw_project_update",
                new=AsyncMock(),
            ),
        ):
            result = asyncio.run(
                update_maafw_project_if_needed(
                    Path("C:/project"),
                    self._interface(mirrorchyan_rid="demo"),
                    mirror_cdk="host-cdk",
                    source_config={"source": "mirrorchyan", "cdk": ""},
                )
            )

        self.assertTrue(result.updated)
        self.assertEqual(client.requests[0][1]["params"]["cdk"], "host-cdk")

    def test_blank_script_channel_inherits_non_empty_host_channel(self) -> None:
        response = httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "version_name": "2.0.0-beta.1",
                    "url": "https://example.invalid/project.zip",
                },
            },
        )
        client = _FakeAsyncClient(response)
        with (
            patch.object(
                updater_module.httpx,
                "AsyncClient",
                return_value=client,
            ),
            patch.object(
                updater_module,
                "apply_maafw_project_update",
                new=AsyncMock(),
            ),
        ):
            result = asyncio.run(
                update_maafw_project_if_needed(
                    Path("C:/project"),
                    self._interface(mirrorchyan_rid="demo"),
                    mirror_cdk="host-cdk",
                    channel="beta",
                    source_config={
                        "source": "mirrorchyan",
                        "cdk": "",
                        "channel": "",
                    },
                )
            )

        self.assertTrue(result.updated)
        self.assertEqual(client.requests[0][1]["params"]["channel"], "beta")

    def test_explicit_github_source_without_cdk_uses_github_directly(self) -> None:
        github_response = httpx.Response(
            200,
            json={
                "tag_name": "v2.0.0",
                "assets": [
                    {
                        "name": "MFAA-win-x86_64-v2.0.0.zip",
                        "browser_download_url": "https://example.invalid/mfaa.zip",
                    },
                    {
                        "name": "M9A-linux-x86_64-v2.0.0.zip",
                        "browser_download_url": "https://example.invalid/m9a-linux.zip",
                    },
                    {
                        "name": "M9A-win-x86_64-v2.0.0.zip",
                        "browser_download_url": "https://example.invalid/m9a-win.zip",
                    },
                ],
            },
        )
        github_client = _FakeAsyncClient(github_response)
        with patch.object(updater_module.httpx, "AsyncClient", return_value=github_client):
            discovery = asyncio.run(
                discover_maafw_project_update(
                    self._interface(
                        name="M9A",
                        mirrorchyan_rid="demo",
                        mirrorchyan_multiplatform=True,
                        github="https://github.com/owner/project",
                    ),
                    current_version="1.0.0",
                    source_config={"source": "github_release"},
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None and discovery.candidate is not None
        self.assertEqual(discovery.source, "github_release")
        self.assertEqual(discovery.candidate.source, "github_release")
        self.assertEqual(
            discovery.candidate.download_url,
            "https://example.invalid/m9a-win.zip",
        )
        self.assertEqual(
            github_client.requests[0][0],
            "https://api.github.com/repos/owner/project/releases/latest",
        )

    def test_automatic_source_with_cdk_uses_mirror_channel(self) -> None:
        mirror_client = _FakeAsyncClient(
            httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "version_name": "2.1.0-beta.1",
                        "url": "https://example.invalid/beta.zip",
                    },
                },
            )
        )
        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=mirror_client,
        ):
            discovery = asyncio.run(
                discover_maafw_project_update(
                    self._interface(
                        name="project",
                        mirrorchyan_rid="demo",
                        mirrorchyan_multiplatform=True,
                        github="https://github.com/owner/project",
                    ),
                    current_version="2.0.0",
                    source_config={"channel": "beta", "cdk": "test-cdk"},
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None and discovery.candidate is not None
        self.assertEqual(discovery.version, "2.1.0-beta.1")
        self.assertEqual(
            discovery.candidate.download_url,
            "https://example.invalid/beta.zip",
        )
        self.assertEqual(mirror_client.requests[0][1]["params"]["channel"], "beta")

    def test_github_candidate_retries_conventional_v_tag_only(self) -> None:
        github_client = _FakeSequentialAsyncClient(
            [
                httpx.Response(404, json={"message": "Not Found"}),
                httpx.Response(
                    200,
                    json={
                        "tag_name": "v2.0.0",
                        "assets": [
                            {
                                "name": "project-v2.0.0.zip",
                                "browser_download_url": "https://example.invalid/project.zip",
                            }
                        ],
                    },
                ),
            ]
        )
        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=github_client,
        ):
            discovery = asyncio.run(
                updater_module._check_github_release_update(
                    self._interface(
                        name="project",
                        github="https://github.com/owner/project",
                    ),
                    current_version="1.0.0",
                    source_config={},
                    proxy=None,
                    target_version="2.0.0",
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None and discovery.candidate is not None
        self.assertEqual(
            [request[0] for request in github_client.requests],
            [
                "https://api.github.com/repos/owner/project/releases/tags/2.0.0",
                "https://api.github.com/repos/owner/project/releases/tags/v2.0.0",
            ],
        )

    def test_exact_github_fallback_refuses_draft_release(self) -> None:
        github_client = _FakeAsyncClient(
            httpx.Response(
                200,
                json={
                    "tag_name": "v2.0.0",
                    "draft": True,
                    "assets": [
                        {
                            "name": "project-v2.0.0.zip",
                            "browser_download_url": "https://example.invalid/project.zip",
                        }
                    ],
                },
            )
        )
        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=github_client,
        ):
            discovery = asyncio.run(
                updater_module._check_github_release_update(
                    self._interface(
                        name="project",
                        github="https://github.com/owner/project",
                    ),
                    current_version="1.0.0",
                    source_config={"cdk": "test-cdk"},
                    proxy=None,
                    target_version="2.0.0",
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None
        self.assertFalse(discovery.installable)
        self.assertIn("draft", discovery.unavailable_reason)

    def test_exact_github_fallback_ignores_stale_explicit_tag(self) -> None:
        github_client = _FakeAsyncClient(
            httpx.Response(
                200,
                json={
                    "tag_name": "2.0.0",
                    "assets": [
                        {
                            "name": "project-v2.0.0.zip",
                            "browser_download_url": "https://example.invalid/project.zip",
                        }
                    ],
                },
            )
        )
        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=github_client,
        ):
            discovery = asyncio.run(
                updater_module._check_github_release_update(
                    self._interface(
                        name="project",
                        github="https://github.com/owner/project",
                    ),
                    current_version="1.0.0",
                    source_config={"github_tag": "v1.5.0"},
                    proxy=None,
                    target_version="2.0.0",
                )
            )

        self.assertIsNotNone(discovery)
        self.assertEqual(
            github_client.requests[0][0],
            "https://api.github.com/repos/owner/project/releases/tags/2.0.0",
        )

    def test_m9a_shell_marker_selects_mfavalonia_asset_over_mxu(self) -> None:
        github_client = _FakeAsyncClient(
            httpx.Response(
                200,
                json={
                    "tag_name": "v4.5.4",
                    "assets": [
                        {
                            "name": "MFAAvalonia-win-x86_64-v4.5.4.zip",
                            "browser_download_url": "https://example.invalid/mfaa.zip",
                        },
                        {
                            "name": "MXU-win-x86_64-v4.5.4.zip",
                            "browser_download_url": "https://example.invalid/mxu.zip",
                        },
                    ],
                },
            )
        )
        apply_update = AsyncMock()
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_path = Path(temporary_directory)
            (project_path / "MFAAvalonia.dll").touch()
            with (
                patch.object(
                    updater_module.httpx,
                    "AsyncClient",
                    return_value=github_client,
                ),
                patch.object(
                    updater_module,
                    "apply_maafw_project_update",
                    new=apply_update,
                ),
            ):
                result = asyncio.run(
                    update_maafw_project_if_needed(
                        project_path,
                        self._interface(
                            name="m9a",
                            version="v4.0.1",
                            mirrorchyan_rid="M9A",
                            mirrorchyan_multiplatform=True,
                            github="https://github.com/MAA1999/M9A",
                        ),
                        source_config={"source": "github_release"},
                    )
                )

        self.assertTrue(result.updated)
        candidate = apply_update.await_args.args[1]
        self.assertEqual(candidate.download_url, "https://example.invalid/mfaa.zip")

    def test_automatic_mirror_without_package_stays_non_installable(self) -> None:
        mirror_client = _FakeAsyncClient(
            httpx.Response(
                200,
                json={"code": 0, "data": {"version_name": "2.0.0"}},
            )
        )
        with patch.object(
            updater_module.httpx,
            "AsyncClient",
            return_value=mirror_client,
        ):
            discovery = asyncio.run(
                discover_maafw_project_update(
                    self._interface(
                        name="M9A",
                        mirrorchyan_rid="demo",
                        mirrorchyan_multiplatform=True,
                        github="https://github.com/owner/project",
                    ),
                    current_version="1.0.0",
                    source_config={"cdk": "test-cdk"},
                )
            )

        self.assertIsNotNone(discovery)
        assert discovery is not None
        self.assertFalse(discovery.installable)
        self.assertIn("without a download URL", discovery.unavailable_reason)

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
        progress = lambda _event: None
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
                    progress=progress,
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
        self.assertIs(apply_mock.await_args.kwargs["progress"], progress)

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
        progress = lambda _event: None
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
                    progress=progress,
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
        self.assertIs(download_mock.await_args.kwargs["progress"], progress)

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
        events: list[dict] = []
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
                        progress=events.append,
                    )
                )
                second = asyncio.run(
                    updater_module.download_maafw_project_package(
                        Path(temporary_directory),
                        candidate,
                        progress=events.append,
                    )
                )

            self.assertEqual(first.path, second.path)
            self.assertEqual(Path(first.path).name, f"{first.sha256}.zip")
            self.assertTrue(Path(first.path).is_file())

        self.assertEqual(events[-1]["stage"], "downloaded")
        self.assertNotIn("final", events[-1])
        self.assertFalse(
            any(event["stage"] in {"completed", "failed"} for event in events)
        )

    def test_service_releases_managed_download_idempotently(self) -> None:
        service = MaaFWProjectUpdateService()
        archive_key = "a" * 24
        content = b"validated managed download"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive_dir = root / archive_key
            archive_dir.mkdir()
            package_path = archive_dir / f"{digest}.zip"
            package_path.write_bytes(content)
            package = {
                "source": "github_release",
                "version": "2.0.0",
                "path": str(package_path),
                "size": len(content),
                "sha256": digest,
            }

            released = asyncio.run(
                service.release_download_package(root, package)
            )
            repeated = asyncio.run(
                service.release_download_package(root, package)
            )

            self.assertEqual(
                released,
                {
                    "released": True,
                    "retained": False,
                    "directoryRemoved": True,
                },
            )
            self.assertEqual(
                repeated,
                {
                    "released": False,
                    "retained": False,
                    "directoryRemoved": False,
                },
            )
            self.assertFalse(package_path.exists())
            self.assertFalse(archive_dir.exists())

    def test_release_rejects_escape_wrong_sha_and_reparse_points(self) -> None:
        service = MaaFWProjectUpdateService()
        content = b"do not release without exact identity"
        digest = hashlib.sha256(content).hexdigest()
        archive_key = "b" * 24
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "managed"
            root.mkdir()

            outside_dir = base / "outside" / archive_key
            outside_dir.mkdir(parents=True)
            outside = outside_dir / f"{digest}.zip"
            outside.write_bytes(content)
            with self.assertRaisesRegex(
                MaaFWProjectUpdateError,
                "escapes managed root",
            ):
                asyncio.run(
                    service.release_download_package(
                        root,
                        {"path": str(outside), "sha256": digest},
                    )
                )
            self.assertTrue(outside.is_file())

            archive_dir = root / archive_key
            archive_dir.mkdir()
            tampered_sha = "c" * 64
            tampered = archive_dir / f"{tampered_sha}.zip"
            tampered.write_bytes(content)
            with self.assertRaisesRegex(
                MaaFWProjectUpdateError,
                "sha256 verification failed",
            ):
                asyncio.run(
                    service.release_download_package(
                        root,
                        {"path": str(tampered), "sha256": tampered_sha},
                    )
                )
            self.assertTrue(tampered.is_file())

            tampered.unlink()
            package_path = archive_dir / f"{digest}.zip"
            package_path.write_bytes(content)
            original_reparse_check = updater_module._is_reparse_path

            def mark_archive_dir_as_reparse(path: Path) -> bool:
                return Path(path) == archive_dir or original_reparse_check(Path(path))

            with (
                patch.object(
                    updater_module,
                    "_is_reparse_path",
                    side_effect=mark_archive_dir_as_reparse,
                ),
                self.assertRaisesRegex(
                    MaaFWProjectUpdateError,
                    "reparse point",
                ),
            ):
                asyncio.run(
                    service.release_download_package(
                        root,
                        {"path": str(package_path), "sha256": digest},
                    )
                )
            self.assertTrue(package_path.is_file())

    def test_failed_managed_download_removes_only_its_empty_archive_dir(self) -> None:
        candidate = MaaFWProjectUpdateCandidate(
            source="github_release",
            version="2.0.0",
            download_url="https://example.invalid/project.zip",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.object(
                updater_module,
                "_download_candidate_to_paths",
                side_effect=MaaFWProjectUpdateError("synthetic download failure"),
            ):
                with self.assertRaisesRegex(
                    MaaFWProjectUpdateError,
                    "synthetic download failure",
                ):
                    asyncio.run(
                        updater_module.download_maafw_project_package(
                            root,
                            candidate,
                        )
                    )

            self.assertEqual(list(root.iterdir()), [])

    def test_cancelled_managed_download_cleans_work_files_before_reraising(
        self,
    ) -> None:
        candidate = MaaFWProjectUpdateCandidate(
            source="github_release",
            version="2.0.0",
            download_url="https://example.invalid/project.zip",
        )

        async def scenario(root: Path) -> None:
            started = asyncio.Event()

            async def cancellable_download(
                temp_path,
                provisional_path,
                _download_url,
                **_kwargs,
            ):
                temp_path.write_bytes(b"partial temp")
                provisional_path.write_bytes(b"partial provisional")
                started.set()
                await asyncio.Event().wait()

            with patch.object(
                updater_module,
                "_download_candidate_to_paths",
                side_effect=cancellable_download,
            ):
                task = asyncio.create_task(
                    updater_module.download_maafw_project_package(
                        root,
                        candidate,
                    )
                )
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            asyncio.run(scenario(root))
            self.assertEqual(list(root.iterdir()), [])

    def test_cancel_during_publish_worker_cannot_recreate_cleaned_download(
        self,
    ) -> None:
        candidate = MaaFWProjectUpdateCandidate(
            source="github_release",
            version="2.0.0",
            download_url="https://example.invalid/project.zip",
        )
        validate_started = threading.Event()
        finish_validate = threading.Event()
        original_validate = updater_module._validate_and_publish_download

        async def fake_stream(temp_path, _url, **_kwargs):
            with zipfile.ZipFile(temp_path, "w") as archive:
                archive.writestr("interface.json", '{"version":"2.0.0"}')
            size = temp_path.stat().st_size
            return size, size

        def blocking_validate(temp_path, package_path, expected_sha256):
            validate_started.set()
            if not finish_validate.wait(timeout=2):
                raise TimeoutError("test validate worker was not released")
            return original_validate(temp_path, package_path, expected_sha256)

        async def scenario(root: Path) -> bool:
            with (
                patch.object(
                    updater_module,
                    "_stream_update_package",
                    side_effect=fake_stream,
                ),
                patch.object(
                    updater_module,
                    "_validate_and_publish_download",
                    side_effect=blocking_validate,
                ),
            ):
                task = asyncio.create_task(
                    updater_module.download_maafw_project_package(
                        root,
                        candidate,
                    )
                )
                self.assertTrue(
                    await asyncio.to_thread(validate_started.wait, 2),
                    "validate worker did not start",
                )
                task.cancel()
                await asyncio.sleep(0)
                pending_after_cancel = not task.done()
                finish_validate.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                return pending_after_cancel

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pending_after_cancel = asyncio.run(scenario(root))
            self.assertTrue(pending_after_cancel)
            self.assertEqual(list(root.iterdir()), [])

    def test_cancelled_release_finishes_unlink_then_remains_idempotent(self) -> None:
        service = MaaFWProjectUpdateService()
        archive_key = "e" * 24
        content = b"release cancellation package"
        digest = hashlib.sha256(content).hexdigest()
        release_started = threading.Event()
        finish_release = threading.Event()
        original_release = updater_module._release_content_addressed_download

        def blocking_release(download_root, package_path, package_sha256):
            release_started.set()
            if not finish_release.wait(timeout=2):
                raise TimeoutError("test release worker was not released")
            return original_release(download_root, package_path, package_sha256)

        async def scenario(root: Path, package: dict) -> bool:
            with patch.object(
                updater_module,
                "_release_content_addressed_download",
                side_effect=blocking_release,
            ):
                task = asyncio.create_task(
                    service.release_download_package(root, package)
                )
                self.assertTrue(
                    await asyncio.to_thread(release_started.wait, 2),
                    "release worker did not start",
                )
                task.cancel()
                await asyncio.sleep(0)
                pending_after_cancel = not task.done()
                finish_release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                return pending_after_cancel

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive_dir = root / archive_key
            archive_dir.mkdir()
            package_path = archive_dir / f"{digest}.zip"
            package_path.write_bytes(content)
            package = {"path": str(package_path), "sha256": digest}

            pending_after_cancel = asyncio.run(scenario(root, package))
            repeated = asyncio.run(
                service.release_download_package(root, package)
            )

            self.assertTrue(pending_after_cancel)
            self.assertFalse(package_path.exists())
            self.assertFalse(repeated["retained"])
            self.assertFalse(repeated["released"])

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

    def test_download_progress_reports_real_chunk_bytes(self) -> None:
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "download.tmp"
            with patch.object(
                updater_module.httpx,
                "AsyncClient",
                return_value=_FakeStreamClient([b"abc", b"defgh"]),
            ):
                downloaded, total = asyncio.run(
                    updater_module._stream_update_package(
                        target,
                        "https://example.invalid/project.zip",
                        proxy=None,
                        progress=events.append,
                    )
                )

        self.assertEqual((downloaded, total), (8, 8))
        downloading = [event for event in events if event["stage"] == "downloading"]
        self.assertEqual(
            [event["downloaded_bytes"] for event in downloading],
            [0, 3, 8],
        )
        self.assertEqual(downloading[-1]["percent"], 100.0)

    def test_download_progress_throttle_bounds_high_rate_callbacks(self) -> None:
        events: list[dict] = []
        now = [0.0]
        total = 100 * 1024 * 1024
        reporter = updater_module._DownloadProgressThrottle(
            callback=events.append,
            total_bytes=total,
            clock=lambda: now[0],
        )
        reporter.report(0, force=True)
        for downloaded in range(64 * 1024, total + 1, 64 * 1024):
            reporter.report(downloaded)
        reporter.report(total, force=True)

        self.assertLessEqual(len(events), 102)
        self.assertEqual(events[0]["downloaded_bytes"], 0)
        self.assertEqual(events[-1]["downloaded_bytes"], total)
        self.assertEqual(events[-1]["percent"], 100.0)

    def test_unknown_length_progress_uses_one_mib_or_time_threshold(self) -> None:
        events: list[dict] = []
        now = [0.0]
        reporter = updater_module._DownloadProgressThrottle(
            callback=events.append,
            total_bytes=None,
            clock=lambda: now[0],
        )
        reporter.report(0, force=True)
        reporter.report(512 * 1024)
        self.assertEqual(len(events), 1)
        reporter.report(1024 * 1024)
        self.assertEqual(len(events), 2)
        now[0] = 0.25
        reporter.report(1024 * 1024 + 64 * 1024)
        self.assertEqual(len(events), 3)

    def test_download_wall_clock_timeout_never_validates_or_applies(self) -> None:
        async def never_finishes(*_args, **_kwargs):
            await asyncio.sleep(1)

        events: list[dict] = []
        logs: list[str] = []
        validate_mock = Mock()
        apply_mock = AsyncMock()
        candidate = MaaFWProjectUpdateCandidate(
            source="github_release",
            version="2.0.0",
            download_url="https://example.invalid/project.zip",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.object(
                    updater_module,
                    "discover_maafw_project_update",
                    new=AsyncMock(
                        return_value=updater_module._build_update_discovery(
                            source="github_release",
                            version="2.0.0",
                            download_url=candidate.download_url,
                            sha256=None,
                            unavailable_reason="",
                        )
                    ),
                ),
                patch.object(updater_module, "DOWNLOAD_TIMEOUT_SECONDS", 0.01),
                patch.object(
                    updater_module,
                    "_stream_update_package",
                    side_effect=never_finishes,
                ),
                patch.object(
                    updater_module,
                    "_validate_and_publish_download",
                    validate_mock,
                ),
                patch.object(
                    updater_module,
                    "_apply_update_package",
                    apply_mock,
                ),
            ):
                with self.assertRaisesRegex(
                    MaaFWProjectUpdateError,
                    "timed out after 0.01 seconds",
                ):
                    asyncio.run(
                        update_maafw_project_if_needed(
                            Path(temporary_directory),
                            self._interface(version="1.0.0"),
                            send_log=logs.append,
                            progress=events.append,
                        )
                    )

            self.assertFalse(
                (Path(temporary_directory) / ".mas-update" / "download.tmp").exists()
            )

        validate_mock.assert_not_called()
        apply_mock.assert_not_awaited()
        terminal = [
            event for event in events if event["stage"] in {"completed", "failed"}
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["stage"], "failed")
        self.assertEqual(terminal[0]["status"], "download_timeout")
        self.assertTrue(terminal[0]["final"])
        self.assertTrue(any("timed out" in message for message in logs))

    def test_retry_sleep_is_capped_by_remaining_download_deadline(self) -> None:
        async def fail_immediately(*_args, **_kwargs):
            raise MaaFWProjectUpdateError("temporary failure")

        sleep_mock = AsyncMock()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.object(updater_module, "DOWNLOAD_TIMEOUT_SECONDS", 0.25),
                patch.object(
                    updater_module,
                    "_stream_update_package",
                    side_effect=fail_immediately,
                ),
                patch.object(updater_module.asyncio, "sleep", sleep_mock),
            ):
                with self.assertRaises(MaaFWProjectUpdateError):
                    asyncio.run(
                        updater_module._download_candidate_to_paths(
                            Path(temporary_directory) / "download.tmp",
                            Path(temporary_directory) / "download.zip",
                            "https://example.invalid/project.zip",
                            expected_sha256=None,
                            proxy=None,
                            send_log=lambda _message: None,
                            max_download_bytes=1024,
                            progress=None,
                        )
                    )

        self.assertTrue(sleep_mock.await_args_list)
        self.assertTrue(
            all(call.args[0] <= 0.25 for call in sleep_mock.await_args_list)
        )

    def test_mirror_download_path_token_is_redacted_from_all_logs(self) -> None:
        secret = "3HMPuqcknzfoghs2XlsabENGxMo"
        download_url = (
            "https://mirrorchyan.com/api/resources/download/"
            f"{secret}?cdk=also-secret"
        )

        async def fail_with_url(*_args, **_kwargs):
            raise MaaFWProjectUpdateError(f"request failed for {download_url}")

        logs: list[str] = []
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.object(updater_module, "DOWNLOAD_RETRY_TIMES", 1),
                patch.object(
                    updater_module,
                    "_stream_update_package",
                    side_effect=fail_with_url,
                ),
            ):
                with self.assertRaises(MaaFWProjectUpdateError):
                    asyncio.run(
                        updater_module._download_candidate_to_paths(
                            Path(temporary_directory) / "download.tmp",
                            Path(temporary_directory) / "download.zip",
                            download_url,
                            expected_sha256=None,
                            proxy=None,
                            send_log=logs.append,
                            max_download_bytes=1024,
                            progress=events.append,
                        )
                    )

        self.assertTrue(logs)
        self.assertFalse(any(secret in message for message in logs))
        self.assertFalse(any("also-secret" in message for message in logs))
        self.assertTrue(any("download/***" in message for message in logs))
        self.assertFalse(any(event["stage"] == "failed" for event in events))

    def test_apply_progress_reports_extract_and_switch_stages(self) -> None:
        events: list[dict] = []

        def fake_apply(_project, _package, _send_log, send_progress) -> None:
            send_progress("extracting")
            send_progress("switching")

        with patch.object(
            updater_module,
            "_apply_update_package_sync",
            side_effect=fake_apply,
        ):
            asyncio.run(
                updater_module._apply_update_package(
                    Path("C:/project"),
                    Path("C:/project/update.zip"),
                    lambda _message: None,
                    progress=events.append,
                )
            )

        self.assertEqual(
            [event["stage"] for event in events],
            ["extracting", "switching"],
        )

    @staticmethod
    def _interface(**updates) -> MaaFWInterface:
        payload = {
            "interface_version": 2,
            "name": "provider-contract",
            "version": "1.0.0",
        }
        payload.update(updates)
        return MaaFWInterface(**payload)


if __name__ == "__main__":
    unittest.main()
