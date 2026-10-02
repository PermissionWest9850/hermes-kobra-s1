#!/usr/bin/env bash
set -Eeuo pipefail

# Hermes + Anycubic Kobra S1 one-command installer.
# Safe by design: this script never installs/modifies Rinkhals, never changes
# printer firmware/network settings, never uploads GCode and never starts prints.

REPO_URL="${KOBRA_REPO_URL:-https://github.com/PermissionWest9850/hermes-kobra-s1.git}"
REPO_ARCHIVE_URL="${KOBRA_REPO_ARCHIVE_URL:-https://github.com/PermissionWest9850/hermes-kobra-s1/archive/refs/heads/main.tar.gz}"
KLIPPERMCP_REPO="${KLIPPERMCP_REPO:-https://github.com/mikehatch/KlipperMCP.git}"
KLIPPERMCP_COMMIT="425e169"
ORCA_VERSION="2.4.2"
PROFILE_NAME="kobra-s1-3d-print"
DEFAULT_PROJECT_DIR="$HOME/3d-print"
DEFAULT_KLIPPER_DIR="$HOME/KlipperMCP-kobra-s1"
DEFAULT_PROFILE_DIR="${HERMES_HOME:-$HOME/.hermes}/profiles/$PROFILE_NAME"
ORCA_INSTALL_DIR_USER_SET="${ORCA_INSTALL_DIR+x}"
ORCA_INSTALL_DIR="${ORCA_INSTALL_DIR:-$HOME/.local/opt/orcaslicer}"
ORCA_APPIMAGE="$ORCA_INSTALL_DIR/OrcaSlicer.AppImage"
ORCA_EXTRACT_DIR="$ORCA_INSTALL_DIR/squashfs-root"
OPT_ORCA_APPIMAGE="/opt/orcaslicer/OrcaSlicer.AppImage"
OPT_ORCA_PROFILE_ROOT="/opt/orcaslicer/squashfs-root/resources/profiles/Anycubic"
DRY_RUN=0
ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help)
      cat <<'HELP'
Hermes Kobra S1 installer

Usage:
  bash install.sh [--dry-run] [--yes]

Environment overrides:
  KOBRA_S1_MOONRAKER_URL  Printer Moonraker URL or bare IP
  PROJECT_DIR             Workspace directory (default: ~/3d-print)
  KLIPPER_MCP_PATH        KlipperMCP directory (default: ~/KlipperMCP-kobra-s1)
  HERMES_HOME             Hermes home (default: ~/.hermes)
  ORCA_INSTALL_DIR        User-local OrcaSlicer directory (default: ~/.local/opt/orcaslicer)
HELP
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ "${KOBRA_INSTALL_DRY_RUN:-0}" == "1" ]]; then
  DRY_RUN=1
fi

STEP=0
step() {
  STEP=$((STEP + 1))
  printf '\n[%d/9] %s\n' "$STEP" "$1"
}

info() { printf '  - %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN: %q' "$1"
    shift || true
    for a in "$@"; do printf ' %q' "$a"; done
    printf '\n'
  else
    "$@"
  fi
}
need_cmd() { command -v "$1" >/dev/null 2>&1; }

ROOT_METHOD=""
ROOT_BOOTSTRAP_ACTIVE=0
KOBRA_PROFILE_CONFIG_LOADED=0

shell_quote() {
  local s="$1"
  s=${s//\'/\'\\\'\'}
  printf "'%s'" "$s"
}

format_shell_command() {
  local out="" q=""
  for arg in "$@"; do
    q="$(shell_quote "$arg")"
    out="${out:+$out }$q"
  done
  printf '%s' "$out"
}

configure_root_access() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    fail "Do not run this installer as root. Run it as your normal user so Hermes, KlipperMCP, the profile, .env and kobra3d are installed in that user's home directory."
  elif need_cmd sudo; then
    ROOT_METHOD="sudo"
    if [[ "$DRY_RUN" != "1" ]]; then
      sudo -v || fail "sudo authentication failed."
    fi
  elif need_cmd su; then
    ROOT_METHOD="su"
    cat >&2 <<'EOF_SU'
This minimal Debian installation does not include sudo.
System packages need root access.
You will be asked for the Debian root password.
Hermes itself will still be installed only for your normal user.

No root password will be stored, logged, put in a variable, or written to the repository.
EOF_SU
    if [[ "$DRY_RUN" != "1" ]] && ! has_tty; then
      fail "su needs an interactive terminal for the root password. Rerun this installer from a terminal session."
    fi
  else
    fail "Root access is required for system packages, but neither sudo nor su is available. Install one of them or use a Debian image that includes su/sudo."
  fi
}

