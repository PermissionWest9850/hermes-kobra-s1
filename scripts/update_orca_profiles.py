#!/usr/bin/env python3
"""Explicit, offline replacement of an existing complete Kobra Orca profile set.

Candidates match prepare_orca_profiles.py (not imported: it writes on import).
Semantic JSON equality is a no-op, even when formatting differs. No inheritance
flattening or profile migration. This is not a three-file atomic transaction:
power loss/SIGKILL can require manual recovery from the retained backup. Advisory
locking coordinates this updater, not editors; malicious same-UID writers are
outside the security boundary.
"""
import sys
sys.dont_write_bytecode = True
import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import NoReturn

ROLES = ('machine', 'process', 'filament')
NAMES = ('Anycubic Kobra S1 0.4 nozzle',
         '0.20mm Standard @Anycubic Kobra S1 0.4 nozzle',
         'Anycubic PLA @Anycubic Kobra S1 0.4 nozzle')
DEFAULTS = tuple(('Filamente/' if r == 'filament' else 'Druckprofile/') + n + '.json'
                 for r, n in zip(ROLES, NAMES))
ENV_KEYS = ('PROJECT_DIR', 'ORCA_PROFILE_ROOT', 'KOBRA_MACHINE_PROFILE',
            'KOBRA_PROCESS_PROFILE', 'KOBRA_FILAMENT_PROFILE')
SAFE_KEYS = frozenset(('type name inherits from instantiation nozzle_diameter printer_model '
    'printer_variant machine_start_gcode machine_end_gcode support_multi_bed_types default_bed_type '
    'bed_type layer_height initial_layer_print_height wall_loops top_shell_layers bottom_shell_layers '
    'sparse_infill_density sparse_infill_pattern nozzle_temperature nozzle_temperature_initial_layer '
    'bed_temperature bed_temperature_initial_layer hot_plate_temp hot_plate_temp_initial_layer '
    'textured_plate_temp textured_plate_temp_initial_layer filament_type filament_vendor filament_diameter '
    'filament_flow_ratio enable_support brim_type seam_position').split())
ENUMS = frozenset(NAMES + ('machine', 'process', 'filament', 'PLA', 'PLA+', 'PETG', 'ABS', 'ASA',
    'TPU', 'Anycubic', 'SUNLU', 'Generic', 'Textured PEI Plate', 'Cool Plate', 'High Temp Plate',
    'Smooth PEI Plate', 'aligned', 'nearest', 'back', 'random', 'grid', 'gyroid', 'rectilinear',
    'auto_brim', 'no_brim', 'outer_only', 'inner_only'))


class UpdateError(ValueError):
    """Fixed diagnostics only: never include file values or OS exception strings."""


def fail() -> NoReturn:
    raise UpdateError('Unsafe, incomplete or changed profile set; update cancelled.')


def literal_path(value):
    if not isinstance(value, str) or not value.startswith('/') or value != os.path.normpath(value):
        fail()
    if value.startswith('//') or any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail()
    return Path(value)


def directory_chain(path):
    chain = {}
    for p in reversed((path, *path.parents)):
        s = p.lstat()
        if not stat.S_ISDIR(s.st_mode):
            fail()
        chain[p] = (s.st_dev, s.st_ino, s.st_mode)
    return chain


def fingerprint(path):
    directory_chain(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            fail()
        with os.fdopen(fd, 'rb', closefd=False) as reader:
            data = reader.read()
        after = os.fstat(fd)
        fields = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
        if fields(before) != fields(after) or fields(path.lstat()) != fields(after):
            fail()
        return data, fields(after)
    finally:
        os.close(fd)


def decode_json(data):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                fail()
            d[k] = v
        return d
    def invalid(_):
        fail()
    def number(text):
        import math
        v = float(text)
        if not math.isfinite(v):
            fail()
        return v
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid, parse_float=number)
    except (ValueError, UnicodeError, RecursionError):
        fail()
    if not isinstance(result, dict):
        fail()
    return result


