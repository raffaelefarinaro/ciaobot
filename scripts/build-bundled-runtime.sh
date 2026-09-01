#!/bin/sh
set -eu

# Build the arm64 Python runtime that lives inside the Apple Silicon-only
# Ciaobot.app. The caller supplies pinned python-build-standalone URLs and
# SHA-256 values; keeping those inputs explicit makes the release workflow
# auditable and prevents a moving "latest" runtime from entering a release
# unnoticed.

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <desktop/runtime-output>" >&2
    exit 2
fi

output=$1
repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-runtime.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

: "${CIAO_PYTHON_ARM64_URL:?CIAO_PYTHON_ARM64_URL is required}"
: "${CIAO_PYTHON_ARM64_SHA256:?CIAO_PYTHON_ARM64_SHA256 is required}"

command -v uv >/dev/null 2>&1 || {
    echo "uv is required to export the checked-in uv.lock for the bundled runtime" >&2
    exit 1
}
requirements="$tmp/runtime-requirements.txt"
uv export \
    --frozen \
    --no-dev \
    --no-emit-project \
    --format requirements.txt \
    --output-file "$requirements"

# setuptools reuses `build/lib` between wheel builds and never removes files
# that have since been deleted from the source tree, so every wheel built on a
# long-lived checkout carries the ghosts of every module ever deleted. A dev
# machine shipped nine of them, four import-broken, because `build/lib` had 119
# modules against the 110 that still existed. Nothing catches it: the ghosts are
# imported by nothing, so mypy and pytest stay green, and the failure only shows
# when something walks the package. Clearing it costs one rebuild of a directory
# that is pure derived output.
rm -rf "$repo_root/build"

rm -rf "$output"
mkdir -p "$output/python" "$output/site-packages" "$output/bin"
# The bundled-site hook is written from a path computed with os.path.relpath,
# which needs both ends anchored the same way; resolve the caller's possibly
# relative output directory once, up front.
output=$(CDPATH= cd -- "$output" && pwd)

