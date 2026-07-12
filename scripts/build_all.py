from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = ROOT / "packages"
DIST_DIR = ROOT / "dist"


def main() -> None:
    packages = sorted(path for path in PACKAGES_DIR.iterdir() if (path / "pyproject.toml").is_file())
    for package in packages:
        output_dir = DIST_DIR / package.name
        output_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "build", str(package), "--outdir", str(output_dir)],
            check=True,
        )


if __name__ == "__main__":
    main()

