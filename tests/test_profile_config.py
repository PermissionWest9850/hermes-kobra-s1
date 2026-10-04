"""Local fixtures only: never touch an installed Hermes profile."""
import importlib.util
import tempfile
import os
import select
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProfileConfigTests(unittest.TestCase):
    def load_helper(self):
        helper = ROOT / "scripts" / "profile_config.py"
        spec = importlib.util.spec_from_file_location("profile_config", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self, module, work):
        source, target = Path(work) / "source", Path(work) / "target"
        source.mkdir()
        for name in module.MANAGED:
            p = source / name
            if name in module.TREES:
                p.mkdir()
                (p / "file.txt").write_text("upstream")
            else:
                p.write_text("upstream")
        module.sync_profile(source, target)
        (target / "config.yaml").write_text("user customization")
        (target / ".env").write_text("PRIVATE_TEST_VALUE=do-not-display\n")
        return source, target

    def test_confirmed_update_backs_up_before_replacing(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            env = (target / ".env").read_bytes()
            module.sync_profile(source, target, update=True, confirm=lambda: True)
            self.assertEqual((target / "config.yaml").read_text(), "upstream")
            self.assertEqual((target / ".env").read_bytes(), env)
            backups = list((target / "backups").glob("config-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "config.yaml").read_text(), "user customization")
            self.assertFalse((backups[0] / ".env").exists())
            self.assertTrue((backups[0] / "backup-manifest.json").is_file())

    def test_declined_update_has_no_writes_or_secret_output(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            before = module.snapshot(target)
            output = StringIO()
            with redirect_stdout(output), self.assertRaises(ValueError):
                module.sync_profile(source, target, update=True, confirm=lambda: False)
            self.assertEqual(module.snapshot(target), before)
            self.assertFalse((target / "backups").exists())
            self.assertNotIn("do-not-display", output.getvalue())
            self.assertNotIn("user customization", output.getvalue())

    def test_dry_run_update_has_no_backup(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            before = module.snapshot(target)
            module.sync_profile(source, target, update=True, dry_run=True, confirm=lambda: self.fail("must not confirm"))
            self.assertEqual(module.snapshot(target), before)
            self.assertFalse((target / "backups").exists())

    def test_partial_payload_refused_without_overwrite(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            (target / "SOUL.md").unlink()
            before = module.snapshot(target)
            for update in [False, True]:
                with self.assertRaises(ValueError):
                    module.sync_profile(source, target, update=update, confirm=lambda: True)
                self.assertEqual(module.snapshot(target), before)

    def test_symlink_payload_refused(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            external = Path(work) / "external"
            external.write_text("must remain")
            (target / "config.yaml").unlink()
            (target / "config.yaml").symlink_to(external)
            with self.assertRaises(ValueError):
                module.sync_profile(source, target, update=True, confirm=lambda: True)
            self.assertEqual(external.read_text(), "must remain")

    def test_concurrent_edit_during_confirmation_aborts(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            def approve():
                (target / "config.yaml").write_text("concurrent customization")
                return True
            with self.assertRaises(ValueError):
                module.sync_profile(source, target, update=True, confirm=approve)
            self.assertEqual((target / "config.yaml").read_text(), "concurrent customization")
            self.assertFalse((target / "backups").exists())

    def test_source_changed_during_confirmation_aborts_before_backup(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            before = module.snapshot(target)
            def approve():
                (source / "scripts/file.txt").unlink()
                (source / "scripts/unreviewed.txt").write_text("not previewed")
                return True
            with self.assertRaisesRegex(ValueError, "Source changed"):
                module.sync_profile(source, target, update=True, confirm=approve)
            self.assertEqual(module.snapshot(target), before)
            self.assertFalse((target / "backups").exists())

    def test_edit_between_original_moves_is_restored_not_lost(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            real_replace = module.os.replace
            def edit_after_first_move(src, dst):
                result = real_replace(src, dst)
                if Path(src) == target / "SOUL.md":
                    (target / "config.yaml").write_text("concurrent-user-edit")
                return result
            with patch.object(module.os, "replace", side_effect=edit_after_first_move), self.assertRaises(ValueError):
                module.sync_profile(source, target, update=True, confirm=lambda: True)
            self.assertEqual((target / "config.yaml").read_text(), "concurrent-user-edit")
            self.assertEqual((target / "SOUL.md").read_text(), "upstream")

    def test_target_recreated_before_publication_survives_rollback(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            real_replace = module.os.replace
            def recreate_after_last_move(src, dst):
                result = real_replace(src, dst)
                if Path(src) == target / "checksums":
                    (target / "config.yaml").write_text("raced-new-revision")
                return result
            with patch.object(module.os, "replace", side_effect=recreate_after_last_move), self.assertRaises(OSError):
                module.sync_profile(source, target, update=True, confirm=lambda: True)
            self.assertEqual((target / "config.yaml").read_text(), "raced-new-revision")
            saved = list(target.glob(".config-stage-*/originals/config.yaml"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_text(), "user customization")

    def test_success_retains_original_inodes_for_late_descriptor_edits(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            with (target / "config.yaml").open("r+") as editor:
                old_inode = os.fstat(editor.fileno()).st_ino
                module.sync_profile(source, target, update=True, confirm=lambda: True)
                editor.seek(0)
                editor.write("late-descriptor-user-edit")
                editor.truncate()
                editor.flush()
                saved = list(target.glob("backups/config-*/moved-originals/config.yaml"))
                self.assertEqual(len(saved), 1, "Actual original inode discarded on success")
                self.assertEqual(saved[0].stat().st_ino, old_inode)
                self.assertEqual(saved[0].read_text(), "late-descriptor-user-edit")
                self.assertEqual((target / "config.yaml").read_text(), "upstream")

    def test_rollback_retains_edits_to_published_candidate(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            real_rename = module.rename_noreplace
            def edit_then_fail(src, dst):
                if Path(src).parent.name == "staged" and Path(src).name == "config.yaml":
                    (target / "SOUL.md").write_text("edited-published-candidate")
                    raise OSError("synthetic publication failure")
                return real_rename(src, dst)
            with patch.object(module, "rename_noreplace", side_effect=edit_then_fail), self.assertRaises(OSError):
                module.sync_profile(source, target, update=True, confirm=lambda: True)
            self.assertEqual((target / "SOUL.md").read_text(), "upstream")
            withdrawn = list(target.glob(".config-stage-*/withdrawn/SOUL.md"))
            self.assertEqual(len(withdrawn), 1)
            self.assertEqual(withdrawn[0].read_text(), "edited-published-candidate")

    def test_atomic_no_overwrite_rejects_empty_directory_and_symlink(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, destination = root / "source", root / "destination"
            source.mkdir()
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                module.rename_noreplace(source, destination)
            self.assertTrue(source.is_dir())
            self.assertTrue(destination.is_dir())
            destination.rmdir()
            destination.symlink_to(root / "absent")
            with self.assertRaises(FileExistsError):
                module.rename_noreplace(source, destination)
            self.assertTrue(source.is_dir())
            self.assertTrue(destination.is_symlink())

    def test_replacement_failure_rolls_back(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            before = module.snapshot(target)
            real_replace = module.rename_noreplace
            def failing_replace(src, dst):
                if Path(src).parent.name == "staged" and Path(src).name == "config.yaml":
                    raise OSError("simulated disk failure")
                return real_replace(src, dst)
            with patch.object(module, "rename_noreplace", side_effect=failing_replace), self.assertRaises(OSError):
                module.sync_profile(source, target, update=True, confirm=lambda: True)
            self.assertEqual(module.snapshot(target), before)
            self.assertEqual(len(list((target / "backups").glob("config-*"))), 1)

    def test_rollback_failure_preserves_recovery_staging(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            real_replace = module.rename_noreplace
            def failing_replace(src, dst):
                if Path(src).parent.name == "originals" or (Path(src).parent.name == "staged" and Path(src).name == "config.yaml"):
                    raise OSError("simulated persistent disk failure")
                return real_replace(src, dst)
            with patch.object(module, "rename_noreplace", side_effect=failing_replace), self.assertRaises(OSError):
                module.sync_profile(source, target, update=True, confirm=lambda: True)
            self.assertTrue(list(target.glob(".config-stage-*/originals/config.yaml")), "Recovery originals were discarded")

    def test_legacy_payload_adds_checksum_directory_only_after_confirmation(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            import shutil
            if (target / "checksums").exists():
                shutil.rmtree(target / "checksums")
            (source / "checksums").mkdir(exist_ok=True)
            (source / "checksums" / "pin.json").write_text("reviewed manifest")
            module.sync_profile(source, target)
            self.assertFalse((target / "checksums").exists())
            module.sync_profile(source, target, update=True, confirm=lambda: True)
            self.assertTrue((target / "checksums" / "pin.json").is_file())

    def test_real_tty_confirmation_can_authorize_fixture_update(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            source, target = self.fixture(module, work)
            pid, fd = os.forkpty()
            if pid == 0:
                os.execv("/usr/bin/python3", ["python3", "-B", str(ROOT / "scripts/profile_config.py"), "--source", str(source), "--target", str(target), "--update"])
            output = b""
            sent = False
            deadline = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    if select.select([fd], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(fd, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        output += chunk
                        if b"Type UPDATE CONFIG" in output and not sent:
                            os.write(fd, b"UPDATE CONFIG\n")
                            sent = True
                done, status = os.waitpid(pid, os.WNOHANG)
                if not done:
                    os.kill(pid, 9)
                    _, status = os.waitpid(pid, 0)
                self.assertTrue(sent, output.decode(errors="replace"))
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors="replace"))
                self.assertEqual((target / "config.yaml").read_text(), "upstream")
            finally:
                os.close(fd)

    def test_normal_rerun_keeps_user_changes(self):
        helper = ROOT / "scripts" / "profile_config.py"
        self.assertTrue(helper.is_file(), "Missing guarded profile installation")
        spec = importlib.util.spec_from_file_location("profile_config", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as work:
            source, target = Path(work) / "source", Path(work) / "target"
            source.mkdir()
            for name in module.MANAGED:
                p = source / name
                if name in module.TREES:
                    p.mkdir()
                    (p / "file.txt").write_text("upstream")
                else:
                    p.write_text("upstream")
            module.sync_profile(source, target)
            (target / "config.yaml").write_text("user customization")
            (target / "scripts" / "custom.py").write_text("user script")
            (target / ".env").write_text("PRIVATE_TEST_VALUE=do-not-display\n")
            before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
            module.sync_profile(source, target)
            after = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((target / "backups").exists())


if __name__ == "__main__":
    unittest.main()
