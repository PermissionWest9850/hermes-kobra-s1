"""Exercise installer functions against fixtures, never the real installer main."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def functions_only():
    return (ROOT / "install.sh").read_text().rsplit('\nmain "$@"', 1)[0]


class InstallerProfileTests(unittest.TestCase):
    def test_update_mode_is_separate_from_installation(self):
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / "profile"
            env = dict(os.environ, PROFILE_DIR=str(target))
            r = subprocess.run(["bash", str(ROOT / "install.sh"), "--update-config", "--dry-run", "--yes"], env=env, text=True, capture_output=True)
            self.assertNotIn("Unknown argument", r.stderr)
            self.assertNotIn("Installing requirements", r.stdout)
            self.assertNotIn("[1/9]", r.stdout)
            self.assertFalse(target.exists())

    def test_corrupt_existing_orca_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as work:
            app = Path(work) / "OrcaSlicer.AppImage"
            app.write_text("corrupt artifact")
            app.chmod(0o755)
            profiles = Path(work) / "profiles"
            profiles.mkdir()
            env = dict(os.environ, TEST_SOURCE=str(ROOT), ORCA_INSTALL_DIR=str(Path(work) / "orca"), ORCA_SLICER_PATH=str(app), ORCA_PROFILE_ROOT=str(profiles))
            r = subprocess.run(["bash", "-c", functions_only() + '\nwork_src="$TEST_SOURCE"; install_orcaslicer\n'], env=env, text=True, capture_output=True)
            self.assertNotEqual(r.returncode, 0, "Installer reused an unverified artifact")
            self.assertEqual(app.read_text(), "corrupt artifact")
            self.assertIn("mismatch", r.stderr)

    def download_fixture(self, work, approve_synthetic=False):
        work = Path(work)
        source, shims, install = work / "source", work / "bin", work / "installed"
        source.mkdir()
        (source / "checksums").mkdir()
        shutil.copytree(ROOT / "scripts", source / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        data = json.loads((ROOT / "checksums/orcaslicer-2.4.2.json").read_text())
        artifact = work / "synthetic-artifact"
        artifact.write_text('#!/bin/sh\nprintf executed > "$TEST_SENTINEL"\nmkdir -p squashfs-root/resources/profiles/Anycubic\n')
        if approve_synthetic:
            data["architectures"]["x86_64"]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            data["architectures"]["x86_64"]["sha512"] = hashlib.sha512(artifact.read_bytes()).hexdigest()
        (source / "checksums/orcaslicer-2.4.2.json").write_text(json.dumps(data))
        asset = data["architectures"]["x86_64"]["asset"]
        metadata = work / "synthetic-release.json"
        metadata.write_text(json.dumps({"assets": [{"name": asset, "browser_download_url": "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/" + asset}]}))
        shims.mkdir()
        curl = shims / "curl"
        curl.write_text('#!/usr/bin/env python3\nimport os,sys,shutil\nargs=sys.argv[1:]\ndest=args[args.index("-o")+1]\nmetadata=any("api.github.com" in a for a in args)\nshutil.copyfile(os.environ["TEST_METADATA" if metadata else "TEST_ARTIFACT"],dest)\n')
        curl.chmod(0o755)
        env = dict(os.environ, PATH=str(shims) + ":" + os.environ["PATH"], TEST_SOURCE=str(source), TEST_METADATA=str(metadata), TEST_ARTIFACT=str(artifact), TEST_SENTINEL=str(work / "executed"), ORCA_INSTALL_DIR=str(install), ORCA_SLICER_PATH=str(install / "OrcaSlicer.AppImage"), ORCA_PROFILE_ROOT=str(install / "squashfs-root/resources/profiles/Anycubic"))
        return env, install, work / "executed"

    def test_download_mismatch_never_executes_artifact(self):
        with tempfile.TemporaryDirectory() as work:
            env, install, sentinel = self.download_fixture(work)
            r = subprocess.run(["bash", "-c", functions_only() + '\nwork_src="$TEST_SOURCE"; install_orcaslicer\n'], env=env, text=True, capture_output=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("mismatch", r.stderr)
            self.assertFalse(sentinel.exists())
            self.assertFalse(install.exists())

    def test_verified_synthetic_download_and_rerun(self):
        with tempfile.TemporaryDirectory() as work:
            env, install, sentinel = self.download_fixture(work, approve_synthetic=True)
            command = ["bash", "-c", functions_only() + '\nwork_src="$TEST_SOURCE"; install_orcaslicer\n']
            r = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(sentinel.exists())
            app = install / "OrcaSlicer.AppImage"
            before = app.stat()
            sentinel.unlink()
            r = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(sentinel.exists(), "Rerun executed existing artifact")
            self.assertEqual((app.stat().st_mtime_ns, app.stat().st_ino), (before.st_mtime_ns, before.st_ino))

    def test_verifier_return_path_swap_cannot_change_execution_or_publication(self):
        with tempfile.TemporaryDirectory() as work:
            env, install, sentinel = self.download_fixture(work, approve_synthetic=True)
            bad = Path(work) / "unverified-synthetic-script"
            bad.write_text('#!/bin/sh\nprintf unverified > "$TEST_SENTINEL"\nmkdir -p squashfs-root/resources/profiles/Anycubic\n')
            shim = Path(work) / "bin/python3"
            shim.write_text('#!/usr/bin/python3\nimport os,sys,subprocess,shutil\nargs=sys.argv[1:]\nr=subprocess.run(["/usr/bin/python3",*args])\nif r.returncode == 0 and any(a.endswith("verify_orca.py") for a in args) and "--file" in args:\n p=args[args.index("--file")+1]\n if not p.startswith(os.environ["ORCA_INSTALL_DIR"]):\n  shutil.copyfile(os.environ["TEST_BAD_ARTIFACT"],p)\nsys.exit(r.returncode)\n')
            shim.chmod(0o755)
            env["TEST_BAD_ARTIFACT"] = str(bad)
            r = subprocess.run(["bash", "-c", functions_only() + '\nwork_src="$TEST_SOURCE"; install_orcaslicer\n'], env=env, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(sentinel.read_text(), "executed", "Executed a substituted pathname after verification")
            self.assertEqual((install / "OrcaSlicer.AppImage").read_bytes(), Path(env["TEST_ARTIFACT"]).read_bytes())

    def test_yes_and_piped_confirmation_cannot_replace_config(self):
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / "profile"
            target.mkdir()
            for name in ["SOUL.md", "config.yaml", "distribution.yaml", ".env.EXAMPLE"]:
                shutil.copy2(ROOT / name, target / name)
            for name in ["scripts", "patches", "checksums"]:
                shutil.copytree(ROOT / name, target / name)
            (target / "config.yaml").write_text("private customization")
            before = (target / "config.yaml").read_bytes()
            env = dict(os.environ, PROFILE_DIR=str(target))
            r = subprocess.run(["bash", str(ROOT / "install.sh"), "--update-config", "--yes"], env=env, input="UPDATE CONFIG\n", text=True, capture_output=True, start_new_session=True, timeout=10)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual((target / "config.yaml").read_bytes(), before)
            self.assertFalse((target / "backups").exists())
            self.assertNotIn("private customization", r.stdout + r.stderr)
            self.assertNotIn("Installing requirements", r.stdout)

    def test_rerun_keeps_user_payload_and_env(self):
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / "profile"
            target.mkdir()
            for name in ["SOUL.md", "config.yaml", "distribution.yaml", ".env.EXAMPLE"]:
                (target / name).write_text("user customized")
            for name in ["scripts", "patches"]:
                (target / name).mkdir()
                (target / name / "custom.txt").write_text("user customized")
            (target / ".env").write_text("PRIVATE_TEST_VALUE=do-not-display\n")
            before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
            env = dict(os.environ, PROFILE_DIR=str(target), TEST_SOURCE=str(ROOT))
            r = subprocess.run(["bash", "-c", functions_only() + '\nwork_src="$TEST_SOURCE"; install_profile_files\n'], env=env, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            after = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
