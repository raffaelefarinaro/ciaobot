"""The Linux one-line installer, `scripts/install.sh`, exercised through `sh`.

The harness rewrites the script's variable block so every external program it
would run is a small fake that logs its argv to one shared log file, and so
the unit path, version and wheel digest are per-test values. A run therefore
never downloads, never touches the real ``/etc`` unit, and never sees this
machine's identity; only core utilities from ``/bin`` and ``/usr/bin`` are
real. The fake ``uv`` writes a fake ``ciao`` that logs too, and the fake
``tar`` materializes the archive members the bootstrap then installs.
"""

from pathlib import Path
import re
import shutil
import stat
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
X86_64_DIGEST = "5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91"
AARCH64_DIGEST = "74f2f5c6024d397481db228db8fc25eed420db192243f88438c118e7e013e585"
WHEEL_URL_2_0_0 = (
    "https://github.com/raffaelefarinaro/ciaobot/releases/download/v2.0.0/"
    "ciaobot-2.0.0-py3-none-any.whl"
)
UV_ARCHIVE_URL_SUFFIX = "uv-x86_64-unknown-linux-gnu.tar.gz"

# Every fake is generated with its paths baked in: no configuration flows
# through the environment, which install.sh must not read.


FAKE_UNAME = r"""#!/bin/sh
printf '%s\n' "uname $*" >> "{log}"
if [ "$1" = "-s" ]; then
  echo "{uname_s}"
  exit 0
fi
if [ "$1" = "-m" ]; then
  echo "{uname_m}"
  exit 0
fi
echo "uname fake: unhandled argv: $*"
exit 2
"""


FAKE_ID = r"""#!/bin/sh
printf '%s\n' "id $*" >> "{log}"
if [ "$1" = "-u" ]; then
  echo "{uid}"
  exit 0
fi
echo "id fake: unhandled argv: $*"
exit 2
"""


FAKE_CURL = r"""#!/bin/sh
printf '%s\n' "curl $*" >> "{log}"
prev=
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then
    : > "$arg"
  fi
  prev="$arg"
done
exit 0
"""


# `-c -` reads the "digest  filename" line on stdin. {maxok} passes the first
# {maxok} invocations and refuses the rest, so a test can fail exactly one
# digest check (0: the uv archive check, 1: the wheel check after it).
FAKE_SHA256SUM = r"""#!/bin/sh
printf '%s\n' "sha256sum $*" >> "{log}"
if [ "$1" = "-c" ]; then
  cat > /dev/null
fi
count=0
if [ -f "{calls}" ]; then
  count=$(cat "{calls}")
fi
count=$((count + 1))
echo "$count" > "{calls}"
if [ "$count" -gt {maxok} ]; then
  echo "sha256sum fake: refusing call $count" >&2
  exit 1
fi
exit 0
"""


# Emulates extraction: the members the bootstrap then installs.
FAKE_TAR = r"""#!/bin/sh
printf '%s\n' "tar $*" >> "{log}"
dir=
prev=
for arg in "$@"; do
  if [ "$prev" = "-C" ]; then
    dir=$arg
  fi
  prev="$arg"
done
if [ -z "$dir" ]; then
  echo "tar fake: no -C in argv" >> "{log}"
  exit 2
fi
for arch in x86_64 aarch64; do
  mkdir -p "$dir/uv-$arch-unknown-linux-gnu"
  cp "{uv_src}" "$dir/uv-$arch-unknown-linux-gnu/uv"
  cp "{uv_src}" "$dir/uv-$arch-unknown-linux-gnu/uvx"
  chmod 755 "$dir/uv-$arch-unknown-linux-gnu/uv" "$dir/uv-$arch-unknown-linux-gnu/uvx"
done
exit 0
"""


FAKE_UV = r"""#!/bin/sh
printf '%s\n' "uv $*" >> "{log}"
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  mkdir -p "{fakebin}"
  cp "{ciao_src}" "{fakebin}/ciao"
  chmod 755 "{fakebin}/ciao"
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then
  echo "{fakebin}"
  exit 0
fi
echo "uv fake: unhandled argv: $*"
exit 2
"""


