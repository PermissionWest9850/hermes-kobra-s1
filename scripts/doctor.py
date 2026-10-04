#!/usr/bin/env python3
"""Read-only installation diagnostics. No repairs or runtime/handshake probes.

Exit 0: required checks passed; 1: warnings/unverified required checks; 2: failure.
An unexposed exact ACE marketing identity is informational, not a requirement.
Only selected .env/config.yaml are read; credentials are not validated.
"""
import sys
sys.dont_write_bytecode = True  # Before optional YAML and checksum-helper imports.
import re
import os
import stat
import json
import math
import platform
import shutil
import subprocess
import importlib.util
import argparse
from pathlib import Path
import ipaddress
import urllib.error
import urllib.parse
import urllib.request

ENV_KEYS = frozenset(("PROJECT_DIR", "KOBRA_S1_MOONRAKER_URL", "KLIPPER_MCP_PATH",
                      "FREECADCMD_PATH", "ORCA_SLICER_PATH", "ORCA_PROFILE_ROOT",
                      "KOBRA_MACHINE_PROFILE", "KOBRA_PROCESS_PROFILE", "KOBRA_FILAMENT_PROFILE"))
EXPECTED_COMMIT = "425e16905c16b6c078028b5063fcb21e0591b190"
ORCA_VERSION = "2.4.2"


class Report:
    def __init__(self):
        self.checks = []

    def add(self, status, label, detail):
        if status not in ("PASS", "WARN", "NOTVERIFIED", "FAIL"):
            raise ValueError("Invalid check status")
        self.checks.append((status, label, detail))

    @property
    def exit_code(self):
        if any(c[0] == "FAIL" for c in self.checks):
            return 2
        return int(any(c[0] != "PASS" and not
                       (c[0] == "NOTVERIFIED" and c[1] == "ace-pro") for c in self.checks))

    def render(self):
        return "\n".join("%s %s: %s" % ("NOT VERIFIED" if status == "NOTVERIFIED" else status, label, detail)
                         for status, label, detail in self.checks)


def parse_env(text):
    """Parse a deliberately small dotenv grammar as DATA; never expand/eval it."""
    result, seen = {}, set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match or match[1] in seen:
            raise ValueError("Invalid environment syntax")
        key, value = match.groups()
        seen.add(key)
        if value.startswith(('"', "'")):
            quote, chars, pos = value[0], [], 1
            while pos < len(value) and value[pos] != quote:
                char = value[pos]
                if char == "\\" and quote == '"':
                    pos += 1
                    if pos >= len(value) or value[pos] not in ('"', "\\", "n", "r", "t", "$"):
                        raise ValueError("Invalid environment syntax")
                    char = {"n": "\n", "r": "\r", "t": "\t"}.get(value[pos], value[pos])
                chars.append(char)
                pos += 1
            if pos >= len(value) or (value[pos + 1:].strip() and not value[pos + 1:].strip().startswith("#")):
                raise ValueError("Invalid environment syntax")
            value = "".join(chars)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if any(c in value for c in ('"', "'", "\x00")):
                raise ValueError("Invalid environment syntax")
        if key in ENV_KEYS:
            result[key] = value
    return result


MAX_DATA = 1024 * 1024
PROFILE_NAMES = {
    "machine": "Anycubic Kobra S1 0.4 nozzle",
    "process": "0.20mm Standard @Anycubic Kobra S1 0.4 nozzle",
    "filament": "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle",
}


