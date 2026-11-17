import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
MODULE_PATH = REPO / "mac/scripts/engine_project_wheel.py"
SPEC = importlib.util.spec_from_file_location("engine_project_wheel", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)
COMMITTED = json.loads((REPO / "mac/engine-build-inputs.json").read_text())

EPOCH = 1723075200
COMMIT_DATE = "2024-08-08T00:00:00Z"
VERSION = "9.8.7"
DEPENDENCIES = ["alpha==1.0", "mcp>=1.30.0,<2.0", "Beta[extra]==2.0"]
PYPROJECT = (
    '[build-system]\nrequires = ["setuptools>=68", "wheel"]\nbuild-backend = "setuptools.build_meta"\n\n'
    f'[project]\nname = "ciaobot"\nversion = "{VERSION}"\n'
    f"dependencies = {json.dumps(DEPENDENCIES)}\n"
)
INDEX = b"<html>index</html>\n"
ASSET = b"console.log(1)\n"

# Stands in for setuptools.build_meta. `__OPTS__` selects a misbehaviour; `__PRELUDE__` runs first
# with its failures swallowed (so only the audit guard's own flag can fail the build).
STUB = '''
import base64, hashlib, os, time, tomllib, zipfile
OPTS = __OPTS__

def get_requires_for_build_wheel(config_settings=None):
    return OPTS.get("requires", [])

def _b64(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    try:
__PRELUDE__
    except BaseException:
        pass
    o = OPTS
    t = time.gmtime(int(os.environ["SOURCE_DATE_EPOCH"]))
    stamp = (1999, 1, 1, 0, 0, 0) if o.get("ts_off") else tuple(t[:6])
    project = tomllib.load(open("pyproject.toml", "rb"))["project"]
    ver = project["version"]
    di = f"ciaobot-{ver}.dist-info"
    files = {}
    for base, dirs, names in os.walk("ciao"):
        dirs.sort()
        for n in sorted(names):
            path = os.path.join(base, n)
            if o.get("skip_static") and path.startswith("ciao/web/static/"):
                continue
            files[path] = open(path, "rb").read() + (b"#" if o.get("mutate") and path.endswith("index.html") else b"")
    deps = list(project["dependencies"])
    if o.get("normalise", True):
        deps = [d.replace("mcp>=1.30.0,<2.0", "mcp<2.0,>=1.30.0") for d in deps]
    if o.get("drift"):
        deps[0] = "alpha==1.1"
    if o.get("extra_dep"):
        deps.append("gamma==3")
    meta = f"Metadata-Version: 2.4\\nName: ciaobot\\nVersion: {o.get('meta_version', ver)}\\n"
    meta += "".join(f"Requires-Dist: {d}\\n" for d in deps)
    meta += 'Requires-Dist: pytest==9; extra == "test"\\n'
    tag = o.get("tag", "py3-none-any")
    files[f"{di}/METADATA"] = meta.encode()
    files[f"{di}/WHEEL"] = f"Wheel-Version: 1.0\\nGenerator: stub\\nRoot-Is-Purelib: true\\nTag: {tag}\\n".encode()
    for extra in o.get("extra", []):
        files[extra[0]] = extra[1].encode()
    rows = [[p, "sha256=" + _b64(d), str(len(d))] for p, d in sorted(files.items())]
    if o.get("bad_record"):
        rows[0][1] = "sha256=" + _b64(b"other")
    rows.append([f"{di}/RECORD", "", ""])
    files[f"{di}/RECORD"] = "".join(",".join(r) + "\\n" for r in rows).encode()

    def emit(filename):
        with zipfile.ZipFile(os.path.join(wheel_directory, filename), "w") as zf:
            for path in sorted(files):
                info = zipfile.ZipInfo(path, date_time=stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, files[path])
            if o.get("symlink"):
                link = zipfile.ZipInfo("ciao/link.py", date_time=stamp)
                link.external_attr = 0o120777 << 16
                zf.writestr(link, b"target")

    emit(o.get("filename", f"ciaobot-{ver}-py3-none-any.whl"))
    if o.get("two"):
        emit(f"ciaobot-{ver}-py2-none-any.whl")
    return "x"
'''


