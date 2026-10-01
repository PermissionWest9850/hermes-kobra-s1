#!/usr/bin/env python3

import os
import subprocess
import sys
from pathlib import Path


def fail(message):
    print(f"ERROR: {message}")
    sys.exit(1)


project_dir = os.environ.get("PROJECT_DIR")

if not project_dir:
    fail("PROJECT_DIR is not set.")

BASE = Path(project_dir).expanduser().resolve()
CAD_DIR = BASE / "CAD"
BUILD_SCRIPT = Path(__file__).resolve().parent / "build_print.py"

if len(sys.argv) != 2:
    fail("Usage: build_offline.py FILE.FCStd")

input_file = Path(sys.argv[1])

if input_file.suffix.lower() != ".fcstd":
    fail("The input file must end in .FCStd.")

if input_file.is_absolute():
    cad_file = input_file.resolve()
else:
    cad_file = (CAD_DIR / input_file.name).resolve()

if not cad_file.is_file():
    fail(f"CAD file not found: {cad_file}")

if not BUILD_SCRIPT.is_file():
    fail(f"Build script not found: {BUILD_SCRIPT}")

print(f"Offline job: {cad_file}")
print("Starting offline build ...")
print()

result = subprocess.run(
    [
        sys.executable,
        str(BUILD_SCRIPT),
        str(cad_file),
    ]
)

if result.returncode != 0:
    fail("Offline build failed.")

print()
print("Offline job prepared successfully.")