root_run() {
  if [[ "$ROOT_BOOTSTRAP_ACTIVE" != "1" ]]; then
    fail "Internal safety stop: root command requested outside the single privileged bootstrap."
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY-RUN: root %s\n' "$(format_shell_command "$@")"
    return 0
  fi
  case "$ROOT_METHOD" in
    sudo) sudo "$@" ;;
    su) su -c "$(format_shell_command "$@")" ;;
    *) fail "Internal error: root access method is not configured." ;;
  esac
}

check_internet_dns() {
  if getent hosts github.com >/dev/null 2>&1 || getent hosts deb.debian.org >/dev/null 2>&1; then
    info "Internet/DNS check passed."
  else
    fail "Internet/DNS check failed before installing requirements. Check VM networking and DNS, then rerun."
  fi
}

bootstrap_system_packages() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: one privileged bootstrap installs required apt packages, FreeCAD, FUSE support and Node.js >=20 if needed"
    return 0
  fi

  local bootstrap_script
  bootstrap_script="$(mktemp)"
  cat >"$bootstrap_script" <<'EOF_BOOTSTRAP'
#!/usr/bin/env bash
set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update

fuse_pkg="libfuse2"
if ! apt-cache show libfuse2 >/dev/null 2>&1; then
  fuse_pkg="libfuse2t64"
fi

apt-get install -y ca-certificates curl git python3 python3-venv xz-utils unzip tar jq gnupg freecad nodejs npm "$fuse_pkg"
EOF_BOOTSTRAP
  chmod 700 "$bootstrap_script"
  ROOT_BOOTSTRAP_ACTIVE=1
  if ! root_run bash "$bootstrap_script"; then
    ROOT_BOOTSTRAP_ACTIVE=0
    rm -f "$bootstrap_script"
    fail "Privileged system bootstrap failed. See apt output above."
  fi
  ROOT_BOOTSTRAP_ACTIVE=0
  rm -f "$bootstrap_script"
  if [[ "$ROOT_METHOD" == "sudo" && "$DRY_RUN" != "1" ]]; then
    sudo -k
  fi
}

version_ge() {
  # usage: version_ge 20.0.0 18.19.0 -> true if first <= second
  printf '%s\n%s\n' "$1" "$2" | sort -V -C
}

prompt() {
  local var_name="$1" prompt_text="$2" default_value="${3:-}" value=""
  if [[ -n "${!var_name:-}" ]]; then
    printf -v "$var_name" '%s' "${!var_name}"
    return 0
  fi
  if [[ "$ASSUME_YES" == "1" && -n "$default_value" ]]; then
    printf -v "$var_name" '%s' "$default_value"
    return 0
  fi
  if [[ -c /dev/tty ]] && { : </dev/tty; } 2>/dev/null; then
    if [[ -n "$default_value" ]]; then
      printf '%s [%s]: ' "$prompt_text" "$default_value" >/dev/tty
    else
      printf '%s: ' "$prompt_text" >/dev/tty
    fi
    IFS= read -r value </dev/tty || true
  else
    if [[ -n "$default_value" ]]; then
      value="$default_value"
      warn "No TTY available; using default for $var_name: $default_value"
    else
      fail "No TTY available for required input $var_name. Set it as an environment variable."
    fi
  fi
  value="${value:-$default_value}"
  printf -v "$var_name" '%s' "$value"
}

normalize_moonraker_url() {
  local input="$1"
  input="${input# }"; input="${input% }"
  [[ -n "$input" ]] || fail "Moonraker address is empty."
  if [[ "$input" =~ ^https?:// ]]; then
    printf '%s\n' "$input"
  else
    input="${input%/}"
    if [[ "$input" == *:* ]]; then
      printf 'http://%s\n' "$input"
    else
      printf 'http://%s:7125\n' "$input"
    fi
  fi
}

dotenv_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/}"
  printf '"%s"' "$value"
}

