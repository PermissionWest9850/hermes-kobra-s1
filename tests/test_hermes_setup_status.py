"""Offline contract tests: synthetic homes only; never inherit host credentials."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hermes_setup_status.py"


class SetupStatusTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / ".hermes"
        self.home.mkdir()
        self.env = {"HOME": str(self.root), "HERMES_HOME": str(self.home),
                    "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}

    def config(self, provider="auto", model="anthropic/claude-opus-4.6", extra=""):
        (self.home / "config.yaml").write_text(
            f"model:\n  provider: {provider}\n  default: {model}\n" + extra)

    def dotenv(self, text):
        path = self.home / ".env"
        path.write_text(text)
        path.chmod(0o600)

    def auth(self, data):
        path = self.home / "auth.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def probe(self, expected, *, args=None):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT)] +
            (["--hermes-home", str(self.home)] if args is None else args),
            env=self.env, cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, expected + "\n")
        self.assertEqual(result.stderr, "")
        return result

    def test_explicit_api_key_present(self):
        for provider, variable, value in (
            ("openrouter", "OPENROUTER_API_KEY", "sk-or-v1-syntheticEvidence42"),
            ("anthropic", "ANTHROPIC_API_KEY", "sk-ant-api03-syntheticEvidence42"),
            ("openai", "OPENAI_API_KEY", "sk-proj-syntheticEvidence42"),
            ("openai-api", "OPENAI_API_KEY", "sk-proj-syntheticEvidence42"),
            ("gemini", "GEMINI_API_KEY", "AIzaSyntheticEvidence42"),
        ):
            with self.subTest(provider=provider):
                self.config(provider)
                self.dotenv(f"{variable}={value}\n")
                self.probe("local_credentials_present")

    def test_explicit_key_missing(self):
        self.config("openrouter")
        self.probe("missing")

    def test_unrelated_key_cannot_satisfy_selected_provider(self):
        self.config("anthropic")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.probe("missing")

    def test_api_key_placeholders_missing(self):
        self.config("openrouter")
        for value in ("", "changeme", "YOUR_API_KEY_HERE", "sk-or-xxx",
                      "sk-or-v1-xxxxxxxx", "sk-xxxxxxxx", "example", "sk-or-"):
            with self.subTest(value=value):
                self.dotenv(f"OPENROUTER_API_KEY={value}\n")
                self.probe("missing")

    def test_reserved_auto_api_key_is_missing(self):
        for provider, variable in (("anthropic", "ANTHROPIC_API_KEY"),
                                   ("openai", "OPENAI_API_KEY"),
                                   ("openai-api", "OPENAI_API_KEY"),
                                   ("gemini", "GEMINI_API_KEY")):
            for source in ("dotenv", "inherited", "pool"):
                with self.subTest(provider=provider, source=source):
                    self.config(provider)
                    self.dotenv("")
                    self.auth({"providers": {}})
                    self.env.pop(variable, None)
                    if source == "dotenv":
                        self.dotenv(f"{variable}=auto\n")
                    elif source == "inherited":
                        self.env[variable] = "auto"
                    else:
                        self.pool(provider, auth_type="api_key", access_token="auto")
                    self.guarded_probe("missing")
                    self.env.pop(variable, None)

    def test_openrouter_wrong_prefix_missing(self):
        self.config("openrouter")
        self.dotenv("OPENROUTER_API_KEY=sk-proj-syntheticEvidence42\n")
        self.probe("missing")

    def test_dotenv_literal_decoding(self):
        self.config("openrouter")
        for line in (
            'export OPENROUTER_API_KEY="sk-or-v1-syntheticEvidence42" # local\n',
            "OPENROUTER_API_KEY='sk-or-v1-syntheticEvidence42'\n",
            "OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42 # local\n",
        ):
            with self.subTest(line=line):
                self.dotenv(line)
                self.probe("local_credentials_present")

    def test_file_values_shadow_inherited_values_including_empty(self):
        self.config("openrouter")
        self.env["OPENROUTER_API_KEY"] = "sk-or-v1-syntheticInherited42"
        for value in ("", "changeme", "sk-proj-wrongProvider42"):
            with self.subTest(value=value):
                self.dotenv(f"OPENROUTER_API_KEY={value}\n")
                self.probe("missing")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticFile42\n")
        self.env["OPENROUTER_API_KEY"] = "changeme"
        self.probe("local_credentials_present")

    def test_inherited_key_supported_if_file_absent(self):
        self.config("anthropic")
        self.env["ANTHROPIC_API_KEY"] = "sk-ant-api03-syntheticEvidence42"
        self.probe("local_credentials_present")

    def test_native_provider_unsupported_key_pointer_unknown(self):
        # Native Anthropic and terminal OpenRouter paths do not consult model
        # pointers (runtime_provider.py / runtime_provider_backends.py).
        for provider in ("anthropic", "openrouter"):
            for pointer in ("key_env", "api_key_env"):
                with self.subTest(provider=provider, pointer=pointer):
                    self.config(provider, extra=f"  {pointer}: LOCAL_KEY\n")
                    self.dotenv("LOCAL_KEY=sk-or-v1-syntheticPointerEvidence42\n")
                    self.guarded_probe("unknown")

    def test_registry_provider_supported_key_pointer_present(self):
        # auth._model_level_key_env is consulted for these registry API routes.
        for provider in ("openai-api", "gemini"):
            for pointer in ("key_env", "api_key_env"):
                with self.subTest(provider=provider, pointer=pointer):
                    self.config(provider, extra=f"  {pointer}: LOCAL_KEY\n")
                    self.dotenv("LOCAL_KEY=syntheticPointerEvidence42\n")
                    self.probe("local_credentials_present")

    def test_dotenv_never_expands_or_executes(self):
        self.config("openrouter")
        self.env["INHERITED"] = "sk-or-v1-syntheticEvidence42"
        for value in ("${INHERITED}", "sk-or-$(touch marker)", "sk-or-`touch marker`",
                      "sk-or-${INHERITED}", "sk-or-v1-synthetic\\nvalue"):
            with self.subTest(value=value):
                self.dotenv(f'OPENROUTER_API_KEY="{value}"\n')
                self.probe("missing")
                self.assertFalse((self.root / "marker").exists())

    def test_malformed_dotenv_unknown(self):
        self.config("openrouter")
        for text in ('OPENROUTER_API_KEY="unterminated\n', "not an assignment\n"):
            self.dotenv(text)
            self.probe("unknown")

    def test_unsupported_routes_unknown(self):
        for provider in ("bedrock", "vertex", "custom", "custom:local", "my-plugin"):
            with self.subTest(provider=provider):
                self.config(provider)
                self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
                self.probe("unknown")

    def test_key_command_and_custom_endpoint_unknown(self):
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        for extra in ("  key_cmd: touch marker\n", "  base_url: https://example.invalid/v1\n",
                      "  api_key: inlineSyntheticCredential42\n"):
            with self.subTest(extra=extra):
                self.config("openrouter", extra=extra)
                self.probe("unknown")
                self.assertFalse((self.root / "marker").exists())

    def test_auto_with_credential_evidence_unknown(self):
        self.config()
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.probe("unknown")

    def oauth(self, *, expires="2999-01-01T00:00:00+00:00", refresh=False):
        tokens = {"access_token": "syntheticOAuthAccessEvidence42"}
        if expires is not None:
            tokens["expires_at"] = expires
        if refresh:
            tokens["refresh_token"] = "syntheticOAuthRefreshEvidence42"
        self.auth({"providers": {"openai-codex": {"tokens": tokens}}})

    def jwt(self, claims):
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJub25lIn0.{payload}.syntheticSignature42"

    def test_codex_jwt_expiry_cannot_be_overridden_by_future_metadata(self):
        self.config("openai-codex")
        for location in ("singleton", "pool"):
            for refresh in (False, True):
                for explicit in ("2999-01-01T00:00:00Z", None):
                    with self.subTest(location=location, refresh=refresh, explicit=explicit):
                        tokens = {"access_token": self.jwt({"exp": 946684800}),
                                  "expires_at": explicit, "expires_at_ms": 32503680000000}
                        if refresh:
                            tokens["refresh_token"] = "syntheticOAuthRefreshEvidence42"
                        if location == "singleton":
                            self.auth({"providers": {"openai-codex": {"tokens": tokens}}})
                        else:
                            self.pool("openai-codex", **tokens)
                        self.probe("refresh_needed" if refresh else "missing")

    def test_codex_invalid_jwt_expiry_unknown_despite_future_metadata(self):
        self.config("openai-codex")
        tokens = [self.jwt(claims) for claims in (
            {}, {"exp": True}, {"exp": "32503680000"}, {"exp": float("inf")},
            {"exp": None}, [], {"exp": float("nan")})]
        tokens += ["eyJhbGciOiJub25lIn0.%%%.syntheticSignature42",
                   "eyJhbGciOiJub25lIn0.not-json.syntheticSignature42"]
        for token in tokens:
            for refresh in (False, True):
                with self.subTest(token=token, refresh=refresh):
                    row = {"access_token": token, "expires_at": "2999-01-01T00:00:00Z"}
                    if refresh:
                        row["refresh_token"] = "syntheticOAuthRefreshEvidence42"
                    self.auth({"providers": {"openai-codex": {"tokens": row}}})
                    self.guarded_probe("unknown")

    def test_codex_jwt_and_explicit_expiry_use_earliest_evidence(self):
        self.config("openai-codex")
        for explicit, millis, expected in (
            (None, None, "local_credentials_present"),
            ("2999-01-01T00:00:00Z", None, "local_credentials_present"),
            ("2000-01-01T00:00:00Z", None, "missing"),
            (None, 946684800000, "missing"),
        ):
            with self.subTest(explicit=explicit, millis=millis):
                self.pool("openai-codex", access_token=self.jwt({"exp": 32503680000}),
                          expires_at=explicit, expires_at_ms=millis)
                self.guarded_probe(expected)

    def test_oauth_unexpired_singleton_present(self):
        self.config("openai-codex", model="gpt-synthetic")
        self.oauth()
        self.probe("local_credentials_present")

    def test_oauth_expired_singleton_missing(self):
        self.config("openai-codex")
        self.oauth(expires="2000-01-01T00:00:00+00:00")
        self.probe("missing")

    def test_oauth_unknown_expiry_unknown(self):
        self.config("openai-codex")
        for expiry in (None, "unparseable", "2999-01-01T00:00:00", True):
            self.oauth(expires=expiry)
            self.probe("unknown")

    def test_oauth_refresh_needed_not_ready(self):
        self.config("openai-codex")
        for expiry in (None, "2000-01-01T00:00:00Z"):
            self.oauth(expires=expiry, refresh=True)
            self.probe("refresh_needed")
        self.auth({"providers": {"openai-codex": {"tokens": {
            "refresh_token": "syntheticOAuthRefreshEvidence42"}}}})
        self.probe("refresh_needed")

    def test_unrelated_oauth_cannot_satisfy_selected_route(self):
        self.config("openrouter")
        self.oauth()
        self.probe("missing")

    def test_oauth_pool_unexpired_present(self):
        self.config("openai-codex")
        self.auth({"credential_pool": {"openai-codex": [{
            "id": "synthetic", "auth_type": "oauth", "source": "manual:device_code",
            "access_token": "syntheticOAuthAccessEvidence42",
            "expires_at": "2999-01-01T00:00:00Z"}]}})
        self.probe("local_credentials_present")

    def test_oauth_pool_expired_with_refresh_needs_refresh(self):
        self.config("openai-codex")
        self.auth({"credential_pool": {"openai-codex": [{
            "auth_type": "oauth", "access_token": "syntheticOAuthAccessEvidence42",
            "refresh_token": "syntheticOAuthRefreshEvidence42",
            "expires_at": "2000-01-01T00:00:00Z"}]}})
        self.probe("refresh_needed")

    def test_auto_with_oauth_evidence_unknown(self):
        self.config()
        self.oauth()
        self.probe("unknown")

    def pool(self, route, **fields):
        row = {"id": "synthetic", "auth_type": "oauth", "source": "manual:device_code",
               "access_token": "syntheticOAuthAccessEvidence42",
               "expires_at": "2999-01-01T00:00:00Z"}
        row.update(fields)
        self.auth({"credential_pool": {route: [row]}})

    def test_pool_blocked_status_never_present(self):
        self.config("openai-codex")
        for status in ("dead", "quarantined", "exhausted"):
            with self.subTest(status=status):
                self.pool("openai-codex", last_status=status,
                          refresh_token="syntheticOAuthRefreshEvidence42")
                self.probe("missing")

    def test_pool_selected_model_cooldown_never_present(self):
        self.config("openai-codex", model="gpt-synthetic")
        self.pool("openai-codex", model_cooldowns={"gpt-synthetic": 32503680000})
        self.probe("missing")

    def test_pool_unrelated_or_elapsed_cooldown_does_not_block(self):
        self.config("openai-codex", model="gpt-synthetic")
        for cooldowns in ({"other": 32503680000}, {"gpt-synthetic": 946684800}):
            self.pool("openai-codex", model_cooldowns=cooldowns)
            self.probe("local_credentials_present")

    def test_blocked_pool_cannot_fall_back_to_singleton(self):
        self.config("openai-codex")
        self.pool("openai-codex", last_status="dead")
        data = json.loads((self.home / "auth.json").read_text())
        data["providers"] = {"openai-codex": {"tokens": {
            "access_token": "syntheticOAuthAccessEvidence42",
            "expires_at": "2999-01-01T00:00:00Z"}}}
        self.auth(data)
        self.probe("missing")

    def test_api_key_pool_present(self):
        self.config("openrouter")
        self.pool("openrouter", auth_type="api_key",
                  access_token="sk-or-v1-syntheticEvidence42")
        self.probe("local_credentials_present")

    def test_api_key_matching_blocked_pool_not_resurrected_by_env(self):
        self.config("openrouter", model="synthetic-model")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.pool("openrouter", auth_type="api_key",
                  access_token="sk-or-v1-syntheticEvidence42", last_status="dead")
        self.probe("missing")

    def test_api_provider_oauth_pool_requires_expiry(self):
        self.config("anthropic")
        self.pool("anthropic", expires_at=None,
                  access_token="sk-ant-oat01-syntheticEvidence42")
        self.probe("unknown")

    def test_anthropic_oauth_env_without_expiry_unknown(self):
        self.config("anthropic")
        self.dotenv("ANTHROPIC_API_KEY=sk-ant-oat01-syntheticEvidence42\n")
        self.probe("unknown")

    def test_anthropic_high_priority_oauth_without_expiry_shadows_api_key(self):
        self.config("anthropic")
        for variable in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            for token in ("sk-ant-oat01-syntheticPriorityEvidence42",
                          self.jwt({}), "cc-syntheticPriorityEvidence42",
                          "syntheticOpaquePriorityEvidence42", "auto"):
                for source in ("dotenv", "inherited"):
                    with self.subTest(variable=variable, source=source, token=token):
                        for name in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
                            self.env.pop(name, None)
                        self.dotenv("ANTHROPIC_API_KEY=sk-ant-api03-syntheticLowerEvidence42\n" +
                                    (f"{variable}={token}\n" if source == "dotenv" else ""))
                        if source == "inherited":
                            self.env[variable] = token
                        self.guarded_probe("unknown")
                        self.env.pop(variable, None)

    def test_anthropic_high_priority_oauth_uses_matching_pool_expiry(self):
        self.config("anthropic")
        token = "sk-ant-oat01-syntheticPriorityEvidence42"
        for variable in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            for expires, refresh, expected in (
                (None, False, "unknown"),
                ("2000-01-01T00:00:00Z", False, "missing"),
                ("2000-01-01T00:00:00Z", True, "refresh_needed"),
                ("2999-01-01T00:00:00Z", False, "local_credentials_present"),
            ):
                with self.subTest(variable=variable, expires=expires, refresh=refresh):
                    self.dotenv("ANTHROPIC_API_KEY=sk-ant-api03-syntheticLowerEvidence42\n" +
                                f"{variable}={token}\n")
                    fields = {"access_token": token, "expires_at": expires}
                    if refresh:
                        fields["refresh_token"] = "syntheticOAuthRefreshEvidence42"
                    self.pool("anthropic", **fields)
                    self.guarded_probe(expected)

    def test_anthropic_blocked_priority_token_does_not_fall_back_to_api_key(self):
        self.config("anthropic")
        for variable in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            with self.subTest(variable=variable):
                token = "sk-ant-oat01-syntheticPriorityEvidence42"
                self.dotenv("ANTHROPIC_API_KEY=sk-ant-api03-syntheticLowerEvidence42\n" +
                            f"{variable}={token}\n")
                self.pool("anthropic", access_token=token, last_status="dead")
                self.guarded_probe("missing")

    def test_anthropic_token_precedes_claude_oauth_token(self):
        self.config("anthropic")
        self.dotenv("ANTHROPIC_API_KEY=sk-ant-api03-syntheticLowerEvidence42\n"
                    "ANTHROPIC_TOKEN=sk-ant-oat01-syntheticPriorityEvidence42\n"
                    "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-syntheticLowerOAuthEvidence42\n")
        self.pool("anthropic", access_token="sk-ant-oat01-syntheticLowerOAuthEvidence42")
        self.guarded_probe("unknown")

    def test_pool_millisecond_expiry_present(self):
        self.config("openai-codex")
        self.pool("openai-codex", expires_at=None, expires_at_ms=32503680000000)
        self.probe("local_credentials_present")

    def test_bad_pool_auth_type_unknown(self):
        self.config("openai-codex")
        for auth_type in ("plugin-oauth", "api_key", None):
            self.pool("openai-codex", auth_type=auth_type)
            self.probe("unknown")

    def test_malformed_pool_metadata_unknown(self):
        self.config("openai-codex")
        for fields in ({"last_status": "surprise"}, {"model_cooldowns": []},
                       {"model_cooldowns": {"synthetic": "tomorrow"}},
                       {"provider": "unrelated"}, {"quarantined": "false"}):
            self.pool("openai-codex", **fields)
            self.probe("unknown")

    def test_quarantine_marker_blocks_singleton(self):
        self.config("openai-codex")
        self.auth({"providers": {"openai-codex": {"quarantined": True,
            "tokens": {"access_token": "syntheticOAuthAccessEvidence42",
                       "expires_at": "2999-01-01T00:00:00Z"}}}})
        self.probe("missing")

    def test_portal_management_token_not_inference_evidence(self):
        self.config("nous")
        self.auth({"providers": {"nous": {
            "access_token": "syntheticPortalManagementToken42",
            "expires_at": "2999-01-01T00:00:00Z"}}})
        self.probe("unknown")

    def guarded_probe(self, expected, *, missing_yaml=False):
        guard = r'''
import builtins, fcntl, os, runpy, socket, sys
attempts = []
original_import = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name.split('.')[0] in ('hermes_cli', 'agent', 'run_agent'):
        attempts.append('runtime import')
        raise RuntimeError('blocked')
    if name == 'yaml' and sys.argv[3] == 'missing':
        raise ImportError('synthetic dependency absence')
    return original_import(name, *args, **kwargs)
builtins.__import__ = checked_import

def deny(*args, **kwargs):
    attempts.append('forbidden operation')
    raise RuntimeError('blocked')
socket.socket = deny
socket.getaddrinfo = deny
fcntl.flock = deny
fcntl.lockf = deny

def audit(event, args):
    if event.startswith(('socket.', 'subprocess.')) or event in (
        'os.system', 'os.mkdir', 'os.remove', 'os.rename', 'os.chmod', 'os.link', 'os.symlink'):
        deny()
    if event == 'open':
        filename, mode, flags = args
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            deny()
        if isinstance(filename, str) and os.path.isabs(filename) and filename.endswith(('.env', 'auth.json', 'config.yaml')):
            if not os.path.abspath(filename).startswith(os.path.abspath(sys.argv[2]) + os.sep):
                deny()
sys.addaudithook(audit)
script, home = sys.argv[1:3]
sys.argv = [script, '--hermes-home', home]
runpy.run_path(script, run_name='__main__')
if attempts:
    raise RuntimeError('forbidden attempt')
'''
        # Capture dependency flag before production main changes argv.
        guard = guard.replace("sys.argv[3] == 'missing'", "missing_yaml")
        guard = guard.replace("attempts = []", f"attempts = []\nmissing_yaml = {missing_yaml!r}")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", guard,
                                 str(SCRIPT), str(self.home)], env=self.env,
                                cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, expected + "\n")
        self.assertEqual(result.stderr, "")

    def test_runtime_network_writes_and_locks_forbidden(self):
        self.config("openrouter")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.auth({"providers": {}})
        self.guarded_probe("local_credentials_present")
        (self.home / "auth.json").write_text("brokenSyntheticSecret42")
        self.guarded_probe("unknown")

    def test_missing_yaml_dependency_unknown(self):
        self.config()
        self.guarded_probe("unknown", missing_yaml=True)

    def test_selected_disabled_provider_never_present(self):
        for provider, variable, credential in (
            ("openrouter", "OPENROUTER_API_KEY", "sk-or-v1-syntheticDisabledEvidence42"),
            ("anthropic", "ANTHROPIC_API_KEY", "sk-ant-api03-syntheticDisabledEvidence42"),
            ("openai", "OPENAI_API_KEY", "sk-syntheticDisabledEvidence42"),
            ("openai-api", "OPENAI_API_KEY", "sk-syntheticDisabledEvidence42"),
            ("gemini", "GEMINI_API_KEY", "AIzaSyntheticDisabledEvidence42"),
            ("openai-codex", None, None),
        ):
            with self.subTest(provider=provider):
                self.config(provider, extra=f"providers:\n  {provider}:\n    enabled: false\n")
                self.dotenv(f"{variable}={credential}\n" if variable else "")
                self.auth({"providers": {}})
                if provider == "openai-codex":
                    self.oauth()
                self.guarded_probe("missing")

    def test_ambiguous_provider_enabled_schema_unknown(self):
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticDisabledEvidence42\n")
        for extra in ("providers: []\n", "providers: null\n",
                      "providers: {openrouter: []}\n", "providers: {openrouter: null}\n",
                      "providers: {openrouter: {enabled: 'false'}}\n",
                      "providers: {openrouter: {enabled: null}}\n",
                      "providers: {openrouter: {enabled: 0}}\n",
                      "providers: {openrouter: {enabled: []}}\n"):
            with self.subTest(extra=extra):
                self.config("openrouter", extra=extra)
                self.guarded_probe("unknown")

    def test_enabled_or_unrelated_disabled_provider_does_not_block(self):
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticDisabledEvidence42\n")
        for extra in ("providers: {openrouter: {enabled: true}}\n",
                      "providers: {openrouter: {}}\n",
                      "providers: {anthropic: {enabled: false}}\n"):
            self.config("openrouter", extra=extra)
            self.guarded_probe("local_credentials_present")

    def test_base_url_environment_override_unknown(self):
        self.config("openai-api")
        self.dotenv("OPENAI_API_KEY=sk-proj-syntheticEvidence42\n"
                    "OPENAI_BASE_URL=https://example.invalid/v1\n")
        self.probe("unknown")

    def test_auto_other_credential_evidence_unknown(self):
        self.config()
        self.dotenv("XAI_API_KEY=syntheticEvidence42\n")
        self.probe("unknown")

    def test_model_whitespace_missing(self):
        self.config("openrouter", model="'   '")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.probe("missing")

    def test_malformed_singleton_token_schema_unknown(self):
        self.config("openai-codex")
        for tokens in (None, [], {"access_token": 42, "expires_at": "2999-01-01T00:00:00Z"}):
            self.auth({"providers": {"openai-codex": {"tokens": tokens}}})
            self.probe("unknown")

    def test_blocked_duplicate_token_cannot_be_resurrected(self):
        self.config("openai-codex")
        self.pool("openai-codex", last_status="dead")
        data = json.loads((self.home / "auth.json").read_text())
        row = dict(data["credential_pool"]["openai-codex"][0], id="twin", last_status="ok")
        data["credential_pool"]["openai-codex"].append(row)
        self.auth(data)
        self.probe("missing")

    def test_provider_prefixed_placeholders_missing(self):
        self.config("openrouter")
        for value in ("sk-or-v1-your-key-here", "sk-or-v1-placeholder",
                      "sk-or-v1-changeme", "sk-or-v1-<your-key>", "sk-or-v1-example"):
            self.dotenv(f"OPENROUTER_API_KEY={value}\n")
            self.probe("missing")

    def test_nous_refresh_material_needs_refresh_not_ready(self):
        self.config("nous")
        for state in ({"refresh_token": "syntheticOAuthRefreshEvidence42"},
                      {"access_token": "syntheticPortalToken42",
                       "expires_at": "2000-01-01T00:00:00Z",
                       "refresh_token": "syntheticOAuthRefreshEvidence42"}):
            self.auth({"providers": {"nous": state}})
            self.probe("refresh_needed")

    def test_nous_selected_without_credentials_missing(self):
        self.config("nous")
        self.probe("missing")

    def test_symlinked_inputs_unknown(self):
        self.config("openrouter")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.auth({"providers": {}})
        for name in ("config.yaml", ".env", "auth.json"):
            with self.subTest(name=name):
                source = self.home / name
                target = self.root / ("synthetic-" + name)
                source.rename(target)
                source.symlink_to(target)
                self.probe("unknown")
                source.unlink()
                target.rename(source)

    def test_symlinked_home_and_ancestor_unknown(self):
        self.config("openrouter")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        link = self.root / "link"
        link.symlink_to(self.home, target_is_directory=True)
        self.probe("unknown", args=["--hermes-home", str(link)])
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(self.root, target_is_directory=True)
        self.probe("unknown", args=["--hermes-home", str(parent_link / ".hermes")])

    def test_private_file_permissions_unknown(self):
        self.config("openrouter")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.auth({"providers": {}})
        for name in (".env", "auth.json"):
            for mode in (0o644, 0o660, 0o666, 0o000):
                with self.subTest(name=name, mode=mode):
                    path = self.home / name
                    path.chmod(mode)
                    self.probe("unknown")
                    path.chmod(0o600)

    def test_hardlinked_private_file_unknown(self):
        self.config("openrouter")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        os.link(self.home / ".env", self.root / "hardlink")
        self.probe("unknown")

    def test_malformed_config_schema_unknown(self):
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        for text in ("[]\n", "model: []\n", "model: {provider: openrouter, default: 123}\n",
                     "model: {provider: openrouter, default: [a]}\n",
                     "model: {provider: openrouter, default: m, provider: anthropic}\n",
                     "model: [\n", "model: {provider: openrouter, default: m, key_env: 42}\n"):
            with self.subTest(text=text):
                (self.home / "config.yaml").write_text(text)
                self.probe("unknown")

    def test_malformed_auth_schema_unknown_even_with_api_key(self):
        self.config("openrouter")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        for data in ([], {"providers": []}, {"providers": {"openrouter": []}},
                     {"credential_pool": []}, {"credential_pool": {"openrouter": {}}},
                     {"credential_pool": {"openrouter": [None]}},
                     {"version": "unknown", "providers": {}}, {"surprise": True}):
            with self.subTest(data=data):
                self.auth(data)
                self.probe("unknown")

    def test_duplicate_json_keys_unknown(self):
        self.config("openai-codex")
        self.auth({"providers": {}})
        (self.home / "auth.json").write_text('{"providers":{},"providers":{}}')
        self.probe("unknown")

    def test_repeat_probes_preserve_bytes_mode_mtime_and_tree(self):
        self.config("openrouter")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.auth({"providers": {}})
        for corrupt in (False, True):
            if corrupt:
                (self.home / "auth.json").write_text('{invalidSyntheticSensitive42')
            paths = sorted(self.home.rglob("*"))
            before = {p.relative_to(self.home): (p.read_bytes(), p.stat().st_mode,
                      p.stat().st_mtime_ns) for p in paths if p.is_file()}
            for _ in range(3):
                self.probe("unknown" if corrupt else "local_credentials_present")
            after = {p.relative_to(self.home): (p.read_bytes(), p.stat().st_mode,
                     p.stat().st_mtime_ns) for p in self.home.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(paths, sorted(self.home.rglob("*")))

    def test_cli_bad_arguments_only_unknown(self):
        for args in ([], ["--help"], ["--hermes-home"],
                     ["--hermes-home", str(self.home), "extra"]):
            self.probe("unknown", args=args)

    def test_selected_model_required(self):
        self.config("openrouter", model="''")
        self.dotenv("OPENROUTER_API_KEY=sk-or-v1-syntheticEvidence42\n")
        self.probe("missing")

    def test_fresh_auto_defaults_missing(self):
        self.config()
        self.probe("missing")

    def test_absent_home_missing_without_creation(self):
        target = self.root / "absent"
        self.probe("missing", args=["--hermes-home", str(target)])
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
