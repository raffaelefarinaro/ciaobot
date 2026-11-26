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
# Trust model: curl talks TLS only -- --proto and --proto-redir pin every
# download and every redirect to https, TLS 1.2 or newer, bounded by
# connect/total timeouts with retries. The release wheel's sha256 is stamped
# into this file by the release pipeline and compared before anything is
# installed, so the download is only installed when it is the release
# artifact byte for byte. uv publishes no signature for its release archive,
# so that archive's sha256 is pinned below and verified before it is
# extracted. A uv already on the machine is trusted only when it runs and
# reports at least UV_MIN_VERSION, the first uv whose `uv tool install`
# supports --force --python together; anything else -- a truncated copy left
# by an interrupted earlier run, a uv too old for those flags -- is ignored
# and replaced by the freshly downloaded one, which is copied to a temporary
# name inside the destination directory and moved into place, so a crashed
# run can never leave a half-written uv that a later run trusts. Root is
# refused: the engine and its workspace belong to a dedicated account, never
# root's.
#
# Accepted design, no behaviour hidden beyond it: `uv tool install --python
# 3.13` may download a standalone Python for Ciaobot, and the wheel's
# dependencies resolve from PyPI. Those downloads ride uv's own TLS; their
# individual artifacts are not digest-pinned by this installer. That is the
# accepted trust boundary.
set -eu

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
# The oldest uv an already-provided one may have: 0.3.0 is the release that
# stabilized the uv tool interface, whose `uv tool install` carries both
# --force and --python (with automatic Python downloads). An existing uv
# older than this, or one that does not run, is ignored and bootstrapped
# over -- see the trust model in the header.
UV_MIN_VERSION=0.3.0
UV_SHA256_X86_64=5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91
UV_SHA256_AARCH64=74f2f5c6024d397481db228db8fc25eed420db192243f88438c118e7e013e585