def make_stub(prelude: str = "pass", **opts) -> str:
    body = "\n".join("        " + line for line in prelude.splitlines())
    return STUB.replace("__OPTS__", repr(opts)).replace("__PRELUDE__", body)


def backend_bytes(source: str, name="setuptools", version="84.2.1") -> bytes:
    raw = io.BytesIO()
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("setuptools/__init__.py", b"")
            zf.writestr("setuptools/build_meta.py", source.encode())
            zf.writestr("setuptools-84.2.1.dist-info/METADATA", meta)
    return raw.getvalue()


def private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


# ------------------------------------------------------------------ export


GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_AUTHOR_DATE": COMMIT_DATE,
    "GIT_COMMITTER_DATE": COMMIT_DATE,
}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["/usr/bin/git", *args], cwd=repo, env=GIT_ENV, check=True,
                          capture_output=True, text=True)
    return done.stdout.strip()


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        self.write("pyproject.toml", PYPROJECT)
        self.write("README.md", "readme\n")
        self.write("LICENSE", "licence\n")
        self.write(".gitignore", "ciao/web/static/assets/\nciao/web/static/index.html\n")
        self.write("ciao/__init__.py", "")
        self.write("ciao/web/static/favicon.png", "png")
        self.write("web/package-lock.json", '{"lockfileVersion": 3}\n')
        self.write("web/package.json", "{}\n")
        self.write("scripts/run.sh", "#!/bin/sh\n", executable=True)
        self.head = self.commit("one")

    def write(self, rel: str, data, executable=False) -> None:
        path = self.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        os.chmod(path, 0o755 if executable else 0o644)

    def commit(self, message: str) -> str:
        git(self.path, "add", "-A", "-f")
        git(self.path, "commit", "-q", "-m", message)
        return git(self.path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path / "repo")


@pytest.fixture
def parent(tmp_path) -> Path:
    return private(tmp_path / "parent")


class Npm:
    """Records the tool seam; npm and node are never run."""

    def __init__(self, monkeypatch, tmp_path, version="v24.22.1", fail=None, static=True):
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.version, self.fail, self.static = version, fail, static
        self.node_dir = tmp_path / "nodebin"
        self.node_dir.mkdir()
        (self.node_dir / "node").write_text("")
        (self.node_dir / "npm").write_text("")
        monkeypatch.setattr(mod, "_node_dir", lambda: self.node_dir)
        monkeypatch.setattr(mod, "_run_tool", self.run)

    def run(self, argv, cwd, env):
        self.calls.append((list(argv), Path(cwd), dict(env)))
        if self.fail and argv[1:] == self.fail:
            raise mod.ProjectWheelError()
        if argv[1:] == ["--version"]:
            return self.version.encode()
        if argv[1:2] == ["ci"]:
            (cwd / "node_modules/pkg").mkdir(parents=True)
            (cwd / "node_modules/pkg/index.js").write_text("x")
        if argv[1:] == ["run", "build"] and self.static:
            static = cwd.parent / "ciao/web/static"
            assert not static.exists()  # the committed brand files were cleared first
            (static / "assets").mkdir(parents=True)
            (static / "index.html").write_bytes(INDEX)
            (static / "assets/app.js").write_bytes(ASSET)
        return b""


def export(repo: Repo, parent: Path, commit=None):
    return mod.export_source(commit or repo.head, parent / "out", repo=repo.path)


def assert_untouched(parent: Path) -> None:
    assert list(parent.iterdir()) == []


