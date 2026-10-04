"""Routing tests use a synthetic doctor, never a live printer or user profile."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DoctorRoutingTests(unittest.TestCase):
    def test_check_dispatches_before_privileged_installation(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            (work / "scripts").mkdir()
            (work / "scripts/doctor.py").write_text('import sys\nprint("SYNTHETIC_DOCTOR", " ".join(sys.argv[1:]))\nsys.exit(1)\n')
            shutil.copy2(ROOT / "install.sh", work / "install.sh")
            bindir = work / "bin"
            bindir.mkdir()
            marker = work / "forbidden-action"
            for name in ["sudo", "su", "curl", "apt-get", "git", "hermes", "mkdir"]:
                p = bindir / name
                p.write_text('#!/bin/sh\nprintf forbidden > "$FORBIDDEN_MARKER"\nexit 88\n')
                p.chmod(0o755)
            env = dict(os.environ, PATH=str(bindir) + ":" + os.environ["PATH"], FORBIDDEN_MARKER=str(marker), PROFILE_DIR=str(work / "profile"), HERMES_HOME=str(work / "hermes"))
            r = subprocess.run(["bash", str(work / "install.sh"), "--check", "--offline"], env=env, text=True, capture_output=True, timeout=10)
            self.assertIn("SYNTHETIC_DOCTOR", r.stdout, r.stdout + r.stderr)
            self.assertEqual(r.returncode, 1, "Doctor exit code must be preserved")
            self.assertNotIn("[1/9]", r.stdout)
            self.assertNotIn("Installing requirements", r.stdout)
            self.assertFalse(marker.exists())
            self.assertFalse((work / "profile").exists())
            self.assertFalse((work / "hermes").exists())

    def _invoke_wrapper_builder(self, home):
        source = (ROOT / "install.sh").read_text()
        self.assertTrue(source.rstrip().endswith('\nmain "$@"'))
        source = source.rstrip()[:-len('main "$@"')]
        fixture = home / "installer-functions.sh"
        fixture.write_text(source)
        guards = home / "guards"
        guards.mkdir()
        for name in ["sudo", "su", "apt-get", "curl", "hermes", "git"]:
            guard = guards / name
            guard.write_text('#!/bin/sh\nprintf "forbidden\\n" >&2\nexit 99\n')
            guard.chmod(0o755)
        env = dict(os.environ, PATH=str(guards) + ":" + os.environ["PATH"], HOME=str(home), HERMES_HOME=str(home / ".hermes"), PROFILE_DIR=str(home / "profile"))
        return subprocess.run(["bash", "-c", 'script="$1"; shift; source "$script"; install_wrapper', "fixture", str(fixture)], env=env, text=True, capture_output=True, timeout=10)

    def test_fresh_launcher_dispatches_doctor_and_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as work:
            home = Path(work)
            (home / "profile/scripts").mkdir(parents=True)
            (home / "profile/scripts/doctor.py").write_text('import sys\nprint("LAUNCHER_DOCTOR", " ".join(sys.argv[1:]))\nsys.exit(2)\n')
            result = self._invoke_wrapper_builder(home)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            r = subprocess.run([str(home / ".local/bin/kobra3d"), "doctor", "--offline"], env=dict(os.environ, HOME=str(home)), capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("LAUNCHER_DOCTOR", r.stdout)
            self.assertIn("--offline", r.stdout)
            self.assertIn(str(home / "profile"), r.stdout)

    def test_existing_launcher_even_with_project_marker_is_kept_byte_identical(self):
        with tempfile.TemporaryDirectory() as work:
            home = Path(work)
            launcher = home / ".local/bin/kobra3d"
            launcher.parent.mkdir(parents=True)
            original = b'#!/bin/sh\n# Hermes Kobra S1 wrapper\nprintf "user modified launcher"\n'
            launcher.write_bytes(original)
            launcher.chmod(0o700)
            before = launcher.stat()
            r = self._invoke_wrapper_builder(home)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(launcher.read_bytes(), original)
            after = launcher.stat()
            self.assertEqual((before.st_mtime_ns, before.st_mode), (after.st_mtime_ns, after.st_mode))

    def test_klippermcp_shell_dispatch_uses_full_pin_without_npm_on_existing_target(self):
        with tempfile.TemporaryDirectory() as work:
            home = Path(work)
            (home / "scripts").mkdir()
            (home / "existing").mkdir()
            (home / "scripts/klippermcp_setup.py").write_text('import sys\nprint("SETUP_DISPATCH", " ".join(sys.argv[1:]))\n')
            source = (ROOT / "install.sh").read_text()
            self.assertTrue(source.rstrip().endswith('\nmain "$@"'))
            source = source.rstrip()[:-len('main "$@"')]
            fixture = home / "installer-functions.sh"
            fixture.write_text(source)
            guards = home / "guards"
            guards.mkdir()
            for name in ["sudo", "su", "apt-get", "curl", "npm", "git"]:
                guard = guards / name
                guard.write_text('#!/bin/sh\nprintf "forbidden\\n" >&2\nexit 99\n')
                guard.chmod(0o755)
            env = dict(os.environ, PATH=str(guards) + ":" + os.environ["PATH"], HOME=str(home), KLIPPER_MCP_PATH=str(home / "existing"))
            r = subprocess.run(["bash", "-c", 'script="$1"; root="$2"; shift 2; source "$script"; work_src="$root"; install_klippermcp', "fixture", str(fixture), str(home)], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("SETUP_DISPATCH", r.stdout)
            self.assertIn("--commit 425e16905c16b6c078028b5063fcb21e0591b190", r.stdout)
            self.assertNotIn("forbidden", r.stderr)
            self.assertIn("--patch", r.stdout)


if __name__ == "__main__":
    unittest.main()
