#!/usr/bin/env python3

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def fail(message):
    print(f"ERROR: {message}")
    sys.exit(1)


project_dir = os.environ.get("PROJECT_DIR")

if not project_dir:
    fail("PROJECT_DIR is not set.")

BASE = Path(project_dir).expanduser().resolve()

CAD_DIR = BASE / "CAD"
GCODE_DIR = BASE / "Kobra-S1"
HISTORY_DIR = BASE / "Druckhistorie"

SCRIPT_DIR = Path(__file__).resolve().parent

FREECAD = Path(
    os.environ.get("FREECADCMD_PATH", "/usr/bin/freecadcmd")
).expanduser()

FREECAD_SCRIPT = SCRIPT_DIR / "fcad_to_stl.py"

ORCA = Path(
    os.environ.get(
        "ORCA_SLICER_PATH",
        "/opt/orcaslicer/OrcaSlicer.AppImage",
    )
).expanduser()

MACHINE_PROFILE = Path(
    os.environ.get(
        "KOBRA_MACHINE_PROFILE",
        str(
            BASE
            / "Druckprofile"
            / "Anycubic Kobra S1 0.4 nozzle.json"
        ),
    )
).expanduser()

PROCESS_PROFILE = Path(
    os.environ.get(
        "KOBRA_PROCESS_PROFILE",
        str(
            BASE
            / "Druckprofile"
            / "0.20mm Standard @Anycubic Kobra S1 0.4 nozzle.json"
        ),
    )
).expanduser()

FILAMENT_PROFILE = Path(
    os.environ.get(
        "KOBRA_FILAMENT_PROFILE",
        str(
            BASE
            / "Filamente"
            / "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle.json"
        ),
    )
).expanduser()


if len(sys.argv) != 2:
    fail("Usage: build_print.py FILE.FCStd")

input_file = Path(sys.argv[1])

if not input_file.is_absolute():
    input_file = CAD_DIR / input_file

input_file = input_file.expanduser().resolve()

if not input_file.is_file():
    fail(f"CAD file not found: {input_file}")

if input_file.suffix.lower() != ".fcstd":
    fail("The input file must be an .FCStd file.")

for path in [
    FREECAD,
    FREECAD_SCRIPT,
    ORCA,
    MACHINE_PROFILE,
    PROCESS_PROFILE,
    FILAMENT_PROFILE,
]:
    if not path.exists():
        fail(f"Required file not found: {path}")

GCODE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

job_name = input_file.stem
output_gcode = GCODE_DIR / f"{job_name}.gcode"

if output_gcode.exists():
    fail(f"GCode already exists: {output_gcode}")

created_at = datetime.now().astimezone().strftime(
    "%Y-%m-%d %H:%M:%S %Z"
)

with tempfile.TemporaryDirectory(prefix="3dprint-") as workdir:
    workdir = Path(workdir)

    stl_file = workdir / f"{job_name}.stl"
    orca_dir = workdir / "orca"
    orca_dir.mkdir()

    print(f"CAD: {input_file}")
    print("1/2 FreeCAD -> STL ...")

    env = os.environ.copy()
    env["FCAD_INPUT"] = str(input_file)
    env["STL_OUTPUT"] = str(stl_file)

    result = subprocess.run(
        [
            str(FREECAD),
            str(FREECAD_SCRIPT),
        ],
        env=env,
    )

    if result.returncode != 0:
        fail("FreeCAD could not generate the STL.")

    if not stl_file.exists():
        fail("FreeCAD reported success, but the STL file is missing.")

    print(f"STL: {stl_file}")

    print("2/2 OrcaSlicer -> GCode ...")

    result = subprocess.run(
        [
            str(ORCA),
            str(stl_file),
            "--load-settings",
            f"{MACHINE_PROFILE};{PROCESS_PROFILE}",
            "--load-filaments",
            str(FILAMENT_PROFILE),
            "--curr-bed-type",
            "Textured PEI Plate",
            "--arrange",
            "1",
            "--slice",
            "0",
            "--outputdir",
            str(orca_dir),
        ]
    )

    if result.returncode != 0:
        fail("OrcaSlicer could not generate GCode.")

    gcode_files = list(orca_dir.glob("*.gcode"))

    if not gcode_files:
        fail(
            "OrcaSlicer reported success, but no GCode file was found."
        )

    source_gcode = gcode_files[0]
    shutil.copy2(source_gcode, output_gcode)

    history_file = HISTORY_DIR / f"{job_name}-auftrag.md"

    history_file.write_text(
        f"""# Print job: {job_name}

- **Status:** prepared - not printed
- **CAD:** `{input_file}`
- **GCode:** `{output_gcode}`
- **Printer:** Anycubic Kobra S1
- **Nozzle:** 0.4 mm
- **Process:** 0.20 mm standard
- **Created:** {created_at}

## Safety

- The printer was not contacted.
- No GCode was uploaded to the printer.
- No print was started.
""",
        encoding="utf-8",
    )

    print()
    print("========================================")
    print("OFFLINE PRINT PREPARATION COMPLETE")
    print("========================================")
    print(f"CAD:      {input_file}")
    print(f"GCODE:    {output_gcode}")
    print(f"HISTORY:  {history_file}")
    print()
    print("The printer was NOT contacted.")
    print("NO print was started.")