usage() {
  echo "Usage: sh install.sh [--workspace DIR] [--no-service] [--replace-unit] [--wheel PATH] [-h]"
  echo "  --workspace DIR   set up this workspace (default: \$HOME/Ciaobot when no service unit exists yet)"
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

# HOME must be usable before anything writes under it. A unset, empty,
# relative or non-existent HOME, and one this account cannot write (what
# running under sudo without -H leaves behind), is a refusal.
home_refusal=
case "${HOME:-}" in
  "")
    home_refusal="HOME is not set or empty"
    ;;
  /*) ;;
  *)
    home_refusal="HOME is not an absolute path: $HOME"
    ;;
esac
if [ -z "$home_refusal" ] && [ ! -d "$HOME" ]; then
  home_refusal="HOME does not exist: $HOME"
fi
if [ -z "$home_refusal" ] && [ ! -w "$HOME" ]; then
  home_refusal="HOME is not writable: $HOME"
fi
if [ -n "$home_refusal" ]; then
  {
    echo "install.sh: refusing to continue: $home_refusal."
    echo "Run this installer as the account that will own Ciaobot, with that account's own home:"
    echo "  sudo -u <user> -H sh install.sh"
  } >&2
  exit 1
fi

# The root check fails closed: when the user id cannot be determined, refuse
# instead of assuming a harmless one.
uid=$("$ID" -u 2>/dev/null) || {
  echo "install.sh: could not determine the user id ($ID -u failed); refusing to continue" >&2
  exit 1
}
case $uid in
  ''|*[!0-9]*)
    echo "install.sh: could not determine the user id ('$uid' is not a number); refusing to continue" >&2
    exit 1
    ;;
esac
if [ "$uid" = "0" ]; then
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

# uv_ver_ge A B: 0 when the dotted version A is at least the dotted version
# B. Numeric components only; a missing component counts as 0 and anything
# unparseable compares as smaller.
uv_ver_ge() {
  a=$1
  b=$2
  while [ -n "$a" ] || [ -n "$b" ]; do
    ac=${a%%.*}
    bc=${b%%.*}
    [ -n "$ac" ] || ac=0
    [ -n "$bc" ] || bc=0
    case $ac in *[!0-9]*) return 1 ;; esac
    case $bc in *[!0-9]*) return 1 ;; esac
    if [ "$ac" -lt "$bc" ]; then
      return 1
    fi
    if [ "$ac" -gt "$bc" ]; then
      return 0
    fi
    case $a in *.*) a=${a#*.} ;; *) a= ;; esac
    case $b in *.*) b=${b#*.} ;; *) b= ;; esac
  done
  return 0
}

# uv_is_usable CANDIDATE: 0 when CANDIDATE is an executable that runs and
# reports a version at least UV_MIN_VERSION.
uv_is_usable() {
  [ -x "$1" ] || return 1
  out=$("$1" --version </dev/null 2>/dev/null) || return 1
  ver=${out#uv }
  ver=${ver%%[!0-9.]*}
  uv_ver_ge "$ver" "$UV_MIN_VERSION"
}

# uv_place SRC DEST: copy SRC to a temporary name in DEST's directory, make
# it executable, then move it into place. `mv` inside one directory is a
# rename, so DEST never holds a half-written file that a later rerun would
# trust.
uv_place() {
  staged=$2.new.$$
  cp "$1" "$staged" || {
    rm -f "$staged"
    return 1
  }
  chmod 755 "$staged" || {
    rm -f "$staged"
    return 1
  }
  mv "$staged" "$2" || {
    rm -f "$staged"
    return 1
  }
}

# The uv to use: an already-provided one only when it passes the check above,
# otherwise the freshly downloaded one.
uv=
cand=$(command -v uv 2>/dev/null) || cand=
if [ -n "$cand" ]; then
  if uv_is_usable "$cand"; then
    uv=$cand
  else
    echo "install.sh: ignoring uv on PATH ($cand): it does not run or predates uv $UV_MIN_VERSION" >&2
  fi
fi
if [ -z "$uv" ] && uv_is_usable "$HOME/.local/bin/uv"; then
  uv=$HOME/.local/bin/uv
elif [ -z "$uv" ] && [ -e "$HOME/.local/bin/uv" ]; then
  echo "install.sh: ignoring $HOME/.local/bin/uv: it does not run or predates uv $UV_MIN_VERSION" >&2
fi

if [ -z "$uv" ]; then
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
  "$CURL" -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 15 --max-time 300 --retry 3 \
    -o "$tmpd/$uv_archive" "$uv_url" </dev/null || {
    echo "install.sh: downloading $uv_url failed" >&2
    exit 1
  }
  if ! printf '%s  %s\n' "$uv_digest" "$tmpd/$uv_archive" | "$SHA256SUM" -c - >/dev/null; then
    echo "install.sh: the downloaded $uv_archive does not match the pinned sha256 ($uv_digest); refusing to use it" >&2
    exit 1
  fi
  # The check above runs before tar reads the archive: a tampered archive is
  # refused without being extracted.
  mkdir -p "$tmpd/uv" || {
    echo "install.sh: could not create $tmpd/uv" >&2
    exit 1
  }
  "$TAR" -xzf "$tmpd/$uv_archive" -C "$tmpd/uv" || {
    echo "install.sh: extracting $uv_archive failed" >&2
    exit 1
  }
  local_bin=$HOME/.local/bin
  mkdir -p "$local_bin" || {
    echo "install.sh: could not create $local_bin" >&2
    exit 1
  }
  uv_place "$tmpd/uv/uv-$uv_arch-unknown-linux-gnu/uv" "$local_bin/uv" || {
    echo "install.sh: could not install the downloaded uv into $local_bin" >&2
    exit 1
  }
  uv_place "$tmpd/uv/uv-$uv_arch-unknown-linux-gnu/uvx" "$local_bin/uvx" || {
    echo "install.sh: could not install the downloaded uvx into $local_bin" >&2
    exit 1
  }
  uv=$local_bin/uv
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
  "$CURL" -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 15 --max-time 300 --retry 3 \
    -o "$wheel" "$wheel_url" </dev/null || {
    echo "install.sh: downloading $wheel_url failed" >&2
    exit 1
  }
  if ! printf '%s  %s\n' "$WHEEL_SHA256" "$wheel" | "$SHA256SUM" -c - >/dev/null; then
    echo "install.sh: the downloaded wheel does not match the sha256 stamped in this installer ($WHEEL_SHA256); refusing to install it" >&2
    exit 1
  fi
fi

# 2.0 upgrades from a finished 1.10 only. Read the 1.10 migration receipts of
# every workspace this run could touch (the existing unit's, and --workspace
# when given) with the wheel's own stdlib-only checker, before uv replaces the
# installed engine; an unfinished migration, or a check that cannot run, stops
# the install and leaves the 1.10 engine in place. Run from the scratch
# directory with --no-project (uv would otherwise search upward from it for a
# pyproject.toml, e.g. when TMPDIR sits inside a project) and python -P, so
# neither the current directory nor a ./ciao folder can stand in for the
# wheel's module. --no-project is in uv 0.3.0, UV_MIN_VERSION (verified).
case $wheel in
  /*) ;;
  *) wheel=$PWD/$wheel ;;
esac
pf_dirs=
if [ -e "$UNIT_PATH" ]; then
  pf_dirs=$(sed -n 's/^WorkingDirectory=//p' "$UNIT_PATH" | head -n 1)
fi
if [ "$have_workspace" -eq 1 ]; then
  pf_dirs="$pf_dirs
$workspace_value"
elif [ -z "$pf_dirs" ] && [ ! -e "$UNIT_PATH" ]; then
  pf_dirs=$HOME/Ciaobot
fi
pf_old_ifs=$IFS
IFS='
'
for pf_ws in $pf_dirs; do
  IFS=$pf_old_ifs
  if [ -n "$pf_ws" ] && [ -d "$pf_ws/.runtime" ]; then
    (cd "$tmpd" && PYTHONPATH=$wheel "$uv" run --no-project --python "$PYTHON_VERSION" python -P -m ciao.upgrade_preflight "$pf_ws" </dev/null) || {
      echo "install.sh: not upgrading $pf_ws; nothing was installed or changed." >&2
      exit 4
    }
  fi
done
IFS=$pf_old_ifs

echo "install.sh: installing Ciaobot with uv..."
"$uv" tool install --force --python "$PYTHON_VERSION" "$wheel" </dev/null || {
  echo "install.sh: uv tool install failed" >&2
  exit 1
}
uv_bin_dir=$("$uv" tool dir --bin </dev/null) || {
  echo "install.sh: uv tool dir --bin failed" >&2
  exit 1
}
ciao_bin=$uv_bin_dir/ciao
if [ ! -x "$ciao_bin" ]; then
  echo "install.sh: ciao was not installed into $uv_bin_dir" >&2
  exit 1
fi
"$ciao_bin" --version </dev/null || {
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
  "$ciao_bin" setup --workspace "$ws" </dev/null || {
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
  # No stdin redirection here: ciao service install reaches sudo through this
  # terminal and must be able to prompt.
  service_rc=0
  "$ciao_bin" service install "$@" || service_rc=$?
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
  echo "  - The service was skipped (--no-service): install it later with:"
  echo "      $ciao_bin service install"
fi
echo "  - Sign in to a model provider from the Home screen."
exit 0
