"""Stdlib-only materializer; initial interpreter MUST be externally isolated."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import secrets
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

BASE = "448ca028e7c9ba836980343e7a86c7eaddd8828a"
# Exact CPython 3.12 PyYAML 6.0.3 wheels; reviewed dependency, no pip/site hooks.
# SHA-256 and URLs from https://pypi.org/pypi/PyYAML/6.0.3/json (read-only).
WHEELS = {
    ("win32", "amd64"): (
        "5fcd34e47f6e0b794d17de1b4ff496c00986e1c83f7ab2fb8fcfe9616ff7477b",
        "86/bf/899e81e4cce32febab4fb42bb97dcdf66bc135272882d1987881a4b519e9/pyyaml-6.0.3-cp312-cp312-win_amd64.whl",
    ),
    ("linux", "x86_64"): (
        "ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc",
        "8b/9d/b3589d3877982d4f2329302ef98a8026e7f4443c765c46cfecc8858c6b4b/pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
    ),
    ("darwin", "arm64"): (
        "fc09d0aa354569bc501d4e787133afc08552722d3ab34836a80547331bb5d4a0",
        "89/a0/6cf41a19a1f2f3feab0e9c0b74134aa2ce6849093d5517a0c550fe37a648/pyyaml-6.0.3-cp312-cp312-macosx_11_0_arm64.whl",
    ),
    ("darwin", "x86_64"): (
        "7f047e29dcae44602496db43be01ad42fc6f1cc0d8cd6c83d342306c32270196",
        "d1/33/422b98d2195232ca1826284a76852ad5a86fe23e31b009c9886b2d0fb8b2/pyyaml-6.0.3-cp312-cp312-macosx_10_13_x86_64.whl",
    ),
}


def require(value):
    if not value:
        raise RuntimeError("materialization-refused")


def write(root, name, content, manifest):
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts and "\\" not in name)
    require(not any(p == "__pycache__" or p.endswith((".pyc", ".pyo", ".pth"))
                    for p in path.parts))
    require(name not in manifest)
    target = root.joinpath(*path.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(content)
    manifest[name] = hashlib.sha256(content).hexdigest()


def main():
    require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode)
    require(sys.version_info[:2] == (3, 12))
    require(len(sys.argv) == 6)
    git, repo, root, harness, hosted = sys.argv[1:]
    root = Path(root).resolve(strict=True)
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
        "PROCESSOR_ARCHITECTURE", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
        "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CIAO_MEMORY_DIR",
        "CIAO_LAUNCH_AGENTS_DIR", "CIAO_QUEUE_LOCK_DIR", "TMPDIR", "TMP", "TEMP",
        "LC_CTYPE",  # CPython may coerce the locale at startup.
    }
    if sys.platform == "darwin":
        # macOS runtime startup adds this even with a sanitized exec environment.
        # Allow its name only on macOS; never log the user-specific value.
        allowed.add("__CF_USER_TEXT_ENCODING")
    require(all(key.upper() in allowed for key in os.environ))
    for key, sub in {
        "CIAO_MEMORY_DIR": "memory", "CIAO_LAUNCH_AGENTS_DIR": "launchagents",
        "CIAO_QUEUE_LOCK_DIR": "locks", "HOME": "home", "USERPROFILE": "home",
    }.items():
        require(Path(os.environ[key]).resolve() == root / sub)
    # Read pinned Git OBJECTS, not working-tree/ignored/untracked files.
    archive = subprocess.run(
        [git, "-c", "core.attributesFile=/dev/null" if sys.platform != "win32" else
         "core.attributesFile=NUL", "archive", "--format=tar", BASE, "ciao"],
        cwd=repo, capture_output=True, check=True, timeout=30,
    ).stdout
    tree = subprocess.run(
        [git, "ls-tree", "-r", "-z", BASE, "ciao"], cwd=repo,
        capture_output=True, check=True, timeout=30,
    ).stdout
    blobs = {}
    for entry in tree.rstrip(b"\0").split(b"\0"):
        info, name = entry.split(b"\t", 1)
        mode, kind, oid = info.split()
        require(mode in {b"100644", b"100755"} and kind == b"blob")
        blobs[name.decode("utf-8")] = oid.decode("ascii")
    source, deps = root / "source", root / "deps"
    source.mkdir()
    deps.mkdir()
    source_files, dep_files = {}, {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isdir():
                continue
            require(member.isfile())  # No links, devices or cached importables.
            content = tar.extractfile(member).read()
            oid = hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
            require(blobs.get(member.name) == oid)
            write(source, member.name, content, source_files)
    require(set(source_files) == set(blobs))  # export-ignore/subst cannot silently alter tree.
    digest, wheel_path = WHEELS[(sys.platform, platform.machine().lower())]
    with urllib.request.urlopen("https://files.pythonhosted.org/packages/" + wheel_path,
                                timeout=30) as response:
        payload = response.read(8 * 1024 * 1024 + 1)
    require(len(payload) <= 8 * 1024 * 1024)
    require(hashlib.sha256(payload).hexdigest() == digest)
    with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
        for member in wheel.infolist():
            if member.is_dir():
                continue
            require(not ((member.external_attr >> 16) & 0o170000 == 0o120000))
            write(deps, member.filename, wheel.read(member), dep_files)
    manifest = root / "import-manifest.json"
    manifest.write_text(json.dumps({
        "source": source_files, "deps": dep_files, "wheel_sha256": digest,
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "base": BASE, "hosted_runner": hosted,
        "protocol_token": secrets.token_hex(32),
    }, sort_keys=True), encoding="utf-8")
    # No outer timeout/kill: the protocol parent must stay alive to reap peers.
    # Each peer operation and cleanup is bounded inside baseline.py.
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-u", harness,
         str(source), str(root), str(manifest)],
        cwd=root,
    )
    code = process.wait()
    if code != 0:
        return code
    receipt = json.loads(root.joinpath("protocol-cleaned.json").read_text(encoding="utf-8"))
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    require(receipt == {
        "parent_pid": process.pid, "protocol_token": expected["protocol_token"],
        "peers_reaped": 2, "pipes_closed": True,
    })
    # Absence/mismatch/uncertain peer ownership or cleanup can never authorize deletion.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"event": "BLOCKED", "stage": "materialization",
                          "error_type": type(exc).__name__}), flush=True)
        sys.exit(1)
