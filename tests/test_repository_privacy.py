"""Repository hygiene guards; no hooks or Git index writes."""
import hashlib
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0")


class RepositoryPrivacyTests(unittest.TestCase):
    def test_local_sensitive_and_backup_paths_ignored_by_normal_git_operations(self):
        paths = ["backups/probe.tar.gz", "nested/backups/private.env", ".hermes-backups/probe", "cache/probe", ".env", ".env.private", "auth.json"]
        r = subprocess.run(["git", "check-ignore", "--no-index", "--", *paths], cwd=ROOT, env=ENV, text=True, capture_output=True, check=True)
        self.assertEqual(set(r.stdout.splitlines()), set(paths))
        example = subprocess.run(["git", "check-ignore", "--no-index", "--", ".env.EXAMPLE"], cwd=ROOT, env=ENV, capture_output=True)
        self.assertEqual(example.returncode, 1)

    def test_no_tracked_backups_or_private_environment_files(self):
        r = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, env=ENV, capture_output=True, check=True)
        for raw in r.stdout.split(b"\0"):
            if not raw:
                continue
            path = Path(os.fsdecode(raw))
            self.assertFalse(set(path.parts) & {"backups", ".hermes-backups", "cache"}, str(path))
            self.assertNotEqual(path.name, "auth.json")
            if path.name == ".env" or path.name.startswith(".env."):
                self.assertIn(path.name, {".env.EXAMPLE", ".env.example", ".env.template"})

    def test_accepted_verifiers_manifest_and_upload_patch_remain_byte_identical(self):
        accepted = {
            "scripts/profile_config.py": "cb61050bc3a8d6330f2fbc7ea68c67c782c975295bdf98311a1ba5007058c38d",
            "scripts/verify_orca.py": "58bd3476d1f011c54cb826357b7fd2906ce532cdd298caf730cd2006f91644c3",
            "checksums/orcaslicer-2.4.2.json": "ec5df4b0e9f76e0c13b8f16a31e303f2c4a9936909cef5688db17bae3b954f11",
            "patches/klippermcp-kobra-upload.patch": "f5c9a7cf3f500498081d70b30d9d3ff542ecd55e3b631351a1e5896c3966d74e",
        }
        for rel, expected in accepted.items():
            with self.subTest(file=rel):
                self.assertEqual(hashlib.sha256((ROOT / rel).read_bytes()).hexdigest(), expected)


if __name__ == "__main__":
    unittest.main()
