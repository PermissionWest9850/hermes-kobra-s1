# Hermes + Anycubic Kobra S1

A reproducible Hermes Agent setup for preparing and safely controlling 3D prints on an Anycubic Kobra S1 through Rinkhals, Moonraker and KlipperMCP.

## Safety model

Offline CAD work, slicing and GCode inspection do not require printer access.

Uploading GCode requires explicit user approval.

Starting a print requires a second, separate explicit user approval.

The second approval is enforced by the Hermes agent policy in `SOUL.md`; KlipperMCP itself does not require `confirmed=true` for `action=start`.

## Architecture

```text
User
  |
  v
Hermes 3D Print Agent
  |
  +--> FreeCAD --> STL
  |
  +--> OrcaSlicer --> GCode
  |
  +--> GCode validation
  |
  +--> Approval #1 --> upload_gcode_file
  |
  +--> KlipperMCP --> Moonraker / Rinkhals --> Kobra S1
  |
  +--> Read-only upload verification
  |
  +--> Approval #2 --> control_print action=start
```

## Tested reference environment

- Anycubic Kobra S1 Combo
- Rinkhals + Moonraker
- Debian VM on Proxmox
- Hermes Agent `v0.21.5+4590.g16c59d0`
- Hermes upstream commit `16c59d0e`
- Python `3.14.7`
- FreeCAD `1.0.0`
- OrcaSlicer `2.4.2`
- KlipperMCP `1.1.0`
- KlipperMCP commit `425e169`
- Node.js `22.23.2`
- npm `12.0.2`

These are tested versions, not necessarily strict requirements.

## Install Hermes

Official Linux installer:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

Reload the shell:

```bash
source ~/.bashrc
```

Configure Hermes:

```bash
hermes setup
```

The reference profile was tested with `openai-codex` and `gpt-5.5`. Other compatible Hermes-supported models may also work.

## Install the Kobra S1 profile

After this repository is published, replace `PermissionWest9850` with the actual GitHub account:

```bash
REPO_URL="https://github.com/PermissionWest9850/hermes-kobra-s1.git"
hermes profile install "$REPO_URL" --alias
```

The profile name is:

```text
kobra-s1-3d-print
```

## Configure the profile

Hermes generates `.env.EXAMPLE` during installation.

Create the local environment file:

```bash
cd ~/.hermes/profiles/kobra-s1-3d-print
cp .env.EXAMPLE .env
nano .env
```

Required values:

```dotenv
KOBRA_S1_MOONRAKER_URL=http://192.168.x.x:7125
KLIPPER_MCP_PATH=/home/YOUR_USER/KlipperMCP
PROJECT_DIR=/home/YOUR_USER/3d-print
```

Never commit the real `.env` file.

## Project structure

Example workspace:

```bash
mkdir -p ~/3d-print/CAD
mkdir -p ~/3d-print/Kobra-S1
mkdir -p ~/3d-print/Druckhistorie
mkdir -p ~/3d-print/Druckprofile
mkdir -p ~/3d-print/Filamente
```

Result:

```text
PROJECT_DIR/
├── CAD/
├── Druckhistorie/
├── Druckprofile/
├── Filamente/
└── Kobra-S1/
```

## Offline FreeCAD pipeline

Included scripts:

```text
scripts/build_offline.py
scripts/build_print.py
scripts/fcad_to_stl.py
```

Pipeline:

```text
.FCStd
  |
  v
build_offline.py
  |
  v
build_print.py
  |
  v
FreeCADCmd
  |
  v
temporary STL
  |
  v
OrcaSlicer
  |
  v
GCode
  |
  v
local Markdown print history
```

The supplied wrapper pipeline currently accepts `.FCStd` files.

The local build refuses to silently overwrite an existing GCode file.

## OrcaSlicer reference profiles

The tested setup uses the OrcaSlicer 2.4.2 Kobra S1 system profiles with a few local changes.

Machine profile:

```text
Anycubic Kobra S1 0.4 nozzle
```

Reference start GCode:

```text
G9111 bedTemp=[first_layer_bed_temperature] extruderTemp=[first_layer_temperature[initial_tool]]
M117
T[initial_tool]
```

Additional tested machine settings:

```text
support_multi_bed_types = 1
default_bed_type = 4
```

PLA profile:

```text
Anycubic PLA @Anycubic Kobra S1 0.4 nozzle
```

The tested profile uses:

```text
bed_type = Textured PEI Plate
```

The tested 0.20 mm process profile is unchanged from the OrcaSlicer 2.4.2 system profile.

## Generate the tested profiles

The repository includes `scripts/prepare_orca_profiles.py`.

Example:

```bash
export PROJECT_DIR="$HOME/3d-print"
~/.hermes/profiles/kobra-s1-3d-print/scripts/prepare_orca_profiles.py
```

The script refuses to overwrite existing destination profiles.

## Headless slicing

The reference workflow explicitly sets:

```text
--curr-bed-type "Textured PEI Plate"
```

This prevents another plate type from being silently inherited from profile or 3MF metadata.

## Reference PLA values

```text
Nozzle:
  normal:       205 °C
  first layer:  215 °C

Textured PEI:
  normal:        55 °C
  first layer:   55 °C
```

## GCode validation

Before upload, inspect the generated GCode.

For the reference PLA / ACE setup, verify in particular:

```text
curr_bed_type = Textured PEI Plate
first_layer_bed_temperature = 55
textured_plate_temp_initial_layer = 55
```

Example start sequence when ACE Slot 1 is selected:

```text
G9111 bedTemp=55 extruderTemp=215
M117
T0
```

The actual nozzle temperature must always match the filament being used.

