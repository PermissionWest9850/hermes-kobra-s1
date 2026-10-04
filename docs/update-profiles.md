# Explicit local Orca-profile updates (v0.3.0 preparation, stage 5)

This separate mode updates an **existing complete three-profile set**, not
Hermes configuration, OrcaSlicer itself or printer configuration. It is local,
unreleased work. No new Clean-VM or hardware validation is claimed.

## Usage and scope

From a complete local clone, as the normal user:

```bash
bash install.sh --update-profiles --dry-run
bash install.sh --update-profiles
```

Select a nondefault installed Kobra profile explicitly if needed:

```bash
PROFILE_DIR="$HOME/.hermes/profiles/kobra-s1-3d-print" \
  bash install.sh --update-profiles --dry-run
```

`PROFILE_DIR` otherwise uses the installer's existing default profile-home
resolution (`HERMES_HOME` or `~/.hermes`, then `profiles/kobra-s1-3d-print`).
There is deliberately no new `kobra3d` subcommand or silent launcher migration.
An isolated copy of `install.sh` without the local helper fails; it does not
fetch the repository or install Python to make this mode work.

The selected `PROFILE_DIR/.env` is read **as literal data** to determine:

- `PROJECT_DIR`: an existing local workspace;
- `ORCA_PROFILE_ROOT`: an existing local extracted Anycubic profile tree;
- optional `KOBRA_MACHINE_PROFILE`, `KOBRA_PROCESS_PROFILE`,
  `KOBRA_FILAMENT_PROFILE`: three existing destination files inside that workspace.

Missing overrides select the normal three files:

```text
PROJECT_DIR/
├── Druckprofile/
│   ├── Anycubic Kobra S1 0.4 nozzle.json
│   └── 0.20mm Standard @Anycubic Kobra S1 0.4 nozzle.json
└── Filamente/
    └── Anycubic PLA @Anycubic Kobra S1 0.4 nozzle.json
```

Environment values such as an ambient `PROJECT_DIR` do not silently override
this saved selection. No `source`, `eval`, shell expansion, command substitution
or interpolation is used. Unknown `.env` keys are not rendered. Paths must be
literal absolute paths, regular files/directories without symlink components.
The installer's double-quoted backslash/quote escapes and a single `export `
prefix are supported without expansion. Mixed/concatenated quotes and text after
a closing quote are refused; full-line comments are accepted. In unquoted values,
`#` is literal path data, not an inline comment. Duplicate selected
keys are refused, including exported/unexported duplicates. Hardlinked files,
changed role/name identities and an existing `backups/` directory without private
0700 permissions are also refused, not silently renamed or repaired.
A missing, partial, invalid or ambiguously selected set is refused rather than
created or repaired. The three targets must be distinct, inside the workspace,
and on its filesystem; unsafe source/target overlaps are refused.

The mode is intercepted before normal installer authentication/bootstrap. It
performs no sudo/su call, APT action, download, Git/npm/build operation, Hermes
onboarding, FreeCAD/Orca execution, Moonraker request, upload or print action.
`--check`, `--update-config` and `--update-profiles` are mutually exclusive;
`--offline` remains valid only for Doctor.

## Candidate and readable comparison

Candidates are built in memory from the same three named factory JSON files
used by the unchanged `prepare_orca_profiles.py`:

- machine: append `T[initial_tool]` only when absent, enable multi-bed types,
  and set the existing Textured-PEI bed identifier;
- process: retain the factory process file, without a new tuning recipe;
- filament: set `bed_type` to `Textured PEI Plate`.

This is **replacement, not a merge**. Local temperature/speed/infill changes,
custom GCode and extra fields can be removed or reset after approval. It does
not silently preserve local tuning while claiming an update. Other material
profiles and other workspace files are outside the replacement scope.

The preview names the machine/process/filament role and lists field-level
`ADD`, `CHANGE`, `REMOVE` differences with old/new values when safe. Numeric
settings and recognized bed/profile enums are readable; arbitrary text, custom
GCode, paths, URLs, sensitive keys and unknown values are redacted instead of
printing complete profiles or secrets. A GCode-change explanation identifies
the initial-tool selection change without exposing private comments/macros.
Unknown redacted text still counts as a change; review your local files yourself
before approving if the withheld detail matters. The comparison is not a
complete Orca schema, inheritance or GCode safety validator.

