#!/usr/bin/env python3
"""Read-only local credential evidence, never a server/authentication check.

The only output is a status enum; no Hermes imports, token refresh or setup.
"""
import base64
import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

API_ENV = {
    "openrouter": ("OPENROUTER_API_KEY",),
    # Native resolver chooses explicit OAuth variables before the API key.
    "anthropic": ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "openai-api": ("OPENAI_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


def usable_secret(value, provider=None):
    if not isinstance(value, str):
        return False
    value = value.strip()
    lowered = value.lower()
    if any(c.isspace() or c in "$`\\\"'<>" or ord(c) < 32 for c in value):
        return False
    if len(value) < 4 or lowered in {
        "changeme", "your_api_key", "your_api_key_here", "your-api-key",
        "placeholder", "example", "dummy", "null", "none", "auto",
    }:
        return False
    if (re.search(r"(?:^|[-_])x+$", lowered) or
            re.search(r"(?:^|[-_])(?:your[-_]|placeholder|changeme|example|dummy)(?:$|[-_])?", lowered)):
        return False
    if provider == "openrouter":
        return value.startswith("sk-or-") and len(value) > len("sk-or-")
    return True


def dotenv_values(text):
    """Decode only single-line literal assignments, without shell/interpolation."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise ValueError
        name, value = match.groups()
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and
                           not value[end + 1:].strip().startswith("#")):
                raise ValueError
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[name] = value
    return values


def timestamp(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value > 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def oauth_status(row):
    if not isinstance(row, dict):
        raise ValueError
    for field in ("access_token", "refresh_token"):
        if row.get(field) is not None and not isinstance(row[field], str):
            raise ValueError
    token = row.get("access_token")
    if usable_secret(token):
        expiry = timestamp(row.get("expires_at"))
        raw_ms = row.get("expires_at_ms")
        if raw_ms is not None:
            millis = timestamp(raw_ms)
            ms_expiry = millis / 1000 if millis is not None else None
            expiry = min(expiry, ms_expiry) if expiry and ms_expiry else ms_expiry
        # JWT claims are local expiry evidence, not signature/authentication proof.
        # Future store metadata must never revive an expired or unreadable JWT.
        if isinstance(token, str) and "." in token:
            try:
                if token.count(".") != 2:
                    return "unknown"
                payload = token.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                claims = json.loads(base64.b64decode(payload, altchars=b"-_", validate=True),
                                    object_pairs_hook=unique_mapping)
                jwt_expiry = claims.get("exp") if isinstance(claims, dict) else None
                if (isinstance(jwt_expiry, bool) or not isinstance(jwt_expiry, (int, float))
                        or not math.isfinite(jwt_expiry)):
                    return "unknown"
                expiry = min(expiry, jwt_expiry) if expiry is not None else jwt_expiry
            except (ValueError, UnicodeError, OverflowError):
                return "unknown"
        if expiry is not None and expiry > time.time():
            return "local_credentials_present"
        if usable_secret(row.get("refresh_token")):
            return "refresh_needed"
        return "unknown" if expiry is None else "missing"
    return "refresh_needed" if usable_secret(row.get("refresh_token")) else "missing"


def blocked(row, model, provider):
    """Conservative: do not resurrect dead/exhausted credentials or cooldowns."""
    if not isinstance(row, dict) or row.get("provider", provider) != provider:
        raise ValueError
    status = row.get("last_status")
    if status not in (None, "ok", "dead", "quarantined", "exhausted"):
        raise ValueError
    quarantine = row.get("quarantined", False)
    if not isinstance(quarantine, bool):
        raise ValueError
    cooldowns = row.get("model_cooldowns")
    if cooldowns is None:
        cooldowns = {}
    if not isinstance(cooldowns, dict):
        raise ValueError
    for key, until in cooldowns.items():
        if not isinstance(key, str) or timestamp(until) is None:
            raise ValueError
    return (status in ("dead", "quarantined", "exhausted") or quarantine or
            any(until > time.time() for key, until in cooldowns.items()
                if key in (model, model.split("/", 1)[-1])))


def row_status(row, provider, model):
    if blocked(row, model, provider):
        return "missing"
    auth_type = row.get("auth_type")
    token = row.get("access_token")
    if provider == "anthropic" and isinstance(token, str) and token.startswith("sk-ant-oat"):
        auth_type = "oauth"
    if auth_type == "oauth" and provider in ("openai-codex", "anthropic", "nous"):
        status = oauth_status(row)
        # A Portal management token is not an inference grant. Do not infer
        # Nous invoke-JWT scope/signature usability in this bounded probe.
        return "unknown" if provider == "nous" and status == "local_credentials_present" else status
    if auth_type == "api_key" and provider in API_ENV:
        return "local_credentials_present" if usable_secret(token, provider) else "missing"
    return "unknown"


def combine(statuses):
    for status in ("local_credentials_present", "unknown", "refresh_needed"):
        if status in statuses:
            return status
    return "missing"


def read_inputs(home):
    """Open beneath an explicit home, never follow symlinks (including ancestors).

    Directory-relative descriptors close the check/open race. Refuse devices,
    FIFOs, hardlinks, non-owner files, writable settings and non-private secrets.
    """
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", directory_flags)
    try:
        for part in Path(os.path.abspath(home)).parts[1:]:
            child = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        directory = os.fstat(fd)
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o022:
            raise ValueError
        texts = {}
        for name in ("config.yaml", ".env", "auth.json"):
            try:
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=fd)
            except FileNotFoundError:
                texts[name] = None
                continue
            with os.fdopen(file_fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                private = name != "config.yaml"
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                        info.st_uid != os.geteuid() or not info.st_mode & 0o400 or
                        info.st_mode & (0o077 if private else 0o022)):
                    raise ValueError
                raw = handle.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError
                texts[name] = raw.decode("utf-8-sig")
        return texts
    finally:
        os.close(fd)


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError
        result[key] = value
    return result


def config_mapping(text):
    import yaml

    class StrictLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        return unique_mapping(loader.construct_pairs(node))

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    config = yaml.load(text, Loader=StrictLoader)
    if not isinstance(config, dict):
        raise ValueError
    model = config.get("model", {})
    if not isinstance(model, dict):
        raise ValueError
    for field in ("provider", "default", "key_env", "api_key_env"):
        if field in model and model[field] is not None and not isinstance(model[field], str):
            raise ValueError
    return config


def auth_mapping(text):
    if text is None:
        return {}
    store = json.loads(text, object_pairs_hook=unique_mapping)
    if not isinstance(store, dict) or not {"providers", "credential_pool"}.intersection(store):
        raise ValueError
    version = store.get("version")
    if version is not None and (isinstance(version, bool) or version != 1):
        raise ValueError
    providers = store.get("providers", {})
    pools = store.get("credential_pool", {})
    if not isinstance(providers, dict) or not isinstance(pools, dict):
        raise ValueError
    if any(not isinstance(state, dict) for state in providers.values()):
        raise ValueError
    for rows in pools.values():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError
    return store


def probe(home):
    try:
        texts = read_inputs(home)
    except FileNotFoundError:
        return "missing"
    if texts["config.yaml"] is None:
        return "missing"
    config = config_mapping(texts["config.yaml"])
    model = config.get("model", {})
    provider = model.get("provider", "auto")
    configured_providers = config.get("providers", {})
    if not isinstance(configured_providers, dict):
        return "unknown"
    selected_block = configured_providers.get(provider, {})
    if not isinstance(selected_block, dict):
        return "unknown"
    enabled = selected_block.get("enabled", True)
    if not isinstance(enabled, bool):
        return "unknown"
    if not enabled:
        return "missing"
    values = dotenv_values(texts[".env"]) if texts[".env"] is not None else {}
    if any(model.get(field) for field in ("key_cmd", "api_key_cmd", "base_url", "api_key")):
        return "unknown"
    store = auth_mapping(texts["auth.json"])
    providers = store.get("providers", {})
    pools = store.get("credential_pool", {})
    if provider == "auto":
        evidence = any(usable_secret(values.get(var, os.environ.get(var)), route)
                       for route, variables in API_ENV.items() for var in variables)
        # Auto routing is deliberately not inferred: SDK/plugin/other credentials
        # can outrank a key. Presence is uncertainty, never readiness.
        effective = {**os.environ, **values}
        evidence = evidence or any(usable_secret(value) for name, value in effective.items()
                                   if name.endswith(("_API_KEY", "_TOKEN", "_ACCESS_KEY_ID",
                                                     "_SECRET_ACCESS_KEY")))
        return "unknown" if evidence or providers or pools else "missing"
    if provider not in API_ENV and provider not in ("openai-codex", "nous"):
        return "unknown"
    if not (model.get("default") or "").strip():
        return "missing"
    base_env = {"openai": "OPENAI_BASE_URL", "openai-api": "OPENAI_BASE_URL",
                "openrouter": "OPENROUTER_BASE_URL", "anthropic": "ANTHROPIC_BASE_URL",
                "gemini": "GEMINI_BASE_URL", "openai-codex": "HERMES_CODEX_BASE_URL"}.get(provider)
    if base_env and values.get(base_env, os.environ.get(base_env)):
        return "unknown"
    rows = pools.get(provider) or []
    model_name = model["default"]
    blocked_tokens = {row.get("access_token") for row in rows
                      if blocked(row, model_name, provider) and isinstance(row.get("access_token"), str)}
    row_results = ["missing" if row.get("access_token") in blocked_tokens else
                   row_status(row, provider, model_name) for row in rows]
    if provider in ("openai-codex", "nous"):
        if rows:
            return combine(row_results)
        state = providers.get(provider, {})
        if blocked(state, model_name, provider):
            return "missing"
        tokens = state.get("tokens", {}) if provider == "openai-codex" else state
        status = oauth_status(tokens)
        return "unknown" if provider == "nous" and status == "local_credentials_present" else status
    pointer = model.get("key_env") or model.get("api_key_env")
    # Native Anthropic and the terminal OpenRouter resolver do not use these
    # model pointers; registry API-key providers resolve them separately.
    if pointer and provider in ("anthropic", "openrouter"):
        return "unknown"
    if pointer is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pointer):
        return "unknown"
    variables = ((pointer,) if pointer else ()) + API_ENV[provider]
    for variable in variables:
        value = values.get(variable, os.environ.get(variable))
        priority_token = provider == "anthropic" and variable != "ANTHROPIC_API_KEY"
        # The native resolver selects the first nonblank token, not the first
        # provably usable credential. Do not let a lower key mask uncertainty.
        if priority_token and isinstance(value, str) and value.strip() and not usable_secret(value):
            return "unknown"
        if usable_secret(value, provider):
            matches = [row for row in rows if row.get("access_token") == value.strip()]
            if any(blocked(row, model_name, provider) for row in matches):
                if priority_token:
                    return "missing"
                continue
            if provider == "anthropic" and (
                    value.strip().startswith("sk-ant-oat") or
                    (priority_token and not value.strip().startswith("sk-ant-api"))):
                return combine([oauth_status(row) for row in matches]) if matches else "unknown"
            return "local_credentials_present"
    return combine(row_results)


def main():
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--hermes-home":
            status = "unknown"
        else:
            status = probe(Path(sys.argv[2]))
    except Exception:
        status = "unknown"
    print(status)


if __name__ == "__main__":
    main()
