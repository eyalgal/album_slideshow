"""Build album_slideshow.zip with forward-slash separators for HACS.

PowerShell's Compress-Archive uses backslashes which breaks unzipping on
Linux HA hosts (rc9 lesson). Always go through Python's zipfile module.
"""
from __future__ import annotations

import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "custom_components" / "album_slideshow"
OUT = ROOT / "album_slideshow.zip"

EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIXES = {".pyc"}


def main() -> None:
    if not SRC.is_dir():
        raise SystemExit(f"source directory not found: {SRC}")

    files: list[pathlib.Path] = []
    for path in SRC.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        files.append(path)

    files.sort()

    OUT.unlink(missing_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = path.relative_to(SRC).as_posix()
            zf.write(path, arcname=arcname)
            print(f"added {arcname}")

    print(f"\nwrote {OUT} ({OUT.stat().st_size:,} bytes, {len(files)} files)")


if __name__ == "__main__":
    main()
