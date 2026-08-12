#!/bin/sh
set -eu

# Build the two architecture-specific Python runtimes that live inside the
# universal Ciaobot.app. The caller supplies pinned python-build-standalone
# URLs and SHA-256 values; keeping those inputs explicit makes the release
# workflow auditable and prevents a moving "latest" runtime from entering a
# release unnoticed.

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
: "${CIAO_PYTHON_X86_64_URL:?CIAO_PYTHON_X86_64_URL is required}"
: "${CIAO_PYTHON_X86_64_SHA256:?CIAO_PYTHON_X86_64_SHA256 is required}"

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

rm -rf "$output"
mkdir -p "$output/python" "$output/site-packages" "$output/bin"

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
    if ! PYTHONPATH="$output/site-packages/$arch" \
        "$python_bin" -c 'import pydantic_core; from pydantic_core import core_schema' >/dev/null 2>&1; then
        echo "Bundled ${arch} runtime cannot import pydantic-core" >&2
        exit 1
    fi
}

download_runtime arm64 "$CIAO_PYTHON_ARM64_URL" "$CIAO_PYTHON_ARM64_SHA256"
download_runtime x86_64 "$CIAO_PYTHON_X86_64_URL" "$CIAO_PYTHON_X86_64_SHA256"

cat > "$output/bin/ciao" <<'LAUNCHER'
#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
case "$(uname -m)" in
    arm64) arch=arm64 ;;
    x86_64) arch=x86_64 ;;
    *) echo "Unsupported macOS architecture: $(uname -m)" >&2; exit 1 ;;
esac

python="$root/python/$arch/bin/python3.12"
site="$root/site-packages/$arch"
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
# Child agent commands must resolve to this matching Python runtime too.  In
# particular, inheriting this process's PYTHONPATH into a Homebrew/repo ciao
# shim would mix CPython 3.12 extension modules with another interpreter.
export PATH="$root/bin${PATH:+:$PATH}"
export PYTHONPATH="$site${PYTHONPATH:+:$PYTHONPATH}"
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
