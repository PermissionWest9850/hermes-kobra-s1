"""Offline local Git/fake npm fixtures; real integration is explicitly opt-in.

Run integration only with KLIPPERMCP_INTEGRATION_SOURCE pointing at a pinned
local checkout and KLIPPERMCP_INTEGRATION_EVIDENCE at an ignored cache directory.
The original checkout is read only. Only the isolated clone runs real npm ci.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
import select
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "klippermcp_setup.py"
PIN = "425e16905c16b6c078028b5063fcb21e0591b190"


def load_helper():
    if not SCRIPT.is_file():
        raise AssertionError("stage3 helper is not implemented")
    spec = importlib.util.spec_from_file_location("klippermcp_setup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(path, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(["git", *args], cwd=path, env=env,
                            capture_output=True, check=True)
    return result.stdout.decode().strip()


def snapshot(path):
    """Detect content/mode/mtime writes including .git, ignoring access times."""
    result = {}
    for entry in sorted(Path(path).rglob("*")):
        st = entry.lstat()
        data = os.readlink(entry) if entry.is_symlink() else (
            hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else None)
        result[entry.relative_to(path).as_posix()] = (st.st_mode, st.st_mtime_ns, data)
    return result


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def fixture(self):
        source = self.work / "upstream"
        source.mkdir()
        (source / "src").mkdir()
        (source / "src/index.ts").write_text('export const patched = false;\n')
        (source / "package.json").write_text(json.dumps({
            "name": "fixture", "version": "1.0.0", "type": "module",
            "scripts": {"build": "tsc"}, "dependencies": {"dep": "1.0.0"}}))
        lock = {"lockfileVersion": 3, "packages": {
            "": {"name": "fixture", "version": "1.0.0", "dependencies": {"dep": "1.0.0"}},
            "node_modules/dep": {"version": "1.0.0"},
            "node_modules/dep/node_modules/transitive": {"version": "2.0.0"}}}
        (source / "package-lock.json").write_text(json.dumps(lock) + "\n")
        (source / "tsconfig.json").write_text('{"compilerOptions":{"outDir":"dist"}}')
        git(source, "init", "-q")
        git(source, "add", ".")
        git(source, "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
            "commit", "-qm", "fixture")
        commit = git(source, "rev-parse", "HEAD")
        patchfile = self.work / "expected.patch"
        (source / "src/index.ts").write_text('export const patched = true;\n')
        patchfile.write_text(git(source, "diff", "--no-ext-diff") + "\n")
        git(source, "restore", "src/index.ts")
        bindir = self.work / "bin"
        bindir.mkdir()
        calls = self.work / "npm-calls.jsonl"
        fake = bindir / "npm"
        fake.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
with open(os.environ["FAKE_NPM_CALLS"], "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")
root = Path.cwd()
mode = os.environ.get("FAKE_NPM_MODE", "")
if mode == "fail":
    print("PRIVATE_TEST_SECRET https://user:password@invalid.example", file=sys.stderr)
    sys.exit(7)
if sys.argv[1] == "ci":
    lock = json.loads((root / "package-lock.json").read_text())
    for name, meta in lock["packages"].items():
        if not name: continue
        p = root / name
        p.mkdir(parents=True, exist_ok=True)
        (p / "package.json").write_text(json.dumps({{"version": meta["version"]}}))
        (p / "index.js").write_text("export const value = 42;\\n")
    (root / "node_modules/.bin").mkdir()
    (root / "node_modules/.bin/dep").symlink_to("../dep/index.js")
else:
    (root / "dist").mkdir()
    for p in (root / "src").rglob("*.ts"):
        out = root / "dist" / p.relative_to(root / "src").with_suffix(".js")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(p.read_text())
if mode == "source-change":
    (root / "src/index.ts").write_text("unexpected source mutation")
if mode == "lock-change":
    with (root / "package-lock.json").open("ab") as f: f.write(b" ")
if mode == "build-missing" and sys.argv[1] != "ci":
    (root / "dist/index.js").unlink()
''')
        fake.chmod(0o755)
        env = patch.dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                         FAKE_NPM_CALLS=str(calls))
        env.start()
        self.addCleanup(env.stop)
        return source, self.work / "target", commit, patchfile, calls

    def test_fresh_clone_preserves_lock_and_builds_with_npm_ci(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        lock = (source / "package-lock.json").read_bytes()
        result = m.setup(target, str(source), commit, patchfile)
        self.assertEqual(result["status"], "installed")
        self.assertEqual(git(target, "rev-parse", "HEAD"), commit)
        self.assertEqual((target / "package-lock.json").read_bytes(), lock)
        self.assertIn("true", (target / "src/index.ts").read_text())
        self.assertIn("true", (target / "dist/index.js").read_text())
        commands = [json.loads(s) for s in calls.read_text().splitlines()]
        self.assertEqual(commands, [["ci", "--no-audit", "--no-fund", "--logs-max=0", "--include=dev"],
                                    ["run", "build"]])
        record = json.loads((target / m.RECORD_NAME).read_text())
        self.assertEqual(record["commit"], commit)
        self.assertEqual(record["lock_sha256"], hashlib.sha256(lock).hexdigest())
        for key in ("source_sha256", "dependencies_sha256", "build_sha256", "patch_sha256"):
            self.assertRegex(record[key], r"^[0-9a-f]{64}$")

    def test_valid_existing_rerun_is_read_only_even_git_index(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        before, commands = snapshot(target), calls.read_bytes()
        result = m.setup(target, "https://unreachable.invalid/repo", commit, patchfile)
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["integrity"], "VERIFIED")
        self.assertEqual(snapshot(target), before)
        self.assertEqual(calls.read_bytes(), commands)

    def assert_preserved_failure(self, m, target, commit, patchfile, calls, message):
        before, commands = snapshot(target), calls.read_bytes()
        with self.assertRaisesRegex(m.SetupError, message):
            m.setup(target, "https://never-used.invalid/secret", commit, patchfile)
        self.assertEqual(snapshot(target), before)
        self.assertEqual(calls.read_bytes(), commands)

    def test_wrong_commit_is_rejected_without_changes(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        git(target, "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
            "commit", "--allow-empty", "-qm", "wrong commit")
        self.assert_preserved_failure(m, target, commit, patchfile, calls, "commit")

    def test_missing_patch_existing_is_never_auto_applied(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        git(target, "restore", "src/index.ts")
        (target / m.RECORD_NAME).unlink()
        self.assert_preserved_failure(m, target, commit, patchfile, calls, "patch")

    def test_legacy_already_applied_patch_reused_with_not_verified_warning(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        (target / m.RECORD_NAME).unlink()
        before, commands = snapshot(target), calls.read_bytes()
        result = m.setup(target, "https://unused.invalid", commit, patchfile)
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["integrity"], "NOT VERIFIED")
        self.assertIn("WARN", result["warning"])
        self.assertEqual(snapshot(target), before)
        self.assertEqual(calls.read_bytes(), commands)
        self.assertFalse((target / m.RECORD_NAME).exists())

    def test_legacy_missing_or_wrong_transitive_dependency_is_rejected(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        (target / m.RECORD_NAME).unlink()
        metadata = target / "node_modules/dep/node_modules/transitive/package.json"
        original = metadata.read_bytes()
        for content in (None, b'{"version":"99.0.0"}'):
            with self.subTest(content=content):
                if content is None:
                    metadata.unlink()
                else:
                    metadata.write_bytes(content)
                self.assert_preserved_failure(m, target, commit, patchfile, calls, "dependenc")
                metadata.write_bytes(original)

    def test_legacy_missing_build_is_rejected_without_rebuilding(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        (target / m.RECORD_NAME).unlink()
        (target / "dist/index.js").unlink()
        self.assert_preserved_failure(m, target, commit, patchfile, calls, "build")

    def test_legacy_modified_lock_is_rejected(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        (target / m.RECORD_NAME).unlink()
        with (target / "package-lock.json").open("ab") as f:
            f.write(b" ")
        self.assert_preserved_failure(m, target, commit, patchfile, calls, "Lockfile")

    def test_short_or_invalid_commit_rejected_before_any_action(self):
        m = load_helper()
        for commit in ("425e169", PIN.upper(), "x" * 40, "--upload-pack=evil"):
            with self.subTest(commit=commit), patch.object(
                    subprocess, "run", side_effect=AssertionError("no commands")):
                with self.assertRaisesRegex(m.SetupError, "full.*commit"):
                    m.setup(self.work / "no-target", "https://unused.invalid", commit,
                            self.work / "missing.patch", dry_run=True)
        self.assertFalse((self.work / "no-target").exists())

    def test_cli_dry_run_contract(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--path", str(self.work / "absent"),
                                 "--repo", "https://unused.invalid", "--commit", PIN,
                                 "--patch", str(self.work / "missing.patch"), "--dry-run"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "dry-run")
        self.assertFalse((self.work / "absent").exists())

    def test_build_failure_or_mutation_never_records_success(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        for mode, diagnostic in (("source-change", "source"), ("lock-change", "Lockfile"),
                                 ("build-missing", "build"), ("fail", "command failed")):
            with self.subTest(mode=mode), patch.dict(os.environ, FAKE_NPM_MODE=mode):
                fresh = target.with_name("target-" + mode)
                with self.assertRaisesRegex(m.SetupError, diagnostic) as error:
                    m.setup(fresh, str(source), commit, patchfile)
                self.assertNotIn("PRIVATE_TEST_SECRET", str(error.exception))
                self.assertFalse((fresh / m.RECORD_NAME).exists())

    def test_patch_application_fresh_then_already_applied_is_idempotent(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        git(self.work, "clone", "--no-hardlinks", str(source), str(target))
        self.assertEqual(m.ensure_patch(target, patchfile, allow_apply=True), "applied")
        before = snapshot(target)
        self.assertEqual(m.ensure_patch(target, patchfile, allow_apply=True), "already-applied")
        self.assertEqual(snapshot(target), before)
        self.assertFalse(calls.exists())

    def test_record_detects_nonversion_dependency_and_build_mutations(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        for filename in ("node_modules/dep/node_modules/transitive/index.js", "dist/index.js",
                         "tsconfig.json", "node_modules/untracked-injected.js"):
            with self.subTest(filename=filename):
                p = target / filename
                original = p.read_bytes() if p.exists() else None
                p.write_bytes((original or b"") + b"\n// injected code\n")
                self.assert_preserved_failure(m, target, commit, patchfile, calls, "integrity")
                if original is None:
                    p.unlink()
                else:
                    p.write_bytes(original)

    def test_unsafe_symlinks_are_rejected_even_without_integrity_record(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        (target / m.RECORD_NAME).unlink()
        outside = self.work / "private.txt"
        outside.write_text("PRIVATE_TEST_SECRET")
        link = target / "node_modules/.bin/unsafe"
        link.symlink_to(outside)
        self.assert_preserved_failure(m, target, commit, patchfile, calls, "symlink")
        link.unlink()
        link.symlink_to("../dep")  # No symlinked directories / traversal cycles.
        self.assert_preserved_failure(m, target, commit, patchfile, calls, "symlink")

    def test_missing_files_and_invalid_record_have_safe_diagnostics(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        for name in ("package-lock.json", m.RECORD_NAME):
            with self.subTest(name=name):
                p = target / name
                original = p.read_bytes()
                if name == m.RECORD_NAME:
                    p.write_text("PRIVATE_TEST_SECRET not json")
                else:
                    p.unlink()
                self.assert_preserved_failure(m, target, commit, patchfile, calls, "metadata|Lockfile")
                p.write_bytes(original)

    def test_fresh_missing_patch_fails_before_creating_target(self):
        m = load_helper()
        target = self.work / "parent" / "absent"
        with patch.object(subprocess, "run", side_effect=AssertionError("no commands")):
            with self.assertRaisesRegex(m.SetupError, "patch"):
                m.setup(target, "https://unused.invalid", PIN, self.work / "missing.patch")
        self.assertFalse(target.parent.exists())

    def test_other_tracked_source_changes_are_detected(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        (source / "build-options.json").write_text('{"trusted":true}')
        git(source, "add", "build-options.json")
        git(source, "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
            "commit", "-qm", "extra source")
        commit = git(source, "rev-parse", "HEAD")
        m.setup(target, str(source), commit, patchfile)
        (target / "build-options.json").write_text('{"trusted":false}')
        self.assert_preserved_failure(m, target, commit, patchfile, calls, "integrity")

    def test_patch_must_not_change_lock_before_npm_ci(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        (source / "src/index.ts").write_text('export const patched = true;\n')
        lockfile = source / "package-lock.json"
        lockfile.write_bytes(lockfile.read_bytes() + b" ")
        patchfile.write_text(git(source, "diff", "--no-ext-diff") + "\n")
        git(source, "restore", "src/index.ts", "package-lock.json")
        with self.assertRaisesRegex(m.SetupError, "Lockfile"):
            m.setup(target, str(source), commit, patchfile)
        self.assertFalse(calls.exists(), "npm must not run against a patched upstream lockfile")
        self.assertFalse((target / m.RECORD_NAME).exists())

    def test_record_symlink_is_not_treated_as_legacy_or_trusted(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        record = target / m.RECORD_NAME
        outside = self.work / "other-record.json"
        outside.write_bytes(record.read_bytes())
        record.unlink()
        record.symlink_to(outside)
        self.assert_preserved_failure(m, target, commit, patchfile, calls, "symlink")

    def test_readonly_inspector_does_not_call_npm_or_write_bytecode_in_target(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        m.setup(target, str(source), commit, patchfile)
        before, commands = snapshot(target), calls.read_bytes()
        self.assertEqual(m.inspect_installation(target, commit, patchfile),
                         {"status": "reused", "integrity": "VERIFIED"})
        self.assertEqual(snapshot(target), before)
        self.assertEqual(calls.read_bytes(), commands)

    def test_empty_existing_directory_and_target_symlink_are_preserved(self):
        m = load_helper()
        source, target, commit, patchfile, calls = self.fixture()
        target.mkdir()
        before = snapshot(target)
        with self.assertRaises(m.SetupError):
            m.setup(target, str(source), commit, patchfile)
        self.assertEqual(snapshot(target), before)
        self.assertFalse(calls.exists())
        link = self.work / "linked-target"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(m.SetupError, "symlink"):
            m.setup(link, str(source), commit, patchfile)
        self.assertTrue(link.is_symlink())
        self.assertEqual(snapshot(target), before)

    def test_dry_run_creates_nothing_and_executes_nothing(self):
        m = load_helper()
        target = self.work / "absent-parent" / "target"
        with patch.object(subprocess, "run", side_effect=AssertionError("no commands")):
            result = m.setup(target, "https://invalid.example/repo", PIN,
                             self.work / "nonexistent.patch", dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertFalse(target.parent.exists())


@unittest.skipUnless(os.environ.get("KLIPPERMCP_INTEGRATION_SOURCE"),
                     "opt-in isolated local clone / real npm ci integration")
class RealIntegrationTests(unittest.TestCase):
    def test_pinned_real_build_rerun_stdio_and_upload_without_printer(self):
        m = load_helper()
        source = Path(os.environ["KLIPPERMCP_INTEGRATION_SOURCE"]).absolute()
        evidence = Path(os.environ["KLIPPERMCP_INTEGRATION_EVIDENCE"]).absolute()
        self.assertEqual(git(source, "rev-parse", "HEAD"), PIN)
        before_source = snapshot(source)
        evidence.mkdir(parents=True, exist_ok=True)
        target = evidence / "isolated-klippermcp"
        self.assertFalse(target.exists(), "use a NEW evidence directory for each integration run")
        patchfile = ROOT / "patches/klippermcp-kobra-upload.patch"
        commands = []
        original_run = m._run

        def record_run(args, cwd=None, check=True):
            result = original_run(args, cwd, check=False)
            commands.append({"argv": args, "cwd": str(cwd), "returncode": result.returncode})
            if args[0] == "npm":
                (evidence / ("npm-" + args[1] + ".log")).write_bytes(result.stdout + result.stderr)
            (evidence / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
            if check and result.returncode:
                raise m.SetupError("Integration command failed")
            return result

        with patch.dict(os.environ, {"npm_config_cache": str(evidence / "npm-cache"), "npm_config_logs_max": "0"}), patch.object(m, "_run", record_run):
            installed = m.setup(target, str(source), PIN, patchfile)
            before_target = snapshot(target)
            reused = m.setup(target, "https://unused.invalid", PIN, patchfile)
        self.assertEqual(installed, {"status": "installed", "integrity": "VERIFIED"})
        self.assertEqual(reused, {"status": "reused", "integrity": "VERIFIED"})
        self.assertEqual(snapshot(target), before_target)
        upstream_lock = subprocess.run(["git", "show", f"{PIN}:package-lock.json"], cwd=source,
                                       env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
                                       capture_output=True, check=True).stdout
        self.assertEqual((target / "package-lock.json").read_bytes(), upstream_lock)

        traffic = []

        class NoPrinter(BaseHTTPRequestHandler):
            def handle_one_request(self):
                traffic.append("connection")
                super().handle_one_request()

            def do_GET(self):
                traffic.append(["GET", self.path])
                self.send_error(503, "No printer here")

            do_POST = do_GET
            do_PUT = do_GET
            do_DELETE = do_GET

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), NoPrinter)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = {"PATH": os.environ["PATH"], "HOME": str(evidence),
                   "TMPDIR": os.environ.get("TMPDIR", str(evidence)),
                   "MOONRAKER_URL": f"http://127.0.0.1:{server.server_port}", "LOG_LEVEL": "error"}
            transcript = []
            with (evidence / "stdio-stderr.log").open("wb") as stderr:
                process = subprocess.Popen(["node", "dist/index.js"], cwd=target, env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
                assert process.stdin is not None and process.stdout is not None
                stdin, stdout = process.stdin, process.stdout
                buffered = b""

                def send(message):
                    transcript.append({"direction": "sent", "message": message})
                    stdin.write(json.dumps(message).encode() + b"\n")
                    stdin.flush()

                def receive(expected_id):
                    nonlocal buffered
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if b"\n" not in buffered:
                            ready, _, _ = select.select([stdout], [], [], 1)
                            if not ready:
                                continue
                            data = os.read(stdout.fileno(), 65536)
                            self.assertTrue(data, "stdio server exited before response")
                            buffered += data
                        while b"\n" in buffered:
                            line, buffered = buffered.split(b"\n", 1)
                            response = json.loads(line)
                            transcript.append({"direction": "received", "message": response})
                            if response.get("id") == expected_id:
                                self.assertNotIn("error", response)
                                return response
                    self.fail("stdio handshake timed out")

                try:
                    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "offline-stage3-test", "version": "1.0"}}})
                    initialized = receive(1)
                    self.assertIn("serverInfo", initialized["result"])
                    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                    tools = receive(2)["result"]["tools"]
                    names = [tool["name"] for tool in tools]
                    self.assertIn("upload_gcode_file", names)
                    self.assertIn("control_print", names)
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=10), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    process.stdout.close()
                    if not process.stdin.closed:
                        process.stdin.close()
                    (evidence / "stdio-transcript.json").write_text(json.dumps(transcript, indent=2) + "\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            (evidence / "loopback-traffic.json").write_text(json.dumps(traffic) + "\n")
        self.assertEqual(traffic, [], "initialize/tools-list must contact NO printer endpoint")
        self.assertEqual([item["message"]["method"] for item in transcript if item["direction"] == "sent"],
                         ["initialize", "notifications/initialized", "tools/list"])

        # Exercise the REAL compiled upload tool + Moonraker client with fake fetch,
        # never tools/call or network. Fail immediately on ANY other client method.
        upload_script = evidence / "fake-upload.mjs"
        upload_script.write_text('''import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const root = process.argv[2];
const { uploadGcodeFile } = await import(pathToFileURL(root + '/dist/tools/uploadGcodeFile.js'));
const { MoonrakerClient } = await import(pathToFileURL(root + '/dist/moonraker/client.js'));
const calls = [];
let conflict = false;
globalThis.fetch = async (url, options) => {
  const path = new URL(url).pathname;
  calls.push([options.method, path]);
  if (path === '/server/files/list') {
    assert.equal(options.method, 'GET');
    return new Response(JSON.stringify({result: conflict ? [{path:'fixture.gcode',size:6}] : []}),
                        {headers:{'content-type':'application/json'}});
  }
  assert.equal(path, '/server/files/upload', 'no print/action endpoint permitted');
  assert.equal(options.method, 'POST');
  assert.deepEqual([...options.body.keys()].sort(), ['file', 'root']);
  assert.equal(options.body.get('root'), 'gcodes');
  assert.equal(options.body.get('file').name, 'fixture.gcode');
  return new Response(JSON.stringify({result: {action:'create_file',print_started:false,print_queued:false}}),
                      {headers:{'content-type':'application/json'}});
};
const client = new MoonrakerClient({name:'fake',url:'http://127.0.0.1:1'});
const guarded = new Proxy(client, {get(target, property) {
  if (!['printerName', 'listGcodeFiles', 'uploadFile'].includes(property)) {
    throw new Error('unexpected client call: ' + String(property));
  }
  return typeof target[property] === 'function' ? target[property].bind(target) : target[property];
}});
const manager = {getClient: () => guarded};
const result = JSON.parse((await uploadGcodeFile(manager, {localPath:process.argv[3]})).content[0].text);
assert.equal(result.uploaded, true);
assert.equal(result.result.print_started, false);
assert.equal(result.result.print_queued, false);
conflict = true;
const second = JSON.parse((await uploadGcodeFile(manager, {localPath:process.argv[3]})).content[0].text);
assert.equal(second.conflict, true);
assert.deepEqual(calls, [['GET','/server/files/list'],['POST','/server/files/upload'],['GET','/server/files/list']]);
console.log(JSON.stringify({result,conflict:second,calls,printStartCalls:0}));
''')
        gcode = evidence / "fixture.gcode"
        gcode.write_text("G1 X0\n")
        upload = subprocess.run(["node", str(upload_script), str(target), str(gcode)],
                                cwd=evidence, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(upload.returncode, 0, upload.stderr)
        upload_evidence = json.loads(upload.stdout)
        self.assertEqual(upload_evidence["printStartCalls"], 0)
        (evidence / "fake-upload-result.json").write_text(json.dumps(upload_evidence, indent=2) + "\n")
        self.assertEqual(snapshot(source), before_source, "installed source path must remain untouched")
        self.assertEqual(snapshot(target), before_target, "stdio/upload probes must not modify installed tree")
        summary = {"pin": PIN, "npm_ci_exit": 0, "npm_build_exit": 0,
                   "lock_sha256": hashlib.sha256(upstream_lock).hexdigest(),
                   "installed": installed, "rerun": reused, "tool_count": len(names),
                   "tool_names": names, "loopback_connections": len(traffic),
                   "fake_upload_print_start_calls": 0, "source_unchanged": True,
                   "target_unchanged_by_probes": True}
        (evidence / "integration-summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