dotenv_get() {
  local env_file="$1" key="$2"
  python3 - "$env_file" "$key" <<'PY'
from pathlib import Path
import shlex
import sys

env_file, wanted_key = sys.argv[1:]
for raw in Path(env_file).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() != wanted_key:
        continue
    value = value.strip()
    if value:
        try:
            parts = shlex.split(value, posix=True)
        except ValueError:
            parts = []
        if parts:
            print(parts[0])
        else:
            print(value)
    sys.exit(0)
sys.exit(1)
PY
}

has_tty() {
  [[ -c /dev/tty ]] && { : </dev/tty; } 2>/dev/null
}

script_dir=""
work_src=""
cleanup_dir=""
resolve_source_tree() {
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$script_dir/SOUL.md" && -f "$script_dir/config.yaml" && -d "$script_dir/scripts" ]]; then
    work_src="$script_dir"
    return 0
  fi
  cleanup_dir="$(mktemp -d)"
  info "Downloading profile repository archive..."
  if [[ "$DRY_RUN" == "1" ]]; then
    work_src="$cleanup_dir/hermes-kobra-s1-main"
    mkdir -p "$work_src/scripts" "$work_src/patches"
    touch "$work_src/SOUL.md" "$work_src/config.yaml" "$work_src/distribution.yaml" "$work_src/.env.EXAMPLE"
  else
    curl -fsSL "$REPO_ARCHIVE_URL" -o "$cleanup_dir/repo.tar.gz"
    tar -xzf "$cleanup_dir/repo.tar.gz" -C "$cleanup_dir"
    work_src="$(find "$cleanup_dir" -maxdepth 1 -type d -name 'hermes-kobra-s1-*' | head -n 1)"
    [[ -n "$work_src" && -f "$work_src/SOUL.md" ]] || fail "Downloaded repository archive does not look valid."
  fi
}

trap '[[ -n "${cleanup_dir:-}" && -d "$cleanup_dir" ]] && rm -rf "$cleanup_dir"' EXIT

install_hermes_if_needed() {
  if need_cmd hermes; then
    info "Hermes found: $(command -v hermes)"
    return 0
  fi
  info "Hermes not found; installing via official installer."
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
    return 0
  else
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
    export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
    if [[ -f "$HOME/.bashrc" ]]; then
      # shellcheck disable=SC1090
      source "$HOME/.bashrc" || true
    fi
  fi
  need_cmd hermes || fail "Hermes was installed but is not on PATH. Try opening a new shell, then rerun this installer."
}

hermes_is_configured() {
  [[ "$DRY_RUN" == "1" ]] && return 0
  local provider="" model=""
  provider="$(hermes config get model.provider 2>/dev/null || true)"
  model="$(hermes config get model.default 2>/dev/null || true)"
  [[ -n "$provider" && -n "$model" ]] || return 1
  return 0
}

onboard_hermes_auth() {
  if hermes_is_configured; then
    info "Existing Hermes configuration found; keeping current provider/model."
    return 0
  fi
  cat <<'EOF_SETUP'
  - Hermes is installed but does not appear to have a model/provider configured yet.
  - No credentials or API keys are copied from any other profile.
  - Configure only your own Hermes/Nous/API access on this machine.
EOF_SETUP
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: hermes setup --portal"
    return 0
  fi
  if has_tty; then
    info "Starting official Hermes Portal setup (OAuth) for this user."
    if hermes setup --portal; then
      return 0
    fi
    warn "Portal setup did not complete. Starting the official quick setup wizard instead."
    hermes setup --quick || fail "Hermes setup did not complete. Run 'hermes setup --portal' or 'hermes setup --quick' and then rerun this installer."
  else
    cat >&2 <<'EOF_SETUP_NO_TTY'
ERROR: Hermes needs first-time setup, but this shell has no interactive TTY.
Run one of these commands as the target user, then rerun install.sh:

  hermes setup --portal

or, for the general setup wizard:

  hermes setup --quick

EOF_SETUP_NO_TTY
    exit 1
  fi
}

install_node_if_needed() {
  local node_version=""
  if need_cmd node; then
    node_version="$(node -v | sed 's/^v//')"
  fi
  if [[ -n "$node_version" ]] && version_ge "20.0.0" "$node_version" && need_cmd npm; then
    info "Node.js $(node -v) and npm $(npm -v) found."
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: verify Node.js >=20 and npm after privileged bootstrap"
    return 0
  fi
  fail "Node.js >=20 and npm are still missing after the privileged bootstrap. Check apt output above."
}

