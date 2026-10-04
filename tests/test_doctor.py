"""Doctor tests use local fixtures and loopback mocks, never real printer data."""
import sys
sys.dont_write_bytecode = True
import importlib.util
import unittest
import hashlib
import json
import os
import shutil
import tempfile
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from unittest import mock
from pathlib import Path

SENTINEL = "DOCTOR_SECRET_MARKER"


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
            None if p.is_dir() else p.read_bytes())
            for p in [root, *sorted(root.rglob("*"))]}


class Fixture:
    """All binaries/manifests below are MOCKED test evidence, not real installs."""
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="doctor-fixture-")
        self.root = Path(self.tmp.name)
        self.profile = self.root / "profile"
        self.home = self.root / "home"
        self.source = self.root / "source"
        self.bin = self.root / "bin"
        self.project = self.root / "project space"
        self.mcp = self.root / "mock-mcp"
        self.orca = self.root / "mock-orca.AppImage"
        self.os_release = self.root / "os-release"
        self.vars = {
            "PROJECT_DIR": str(self.project), "KLIPPER_MCP_PATH": str(self.mcp),
            "FREECADCMD_PATH": str(self.bin / "freecadcmd"), "ORCA_SLICER_PATH": str(self.orca),
            "ORCA_PROFILE_ROOT": str(self.root / "orca-profiles"),
            "KOBRA_S1_MOONRAKER_URL": "http://127.0.0.1:1"}
        for path in [self.profile, self.home, self.source / "checksums", self.source / "patches",
                     self.bin, self.project / "Druckprofile", self.project / "Filamente",
                     self.mcp / "dist", self.root / "orca-profiles", self.home / "xdg"]:
            path.mkdir(parents=True, exist_ok=True)
        self.os_release.write_text('ID=debian\nVERSION_ID="13"\n')
        self.orca.write_bytes(b"MOCKED AppImage bytes - never executed")
        entry = {"automatic_verification_approved": True,
                 "asset": "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage",
                 "sha256": hashlib.sha256(self.orca.read_bytes()).hexdigest(),
                 "sha512": hashlib.sha512(self.orca.read_bytes()).hexdigest(),
                 "community_anchor": {"kind": "AUR Community PKGBUILD", "commit": "a" * 40}}
        (self.source / "checksums/orcaslicer-2.4.2.json").write_text(json.dumps(
            {"schema_version": 1, "version": "2.4.2", "architectures": {"x86_64": entry}}))
        (self.source / "patches/klippermcp-kobra-upload.patch").write_text("MOCKED PATCH")
        (self.mcp / "dist/index.js").write_text("MOCKED BUILD - never executed")
        (self.mcp / ".git").mkdir()
        self.command("node", 'printf "v20.19.0\\n"')
        self.command("git", 'case "$*" in *rev-parse*) printf "425e16905c16b6c078028b5063fcb21e0591b190\\n";; *"config --null --name-only --get-regexp"*) exit 1;; *"apply --reverse --check"*) exit 0;; *) exit 90;; esac')
        for name in ["hermes", "freecadcmd", "npm"]:
            self.command(name, "exit 91")  # must never execute these
        self.machine = {"type": "machine", "name": "Anycubic Kobra S1 0.4 nozzle",
                        "nozzle_diameter": ["0.4"], "printable_height": "250",
                        "machine_start_gcode": "G9111\nT[initial_tool]",
                        "support_multi_bed_types": "1", "default_bed_type": "4"}
        self.process = {"type": "process", "name": "0.20mm Standard @Anycubic Kobra S1 0.4 nozzle",
                        "layer_height": "0.2", "compatible_printers": [self.machine["name"]]}
        self.filament = {"type": "filament", "name": "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle",
                         "filament_diameter": ["1.75"], "nozzle_temperature": ["220"],
                         "bed_type": ["Textured PEI Plate"], "compatible_printers": [self.machine["name"]]}
        for kind, data in [("machine", self.machine), ("process", self.process), ("filament", self.filament)]:
            path = self.project / ("Filamente" if kind == "filament" else "Druckprofile") / (data["name"] + ".json")
            path.write_text(json.dumps(data))
            self.vars["KOBRA_" + kind.upper() + "_PROFILE"] = str(path)
        self.write_env()
        (self.profile / "config.yaml").write_text(
            'model:\n  default: mock-model\n  provider: mock-provider\n'
            'mcp_servers:\n  kobra:\n    command: node\n    args: ["${KLIPPER_MCP_PATH}/dist/index.js"]\n'
            '    env: {PRINTER_KOBRA: "${KOBRA_S1_MOONRAKER_URL}"}\n    enabled: true\n')
        (self.home / ".env").write_text(SENTINEL + " - DO NOT READ")
        (self.home / "auth.json").write_text(SENTINEL + " - DO NOT READ")
        self.env = dict(os.environ, PATH=str(self.bin), HOME=str(self.home), HERMES_HOME=str(self.home),
                        XDG_CACHE_HOME=str(self.home / "xdg"), XDG_CONFIG_HOME=str(self.home / "xdg"),
                        XDG_DATA_HOME=str(self.home / "xdg"), PYTHONDONTWRITEBYTECODE="1")

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n# MOCKED fixture command\n" + body + "\n")
        path.chmod(0o755)

    def write_env(self):
        (self.profile / ".env").write_text("\n".join(k + "=" + json.dumps(v) for k, v in self.vars.items()) +
                                             '\nOPENROUTER_API_KEY="' + SENTINEL + '"\n')

    def close(self):
        self.tmp.cleanup()