def test_export_records_the_exact_commit_and_runs_npm_at_the_seam(repo, parent, tmp_path, monkeypatch) -> None:
    npm = Npm(monkeypatch, tmp_path)
    # Ignored build output sitting in the checkout must never reach the export.
    repo.write("ciao/web/static/assets/leak.js", "leak")
    repo.write("ciao/web/static/index.html", "stale")
    repo.write("untracked.txt", "x")
    receipt = export(repo, parent)

    tree = git(repo.path, "rev-parse", "HEAD^{tree}")
    lock = (repo.path / "web/package-lock.json").read_bytes()
    assert receipt == {
        "schema_version": 1,
        "commit": repo.head,
        "tree": tree,
        "source_date_epoch": EPOCH,
        "package_lock_sha256": hashlib.sha256(lock).hexdigest(),
        "static_inventory": [
            {"path": "assets/app.js", "size": len(ASSET), "sha256": hashlib.sha256(ASSET).hexdigest()},
            {"path": "index.html", "size": len(INDEX), "sha256": hashlib.sha256(INDEX).hexdigest()},
        ],
    }
    out = parent / "out"
    assert json.loads((out / "receipt.json").read_text()) == receipt
    source = out / "source"
    assert not (source / "ciao/web/static/assets/leak.js").exists()
    assert not (source / "ciao/web/static/favicon.png").exists()  # replaced wholesale by the build
    assert not (source / "untracked.txt").exists()
    assert not (source / "web/node_modules").exists()
    assert (source / "pyproject.toml").read_text() == PYPROJECT
    assert os.access(source / "scripts/run.sh", os.X_OK) and not os.access(source / "README.md", os.X_OK)
    assert [p.name for p in parent.iterdir()] == ["out"]

    (node_argv, node_cwd, node_env), (ci, ci_cwd, ci_env), (build, build_cwd, build_env) = npm.calls
    node = str(npm.node_dir / "node")
    npm_bin = str(npm.node_dir / "npm")
    assert node_argv == [node, "--version"]
    assert ci == [npm_bin, "ci", "--no-audit", "--no-fund"]
    assert build == [npm_bin, "run", "build"]
    # npm runs inside the staging copy of the export (renamed into place afterwards).
    assert ci_cwd == build_cwd and ci_cwd.parts[-3:] == ("out", "source", "web")
    for env in (node_env, ci_env, build_env):
        assert set(env) == {"PATH", "HOME", "TMPDIR", "LC_ALL", "TZ"}
        assert env["PATH"] == f"{npm.node_dir}:/usr/bin:/bin"
        assert env["LC_ALL"] == "C" and env["TZ"] == "UTC"
        assert not Path(env["HOME"]).is_relative_to(ci_cwd.parent) and not Path(env["TMPDIR"]).is_relative_to(ci_cwd.parent)
    assert not Path(ci_env["HOME"]).exists()  # scratch is gone with the staging directory


def test_export_is_repeatable_across_parents(repo, tmp_path, monkeypatch) -> None:
    Npm(monkeypatch, tmp_path)
    first, second = private(tmp_path / "p1"), private(tmp_path / "p2")
    assert export(repo, first) == export(repo, second)


def test_export_refuses_a_dirty_tracked_tree(repo, parent, tmp_path, monkeypatch) -> None:
    npm = Npm(monkeypatch, tmp_path)
    repo.write("README.md", "changed\n")
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)
    git(repo.path, "checkout", "--", "README.md")
    repo.write("README.md", "staged\n")
    git(repo.path, "add", "README.md")
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)
    assert npm.calls == []
    assert_untouched(parent)


def test_export_refuses_a_commit_that_is_not_head(repo, parent, tmp_path, monkeypatch) -> None:
    npm = Npm(monkeypatch, tmp_path)
    older = repo.head
    repo.write("ciao/extra.py", "x = 1\n")
    repo.commit("two")
    for bad in (older, "0" * 40, "HEAD", older[:12], older.upper(), ""):
        with pytest.raises(mod.ProjectWheelError):
            export(repo, parent, commit=bad)
    assert npm.calls == [] and not (parent / "out").exists()