# `service install` models the CLI contract: a conflicting unit exits 3 unless
# --replace is passed, so the mutation "always pass --replace" is observable.
FAKE_CIAO = r"""#!/bin/sh
printf '%s\n' "ciao $*" >> "{log}"
if [ "$1" = "--version" ]; then
  echo "ciaobot {version}"
  exit 0
fi
if [ "$1" = "setup" ]; then
  echo "Login: http://127.0.0.1:8543/login?token=TESTTOKEN"
  exit 0
fi
if [ "$1" = "service" ] && [ "$2" = "install" ]; then
  for arg in "$@"; do
    if [ "$arg" = "--replace" ]; then
      exit 0
    fi
  done
  if [ "{service_mode}" = "conflict" ]; then
    echo "ciao fake: UnitConflict: the existing unit runs another interpreter" >&2
    exit 3
  fi
  exit 0
fi
exit 0
"""


def _rewritten(source: str, **lines: str) -> str:
    """Replace each `name=...` line with `name=value`, whole lines only."""
    for name, value in lines.items():
        source = re.sub(rf"(?m)^{re.escape(name)}=.*$", lambda _, v=value: f"{name}={v}", source)
    return source


class Harness:
    """A rewritten copy of install.sh plus the fakes it calls."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        stamped: bool = True,
        uid: str = "1000",
        uname_s: str = "Linux",
        uname_m: str = "x86_64",
        max_sha256_ok: int = 999999,
        service_mode: str = "ok",
        with_unit: bool = False,
    ) -> None:
        self.root = tmp_path / "harness"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(parents=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.tmp_root = self.root / "tmp"
        self.tmp_root.mkdir()
        self.log = self.root / "run.log"
        self.unit = self.root / "unit.service"
        if with_unit:
            self.unit.write_text("ExecStart=/usr/bin/python3 -m ciao.cli run\n", encoding="utf-8")
        staged = self.root / "staged"
        staged.mkdir()
        ciao_src = staged / "ciao"
        ciao_src.write_text(
            FAKE_CIAO.format(log=self.log, version="2.0.0", service_mode=service_mode),
            encoding="utf-8",
        )
        ciao_src.chmod(0o755)
        uv_src = staged / "uv"
        uv_src.write_text(
            FAKE_UV.format(log=self.log, fakebin=staged / "uvbin", ciao_src=ciao_src),
            encoding="utf-8",
        )
        uv_src.chmod(0o755)
        self._fake(self.bin_dir / "uname", FAKE_UNAME, log=self.log, uname_s=uname_s, uname_m=uname_m)
        self._fake(self.bin_dir / "curl", FAKE_CURL, log=self.log)
        self._fake(
            self.bin_dir / "sha256sum", FAKE_SHA256SUM, log=self.log,
            calls=self.root / "sha256.calls", maxok=max_sha256_ok,
        )
        self._fake(self.bin_dir / "tar", FAKE_TAR, log=self.log, uv_src=uv_src)
        self._fake(self.bin_dir / "id", FAKE_ID, log=self.log, uid=uid)
        script_lines = {
            "UNAME": str(self.bin_dir / "uname"),
            "CURL": str(self.bin_dir / "curl"),
            "SHA256SUM": str(self.bin_dir / "sha256sum"),
            "TAR": str(self.bin_dir / "tar"),
            "ID": str(self.bin_dir / "id"),
            "UNIT_PATH": str(self.unit),
        }
        if stamped:
            script_lines["CIAOBOT_VERSION"] = "2.0.0"
            script_lines["WHEEL_SHA256"] = "a" * 64
        self.script = self.root / "install.sh"
        self.script.write_text(_rewritten(INSTALL_SH.read_text(encoding="utf-8"), **script_lines), encoding="utf-8")
        self.script.chmod(0o755)

    @staticmethod
    def _fake(path: Path, template: str, **fields: object) -> None:
        path.write_text(template.format(**fields), encoding="utf-8")
        path.chmod(0o755)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(self.script), *args],
            env={
                "PATH": f"{self.bin_dir}:/usr/bin:/bin",
                "HOME": str(self.home),
                "TMPDIR": str(self.tmp_root),
                "LANG": "C",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    def log_lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def lines_starting(self, prefix: str) -> list[str]:
        return [line for line in self.log_lines() if line.startswith(prefix)]

    def local_wheel(self) -> Path:
        wheel = self.root / "ciaobot-2.0.0-py3-none-any.whl"
        wheel.write_bytes(b"a fake wheel")
        return wheel


def test_refuses_macos_and_points_at_the_dmg(tmp_path):
    harness = Harness(tmp_path, uname_s="Darwin")
    result = harness.run()
    assert result.returncode == 1
    assert "https://github.com/raffaelefarinaro/ciaobot/releases/latest" in result.stderr
    assert "macOS" in result.stderr
    assert "app" in result.stderr
    assert harness.lines_starting("curl ") == []
    assert harness.lines_starting("tar ") == []
    assert harness.lines_starting("uv ") == []


def test_refuses_other_non_linux(tmp_path):
    harness = Harness(tmp_path, uname_s="FreeBSD")
    result = harness.run()
    assert result.returncode == 1
    assert "supports Linux only" in result.stderr
    assert harness.lines_starting("curl ") == []


def test_refuses_root(tmp_path):
    harness = Harness(tmp_path, uid="0")
    result = harness.run()
    assert result.returncode == 1
    assert "useradd" in result.stderr
    assert "sudo -u <user> -H sh install.sh" in result.stderr
    assert harness.lines_starting("curl ") == []
    assert harness.lines_starting("tar ") == []
    assert harness.lines_starting("uv ") == []


def test_unstamped_copy_requires_wheel(tmp_path):
    harness = Harness(tmp_path, stamped=False)
    result = harness.run()
    assert result.returncode == 1
    assert "source copy" in result.stderr
    assert (
        "https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh"
        in result.stderr
    )
    assert harness.lines_starting("curl ") == []
    # --wheel is the documented way to install from the source copy: the run
    # must proceed to the install step rather than refusing everything.
    second = harness.run("--wheel", str(harness.local_wheel()))
    assert second.returncode == 0
    assert harness.lines_starting("uv tool install ") != []


def test_digest_mismatch_never_reaches_uv(tmp_path):
    # Call 1 is the uv bootstrap archive check (it passes), call 2 the wheel
    # check (it fails), so the refusal is attributable to the wheel digest.
    harness = Harness(tmp_path, max_sha256_ok=1)
    result = harness.run()
    assert result.returncode == 1
    assert "refusing to install" in result.stderr
    assert harness.lines_starting("uv ") == []
    assert any(WHEEL_URL_2_0_0 in line for line in harness.lines_starting("curl "))


def test_bootstraps_uv_when_missing(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    assert result.returncode == 0
    assert any(UV_ARCHIVE_URL_SUFFIX in line for line in harness.lines_starting("curl "))
    installed_uv = harness.home / ".local" / "bin" / "uv"
    assert installed_uv.is_file()
    assert stat.S_IMODE(installed_uv.stat().st_mode) == 0o755
    assert (harness.home / ".local" / "bin" / "uvx").is_file()


def test_bootstrap_rejects_a_tampered_uv_archive_before_extracting(tmp_path):
    harness = Harness(tmp_path, max_sha256_ok=0)
    result = harness.run()
    assert result.returncode == 1
    assert "pinned sha256" in result.stderr
    assert harness.lines_starting("tar ") == []
    assert harness.lines_starting("uv ") == []
    assert any(UV_ARCHIVE_URL_SUFFIX in line for line in harness.lines_starting("curl "))


def test_bootstrap_without_a_pinned_architecture_refuses(tmp_path):
    harness = Harness(tmp_path, uname_m="armv7l")
    result = harness.run()
    assert result.returncode == 1
    assert "x86_64 and aarch64" in result.stderr
    assert harness.lines_starting("curl ") == []
    assert harness.lines_starting("tar ") == []


def test_installs_with_force_and_pinned_python(tmp_path):
    harness = Harness(tmp_path)
    wheel = harness.local_wheel()
    result = harness.run("--wheel", str(wheel))
    assert result.returncode == 0
    install_lines = harness.lines_starting("uv tool install ")
    assert install_lines == [f"uv tool install --force --python 3.13 {wheel}"]


def test_missing_local_wheel_refuses(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run("--wheel", str(harness.root / "nope.whl"))
    assert result.returncode == 1
    assert "no such file" in result.stderr
    assert harness.lines_starting("uv ") == []


def test_stamped_run_downloads_the_versioned_wheel_url(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    assert result.returncode == 0
    assert any(WHEEL_URL_2_0_0 in line for line in harness.lines_starting("curl "))


def test_runs_setup_then_service_install(tmp_path):
    harness = Harness(tmp_path)
    workspace = harness.root / "ws"
    result = harness.run("--workspace", str(workspace))
    assert result.returncode == 0
    log = harness.log_lines()
    install_index = log.index([line for line in log if line.startswith("uv tool install")][0])
    version_index = log.index("ciao --version")
    setup_index = log.index(f"ciao setup --workspace {workspace}")
    service_index = log.index(f"ciao service install --workspace {workspace}")
    assert install_index < version_index < setup_index < service_index
    # The trap cleaned the scratch directory up.
    assert list(harness.tmp_root.glob("ciaobot-install.*")) == []


def test_no_service_skips_service_install(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run("--no-service")
    assert result.returncode == 0
    assert harness.lines_starting("ciao service install") == []
    assert harness.lines_starting("ciao setup --workspace") != []


def test_no_workspace_defaults_to_home_ciaobot_when_no_unit(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    assert result.returncode == 0
    default = f"{harness.home}/Ciaobot"
    assert f"ciao setup --workspace {default}" in harness.log_lines()
    assert f"ciao service install --workspace {default}" in harness.log_lines()


def test_rerun_with_existing_unit_skips_setup_and_omits_workspace(tmp_path):
    harness = Harness(tmp_path, with_unit=True)
    result = harness.run()
    assert result.returncode == 0
    assert harness.lines_starting("ciao setup") == []
    assert "ciao service install" in harness.log_lines()
    assert not any(line.startswith("ciao service install --workspace") for line in harness.log_lines())


def test_service_conflict_exit_3_is_propagated_without_replace(tmp_path):
    harness = Harness(tmp_path, service_mode="conflict")
    workspace = harness.root / "ws"
    result = harness.run("--workspace", str(workspace))
    assert result.returncode == 3
    assert "--replace-unit" in result.stderr
    assert "docs/LINUX.md" in result.stderr
    service_lines = harness.lines_starting("ciao service install")
    assert len(service_lines) == 1
    assert "--replace" not in service_lines[0]


def test_replace_unit_flag_is_passed_through(tmp_path):
    harness = Harness(tmp_path)
    workspace = harness.root / "ws"
    result = harness.run("--workspace", str(workspace), "--replace-unit")
    assert result.returncode == 0
    service_lines = harness.lines_starting("ciao service install")
    assert service_lines == [f"ciao service install --workspace {workspace} --replace"]


def test_unknown_flag_exits_2(tmp_path):
    harness = Harness(tmp_path)
    for args in (("--bogus",), ("--workspace",), ("--workspace", "")):
        result = harness.run(*args)
        assert result.returncode == 2, args
        assert "unknown flag" in result.stderr or "Usage" in result.stderr, args
    # -h is not an error.
    result = harness.run("-h")
    assert result.returncode == 0
    assert "Usage" in result.stdout


def test_uv_pin_matches_pyproject_required_version():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'required-version\s*=\s*"==([^"]+)"', pyproject)
    assert match, "pyproject.toml lost its [tool.uv] required-version pin"
    source = INSTALL_SH.read_text(encoding="utf-8")
    version = re.search(r"(?m)^UV_VERSION=(\S+)$", source)
    assert version and version.group(1) == match.group(1)


def test_uv_digests_are_sha256_hex():
    source = INSTALL_SH.read_text(encoding="utf-8")
    assert re.search(rf"(?m)^UV_SHA256_X86_64=({X86_64_DIGEST})$", source)
    assert re.search(rf"(?m)^UV_SHA256_AARCH64=({AARCH64_DIGEST})$", source)
    for name in ("X86_64", "AARCH64"):
        assert re.search(rf"(?m)^UV_SHA256_{name}=([0-9a-f]{{64}})$", source), name


def test_script_parses_with_sh_n():
    subprocess.run(["/bin/sh", "-n", str(INSTALL_SH)], check=True, capture_output=True, text=True)
    dash = shutil.which("dash")
    if dash:
        subprocess.run([dash, "-n", str(INSTALL_SH)], check=True, capture_output=True, text=True)