def read_env(data):
    # Tokenization of one quoted literal only; shlex performs no expansion or exec.
    import shlex
    result = {}
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        fail()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        key = key.strip()
        if key not in ENV_KEYS:
            continue  # Unknown keys are not inspected, evaluated or printed.
        if not sep or key in result:
            fail()
        value = value.strip()
        if value.startswith(('"', "'")):
            pattern = r'"(?:[^"\\]|\\.)*"' if value[0] == '"' else r"'[^']*'"
            if not re.fullmatch(pattern, value):
                fail()
            try:
                words = shlex.split(value, comments=False, posix=True)
            except ValueError:
                fail()
            if len(words) != 1:
                fail()
            value = words[0]
        elif '"' in value or "'" in value:
            fail()  # Refuse mixed/concatenated quoting, not a shell expression.
        result[key] = literal_path(value)
    if any(k not in result for k in ENV_KEYS[:2]):
        fail()
    return result


def validate_profile(data, role, name, candidate=False):
    if data.get('type') != role or data.get('name') != name:
        fail()
    if role == 'machine' and not isinstance(data.get('machine_start_gcode', ''), str):
        fail()
    for key in ('inherits', 'printer_model', 'printer_variant'):
        if key in data and (not isinstance(data[key], str) or not data[key] or
                any(ord(c) < 32 or ord(c) == 127 for c in data[key])):
            fail()
    for key in ('compatible_printers', 'compatible_prints', 'default_print_profile', 'default_filament_profile'):
        if key not in data:
            continue
        values = data[key]
        # Orca default_print_profile is a single name, the other refs are lists.
        if key == 'default_print_profile' and isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or any(not isinstance(v, str) or not v or
                any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values):
            fail()
    for key in ('nozzle_diameter', 'filament_diameter'):
        if key in data and (not isinstance(data[key], list) or not data[key] or
                any(not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', str(v)) or float(v) <= 0 for v in data[key])):
            fail()
    if 'layer_height' in data:
        v = data['layer_height']
        if not isinstance(v, (str, int, float)) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', str(v)) or not 0 < float(v) <= 10:
            fail()
    if candidate:
        if 'sparse_infill_density' in data:
            density = str(data['sparse_infill_density'])
            if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?%?', density) or not 0 <= float(density.rstrip('%')) <= 100:
                fail()
        for key in ('wall_loops', 'top_shell_layers', 'bottom_shell_layers'):
            if key in data and (not re.fullmatch(r'[0-9]+', str(data[key])) or not 0 <= int(data[key]) <= 256):
                fail()
        temperature_keys = ('nozzle_temperature', 'nozzle_temperature_initial_layer',
                            'bed_temperature', 'bed_temperature_initial_layer', 'hot_plate_temp',
                            'hot_plate_temp_initial_layer', 'textured_plate_temp', 'textured_plate_temp_initial_layer')
        for key in temperature_keys:
            if key not in data:
                continue
            values = data[key] if isinstance(data[key], list) else [data[key]]
            ceiling = 500 if key.startswith('nozzle') else 200
            if not values or any(not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', str(v)) or
                                 not 0 <= float(v) <= ceiling for v in values):
                fail()
    if candidate and role == 'machine' and not isinstance(data.get('machine_start_gcode'), str):
        fail()


def build_candidates(source_bytes):
    """Pure byte construction; process is exact copy2 byte parity, not reserialized."""
    parsed = [decode_json(b) for b in source_bytes]
    for d, r, n in zip(parsed, ROLES, NAMES):
        validate_profile(d, r, n)
    machine, _, filament = parsed
    start = machine.get('machine_start_gcode', '')
    if 'T[initial_tool]' not in start.splitlines():
        machine['machine_start_gcode'] = start.rstrip() + '\nT[initial_tool]'
    machine['support_multi_bed_types'] = '1'
    machine['default_bed_type'] = '4'
    filament['bed_type'] = ['Textured PEI Plate']
    serialize = lambda d: (json.dumps(d, indent='\t', ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    result = (serialize(machine), source_bytes[1], serialize(filament))
    for b, r, n in zip(result, ROLES, NAMES):
        validate_profile(decode_json(b), r, n, candidate=True)
    return result


def safe_value(v, known=True):
    if not known:
        return '[private value redacted]'
    if v is None or isinstance(v, (bool, int, float)):
        return json.dumps(v)
    if isinstance(v, str) and (v in ENUMS or re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?%?', v)):
        return json.dumps(v, ensure_ascii=True)
    if isinstance(v, list) and len(v) <= 32:
        return '[' + ', '.join(safe_value(i) for i in v) + ']'
    return '[private value redacted]'


def semantic_equal(a, b):
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(semantic_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(semantic_equal(x, y) for x, y in zip(a, b))
    return a == b


def field_diff(old, new, prefix=''):
    lines = []
    missing = object()
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key, missing), new.get(key, missing)
        if semantic_equal(a, b):
            continue
        known = key in SAFE_KEYS
        path = prefix + '/' + (key if known else '[private field]')
        action = 'ADD' if a is missing else 'REMOVE' if b is missing else 'CHANGE'
        if known and isinstance(a, dict) and isinstance(b, dict):
            lines.extend(field_diff(a, b, path))
        else:
            left = '(absent)' if a is missing else safe_value(a, known)
            right = '(absent)' if b is missing else safe_value(b, known)
            lines.append(f'{action} {path}: {left} -> {right}')
    return lines


def validate_work_paths(project):
    backup, lock = project / 'backups', project / '.orca-profile-update.lock'
    for path, directory in ((backup, True), (lock, False)):
        if not os.path.lexists(path):
            continue
        s = path.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected_type(s.st_mode) or stat.S_IMODE(s.st_mode) != (0o700 if directory else 0o600)
                or s.st_uid != os.geteuid() or s.st_dev != project.stat().st_dev
                or (not directory and s.st_nlink != 1)):
            fail()


def preview(profile_dir):
    """Read only the selected .env and six JSON files; bind preview to snapshots."""
    profile = literal_path(str(profile_dir))
    envpath = profile / '.env'
    envstate = fingerprint(envpath)
    env = read_env(envstate[0])
    project, source = env['PROJECT_DIR'], env['ORCA_PROFILE_ROOT']
    if project.is_relative_to(source) or source.is_relative_to(project):
        fail()
    targets = tuple(env.get(k, project / default) for k, default in zip(ENV_KEYS[2:], DEFAULTS))
    sources = tuple(source / r / (n + '.json') for r, n in zip(ROLES, NAMES))
    allpaths = (envpath, *sources, *targets)
    if len(set(allpaths)) != len(allpaths):
        fail()
    for target in targets:
        if (not target.is_relative_to(project) or target == project or target.suffix != '.json'
                or target.is_relative_to(project / 'backups')):
            fail()
    for p in allpaths:
        if any(p != q and p in q.parents for q in allpaths):
            fail()
    parents = directory_chain(project)
    validate_work_paths(project)
    if os.path.lexists(project / 'backups'):
        parents.update(directory_chain(project / 'backups'))
    for p in allpaths:
        parents.update(directory_chain(p.parent))
    states = {p: fingerprint(p) for p in allpaths}
    if states[envpath] != envstate:
        fail()
    dev = project.stat().st_dev
    if any(states[p][1][0] != dev for p in targets):
        fail()
    candidates = build_candidates(tuple(states[p][0] for p in sources))
    changed = False
    print('Selected profile .env controls these destinations; verify the selected profile is correct.')
    print('This operation will replace LOCAL customizations, not merge them.')
    for role, name, path, default, candidate in zip(ROLES, NAMES, targets, DEFAULTS, candidates):
        old, new = decode_json(states[path][0]), decode_json(candidate)
        validate_profile(old, role, name)
        label = default if path == project / default else '(custom inside PROJECT_DIR)'
        print(f'{role}: {label}')
        lines = field_diff(old, new)
        changed |= bool(lines)
        for line in lines:
            print('  ' + line)
    print('Machine recipe: add T[initial_tool] once when absent; support_multi_bed_types=1; default_bed_type=4.')
    print('Filament recipe: Textured PEI Plate. Numeric temperatures, infill percentages and walls above are replacements.')
    print('Private custom fields/text are compared but redacted; unchanged inheritance is not flattened.')
    return dict(project=project, envpath=envpath, sources=sources, targets=targets,
                states=states, parents=parents, candidates=candidates, changed=changed)


def rename_noreplace(src, dst):
    """Fail closed without Linux renameat2; never fall back to overwrite rename."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(src), -100, os.fsencode(dst), 1):
        code = ctypes.get_errno()
        raise OSError(code, 'Atomic no-overwrite rename failed')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as writer:
            writer.write(data)
            writer.flush()
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def check_inputs(plan, targets=True):
    for p, expected in plan['parents'].items():
        s = p.lstat()
        if (s.st_dev, s.st_ino, s.st_mode) != expected or not stat.S_ISDIR(s.st_mode):
            fail()
    paths = (plan['envpath'], *plan['sources'], *(plan['targets'] if targets else ()))
    if any(fingerprint(p) != plan['states'][p] for p in paths):
        fail()


def moved_matches(path, expected):
    actual = fingerprint(path)
    # rename changes ctime; byte content, inode, mode, mtime and size must match.
    indices = (0, 1, 2, 3, 4, 6)
    return actual[0] == expected[0] and all(actual[1][i] == expected[1][i] for i in indices)


def rollback(started, published, originals, recovery, parents):
    """Withdraw only our inode; never unlink or overwrite a concurrent revision."""
    withdrawn = recovery / 'withdrawn'
    incomplete, interruption = False, None
    try:
        withdrawn.mkdir(mode=0o700)
    except BaseException as error:
        incomplete = True
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            interruption = error
    for role, target in reversed(started):
        saved = originals / (role + '.json')
        try:
            if any(parents.get(p) != actual for p, actual in directory_chain(target.parent).items()):
                fail()  # Never restore into a replaced or redirected parent.
            if target in published and os.path.lexists(target):
                s = target.lstat()
                if (s.st_dev, s.st_ino) == published[target]:
                    rename_noreplace(target, withdrawn / (role + '.json'))
            if os.path.lexists(saved):
                rename_noreplace(saved, target)
            sync_dir(target.parent)
        except BaseException as error:
            incomplete = True
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                interruption = error
    print('Profile replacement failed; ' +
          ('conflicting new files and originals retained for manual recovery.' if incomplete else
           'moved originals restored; withdrawn revisions retained for recovery.'))
    if interruption is not None:
        raise interruption


def replace_profiles(plan):
    import fcntl
    import uuid
    from datetime import datetime
    if os.geteuid() == 0:
        raise UpdateError('Refusing profile changes as root.')
    check_inputs(plan)
    project = plan['project']
    fd = os.open(project / '.orca-profile-update.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1 or s.st_uid != os.geteuid():
            fail()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_inputs(plan)
        parent = project / 'backups'
        parent.mkdir(mode=0o700, exist_ok=True)
        directory_chain(parent)
        s = parent.lstat()
        if stat.S_IMODE(s.st_mode) != 0o700 or s.st_uid != os.geteuid() or s.st_dev != project.stat().st_dev:
            fail()
        stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')
        token = 'orca-' + stamp + '-' + uuid.uuid4().hex
        recovery = parent / token
        recovery.mkdir(mode=0o700)
        print('Recovery location: PROJECT_DIR/backups/' + token)
        copies, staged, originals = (recovery / p for p in ('copies', 'staged', 'moved-originals'))
        for p in (copies, staged, originals):
            p.mkdir(mode=0o700)
        manifest = {'schema_version': 1, 'env_included': False, 'entries': {}}
        digest = lambda b: hashlib.sha256(b).hexdigest()
        for role, target, candidate in zip(ROLES, plan['targets'], plan['candidates']):
            data, meta = plan['states'][target]
            write_file(copies / (role + '.json'), data)
            write_file(staged / (role + '.json'), candidate, stat.S_IMODE(meta[2]))
            manifest['entries'][role] = {'sha256': digest(data), 'candidate_sha256': digest(candidate),
                                         'mode': stat.S_IMODE(meta[2]), 'mtime_ns': meta[4]}
        for role, target, candidate in zip(ROLES, plan['targets'], plan['candidates']):
            b, s = fingerprint(copies / (role + '.json'))
            if b != plan['states'][target][0] or stat.S_IMODE(s[2]) != 0o600:
                fail()
            b, s = fingerprint(staged / (role + '.json'))
            if b != candidate or stat.S_IMODE(s[2]) != stat.S_IMODE(plan['states'][target][1][2]):
                fail()
        write_file(recovery / 'backup-manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
        for p in (copies, staged, originals, recovery, parent, project):
            sync_dir(p)
        print('Backup verified for all three profiles before any target move or publication; .env excluded.')
        check_inputs(plan)
        started, published, published_states = [], {}, {}
        try:
            for role, target in zip(ROLES, plan['targets']):
                check_inputs(plan, targets=False)
                if fingerprint(target) != plan['states'][target]:
                    fail()
                started.append((role, target))
                rename_noreplace(target, originals / (role + '.json'))
            for role, target in zip(ROLES, plan['targets']):
                if not moved_matches(originals / (role + '.json'), plan['states'][target]):
                    fail()
            check_inputs(plan, targets=False)
            for role, target, candidate in zip(ROLES, plan['targets'], plan['candidates']):
                check_inputs(plan, targets=False)
                if any(not moved_matches(p, state) for p, state in published_states.items()):
                    fail()
                for old_role, old_target in zip(ROLES, plan['targets']):
                    if not moved_matches(originals / (old_role + '.json'), plan['states'][old_target]):
                        fail()
                state = fingerprint(staged / (role + '.json'))
                if state[0] != candidate or stat.S_IMODE(state[1][2]) != stat.S_IMODE(plan['states'][target][1][2]):
                    fail()
                # Register before the syscall: cleanup also handles an interrupt
                # immediately after a successful rename and before its return.
                published[target] = state[1][:2]
                published_states[target] = state
                rename_noreplace(staged / (role + '.json'), target)
            check_inputs(plan, targets=False)
            for role, target, candidate in zip(ROLES, plan['targets'], plan['candidates']):
                if not moved_matches(target, published_states[target]):
                    fail()
                if not moved_matches(originals / (role + '.json'), plan['states'][target]):
                    fail()
            for p in (*set(t.parent for t in plan['targets']), originals, staged, recovery):
                sync_dir(p)
            check_inputs(plan, targets=False)
            for role, target in zip(ROLES, plan['targets']):
                if not moved_matches(target, published_states[target]) or not moved_matches(
                        originals / (role + '.json'), plan['states'][target]):
                    fail()
        except BaseException:
            rollback(started, published, originals, recovery, plan['parents'])
            raise
        print('Updated exactly three profiles; actual original inodes retained in moved-originals.')
    finally:
        os.close(fd)


def confirm_update():
    try:
        with open('/dev/tty', 'r', encoding='utf-8') as reader, open('/dev/tty', 'w', encoding='utf-8') as writer:
            writer.write('Destructive replacement of the displayed LOCAL profile destinations.\n'
                         'Type UPDATE PROFILES exactly to confirm: ')
            writer.flush()
            return reader.readline().rstrip('\n') == 'UPDATE PROFILES'
    except OSError:
        return False


def update_profiles(profile_dir, dry_run=False, confirm=None):
    """Return 'updated', 'noop' or 'dry-run'; reject via UpdateError/OSError.

    profile_dir is an absolute, non-symlink selected installed profile directory.
    confirm is a core-test callback only. The CLI exposes no auto-confirm option
    and always reads the exact UPDATE PROFILES phrase from /dev/tty.
    """
    plan = preview(profile_dir)
    if not plan['changed']:
        print('No semantic JSON changes; formatting left untouched.')
        return 'noop'
    if dry_run:
        print('DRY-RUN: no prompt, lock, backup or writes.')
        return 'dry-run'
    if os.geteuid() == 0:
        raise UpdateError('Refusing profile changes as root.')
    if not (confirm or confirm_update)():
        raise UpdateError('Profile update not explicitly confirmed; unchanged.')
    check_inputs(plan)
    replace_profiles(plan)
    return 'updated'


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'ERROR: Invalid arguments. Use --profile-dir DIR [--dry-run].\n')


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--profile-dir', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        update_profiles(args.profile_dir, dry_run=args.dry_run)
    except (ValueError, OSError, AttributeError, RecursionError):
        # No raw exceptions: these can contain private paths or profile values.
        print('ERROR: Profile update declined, conflicted or failed. Files were not blindly overwritten; '
              'check any retained PROJECT_DIR/backups recovery data before retrying.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