ROOT = Path(__file__).resolve().parents[1]


def load_doctor():
    spec = importlib.util.spec_from_file_location("doctor_under_test", ROOT / "scripts/doctor.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.d = load_doctor()

    def test_exit_severity_and_fixed_output(self):
        report = self.d.Report()
        report.add("PASS", "environment", "valid")
        self.assertEqual(report.exit_code, 0)
        report.add("NOTVERIFIED", "network", "offline")
        self.assertEqual(report.exit_code, 1)
        report.add("WARN", "mapping", "plausible")
        self.assertEqual(report.exit_code, 1)
        report.add("FAIL", "environment", "invalid")
        self.assertEqual(report.exit_code, 2)
        self.assertIn("NOT VERIFIED network: offline", report.render())

    def test_dotenv_literal_spaces_exports_escapes_unknown_ignored(self):
        values = self.d.parse_env('export PROJECT_DIR="/fixture/a b"\n'
                                  "FREECADCMD_PATH='/fixture/freecad' # comment\n"
                                  'KOBRA_S1_MOONRAKER_URL="http://localhost:7125"\n'
                                  'KOBRA_MACHINE_PROFILE="/fixture/a\\\\b\\\"c"\n'
                                  'SECRET_TOKEN="MARKER"\n')
        self.assertEqual(values["PROJECT_DIR"], "/fixture/a b")
        self.assertEqual(values["KOBRA_MACHINE_PROFILE"], '/fixture/a\\b"c')
        self.assertNotIn("SECRET_TOKEN", values)

    def test_dotenv_rejects_duplicates_and_malformed_syntax_without_values(self):
        for text in ['PROJECT_DIR=a\nPROJECT_DIR=b', 'PROJECT_DIR="MARKER',
                     'PROJECT_DIR="a" MARKER', 'export nonsense', 'PROJECT_DIR="a\\q"']:
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as error:
                    self.d.parse_env(text)
                self.assertNotIn("MARKER", str(error.exception))

    def test_dotenv_never_evaluates_interpolation(self):
        value = self.d.parse_env('PROJECT_DIR="$(touch /not-created)"')["PROJECT_DIR"]
        self.assertEqual(value, "$(touch /not-created)")


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.d = load_doctor()
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def run_local(self):
        report = self.d.Report()
        with mock.patch.dict(os.environ, self.f.env, clear=True), mock.patch.object(self.d.platform, "machine", return_value="x86_64"):
            values = self.d.local_checks(report, self.f.profile, self.f.home, self.f.source, 1,
                                         os_release=self.f.os_release)
        return report, values

    def status(self, report, label):
        return next(c[0] for c in report.checks if c[1] == label)

    def test_complete_mocked_local_install_passes_and_changes_nothing(self):
        before = snapshot(self.f.root)
        report, values = self.run_local()
        self.assertEqual(report.exit_code, 0, report.render())
        self.assertEqual(values["PROJECT_DIR"], str(self.f.project))
        self.assertEqual(snapshot(self.f.root), before)
        self.assertNotIn(SENTINEL, report.render())
        self.assertNotIn(str(self.f.root), report.render())

    def test_required_environment_missing_is_fail_no_cross_profile_fallback(self):
        (self.f.profile / ".env").unlink()
        report, values = self.run_local()
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(self.status(report, "environment"), "FAIL")
        self.assertEqual(values, {})

    def test_model_provider_structure_not_top_level_or_credentials(self):
        (self.f.profile / "config.yaml").write_text('model: mock-model\nprovider: mock-provider\napi_key: ' + SENTINEL)
        report, _ = self.run_local()
        self.assertEqual(self.status(report, "model-provider"), "FAIL")
        self.assertNotIn(SENTINEL, report.render())

    def test_main_config_model_fallback_only_when_profile_model_absent(self):
        config = self.f.profile / "config.yaml"
        config.write_text('mcp_servers:' + config.read_text().split('mcp_servers:', 1)[1])
        (self.f.home / "config.yaml").write_text('model: {default: mock-model, provider: mock-provider}\n')
        report, _ = self.run_local()
        self.assertEqual(self.status(report, "model-provider"), "PASS")

    def test_yaml_unavailable_is_notverified_without_guessing(self):
        with mock.patch.object(self.d, "load_yaml", side_effect=ImportError):
            report, _ = self.run_local()
        self.assertEqual(self.status(report, "model-provider"), "NOTVERIFIED")
        self.assertEqual(report.exit_code, 1)

    def test_missing_commands_no_runtime_calls(self):
        for name in ["hermes", "node", "npm", "git", "freecadcmd"]:
            (self.f.bin / name).unlink()
        report, _ = self.run_local()
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(self.status(report, "node"), "FAIL")

    def test_node_old_or_untrusted_output_fails_without_echo(self):
        for output in ["v18.0.0", SENTINEL, "v20.0.0\\n" + SENTINEL]:
            self.f.command("node", 'printf "%s\\n" "' + output + '"')
            report, _ = self.run_local()
            self.assertEqual(self.status(report, "node"), "FAIL")
            self.assertNotIn(SENTINEL, report.render())

    def test_full_commit_must_match_and_patch_check_is_read_only(self):
        self.f.command("git", 'case "$*" in *rev-parse*) printf "425e169\\n";; *) exit 1;; esac')
        report, _ = self.run_local()
        self.assertEqual(self.status(report, "mcp-commit"), "FAIL")
        self.assertEqual(self.status(report, "mcp-patch"), "FAIL")

    def test_build_presence_is_not_handshake(self):
        report, _ = self.run_local()
        self.assertIn("not a handshake", report.render())
        (self.f.mcp / "dist/index.js").unlink()
        report, _ = self.run_local()
        self.assertEqual(self.status(report, "mcp-build"), "FAIL")

    def test_orca_hash_does_not_execute_and_mismatch_fails(self):
        self.f.orca.write_bytes(b"#!/bin/sh\nexit 97\n" + SENTINEL.encode())
        self.f.orca.chmod(0o755)
        report, _ = self.run_local()
        self.assertEqual(self.status(report, "orca-checksum"), "FAIL")
        self.assertNotIn(SENTINEL, report.render())

    def test_unapproved_architecture_is_notverified(self):
        with mock.patch.dict(os.environ, self.f.env, clear=True), mock.patch.object(self.d.platform, "machine", return_value="aarch64"):
            report = self.d.Report()
            self.d.local_checks(report, self.f.profile, self.f.home, self.f.source, 1, os_release=self.f.os_release)
        self.assertEqual(self.status(report, "orca-checksum"), "NOTVERIFIED")

    def test_profiles_required_missing_wrong_types_names_references(self):
        path = Path(self.f.vars["KOBRA_PROCESS_PROFILE"])
        for data in [[], {"name": SENTINEL}, dict(self.f.process, layer_height="garbage"),
                     dict(self.f.process, compatible_printers=["Wrong printer"]),
                     dict(self.f.process, inherits="../auth.json")]:
            path.write_text(json.dumps(data))
            report, _ = self.run_local()
            self.assertEqual(self.status(report, "profile-process"), "FAIL")
            self.assertNotIn(SENTINEL, report.render())

    def test_profile_inheritance_resolves_read_only_and_rejects_cycle(self):
        root = Path(self.f.vars["ORCA_PROFILE_ROOT"]) / "process"
        root.mkdir()
        (root / "base.json").write_text(json.dumps({"layer_height": "0.2"}))
        data = dict(self.f.process, inherits="base")
        del data["layer_height"]
        Path(self.f.vars["KOBRA_PROCESS_PROFILE"]).write_text(json.dumps(data))
        report, _ = self.run_local()
        self.assertEqual(self.status(report, "profile-process"), "PASS")
        (root / "base.json").write_text('{"inherits":"base"}')
        report, _ = self.run_local()
        self.assertEqual(self.status(report, "profile-process"), "FAIL")

    def test_symlink_environment_rejected_without_reading_other_profile(self):
        (self.f.profile / ".env").unlink()
        (self.f.profile / ".env").symlink_to(self.f.home / ".env")
        report, _ = self.run_local()
        self.assertEqual(self.status(report, "environment"), "FAIL")

    def test_malformed_env_blocks_all_network_values(self):
        (self.f.profile / ".env").write_text('PROJECT_DIR="' + SENTINEL)
        report, values = self.run_local()
        self.assertEqual(values, {})
        self.assertNotIn(SENTINEL, report.render())


class MockMoonraker:
    """Real loopback HTTP server with a complete method/path request ledger."""
    def __init__(self):
        self.requests = []
        self.responses = {
            "/server/info": {"result": {"klippy_connected": True, "klippy_state": "ready"}},
            "/printer/info": {"result": {"model": "Anycubic Kobra S1", "hostname": SENTINEL}},
            "/printer/objects/list": {"result": {"objects": ["mmu", "mmu_machine", "configfile", "wifi", SENTINEL]}},
            "/printer/objects/query": {"result": {"status": {
                "mmu": {"enabled": True, "gate_status": [0, 0, 0, 0], "tool_to_gate_map": [0, 1, 2, 3],
                        "slicer_tool_map": {"tools": [{"name": "T" + str(i), "material": SENTINEL, "temp": 220} for i in range(4)]}},
                "mmu_machine": {"num_gates": 4, "vendor": "Anycubic", "model": "ACE Pro"},
                "configfile": {"config": SENTINEL}, "wifi": SENTINEL}}}}
        self.special = {}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.command, self.path, dict(self.headers)))
                path = urlsplit(self.path).path
                code, body, headers, delay = owner.special.get(path, (200, owner.responses.get(path, {}), {}, 0))
                if delay:
                    time.sleep(delay)
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_POST = do_GET
            do_PUT = do_GET
            do_DELETE = do_GET
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def assert_safe(self, case):
        for method, target, headers in self.requests:
            parsed = urlsplit(target)
            case.assertEqual(method, "GET")
            case.assertIn(parsed.path, ["/server/info", "/printer/info", "/printer/objects/list", "/printer/objects/query"])
            case.assertNotIn("Authorization", headers)
            case.assertNotIn("Cookie", headers)
            if parsed.path == "/printer/objects/query":
                fields = parse_qs(parsed.query)
                case.assertTrue(fields)
                case.assertTrue(set(fields) <= {"mmu", "mmu_machine"})
                if "mmu" in fields:
                    case.assertEqual(fields["mmu"][0].split(","), ["gate_status", "slicer_tool_map", "tool_to_gate_map", "ttg_map", "enabled", "num_gates"])
                if "mmu_machine" in fields:
                    case.assertEqual(fields["mmu_machine"][0].split(","), ["num_gates", "vendor", "model", "units", "num_units", "unit_0", "unit_1"])
            else:
                case.assertEqual(parsed.query, "")


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.d = load_doctor()
        self.server = MockMoonraker()
        self.addCleanup(self.server.close)
        self.addCleanup(self.server.assert_safe, self)

    def run_network(self, offline=False, timeout=.15, url=None):
        report = self.d.Report()
        self.d.network_checks(report, self.server.url if url is None else url, offline, timeout)
        self.assertNotIn(SENTINEL, report.render())
        self.assertNotIn(self.server.url, report.render())
        return report

    def status(self, report, label):
        return next(c[0] for c in report.checks if c[1] == label)

    def test_real_mock_http_logged_requests_explicit_model_ace_mapping_exit_zero(self):
        report = self.run_network()
        self.assertEqual(report.exit_code, 0, report.render())
        self.assertEqual(len(self.server.requests), 4)
        self.assertEqual(self.status(report, "ace-pro"), "PASS")
        self.assertEqual(self.status(report, "tool-gate-mapping"), "PASS")
        self.server.assert_safe(self)

    def test_offline_performs_zero_requests_and_exit_one(self):
        report = self.run_network(offline=True)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(self.server.requests, [])
        self.assertIn("NOT VERIFIED", report.render())

    def test_connected_false_is_failure_separate_from_ready_state(self):
        self.server.responses["/server/info"]["result"]["klippy_connected"] = False
        report = self.run_network()
        self.assertEqual(self.status(report, "klippy-connected"), "FAIL")
        self.assertEqual(self.status(report, "klippy-state"), "PASS")
        self.assertEqual(report.exit_code, 2)

    def test_connected_true_shutdown_state_is_failure(self):
        self.server.responses["/server/info"]["result"]["klippy_state"] = "shutdown"
        report = self.run_network()
        self.assertEqual(self.status(report, "klippy-connected"), "PASS")
        self.assertEqual(self.status(report, "klippy-state"), "FAIL")

    def test_auth_denied_server_failure_and_body_secret_never_printed(self):
        for code in [401, 403, 500]:
            with self.subTest(code=code):
                self.server.special["/server/info"] = (code, SENTINEL.encode(), {}, 0)
                report = self.run_network()
                self.assertEqual(report.exit_code, 2)
                self.assertIn("FAIL moonraker-server", report.render())

    def test_bad_json_duplicate_keys_and_large_response_fail(self):
        for body in [SENTINEL.encode(), b'{"result": {}, "result": {}}', b"x" * (1024 * 1024 + 1)]:
            self.server.special["/server/info"] = (200, body, {}, 0)
            report = self.run_network()
            self.assertEqual(report.exit_code, 2)

    def test_timeout_fails_without_exception_payload(self):
        self.server.special["/server/info"] = (200, SENTINEL.encode(), {}, .25)
        report = self.run_network(timeout=.02)
        self.assertEqual(report.exit_code, 2)

    def test_redirect_does_not_follow_unsafe_endpoint(self):
        self.server.special["/server/info"] = (302, SENTINEL.encode(), {"Location": self.server.url + "/machine/reboot"}, 0)
        report = self.run_network()
        self.assertEqual(report.exit_code, 2)
        self.assertNotIn("/machine/reboot", [x[1] for x in self.server.requests])

    def test_proxies_disabled_even_environment_points_to_another_mock(self):
        other = MockMoonraker()
        self.addCleanup(other.close)
        with mock.patch.dict(os.environ, {"http_proxy": other.url, "HTTP_PROXY": other.url, "ALL_PROXY": other.url, "NO_PROXY": ""}):
            report = self.run_network()
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(other.requests, [])

    def test_invalid_baseurl_no_requests_no_secrets(self):
        for value in [self.server.url + "/unsafe", self.server.url + "?token=" + SENTINEL,
                      self.server.url + "#" + SENTINEL, "http://user:" + SENTINEL + "@localhost",
                      "ftp://127.0.0.1", "http:///missing", "http://localhost:bad", "http://localhost\\@127.0.0.1",
                      self.server.url + "/%2f", self.server.url + "\n" + SENTINEL]:
            report = self.run_network(url=value)
            self.assertEqual(report.exit_code, 2)
        self.assertEqual(self.server.requests, [])

    def test_hostname_is_not_model_and_four_gates_not_ace_identity(self):
        self.server.responses["/printer/info"] = {"result": {"hostname": "Anycubic Kobra S1"}}
        self.server.responses["/printer/objects/query"]["result"]["status"]["mmu_machine"] = {"num_gates": 4}
        report = self.run_network()
        self.assertEqual(self.status(report, "printer-model"), "NOTVERIFIED")
        self.assertEqual(self.status(report, "ace-pro"), "NOTVERIFIED")
        self.assertEqual(report.exit_code, 1)

    def test_explicit_non_ace_model_warns(self):
        self.server.responses["/printer/objects/query"]["result"]["status"]["mmu_machine"]["model"] = "Other MMU"
        report = self.run_network()
        self.assertEqual(self.status(report, "ace-pro"), "WARN")

    def test_no_occupied_gates_still_four_gate_hardware(self):
        for gates in [[0, 0, 0, 0], [-1, -1, -1, -1]]:
            self.server.responses["/printer/objects/query"]["result"]["status"]["mmu"]["gate_status"] = gates
            report = self.run_network()
            self.assertEqual(self.status(report, "gate-count"), "PASS")
            self.assertEqual(self.status(report, "mmu-status"), "PASS")

    def test_names_only_are_plausibility_not_physical_mapping(self):
        del self.server.responses["/printer/objects/query"]["result"]["status"]["mmu"]["tool_to_gate_map"]
        report = self.run_network()
        self.assertEqual(self.status(report, "tool-gate-mapping"), "NOTVERIFIED")
        self.assertIn("plausible", report.render())

    def test_explicit_duplicate_out_of_range_wrong_mapping_warns(self):
        mmu = self.server.responses["/printer/objects/query"]["result"]["status"]["mmu"]
        for value in [[0, 0, 2, 3], [0, 1, 2, 4], [3, 2, 1, 0], [False, 1, 2, 3], SENTINEL]:
            mmu["tool_to_gate_map"] = value
            report = self.run_network()
            self.assertEqual(self.status(report, "tool-gate-mapping"), "WARN")

    def test_ttg_alias_and_unit_literal_identity(self):
        status = self.server.responses["/printer/objects/query"]["result"]["status"]
        status["mmu"]["ttg_map"] = status["mmu"].pop("tool_to_gate_map")
        status["mmu_machine"] = {"num_gates": 4, "units": [{"vendor": "Anycubic", "model": "ACE Pro"}]}
        report = self.run_network()
        self.assertEqual(report.exit_code, 0, report.render())

    def test_absent_objects_do_not_query_or_infer_from_unrequested_status(self):
        self.server.responses["/printer/objects/list"] = {"result": {"objects": ["configfile", "wifi"]}}
        report = self.run_network()
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.status(report, "mmu-status"), "NOTVERIFIED")
        self.assertEqual(self.status(report, "ace-pro"), "NOTVERIFIED")

    def test_explicit_literal_vendor_ace_identity_and_only_listed_objects_queried(self):
        self.server.responses["/printer/objects/list"]["result"]["objects"] = ["mmu_machine"]
        self.server.responses["/printer/objects/query"]["result"]["status"]["mmu_machine"] = {"vendor": "ACE Pro", "num_gates": 4}
        report = self.run_network()
        self.assertEqual(self.status(report, "ace-pro"), "PASS")
        query = parse_qs(urlsplit(self.server.requests[-1][1]).query)
        self.assertEqual(set(query), {"mmu_machine"})
        self.assertEqual(self.status(report, "tool-gate-mapping"), "NOTVERIFIED")

    def test_client_refuses_arbitrary_endpoints_objects_and_fields(self):
        client = self.d.Moonraker(self.server.url, .15)
        for path, objects in [("/machine/reboot", ()), ("/printer/gcode/script", ()),
                              ("/printer/objects/query", ("configfile",)),
                              ("/printer/objects/query", ("mmu:all",)),
                              ("/printer/info", ("mmu",))]:
            with self.assertRaises(ValueError):
                client.get(path, objects)
        self.assertEqual(self.server.requests, [])

    def test_empty_gate_array_is_not_absence_of_mmu_hardware(self):
        self.server.responses["/printer/objects/query"]["result"]["status"]["mmu"]["gate_status"] = []
        report = self.run_network()
        self.assertEqual(self.status(report, "mmu-status"), "PASS")
        self.assertEqual(self.status(report, "ace-pro"), "PASS")
        self.assertEqual(self.status(report, "gate-count"), "PASS")

    def test_absent_optional_status_fields_notverified(self):
        self.server.responses["/server/info"] = {"result": {}}
        self.server.responses["/printer/objects/query"] = {"result": {"status": {"mmu": {}, "mmu_machine": {}}}}
        report = self.run_network()
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(self.status(report, "klippy-connected"), "NOTVERIFIED")
        self.assertEqual(self.status(report, "gate-count"), "NOTVERIFIED")


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.d = load_doctor()
        self.f = Fixture()
        self.addCleanup(self.f.close)

    run_local = LocalTests.run_local
    status = LocalTests.status

    def test_command_allowlist_and_git_optional_locks_node_preloads_disabled(self):
        self.f.env.update(NODE_OPTIONS=SENTINEL, GIT_DIR=SENTINEL)
        original = self.d.subprocess.run
        with mock.patch.object(self.d.subprocess, "run", wraps=original) as calls:
            report, _ = self.run_local()
        self.assertEqual(report.exit_code, 0, report.render())
        self.assertEqual(len(calls.call_args_list), 4)
        arguments = [call.args[0] for call in calls.call_args_list]
        self.assertEqual(arguments[0], [str(self.f.bin / "node"), "--version"])
        self.assertEqual(arguments[1][1:], ["rev-parse", "--verify", "HEAD^{commit}"])
        self.assertEqual(arguments[2][1:], ["config", "--null", "--name-only", "--get-regexp",
                                          r"^filter\..*\.(clean|smudge|process)$"])
        self.assertEqual(arguments[3][1:4], ["apply", "--reverse", "--check"])
        for call in calls.call_args_list:
            self.assertEqual(call.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
            self.assertNotIn("GIT_DIR", call.kwargs["env"])
            self.assertNotIn("NODE_OPTIONS", call.kwargs["env"])
            self.assertFalse(call.kwargs.get("shell", False))

    def test_no_other_env_auth_or_secret_config_reads(self):
        original_open = self.d.os.open
        original_read = Path.read_text
        forbidden = {self.f.home / ".env", self.f.home / "auth.json", self.f.home / "providers.yaml"}
        reads = []
        def guarded_open(path, *args, **kwargs):
            reads.append(Path(path))
            self.assertNotIn(Path(path), forbidden)
            return original_open(path, *args, **kwargs)
        def guarded_read(path, *args, **kwargs):
            reads.append(path)
            self.assertNotIn(path, forbidden)
            return original_read(path, *args, **kwargs)
        with mock.patch.object(self.d.os, "open", side_effect=guarded_open), mock.patch.object(Path, "read_text", guarded_read):
            report, _ = self.run_local()
        self.assertEqual(report.exit_code, 0, report.render())
        self.assertIn(self.f.profile / ".env", reads)

    def test_duplicate_yaml_json_keys_unsafe_tags_aliases_rejected(self):
        config = self.f.profile / "config.yaml"
        for text in ['model: {}\nmodel: {}\n', 'model: &x {default: a, provider: b}\ncopy: *x\n',
                     'model: !!python/object/apply:os.system ["' + SENTINEL + '"]\n']:
            config.write_text(text)
            report, _ = self.run_local()
            self.assertEqual(self.status(report, "model-provider"), "FAIL")
            self.assertNotIn(SENTINEL, report.render())
        with self.assertRaises(ValueError):
            self.d.unique_object([("key", "one"), ("key", "two")])

    def test_literal_paths_reject_relative_interpolated_and_control_values(self):
        for value in ["relative", "$HOME/secret", "`command`", "/path\x00secret", "/path\nsecret"]:
            with self.assertRaises(ValueError):
                self.d.literal_path(value)

    def test_bounded_file_reads_reject_fifo_symlink_and_oversize(self):
        path = self.f.root / "untrusted"
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            self.d.read_text(path)
        path.unlink()
        path.symlink_to(self.f.home / "auth.json")
        with self.assertRaises(OSError):
            self.d.read_text(path)
        path.unlink()
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.d.read_text(path)

    def test_orca_env_or_auth_path_is_rejected_before_any_content_read(self):
        for artifact in [self.f.home / ".env", self.f.home / "auth.json"]:
            self.f.vars["ORCA_SLICER_PATH"] = str(artifact)
            self.f.write_env()
            original = self.d.os.open
            def guarded(path, *args, **kwargs):
                self.assertNotEqual(Path(path), artifact)
                return original(path, *args, **kwargs)
            with mock.patch.object(self.d.os, "open", side_effect=guarded):
                report, _ = self.run_local()
            self.assertEqual(self.status(report, "orca-checksum"), "FAIL")

    def test_symlink_checksum_manifest_not_followed_to_secrets(self):
        manifest = self.f.source / "checksums/orcaslicer-2.4.2.json"
        manifest.unlink()
        manifest.symlink_to(self.f.home / "auth.json")
        original = Path.read_text
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path, manifest)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", guarded):
            report, _ = self.run_local()
        self.assertEqual(self.status(report, "orca-checksum"), "FAIL")

    def test_symlink_selected_profile_does_not_read_unselected_config(self):
        real = self.f.profile
        alias = self.f.root / "profile-alias"
        alias.symlink_to(real, target_is_directory=True)
        self.f.profile = alias
        original = self.d.os.open
        def guarded(path, *args, **kwargs):
            self.assertFalse(str(path).startswith(str(alias)))
            return original(path, *args, **kwargs)
        with mock.patch.object(self.d.os, "open", side_effect=guarded):
            report, _ = self.run_local()
        self.assertEqual(self.status(report, "environment"), "FAIL")
        self.assertEqual(self.status(report, "model-provider"), "FAIL")

    def test_invalid_inheritance_types_empty_and_secret_basename_never_read(self):
        path = Path(self.f.vars["KOBRA_PROCESS_PROFILE"])
        for reference in [[], {}, False, 4, "auth.json", "config.yaml"]:
            path.write_text(json.dumps(dict(self.f.process, inherits=reference)))
            report, _ = self.run_local()
            self.assertEqual(self.status(report, "profile-process"), "FAIL")


