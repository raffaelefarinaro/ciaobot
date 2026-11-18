#!/bin/sh
# Ciaobot Linux installer.
#
# Run as the account that will own Ciaobot:
#
#   sh install.sh
#
# It installs this release's `ciaobot` engine for the invoking user with uv,
# runs `ciao setup`, and -- unless --no-service -- installs the systemd service
# through `ciao service install`, which asks for sudo so it can write the unit
# at /etc/systemd/system/ciaobot.service.
#
# Linux only: on macOS, install the Ciaobot app instead --
# https://github.com/raffaelefarinaro/ciaobot/releases/latest
#
# Trust model: downloads are made over TLS with curl. The release wheel's
# sha256 is stamped into this file by the release pipeline and compared before
# anything is installed, so the download is only installed when it is the
# release artifact byte for byte. uv publishes no signature for its release
# archive, so that archive's sha256 is pinned below and verified before it is
# extracted. Root is refused: the engine and its workspace belong to a
# dedicated account, never root's.
set -u

# Script variables, not environment variables: the release pipeline stamps the
# version and the wheel digest into this file, and the test harness rewrites
# these lines in its copy, so a test run never downloads, never writes the
# real unit file and never sees this machine's identity.
UNAME=uname
CURL=curl
SHA256SUM=sha256sum
TAR=tar
ID=id
UNIT_PATH=/etc/systemd/system/ciaobot.service
REPO=raffaelefarinaro/ciaobot
CIAOBOT_VERSION=__CIAOBOT_VERSION__
WHEEL_SHA256=__WHEEL_SHA256__
PYTHON_VERSION=3.13
UV_VERSION=0.13.8
UV_SHA256_X86_64=5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91
UV_SHA256_AARCH64=74f2f5c6024d397481db228db8fc25eed420db192243f88438c118e7e013e585

usage() {
  echo "Usage: sh install.sh [--workspace DIR] [--no-service] [--replace-unit] [--wheel PATH] [-h]"
  echo "  --workspace DIR   set up this workspace (default: $HOME/Ciaobot when no service unit exists yet)"
  echo "  --no-service      skip installing the systemd service unit"
  echo "  --replace-unit    have ciao service install take over a unit that belongs to a source install"
  echo "  --wheel PATH      install this local wheel instead of the release wheel (CI and rehearsal)"
  echo "  -h                show this help"
}

have_workspace=0
workspace_value=
no_service=0
replace_unit=0
wheel_arg=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --workspace)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        usage >&2
        exit 2
      fi
      have_workspace=1
      workspace_value=$2
      shift 2
      ;;
    --workspace=*)
      workspace_value=${1#--workspace=}
      if [ -z "$workspace_value" ]; then
        usage >&2
        exit 2
      fi
      have_workspace=1
      shift
      ;;
    --no-service)
      no_service=1
      shift
      ;;
    --replace-unit)
      replace_unit=1
      shift
      ;;
    --wheel)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        usage >&2
        exit 2
      fi
      wheel_arg=$2
      shift 2
      ;;
    --wheel=*)
      wheel_arg=${1#--wheel=}
      if [ -z "$wheel_arg" ]; then
        usage >&2
        exit 2
      fi
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "install.sh: unknown flag: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

os=$("$UNAME" -s) || {
  echo "install.sh: uname -s failed; this installer supports Linux only" >&2
  exit 1
}
case "$os" in
  Linux) ;;
  Darwin)
    echo "install.sh: On macOS, install the Ciaobot app: https://github.com/$REPO/releases/latest" >&2
    exit 1
    ;;
  *)
    echo "install.sh: this installer supports Linux only, not $os." >&2
    exit 1
    ;;
esac

if [ "$("$ID" -u)" = "0" ]; then
  {
    echo "install.sh: refusing to run as root: Ciaobot must run under its own account."
    echo "Create that account and rerun this installer as it:"
    echo "  useradd -m <user>"
    echo "  sudo -u <user> -H sh install.sh"
  } >&2
  exit 1
fi

case "$CIAOBOT_VERSION" in
  __*__)
    if [ -z "$wheel_arg" ]; then
      echo "install.sh: this is the source copy; use the release asset:" >&2
      echo "  https://github.com/$REPO/releases/latest/download/install.sh" >&2
      exit 1
    fi
    ;;
esac

tmpd=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-install.XXXXXX") || {
  echo "install.sh: could not create the scratch directory" >&2
  exit 1
}
trap 'rm -rf "$tmpd"' EXIT
trap 'exit 1' HUP INT TERM

if command -v uv >/dev/null 2>&1; then
  uv=uv
elif [ -x "$HOME/.local/bin/uv" ]; then
  uv=$HOME/.local/bin/uv