install_klippermcp() {
  KLIPPER_MCP_PATH="${KLIPPER_MCP_PATH:-$DEFAULT_KLIPPER_DIR}"
  if [[ -e "$KLIPPER_MCP_PATH" && ! -d "$KLIPPER_MCP_PATH/.git" ]]; then
    fail "KLIPPER_MCP_PATH exists but is not a git checkout: $KLIPPER_MCP_PATH"
  fi
  if [[ ! -d "$KLIPPER_MCP_PATH/.git" ]]; then
    info "Cloning KlipperMCP to $KLIPPER_MCP_PATH"
    run git clone "$KLIPPERMCP_REPO" "$KLIPPER_MCP_PATH"
    if [[ "$DRY_RUN" != "1" ]]; then
      git -C "$KLIPPER_MCP_PATH" fetch --tags --quiet origin
      git -C "$KLIPPER_MCP_PATH" checkout --quiet "$KLIPPERMCP_COMMIT"
    else
      echo "DRY-RUN: git -C $KLIPPER_MCP_PATH fetch && checkout $KLIPPERMCP_COMMIT"
    fi
  else
    info "Existing KlipperMCP checkout found: $KLIPPER_MCP_PATH"
    if [[ "$DRY_RUN" != "1" ]]; then
      local current_commit
      current_commit="$(git -C "$KLIPPER_MCP_PATH" rev-parse --short HEAD)"
      if [[ "$current_commit" != "$KLIPPERMCP_COMMIT" ]]; then
        fail "Existing KlipperMCP checkout is at $current_commit, expected $KLIPPERMCP_COMMIT. Refusing to change an existing installation silently. Choose an empty KLIPPER_MCP_PATH or update it manually."
      fi
    else
      echo "DRY-RUN: verify existing KlipperMCP is at $KLIPPERMCP_COMMIT"
    fi
  fi

  local patch_file="$work_src/patches/klippermcp-kobra-upload.patch"
  [[ -f "$patch_file" || "$DRY_RUN" == "1" ]] || fail "Patch not found: $patch_file"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: verify/apply KlipperMCP patch idempotently"
  elif git -C "$KLIPPER_MCP_PATH" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
    info "KlipperMCP patch already applied."
  elif git -C "$KLIPPER_MCP_PATH" apply --check "$patch_file" >/dev/null 2>&1; then
    git -C "$KLIPPER_MCP_PATH" apply "$patch_file"
    info "KlipperMCP patch applied."
  else
    fail "KlipperMCP patch does not apply cleanly and is not already applied. Refusing to modify it."
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: npm install && npm run build in $KLIPPER_MCP_PATH"
  else
    npm --prefix "$KLIPPER_MCP_PATH" install
    npm --prefix "$KLIPPER_MCP_PATH" run build
    [[ -f "$KLIPPER_MCP_PATH/dist/index.js" ]] || fail "KlipperMCP build did not produce dist/index.js."
  fi
}

install_freecad() {
  if need_cmd freecadcmd; then
    FREECADCMD_PATH="$(command -v freecadcmd)"
    info "FreeCAD found: $FREECADCMD_PATH"
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    FREECADCMD_PATH="/usr/bin/freecadcmd"
    echo "DRY-RUN: verify FreeCAD after privileged bootstrap"
    return 0
  fi
  fail "FreeCAD command 'freecadcmd' is still missing after the privileged bootstrap. Check apt output above."
}

select_orca_asset() {
  local arch="$1" metadata_file="$2"
  local arch_pattern=""
  case "$arch" in
    x86_64|amd64)
      # v2.4.2 publishes Ubuntu-versioned x86_64 AppImages, e.g.
      # OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage.
      arch_pattern='Linux_AppImage(?:_Ubuntu[0-9]+)?_V'
      ;;
    aarch64|arm64)
      # v2.4.2 publishes OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.4.2.AppImage.
      arch_pattern='Linux_AppImage.*_aarch64_V'
      ;;
    *) fail "Unsupported architecture for automatic OrcaSlicer install: $arch" ;;
  esac

  python3 - "$metadata_file" "$ORCA_VERSION" "$arch_pattern" <<'PY'
import json
import re
import sys

