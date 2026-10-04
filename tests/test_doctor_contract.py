"""Parent acceptance regressions: no premature platform support or HTTP transport."""
import os
import unittest
from unittest import mock
from test_doctor import Fixture, load_doctor


class DoctorContractTests(unittest.TestCase):
    def setUp(self):
        self.d = load_doctor()
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def report(self, architecture="x86_64"):
        report = self.d.Report()
        with mock.patch.dict(os.environ, self.f.env, clear=True), mock.patch.object(self.d.platform, "machine", return_value=architecture):
            self.d.local_checks(report, self.f.profile, self.f.home, self.f.source, 1, os_release=self.f.os_release)
        return report

    def status(self, report, label):
        return next(c[0] for c in report.checks if c[1] == label)

    def test_unvalidated_distribution_must_not_claim_supported_or_tested(self):
        for distro, version in [("debian", "12"), ("ubuntu", "24.04"), ("ubuntu", "22.04")]:
            with self.subTest(distro=distro, version=version):
                self.f.os_release.write_text('ID=' + distro + '\nVERSION_ID="' + version + '"\n')
                report = self.report()
                self.assertEqual(self.status(report, "os"), "WARN")
                self.assertIn(version, report.render())
                self.assertIn("not validated", report.render())

    def test_baseline_os_and_architecture_are_reported_without_new_vm_claim(self):
        report = self.report()
        self.assertEqual(self.status(report, "os"), "PASS")
        self.assertIn("Debian 13", report.render())
        self.assertIn("v0.2.0 baseline", report.render())
        self.assertIn("x86_64", report.render())

    def test_aarch64_is_not_presented_as_supported(self):
        report = self.report("aarch64")
        self.assertEqual(self.status(report, "architecture"), "WARN")
        self.assertEqual(self.status(report, "orca-checksum"), "NOTVERIFIED")
        self.assertIn("aarch64", report.render())
        self.assertIn("blocked", report.render())

    def test_stdio_registration_rejects_an_additional_http_url(self):
        path = self.f.profile / "config.yaml"
        path.write_text(path.read_text() + '    url: https://invalid.example/mcp\n')
        report = self.report()
        self.assertEqual(self.status(report, "mcp-registration"), "FAIL")
        self.assertNotIn("invalid.example", report.render())

    def test_unverified_status_renders_with_requested_spacing(self):
        report = self.d.Report()
        report.add("NOTVERIFIED", "network", "offline")
        self.assertEqual(report.render(), "NOT VERIFIED network: offline")
        self.assertEqual(report.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