def test_export_refuses_a_tracked_symlink(repo, parent, tmp_path, monkeypatch) -> None:
    npm = Npm(monkeypatch, tmp_path)
    os.symlink("/etc/passwd", repo.path / "ciao/web/static/leak")
    repo.commit("symlink")
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent, commit=git(repo.path, "rev-parse", "HEAD"))
    assert npm.calls == []
    assert_untouched(parent)


def test_export_refuses_archive_that_differs_from_the_committed_blob(repo, parent, tmp_path, monkeypatch) -> None:
    Npm(monkeypatch, tmp_path)
    repo.write(".gitattributes", "README.md export-subst\n")
    repo.write("README.md", "$Format:%H$\n")
    head = repo.commit("subst")
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent, commit=head)
    assert_untouched(parent)


def test_export_refuses_when_export_ignore_drops_a_file(repo, parent, tmp_path, monkeypatch) -> None:
    Npm(monkeypatch, tmp_path)
    repo.write(".gitattributes", "ciao/__init__.py export-ignore\n")
    head = repo.commit("ignore")
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent, commit=head)
    assert_untouched(parent)


@pytest.mark.parametrize("version", ["v22.22.1", "v22.21.9", "v23.0.0", "v24.14.9", "v25.1.0", "v20.19.0", "24.22.1", ""])
def test_export_refuses_unsupported_node(repo, parent, tmp_path, monkeypatch, version) -> None:
    npm = Npm(monkeypatch, tmp_path, version=version)
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)
    assert [c[0][1:] for c in npm.calls] == [["--version"]]  # checked before any install
    assert_untouched(parent)


@pytest.mark.parametrize("version", ["v22.22.2", "v22.23.0", "v24.15.0", "v24.22.1", "v26.0.0", "v27.1.0"])
def test_node_range_accepts(version) -> None:
    assert mod._node_supported(version)


def test_node_range_matches_the_frontend_declaration() -> None:
    engines = json.loads((REPO / "web/package.json").read_text())["engines"]["node"]
    assert engines == mod.NODE_RANGE
    assert f"SUPPORTED_RANGE = '{mod.NODE_RANGE}'" in (REPO / "web/scripts/check-node.mjs").read_text()


@pytest.mark.parametrize("fail", [["ci", "--no-audit", "--no-fund"], ["run", "build"]])
def test_export_leaves_nothing_when_npm_fails(repo, parent, tmp_path, monkeypatch, fail) -> None:
    Npm(monkeypatch, tmp_path, fail=fail)
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)
    assert_untouched(parent)


def test_export_refuses_a_build_without_index_html(repo, parent, tmp_path, monkeypatch) -> None:
    Npm(monkeypatch, tmp_path, static=False)
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)
    assert_untouched(parent)


def test_export_refuses_a_symlink_in_the_build_output(repo, parent, tmp_path, monkeypatch) -> None:
    npm = Npm(monkeypatch, tmp_path)
    real = npm.run

    def run(argv, cwd, env):
        out = real(argv, cwd, env)
        if argv[1:] == ["run", "build"]:
            os.symlink("/etc/passwd", cwd.parent / "ciao/web/static/link.js")
        return out

    monkeypatch.setattr(mod, "_run_tool", run)
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)
    assert_untouched(parent)


def test_export_refuses_a_lockfile_changed_by_the_build(repo, parent, tmp_path, monkeypatch) -> None:
    npm = Npm(monkeypatch, tmp_path)
    real = npm.run

    def run(argv, cwd, env):
        out = real(argv, cwd, env)
        if argv[1:2] == ["ci"]:
            (cwd / "package-lock.json").write_text("{}")
        return out

    monkeypatch.setattr(mod, "_run_tool", run)
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)
    assert_untouched(parent)


