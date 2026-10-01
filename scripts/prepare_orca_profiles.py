#!/usr/bin/env python3

import json
import os
import shutil
import sys
from pathlib import Path


def fail(message):
    print(f"ERROR: {message}")
    sys.exit(1)


project_dir = os.environ.get("PROJECT_DIR")

if not project_dir:
    fail("PROJECT_DIR is not set.")

PROJECT_DIR = Path(project_dir).expanduser().resolve()

ORCA_PROFILE_ROOT = Path(
    os.environ.get(
        "ORCA_PROFILE_ROOT",
        "/opt/orcaslicer/squashfs-root/resources/profiles/Anycubic",
    )
).expanduser().resolve()

SOURCE_MACHINE = (
    ORCA_PROFILE_ROOT
    / "machine"
    / "Anycubic Kobra S1 0.4 nozzle.json"
)

SOURCE_PROCESS = (
    ORCA_PROFILE_ROOT
    / "process"
    / "0.20mm Standard @Anycubic Kobra S1 0.4 nozzle.json"
)

SOURCE_FILAMENT = (
    ORCA_PROFILE_ROOT
    / "filament"
    / "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle.json"
)

DEST_MACHINE_DIR = PROJECT_DIR / "Druckprofile"
DEST_FILAMENT_DIR = PROJECT_DIR / "Filamente"

DEST_MACHINE = DEST_MACHINE_DIR / SOURCE_MACHINE.name
DEST_PROCESS = DEST_MACHINE_DIR / SOURCE_PROCESS.name
DEST_FILAMENT = DEST_FILAMENT_DIR / SOURCE_FILAMENT.name


def load_json(path):
    if not path.is_file():
        fail(f"OrcaSlicer profile not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"Could not read JSON profile {path}: {exc}")


def write_json(path, data):
    if path.exists():
        fail(f"Destination already exists; refusing to overwrite: {path}")

    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            data,
            indent="\t",
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


# Refuse before writing anything if any destination already exists.
existing_destinations = [
    path
    for path in [
        DEST_MACHINE,
        DEST_PROCESS,
        DEST_FILAMENT,
    ]
    if path.exists()
]

if existing_destinations:
    print("ERROR: Refusing to overwrite existing profile files:")
    for path in existing_destinations:
        print(f"- {path}")
    sys.exit(1)


machine = load_json(SOURCE_MACHINE)
process = load_json(SOURCE_PROCESS)
filament = load_json(SOURCE_FILAMENT)

if machine.get("name") != "Anycubic Kobra S1 0.4 nozzle":
    fail("Unexpected machine profile.")

if process.get("name") != "0.20mm Standard @Anycubic Kobra S1 0.4 nozzle":
    fail("Unexpected process profile.")

if filament.get("name") != "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle":
    fail("Unexpected filament profile.")


# Kobra S1 + ACE:
# explicitly select the initial tool after the Anycubic start sequence.
start_gcode = machine.get("machine_start_gcode", "")

tool_command = "T[initial_tool]"

if tool_command not in start_gcode.splitlines():
    machine["machine_start_gcode"] = (
        start_gcode.rstrip() + "\n" + tool_command
    )

# Enable OrcaSlicer's multiple-bed-type handling.
machine["support_multi_bed_types"] = "1"

# Tested reference setting used by this setup.
machine["default_bed_type"] = "4"

# Prevent the stock PLA profile from defaulting to Cool Plate.
filament["bed_type"] = ["Textured PEI Plate"]


DEST_MACHINE_DIR.mkdir(parents=True, exist_ok=True)
DEST_FILAMENT_DIR.mkdir(parents=True, exist_ok=True)

write_json(DEST_MACHINE, machine)
shutil.copy2(SOURCE_PROCESS, DEST_PROCESS)
write_json(DEST_FILAMENT, filament)


print("OrcaSlicer profiles prepared successfully.")
print()
print(f"Machine:  {DEST_MACHINE}")
print(f"Process:  {DEST_PROCESS}")
print(f"Filament: {DEST_FILAMENT}")
print()
print("Applied changes:")
print("- added T[initial_tool] to Kobra S1 machine start GCode")
print("- enabled support_multi_bed_types")
print("- set default_bed_type to 4")
print("- changed PLA bed_type to Textured PEI Plate")
print()
print("Existing destination profiles are never overwritten.")
