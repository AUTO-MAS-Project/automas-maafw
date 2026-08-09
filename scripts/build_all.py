from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = ROOT / "packages"
DIST_DIR = ROOT / "dist"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build every automas-maafw distribution.")
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=DIST_DIR,
        help="Artifact root directory (default: repository dist directory).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dist_dir = args.dist_dir.resolve()
    packages = sorted(path for path in PACKAGES_DIR.iterdir() if (path / "pyproject.toml").is_file())
    for package in packages:
        output_dir = dist_dir / package.name
        output_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "build", str(package), "--outdir", str(output_dir)],
            check=True,
        )


if __name__ == "__main__":
    main()