def test_export_refuses_existing_or_unsafe_destination(repo, parent, tmp_path, monkeypatch) -> None:
    Npm(monkeypatch, tmp_path)
    (parent / "out").mkdir()
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)
    (parent / "out").rmdir()
    os.chmod(parent, 0o755)
    with pytest.raises(mod.ProjectWheelError):
        export(repo, parent)


def entries_of(repo: Repo) -> dict:
    return mod._tree_entries(repo.path, repo.head)


def make_tar(members) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for info, data in members:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return raw.getvalue()


def reg(name: str, data: bytes = b"", mode=0o644):
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), mode
    return info, data


def test_archive_prescan_refuses_anything_but_the_listed_regular_files(tmp_path) -> None:
    entries = {"a.txt": (False, "0" * 40), "bin/run": (True, "1" * 40)}
    good = [reg("a.txt"), reg("bin/run", mode=0o755)]
    archive = tmp_path / "t.tar"
    archive.write_bytes(make_tar(good))
    assert sorted(mod._scan_archive(archive, entries)) == ["a.txt", "bin/run"]

    link = tarfile.TarInfo("a.txt")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    hard = tarfile.TarInfo("a.txt")
    hard.type, hard.linkname = tarfile.LNKTYPE, "bin/run"
    fifo = tarfile.TarInfo("a.txt")
    fifo.type = tarfile.FIFOTYPE
    bad = {
        "symlink": [(link, None), reg("bin/run", mode=0o755)],
        "hardlink": [reg("bin/run", mode=0o755), (hard, None)],
        "fifo": [(fifo, None), reg("bin/run", mode=0o755)],
        "unlisted": [*good, reg("extra.txt")],
        "missing": [reg("a.txt")],
        "duplicate": [*good, reg("a.txt")],
        "traversal": [reg("../a.txt"), reg("bin/run", mode=0o755)],
        "absolute": [reg("/a.txt"), reg("bin/run", mode=0o755)],
        "exec bit": [reg("a.txt", mode=0o755), reg("bin/run", mode=0o755)],
    }
    for label, members in bad.items():
        archive.write_bytes(make_tar(members))
        with pytest.raises((mod.ProjectWheelError, mod.derived.DerivedWheelError)):  # private seam, unwrapped
            mod._scan_archive(archive, entries)
        assert label


# ------------------------------------------------------------------- build


class Fixture:
    """An exported tree plus a stub setuptools wheel pinned into the loader."""

    def __init__(self, tmp_path: Path, monkeypatch, prelude="pass", **opts):
        self.tmp = tmp_path
        self.backend_data = backend_bytes(make_stub(prelude, **opts))
        self.backend = tmp_path / "setuptools-84.2.1-py3-none-any.whl"
        self.backend.write_bytes(self.backend_data)
        pin = copy.deepcopy(COMMITTED)
        pin["build_tools"][0].update(size=len(self.backend_data), sha256=hashlib.sha256(self.backend_data).hexdigest())
        self.pin = pin
        monkeypatch.setattr(mod.derived, "_read_inputs_bytes", lambda: json.dumps(self.pin).encode())
        self.root = private(tmp_path / "export")
        self.source = self.root / "source"
        self.put("pyproject.toml", PYPROJECT)
        self.put("README.md", "readme\n")
        self.put("LICENSE", "licence\n")
        self.put("web/package-lock.json", '{"lockfileVersion": 3}\n')
        self.put("web/package.json", "{}\n")
        self.put("ciao/__init__.py", "")
        self.put("ciao/web/__init__.py", "")
        self.put("ciao/web/static/index.html", INDEX)
        self.put("ciao/web/static/assets/app.js", ASSET)
        self.receipt = {
            "schema_version": 1,
            "commit": "a" * 40,
            "tree": "b" * 40,
            "source_date_epoch": EPOCH,
            "package_lock_sha256": hashlib.sha256((self.source / "web/package-lock.json").read_bytes()).hexdigest(),
            "static_inventory": mod._inventory(self.source / "ciao/web/static"),
        }
        self.write_receipt()
        self.parent = private(tmp_path / "parent")

    def put(self, rel: str, data) -> None:
        path = self.source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())

    def write_receipt(self) -> None:
        (self.root / "receipt.json").write_text(json.dumps(self.receipt))

    def build(self, parent: Path | None = None):
        return mod.build_project_wheel(self.root, self.backend, (parent or self.parent) / "out")

    def refused(self) -> None:
        with pytest.raises(mod.ProjectWheelError):
            self.build()
        assert list(self.parent.iterdir()) == []