The printer reports four ACE slots with the mapping Slot 1 -> `T0`, Slot 2 -> `T1`, Slot 3 -> `T2`, Slot 4 -> `T3`. Slot 1 is practically verified with PLA. Slots 2-4 were recognized but empty during documentation, so their mapping is system-confirmed but not yet filament-tested. Do not assume `T0` for every print.

Also inspect for unexpected printer-specific start commands or macros inherited from another printer profile.

## Foreign 3MF files

3MF files may contain more than geometry. They can embed:

- printer settings
- filament settings
- plate type
- process settings
- start GCode
- printer-specific commands

Do not blindly reuse these embedded settings.

Geometry and useful placement information may be reused, but the final slice should normally use the tested local Kobra S1 profiles unless explicitly requested otherwise.

For example, an embedded:

```text
curr_bed_type = High Temp Plate
```

should not silently replace the local Textured PEI configuration.

## Install KlipperMCP

Clone the tested KlipperMCP revision:

```bash
cd ~
git clone https://github.com/mikehatch/KlipperMCP.git
cd KlipperMCP
git checkout 425e169
npm install
```

KlipperMCP requires Node.js 20 or newer.

## Apply the Kobra upload patch

The Hermes profile includes:

```text
patches/klippermcp-kobra-upload.patch
```

Apply it:

```bash
cd ~/KlipperMCP
git apply ~/.hermes/profiles/kobra-s1-3d-print/patches/klippermcp-kobra-upload.patch
npm run build
```

The patch has been verified against KlipperMCP commit `425e169`.

It:

- adds `upload_gcode_file`
- refuses an existing remote filename
- restricts explicit remote filenames to plain filenames
- increases the Moonraker upload timeout to 120 seconds
- never starts a print

## Upload workflow

```text
GCode validation
      |
      v
list_gcode_files
      |
      v
filename free?
      |
      v
Approval #1
      |
      v
upload_gcode_file
      |
      v
list_gcode_files
      |
      v
verify upload
```

Upload approval is approval for the upload only.

## Print start

KlipperMCP provides `control_print` with the actions `start`, `pause`, `resume` and `cancel`.

Starting a print requires a filename.

The intended workflow is:

```text
Approval #1
    |
    v
upload_gcode_file
    |
    v
upload verification
    |
    v
Approval #2
    |
    v
control_print action=start
```

Never treat upload approval as permission to start the print.

## Rinkhals / Moonraker

This setup assumes Rinkhals is already installed and Moonraker is reachable from the Hermes machine.

Typical Moonraker URL:

```text
http://PRINTER_IP:7125
```

No printer IP address is hard-coded in this repository.

## Optional LAN setup

Ethernet was used as the primary printer connection in the tested setup.

Tested USB Ethernet adapter:

```text
D-Link DUB-1312
ASIX AX88179 family
USB ID 0b95:1790
```

On the tested Rinkhals system it appeared as `eth1` and reported:

```text
carrier = 1
speed   = 1000
duplex  = full
```

Wi-Fi remained configured, but automatic Ethernet-to-Wi-Fi failover was not live-tested and is not guaranteed by this guide.

Useful read-only Rinkhals diagnostics:

```bash
lsusb
ifconfig
route -n
cat /sys/class/net/eth1/carrier
cat /sys/class/net/eth1/speed
cat /sys/class/net/eth1/duplex
readlink -f /sys/class/net/eth1/device/driver
```

Avoid changing network interfaces during an active print.

## Start Hermes with the profile

Select the installed profile:

```bash
hermes profile use kobra-s1-3d-print
```

Then start Hermes normally:

```bash
hermes
```

Before any printer-changing action, verify that the expected Kobra S1 profile is active.

## Repository contents

```text
README.md
SOUL.md
config.yaml
distribution.yaml
patches/
  klippermcp-kobra-upload.patch
scripts/
  build_offline.py
  build_print.py
  fcad_to_stl.py
  prepare_orca_profiles.py
```

Not included:

- API keys or tokens
- passwords
- private IP addresses
- Wi-Fi credentials
- personal Hermes memories or sessions
- generated GCode, STL or 3MF files

## Tested workflow

The reference setup has been used for:

```text
FreeCAD project
   -> STL export
   -> OrcaSlicer
   -> GCode validation
   -> explicit upload approval
   -> KlipperMCP upload
   -> read-only verification
   -> separate print-start approval
   -> Kobra S1
```

The repository is intentionally conservative around printer-changing actions.

## Upstream attribution

This project uses KlipperMCP by mikehatch as the Moonraker/MCP bridge.

The included patch:

```text
patches/klippermcp-kobra-upload.patch
```

is based on KlipperMCP commit `425e169` and adds the upload workflow used by this Kobra S1 setup.

KlipperMCP declares the MIT license in its `package.json`.

Upstream project:

```text
https://github.com/mikehatch/KlipperMCP
```

## ACE Pro slot mapping

The tested Kobra S1 reports one ACE Pro with four available gates:

| ACE slot | Tool command | Current verification |
| --- | --- | --- |
| Slot 1 | `T0` | Practically verified with PLA |
| Slot 2 | `T1` | System-confirmed, slot was empty during testing |
| Slot 3 | `T2` | System-confirmed, slot was empty during testing |
| Slot 4 | `T3` | System-confirmed, slot was empty during testing |

The reported tool-to-gate mapping is `[0, 1, 2, 3]`.

The OrcaSlicer machine profile uses `T[initial_tool]`, so the generated tool command should follow the selected ACE slot instead of being hard-coded to `T0`.

Slots 2-4 were recognized correctly by the system but have not yet been practically filament-tested.