def read_text(path):
    """Bounded regular-file read, refusing final symlinks/FIFOs/devices."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Invalid data file")
        data = stream.read(MAX_DATA + 1)
        if len(data) > MAX_DATA:
            raise ValueError("Data limit exceeded")
    return data.decode("utf-8")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate data key")
        result[key] = value
    return result


def load_yaml(path):
    """Optional safe parser: absence never falls back to a guessed grammar."""
    import yaml
    class StrictLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise ValueError("YAML aliases unsupported")
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise ValueError("Invalid YAML keys")
                result[key] = self.construct_object(value_node, deep=deep)
            return result
    try:
        data = yaml.load(read_text(path), Loader=StrictLoader)
    except yaml.YAMLError:
        raise ValueError("Invalid YAML configuration") from None
    if not isinstance(data, dict):
        raise ValueError("Invalid YAML configuration")
    return data


def literal_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in ("$", "`", "\x00", "\n", "\r")):
        raise ValueError("Invalid literal path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Absolute literal path required")
    return path


def probe(arguments, timeout, cwd=None, capture=False, allow_no_match=False):
    # No shell. Never npm/Hermes/FreeCAD/Orca runtime invocation. Disable Node
    # preload flags and Git optional index locking/config-based external hooks.
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith("GIT_") and k not in ("NODE_OPTIONS", "NODE_PATH")}
    environment.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1",
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    completed = subprocess.run(arguments, cwd=cwd, env=environment, timeout=timeout,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
    if allow_no_match and completed.returncode == 1 and not completed.stdout:
        return ""
    if completed.returncode:
        raise ValueError("Read-only command failed")
    return completed.stdout.decode("ascii").strip() if capture else ""


def check_config(report, profile, home, values):
    try:
        if profile.is_symlink():
            raise ValueError("Invalid profile directory")
        config = load_yaml(profile / "config.yaml")
    except ImportError:
        report.add("NOTVERIFIED", "model-provider", "safe YAML parser unavailable; credentials not read")
        report.add("NOTVERIFIED", "mcp-registration", "safe YAML parser unavailable")
        return
    except (OSError, ValueError, TypeError, RecursionError):
        report.add("FAIL", "model-provider", "selected configuration missing or invalid")
        report.add("FAIL", "mcp-registration", "selected configuration missing or invalid")
        return
    try:
        model_config = config
        if "model" not in config and home != profile:
            model_config = load_yaml(home / "config.yaml")
        model = model_config.get("model")
        valid = isinstance(model, dict) and all(isinstance(model.get(k), str) and model[k].strip()
                                                 for k in ("default", "provider"))
        report.add("PASS" if valid else "FAIL", "model-provider",
                   "model and provider configured; credentials not read or tested" if valid else "model/provider structure incomplete")
    except ImportError:
        report.add("NOTVERIFIED", "model-provider", "safe YAML parser unavailable")
    except (OSError, ValueError, TypeError, RecursionError):
        report.add("FAIL", "model-provider", "model/provider configuration unavailable or invalid")
    try:
        kobra = config.get("mcp_servers", {}).get("kobra", {})
        expected = str(literal_path(values.get("KLIPPER_MCP_PATH")) / "dist/index.js")
        valid = (isinstance(kobra, dict) and "url" not in kobra and kobra.get("enabled", True) is True
                 and kobra.get("command") in ("node", shutil.which("node"))
                 and kobra.get("args") in ([expected], ["${KLIPPER_MCP_PATH}/dist/index.js"])
                 and isinstance(kobra.get("env"), dict)
                 and kobra["env"].get("PRINTER_KOBRA") in (values.get("KOBRA_S1_MOONRAKER_URL"), "${KOBRA_S1_MOONRAKER_URL}"))
        report.add("PASS" if valid else "FAIL", "mcp-registration", "static registration valid; not a handshake" if valid else "static registration incomplete or mismatched")
    except (ValueError, TypeError, AttributeError):
        report.add("FAIL", "mcp-registration", "static registration incomplete or mismatched")


def load_profile(path, kind, root, seen=None):
    seen = set() if seen is None else seen
    if path.name.lower() == "auth.json" or path.name.startswith(".") or path.suffix != ".json":
        raise ValueError("Invalid profile data path")
    identity = str(path.absolute())
    if identity in seen or len(seen) >= 16:
        raise ValueError("Invalid profile inheritance")
    seen.add(identity)
    data = json.loads(read_text(path), object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise ValueError("Invalid profile structure")
    parent = data.get("inherits")
    if "inherits" in data and not isinstance(parent, str):
        raise ValueError("Invalid profile reference")
    if parent:
        if parent.lower() in ("auth.json", "config.yaml", "config.yml") or parent.startswith(".") or any(c in parent for c in ("/", "\\", "\x00")):
            raise ValueError("Invalid profile reference")
        name = parent if parent.endswith(".json") else parent + ".json"
        candidate = path.parent / name
        if not candidate.is_file():
            candidate = root / kind / name
        inherited = load_profile(candidate, kind, root, seen)
        inherited.update(data)
        data = inherited
    return data


def numbers(value, low, high):
    items = value if isinstance(value, list) else [value]
    if not items or any(type(x) not in (str, int, float) for x in items):
        return False
    try:
        return all(math.isfinite(float(x)) and low <= float(x) <= high for x in items)
    except (TypeError, ValueError, OverflowError):
        return False


def check_profiles(report, values):
    for kind, name in PROFILE_NAMES.items():
        try:
            project = literal_path(values.get("PROJECT_DIR"))
            root = literal_path(values.get("ORCA_PROFILE_ROOT"))
            override = values.get("KOBRA_" + kind.upper() + "_PROFILE")
            path = literal_path(override) if override else project / ("Filamente" if kind == "filament" else "Druckprofile") / (name + ".json")
            data = load_profile(path, kind, root)
            valid = data.get("type") == kind and data.get("name") == name
            compatible = data.get("compatible_printers")
            if compatible is not None:
                valid = valid and isinstance(compatible, list) and all(isinstance(x, str) for x in compatible) and (not compatible or PROFILE_NAMES["machine"] in compatible)
            if kind == "machine":
                valid = (valid and numbers(data.get("nozzle_diameter"), .4, .4)
                         and numbers(data.get("printable_height"), 200, 300)
                         and isinstance(data.get("machine_start_gcode"), str)
                         and "T[initial_tool]" in data["machine_start_gcode"].splitlines()
                         and data.get("support_multi_bed_types") == "1" and data.get("default_bed_type") == "4")
            elif kind == "process":
                valid = valid and numbers(data.get("layer_height"), .05, .4)
            else:
                valid = (valid and numbers(data.get("filament_diameter"), 1.7, 1.8)
                         and numbers(data.get("nozzle_temperature"), 180, 260)
                         and data.get("bed_type") == ["Textured PEI Plate"])
            report.add("PASS" if valid else "FAIL", "profile-" + kind,
                       "expected profile complete and plausible; not a slice test" if valid else "profile fields or compatibility invalid")
        except (OSError, ValueError, TypeError, RecursionError):
            report.add("FAIL", "profile-" + kind, "profile missing, malformed or inheritance unresolved")


def local_checks(report, profile, home, source, timeout, os_release=Path("/etc/os-release")):
    """Read selected files; only execute trusted node --version and Git checks."""
    profile, home, source = Path(profile), Path(home), Path(source)
    try:
        release = {}
        # /etc/os-release is normally a distro-owned symlink to /usr/lib.
        # Only this fixed OS-identity read follows that link; user data does not.
        for line in read_text(Path(os_release).resolve()).splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in ("ID", "VERSION_ID"):
                    release[key] = value.strip('"\'')
        distro, version = release.get("ID"), release.get("VERSION_ID", "")
        valid_version = bool(re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){0,2}", version))
        if distro == "debian" and version == "13":
            report.add("PASS", "os", "Debian 13 (v0.2.0 baseline; v0.3.0 Clean-VM not run)")
        elif distro in ("debian", "ubuntu") and valid_version:
            report.add("WARN", "os", "%s %s: not validated or newly supported" % (distro.capitalize(), version))
        else:
            report.add("WARN", "os", "release identity unrecognized; not validated")
    except (OSError, ValueError):
        report.add("NOTVERIFIED", "os", "release identity unavailable")
    architecture = platform.machine()
    if architecture == "x86_64":
        report.add("PASS", "architecture", "x86_64 (approved Orca checksum anchor; not a new VM validation)")
    elif architecture == "aarch64":
        report.add("WARN", "architecture", "aarch64 (automatic Orca installation/reuse blocked; no approved additional anchor)")
    else:
        report.add("WARN", "architecture", "CPU architecture not validated")
    values = {}
    try:
        if profile.is_symlink():
            raise ValueError("Invalid profile directory")
        values = parse_env(read_text(profile / ".env"))
        for key in ("PROJECT_DIR", "KLIPPER_MCP_PATH", "FREECADCMD_PATH", "ORCA_SLICER_PATH", "ORCA_PROFILE_ROOT"):
            literal_path(values.get(key))
        if not values.get("KOBRA_S1_MOONRAKER_URL"):
            raise ValueError("Missing endpoint")
        report.add("PASS", "environment", "selected dotenv syntax and required keys valid; no evaluation")
    except (OSError, ValueError, TypeError):
        values = {}
        report.add("FAIL", "environment", "selected dotenv missing, malformed or required keys invalid")
    report.add("PASS" if shutil.which("hermes") else "FAIL", "hermes", "launcher present; not executed" if shutil.which("hermes") else "launcher missing")
    check_config(report, profile, home, values)
    node = shutil.which("node")
    try:
        if not node:
            raise ValueError("Missing Node")
        version = probe([node, "--version"], timeout, capture=True)
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version) or int(version[1:].split(".")[0]) < 20:
            raise ValueError("Invalid Node version")
        report.add("PASS", "node", version + " (requires >=20)")
    except (OSError, ValueError, subprocess.SubprocessError):
        report.add("FAIL", "node", "missing, unavailable or unsupported version; requires >=20")
    report.add("PASS" if shutil.which("npm") else "FAIL", "npm", "command present; not executed" if shutil.which("npm") else "command missing")
    try:
        mcp = literal_path(values.get("KLIPPER_MCP_PATH"))
        present = mcp.is_dir() and (mcp / ".git").exists()
    except (ValueError, OSError):
        mcp, present = None, False
    report.add("PASS" if present else "FAIL", "mcp-directory", "checkout present" if present else "checkout missing")
    git = shutil.which("git")
    try:
        if not present or not git:
            raise ValueError("Checkout unavailable")
        commit = probe([git, "rev-parse", "--verify", "HEAD^{commit}"], timeout, cwd=mcp, capture=True)
        if commit != EXPECTED_COMMIT:
            raise ValueError("Unexpected commit")
        report.add("PASS", "mcp-commit", EXPECTED_COMMIT)
    except (OSError, ValueError, subprocess.SubprocessError):
        report.add("FAIL", "mcp-commit", "full pinned commit missing or mismatched")
    try:
        patch = source / "patches/klippermcp-kobra-upload.patch"
        if not present or not git or not patch.is_file():
            raise ValueError("Patch unavailable")
        # --check is not write-free with Git clean/process filters. Effective
        # config includes local include files; global/system config is disabled
        # by probe. Capture names only and never include them in diagnostics.
        filters = probe([git, "config", "--null", "--name-only", "--get-regexp",
                         r"^filter\..*\.(clean|smudge|process)$"],
                        timeout, cwd=mcp, capture=True, allow_no_match=True)
        if filters:
            raise ValueError("Git conversion filters configured")
        probe([git, "apply", "--reverse", "--check", str(patch.absolute())], timeout, cwd=mcp)
        report.add("PASS", "mcp-patch", "expected upload patch present (reverse check only)")
    except (OSError, ValueError, subprocess.SubprocessError):
        report.add("FAIL", "mcp-patch", "expected patch missing, Git filter preflight unsafe or reverse check failed")
    built = mcp is not None and (mcp / "dist/index.js").is_file()
    report.add("PASS" if built else "FAIL", "mcp-build", "build entry present; not a handshake" if built else "build entry missing")
    try:
        cad = literal_path(values.get("FREECADCMD_PATH"))
        if not cad.is_file() or not os.access(cad, os.X_OK):
            raise ValueError("Missing FreeCAD")
        report.add("PASS", "freecad", "command present; not executed")
    except (OSError, ValueError):
        report.add("FAIL", "freecad", "configured command missing or not executable")
    try:
        spec = importlib.util.spec_from_file_location("doctor_orca_verify", Path(__file__).with_name("verify_orca.py"))
        if spec is None or spec.loader is None:
            raise ImportError("Checksum helper unavailable")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        if architecture != "x86_64":
            report.add("NOTVERIFIED", "orca-checksum", "no approved checksum anchor for this architecture")
        else:
            manifest = source / "checksums/orcaslicer-2.4.2.json"
            read_text(manifest)  # bounded regular-file preflight; reject final symlinks before helper reread
            artifact = literal_path(values.get("ORCA_SLICER_PATH"))
            if artifact.name.lower() in ("auth.json", "config.yaml", "config.yml", "providers.yaml", "provider.yaml") or artifact.name.lower().startswith(".env"):
                raise ValueError("Invalid artifact data path")
            entry = helper.load_entry(manifest, ORCA_VERSION, architecture)
            helper.verify_file(artifact, entry)
            report.add("PASS", "orca-checksum", "OrcaSlicer 2.4.2 pinned SHA256/SHA512 match; not executed or upstream-signed")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
        report.add("FAIL", "orca-checksum", "artifact or approved checksum manifest unavailable, invalid or mismatched")
    check_profiles(report, values)
    return values


OBJECT_FIELDS = {
    "mmu": ("gate_status", "slicer_tool_map", "tool_to_gate_map", "ttg_map", "enabled", "num_gates"),
    "mmu_machine": ("num_gates", "vendor", "model", "units", "num_units", "unit_0", "unit_1"),
}
NETWORK_LABELS = ("moonraker-server", "klippy-connected", "klippy-state", "printer-model",
                  "mmu-status", "mmu-enabled", "ace-pro", "gate-count", "tool-gate-mapping")


def validate_url(value):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Invalid endpoint")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.path not in ("", "/")):
        raise ValueError("Invalid endpoint")
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split(".")):
            raise ValueError("Invalid endpoint") from None
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid endpoint")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Moonraker:
    """Only fixed GET endpoints and explicit small object-field queries."""
    def __init__(self, base, timeout):
        self.base = validate_url(base)
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("Invalid timeout")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, path, objects=()):
        if path not in ("/server/info", "/printer/info", "/printer/objects/list", "/printer/objects/query"):
            raise ValueError("Endpoint not allowed")
        query = ""
        if path == "/printer/objects/query":
            if not objects or any(obj not in OBJECT_FIELDS for obj in objects):
                raise ValueError("Object not allowed")
            query = "?" + urllib.parse.urlencode([(obj, ",".join(OBJECT_FIELDS[obj])) for obj in objects])
        elif objects:
            raise ValueError("Unexpected query")
        request = urllib.request.Request(self.base + path + query, method="GET", headers={"Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_DATA + 1)
                if len(body) > MAX_DATA:
                    raise ValueError("Response limit exceeded")
            data = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
            if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
                raise ValueError("Invalid response structure")
            return data["result"]
        except urllib.error.HTTPError as error:
            error.close()  # never read/print arbitrary error bodies or redirects
            raise ValueError("HTTP request denied or unsuccessful") from None
        except (urllib.error.URLError, OSError, ValueError, TypeError, RecursionError):
            raise ValueError("Request unavailable or response invalid") from None


def analyze_mmu(report, objects, status):
    # Ignore extra objects/fields even if the server returns them unsolicited.
    selected = {obj: {field: status[obj][field] for field in OBJECT_FIELDS[obj] if field in status[obj]}
                for obj in OBJECT_FIELDS if obj in objects and isinstance(status.get(obj), dict)}
    mmu, machine = selected.get("mmu", {}), selected.get("mmu_machine", {})
    present = bool(mmu or machine)
    enabled_warning = "enabled" in mmu and mmu["enabled"] is not True
    report.add("WARN" if enabled_warning else "PASS" if present else "NOTVERIFIED", "mmu-status",
               "MMU reports disabled or invalid enabled flag" if enabled_warning else
               "requested MMU objects returned targeted status" if present else "MMU not verified by requested objects and targeted status")
    report.add("PASS" if mmu.get("enabled") is True else "WARN" if "enabled" in mmu else "NOTVERIFIED", "mmu-enabled",
               "enabled=true reported" if mmu.get("enabled") is True else
               "MMU disabled or enabled flag invalid" if "enabled" in mmu else "explicit enabled flag unavailable")
    identities = [machine]
    units = machine.get("units")
    if isinstance(units, list) and len(units) <= 16:
        identities.extend(unit for unit in units if isinstance(unit, dict))
    elif isinstance(units, dict) and len(units) <= 16:
        identities.extend(unit for unit in units.values() if isinstance(unit, dict))
    ace = any(isinstance(identity.get(field), str) and identity[field].strip().lower() in ("ace pro", "anycubic ace pro")
              for identity in identities for field in ("vendor", "model"))
    other_model = any(isinstance(identity.get("model"), str) and bool(identity["model"].strip()) for identity in identities)
    conflicting_model = any(isinstance(identity.get("model"), str) and identity["model"].strip()
                            and identity["model"].strip().lower() not in ("ace pro", "anycubic ace pro") for identity in identities)
    ace = ace and not conflicting_model
    report.add("PASS" if ace else "WARN" if other_model else "NOTVERIFIED", "ace-pro",
               "explicit ACE Pro identity reported; not physical inspection" if ace else "explicit MMU model is not recognized as ACE Pro" if other_model else "exact ACE model identity not exposed by API")
    gates = mmu.get("gate_status")
    counts = []
    valid_gates = (isinstance(gates, list) and 0 < len(gates) <= 64
                   and all(type(x) is int and -1 <= x <= 2 for x in gates))
    if valid_gates and isinstance(gates, list):
        counts.append(len(gates))  # count slots, NOT occupied gates
    if type(machine.get("num_gates")) is int and 0 < machine["num_gates"] <= 64:
        counts.append(machine["num_gates"])
    invalid_count = ("num_gates" in machine and
                     not (type(machine["num_gates"]) is int and 0 < machine["num_gates"] <= 64))
    # Empty arrays can mean unavailable occupancy. Other malformed/oversized
    # supplied arrays must not be hidden by a second, apparently valid count.
    if "gate_status" in mmu and gates != [] and not valid_gates:
        invalid_count = True
    if "num_gates" in mmu:
        if type(mmu["num_gates"]) is int and 0 < mmu["num_gates"] <= 64:
            counts.append(mmu["num_gates"])
        else:
            invalid_count = True
    if "num_units" in machine or any(key in machine for key in ("unit_0", "unit_1")):
        number = machine.get("num_units")
        if type(number) is not int or not 0 <= number <= 2:
            invalid_count = True
        else:
            unit_counts = []
            for index in range(number):
                unit = machine.get("unit_" + str(index))
                count = unit.get("num_gates") if isinstance(unit, dict) else None
                if isinstance(unit, dict) and type(count) is int and 0 < count <= 64:
                    if "first_gate" in unit and (type(unit["first_gate"]) is not int
                                                 or unit["first_gate"] != sum(unit_counts)):
                        invalid_count = True
                    unit_counts.append(count)
                else:
                    invalid_count = True
            if len(unit_counts) == number:
                counts.append(sum(unit_counts))
            # The observed inactive unit_1 is zero-filled, not a second ACE.
            for index in range(number, 2):
                key = "unit_" + str(index)
                if key in machine:
                    unit = machine[key]
                    if (not isinstance(unit, dict) or type(unit.get("num_gates")) is not int
                            or unit["num_gates"] != 0):
                        invalid_count = True
    count_ok = bool(counts) and not invalid_count and all(n == 4 for n in counts)
    report.add("PASS" if count_ok else "WARN" if counts or invalid_count else "NOTVERIFIED", "gate-count",
               "four gate slots reported; occupancy is not hardware presence" if count_ok else "reported gate count differs, conflicts or is invalid" if counts or invalid_count else "gate count unavailable")
    maps = [mmu[key] for key in ("tool_to_gate_map", "ttg_map") if key in mmu]
    if maps:
        valid = all(isinstance(mapping, list) and len(mapping) == 4 and all(type(x) is int for x in mapping)
                    and mapping == [0, 1, 2, 3] for mapping in maps)
        report.add("PASS" if valid else "WARN", "tool-gate-mapping",
                   "explicit API map T0/T1/T2/T3 to gates 0/1/2/3; physical routing not tested" if valid else "explicit tool/gate map malformed, conflicting or differs from expected order")
    else:
        slicer = mmu.get("slicer_tool_map")
        tools = slicer.get("tools") if isinstance(slicer, dict) else None
        plausible = isinstance(tools, list) and len(tools) == 4 and all(isinstance(tool, dict) and tool.get("name") == "T" + str(i) for i, tool in enumerate(tools))
        report.add("NOTVERIFIED", "tool-gate-mapping",
                   "T0/T1/T2/T3 name order plausible; no explicit gate mapping or physical proof" if plausible else "explicit tool/gate mapping unavailable; physical routing not tested")


def network_checks(report, url, offline, timeout):
    try:
        client = Moonraker(url, timeout)
    except (ValueError, TypeError):
        report.add("FAIL", "moonraker-endpoint", "base URL or timeout invalid; no requests")
        return
    if offline:
        for label in NETWORK_LABELS:
            report.add("NOTVERIFIED", label, "offline; network check skipped")
        return
    extension_mmu = False
    try:
        server = client.get("/server/info")
        components, failed = server.get("components"), server.get("failed_components")
        # The observed mmu_ace Moonraker extension injects these two virtual
        # status objects without registering them in Klippy's objects/list.
        # Its declaration authorizes a bounded query, never an identity PASS.
        extension_mmu = (isinstance(components, list) and all(isinstance(x, str) for x in components)
                         and "mmu_ace" in components and isinstance(failed, list)
                         and all(isinstance(x, str) for x in failed) and "mmu_ace" not in failed
                         and server.get("klippy_connected") is True and server.get("klippy_state") == "ready")
        report.add("PASS", "moonraker-server", "server info reachable and valid")
        connected = server.get("klippy_connected")
        report.add("PASS" if connected is True else "FAIL" if connected is False else "NOTVERIFIED", "klippy-connected",
                   "connected" if connected is True else "not connected" if connected is False else "connection flag unavailable")
        state = server.get("klippy_state")
        report.add("PASS" if state == "ready" else "FAIL" if state in ("error", "shutdown", "disconnected", "startup") else "NOTVERIFIED", "klippy-state",
                   "ready" if state == "ready" else "not ready" if state in ("error", "shutdown", "disconnected", "startup") else "state unavailable or unrecognized")
    except ValueError:
        report.add("FAIL", "moonraker-server", "GET failed, timed out or returned invalid data; no repairs")
        report.add("NOTVERIFIED", "klippy-connected", "server info unavailable")
        report.add("NOTVERIFIED", "klippy-state", "server info unavailable")
    try:
        info = client.get("/printer/info")
        models = [info[key] for key in ("model", "printer_model", "device_type") if isinstance(info.get(key), str)]
        recognized = bool(models) and all(model.strip().lower() in ("anycubic kobra s1", "kobra s1") for model in models)
        report.add("PASS" if recognized else "WARN" if models else "NOTVERIFIED", "printer-model",
                   "explicit Kobra S1 model reported" if recognized else "explicit model not recognized as Kobra S1" if models else "explicit model unavailable; hostname is not model proof")
    except ValueError:
        report.add("FAIL", "printer-model", "printer info GET unavailable or invalid")
    objects, status = [], {}
    try:
        listed = client.get("/printer/objects/list").get("objects")
        if not isinstance(listed, list) or not all(isinstance(x, str) for x in listed):
            raise ValueError("Invalid object list")
        objects = [obj for obj in OBJECT_FIELDS if obj in listed or extension_mmu]
        if objects:
            status = client.get("/printer/objects/query", objects).get("status")
            if not isinstance(status, dict):
                raise ValueError("Invalid object status")
    except ValueError:
        report.add("FAIL", "moonraker-objects", "targeted object GET unavailable or invalid")
        objects, status = [], {}
    analyze_mmu(report, objects, status)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes arbitrary CLI values (possibly secrets).
        self.exit(2, "FAIL arguments: invalid or missing arguments; use --help\n")


def main(argv=None):
    parser = SafeParser(prog="doctor.py", description=__doc__, allow_abbrev=False)
    parser.add_argument("--profile-dir", required=True, type=Path, help="Selected profile directory (only its dotenv is read)")
    parser.add_argument("--hermes-home", required=True, type=Path, help="Main Hermes home; config.yaml model fallback only")
    parser.add_argument("--source-dir", required=True, type=Path, help="Trusted distribution clone holding patches and checksum pins")
    parser.add_argument("--offline", action="store_true", help="Skip every HTTP check; exit 1 if local checks pass")
    parser.add_argument("--timeout", type=float, default=3.0, help="Per-command/request timeout in seconds (0 < timeout <= 30)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 30:
        parser.error("Invalid timeout")
    report = Report()
    try:
        values = local_checks(report, args.profile_dir, args.hermes_home, args.source_dir, args.timeout)
        network_checks(report, values.get("KOBRA_S1_MOONRAKER_URL"), args.offline, args.timeout)
    except (Exception, KeyboardInterrupt):
        # A malformed optional field must never leak a traceback, path or value.
        report.add("FAIL", "doctor", "diagnostic could not complete; no repairs attempted")
    print(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
