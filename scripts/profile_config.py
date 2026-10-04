#!/usr/bin/env python3
"""Install profile payload without changing existing user configuration.

No dotenv evaluation and no rendering of configuration contents.
"""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

MANAGED = ("SOUL.md", "config.yaml", "distribution.yaml", "scripts", "patches", ".env.EXAMPLE", "checksums")
TREES = ("scripts", "patches", "checksums")
NEW_IN_V030 = ("checksums",)


def snapshot(root):
    """Fingerprint only managed entries; reject links and special files."""
    if root.is_symlink():
        raise ValueError("Profile/source directory must not be a symlink")
    result = {}
    for name in MANAGED:
        top = root / name
        if not top.exists() and not top.is_symlink():
            continue
        entries = [top]
        if top.is_dir() and not top.is_symlink():
            entries.extend(sorted(top.rglob("*")))
        for path in entries:
            rel = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("Managed payload contains a symlink or special file")
            digest = None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            result[rel] = ("directory" if path.is_dir() else "file", digest, stat.S_IMODE(mode))
    return result


def validate_source(root):
    state = snapshot(root)
    for name in MANAGED:
        expected = "directory" if name in TREES else "file"
        if name not in state or state[name][0] != expected:
            raise ValueError("Incomplete or invalid source profile payload")
    return state


def copy_entry(src, dst):
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def rename_noreplace(src, dst):
    """Linux atomic no-overwrite rename, including empty directories/symlinks."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(src), -100, os.fsencode(dst), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def identity(path):
    state = path.lstat()
    return state.st_dev, state.st_ino


@contextmanager
def staging_area(target):
    work = Path(tempfile.mkdtemp(prefix=".config-stage-", dir=target))
    try:
        yield work
    except BaseException:
        # In particular, do not discard originals if rollback itself fails.
        print("Recovery staging retained: " + str(work))
        raise
    else:
        shutil.rmtree(work)


def replace_payload(source, target, before, source_state, backup=False):
    """Stage on the same filesystem; retain originals until all replacements work."""
    target.mkdir(parents=True, exist_ok=True)
    lock_path = target / ".profile-config.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if snapshot(target) != before:
            raise ValueError("Profile changed during confirmation; update cancelled")
        with staging_area(target) as work:
            staged, originals = work / "staged", work / "originals"
            staged.mkdir()
            originals.mkdir()
            if validate_source(source) != source_state:
                raise ValueError("Source changed since preview; update cancelled")
            for name in MANAGED:
                copy_entry(source / name, staged / name)
            if snapshot(staged) != source_state:
                raise ValueError("Source changed while staging; update cancelled")
            backup_dir = None
            if backup:
                parent = target / "backups"
                if parent.is_symlink():
                    raise ValueError("Backup directory must not be a symlink")
                parent.mkdir(mode=0o700, exist_ok=True)
                stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
                backup_dir = parent / ("config-" + stamp + "-" + uuid.uuid4().hex[:8])
                backup_dir.mkdir(mode=0o700)
                for name in MANAGED:
                    if name in before:
                        copy_entry(target / name, backup_dir / name)
                if snapshot(backup_dir) != before:
                    raise ValueError("Backup verification failed; update cancelled")
                metadata = {"schema_version": 1, "created_at": stamp, "entries": before,
                            "candidate_entries": source_state, "env_included": False}
                manifest = backup_dir / "backup-manifest.json"
                manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
                manifest.chmod(0o600)
                print("Verified configuration backup: " + str(backup_dir))
            if snapshot(target) != before:
                raise ValueError("Profile changed during backup; update cancelled")
            started = []
            published = {}
            try:
                for name in MANAGED:
                    started.append(name)
                    old = target / name
                    if old.exists():
                        os.replace(old, originals / name)
                if snapshot(originals) != before:
                    raise ValueError("Profile changed while moving originals; update cancelled")
                for name in MANAGED:
                    candidate_id = identity(staged / name)
                    rename_noreplace(staged / name, target / name)
                    published[name] = candidate_id
                if backup_dir is not None:
                    # Preserve actual inodes, not only the pre-update copy:
                    # editors holding old descriptors can still write later.
                    rename_noreplace(originals, backup_dir / "moved-originals")
                    print("Moved original inodes retained: " + str(backup_dir / "moved-originals"))
            except BaseException:
                withdrawn = work / "withdrawn"
                withdrawn.mkdir()
                incomplete = False
                for name in reversed(started):
                    saved, installed = originals / name, target / name
                    try:
                        # Never delete a revision: even our candidates may have
                        # been edited since publication. Unknown new roots stay.
                        if name in published and (installed.exists() or installed.is_symlink()):
                            if identity(installed) == published[name]:
                                rename_noreplace(installed, withdrawn / name)
                        if saved.exists() or saved.is_symlink():
                            rename_noreplace(saved, installed)
                    except OSError:
                        incomplete = True
                print("Configuration replacement failed; " +
                      ("conflicting roots/originals retained for manual recovery." if incomplete else
                       "moved originals restored; withdrawn revisions retained in recovery staging."))
                raise
    print("Hermes profile payload installed; .env not touched.")


def confirm_update():
    """--yes and piped input never authorize configuration replacement."""
    try:
        with open("/dev/tty", "r", encoding="utf-8") as reader, open("/dev/tty", "w", encoding="utf-8") as writer:
            writer.write("Existing managed files will be replaced (including listed removals).\n"
                         "Type UPDATE CONFIG to confirm, or anything else to cancel: ")
            writer.flush()
            return reader.readline().strip() == "UPDATE CONFIG"
    except OSError:
        return False


def sync_profile(source, target, dry_run=False, update=False, confirm=None):
    source, target = Path(source), Path(target)
    expected = validate_source(source)
    current = snapshot(target)
    if (target / ".env").is_symlink():
        raise ValueError(".env must not be a symlink")
    if current:
        if any((name not in current and name not in NEW_IN_V030) or
               (name in current and current[name][0] != expected[name][0]) for name in MANAGED):
            raise ValueError("Incomplete or inconsistent existing profile payload; keeping all files unchanged")
        changed = sorted(key for key in expected.keys() | current.keys() if expected.get(key) != current.get(key))
        if not update or not changed:
            print("Existing Hermes profile files kept unchanged.")
            if changed:
                print("Payload differs in %d entries; use --update-config for an explicit update." % len(changed))
            return
        print("Planned configuration changes (paths/status only; no contents or secrets):")
        for key in changed:
            action = "ADD" if key not in current else "REMOVE" if key not in expected else "CHANGE"
            print("  %s %s" % (action, json.dumps(key)))
        if dry_run:
            print("DRY-RUN: no confirmation, backup or replacement performed")
            return
        if not (confirm or confirm_update)():
            raise ValueError("Configuration update not explicitly confirmed; unchanged")
        if snapshot(target) != current:
            raise ValueError("Profile changed during confirmation; unchanged")
        replace_payload(source, target, current, expected, backup=True)
        return
    if update:
        raise ValueError("No complete existing profile to update; run the normal installer first")
    if dry_run:
        print("DRY-RUN: install initial Hermes profile payload without overwriting .env")
        return
    replace_payload(source, target, current, expected)



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    try:
        sync_profile(args.source, args.target, dry_run=args.dry_run, update=args.update)
    except (ValueError, OSError):
        # Do not expose arbitrary file contents or exception payloads.
        parser.exit(1, "ERROR: Profile operation cancelled or failed. Check completeness/permissions and any retained backup before retrying; .env was not modified.\n")


if __name__ == "__main__":
    main()
