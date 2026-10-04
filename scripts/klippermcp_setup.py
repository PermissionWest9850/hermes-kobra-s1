#!/usr/bin/env python3
"""Pinned KlipperMCP setup: fresh builds only; existing installs are read only.

No printer endpoints are contacted. Hashes are local change-detection baselines,
not signatures or a guarantee that npm dependencies are free of vulnerabilities.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys

PINNED_COMMIT = "425e16905c16b6c078028b5063fcb21e0591b190"
RECORD_NAME = ".hermes-klippermcp-integrity.json"


class SetupError(ValueError):
    """Safe, fixed diagnostic; never exposes command output or file contents."""


def _run(args, cwd=None, check=True):
    # Repository selection, config injection and tracing must not be inherited.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True)
    except OSError:
        raise SetupError("Required command could not be executed") from None
    if check and result.returncode:
        raise SetupError("Setup command failed; no automatic repair attempted")
    return result


def _git(path, *args, check=True):
    return _run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                 *args], cwd=path, check=check)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _tree(path, allow_links=False):
    if path.is_symlink() or not path.is_dir():
        raise SetupError("Missing tree or unsafe root symlink")
    entries = []
    for entry in sorted(path.rglob("*")):
        relative = entry.relative_to(path).as_posix()
        if entry.is_symlink():
            try:
                target = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                raise SetupError("Broken or cyclic symlink in installation") from None
            if not allow_links or not target.is_relative_to(path.resolve()) or not target.is_file():
                raise SetupError("Unsafe symlink in installation")
            entries.append([relative, "link", os.readlink(entry)])
        elif entry.is_file():
            entries.append([relative, "file", entry.stat().st_mode & 0o777, _sha(entry.read_bytes())])
        elif entry.is_dir():
            entries.append([relative, "directory"])
        else:
            raise SetupError("Unsupported filesystem entry")
    return _sha(json.dumps(entries, separators=(",", ":")).encode())


def _source(path):
    entries = {"src": _tree(path / "src")}
    tracked = _git(path, "ls-tree", "-r", "--name-only", "-z", "HEAD").stdout
    for name in (n.decode() for n in tracked.split(b"\0") if n):
        if (path / name).is_symlink():
            raise SetupError("Unsafe source symlink")
        entries[name] = _sha((path / name).read_bytes())
    return _sha(json.dumps(entries, sort_keys=True).encode())


def _fingerprint(path, commit, patch):
    return {"schema": 1, "commit": commit, "patch_sha256": _sha(patch.read_bytes()),
            "lock_sha256": _sha((path / "package-lock.json").read_bytes()),
            "source_sha256": _source(path),
            "dependencies_sha256": _tree(path / "node_modules", allow_links=True),
            "build_sha256": _tree(path / "dist")}


def _json(path, diagnostic):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        raise SetupError(diagnostic) from None


def _validate_lock(path, commit):
    lockfile = path / "package-lock.json"
    if lockfile.read_bytes() != _git(path, "show", f"{commit}:package-lock.json").stdout:
        raise SetupError("Lockfile differs from pinned upstream; preserved unchanged")
    lock = _json(lockfile, "Invalid upstream lockfile")
    if not isinstance(lock, dict) or lock.get("lockfileVersion") not in (2, 3):
        raise SetupError("Unsupported lockfile format")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not packages.get(""):
        raise SetupError("Invalid lockfile package metadata")
    manifest = _json(path / "package.json", "Invalid source package metadata")
    for field in ("name", "version", "dependencies", "devDependencies", "optionalDependencies"):
        if manifest.get(field) != packages[""].get(field):
            raise SetupError("Package manifest does not match lockfile")
    return packages


def _validate_layout(path, commit):
    packages = _validate_lock(path, commit)
    if not (path / "node_modules").is_dir():
        raise SetupError("Missing dependencies; existing install will not be repaired")
    _tree(path / "node_modules", allow_links=True)
    _tree(path / "dist")
    _source(path)
    for location, meta in packages.items():
        if not location:
            continue
        if not location.startswith("node_modules/") or ".." in Path(location).parts:
            raise SetupError("Invalid dependency location in lockfile")
        package = path / location / "package.json"
        if meta.get("optional") and not package.exists():
            continue  # npm omits platform-specific optional packages.
        actual = _json(package, "Missing or invalid dependency metadata")
        if not meta.get("version") or actual.get("version") != meta["version"]:
            raise SetupError("Installed dependency version differs from lockfile")
    if not (path / "dist/index.js").is_file():
        raise SetupError("Missing compiled build; existing install will not be rebuilt")
    for source in (path / "src").rglob("*.ts"):
        if source.name.endswith(".d.ts"):
            continue
        output = path / "dist" / source.relative_to(path / "src").with_suffix(".js")
        if not output.is_file():
            raise SetupError("Incomplete compiled build; existing install will not be rebuilt")


def _check_git_filters(path):
    # Even apply --check runs clean/process filters. Query effective config
    # (including repository includes), never command values, and fail closed.
    result = _git(path, "config", "--null", "--name-only", "--get-regexp",
                  r"^filter\..*\.(clean|smudge|process)$", check=False)
    if result.returncode != 1 or result.stdout:
        raise SetupError("Git conversion filters unavailable or configured; patch check refused")


def ensure_patch(path, patch, allow_apply=False):
    """Apply only to a caller-owned fresh clone; default is read-only checking."""
    _check_git_filters(path)
    if not _git(path, "apply", "--reverse", "--check", str(patch), check=False).returncode:
        return "already-applied"
    if not allow_apply:
        raise SetupError("Required patch missing or inconsistent; existing installation preserved")
    if _git(path, "apply", "--check", str(patch), check=False).returncode:
        raise SetupError("Required patch cannot be applied to fresh clone")
    _git(path, "apply", str(patch))
    return "applied"


def _inspect_installation(path, commit, patch):
    """Read-only integrity check: no npm, fetch, index refresh, or marker writes."""
    path, patch = Path(path), Path(patch)
    if _git(path, "rev-parse", "HEAD").stdout.decode().strip() != commit:
        raise SetupError("Existing installation has wrong commit; preserved unchanged")
    ensure_patch(path, patch)
    _validate_layout(path, commit)
    if (path / RECORD_NAME).is_symlink():
        raise SetupError("Unsafe integrity record symlink; preserved unchanged")
    if not (path / RECORD_NAME).exists():
        return {"status": "reused", "integrity": "NOT VERIFIED",
                "warning": "WARN: legacy install has no recorded tree integrity; metadata only, NOT VERIFIED"}
    record = _json(path / RECORD_NAME, "Invalid recorded integrity metadata")
    if record != _fingerprint(path, commit, patch):
        raise SetupError("Recorded installation integrity mismatch; preserved unchanged")
    return {"status": "reused", "integrity": "VERIFIED"}


def inspect_installation(path, commit, patch):
    """Read-only public inspector, also suitable for offline doctor checks."""
    try:
        return _inspect_installation(Path(path), commit, Path(patch).absolute())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        if isinstance(error, SetupError):
            raise
        raise SetupError("Unreadable or invalid installation metadata; preserved unchanged") from None


def _setup(path, repo, commit, patch, dry_run=False):
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SetupError("A full lowercase 40-hex commit is required")
    if dry_run:
        return {"status": "dry-run", "integrity": "NOT VERIFIED"}
    path, patch = Path(path).absolute(), Path(patch).absolute()
    if not patch.is_file() or patch.is_symlink():
        raise SetupError("Required patch file is missing or unsafe")
    if path.is_symlink():
        raise SetupError("Target symlink refused; preserved unchanged")
    if path.exists():
        return inspect_installation(path, commit, patch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()  # Atomic reservation; never clone into a concurrently created target.
    _git(None, "clone", "--no-checkout", "--no-hardlinks", "--", str(repo), str(path))
    _git(path, "checkout", "--detach", commit)
    if _git(path, "rev-parse", "HEAD").stdout.decode().strip() != commit:
        raise SetupError("Checkout does not match requested full commit")
    lock = (path / "package-lock.json").read_bytes()
    if lock != _git(path, "show", f"{commit}:package-lock.json").stdout:
        raise SetupError("Lockfile differs from pinned upstream")
    _validate_lock(path, commit)
    ensure_patch(path, patch, allow_apply=True)
    _validate_lock(path, commit)
    source_hash = _source(path)
    _run(["npm", "ci", "--no-audit", "--no-fund", "--logs-max=0", "--include=dev"], cwd=path)
    _run(["npm", "run", "build"], cwd=path)
    if (path / "package-lock.json").read_bytes() != lock:
        raise SetupError("Lockfile changed during build")
    if _source(path) != source_hash:
        raise SetupError("Patched source changed during npm ci or build")
    _validate_layout(path, commit)
    record = _fingerprint(path, commit, patch)
    (path / RECORD_NAME).write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    return {"status": "installed", "integrity": "VERIFIED"}


def setup(path, repo, commit, patch, dry_run=False):
    """Install a previously absent target or inspect an existing target read only."""
    try:
        return _setup(path, repo, commit, patch, dry_run)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        if isinstance(error, SetupError):
            raise
        raise SetupError("Unreadable or invalid installation metadata; no automatic repair") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = setup(args.path, args.repo, args.commit, args.patch, args.dry_run)
    except SetupError as error:
        print(f"KlipperMCP setup failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        print("KlipperMCP setup failed: unreadable or invalid installation metadata; no automatic repair", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
