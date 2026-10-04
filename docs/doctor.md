# Read-only Doctor

The shared core is `scripts/doctor.py`. From a complete local checkout:

```bash
./install.sh --check
./install.sh --check --offline
```

`PROFILE_DIR` selects the installed profile; otherwise installer profile-home
resolution is retained. The script is intercepted before the normal `main`
installation flow, root authentication, APT, downloads and directory creation.
A lone downloaded install.sh without its local Doctor core fails without
fetching the missing code. Python 3 must already exist; Doctor does not install it.

A newly created `kobra3d` launcher provides:

```bash
kobra3d doctor
kobra3d doctor --offline
```

Both entrypoints exec the same Python core with `-B`. Existing launchers and
profile scripts remain unchanged on reruns, including locally modified launchers
with the project's old marker. Therefore an existing old launcher may not support
this subcommand yet. Use the local checkout entrypoint; no implicit migration is
performed. A future deliberate profile/launcher update must protect user edits.

Direct core options select fixtures/custom diagnostic locations:
`--profile-dir`, `--hermes-home`, `--source-dir`, `--offline`, `--timeout`.
Do not accidentally select another profile's files.

## Status and exit codes

- `PASS`: check backed by the available local/status evidence.
- `WARN`: questionable/incomplete state needing review.
- `FAIL`: required check failed.
- `NOT VERIFIED`: available data cannot establish the claimed capability.

Exit **0** = required checks passed with no warnings; **1** = warnings/incomplete
proofs; **2** = at least one required check failed. Offline mode skips every
network request and reports missing network evidence rather than inventing it.
Some checks may pass even when the overall exit code is 1 or 2.

## Local checks

OS/version/architecture (Debian 13 identified as the v0.2.0 baseline, not a new
v0.3.0 Clean-VM result; other releases and aarch64 warn rather than claim support),
Hermes executable and basic model configuration,
Node >=20, npm availability, KlipperMCP directory/full pinned commit/patch/build
artifact, FreeCAD availability, Orca AppImage/manifest/hash and the three expected
Kobra machine/process/filament profile files, selected .env syntax/required keys.

Executable presence is not the same as a runtime compatibility test. Model
configuration presence is not authentication verification. If PyYAML is not
available in the selected Python interpreter, YAML-dependent checks are
`NOT VERIFIED`; Doctor does not install it or guess YAML parsing. In particular,
a system Python on a fresh host may lack PyYAML even if Hermes's own environment
has it. A conflicting HTTP `url` alongside the stdio command is refused. A build file is not
proof of a successful current runtime handshake. Orca is hashed without running
it; the accepted verifier remains unchanged. A hash proves agreement with the
reviewed manifest, not general binary safety or an authenticated upstream
signature. Modified extracted profile trees are not covered by an AppImage hash.

`.env` is data only: it is never sourced/eval'ed or executed. Unknown credential
values are not printed; diagnostic output omits arbitrary paths, URLs, config
contents, server error bodies and unsanitized exception strings. No authentication
store, Wi-Fi/network configuration, printer config dump or remote log is read.
Git subprocesses discard inherited GIT_* routing/config/tracing variables and
ignore global/system config. Before reverse patch checking, a names-only query
checks effective local config, including includes, for clean/smudge/process
conversion drivers. Any configured driver (even unused) or unsafe config-query
result produces FAIL before Git apply can execute it. No config is repaired and
no driver names/commands/values are printed. Undefined attributes with no driver
are harmless and tested. This is necessary because `git apply --check` by itself
can execute conversion filters and is not inherently write-free.

There is no npm audit network query, npm install, build, git fetch, repair or
MCP startup in Doctor. Version queries must not invoke tools that can initialize
Hermes, CAD, slicer or npm runtime state/log directories.

## Network boundary

Only explicit GET requests to these status endpoints are supported:

- `/server/info`
- `/printer/info`
- `/printer/objects/list`
- `/printer/objects/query` with fixed allowlisted status objects/fields

No arbitrary API URL/endpoint is accepted. Redirects are refused; environment
HTTP proxies are not used. URL credentials, query strings/fragments and unsafe
base paths are refused. Responses/timeouts are bounded; HTTP 401/403/500, invalid
JSON, offline endpoints and timeouts are reported without echoing private values.

Moonraker reachability, Klippy connectivity and Klippy readiness are separate
checks. `/server/info` alone does not establish a printer model or ACE Pro.
MMU objects/status can establish indicators and gate counts; four empty gates
still represent four gates, not absent hardware. An explicit supported identity
is required before naming a model/ACE unit. An absent identity is `NOT VERIFIED`,
not a guessed label. Tool names/order alone do not prove physical tool-to-gate
mapping. Explicit mapping evidence must be consistent; incomplete evidence is
reported honestly. This is not a physical filament-feed test.

There is no upload, print start, GCode dispatch, homing, motion, temperature,
restart, firmware or Rinkhals write endpoint in this core. Doctor makes no
backups, new logfiles, package changes, mkdirs or config changes. Bytecode output
is disabled. Like any userspace read-only check, file access times may change on
filesystems configured to record reads; content/mtime/modes/tree are the relevant
no-write assertions. An actively malicious same-UID process or executable is
outside the boundary, not made safe by diagnostic code.

## Test boundary

Mock-Moonraker tests run only on loopback and log every HTTP request to verify
method/path/query allowlists, failure handling, MMU evidence and secret redaction.
Fixtures compare filesystem contents/types/modes/mtime before and after Doctor.
Installer dispatch tests put guarded fake root/install/network commands on PATH
and show that `--check` bypasses the installation flow and preserves Doctor's
exit status. These are local tests, not Clean-VM or real-printer tests.