else
  machine=$("$UNAME" -m) || {
    echo "install.sh: uname -m failed" >&2
    exit 1
  }
  case "$machine" in
    x86_64)
      uv_arch=x86_64
      uv_digest=$UV_SHA256_X86_64
      ;;
    aarch64)
      uv_arch=aarch64
      uv_digest=$UV_SHA256_AARCH64
      ;;
    *)
      echo "install.sh: no uv release is pinned for $machine; this installer supports x86_64 and aarch64 Linux." >&2
      exit 1
      ;;
  esac
  uv_archive=uv-$uv_arch-unknown-linux-gnu.tar.gz
  uv_url=https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$uv_archive
  echo "install.sh: downloading uv $UV_VERSION for $uv_arch..."
  "$CURL" -fsSL -o "$tmpd/$uv_archive" "$uv_url" || {
    echo "install.sh: downloading $uv_url failed" >&2
    exit 1
  }
  if ! printf '%s  %s\n' "$uv_digest" "$tmpd/$uv_archive" | "$SHA256SUM" -c - >/dev/null; then
    echo "install.sh: the downloaded $uv_archive does not match the pinned sha256 ($uv_digest); refusing to use it" >&2
    exit 1
  fi
  # The check above runs before tar reads the archive: a tampered archive is
  # refused without being extracted.
  mkdir -p "$tmpd/uv"
  "$TAR" -xzf "$tmpd/$uv_archive" -C "$tmpd/uv" || {
    echo "install.sh: extracting $uv_archive failed" >&2
    exit 1
  }
  mkdir -p "$HOME/.local/bin"
  cp "$tmpd/uv/uv-$uv_arch-unknown-linux-gnu/uv" "$HOME/.local/bin/uv" || {
    echo "install.sh: $uv_archive did not contain a usable uv" >&2
    exit 1
  }
  cp "$tmpd/uv/uv-$uv_arch-unknown-linux-gnu/uvx" "$HOME/.local/bin/uvx" || {
    echo "install.sh: $uv_archive did not contain a usable uvx" >&2
    exit 1
  }
  chmod 755 "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx"
  uv=$HOME/.local/bin/uv
fi

if [ -n "$wheel_arg" ]; then
  wheel=$wheel_arg
  if [ ! -f "$wheel" ]; then
    echo "install.sh: --wheel: no such file: $wheel" >&2
    exit 1
  fi
else
  version=$CIAOBOT_VERSION
  wheel_url=https://github.com/$REPO/releases/download/v$version/ciaobot-$version-py3-none-any.whl
  wheel=$tmpd/ciaobot-$version-py3-none-any.whl
  echo "install.sh: downloading the Ciaobot $version wheel..."
  "$CURL" -fsSL -o "$wheel" "$wheel_url" || {
    echo "install.sh: downloading $wheel_url failed" >&2
    exit 1
  }
  if ! printf '%s  %s\n' "$WHEEL_SHA256" "$wheel" | "$SHA256SUM" -c - >/dev/null; then
    echo "install.sh: the downloaded wheel does not match the sha256 stamped in this installer ($WHEEL_SHA256); refusing to install it" >&2
    exit 1
  fi
fi

echo "install.sh: installing Ciaobot with uv..."
"$uv" tool install --force --python "$PYTHON_VERSION" "$wheel" || {
  echo "install.sh: uv tool install failed" >&2
  exit 1
}
uv_bin_dir=$("$uv" tool dir --bin) || {
  echo "install.sh: uv tool dir --bin failed" >&2
  exit 1
}
ciao_bin=$uv_bin_dir/ciao
if [ ! -x "$ciao_bin" ]; then
  echo "install.sh: ciao was not installed into $uv_bin_dir" >&2
  exit 1
fi
"$ciao_bin" --version || {
  echo "install.sh: the installed ciao does not run" >&2
  exit 1
}

ws=
if [ "$have_workspace" -eq 1 ]; then
  ws=$workspace_value
elif [ ! -e "$UNIT_PATH" ]; then
  ws=$HOME/Ciaobot
fi
if [ -n "$ws" ]; then
  echo "install.sh: ciao setup --workspace $ws"
  "$ciao_bin" setup --workspace "$ws" || {
    echo "install.sh: ciao setup failed; fix the problem and rerun it" >&2
    exit 1
  }
else
  echo "install.sh: keeping the workspace the installed unit records"
fi

if [ "$no_service" -eq 0 ]; then
  set --
  if [ -n "$ws" ]; then
    set -- "$@" --workspace "$ws"
  fi
  if [ "$replace_unit" -eq 1 ]; then
    set -- "$@" --replace
  fi
  if [ -n "$ws" ]; then
    echo "install.sh: ciao service install --workspace $ws"
  else
    echo "install.sh: ciao service install"
  fi
  "$ciao_bin" service install "$@"
  service_rc=$?
  if [ "$service_rc" -eq 3 ]; then
    {
      echo "install.sh: the existing $UNIT_PATH belongs to a source install, and installing over it"
      echo "install.sh: would break that install (docs/LINUX.md#existing-source-installs)."
      echo "install.sh: take it over on purpose by rerunning this installer with --replace-unit."
    } >&2
    exit 3
  fi
  if [ "$service_rc" -ne 0 ]; then
    echo "install.sh: ciao service install failed with exit code $service_rc" >&2
    exit "$service_rc"
  fi
fi

echo
echo "Ciaobot is installed: $ciao_bin"
echo "Next steps:"
echo "  - Log in: open Ciaobot in a browser; the login URL is the one ciao setup prints."
if [ "$no_service" -eq 0 ]; then
  echo "  - Check the service: systemctl status ciaobot"
else
  echo "  - The service was skipped (--no-service): install it later with: ciao service install"
fi
echo "  - Sign in to a model provider from the Home screen."
exit 0
