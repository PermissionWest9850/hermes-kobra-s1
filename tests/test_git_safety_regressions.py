"""Real-Git regressions, confined to synthetic repositories and fixture homes."""
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

import test_klippermcp_setup as setup_tests
from test_klippermcp_setup import git, load_helper, snapshot
from test_doctor import Fixture, load_doctor


class GitSafetyTests(unittest.TestCase):
    def setUp(self):
        fixture = setup_tests.SetupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.work = fixture.work
        self.home = self.work / "isolated-home"
        self.home.mkdir()
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home),
                           GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        clean_env = mock.patch.dict(os.environ, environment, clear=True)
        clean_env.start()
        self.addCleanup(clean_env.stop)
        self.m = load_helper()
        self.source, self.target, self.commit, self.patchfile, self.calls = fixture.fixture()

    def install(self):
        self.m.setup(self.target, str(self.source), self.commit, self.patchfile)

    def add_filter(self, target, kind="clean", included=False):
        marker = self.work / "filter-was-executed"
        program = self.work / "filter.py"
        program.write_text("import sys\nfrom pathlib import Path\n"
                           f"Path({str(marker)!r}).write_text('FILTER_VALUE_SECRET')\n"
                           + ("sys.exit(1)\n" if kind == "process" else
                            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"))
        (target / ".git/info/attributes").write_text("src/index.ts filter=filter-name-secret\n")
        command = f'"{sys.executable}" "{program}"'
        key = "filter.filter-name-secret." + kind
        if included:
            config = self.work / "included-config"
            git(self.work, "config", "--file", str(config), key, command)
            git(target, "config", "include.path", str(config))
        else:
            git(target, "config", key, command)
        return marker

    def assert_filter_refused_without_writes(self, operation, marker):
        before = snapshot(self.work)
        error = None
        try:
            operation()
        except self.m.SetupError as caught:
            error = caught
        self.assertFalse(marker.exists(), "patch verification executed a configured Git filter")
        self.assertEqual(snapshot(self.work), before)
        self.assertIsNotNone(error, "Git conversion filters must fail closed before any apply")
        self.assertIn("filter", str(error))
        self.assertNotIn("filter-name-secret", str(error))
        self.assertNotIn("FILTER_VALUE_SECRET", str(error))
        self.assertNotIn(str(self.work), str(error))

    def test_inspector_refuses_clean_filter_without_executing_it(self):
        self.install()
        marker = self.add_filter(self.target)
        self.assert_filter_refused_without_writes(
            lambda: self.m.inspect_installation(self.target, self.commit, self.patchfile), marker)

    def test_fresh_patch_permission_does_not_allow_git_filter_execution(self):
        marker = self.add_filter(self.source)
        self.assert_filter_refused_without_writes(
            lambda: self.m.ensure_patch(self.source, self.patchfile, allow_apply=True), marker)

    def test_included_process_filter_is_refused_before_patch_verification(self):
        self.install()
        marker = self.add_filter(self.target, kind="process", included=True)
        self.assert_filter_refused_without_writes(
            lambda: self.m.inspect_installation(self.target, self.commit, self.patchfile), marker)

    def test_smudge_filter_is_conservatively_refused_before_patch_verification(self):
        self.install()
        marker = self.add_filter(self.target, kind="smudge")
        self.assert_filter_refused_without_writes(
            lambda: self.m.ensure_patch(self.target, self.patchfile), marker)

    def doctor_fixture(self):
        d = load_doctor()
        f = Fixture()
        self.addCleanup(f.close)
        (f.bin / "git").unlink()
        (f.bin / "git").symlink_to(shutil.which("git"))
        f.vars["KLIPPER_MCP_PATH"] = str(self.target)
        f.write_env()
        shutil.copyfile(self.patchfile, f.source / "patches/klippermcp-kobra-upload.patch")
        d.EXPECTED_COMMIT = self.commit
        return d, f

    def run_doctor(self, d, f):
        report = d.Report()
        with mock.patch.dict(os.environ, f.env, clear=True), mock.patch.object(
                d.platform, "machine", return_value="x86_64"):
            d.local_checks(report, f.profile, f.home, f.source, 2, os_release=f.os_release)
        return report

    def test_doctor_refuses_clean_filter_without_marker_or_diagnostic_secrets(self):
        self.install()
        marker = self.add_filter(self.target)
        d, f = self.doctor_fixture()
        before, doctor_before = snapshot(self.work), snapshot(f.root)
        report = self.run_doctor(d, f)
        self.assertFalse(marker.exists(), "doctor reverse patch check executed a Git clean filter")
        self.assertEqual(snapshot(self.work), before)
        self.assertEqual(snapshot(f.root), doctor_before)
        self.assertEqual(next(c[0] for c in report.checks if c[1] == "mcp-patch"), "FAIL")
        self.assertEqual(report.exit_code, 2)
        for secret in ("filter-name-secret", "FILTER_VALUE_SECRET", str(self.work)):
            self.assertNotIn(secret, report.render())
        for label in ("mcp-commit", "mcp-build", "mcp-registration"):
            self.assertEqual(next(c[0] for c in report.checks if c[1] == label), "PASS")

    def test_filter_attribute_without_configured_driver_remains_read_only_and_valid(self):
        self.install()
        (self.target / ".git/info/attributes").write_text("src/index.ts filter=undefined\n")
        d, f = self.doctor_fixture()
        before, doctor_before = snapshot(self.work), snapshot(f.root)
        self.assertEqual(self.m.inspect_installation(self.target, self.commit, self.patchfile),
                         {"status": "reused", "integrity": "VERIFIED"})
        report = self.run_doctor(d, f)
        self.assertEqual(report.exit_code, 0, report.render())
        self.assertEqual(snapshot(self.work), before)
        self.assertEqual(snapshot(f.root), doctor_before)

    def test_global_and_system_filter_sources_are_ignored_without_writes(self):
        self.install()
        marker = self.add_filter(self.target)
        command = git(self.target, "config", "--get", "filter.filter-name-secret.clean")
        git(self.target, "config", "--unset", "filter.filter-name-secret.clean")
        global_config, system_config = self.home / ".gitconfig", self.work / "system-gitconfig"
        for config in (global_config, system_config):
            git(self.work, "config", "--file", str(config), "filter.filter-name-secret.clean", command)
        d, f = self.doctor_fixture()
        shutil.copyfile(global_config, f.home / ".gitconfig")
        f.env.update(GIT_CONFIG_GLOBAL=str(global_config), GIT_CONFIG_SYSTEM=str(system_config),
                     GIT_CONFIG_NOSYSTEM="0")
        before, doctor_before = snapshot(self.work), snapshot(f.root)
        with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config),
                             GIT_CONFIG_SYSTEM=str(system_config), GIT_CONFIG_NOSYSTEM="0"):
            result = self.m.inspect_installation(self.target, self.commit, self.patchfile)
        self.assertEqual(result, {"status": "reused", "integrity": "VERIFIED"})
        report = self.run_doctor(d, f)
        self.assertEqual(report.exit_code, 0, report.render())
        self.assertFalse(marker.exists())
        self.assertEqual(snapshot(self.work), before)
        self.assertEqual(snapshot(f.root), doctor_before)

    def test_invalid_config_preflight_refuses_apply_without_writes(self):
        self.install()
        included = self.work / "invalid-gitconfig"
        included.write_text("[invalid\nCONFIG_VALUE_SECRET\n")
        git(self.target, "config", "include.path", str(included))
        before = snapshot(self.work)
        with self.assertRaisesRegex(self.m.SetupError, "filter") as error:
            self.m.ensure_patch(self.target, self.patchfile, allow_apply=True)
        self.assertNotIn("CONFIG_VALUE_SECRET", str(error.exception))
        self.assertNotIn(str(included), str(error.exception))
        self.assertEqual(snapshot(self.work), before)
        d, f = self.doctor_fixture()
        before, doctor_before = snapshot(self.work), snapshot(f.root)
        report = self.run_doctor(d, f)
        self.assertEqual(next(c[0] for c in report.checks if c[1] == "mcp-patch"), "FAIL")
        self.assertNotIn("CONFIG_VALUE_SECRET", report.render())
        self.assertNotIn(str(included), report.render())
        self.assertEqual(snapshot(self.work), before)
        self.assertEqual(snapshot(f.root), doctor_before)

    def test_wrong_target_head_cannot_be_verified_through_inherited_git_dir(self):
        self.install()
        git(self.target, "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
            "commit", "--allow-empty", "-qm", "different target HEAD")
        before = snapshot(self.work)
        error = None
        with mock.patch.dict(os.environ, GIT_DIR=str(self.source / ".git")):
            try:
                self.m.inspect_installation(self.target, self.commit, self.patchfile)
            except self.m.SetupError as caught:
                error = caught
        self.assertEqual(snapshot(self.work), before)
        self.assertIsNotNone(error, "wrong target HEAD was VERIFIED using the inherited GIT_DIR")
        self.assertIn("wrong commit", str(error))

    def test_existing_target_ignores_routing_config_and_trace_environment(self):
        self.install()
        trace = self.work / "unexpected-trace"
        cases = [
            {"GIT_DIR": str(self.source / ".git")},
            {"GIT_WORK_TREE": str(self.source)},
            {"GIT_INDEX_FILE": str(self.work / "unexpected-index")},
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true"},
            {"GIT_TRACE": str(trace), "GIT_TRACE2_EVENT": str(self.work / "unexpected-trace2")},
        ]
        for environment in cases:
            with self.subTest(environment=list(environment)):
                before = snapshot(self.work)
                with mock.patch.dict(os.environ, environment):
                    result = self.m.inspect_installation(self.target, self.commit, self.patchfile)
                self.assertEqual(snapshot(self.work), before, "Git environment caused a write")
                self.assertEqual(result, {"status": "reused", "integrity": "VERIFIED"})

    def test_fresh_clone_checkout_and_patch_are_not_redirected_by_git_environment(self):
        before_source = snapshot(self.source)
        environment = {"GIT_DIR": str(self.source / ".git"), "GIT_WORK_TREE": str(self.source),
                       "GIT_INDEX_FILE": str(self.source / ".git/index"),
                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare",
                       "GIT_CONFIG_VALUE_0": "true", "GIT_TRACE": str(self.work / "unexpected-trace")}
        with mock.patch.dict(os.environ, environment):
            result = self.m.setup(self.target, str(self.source), self.commit, self.patchfile)
        self.assertEqual(result, {"status": "installed", "integrity": "VERIFIED"})
        self.assertEqual(snapshot(self.source), before_source)
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.commit)
        self.assertIn("true", (self.target / "src/index.ts").read_text())
        self.assertFalse((self.work / "unexpected-trace").exists())


if __name__ == "__main__":
    unittest.main()