def make(tmp_path, monkeypatch, prelude="pass", **opts) -> Fixture:
    return Fixture(tmp_path, monkeypatch, prelude, **opts)


def make_second(tmp_path, monkeypatch, prelude="pass", **opts) -> Fixture:
    return Fixture(private(tmp_path / "second"), monkeypatch, prelude, **opts)


def test_build_produces_the_one_validated_wheel(tmp_path, monkeypatch) -> None:
    fx = make(tmp_path, monkeypatch)
    receipt = fx.build()
    wheels = list((fx.parent / "out").iterdir())
    assert [w.name for w in wheels] == [f"ciaobot-{VERSION}-py3-none-any.whl"]
    data = wheels[0].read_bytes()
    assert receipt == {
        "wheel": {"filename": wheels[0].name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        "commit": "a" * 40,
        "tree": "b" * 40,
        "backend_sha256": fx.pin["build_tools"][0]["sha256"],
        "source_date_epoch": EPOCH,
    }
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert archive.read("ciao/web/static/index.html") == INDEX
        assert archive.read("ciao/web/static/assets/app.js") == ASSET
    assert not any(n.startswith("mac/") for n in names)
    assert not (fx.source / "build").exists() and not list(fx.source.glob("*.egg-info"))  # the export is read only


def test_build_is_deterministic_across_parents(tmp_path, monkeypatch) -> None:
    fx = make(tmp_path, monkeypatch)
    other = private(tmp_path / "other")
    first, second = fx.build(), fx.build(other)
    assert first == second
    name = first["wheel"]["filename"]
    assert (fx.parent / "out" / name).read_bytes() == (other / "out" / name).read_bytes()


def test_build_prints_only_the_receipt(tmp_path, monkeypatch, capsys) -> None:
    fx = make(tmp_path, monkeypatch, "print('noise')\nimport sys\nsys.stderr.write('warn')")
    argv = ["build", "--source", str(fx.root), "--backend-wheel", str(fx.backend), "--destination", str(fx.parent / "out")]
    assert mod.main(argv) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["commit"] == "a" * 40 and captured.err == ""


def test_build_does_not_leak_into_the_caller_process(tmp_path, monkeypatch) -> None:
    fx = make(tmp_path, monkeypatch)
    before = dict(os.environ), sys.path[:], os.getcwd(), os.umask(0o022)
    fx.build()
    assert (dict(os.environ), sys.path[:], os.getcwd(), os.umask(0o022)) == before


@pytest.mark.parametrize("prelude", [
    "import subprocess\nsubprocess.run(['/usr/bin/true'])",
    "import socket\nsocket.socket().connect(('127.0.0.1', 9))",
    "import os\nos.system('true')",
    "import urllib.request\nurllib.request.urlopen('http://127.0.0.1:9')",
])
def test_build_guard_blocks_subprocess_and_network(tmp_path, monkeypatch, prelude) -> None:
    make(tmp_path, monkeypatch, prelude).refused()
    assert mod.derived._GUARD["on"] is False


@pytest.mark.parametrize("opts", [
    {"extra": [["mac/scripts/x.sh", "x"]]},
    {"extra": [["tests/test_x.py", "x"]]},
    {"skip_static": True},
    {"mutate": True},
    {"extra": [["ciao/web/static/extra.js", "x"]]},
    {"drift": True},
    {"extra_dep": True},
    {"meta_version": "1.0.0"},
    {"filename": "ciaobot-1.0.0-py3-none-any.whl"},
    {"filename": f"ciaobot-{VERSION}-py3-none-macosx_11_0_arm64.whl"},
    {"tag": "cp312-cp312-macosx_11_0_arm64"},
    {"two": True},
    {"ts_off": True},
    {"bad_record": True},
    {"symlink": True},
    {"requires": ["flit-core"]},
    {"requires": ["setuptools>=999"]},
], ids=lambda o: next(iter(o)))
def test_build_refuses_a_wheel_that_breaks_an_invariant(tmp_path, monkeypatch, opts) -> None:
    make(tmp_path, monkeypatch, **opts).refused()


def test_build_requires_dist_must_equal_pyproject_in_both_directions(tmp_path, monkeypatch) -> None:
    make(tmp_path, monkeypatch, normalise=False).build()  # a literal copy of the strings is fine too
    make_second(tmp_path, monkeypatch, drift=True).refused()


def test_build_refuses_a_backend_that_is_not_the_pin(tmp_path, monkeypatch) -> None:
    fx = make(tmp_path, monkeypatch)
    fx.backend.write_bytes(fx.backend_data + b"\0")
    fx.refused()
    fx.backend.write_bytes(backend_bytes(make_stub("x = 1")))  # same shape, different bytes
    fx.refused()


def test_build_checks_the_backend_before_parsing_the_receipt(tmp_path, monkeypatch) -> None:
    fx = make(tmp_path, monkeypatch)
    spy = []
    real = mod.derived._extract_backend
    monkeypatch.setattr(mod.derived, "_extract_backend", lambda *a, **k: spy.append(1) or real(*a, **k))
    fx.backend.write_bytes(b"not a zip")
    fx.refused()
    assert spy == []


@pytest.mark.parametrize("tamper", ["lock", "static_content", "static_extra", "static_missing", "key", "commit",
                                    "epoch", "unsorted", "no_index", "layout", "symlink_source", "no_licence", "dynamic", "backend", "symlink_in_ciao", "version"])
def test_build_refuses_a_tampered_export(tmp_path, monkeypatch, tamper) -> None:
    fx = make(tmp_path, monkeypatch)
    if tamper == "lock":
        fx.put("web/package-lock.json", "{}\n")
    elif tamper == "static_content":
        fx.put("ciao/web/static/assets/app.js", b"changed")
    elif tamper == "static_extra":
        fx.put("ciao/web/static/assets/extra.js", b"x")
    elif tamper == "static_missing":
        (fx.source / "ciao/web/static/assets/app.js").unlink()
    elif tamper == "key":
        fx.receipt["extra"] = 1
        fx.write_receipt()
    elif tamper == "commit":
        fx.receipt["commit"] = "A" * 40
        fx.write_receipt()
    elif tamper == "epoch":
        fx.receipt["source_date_epoch"] = 5
        fx.write_receipt()
    elif tamper == "unsorted":
        fx.receipt["static_inventory"].reverse()
        fx.write_receipt()
    elif tamper == "no_index":
        fx.receipt["static_inventory"] = fx.receipt["static_inventory"][:1]
        fx.write_receipt()
    elif tamper == "layout":
        (fx.root / "stray").write_text("x")
    elif tamper == "symlink_source":
        os.symlink("/etc/passwd", fx.source / "ciao/web/static/assets/link.js")
    elif tamper == "no_licence":
        (fx.source / "LICENSE").unlink()
    elif tamper == "dynamic":
        fx.put("pyproject.toml", PYPROJECT.replace('name = "ciaobot"', 'name = "ciaobot"\ndynamic = ["readme"]'))
    elif tamper == "backend":
        fx.put("pyproject.toml", PYPROJECT.replace("setuptools.build_meta", "flit_core.buildapi"))
    elif tamper == "symlink_in_ciao":
        os.symlink("/etc/passwd", fx.source / "ciao/link.py")
    elif tamper == "version":
        fx.put("pyproject.toml", PYPROJECT.replace(VERSION, "9.x"))
    fx.refused()


def test_build_only_copies_the_package_and_its_metadata(tmp_path, monkeypatch) -> None:
    fx = make(tmp_path, monkeypatch)
    fx.put("setup.py", "raise SystemExit(1)\n")
    fx.put("mac/scripts/x.sh", "x")
    fx.put("tests/test_x.py", "x")
    fx.put("web/node_modules/pkg/index.js", "x")
    receipt = fx.build()
    with zipfile.ZipFile(fx.parent / "out" / receipt["wheel"]["filename"]) as archive:
        assert not [n for n in archive.namelist() if n.startswith(("mac/", "tests/", "web/"))]


def test_build_refuses_unsafe_parent_destination_and_source(tmp_path, monkeypatch) -> None:
    fx = make(tmp_path, monkeypatch)
    (fx.parent / "out").mkdir()
    with pytest.raises(mod.ProjectWheelError):
        fx.build()
    (fx.parent / "out").rmdir()
    os.chmod(fx.parent, 0o755)
    with pytest.raises(mod.ProjectWheelError):
        fx.build()
    os.chmod(fx.parent, 0o700)
    os.chmod(fx.root, 0o755)
    with pytest.raises(mod.ProjectWheelError):
        fx.build()


def test_pyproject_dependency_normalisation_matches_setuptools_output() -> None:
    norm = mod._normalise_requirement
    assert norm("mcp>=1.30.0,<2.0") == norm("mcp<2.0,>=1.30.0")
    assert norm("uvicorn[standard]==0.54.0") == norm("uvicorn[standard] ==0.54.0")
    assert norm("PyJWT[crypto]==2.15.1") == norm("pyjwt[crypto]==2.15.1")
    assert norm("a==1") != norm("a==2") and norm("a[x]==1") != norm("a==1")


def test_the_committed_pyproject_declares_what_the_helper_accepts() -> None:
    declared = (REPO / "pyproject.toml").read_text()
    import tomllib

    data = tomllib.loads(declared)
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"
    tool = COMMITTED["build_tools"][0]
    assert all(mod.derived._requirement_ok(r, tool) for r in data["build-system"]["requires"])
    assert "dynamic" not in data["project"]
    assert all(";" not in d and mod._REQUIREMENT.fullmatch(d.strip()) for d in data["project"]["dependencies"])
    assert set(mod.PROJECT_FILES) <= {p.name for p in REPO.iterdir()}


# --------------------------------------------------------------------- CLI


def test_cli_usage_errors_exit_2_with_one_fixed_line(capsys) -> None:
    for argv in ([], ["export"], ["build", "--source", "x"], ["export", "--comm", "x", "--destination", "y"],
                 ["bogus"], ["export", "--commit", "x", "--destination", "y", "--extra", "z"]):
        with pytest.raises(SystemExit) as exc:
            mod.main(argv)
        assert exc.value.code == 2
    err = capsys.readouterr().err.splitlines()
    assert set(err) == {"project wheel failed"}


def test_cli_validation_failures_exit_1_without_detail(tmp_path, capsys) -> None:
    for dest in (str(tmp_path / "a" / ".." / "b"), ""):
        assert mod.main(["export", "--commit", "a" * 40, "--destination", dest]) == 1
    assert mod.main(["build", "--source", "/nonexistent", "--backend-wheel", "/nonexistent",
                     "--destination", str(tmp_path / "out")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert set(captured.err.splitlines()) == {"project wheel failed"}
