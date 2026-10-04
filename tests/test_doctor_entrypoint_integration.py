"""Exercise both real shell entrypoints against ONLY synthetic loopback fixtures."""
import os
import shutil
import subprocess
import sys
import unittest
from test_doctor import ROOT, Fixture, MockMoonraker, SENTINEL, snapshot


class ActualDoctorEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.server = MockMoonraker()
        self.addCleanup(self.server.close)
        self.f.vars["KOBRA_S1_MOONRAKER_URL"] = self.server.url
        self.f.write_env()
        self.env = dict(self.f.env, PATH=str(self.f.bin) + ":" + os.environ["PATH"], PROFILE_DIR=str(self.f.profile))
        for directory in (self.f.source, self.f.profile):
            (directory / "scripts").mkdir(exist_ok=True)
            for name in ("doctor.py", "verify_orca.py"):
                shutil.copy2(ROOT / "scripts" / name, directory / "scripts" / name)
        for name in ("checksums", "patches"):
            shutil.copytree(self.f.source / name, self.f.profile / name)
        shutil.copy2(ROOT / "install.sh", self.f.source / "install.sh")
        for name in ("sudo", "su", "apt-get", "curl"):
            self.f.command(name, "exit 95")
        # Normal launcher creation is an allowed fixture setup step, NOT Doctor.
        functions = (ROOT / "install.sh").read_text()
        self.assertTrue(functions.rstrip().endswith('\nmain "$@"'))
        fixture = self.f.root / "launcher-fixture.sh"
        fixture.write_text(functions.rstrip()[:-len('main "$@"')])
        r = subprocess.run(["bash", "-c", 'script="$1"; shift; source "$script"; install_wrapper', "fixture", str(fixture)], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.f.command("mkdir", "exit 96")  # Doctor must never execute mkdir.
        self.entries = [["bash", str(self.f.source / "install.sh"), "--check"],
                        [str(self.f.home / ".local/bin/kobra3d"), "doctor"]]

    def invoke(self, arguments):
        before = snapshot(self.f.root)
        self.server.requests.clear()
        r = subprocess.run(arguments, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(snapshot(self.f.root), before)
        self.assertNotIn(SENTINEL, r.stdout + r.stderr)
        self.assertNotIn(str(self.f.root), r.stdout + r.stderr)
        self.server.assert_safe(self)
        return r

    def test_both_real_entrypoints_online_use_same_core_and_allowed_gets_only(self):
        reports = []
        for arguments in self.entries:
            with self.subTest(entry=arguments[0]):
                r = self.invoke(arguments)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(len(self.server.requests), 4)
                self.assertIn("PASS node:", r.stdout)
                self.assertIn("PASS tool-gate-mapping:", r.stdout)
                reports.append(r.stdout)
        self.assertEqual(reports[0], reports[1])

    def test_both_real_entrypoints_offline_preserve_code_one_and_zero_requests(self):
        for arguments in self.entries:
            with self.subTest(entry=arguments[0]):
                r = self.invoke([*arguments, "--offline"])
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(self.server.requests, [])
                self.assertIn("NOT VERIFIED moonraker-server:", r.stdout)

    def test_both_real_entrypoints_missing_env_preserve_code_two_and_zero_requests(self):
        (self.f.profile / ".env").unlink()
        for arguments in self.entries:
            with self.subTest(entry=arguments[0]):
                r = self.invoke(arguments)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertEqual(self.server.requests, [])
                self.assertIn("FAIL environment:", r.stdout)


if __name__ == "__main__":
    unittest.main()
