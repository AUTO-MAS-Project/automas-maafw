from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SOURCE = ROOT / "packages" / "automas_script_maafw" / "src"
if str(SCRIPT_SOURCE) not in sys.path:
    sys.path.insert(0, str(SCRIPT_SOURCE))

from automas_script_maafw.project_path import (  # noqa: E402
    normalize_project_path,
    release_project_path,
    try_reserve_project_path,
)


class MaaFWProjectPathLockContractTest(unittest.TestCase):
    def test_update_and_runner_share_one_fail_fast_path_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_path = Path(temporary_directory) / "project"
            project_path.mkdir()

            async def exercise() -> None:
                first = await try_reserve_project_path(project_path)
                self.assertEqual(first, normalize_project_path(project_path))
                self.assertIsNone(
                    await try_reserve_project_path(project_path / ".")
                )

                await release_project_path(first)
                second = await try_reserve_project_path(project_path)
                self.assertEqual(second, first)
                await release_project_path(second)

            asyncio.run(exercise())

    def test_release_is_idempotent(self) -> None:
        async def exercise() -> None:
            await release_project_path(None)
            await release_project_path("not-reserved")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