Only formatting/key-order differences are a semantic no-op. A no-op leaves all
files untouched, without a prompt, lock, stage or backup. If some profiles need
an update, the confirmation warns that the selected three-file set is replaced.
The process candidate retains exact source bytes; machine/filament serialization
matches the existing generator. Dry-run never writes candidates or creates any
transaction directory, lock or backup.

**Source trust limitation:** this mode reads the already selected local profile
tree. It neither executes nor independently authenticates that tree, downloads
new settings, updates inherited factory profiles, or revalidates the AppImage.
An AppImage checksum does not authenticate later edits to extracted JSON files.
Use the intended local Orca 2.4.2 tree and inspect the comparison. Source and
selection changes during the update are conflicts, not automatic new baselines.

## Explicit confirmation, verified backup and conflicts

For a nonempty comparison, authorization requires the exact phrase:

```text
UPDATE PROFILES
```

It must come from the controlling `/dev/tty`. `--yes`, piped stdin and ambient
environment variables cannot approve replacement. No TTY, refusal, a wrong
phrase or end-of-input cancels without writing a lock, backup or stage.

After approval the transaction:

1. Rechecks the selected `.env`, source profiles, original destination profiles
   and directory identities against the preview. It never rereads an edited
   file and treats it as newly approved content.
2. Acquires a nonblocking workspace update lock. Other updater instances fail
   closed while it is held; arbitrary editors are not serialized by that lock.
3. Stages candidates and creates a uniquely named private backup beneath
   `PROJECT_DIR/backups/`. Backup/stage root directories are 0700; the manifest
   and backup file copies are 0600. Existing backups are never overwritten.
4. Verifies the backup's original bytes before moving/replacing any target. The
   manifest contains fingerprints and metadata, **not `.env` content**. Only the
   three profile files are backed up; no GCode or unrelated configuration is copied.
5. Rechecks the approved inputs, moves the actual original inodes into private
   recovery storage and verifies those moved originals before publication.
6. Publishes candidates using Linux `renameat2(RENAME_NOREPLACE)`. A newly
   appearing target file, empty directory or symlink is a conflict and is not
   overwritten. There is no fallback to a check-then-overwrite operation.
7. Retains the actual original inodes after success in addition to the verified
   backup copies. Late writes through an editor's old descriptor remain
   recoverable there; they are not silently merged into the new profiles.

A late conflict/failure after approval can leave a lock, verified backup and
recovery/staging artifacts. That is intentional recovery data, not evidence of
successful replacement. Do not retry by deleting it blindly.

## Rollback and recovery limits

On a replacement failure, the helper conservatively withdraws **only its own
published revisions** and restores moved originals into absent target paths.
Withdrawn revisions are retained, including edits made after publication.
An unfamiliar concurrently created/replaced target is never deleted or
replaced to force a rollback. If it blocks restoration, it stays in place and
the corresponding moved original stays in recovery storage. The operation then
fails with an explicit incomplete-recovery indication.

This is an automatic **failure rollback**, not a new automatic repair service
or a blanket permission to restore any older backup over current files. Inspect
backup copies, moved originals, withdrawn candidates and current targets; choose
a revision and perform any manual restoration only after explicit approval.

The three file publications are **not one atomic filesystem transaction**.
SIGKILL, power loss, disk-full/fsync faults or persistent I/O errors can require
manual recovery. The private backup/staging boundary assumes no actively
malicious process with the same user permissions. Directory fingerprints and
advisory locks improve conflict detection; they do not provide sandboxing.
Close editors before updating. Retained old inodes can change after success;
only the verified backup copies represent the pre-update byte snapshot.

Backups can contain private custom profile values even though `.env` is excluded.
Keep them local and do not attach them to public issues or force-add them to Git.
The existing `backups/` and `cache/` exclusions remain in effect. Originals retain
their access modes on rollback; the retained-inode storage is inside a private
0700 directory. There is no automatic backup retention/deletion policy.

## Local validation

```bash
python3 -B -m unittest discover -s tests -p 'test_update*profiles*.py' -v
python3 -B -m unittest discover -s tests -v
bash -n install.sh
git diff --check
```

Tests use synthetic local JSON trees, workspace files and a simulated
controlling TTY; fault injection covers conflicts and rollback. Generator parity
uses the unchanged preparation script only on those fixtures. This is neither
a real installation upgrade, a slicer/GCode production run, nor a printer test.
Optional older integration tests remain separately opt-in; do not interpret an
explicit skip as a newly executed npm/build/stdio validation.