class CLITests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.server = MockMoonraker()
        self.addCleanup(self.server.close)
        self.addCleanup(self.server.assert_safe, self)
        self.f.vars["KOBRA_S1_MOONRAKER_URL"] = self.server.url
        self.f.write_env()
        scripts = self.f.source / "scripts"
        scripts.mkdir()
        for name in ["doctor.py", "verify_orca.py"]:
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        self.script = scripts / "doctor.py"

    def cli(self, *extra, bytecode_flag=True, env=None):
        command = [sys.executable] + (["-B"] if bytecode_flag else []) + [str(self.script),
                   "--profile-dir", str(self.f.profile), "--hermes-home", str(self.f.home),
                   "--source-dir", str(self.f.source), "--timeout", "0.5", *extra]
        result = subprocess.run(command, cwd=self.f.home, env=self.f.env if env is None else env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        self.assertNotIn(str(self.f.root), result.stdout + result.stderr)
        return result

    def test_online_cli_exit_zero_fixed_report_logged_gets_and_no_changes(self):
        before = snapshot(self.f.root)
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS orca-checksum:", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(self.server.requests), 4)
        self.assertEqual(snapshot(self.f.root), before)

    def test_offline_cli_exit_one_zero_requests_no_bytecode_without_B(self):
        environment = dict(self.f.env)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        before = snapshot(self.f.root)
        result = self.cli("--offline", bytecode_flag=False, env=environment)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(snapshot(self.f.root), before)
        self.assertFalse(list(self.f.root.rglob("*.pyc")))

    def test_required_failure_exit_two_offline_no_changes(self):
        (self.f.profile / ".env").unlink()
        before = snapshot(self.f.root)
        result = self.cli("--offline")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(snapshot(self.f.root), before)
        self.assertEqual(self.server.requests, [])

    def test_readonly_directories_files_home_xdg_commands_remain_unchanged(self):
        paths = [self.f.root, *self.f.root.rglob("*")]
        original = {p: p.stat().st_mode & 0o777 for p in paths}
        try:
            for path in paths:
                path.chmod(0o555 if path.is_dir() or original[path] & 0o111 else 0o444)
            before = snapshot(self.f.root)
            result = self.cli()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(snapshot(self.f.root), before)
        finally:
            for path in paths:
                path.chmod(original[path])

    def test_cli_invalid_arguments_and_timeout_never_echo_input(self):
        for args in [("--unknown-" + SENTINEL,), ("--timeout", SENTINEL),
                     ("--timeout", "nan"), ("--timeout", "0"), ("--timeout", "31")]:
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2)
        self.assertEqual(self.server.requests, [])

    def test_help_no_file_reads_or_commands_no_changes(self):
        before = snapshot(self.f.root)
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--profile-dir", result.stdout)
        self.assertEqual(snapshot(self.f.root), before)
        self.assertEqual(self.server.requests, [])

    def test_dotenv_shell_payload_never_executes(self):
        self.f.vars["PROJECT_DIR"] = "$(touch " + str(self.f.root / "owned") + ")"
        self.f.write_env()
        before = snapshot(self.f.root)
        result = self.cli("--offline")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(snapshot(self.f.root), before)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
