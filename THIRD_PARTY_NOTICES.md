# Third-Party Notices

This repository contains original project code and documentation licensed under
the MIT License unless otherwise noted.

It also installs, interoperates with, references, or patches third-party
open-source projects. Those projects remain subject to their own licenses.

## Hermes Agent

Upstream:
https://github.com/NousResearch/hermes-agent

License: MIT

Hermes Agent is developed by Nous Research.

This repository uses the official Hermes installation mechanism but does not
redistribute the Hermes Agent source tree or binaries.

## KlipperMCP

Upstream:
https://github.com/mikehatch/KlipperMCP

License: MIT

This repository includes:

`patches/klippermcp-kobra-upload.patch`

The patch is intended for the tested KlipperMCP revision and adds Kobra S1
GCode upload support used by this project.

KlipperMCP itself remains third-party software under its upstream license.
Any upstream code represented in patch context remains subject to the
KlipperMCP license.

## FreeCAD

Upstream:
https://github.com/FreeCAD/FreeCAD

License: GNU Lesser General Public License v2.1 or later (LGPL-2.1-or-later)

FreeCAD is installed as an external system dependency. This repository does
not redistribute FreeCAD binaries or source code.

Files created by users with FreeCAD are not automatically relicensed by this
project.

## OrcaSlicer

Upstream:
https://github.com/OrcaSlicer/OrcaSlicer

License: GNU Affero General Public License v3.0 (AGPL-3.0)

The installer downloads OrcaSlicer from its official upstream GitHub release.

This repository does not redistribute the OrcaSlicer AppImage or OrcaSlicer
source code.

Kobra S1 profile files are prepared locally from resources available in the
user's installed OrcaSlicer environment; OrcaSlicer stock profile data is not
bundled in this repository.

## Rinkhals

Current upstream:
https://github.com/rinkhals-community/Rinkhals

License: MIT for original Rinkhals code. Bundled third-party components retain
their respective licenses.

Rinkhals is an external prerequisite / integration point. This repository does
not install or redistribute Rinkhals firmware.

## Anycubic and Fujitsu

Anycubic Kobra S1 / ACE Pro and Fujitsu S740 are referenced only to describe
hardware compatibility and the project's real-world test environment.

No Anycubic or Fujitsu firmware, software, or product assets are redistributed
by this repository.

## Trademarks and affiliation

This is an independent community project.

It is not affiliated with, sponsored by, or endorsed by Anycubic, Fujitsu,
Nous Research, FreeCAD, OrcaSlicer, KlipperMCP, or the Rinkhals project.

Product names, project names, logos, and other marks belong to their respective
owners and are used here only for identification, compatibility description,
and attribution.

## License scope

The MIT License in this repository applies to this project's own original
material except where otherwise noted.

Third-party software remains governed by its respective upstream license.

This notice is provided for attribution and transparency and does not replace
the complete license terms supplied by the upstream projects.