metadata_path, version, arch_pattern = sys.argv[1:]
release = json.load(open(metadata_path, encoding="utf-8"))
pattern = re.compile(rf"^OrcaSlicer_{arch_pattern}{re.escape(version)}\.AppImage$")
matches = []
for asset in release.get("assets", []):
    name = asset.get("name") or ""
    url = asset.get("browser_download_url") or ""
    if pattern.match(name) and url:
        matches.append((name, url))

if len(matches) != 1:
    print(f"ERROR: expected exactly one OrcaSlicer AppImage asset, found {len(matches)}", file=sys.stderr)
    if matches:
        for name, _url in matches:
            print(f"- {name}", file=sys.stderr)
    else:
        print("Available AppImage assets:", file=sys.stderr)
        for asset in release.get("assets", []):
            name = asset.get("name") or ""
            if "AppImage" in name:
                print(f"- {name}", file=sys.stderr)
    sys.exit(1)

print(matches[0][0])
print(matches[0][1])
PY
}

install_orcaslicer() {
  if [[ -z "$ORCA_INSTALL_DIR_USER_SET" && -z "${ORCA_SLICER_PATH:-}" && -z "${ORCA_PROFILE_ROOT:-}" && -x "$OPT_ORCA_APPIMAGE" && -d "$OPT_ORCA_PROFILE_ROOT" ]]; then
    ORCA_SLICER_PATH="$OPT_ORCA_APPIMAGE"
    ORCA_PROFILE_ROOT="$OPT_ORCA_PROFILE_ROOT"
    info "Existing system-wide OrcaSlicer found; using it read-only: $ORCA_SLICER_PATH"
    return 0
  fi
  ORCA_SLICER_PATH="${ORCA_SLICER_PATH:-$ORCA_APPIMAGE}"
  ORCA_PROFILE_ROOT="${ORCA_PROFILE_ROOT:-$ORCA_EXTRACT_DIR/resources/profiles/Anycubic}"
  if [[ -x "$ORCA_SLICER_PATH" && -d "$ORCA_PROFILE_ROOT" ]]; then
    info "OrcaSlicer found: $ORCA_SLICER_PATH"
    return 0
  fi
  case "$ORCA_SLICER_PATH" in
    /opt/*|/usr/*|/bin/*|/sbin/*)
      fail "Refusing to install or overwrite OrcaSlicer in system path '$ORCA_SLICER_PATH'. Existing system-wide OrcaSlicer may be used read-only, but new installs default to user-local '$ORCA_APPIMAGE'. Set ORCA_INSTALL_DIR to another user-writable path if needed."
      ;;
  esac
  local arch asset_url tmpdir asset_name metadata_file selection
  arch="$(uname -m)"
  info "Installing OrcaSlicer ${ORCA_VERSION} from official SoftFever GitHub release."
  tmpdir="$(mktemp -d)"
  metadata_file="$tmpdir/orca-release.json"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: query https://api.github.com/repos/SoftFever/OrcaSlicer/releases/tags/v${ORCA_VERSION} and select exactly one AppImage for $arch"
    case "$arch" in
      x86_64|amd64)
        asset_name="OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage"
        ;;
      aarch64|arm64)
        asset_name="OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V${ORCA_VERSION}.AppImage"
        ;;
      *) fail "Unsupported architecture for automatic OrcaSlicer install: $arch" ;;
    esac
    asset_url="https://github.com/SoftFever/OrcaSlicer/releases/download/v${ORCA_VERSION}/${asset_name}"
  else
    curl -fsSL "https://api.github.com/repos/SoftFever/OrcaSlicer/releases/tags/v${ORCA_VERSION}" -o "$metadata_file"
    selection="$(select_orca_asset "$arch" "$metadata_file")"
    asset_name="$(printf '%s\n' "$selection" | sed -n '1p')"
    asset_url="$(printf '%s\n' "$selection" | sed -n '2p')"
    [[ -n "$asset_name" && -n "$asset_url" ]] || fail "Could not determine OrcaSlicer download asset."
  fi
  info "Selected OrcaSlicer asset: $asset_name"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: download $asset_url to $ORCA_SLICER_PATH and extract profiles to $ORCA_EXTRACT_DIR"
    rm -rf "$tmpdir"
    return 0
  fi
  curl -fL "$asset_url" -o "$tmpdir/OrcaSlicer.AppImage"
  # If upstream checksum assets are present in the future, this block verifies them.
  if curl -fsSL "https://github.com/SoftFever/OrcaSlicer/releases/download/v${ORCA_VERSION}/SHA256SUMS" -o "$tmpdir/SHA256SUMS"; then
    if grep -F " $asset_name" "$tmpdir/SHA256SUMS" >"$tmpdir/SHA256SUMS.one"; then
      (cd "$tmpdir" && mv OrcaSlicer.AppImage "$asset_name" && sha256sum -c SHA256SUMS.one && mv "$asset_name" OrcaSlicer.AppImage)
    else
      warn "SHA256SUMS did not contain $asset_name; continuing with TLS-protected GitHub download only."
    fi
  else
    warn "No upstream SHA256SUMS asset found for OrcaSlicer ${ORCA_VERSION}; continuing with TLS-protected GitHub download only."
  fi
  mkdir -p "$(dirname "$ORCA_SLICER_PATH")"
  install -m 0755 "$tmpdir/OrcaSlicer.AppImage" "$ORCA_SLICER_PATH"
  rm -rf "$tmpdir"
  rm -rf "$ORCA_EXTRACT_DIR"
  mkdir -p "$ORCA_EXTRACT_DIR"
  (cd "$(dirname "$ORCA_EXTRACT_DIR")" && "$ORCA_SLICER_PATH" --appimage-extract >/dev/null)
  [[ -d "$ORCA_PROFILE_ROOT" ]] || fail "OrcaSlicer profile root not found after extraction: $ORCA_PROFILE_ROOT"
}

install_profile_files() {
  local profile_dir="${PROFILE_DIR:-$DEFAULT_PROFILE_DIR}"
  local env_file="$profile_dir/.env"
  info "Installing Hermes profile to $profile_dir"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: create/update profile files in $profile_dir without overwriting .env"
    return 0
  fi
  mkdir -p "$profile_dir"
  cp -a "$work_src/SOUL.md" "$work_src/config.yaml" "$work_src/distribution.yaml" "$profile_dir/"
  rm -rf "$profile_dir/scripts" "$profile_dir/patches"
  cp -a "$work_src/scripts" "$work_src/patches" "$profile_dir/"
  cp -a "$work_src/.env.EXAMPLE" "$profile_dir/.env.EXAMPLE"

  if [[ -f "$env_file" ]]; then
    if [[ "$KOBRA_PROFILE_CONFIG_LOADED" != "1" ]]; then
      info "Existing Kobra profile configuration found; keeping .env unchanged."
    fi
  else
    cat >"$env_file" <<EOF_ENV
KOBRA_S1_MOONRAKER_URL=$(dotenv_quote "$KOBRA_S1_MOONRAKER_URL")
KLIPPER_MCP_PATH=$(dotenv_quote "$KLIPPER_MCP_PATH")
PROJECT_DIR=$(dotenv_quote "$PROJECT_DIR")
FREECADCMD_PATH=$(dotenv_quote "$FREECADCMD_PATH")
ORCA_SLICER_PATH=$(dotenv_quote "$ORCA_SLICER_PATH")
ORCA_PROFILE_ROOT=$(dotenv_quote "$ORCA_PROFILE_ROOT")
KOBRA_MACHINE_PROFILE=$(dotenv_quote "$PROJECT_DIR/Druckprofile/Anycubic Kobra S1 0.4 nozzle.json")
KOBRA_PROCESS_PROFILE=$(dotenv_quote "$PROJECT_DIR/Druckprofile/0.20mm Standard @Anycubic Kobra S1 0.4 nozzle.json")
KOBRA_FILAMENT_PROFILE=$(dotenv_quote "$PROJECT_DIR/Filamente/Anycubic PLA @Anycubic Kobra S1 0.4 nozzle.json")
EOF_ENV
    chmod 600 "$env_file"
  fi
}

load_existing_kobra_profile_config() {
  local profile_dir="${PROFILE_DIR:-$DEFAULT_PROFILE_DIR}"
  local env_file="$profile_dir/.env"
  local env_project="" env_moonraker=""

  [[ -f "$env_file" ]] || return 1

  env_project="$(dotenv_get "$env_file" PROJECT_DIR 2>/dev/null || true)"
  env_moonraker="$(dotenv_get "$env_file" KOBRA_S1_MOONRAKER_URL 2>/dev/null || true)"

  if [[ -z "$env_project" || -z "$env_moonraker" ]]; then
    fail "Existing Kobra profile .env is incomplete. Refusing to overwrite it. Missing PROJECT_DIR or KOBRA_S1_MOONRAKER_URL in $env_file"
  fi

  PROJECT_DIR="$env_project"
  KOBRA_S1_MOONRAKER_URL="$(normalize_moonraker_url "$env_moonraker")"
  KOBRA_PROFILE_CONFIG_LOADED=1
  info "Existing Kobra profile configuration found; keeping .env unchanged."
  return 0
}

create_project_dir() {
  PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_PROJECT_DIR}"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: mkdir -p $PROJECT_DIR/{CAD,Druckhistorie,Druckprofile,Filamente,Kobra-S1,Modelle/Downloads}"
    return 0
  fi
  mkdir -p "$PROJECT_DIR"/{CAD,Druckhistorie,Druckprofile,Filamente,Kobra-S1,Modelle/Downloads}
}

prepare_orca_profiles() {
  local script="$work_src/scripts/prepare_orca_profiles.py"
  local expected_profiles=(
    "$PROJECT_DIR/Druckprofile/Anycubic Kobra S1 0.4 nozzle.json"
    "$PROJECT_DIR/Druckprofile/0.20mm Standard @Anycubic Kobra S1 0.4 nozzle.json"
    "$PROJECT_DIR/Filamente/Anycubic PLA @Anycubic Kobra S1 0.4 nozzle.json"
  )
  local existing_profiles=()
  local missing_profiles=()
  local path=""
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: PROJECT_DIR=$PROJECT_DIR ORCA_PROFILE_ROOT=$ORCA_PROFILE_ROOT $script"
    return 0
  fi

  for path in "${expected_profiles[@]}"; do
    if [[ -e "$path" ]]; then
      existing_profiles+=("$path")
    else
      missing_profiles+=("$path")
    fi
  done

  if [[ "${#existing_profiles[@]}" -eq "${#expected_profiles[@]}" ]]; then
    info "Existing Orca profiles kept unchanged."
    return 0
  fi

  if [[ "${#existing_profiles[@]}" -gt 0 ]]; then
    warn "Incomplete Orca profile set found; refusing to overwrite or partially regenerate profiles."
    printf 'Existing profile files kept unchanged:\n' >&2
    for path in "${existing_profiles[@]}"; do
      printf -- '- %s\n' "$path" >&2
    done
    printf 'Missing expected profile files:\n' >&2
    for path in "${missing_profiles[@]}"; do
      printf -- '- %s\n' "$path" >&2
    done
    fail "Remove or back up the partial profile set, or restore all three expected profiles, then rerun the installer."
  fi

  PROJECT_DIR="$PROJECT_DIR" ORCA_PROFILE_ROOT="$ORCA_PROFILE_ROOT" python3 "$script"
}

install_wrapper() {
  local bin_dir="$HOME/.local/bin"
  local wrapper="$bin_dir/kobra3d"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY-RUN: install wrapper $wrapper"
    return 0
  fi
  mkdir -p "$bin_dir"
  if [[ -e "$wrapper" ]] && ! grep -q "Hermes Kobra S1 wrapper" "$wrapper" 2>/dev/null; then
    warn "Wrapper $wrapper already exists and was not created/changed."
    return 0
  fi
  cat >"$wrapper" <<'EOF_WRAP'
#!/usr/bin/env bash
# Hermes Kobra S1 wrapper
exec hermes -p kobra-s1-3d-print chat "$@"
EOF_WRAP
  chmod +x "$wrapper"
}

check_moonraker_readonly() {
  while true; do
    info "Testing Moonraker read-only endpoint: $KOBRA_S1_MOONRAKER_URL"
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "DRY-RUN: curl $KOBRA_S1_MOONRAKER_URL/server/info"
      MOONRAKER_STATUS="not tested (dry-run)"
      return 0
    fi
    if curl -fsS --max-time 8 "$KOBRA_S1_MOONRAKER_URL/server/info" >/dev/null; then
      MOONRAKER_STATUS="reachable"
      return 0
    fi

    MOONRAKER_STATUS="not reachable"
    warn "Moonraker is not reachable at $KOBRA_S1_MOONRAKER_URL."
    if ! has_tty; then
      warn "No interactive TTY available; continuing installation. You can fix the URL/network and rerun later."
      return 0
    fi

    local choice="" new_url=""
    cat >/dev/tty <<'EOF_MOONRAKER'
Moonraker could not be reached. Choose one:
  1) Retry the same URL
  2) Enter another IP/URL
  3) Continue installation anyway
EOF_MOONRAKER
    printf 'Selection [1/2/3]: ' >/dev/tty
    IFS= read -r choice </dev/tty || true
    case "${choice:-}" in
      1|r|R|retry|Retry)
        continue
        ;;
      2|u|U|url|URL)
        printf 'New Moonraker IP or URL: ' >/dev/tty
        IFS= read -r new_url </dev/tty || true
        KOBRA_S1_MOONRAKER_URL="$(normalize_moonraker_url "$new_url")"
        continue
        ;;
      3|c|C|continue|Continue|"")
        warn "Continuing without a reachable Moonraker check. No printer changes were made."
        return 0
        ;;
      *)
        warn "Unknown selection. Please choose 1, 2, or 3."
        ;;
    esac
  done
}

final_verify() {
  local hermes_status="missing" klipper_status="missing" freecad_status="missing" orca_status="missing" profile_status="missing"
  need_cmd hermes && hermes_status="ready"
  [[ -f "$KLIPPER_MCP_PATH/dist/index.js" || "$DRY_RUN" == "1" ]] && klipper_status="ready"
  [[ -x "$FREECADCMD_PATH" || "$DRY_RUN" == "1" ]] && freecad_status="ready"
  [[ -x "$ORCA_SLICER_PATH" && -d "$ORCA_PROFILE_ROOT" || "$DRY_RUN" == "1" ]] && orca_status="ready"
  [[ -d "${PROFILE_DIR:-$DEFAULT_PROFILE_DIR}" || "$DRY_RUN" == "1" ]] && profile_status="$PROFILE_NAME"

  cat <<EOF_SUMMARY

Installation complete.

Printer:        Anycubic Kobra S1
Moonraker:      ${MOONRAKER_STATUS:-not tested}
Hermes:         $hermes_status
Hermes profile: $profile_status
KlipperMCP:     $klipper_status
FreeCAD:        $freecad_status
OrcaSlicer:     $orca_status
ACE profile:    4-slot mapping configured (Slot 1 -> T0, Slot 2 -> T1, Slot 3 -> T2, Slot 4 -> T3)
ACE hardware:   not verified by installer
Project dir:    $PROJECT_DIR

Start with:

  hermes -p $PROFILE_NAME chat

Or, if ~/.local/bin is on PATH:

  kobra3d

Safety reminder: this installer did not install or modify Rinkhals, did not change printer firmware/network settings, did not upload GCode and did not start a print.
EOF_SUMMARY
}

main() {
  step "Checking system..."
  case "$(uname -s)" in Linux) ;; *) fail "This installer supports Linux only." ;; esac
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
      debian|ubuntu) info "Detected ${PRETTY_NAME:-$ID}" ;;
      *)
        case " ${ID_LIKE:-} " in *" debian "*|*" ubuntu "*) info "Detected Debian-like system: ${PRETTY_NAME:-$ID}" ;; *) fail "Only Debian/Ubuntu systems are supported." ;; esac
        ;;
    esac
  else
    fail "/etc/os-release not found."
  fi
  info "Architecture: $(uname -m)"
  configure_root_access
  check_internet_dns

  step "Installing requirements..."
  bootstrap_system_packages

  resolve_source_tree

  step "Installing Hermes..."
  install_hermes_if_needed
  onboard_hermes_auth

  step "Installing KlipperMCP..."
  install_node_if_needed
  install_klippermcp

  step "Installing FreeCAD..."
  install_freecad

  step "Installing OrcaSlicer..."
  install_orcaslicer

  step "Configuring Kobra S1 profile..."
  if load_existing_kobra_profile_config; then
    create_project_dir
  else
    prompt PROJECT_DIR "Project directory" "$DEFAULT_PROJECT_DIR"
    create_project_dir
    prompt moonraker_input "Kobra S1 Moonraker URL or IP" "${KOBRA_S1_MOONRAKER_URL:-}"
    KOBRA_S1_MOONRAKER_URL="$(normalize_moonraker_url "$moonraker_input")"
  fi
  install_profile_files
  prepare_orca_profiles
  install_wrapper

  step "Checking Moonraker..."
  check_moonraker_readonly

  step "Final verification..."
  final_verify
}

main "$@"
