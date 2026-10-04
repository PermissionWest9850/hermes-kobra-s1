#!/usr/bin/env python3
"""Fail-closed pin verification; optional descriptor-bound fresh extraction."""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate checksum manifest key; verification refused")
        result[key] = value
    return result


def load_entry(manifest, version, architecture, asset=None):
    data = json.loads(Path(manifest).read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1 or data.get("version") != version:
        raise ValueError("Unsupported checksum manifest schema/version")
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(architecture, architecture)
    entry = data.get("architectures", {}).get(architecture)
    if not isinstance(entry, dict) or entry.get("automatic_verification_approved") is not True:
        raise ValueError("No approved additional trust anchor for this architecture; automatic Orca installation/reuse blocked")
    expected_asset = "OrcaSlicer_Linux_AppImage_Ubuntu2404_V%s.AppImage" % version
    if architecture != "x86_64" or entry.get("asset") != expected_asset:
        raise ValueError("Checksum asset/architecture mismatch")
    if asset is not None and asset != entry["asset"]:
        raise ValueError("Selected release asset differs from reviewed checksum pin")
    for algorithm, length in [("sha256", 64), ("sha512", 128)]:
        if not isinstance(entry.get(algorithm), str) or not re.fullmatch("[0-9a-f]{%d}" % length, entry[algorithm]):
            raise ValueError("Required pinned checksum missing or malformed")
    anchor = entry.get("community_anchor", {})
    if anchor.get("kind") != "AUR Community PKGBUILD" or not re.fullmatch("[0-9a-f]{40}", anchor.get("commit", "")):
        raise ValueError("Reviewed community anchor missing")
    return entry


@contextmanager
def open_artifact(path):
    path = Path(path)
    named = path.lstat()
    if not stat.S_ISREG(named.st_mode):
        raise ValueError("Orca artifact must be a regular file, not a symlink or special file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Orca artifact changed before opening or is not a regular file")
        yield stream


def verify_stream(stream, entry, path=None):
    hashes = {"sha256": hashlib.sha256(), "sha512": hashlib.sha512()}
    initial = os.fstat(stream.fileno())
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        for digest in hashes.values():
            digest.update(chunk)
    final = os.fstat(stream.fileno())
    if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
        raise ValueError("Orca artifact changed during verification")
    if path is not None:
        named = Path(path).lstat()
        if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (final.st_dev, final.st_ino):
            raise ValueError("Orca artifact pathname changed during verification")
    for algorithm, digest in hashes.items():
        if digest.hexdigest() != entry[algorithm]:
            raise ValueError("Orca %s mismatch; installation/execution refused" % algorithm.upper())


def verify_file(path, entry):
    """Read-only check: never chmod or execute an existing artifact."""
    with open_artifact(path) as stream:
        verify_stream(stream, entry, path)


def extract_verified(path, entry, directory, destination):
    """Hold the verified inode through execution and exclusive publication.

    The caller supplies a private extraction directory. This is not protection
    against an actively malicious same-UID writer modifying the held inode.
    """
    directory, destination = Path(directory), Path(destination)
    with open_artifact(path) as stream:
        verify_stream(stream, entry, path)
        fd = stream.fileno()
        os.fchmod(fd, 0o755)
        subprocess.run(["/proc/self/fd/%d" % fd, "--appimage-extract"],
                       pass_fds=(fd,), cwd=directory, stdout=subprocess.DEVNULL, check=True)
        if not (directory / "squashfs-root/resources/profiles/Anycubic").is_dir():
            raise ValueError("Orca extracted profile root missing; publication refused")
        # Do not reopen the download pathname for publication. Check the held
        # inode again after extraction and verify the actual copied bytes too.
        verify_stream(stream, entry)
        destination.parent.mkdir(parents=True, exist_ok=True)
        output_fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(output_fd, "w+b") as output:
            stream.seek(0)
            shutil.copyfileobj(stream, output)
            output.flush()
            verify_stream(output, entry, destination)
            os.fchmod(output.fileno(), 0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--asset")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--extract", type=Path, help="Private directory for verified fresh extraction")
    parser.add_argument("--publish", type=Path, help="Exclusive AppImage destination (requires --extract)")
    args = parser.parse_args()
    if (args.extract is None) != (args.publish is None) or (args.extract is not None and args.file is None):
        parser.error("--extract and --publish require each other and --file")
    try:
        entry = load_entry(args.manifest, args.version, args.arch, args.asset)
        if args.file is None:
            print(entry["asset"])
        else:
            if args.extract is not None:
                extract_verified(args.file, entry, args.extract, args.publish)
            else:
                verify_file(args.file, entry)
            print("OrcaSlicer verified: pinned SHA256 and AUR Community SHA512 match (not an upstream signature).")
    except ValueError as error:
        # Only our validation errors, never configuration/file content.
        if isinstance(error, json.JSONDecodeError):
            parser.exit(1, "ERROR: Invalid checksum manifest; verification refused.\n")
        parser.exit(1, "ERROR: " + str(error) + "\n")
    except (OSError, KeyError, TypeError, AttributeError, subprocess.CalledProcessError):
        parser.exit(1, "ERROR: Checksum manifest/artifact unavailable or invalid; verification refused.\n")


if __name__ == "__main__":
    main()
