"""Installer mode isolation; all helpers/targets below are synthetic fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UpdateProfileRoutingTests(unittest.TestCase):
    def fixture(self, root):
        (root / "scripts").mkdir()
        shutil.copy2(ROOT / "install.sh", root / "install.sh")
        helper = root / "scripts/update_orca_profiles.py"
        helper.write_text('import sys\nprint("SYNTHETIC_PROFILE_UPDATE", " ".join(sys.argv[1:]))\nsys.exit(1)\n')
        bindir = root / "bin"
        bindir.mkdir()
        marker = root / "forbidden-call"
        for name in ("sudo", "su", "apt-get", "curl", "git", "hermes", "node", "npm", "freecadcmd", "orca", "mkdir"):
            command = bindir / name
            command.write_text('#!/bin/sh\nprintf forbidden > "$FORBIDDEN_MARKER"\nexit 90\n')
            command.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + ":" + os.environ["PATH"], PROFILE_DIR=str(root / "selected profile"), HERMES_HOME=str(root / "fake home"), FORBIDDEN_MARKER=str(marker))
        return env, marker

    def test_update_profiles_dispatches_early_and_dry_run_does_not_forward_yes(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            env, marker = self.fixture(root)
            r = subprocess.run(["bash", str(root / "install.sh"), "--update-profiles", "--dry-run", "--yes"], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("SYNTHETIC_PROFILE_UPDATE", r.stdout)
            self.assertIn("--profile-dir " + str(root / "selected profile"), r.stdout)
            self.assertIn("--dry-run", r.stdout)
            self.assertNotIn("--yes", r.stdout)
            self.assertNotIn("[1/9]", r.stdout)
            self.assertFalse(marker.exists())
            self.assertFalse((root / "selected profile").exists())
            self.assertFalse((root / "fake home").exists())

    def test_update_profiles_modes_are_exclusive_before_any_helper_or_install_call(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            env, marker = self.fixture(root)
            for flags in [("--update-profiles", "--update-config"), ("--check", "--update-profiles"), ("--check", "--update-config", "--update-profiles"), ("--update-profiles", "--offline")]:
                with self.subTest(flags=flags):
                    r = subprocess.run(["bash", str(root / "install.sh"), *flags], env=env, text=True, capture_output=True, timeout=10)
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertNotIn("SYNTHETIC_PROFILE_UPDATE", r.stdout)
                    self.assertNotIn("[1/9]", r.stdout)
                    self.assertFalse(marker.exists())

    def test_normal_rerun_profile_function_preserves_local_customizations(self):
        # Exercise the real normal-rerun function, not main (which includes network).
        shell = (ROOT / "install.sh").read_text().rsplit('main "$@"', 1)[0]
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            env, marker = self.fixture(root)
            project = root / "workspace"
            paths = [project / "Druckprofile/Anycubic Kobra S1 0.4 nozzle.json", project / "Druckprofile/0.20mm Standard @Anycubic Kobra S1 0.4 nozzle.json", project / "Filamente/Anycubic PLA @Anycubic Kobra S1 0.4 nozzle.json"]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"local_customization": "PRIVATE_FIXTURE_VALUE", "nozzle_temperature": ["237"]}\n')
            env.update(PROJECT_DIR=str(project), ORCA_PROFILE_ROOT=str(root / "absent source"), work_src=str(ROOT))
            def snapshot():
                return {str(p.relative_to(project)): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_mode) for p in project.rglob("*") if p.is_file()}
            before = snapshot()
            r = subprocess.run(["bash", "-c", shell + "\nprepare_orca_profiles\n"], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("Existing Orca profiles kept unchanged", r.stdout)
            self.assertNotIn("PRIVATE_FIXTURE_VALUE", r.stdout + r.stderr)
            self.assertNotIn("SYNTHETIC_PROFILE_UPDATE", r.stdout)
            self.assertEqual(snapshot(), before)
            self.assertFalse(marker.exists())

    def test_real_installer_yes_and_piped_phrase_cannot_authorize_core(self):
        import json
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            env, marker = self.fixture(root)
            profile = root / 'selected profile'
            project, source = root / 'workspace', root / 'factory'
            profile.mkdir()
            project.mkdir()
            source.mkdir()
            names = ('Anycubic Kobra S1 0.4 nozzle',
                     '0.20mm Standard @Anycubic Kobra S1 0.4 nozzle',
                     'Anycubic PLA @Anycubic Kobra S1 0.4 nozzle')
            for role, name in zip(('machine', 'process', 'filament'), names):
                data: dict[str, object] = {'type': role, 'name': name}
                if role == 'machine':
                    data['machine_start_gcode'] = 'G9111\nM117'
                elif role == 'process':
                    data['wall_loops'] = '2'
                else:
                    data['bed_type'] = ['Cool Plate']
                stock = source / role / (name + '.json')
                stock.parent.mkdir()
                stock.write_text(json.dumps(data))
                target = project / ('Filamente' if role == 'filament' else 'Druckprofile') / stock.name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(stock.read_bytes())
            (profile / '.env').write_text('PROJECT_DIR="' + str(project) + '"\nORCA_PROFILE_ROOT="' + str(source) + '"\nPRIVATE_KEY=SECRET_FIXTURE_ONLY\n')
            def state():
                return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns, p.stat().st_ino, p.read_bytes() if p.is_file() else None) for p in [root, *root.rglob('*')]}
            before = state()
            r = subprocess.run(['bash', str(ROOT / 'install.sh'), '--update-profiles', '--yes'],
                               env=env, input='UPDATE PROFILES\n', text=True,
                               capture_output=True, start_new_session=True, timeout=10)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn('replace LOCAL customizations, not merge', r.stdout)
            self.assertNotIn('SECRET_FIXTURE_ONLY', r.stdout + r.stderr)
            self.assertNotIn('[1/9]', r.stdout)
            self.assertFalse(marker.exists())
            self.assertEqual(state(), before)

    def test_lone_installer_without_update_core_does_not_download_or_initialize_anything(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            env, marker = self.fixture(root)
            (root / "scripts/update_orca_profiles.py").unlink()
            r = subprocess.run(["bash", str(root / "install.sh"), "--update-profiles"], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("complete local clone", r.stderr)
            self.assertNotIn("[1/9]", r.stdout)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
