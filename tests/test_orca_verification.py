"""Checksum fixtures are synthetic data, never release hashes."""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class OrcaVerificationTests(unittest.TestCase):
    def load_helper(self):
        helper = ROOT / "scripts" / "verify_orca.py"
        self.assertTrue(helper.is_file(), "Mandatory Orca verifier missing")
        spec = importlib.util.spec_from_file_location("verify_orca", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_changed_byte_rejected(self):
        module = self.load_helper()
        original = b"synthetic fixture, not an AppImage"
        entry = {"sha256": hashlib.sha256(original).hexdigest(), "sha512": hashlib.sha512(original).hexdigest()}
        with tempfile.TemporaryDirectory() as work:
            p = Path(work) / "artifact"
            p.write_bytes(original)
            module.verify_file(p, entry)
            p.write_bytes(original + b"changed")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                module.verify_file(p, entry)

    def test_sha512_is_required_even_when_sha256_matches(self):
        module = self.load_helper()
        data = b"synthetic fixture, not an AppImage"
        entry = {"sha256": hashlib.sha256(data).hexdigest(), "sha512": hashlib.sha512(b"different synthetic data").hexdigest()}
        with tempfile.TemporaryDirectory() as work:
            p = Path(work) / "artifact"
            p.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "SHA512 mismatch"):
                module.verify_file(p, entry)

    def test_aarch64_blocked_without_additional_anchor(self):
        module = self.load_helper()
        for arch in ["aarch64", "arm64", "riscv64"]:
            with self.assertRaisesRegex(ValueError, "No approved additional trust anchor"):
                module.load_entry(ROOT / "checksums/orcaslicer-2.4.2.json", "2.4.2", arch)

    def test_wrong_version_or_selected_asset_rejected(self):
        module = self.load_helper()
        manifest = ROOT / "checksums/orcaslicer-2.4.2.json"
        with self.assertRaises(ValueError):
            module.load_entry(manifest, "2.4.3", "x86_64")
        with self.assertRaises(ValueError):
            module.load_entry(manifest, "2.4.2", "x86_64", "unexpected.AppImage")
        self.assertEqual(module.load_entry(manifest, "2.4.2", "amd64")["asset"], "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage")

    def test_missing_or_malformed_pin_rejected(self):
        module = self.load_helper()
        original = json.loads((ROOT / "checksums/orcaslicer-2.4.2.json").read_text())
        with tempfile.TemporaryDirectory() as work:
            p = Path(work) / "manifest.json"
            for key in ["sha256", "sha512", "community_anchor"]:
                data = json.loads(json.dumps(original))
                data["architectures"]["x86_64"].pop(key)
                p.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    module.load_entry(p, "2.4.2", "x86_64")
            p.write_text("invalid json")
            with self.assertRaises(ValueError):
                module.load_entry(p, "2.4.2", "x86_64")

    def test_schema_version_requires_integer_not_boolean_or_float(self):
        module = self.load_helper()
        original = json.loads((ROOT / "checksums/orcaslicer-2.4.2.json").read_text())
        with tempfile.TemporaryDirectory() as work:
            p = Path(work) / "manifest.json"
            for value in (True, 1.0):
                original["schema_version"] = value
                p.write_text(json.dumps(original))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    module.load_entry(p, "2.4.2", "x86_64")

    def test_duplicate_manifest_keys_are_rejected_at_every_level(self):
        module = self.load_helper()
        original = (ROOT / "checksums/orcaslicer-2.4.2.json").read_text()
        with tempfile.TemporaryDirectory() as work:
            p = Path(work) / "manifest.json"
            for data in (original.replace('"schema_version": 1', '"schema_version": 0, "schema_version": 1'),
                         original.replace('"sha256":', '"sha256": "ambiguous", "sha256":', 1)):
                p.write_text(data)
                with self.assertRaisesRegex(ValueError, "Duplicate"):
                    module.load_entry(p, "2.4.2", "x86_64")

    def test_symlink_artifact_rejected(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            p = Path(work) / "artifact"
            p.write_bytes(b"synthetic")
            link = Path(work) / "link"
            link.symlink_to(p)
            with self.assertRaisesRegex(ValueError, "regular file"):
                module.verify_file(link, {})

    def test_path_substitution_between_lstat_and_open_is_rejected(self):
        module = self.load_helper()
        data = b"synthetic identical fixture bytes"
        entry = {name: hashlib.new(name, data).hexdigest() for name in ("sha256", "sha512")}
        with tempfile.TemporaryDirectory() as work:
            p, replacement = Path(work) / "artifact", Path(work) / "replacement"
            real_lstat = Path.lstat
            for symlink in (False, True):
                with self.subTest(symlink=symlink):
                    if p.exists() or p.is_symlink():
                        p.unlink()
                    p.write_bytes(data)
                    replacement.write_bytes(data)
                    swapped = False
                    def swap_after_lstat(path, *args, **kwargs):
                        nonlocal swapped
                        state = real_lstat(path, *args, **kwargs)
                        if path == p and not swapped:
                            swapped = True
                            if symlink:
                                p.unlink()
                                p.symlink_to(replacement)
                            else:
                                os.replace(replacement, p)
                        return state
                    with patch.object(Path, "lstat", swap_after_lstat), self.assertRaises((ValueError, OSError)):
                        module.verify_file(p, entry)

    def test_path_replaced_during_hashing_is_rejected(self):
        module = self.load_helper()
        data = b"synthetic same bytes, different inode"
        entry = {name: hashlib.new(name, data).hexdigest() for name in ("sha256", "sha512")}
        with tempfile.TemporaryDirectory() as work:
            p, replacement = Path(work) / "artifact", Path(work) / "replacement"
            p.write_bytes(data)
            replacement.write_bytes(data)
            real_digest = hashlib.sha256()
            class SwappingDigest:
                def update(self, chunk):
                    real_digest.update(chunk)
                    if replacement.exists():
                        os.replace(replacement, p)
                def hexdigest(self):
                    return real_digest.hexdigest()
            with patch.object(module.hashlib, "sha256", return_value=SwappingDigest()), self.assertRaisesRegex(ValueError, "changed"):
                # Unlinking the old inode may also change its ctime: either
                # the descriptor metadata or pathname identity must reject it.
                module.verify_file(p, entry)

    def test_extraction_holds_descriptor_when_path_swaps_before_execution(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            p, bad, destination = Path(work) / "artifact", Path(work) / "bad", Path(work) / "published"
            # Explicitly synthetic scripts: neither is a real Orca binary.
            data = b'#!/bin/sh\nprintf verified > marker\nmkdir -p squashfs-root/resources/profiles/Anycubic\n'
            p.write_bytes(data)
            bad.write_text('#!/bin/sh\nprintf unverified > marker\nmkdir -p squashfs-root/resources/profiles/Anycubic\n')
            bad.chmod(0o755)
            entry = {name: hashlib.new(name, data).hexdigest() for name in ("sha256", "sha512")}
            real_run = module.subprocess.run
            def swap_then_execute(args, **kwargs):
                self.assertTrue(args[0].startswith("/proc/self/fd/"))
                self.assertIn(int(args[0].rsplit("/", 1)[1]), kwargs["pass_fds"])
                os.replace(bad, p)
                return real_run(args, **kwargs)
            with patch.object(module.subprocess, "run", side_effect=swap_then_execute):
                module.extract_verified(p, entry, Path(work), destination)
            self.assertEqual((Path(work) / "marker").read_text(), "verified")
            self.assertEqual(destination.read_bytes(), data)
            self.assertNotEqual(p.read_bytes(), data)

    def test_publication_never_overwrites_existing_destination(self):
        module = self.load_helper()
        with tempfile.TemporaryDirectory() as work:
            p, destination = Path(work) / "artifact", Path(work) / "published"
            data = b'#!/bin/sh\nmkdir -p squashfs-root/resources/profiles/Anycubic\n'
            p.write_bytes(data)
            destination.write_text("concurrent-user-file")
            entry = {name: hashlib.new(name, data).hexdigest() for name in ("sha256", "sha512")}
            with self.assertRaises(FileExistsError):
                module.extract_verified(p, entry, Path(work), destination)
            self.assertEqual(destination.read_text(), "concurrent-user-file")

    def test_production_manifest_verifies_existing_official_appimage(self):
        module = self.load_helper()
        app = Path("/opt/orcaslicer/OrcaSlicer.AppImage")
        if not app.is_file():
            self.skipTest("No local AppImage; no download or installation permitted by this test")
        entry = module.load_entry(ROOT / "checksums/orcaslicer-2.4.2.json", "2.4.2", "x86_64")
        module.verify_file(app, entry)


if __name__ == "__main__":
    unittest.main()
