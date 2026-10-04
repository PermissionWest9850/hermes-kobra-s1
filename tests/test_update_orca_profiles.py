"""Stage 5: synthetic, isolated filesystem fixtures; never installed profiles/hardware."""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
NAMES = ('Anycubic Kobra S1 0.4 nozzle',
         '0.20mm Standard @Anycubic Kobra S1 0.4 nozzle',
         'Anycubic PLA @Anycubic Kobra S1 0.4 nozzle')
ROLES = ('machine', 'process', 'filament')


def snapshot(root):
    result = {}
    for p in [root, *sorted(root.rglob('*'))]:
        s = p.lstat()
        result[str(p.relative_to(root))] = (s.st_mode, s.st_mtime_ns, s.st_ino,
            p.read_bytes() if stat.S_ISREG(s.st_mode) else None)
    return result


class UpdateProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / 'selected'
        self.project = self.root / 'project space'
        self.source = self.root / 'stock'
        self.profile.mkdir()
        self.project.mkdir()
        self.source.mkdir()
        self.targets = []
        self.sources = []
        for role, name in zip(ROLES, NAMES):
            data: dict[str, object] = {'type': role, 'name': name}
            if role == 'machine':
                data.update(machine_start_gcode='G9111\nM117', nozzle_diameter=['0.4'])
            elif role == 'process':
                data.update(layer_height='0.2', wall_loops='2', sparse_infill_density='15%')
            else:
                data.update(bed_type=['Cool Plate'], nozzle_temperature=['205'], filament_type=['PLA'])
            src = self.source / role / (name + '.json')
            src.parent.mkdir()
            src.write_text(json.dumps(data, indent=2) + '\n')
            dest = self.project / ('Filamente' if role == 'filament' else 'Druckprofile') / src.name
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(src.read_bytes())
            dest.chmod(0o640)
            self.sources.append(src)
            self.targets.append(dest)
        self.env = self.profile / '.env'
        self.env.write_text(f'PROJECT_DIR="{self.project}"\nORCA_PROFILE_ROOT=\'{self.source}\'\nPRIVATE_TOKEN=ENV_SECRET_MARKER\n')
        (self.project / 'unrelated').write_text('keep')
        self.output = StringIO()

    def load(self):
        helper = ROOT / 'scripts/update_orca_profiles.py'
        self.assertTrue(helper.exists(), 'stage5 core does not exist yet')
        spec = importlib.util.spec_from_file_location('update_orca_profiles', helper)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_update(self, module, **kw):
        with redirect_stdout(self.output):
            return module.update_profiles(self.profile, **kw)

    def tty_cli(self, phrase, on_prompt=None, piped=False):
        import pty
        import select
        import time
        readfd, writefd = os.pipe() if piped else (-1, -1)
        pid, master = pty.fork()
        if pid == 0:
            try:
                if piped:
                    os.dup2(readfd, 0)
                    os.close(readfd)
                    os.close(writefd)
                os.execve(sys.executable, [sys.executable, '-B', str(ROOT / 'scripts/update_orca_profiles.py'),
                          '--profile-dir', str(self.profile)], {'PATH': os.environ.get('PATH', ''),
                          'PYTHONDONTWRITEBYTECODE': '1'})
            except BaseException:
                os._exit(99)
        if piped:
            os.close(readfd)
            os.write(writefd, b'UPDATE PROFILES\n')
            os.close(writefd)
        output, sent, status, reaped = b'', False, None, False
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b''
                    output += chunk
                    if b'Type UPDATE PROFILES exactly to confirm:' in output and not sent:
                        if on_prompt:
                            on_prompt()
                        os.write(master, phrase.encode() + b'\n')
                        sent = True
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    reaped = True
                    return os.waitstatus_to_exitcode(status), output.decode(errors='replace'), sent
            self.fail('fixture TTY timed out')
        finally:
            os.close(master)
            if not reaped:
                try:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                except ProcessLookupError:
                    pass

    def test_installer_dotenv_quote_roundtrip_for_special_character_paths(self):
        import subprocess
        m = self.load()
        old_project = self.project
        self.project = self.root / 'project "quote" \\ slash $literal `literal`'
        old_project.rename(self.project)
        self.targets = [self.project / p.relative_to(old_project) for p in self.targets]
        shell = (ROOT / 'install.sh').read_text().rsplit('main "$@"', 1)[0]
        lines = []
        values = [('PROJECT_DIR', self.project), ('ORCA_PROFILE_ROOT', self.source)]
        values.extend(zip(m.ENV_KEYS[2:], self.targets))
        for key, value in values:
            result = subprocess.run(['bash', '-c', shell + '\ndotenv_quote "$ROUNDTRIP_VALUE"\n'],
                                    env=dict(os.environ, ROUNDTRIP_VALUE=str(value)),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines.append(key + '=' + result.stdout)
        self.env.write_text('\n'.join(lines) + '\nPRIVATE_TOKEN=ENV_SECRET_MARKER\n')
        parsed = m.read_env(self.env.read_bytes())
        self.assertEqual(parsed['PROJECT_DIR'], self.project)
        self.assertEqual([parsed[k] for k in m.ENV_KEYS[2:]], self.targets)
        before = snapshot(self.root)
        self.assertEqual(self.run_update(m, dry_run=True), 'dry-run')
        self.assertEqual(snapshot(self.root), before)
        candidates = m.build_candidates(tuple(p.read_bytes() for p in self.sources))
        self.assertEqual(self.run_update(m, confirm=lambda: True), 'updated')
        self.assertEqual([p.read_bytes() for p in self.targets], list(candidates))
        self.assertNotIn(str(self.root), self.output.getvalue())

    def test_exported_path_keys_are_literal_data_and_duplicates_rejected(self):
        m = self.load()
        self.env.write_text('export PROJECT_DIR="' + str(self.project) + '"\n'
                            'export ORCA_PROFILE_ROOT="' + str(self.source) + '"\n'
                            'export PRIVATE_TOKEN=$(touch ENV_SECRET_MARKER)\n')
        before = snapshot(self.root)
        self.assertEqual(self.run_update(m, dry_run=True), 'dry-run')
        self.assertEqual(snapshot(self.root), before)
        with self.assertRaises(m.UpdateError):
            m.read_env(self.env.read_bytes() + ('PROJECT_DIR="' + str(self.project) + '"\n').encode())
        self.assertNotIn('ENV_SECRET_MARKER', self.output.getvalue())

    def test_malformed_or_concatenated_quoted_path_values_are_refused(self):
        m = self.load()
        for value in ('"/private/SECRET_MARKER', '"/private/SECRET_MARKER" suffix',
                      '"/private/SECRET_MARKER""extra"', "'/private/SECRET_MARKER' suffix"):
            with self.subTest(value=value), self.assertRaises(m.UpdateError):
                m.read_env(('PROJECT_DIR=' + value + '\nORCA_PROFILE_ROOT=/stock\n').encode())

    def test_controlling_tty_with_piped_phrase_still_requires_tty_approval(self):
        before = snapshot(self.root)
        code, out, sent = self.tty_cli('no', piped=True)
        self.assertTrue(sent)
        self.assertEqual(code, 1, out)
        self.assertEqual(snapshot(self.root), before)

    def test_real_tty_source_conflict_before_confirmation_writes_nothing(self):
        expected = []
        def mutate():
            self.sources[1].write_bytes(self.sources[1].read_bytes() + b' ')
            expected.append(snapshot(self.root))
        code, out, sent = self.tty_cli('UPDATE PROFILES', on_prompt=mutate)
        self.assertTrue(sent)
        self.assertEqual(code, 1, out)
        self.assertEqual(snapshot(self.root), expected[0])
        self.assertNotIn(str(self.root), out)

    def test_real_tty_env_conflict_before_confirmation_writes_nothing(self):
        expected = []
        def mutate():
            self.env.write_text(self.env.read_text() + 'UNRELATED=ENV_SECRET_MARKER\n')
            expected.append(snapshot(self.root))
        code, out, sent = self.tty_cli('UPDATE PROFILES', on_prompt=mutate)
        self.assertTrue(sent)
        self.assertEqual(code, 1, out)
        self.assertEqual(snapshot(self.root), expected[0])
        self.assertNotIn('ENV_SECRET_MARKER', out)

    def test_real_tty_target_conflict_before_confirmation_writes_nothing(self):
        expected = []
        def mutate():
            self.targets[1].write_bytes(self.targets[1].read_bytes() + b' ')
            expected.append(snapshot(self.root))
        code, out, sent = self.tty_cli('UPDATE PROFILES', on_prompt=mutate)
        self.assertTrue(sent)
        self.assertEqual(code, 1, out)
        self.assertEqual(snapshot(self.root), expected[0])

    def test_real_controlling_tty_exact_phrase_updates_only_selected_three(self):
        m = self.load()
        old = [p.read_bytes() for p in self.targets]
        env, stock = snapshot(self.profile), snapshot(self.source)
        code, out, sent = self.tty_cli('UPDATE PROFILES')
        self.assertTrue(sent)
        self.assertEqual(code, 0, out)
        candidates = m.build_candidates(tuple(p.read_bytes() for p in self.sources))
        self.assertEqual([p.read_bytes() for p in self.targets], list(candidates))
        backup = next((self.project / 'backups').glob('orca-*'))
        self.assertEqual([(backup / 'copies' / (r + '.json')).read_bytes() for r in ROLES], old)
        self.assertEqual(snapshot(self.profile), env)
        self.assertEqual(snapshot(self.source), stock)
        self.assertEqual((self.project / 'unrelated').read_text(), 'keep')
        for secret in ('ENV_SECRET_MARKER', str(self.root)):
            self.assertNotIn(secret, out)

    def test_real_tty_wrong_and_whitespace_phrases_decline_zero_writes(self):
        before = snapshot(self.root)
        for phrase in ('UPDATE CONFIG', 'UPDATE PROFILES ', ' UPDATE PROFILES', 'yes'):
            with self.subTest(phrase=phrase):
                code, out, sent = self.tty_cli(phrase)
                self.assertTrue(sent)
                self.assertEqual(code, 1, out)
                self.assertEqual(snapshot(self.root), before)

    def test_semantic_format_noop_does_not_prompt_or_create_files(self):
        m = self.load()
        for p, b in zip(self.targets, m.build_candidates(tuple(p.read_bytes() for p in self.sources))):
            p.write_text(json.dumps(json.loads(b), sort_keys=True, indent=4))
        before = snapshot(self.root)
        self.assertEqual(self.run_update(m, confirm=lambda: self.fail('noop prompt')), 'noop')
        self.assertEqual(snapshot(self.root), before)

    def test_decline_has_zero_writes(self):
        m = self.load()
        before = snapshot(self.root)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: False)
        self.assertEqual(snapshot(self.root), before)

    def test_root_refuses_profile_changes_before_confirmation(self):
        m = self.load()
        before = snapshot(self.root)
        with patch.object(m.os, 'geteuid', return_value=0), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: self.fail('root prompt'))
        self.assertEqual(snapshot(self.root), before)

    def test_ambient_paths_ignored_and_env_is_only_literal_data(self):
        m = self.load()
        self.env.write_text(self.env.read_text() + 'IGNORED=$(touch ENV_SECRET_MARKER)\nWIFI=ENV_SECRET_MARKER\n')
        before = snapshot(self.root)
        with patch.dict(os.environ, {'PROJECT_DIR': '/PRIVATE_REDIRECTION', 'ORCA_PROFILE_ROOT': '/PRIVATE_REDIRECTION',
                                     'KOBRA_MACHINE_PROFILE': '/PRIVATE_REDIRECTION'}):
            self.assertEqual(self.run_update(m, dry_run=True), 'dry-run')
        self.assertEqual(snapshot(self.root), before)
        self.assertNotIn('PRIVATE_REDIRECTION', self.output.getvalue())
        self.assertNotIn('ENV_SECRET_MARKER', self.output.getvalue())

    def test_custom_override_inside_project_only(self):
        m = self.load()
        custom = self.project / 'custom PRIVATE_MARKER.json'
        self.targets[0].rename(custom)
        self.env.write_text(self.env.read_text() + f'KOBRA_MACHINE_PROFILE="{custom}"\n')
        self.assertEqual(self.run_update(m, confirm=lambda: True), 'updated')
        self.assertEqual(custom.read_bytes(), m.build_candidates(tuple(p.read_bytes() for p in self.sources))[0])
        self.assertFalse(self.targets[0].exists())
        self.assertIn('machine: (custom inside PROJECT_DIR)', self.output.getvalue())
        self.assertNotIn('PRIVATE_MARKER', self.output.getvalue())

    def test_core_has_no_process_network_or_hardware_calls(self):
        import socket
        import subprocess
        m = self.load()
        prohibited = lambda *a, **kw: self.fail('prohibited process/network call')
        with patch.object(socket, 'socket', prohibited), patch.object(socket, 'create_connection', prohibited), \
             patch.object(subprocess, 'run', prohibited), patch.object(subprocess, 'Popen', prohibited), \
             patch.object(os, 'system', prohibited):
            self.assertEqual(self.run_update(m, confirm=lambda: True), 'updated')

    def test_backup_write_failure_leaves_all_original_targets_untouched(self):
        m = self.load()
        before = [m.fingerprint(p) for p in self.targets]
        real = m.write_file
        def fail_copy(path, data, *args):
            if path.parent.name == 'copies' and path.name == 'process.json':
                raise OSError('PRIVATE_BACKUP_FAILURE')
            return real(path, data, *args)
        with patch.object(m, 'write_file', side_effect=fail_copy), self.assertRaises(OSError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([m.fingerprint(p) for p in self.targets], before)
        recovery = next((self.project / 'backups').glob('orca-*'))
        self.assertEqual(list((recovery / 'moved-originals').iterdir()), [])
        self.assertNotIn('PRIVATE_BACKUP_FAILURE', self.output.getvalue())

    def test_transaction_entrypoint_root_refusal_creates_no_lock(self):
        m = self.load()
        with redirect_stdout(self.output):
            plan = m.preview(self.profile)
        before = snapshot(self.root)
        with patch.object(os, 'geteuid', return_value=0), self.assertRaises(m.UpdateError):
            m.replace_profiles(plan)
        self.assertEqual(snapshot(self.root), before)

    def test_source_edit_during_final_fsync_triggers_rollback(self):
        m = self.load()
        real = m.sync_dir
        before = [p.read_bytes() for p in self.targets]
        def inject(path):
            result = real(path)
            if path.name == 'staged' and (path.parent / 'moved-originals/machine.json').exists():
                self.sources[1].write_bytes(self.sources[1].read_bytes() + b' ')
            return result
        with patch.object(m, 'sync_dir', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([p.read_bytes() for p in self.targets], before)
        self.assertTrue(self.sources[1].read_bytes().endswith(b' '))

    def test_private_gcode_urls_paths_and_sensitive_field_names_never_printed(self):
        m = self.load()
        marker = 'PROFILE_SECRET_MARKER'
        machine = json.loads(self.targets[0].read_bytes())
        machine['machine_start_gcode'] = '\x1b[31m; ' + marker + ' https://user:' + marker + '@example.invalid\nM117'
        machine['machine_end_gcode'] = '/private/' + marker
        machine['token_' + marker] = marker
        self.targets[0].write_text(json.dumps(machine))
        process = json.loads(self.targets[1].read_bytes())
        process.update(comments=marker, password=marker, api_key=marker,
                       credential={'wall_loops': marker}, local_path='/private/' + marker,
                       url='https://user:' + marker + '@example.invalid', wall_loops='5')
        self.targets[1].write_text(json.dumps(process))
        filament = json.loads(self.targets[2].read_bytes())
        filament['nozzle_temperature'] = ['225']
        self.targets[2].write_text(json.dumps(filament))
        self.env.write_text(self.env.read_text() + 'UNKNOWN=https://user:ENV_SECRET_MARKER@example.invalid\n')
        before = snapshot(self.root)
        self.assertEqual(self.run_update(m, dry_run=True), 'dry-run')
        self.assertEqual(snapshot(self.root), before)
        out = self.output.getvalue()
        for private in (marker, 'ENV_SECRET_MARKER', 'https://', '/private/', '\x1b', 'token_', 'api_key', '/password', '/credential'):
            self.assertNotIn(private, out)
        self.assertIn('CHANGE /nozzle_temperature: ["225"] -> ["205"]', out)
        self.assertIn('CHANGE /wall_loops: "5" -> "2"', out)
        self.assertIn('CHANGE /machine_start_gcode: [private value redacted]', out)
        self.assertIn('private field', out)

    def test_cli_invalid_env_path_fixed_diagnostic_no_secret_payload(self):
        import subprocess
        marker = 'PATH_SECRET_MARKER'
        self.env.write_text('PROJECT_DIR=https://user:' + marker + '@example.invalid\nORCA_PROFILE_ROOT=/private/' + marker + '\n')
        before = snapshot(self.root)
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/update_orca_profiles.py'),
                                '--profile-dir', str(self.profile), '--dry-run'],
                                capture_output=True, text=True, start_new_session=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(snapshot(self.root), before)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_dryrun_decline_do_not_create_or_modify_repository_bytecode(self):
        m = self.load()
        roots = [ROOT / 'scripts/__pycache__', ROOT / 'tests/__pycache__']
        before = [snapshot(p) if p.exists() else None for p in roots]
        self.run_update(m, dry_run=True)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: False)
        after = [snapshot(p) if p.exists() else None for p in roots]
        self.assertEqual(after, before)
        self.assertFalse(any((p / 'update_orca_profiles.cpython-313.pyc').exists() for p in roots))

    def test_selected_env_only_no_other_private_files_read_or_changed(self):
        m = self.load()
        forbidden = (self.profile / 'passwords.json', self.profile / 'auth.json',
                     self.profile / 'wifi.env', self.project / '.env',
                     self.project / 'launcher.sh', self.project / 'config.yaml',
                     self.project / 'Filamente/Other Material.json')
        for path in forbidden:
            path.write_bytes(b'PRIVATE_NOT_SELECTED')
        before = {p: m.fingerprint(p) for p in forbidden}
        real = os.open
        def guarded(path, *args, **kw):
            self.assertNotIn(Path(path), forbidden, 'non-selected private file access')
            return real(path, *args, **kw)
        with patch.object(os, 'open', side_effect=guarded):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual({p: m.fingerprint(p) for p in forbidden}, before)
        self.assertNotIn('PRIVATE_NOT_SELECTED', self.output.getvalue())

    def confirmation_conflict(self, mutate):
        m = self.load()
        expected = []
        def confirm():
            mutate()
            expected.append(snapshot(self.root))
            return True
        with self.assertRaises((m.UpdateError, OSError)):
            self.run_update(m, confirm=confirm)
        self.assertEqual(snapshot(self.root), expected[0])
        self.assertFalse((self.project / 'backups').exists())
        self.assertFalse((self.project / '.orca-profile-update.lock').exists())

    def test_source_edit_during_confirmation_cancels_before_writes(self):
        self.confirmation_conflict(lambda: self.sources[1].write_bytes(self.sources[1].read_bytes() + b' '))

    def test_env_edit_during_confirmation_cancels_before_writes(self):
        self.confirmation_conflict(lambda: self.env.write_text(self.env.read_text() + 'UNRELATED=PRIVATE_EDIT\n'))

    def test_target_edit_during_confirmation_cancels_before_writes(self):
        self.confirmation_conflict(lambda: self.targets[1].write_bytes(self.targets[1].read_bytes() + b' '))

    def test_target_mode_during_confirmation_cancels_before_writes(self):
        self.confirmation_conflict(lambda: self.targets[1].chmod(0o600))

    def test_target_inode_during_confirmation_cancels_before_writes(self):
        def mutate():
            old = self.targets[1].read_bytes()
            self.targets[1].unlink()
            self.targets[1].write_bytes(old)
        self.confirmation_conflict(mutate)

    def test_parent_identity_during_confirmation_cancels_before_writes(self):
        def mutate():
            parent = self.project / 'Druckprofile'
            parent.rename(self.project / 'parent-old')
            parent.mkdir()
        self.confirmation_conflict(mutate)

    def test_source_edit_during_backup_cancels_before_move(self):
        m = self.load()
        before = [m.fingerprint(p) for p in self.targets]
        real = m.write_file
        def inject(path, data, *args):
            real(path, data, *args)
            if path.parent.name == 'copies' and path.name == 'filament.json':
                self.sources[1].write_bytes(self.sources[1].read_bytes() + b' ')
        with patch.object(m, 'write_file', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([m.fingerprint(p) for p in self.targets], before)
        recovery = next((self.project / 'backups').glob('orca-*'))
        self.assertEqual(list((recovery / 'moved-originals').iterdir()), [])

    def test_target_edit_during_backup_preserved_without_move(self):
        m = self.load()
        real = m.write_file
        edited = self.targets[0].read_bytes() + b' '
        def inject(path, data, *args):
            real(path, data, *args)
            if path.parent.name == 'copies' and path.name == 'filament.json':
                self.targets[0].write_bytes(edited)
        with patch.object(m, 'write_file', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual(self.targets[0].read_bytes(), edited)
        recovery = next((self.project / 'backups').glob('orca-*'))
        self.assertEqual(list((recovery / 'moved-originals').iterdir()), [])

    def test_corrupt_backup_detected_before_any_move(self):
        m = self.load()
        before = [m.fingerprint(p) for p in self.targets]
        real = m.write_file
        def inject(path, data, *args):
            real(path, data, *args)
            if path.parent.name == 'copies' and path.name == 'filament.json':
                path.write_bytes(b'CORRUPT_PRIVATE_MARKER')
        with patch.object(m, 'write_file', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([m.fingerprint(p) for p in self.targets], before)
        self.assertNotIn('CORRUPT_PRIVATE_MARKER', self.output.getvalue())

    def test_backup_and_fsync_verified_before_first_target_move(self):
        m = self.load()
        real, synced = m.rename_noreplace, []
        sync = m.sync_dir
        def fsync(path):
            synced.append(path)
            return sync(path)
        def inspect(src, dst):
            if src == self.targets[0]:
                recovery = dst.parent.parent
                manifest = json.loads((recovery / 'backup-manifest.json').read_bytes())
                self.assertFalse(manifest['env_included'])
                for role, p in zip(ROLES, self.targets):
                    copy = recovery / 'copies' / (role + '.json')
                    self.assertEqual(copy.read_bytes(), p.read_bytes())
                    self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
                    self.assertNotEqual(copy.stat().st_ino, p.stat().st_ino)
                self.assertIn(recovery / 'copies', synced)
                self.assertIn(recovery, synced)
            return real(src, dst)
        with patch.object(m, 'rename_noreplace', side_effect=inspect), patch.object(m, 'sync_dir', side_effect=fsync):
            self.assertEqual(self.run_update(m, confirm=lambda: True), 'updated')

    def test_source_change_during_move_restores_originals(self):
        m = self.load()
        before = [p.read_bytes() for p in self.targets]
        real = m.rename_noreplace
        def inject(src, dst):
            result = real(src, dst)
            if src == self.targets[0]:
                self.sources[1].write_bytes(self.sources[1].read_bytes() + b' ')
            return result
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([p.read_bytes() for p in self.targets], before)
        self.assertTrue(self.sources[1].read_bytes().endswith(b' '))

    def test_target_change_before_its_move_not_lost(self):
        m = self.load()
        real = m.rename_noreplace
        changed = self.targets[1].read_bytes() + b' '
        def inject(src, dst):
            result = real(src, dst)
            if src == self.targets[0]:
                self.targets[1].write_bytes(changed)
            return result
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual(self.targets[1].read_bytes(), changed)
        self.assertEqual(self.targets[0].read_bytes(), self.sources[0].read_bytes())

    def stale_fd_during_transaction(self, event):
        m = self.load()
        real = m.rename_noreplace
        before = self.targets[0].read_bytes()
        changed = before + b' '
        with self.targets[0].open('r+b') as held:
            def inject(src, dst):
                result = real(src, dst)
                fire = (event == 'move' and src == self.targets[2]) or \
                       (event == 'publish' and src.parent.name == 'staged' and dst == self.targets[0])
                if fire:
                    held.seek(0)
                    held.write(changed)
                    held.truncate()
                    held.flush()
                return result
            with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(m.UpdateError):
                self.run_update(m, confirm=lambda: True)
        self.assertEqual(self.targets[0].read_bytes(), changed)
        self.assertEqual(self.targets[1].read_bytes(), self.sources[1].read_bytes())
        self.assertEqual(self.targets[2].read_bytes(), self.sources[2].read_bytes())

    def test_stale_fd_edit_after_moves_verified_and_restored(self):
        self.stale_fd_during_transaction('move')

    def test_stale_fd_edit_during_publish_verified_and_restored(self):
        self.stale_fd_during_transaction('publish')

    def test_stale_fd_post_success_actual_inode_retained(self):
        m = self.load()
        old = self.targets[0].read_bytes()
        with self.targets[0].open('r+b') as held:
            oldino = os.fstat(held.fileno()).st_ino
            self.run_update(m, confirm=lambda: True)
            held.seek(0)
            held.write(b'POST_SUCCESS_OLD_FD_EDIT')
            held.truncate()
            held.flush()
        recovery = next((self.project / 'backups').glob('orca-*'))
        moved = recovery / 'moved-originals/machine.json'
        self.assertEqual(moved.stat().st_ino, oldino)
        self.assertEqual(moved.read_bytes(), b'POST_SUCCESS_OLD_FD_EDIT')
        self.assertEqual((recovery / 'copies/machine.json').read_bytes(), old)
        self.assertEqual(self.targets[0].read_bytes(), m.build_candidates(tuple(p.read_bytes() for p in self.sources))[0])

    def source_change_during_publish(self, selected):
        m = self.load()
        real = m.rename_noreplace
        before = [p.read_bytes() for p in self.targets]
        def inject(src, dst):
            result = real(src, dst)
            if src.parent.name == 'staged' and dst == self.targets[selected]:
                self.sources[1].write_bytes(self.sources[1].read_bytes() + b' ')
            return result
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([p.read_bytes() for p in self.targets], before)
        self.assertTrue(self.sources[1].read_bytes().endswith(b' '))

    def test_source_edit_during_first_publish_triggers_rollback(self):
        self.source_change_during_publish(0)

    def test_source_edit_during_last_publish_triggers_rollback(self):
        self.source_change_during_publish(2)

    def concurrent_destination(self, kind):
        m = self.load()
        real = m.rename_noreplace
        before = [p.read_bytes() for p in self.targets]
        unknown = self.targets[1]
        def inject(src, dst):
            if src.parent.name == 'staged' and dst == unknown:
                if kind == 'file':
                    unknown.write_bytes(b'PRIVATE_NEW_REVISION')
                elif kind == 'directory':
                    unknown.mkdir()
                else:
                    unknown.symlink_to(self.project / 'unrelated')
            return real(src, dst)
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(OSError):
            self.run_update(m, confirm=lambda: True)
        if kind == 'file':
            self.assertEqual(unknown.read_bytes(), b'PRIVATE_NEW_REVISION')
        elif kind == 'directory':
            self.assertTrue(unknown.is_dir())
            self.assertEqual(list(unknown.iterdir()), [])
        else:
            self.assertTrue(unknown.is_symlink())
            self.assertEqual(os.readlink(unknown), str(self.project / 'unrelated'))
        recovery = next((self.project / 'backups').glob('orca-*'))
        self.assertEqual((recovery / 'moved-originals/process.json').read_bytes(), before[1])
        self.assertEqual(self.targets[0].read_bytes(), before[0])
        self.assertEqual(self.targets[2].read_bytes(), before[2])
        self.assertIn('manual recovery', self.output.getvalue())
        self.assertNotIn('PRIVATE_NEW_REVISION', self.output.getvalue())

    def test_concurrent_new_file_never_overwritten(self):
        self.concurrent_destination('file')

    def test_concurrent_empty_directory_never_overwritten(self):
        self.concurrent_destination('directory')

    def test_concurrent_symlink_never_overwritten(self):
        self.concurrent_destination('symlink')

    def test_rollback_restore_failure_preserves_original_and_withdrawn_revision(self):
        m = self.load()
        real = m.rename_noreplace
        before = self.targets[0].read_bytes()
        def inject(src, dst):
            if src.parent.name == 'staged' and dst == self.targets[1]:
                raise OSError('PRIVATE_PUBLISH_FAILURE')
            if src.parent.name == 'moved-originals' and dst == self.targets[0]:
                raise OSError('PRIVATE_ROLLBACK_FAILURE')
            return real(src, dst)
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(OSError):
            self.run_update(m, confirm=lambda: True)
        recovery = next((self.project / 'backups').glob('orca-*'))
        self.assertEqual((recovery / 'moved-originals/machine.json').read_bytes(), before)
        self.assertTrue((recovery / 'withdrawn/machine.json').exists())
        self.assertFalse(self.targets[0].exists())
        self.assertIn('manual recovery', self.output.getvalue())
        self.assertNotIn('PRIVATE_ROLLBACK_FAILURE', self.output.getvalue())

    def test_unknown_revision_replacing_our_inode_is_not_withdrawn(self):
        m = self.load()
        real = m.rename_noreplace
        before = self.targets[0].read_bytes()
        def inject(src, dst):
            result = real(src, dst)
            if src.parent.name == 'staged' and dst == self.targets[0]:
                dst.rename(self.project / 'concurrently-retained-candidate.json')
                dst.write_bytes(b'UNKNOWN_PRIVATE_REVISION')
            return result
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual(self.targets[0].read_bytes(), b'UNKNOWN_PRIVATE_REVISION')
        recovery = next((self.project / 'backups').glob('orca-*'))
        self.assertEqual((recovery / 'moved-originals/machine.json').read_bytes(), before)
        self.assertFalse((recovery / 'withdrawn/machine.json').exists())
        self.assertIn('manual recovery', self.output.getvalue())

    def test_lock_contention_nonblocking_no_profile_changes(self):
        import fcntl
        m = self.load()
        lock = self.project / '.orca-profile-update.lock'
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = snapshot(self.root)
            with self.assertRaises(BlockingIOError):
                self.run_update(m, confirm=lambda: True)
            self.assertEqual(snapshot(self.root), before)
        finally:
            os.close(fd)

    def test_keyboard_interrupt_after_successful_move_rolls_back_and_propagates(self):
        m = self.load()
        before = [p.read_bytes() for p in self.targets]
        real = m.rename_noreplace
        def inject(src, dst):
            result = real(src, dst)
            if src == self.targets[1] and dst.parent.name == 'moved-originals':
                raise KeyboardInterrupt()
            return result
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(KeyboardInterrupt):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([p.read_bytes() for p in self.targets], before)

    def test_keyboard_interrupt_after_successful_publish_rolls_back_and_propagates(self):
        m = self.load()
        before = [p.read_bytes() for p in self.targets]
        real = m.rename_noreplace
        def inject(src, dst):
            result = real(src, dst)
            if src.parent.name == 'staged' and dst == self.targets[0]:
                raise KeyboardInterrupt()
            return result
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(KeyboardInterrupt):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([p.read_bytes() for p in self.targets], before)

    def test_unsupported_atomic_rename_fails_closed_no_overwrite_fallback(self):
        m = self.load()
        before = [p.read_bytes() for p in self.targets]
        with patch.object(m, 'rename_noreplace', side_effect=OSError('unsupported')), \
             patch.object(os, 'replace', side_effect=AssertionError('overwrite fallback')), self.assertRaises(OSError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual([p.read_bytes() for p in self.targets], before)

    def test_published_edit_detected_before_next_publish(self):
        m = self.load()
        real = m.rename_noreplace
        published = []
        def inject(src, dst):
            result = real(src, dst)
            if src.parent.name == 'staged':
                published.append(dst)
                if dst == self.targets[0]:
                    dst.write_bytes(b'PRIVATE_CONCURRENT_EDIT')
            return result
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        self.assertEqual(published, [self.targets[0]])
        recovery = next((self.project / 'backups').glob('orca-*'))
        self.assertEqual((recovery / 'withdrawn/machine.json').read_bytes(), b'PRIVATE_CONCURRENT_EDIT')
        self.assertNotIn('PRIVATE_CONCURRENT_EDIT', self.output.getvalue())

    def test_invalid_candidate_infill_rejected(self):
        m = self.load()
        data = json.loads(self.sources[1].read_bytes())
        data['sparse_infill_density'] = '101%'
        self.sources[1].write_text(json.dumps(data))
        before = snapshot(self.root)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, dry_run=True)
        self.assertEqual(snapshot(self.root), before)

    def test_invalid_candidate_wall_count_rejected(self):
        m = self.load()
        data = json.loads(self.sources[1].read_bytes())
        data['wall_loops'] = 'PROFILE_SECRET_MARKER'
        self.sources[1].write_text(json.dumps(data))
        before = snapshot(self.root)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, dry_run=True)
        self.assertEqual(snapshot(self.root), before)

    def test_invalid_candidate_temperature_rejected(self):
        m = self.load()
        data = json.loads(self.sources[2].read_bytes())
        data['nozzle_temperature'] = ['PROFILE_SECRET_MARKER']
        self.sources[2].write_text(json.dumps(data))
        before = snapshot(self.root)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, dry_run=True)
        self.assertEqual(snapshot(self.root), before)

    def test_invalid_candidate_reference_structure_rejected(self):
        m = self.load()
        data = json.loads(self.sources[1].read_bytes())
        data['compatible_printers'] = 'PRIVATE_REFERENCE_MARKER'
        self.sources[1].write_text(json.dumps(data))
        before = snapshot(self.root)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, dry_run=True)
        self.assertEqual(snapshot(self.root), before)
        self.assertNotIn('PRIVATE_REFERENCE_MARKER', self.output.getvalue())

    def test_parent_swap_during_transaction_preserves_originals_for_manual_recovery(self):
        m = self.load()
        real = m.rename_noreplace
        def inject(src, dst):
            result = real(src, dst)
            if src == self.targets[2] and dst.parent.name == 'moved-originals':
                old = self.project / 'Druckprofile'
                old.rename(self.project / 'replaced-parent')
                old.mkdir()
            return result
        with patch.object(m, 'rename_noreplace', side_effect=inject), self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: True)
        recovery = next((self.project / 'backups').glob('orca-*'))
        self.assertTrue((recovery / 'moved-originals/machine.json').exists())
        self.assertTrue((recovery / 'moved-originals/process.json').exists())
        self.assertEqual(list((self.project / 'Druckprofile').iterdir()), [])
        self.assertIn('manual recovery', self.output.getvalue())
        self.assertEqual(self.targets[2].read_bytes(), self.sources[2].read_bytes())

    def assert_invalid_without_writes(self):
        m = self.load()
        before = snapshot(self.root)
        with self.assertRaises((m.UpdateError, OSError)):
            self.run_update(m, dry_run=True, confirm=lambda: self.fail('invalid prompt'))
        self.assertEqual(snapshot(self.root), before)
        for text in ('ENV_SECRET_MARKER', 'PROFILE_SECRET_MARKER', str(self.root)):
            self.assertNotIn(text, self.output.getvalue())

    def test_precondition_missing_target(self):
        self.targets[0].unlink()
        self.assert_invalid_without_writes()

    def test_precondition_no_existing_targets(self):
        for p in self.targets:
            p.unlink()
        self.assert_invalid_without_writes()

    def test_precondition_missing_source(self):
        self.sources[1].unlink()
        self.assert_invalid_without_writes()

    def test_precondition_missing_selected_env(self):
        self.env.unlink()
        self.assert_invalid_without_writes()

    def test_precondition_invalid_json(self):
        self.targets[0].write_text('PROFILE_SECRET_MARKER')
        self.assert_invalid_without_writes()

    def test_precondition_duplicate_json_keys(self):
        self.targets[1].write_text('{"name":"PROFILE_SECRET_MARKER","name":"x"}')
        self.assert_invalid_without_writes()

    def test_precondition_nan(self):
        self.sources[1].write_text('{"private":NaN}')
        self.assert_invalid_without_writes()

    def test_precondition_non_object_json(self):
        self.targets[1].write_text('["PROFILE_SECRET_MARKER"]')
        self.assert_invalid_without_writes()

    def test_precondition_wrong_role(self):
        data = json.loads(self.targets[0].read_bytes())
        data['type'] = 'process'
        self.targets[0].write_text(json.dumps(data))
        self.assert_invalid_without_writes()

    def test_precondition_wrong_candidate_name(self):
        data = json.loads(self.sources[2].read_bytes())
        data['name'] = 'PROFILE_SECRET_MARKER'
        self.sources[2].write_text(json.dumps(data))
        self.assert_invalid_without_writes()

    def test_precondition_invalid_gcode_type(self):
        data = json.loads(self.sources[0].read_bytes())
        data['machine_start_gcode'] = ['PROFILE_SECRET_MARKER']
        self.sources[0].write_text(json.dumps(data))
        self.assert_invalid_without_writes()

    def test_precondition_invalid_diameter(self):
        data = json.loads(self.sources[0].read_bytes())
        data['nozzle_diameter'] = ['0']
        self.sources[0].write_text(json.dumps(data))
        self.assert_invalid_without_writes()

    def test_precondition_invalid_layer_height(self):
        data = json.loads(self.sources[1].read_bytes())
        data['layer_height'] = 'PROFILE_SECRET_MARKER'
        self.sources[1].write_text(json.dumps(data))
        self.assert_invalid_without_writes()

    def test_precondition_symlink_target(self):
        self.targets[0].unlink()
        self.targets[0].symlink_to(self.sources[0])
        self.assert_invalid_without_writes()

    def test_precondition_symlink_source(self):
        self.sources[0].unlink()
        self.sources[0].symlink_to(self.targets[0])
        self.assert_invalid_without_writes()

    def test_precondition_symlink_env(self):
        dest = self.root / 'env-real'
        self.env.rename(dest)
        self.env.symlink_to(dest)
        self.assert_invalid_without_writes()

    def test_precondition_symlink_parent(self):
        old = self.project / 'Druckprofile'
        dest = self.project / 'real-parent'
        old.rename(dest)
        old.symlink_to(dest, target_is_directory=True)
        self.assert_invalid_without_writes()

    def test_precondition_fifo_target_nonblocking(self):
        self.targets[1].unlink()
        os.mkfifo(self.targets[1])
        self.assert_invalid_without_writes()

    def test_precondition_directory_target(self):
        self.targets[1].unlink()
        self.targets[1].mkdir()
        self.assert_invalid_without_writes()

    def test_precondition_hardlinked_target(self):
        os.link(self.targets[0], self.project / 'alias.json')
        self.assert_invalid_without_writes()

    def test_precondition_external_override_no_workaround(self):
        external = self.root / 'PRIVATE_SECRET_PATH.json'
        external.write_bytes(self.targets[0].read_bytes())
        self.env.write_text(self.env.read_text() + f'KOBRA_MACHINE_PROFILE="{external}"\n')
        self.assert_invalid_without_writes()
        self.assertNotIn('PRIVATE_SECRET_PATH', self.output.getvalue())

    def test_precondition_duplicate_overrides(self):
        self.env.write_text(self.env.read_text() + f'KOBRA_MACHINE_PROFILE="{self.targets[1]}"\n')
        self.assert_invalid_without_writes()

    def test_precondition_duplicate_env_key(self):
        self.env.write_text(self.env.read_text() + f'PROJECT_DIR="{self.project}"\n')
        self.assert_invalid_without_writes()

    def test_precondition_relative_path(self):
        self.env.write_text('PROJECT_DIR=relative\nORCA_PROFILE_ROOT=/PROFILE_SECRET_MARKER\n')
        self.assert_invalid_without_writes()

    def test_precondition_traversal_path(self):
        self.env.write_text(f'PROJECT_DIR="{self.project}/../project space"\nORCA_PROFILE_ROOT="{self.source}"\n')
        self.assert_invalid_without_writes()

    def test_precondition_shell_text_path_never_evaluated(self):
        self.env.write_text(f'PROJECT_DIR="$(touch {self.root}/PROFILE_SECRET_MARKER)"\nORCA_PROFILE_ROOT="{self.source}"\n')
        self.assert_invalid_without_writes()
        self.assertFalse((self.root / 'PROFILE_SECRET_MARKER').exists())

    def test_precondition_missing_required_env_not_ambient(self):
        self.env.write_text('UNRELATED=ENV_SECRET_MARKER\n')
        with patch.dict(os.environ, {'PROJECT_DIR': str(self.project), 'ORCA_PROFILE_ROOT': str(self.source)}):
            self.assert_invalid_without_writes()

    def test_precondition_unsafe_lock_symlink(self):
        (self.project / '.orca-profile-update.lock').symlink_to(self.env)
        self.assert_invalid_without_writes()

    def test_precondition_unsafe_lock_permissions(self):
        lock = self.project / '.orca-profile-update.lock'
        lock.write_bytes(b'')
        lock.chmod(0o644)
        self.assert_invalid_without_writes()

    def test_different_target_filesystem_rejected(self):
        m = self.load()
        real = m.fingerprint
        def fake_device(path):
            data, meta = real(path)
            if path == self.targets[0]:
                meta = (meta[0] + 1, *meta[1:])
            return data, meta
        before = snapshot(self.root)
        with patch.object(m, 'fingerprint', side_effect=fake_device), self.assertRaises(m.UpdateError):
            self.run_update(m, dry_run=True)
        self.assertEqual(snapshot(self.root), before)

    def test_boolean_number_not_semantic_noop(self):
        m = self.load()
        candidates = m.build_candidates(tuple(p.read_bytes() for p in self.sources))
        for target, candidate in zip(self.targets, candidates):
            target.write_bytes(candidate)
        for p, value in ((self.sources[1], 1), (self.targets[1], True)):
            d = json.loads(p.read_bytes())
            d['enable_support'] = value
            p.write_text(json.dumps(d))
        self.assertEqual(self.run_update(m, dry_run=True), 'dry-run')
        self.assertIn('CHANGE /enable_support: true -> 1', self.output.getvalue())

    def test_recovery_symlink_rejected_before_prompt(self):
        m = self.load()
        (self.project / 'backups').symlink_to(self.source, target_is_directory=True)
        before = snapshot(self.root)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: self.fail('unsafe recovery prompt'))
        self.assertEqual(snapshot(self.root), before)

    def test_overlapping_project_source_rejected_before_prompt(self):
        m = self.load()
        self.env.write_text(f'PROJECT_DIR="{self.project}"\nORCA_PROFILE_ROOT="{self.project}"\n')
        # A valid-looking stock tree within the project must not become a source/destination overlap.
        import shutil
        for role in ROLES:
            shutil.copytree(self.source / role, self.project / role)
        before = snapshot(self.root)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, confirm=lambda: self.fail('unsafe prompt'))
        self.assertEqual(snapshot(self.root), before)

    def test_cli_dry_run_no_tty_and_yes_never_authorize(self):
        import subprocess
        m = self.load()
        before = snapshot(self.root)
        command = [sys.executable, '-B', str(ROOT / 'scripts/update_orca_profiles.py'),
                   '--profile-dir', str(self.profile)]
        for extra, code in ((['--dry-run'], 0), ([], 1), (['--yes'], 2), (['--bad=CLI_SECRET_MARKER'], 2)):
            with self.subTest(extra=extra):
                result = subprocess.run(command + extra, input='UPDATE PROFILES\n', capture_output=True,
                                        text=True, start_new_session=True, timeout=10)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(snapshot(self.root), before)
                self.assertNotIn('ENV_SECRET_MARKER', result.stdout + result.stderr)
                self.assertNotIn('CLI_SECRET_MARKER', result.stdout + result.stderr)
                self.assertNotIn(str(self.root), result.stdout + result.stderr)

    def test_second_publish_failure_restores_actual_originals_retains_edits(self):
        m = self.load()
        before = [m.fingerprint(p) for p in self.targets]
        real = m.rename_noreplace
        def inject(src, dst):
            if src.parent.name == 'staged' and dst == self.targets[1]:
                self.targets[0].write_bytes(b'EDIT_OF_PUBLISHED_REVISION')
                raise OSError('SECRET_EXCEPTION_MARKER')
            return real(src, dst)
        with patch.object(m, 'rename_noreplace', side_effect=inject):
            with self.assertRaises(OSError):
                self.run_update(m, confirm=lambda: True)
        for p, old in zip(self.targets, before):
            actual = m.fingerprint(p)
            self.assertEqual(actual[0], old[0])
            self.assertEqual(actual[1][:5], old[1][:5])
        backups = list((self.project / 'backups').glob('orca-*'))
        self.assertEqual((backups[0] / 'withdrawn/machine.json').read_bytes(), b'EDIT_OF_PUBLISHED_REVISION')
        self.assertIn('restored', self.output.getvalue())
        self.assertNotIn('SECRET_EXCEPTION_MARKER', self.output.getvalue())

    def test_success_backup_verified_before_publish_modes_and_repeat_noop(self):
        m = self.load()
        originals = [p.read_bytes() for p in self.targets]
        env = snapshot(self.profile)
        source = snapshot(self.source)
        unrelated = (self.project / 'unrelated').read_bytes()
        self.assertEqual(self.run_update(m, confirm=lambda: True), 'updated')
        candidates = m.build_candidates(tuple(p.read_bytes() for p in self.sources))
        for p, b in zip(self.targets, candidates):
            self.assertEqual(p.read_bytes(), b)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o640)
        backups = list((self.project / 'backups').glob('orca-*'))
        self.assertEqual(len(backups), 1)
        backup = backups[0]
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        manifest = json.loads((backup / 'backup-manifest.json').read_text())
        self.assertFalse(manifest['env_included'])
        self.assertEqual(stat.S_IMODE((backup / 'backup-manifest.json').stat().st_mode), 0o600)
        for r, b in zip(ROLES, originals):
            self.assertEqual((backup / 'copies' / (r + '.json')).read_bytes(), b)
            self.assertEqual((backup / 'moved-originals' / (r + '.json')).read_bytes(), b)
            self.assertEqual(stat.S_IMODE((backup / 'copies' / (r + '.json')).stat().st_mode), 0o600)
            self.assertEqual(manifest['entries'][r]['mode'], 0o640)
        self.assertEqual(snapshot(self.profile), env)
        self.assertEqual(snapshot(self.source), source)
        self.assertEqual((self.project / 'unrelated').read_bytes(), unrelated)
        before = snapshot(self.root)
        self.assertEqual(self.run_update(m, confirm=lambda: self.fail('noop prompt')), 'noop')
        self.assertEqual(snapshot(self.root), before)

    def test_candidate_byte_parity_with_immutable_generator(self):
        import subprocess
        m = self.load()
        out = self.root / 'generator-only-fixture'
        source_before = snapshot(self.source)
        env = {'PATH': os.environ.get('PATH', ''), 'PROJECT_DIR': str(out),
               'ORCA_PROFILE_ROOT': str(self.source), 'PYTHONDONTWRITEBYTECODE': '1'}
        # The only permitted generator invocation: all input/output is this fixture.
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/prepare_orca_profiles.py')],
                                env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        candidates = m.build_candidates(tuple(p.read_bytes() for p in self.sources))
        for b, default in zip(candidates, m.DEFAULTS):
            self.assertEqual(b, (out / default).read_bytes())
        self.assertEqual(candidates[1], self.sources[1].read_bytes())
        self.assertEqual(snapshot(self.source), source_before)
        data = json.loads(self.sources[0].read_bytes())
        data['machine_start_gcode'] += '\nT[initial_tool]'
        self.sources[0].write_text(json.dumps(data))
        c = m.build_candidates(tuple(p.read_bytes() for p in self.sources))
        self.assertEqual(json.loads(c[0])['machine_start_gcode'].splitlines().count('T[initial_tool]'), 1)

    def test_json_exponent_overflow_rejected(self):
        m = self.load()
        self.targets[1].write_text('{"type":"process","name":' + json.dumps(NAMES[1]) + ',"private":1e999}')
        before = snapshot(self.root)
        with self.assertRaises(m.UpdateError):
            self.run_update(m, dry_run=True)
        self.assertEqual(snapshot(self.root), before)

    def test_dry_run_readable_diff_without_writes_or_secrets(self):
        m = self.load()
        data = json.loads(self.targets[1].read_bytes())
        data.update(wall_loops='5', sparse_infill_density='25%', custom='PROFILE_SECRET_MARKER',
                    password='PROFILE_SECRET_MARKER', **{'PROFILE_SECRET_MARKER': 'x'})
        self.targets[1].write_text(json.dumps(data))
        before = snapshot(self.root)
        self.assertEqual(self.run_update(m, dry_run=True, confirm=lambda: self.fail('prompt')), 'dry-run')
        self.assertEqual(snapshot(self.root), before)
        out = self.output.getvalue()
        for text in ('process', 'CHANGE /wall_loops: "5" -> "2"', 'CHANGE /sparse_infill_density: "25%" -> "15%"', 'REMOVE', 'T[initial_tool]', 'replace', 'not merge'):
            self.assertIn(text, out)
        for secret in ('ENV_SECRET_MARKER', 'PROFILE_SECRET_MARKER', str(self.root)):
            self.assertNotIn(secret, out)
