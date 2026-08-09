import sys
import unittest
from pathlib import Path

from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = ROOT / "packages" / "automas_maafw_controller_win32" / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from automas_maafw_controller_win32.service import (  # noqa: E402
    MaaFWWin32ControllerService,
    MaaFWWin32Window,
)


class ForeignController(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    type: str


class MaaFWWin32ControllerContractTest(unittest.TestCase):
    def test_service_accepts_a_foreign_pydantic_controller(self) -> None:
        controller = ForeignController(
            name="Win32-Front",
            type="Win32",
            win32={"window_regex": ".*MaaEnd.*"},
        )
        service = MaaFWWin32ControllerService()

        matches = service.match_controller_windows(
            controller,
            [MaaFWWin32Window(hWnd=1, className="MaaEnd", windowName="MaaEnd")],
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].controllerName, "Win32-Front")


if __name__ == "__main__":
    unittest.main()