download_runtime() {
    arch=$1
    url=$2
    expected=$3
    archive="$tmp/python-${arch}.tar.gz"
    install_root="$tmp/install-${arch}"

    curl -fsSL --retry 3 --connect-timeout 15 "$url" -o "$archive"
    actual=$(shasum -a 256 "$archive" | awk '{print $1}')
    if [ "$actual" != "$expected" ]; then
        echo "Python runtime checksum mismatch for ${arch}" >&2
        exit 1
    fi
    mkdir -p "$install_root"
    tar -xzf "$archive" -C "$install_root"
    python_bin=$(find "$install_root" -type f -path '*/bin/python3.12' -print -quit)
    if [ -z "$python_bin" ]; then
        echo "Python runtime archive for ${arch} has no python3.12" >&2
        exit 1
    fi
    python_root=$(CDPATH= cd -- "$(dirname "$python_bin")/.." && pwd)
    cp -R "$python_root" "$output/python/$arch"
    python_bin="$output/python/$arch/bin/python3.12"
    mkdir -p "$output/site-packages/$arch"
    if ! "$python_bin" -m pip --version >/dev/null 2>&1; then
        "$python_bin" -m ensurepip --upgrade >/dev/null 2>&1
    fi
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
        "$python_bin" -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        --no-compile \
        --target "$output/site-packages/$arch" \
        --require-hashes \
        -r "$requirements"
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
        "$python_bin" -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        --no-compile \
        --no-deps \
        --target "$output/site-packages/$arch" \
        "$repo_root"
    # Attach the dependency tree to this interpreter only, through a .pth hook
    # in its own site-packages. Exporting PYTHONPATH from the launcher instead
    # would hand the tree to every descendant process, including a Homebrew or
    # repo-venv `ciao` on a different CPython, which then dies importing the
    # 3.12 extension modules.
    purelib=$("$python_bin" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    if [ ! -d "$purelib" ]; then
        echo "Bundled ${arch} interpreter has no site-packages at $purelib" >&2
        exit 1
    fi
    rel=$("$python_bin" -c \
        'import os, sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' \
        "$output/site-packages/$arch" "$purelib")
    sed "s|@BUNDLED_SITE_REL@|$rel|" \
        "$repo_root/scripts/runtime_bundled_site.py" >"$purelib/ciao_bundled_site.py"
    if grep -q '@BUNDLED_SITE_REL@' "$purelib/ciao_bundled_site.py"; then
        echo "Bundled ${arch} site hook kept its unsubstituted placeholder" >&2
        exit 1
    fi
    # Sorts after any .pth the interpreter ships, so those run first.
    echo 'import ciao_bundled_site' >"$purelib/zz-ciao-bundled-site.pth"

    # The claude-agent-sdk wheel bundles a ~325MB standalone Claude CLI binary
    # (_bundled/claude) so the SDK has a guaranteed CLI. Bundling it doubles the
    # app's download size, so strip it here and rely on an external `claude` on
    # PATH instead. ciao's provider code already falls back to the external CLI
    # when no bundled binary is present.
    "$python_bin" -c 'from pathlib import Path; import claude_agent_sdk; Path(claude_agent_sdk.__file__).parent.joinpath("_bundled", "claude").unlink(missing_ok=True)' >/dev/null 2>&1 || true

    # Probe with no PYTHONPATH at all: this is what the launcher now relies on.
    if ! (unset PYTHONPATH; "$python_bin" -c \
        'import pydantic_core, ciao; from pydantic_core import core_schema') >/dev/null 2>&1; then
        echo "Bundled ${arch} runtime cannot import pydantic-core" >&2
        exit 1
    fi
}

download_runtime arm64 "$CIAO_PYTHON_ARM64_URL" "$CIAO_PYTHON_ARM64_SHA256"

cat > "$output/bin/ciao" <<'LAUNCHER'
#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
# Where the caller ran us, captured before the cd below throws it away. A
# relative path in an argument -- `ciao critique --input memory-vault/x.md` --
# means "relative to the workspace I am standing in", and after `cd "$root"`
# Python would resolve it against the app bundle and report the file missing.
CIAO_INVOCATION_CWD=$PWD
export CIAO_INVOCATION_CWD
cd "$root"
python="$root/python/arm64/bin/python3.12"
site="$root/site-packages/arm64"
if [ ! -x "$python" ] || [ ! -d "$site" ]; then
    echo "Ciaobot's bundled Python runtime is incomplete" >&2
    exit 1
fi
export CIAO_BUNDLED_APP=1
export CIAO_ENGINE_PATH="$root/bin/ciao"
# The app bundle contains the engine, not mutable user data. A configured
# LaunchAgent supplies CIAO_RUNTIME_ROOT from the user's workspace; leave it
# unset for workspace-less bootstrap so CiaoConfig uses its external default.
if [ -n "${CIAO_RUNTIME_ROOT:-}" ]; then
    export CIAO_RUNTIME_ROOT
else
    unset CIAO_RUNTIME_ROOT
fi
# Child agent commands should resolve to this matching Python runtime too.
export PATH="$root/bin${PATH:+:$PATH}"
# Deliberately no PYTHONPATH: the interpreter finds its own dependency tree
# through the ciao_bundled_site hook in its site-packages. PYTHONPATH is
# inherited by every descendant, so exporting it here handed the 3.12 tree to
# any child running a different Python - and a shell profile that re-prepends
# /opt/homebrew/bin defeats the PATH ordering above, so `ciao` in a child shell
# resolved to the Homebrew shim and died with
# `No module named 'pydantic_core._pydantic_core'`. An inherited PYTHONPATH is
# dropped for the same reason, in reverse: it must not shadow the bundle.
unset PYTHONPATH
# Python writes __pycache__ next to the sources it imports, and those sources
# live inside the signed app bundle. Left alone, the first run adds files the
# code signature does not seal, so `codesign -v` fails on an app that has only
# been used. Redirect the cache out of the bundle rather than disabling it, so
# startup stays warm.
if [ -z "${PYTHONPYCACHEPREFIX:-}" ]; then
    cache_root=${XDG_CACHE_HOME:-${HOME:+$HOME/Library/Caches}}
    if [ -n "$cache_root" ]; then
        PYTHONPYCACHEPREFIX="$cache_root/Ciaobot/pycache"
    fi
fi
if [ -n "${PYTHONPYCACHEPREFIX:-}" ]; then
    export PYTHONPYCACHEPREFIX
else
    export PYTHONDONTWRITEBYTECODE=1
fi
exec "$python" -m ciao.cli "$@"
LAUNCHER
chmod 755 "$output/bin/ciao"

cat > "$output/README.txt" <<'RUNTIME_README'
Ciaobot's embedded Python runtime. Generated by scripts/build-bundled-runtime.sh.
Do not edit files in this directory; replace the app through the signed updater.
RUNTIME_README
