"""The Linux one-line installer, `scripts/install.sh`, exercised through `sh`.

The harness rewrites the script's variable block so every external program it
would run is a small fake that logs its argv to one shared log file, and so
the unit path, version and wheel digest are per-test values. A run therefore
never downloads, never touches the real ``/etc`` unit, and never sees this
machine's identity; only core utilities from ``/bin`` and ``/usr/bin`` are
real. The fake ``uv`` writes a fake ``ciao`` that logs too, and the fake
``tar`` materializes the archive members the bootstrap then installs. Fakes
that model children which must not consume the script (curl, ``uv tool
install``, ``ciao setup``) also record whether they could read stdin, so a
run with a canary on stdin proves the script redirects their stdin; the
fake ``ciao`` records the same for ``service install``, which must keep the
terminal for its sudo prompt.
"""

from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

# Sentinel for `Harness.run(home=...)`: by default the harness home; None
# drops HOME from the environment entirely.
UNSET = object()

# The canary a stdin-reading child would echo back into the log if the script
# failed to redirect its stdin.
STDIN_CANARY = b"STDIN-CANARY\n"
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
if read -r stdin_probe; then
  printf '%s\n' "curl-stdin:$stdin_probe" >> "{log}"
else
  printf '%s\n' "curl-stdin:" >> "{log}"
fi
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


# Emulates extraction: the members the bootstrap then installs. No chmod on
# the extracted members: the script's own `uv_place` sets the mode, so a
# failing-chmod test observes the script's chmod, not the fake tar's.
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
done
exit 0
"""


FAKE_UV = r"""#!/bin/sh
printf '%s\n' "uv $*" >> "{log}"
if [ "$1" = "--version" ]; then
  echo "{uv_version}"
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  if read -r stdin_probe; then
    printf '%s\n' "uv-stdin:$stdin_probe" >> "{log}"
  else
    printf '%s\n' "uv-stdin:" >> "{log}"
  fi
  mkdir -p "{fakebin}"
  cp "{ciao_src}" "{fakebin}/ciao"
  chmod 755 "{fakebin}/ciao"
  exit 0
fi
if [ "$1" = "run" ]; then
  # The preflight: run the repository's own checker, as the wheel would be.
  while [ "$1" != "python" ]; do shift; done
  shift
  PYTHONPATH="{repo}" exec "{python}" "$@"
fi
if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then
  echo "{fakebin}"
  exit 0
fi
echo "uv fake: unhandled argv: $*"
exit 2
"""


# A uv candidate that exists on disk but cannot run: what a truncated or
# partially written binary behaves like when `uv_is_usable` probes it.
BROKEN_UV = r"""#!/bin/sh
printf '%s\n' "uv $*" >> "{log}"
echo "uv fake: truncated binary, refusing to run" >&2
exit 1
"""


# A generic external that refuses: mounted into bin/ over mkdir, chmod or id
# by the tests that check install.sh aborts on their failure.
FAILING_FAKE = r"""#!/bin/sh
printf '%s\n' "{name} $*" >> "{log}"
echo "{name} fake: refusing $*" >&2
exit 1
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
  if read -r stdin_probe; then
    printf '%s\n' "ciao-stdin:$stdin_probe" >> "{log}"
  else
    printf '%s\n' "ciao-stdin:" >> "{log}"
  fi
  echo "Login: http://127.0.0.1:8543/login?token=TESTTOKEN"
  exit 0
fi
if [ "$1" = "service" ] && [ "$2" = "install" ]; then
  if read -r stdin_probe; then
    printf '%s\n' "ciao-service-stdin:$stdin_probe" >> "{log}"
  else
    printf '%s\n' "ciao-service-stdin:" >> "{log}"
  fi
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
        name: str = "harness",
        stamped: bool = True,
        uid: str = "1000",
        uname_s: str = "Linux",
        uname_m: str = "x86_64",
        max_sha256_ok: int = 999999,
        service_mode: str = "ok",
        with_unit: bool = False,
    ) -> None:
        self.root = tmp_path / name
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
            FAKE_UV.format(
                log=self.log, fakebin=staged / "uvbin", ciao_src=ciao_src, uv_version="uv 999.0.0 (fresh)",
                repo=REPO_ROOT, python=sys.executable,
            ),
            encoding="utf-8",
        )
        uv_src.chmod(0o755)
        self.ciao_src = ciao_src
        self._fake(self.bin_dir / "uname", FAKE_UNAME, log=self.log, uname_s=uname_s, uname_m=uname_m)
        self._fake(self.bin_dir / "curl", FAKE_CURL, log=self.log)
        self._fake(
            self.bin_dir / "sha256sum", FAKE_SHA256SUM, log=self.log,
            calls=self.root / "sha256.calls", maxok=max_sha256_ok,
        )
        self._fake(self.bin_dir / "tar", FAKE_TAR, log=self.log, uv_src=uv_src)
        self._fake(self.bin_dir / "id", FAKE_ID, log=self.log, uid=uid)
        # A uv on PATH that is too old for `uv tool install --force --python`:
        # deterministic on every machine (the script must ignore it and
        # bootstrap), and reproducible wherever the tests run.
        self._fake(
            self.bin_dir / "uv", FAKE_UV, log=self.log,
            fakebin=staged / "uvbin", ciao_src=ciao_src, repo=REPO_ROOT, python=sys.executable,
            uv_version="uv 0.0.0 (too old for tool install --python)",
        )
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

    def run(
        self,
        *args: str,
        stdin: object = subprocess.DEVNULL,
        home: object = UNSET,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp_root),
            "LANG": "C",
        }
        if home is not UNSET:
            if home is None:
                del env["HOME"]
            else:
                env["HOME"] = str(home)
        return subprocess.run(
            ["/bin/sh", str(self.script), *args],
            env=env,
            stdin=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def install_fake(self, program: str, template: str, **fields: object) -> None:
        """Mount another fake over a core utility in the harness bin dir."""
        self._fake(self.bin_dir / program, template, **fields)

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
    # Nothing but the usability probe of the too-old PATH uv ever ran: uv
    # tool install is unreached while bootstrap artifacts exist untouched.
    assert harness.lines_starting("uv tool") == []
    assert any(WHEEL_URL_2_0_0 in line for line in harness.lines_starting("curl "))


def test_bootstraps_uv_when_missing(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run()
    assert result.returncode == 0
    assert any(UV_ARCHIVE_URL_SUFFIX in line for line in harness.lines_starting("curl "))
    installed_uv = harness.home / ".local" / "bin" / "uv"
    assert installed_uv.is_file()
    assert stat.S_IMODE(installed_uv.stat().st_mode) == 0o755
    # The install left no staging copy behind: everything landed in place.
    assert sorted(p.name for p in installed_uv.parent.iterdir()) == ["uv", "uvx"]
    assert (harness.home / ".local" / "bin" / "uvx").is_file()


def test_bootstrap_rejects_a_tampered_uv_archive_before_extracting(tmp_path):
    harness = Harness(tmp_path, max_sha256_ok=0)
    result = harness.run()
    assert result.returncode == 1
    assert "pinned sha256" in result.stderr
    assert harness.lines_starting("tar ") == []
    assert harness.lines_starting("uv tool") == []
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
    assert harness.lines_starting("uv tool") == []


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


TLS_CURL_FLAGS = (
    "-fsSL",
    "--proto '=https'",
    "--proto-redir '=https'",
    "--tlsv1.2",
    "--connect-timeout 15",
    "--max-time 300",
    "--retry 3",
)


def test_every_curl_call_pins_https():
    """Static: no `$CURL` invocation line may exist without the TLS locks."""
    source = INSTALL_SH.read_text(encoding="utf-8")
    calls = [line for line in source.splitlines() if '"$CURL"' in line]
    assert calls, "install.sh lost its curl invocations"
    for line in calls:
        for flag in TLS_CURL_FLAGS:
            assert flag in line, (flag, line)


def test_every_curl_run_locks_tls(tmp_path):
    """Runtime: both downloads the fake curl saw carried the TLS locks."""
    harness = Harness(tmp_path)
    result = harness.run()
    assert result.returncode == 0
    curl_lines = harness.lines_starting("curl ")
    assert len(curl_lines) == 2  # the uv archive and the wheel
    for line in curl_lines:
        assert "--proto =https" in line, line
        assert "--proto-redir =https" in line, line
        assert "--tlsv1.2" in line, line
        assert "--connect-timeout 15" in line, line
        assert "--max-time 300" in line, line
        assert "--retry 3" in line, line


def test_script_sets_set_eu():
    source = INSTALL_SH.read_text(encoding="utf-8")
    assert re.search(r"(?m)^set -eu$", source)


def test_uv_minimum_version_is_stated_and_not_above_the_pin():
    source = INSTALL_SH.read_text(encoding="utf-8")
    minimum = re.search(r"(?m)^UV_MIN_VERSION=([0-9][0-9.]*)$", source)
    pin = re.search(r"(?m)^UV_VERSION=([0-9][0-9.]*)$", source)
    assert minimum, "install.sh lost UV_MIN_VERSION"
    assert pin
    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(component) for component in version.split("."))
    assert parts(minimum.group(1)) <= parts(pin.group(1))


def test_failing_mkdir_aborts(tmp_path):
    harness = Harness(tmp_path)
    harness.install_fake("mkdir", FAILING_FAKE, name="mkdir", log=harness.log)
    result = harness.run()
    assert result.returncode != 0
    assert "could not create" in result.stderr
    # The abort is explicit: nothing after the digest check ran.
    curl_lines = harness.lines_starting("curl ")
    assert len(curl_lines) == 1
    assert UV_ARCHIVE_URL_SUFFIX in curl_lines[0]
    assert harness.lines_starting("tar ") == []
    assert harness.lines_starting("uv tool") == []


SELECTIVE_MKDIR = r"""#!/bin/sh
printf '%s\n' "mkdir $*" >> "{log}"
case "$*" in
  *"{fail_on}"*)
    echo "mkdir fake: refusing $*" >&2
    exit 1
    ;;
esac
exec /bin/mkdir "$@"
"""


def test_failing_mkdir_of_the_uv_directory_aborts(tmp_path):
    harness = Harness(tmp_path)
    harness.install_fake("mkdir", SELECTIVE_MKDIR, log=harness.log, fail_on=".local/bin")
    result = harness.run()
    assert result.returncode != 0
    assert "could not create" in result.stderr
    assert ".local/bin" in result.stderr
    assert harness.lines_starting("uv tool") == []
    assert len(harness.lines_starting("curl ")) == 1


def test_failing_chmod_aborts(tmp_path):
    harness = Harness(tmp_path)
    harness.install_fake("chmod", FAILING_FAKE, name="chmod", log=harness.log)
    result = harness.run()
    assert result.returncode != 0
    assert "could not install the downloaded uv" in result.stderr
    # Aborted during the bootstrap: the wheel download and uv never ran.
    assert harness.lines_starting("tar ") != []
    assert len(harness.lines_starting("curl ")) == 1
    assert harness.lines_starting("uv tool") == []
    # Neither the staged nor the final binary was left behind half-trusted.
    bin_entries = sorted(p.name for p in (harness.home / ".local" / "bin").iterdir())
    assert not any(".new." in name for name in bin_entries)


def test_invalid_home_refuses(tmp_path):
    harness = Harness(tmp_path)
    cases = (
        (None, "HOME is not set"),
        ("", "HOME is not set"),
        ("relative/path", "not an absolute path"),
        (harness.root / "missing", "does not exist"),
    )
    for home, fragment in cases:
        result = harness.run(home=home)
        assert result.returncode == 1, home
        assert fragment in result.stderr, (home, result.stderr)
        assert harness.lines_starting("curl ") == [], home
        assert harness.lines_starting("uv tool") == [], home
    # -h must stay usable even with no HOME at all (usage prints it as text).
    result = harness.run("-h", home=None)
    assert result.returncode == 0
    assert "Usage" in result.stdout


def test_unwritable_home_refuses_and_hints_sudo_with_h(tmp_path):
    harness = Harness(tmp_path)
    ro_home = harness.root / "rohome"
    ro_home.mkdir()
    ro_home.chmod(0o555)
    try:
        result = harness.run(home=ro_home)
        assert result.returncode == 1
        assert "HOME is not writable" in result.stderr
        # What running under sudo without -H leaves behind: the remedy.
        assert "sudo -u <user> -H sh install.sh" in result.stderr
    finally:
        ro_home.chmod(0o755)
    assert harness.lines_starting("curl ") == []
    assert harness.lines_starting("uv tool") == []


BAD_ID = r"""#!/bin/sh
printf '%s\n' "id $*" >> "{log}"
if [ "{mode}" = "fail" ]; then
  echo "id fake: refusing" >&2
  exit 1
fi
echo "{uid}"
exit 0
"""


def test_root_check_fails_closed_when_id_cannot_be_determined(tmp_path):
    for mode, uid in (
        ("fail", "ignored"),          # `id -u` errors out
        ("empty", ""),                # prints nothing, exits 0
        ("garbage", "n0t-a-number"),  # prints non-numeric output
    ):
        harness = Harness(tmp_path, name=f"harness-{mode}")
        harness.install_fake("id", BAD_ID, log=harness.log, mode=mode, uid=uid)
        result = harness.run()
        assert result.returncode == 1, mode
        assert "could not determine the user id" in result.stderr, (mode, result.stderr)
        assert harness.lines_starting("curl ") == [], mode
        assert harness.lines_starting("tar ") == [], mode
        assert harness.lines_starting("uv tool") == [], mode


def test_uv_on_path_is_used_when_new_enough(tmp_path):
    harness = Harness(tmp_path)
    fresh_bin = harness.root / "freshbin"
    harness.install_fake(
        "uv", FAKE_UV, log=harness.log,
        fakebin=fresh_bin, ciao_src=harness.ciao_src, repo=REPO_ROOT, python=sys.executable,
        uv_version="uv 12.3.4 (new enough)",
    )
    wheel = harness.local_wheel()
    result = harness.run("--wheel", str(wheel))
    assert result.returncode == 0
    assert "ignoring" not in result.stderr
    # Trusted path uv: no bootstrap download at all (and no wheel download).
    assert harness.lines_starting("curl ") == []
    assert harness.lines_starting("uv tool install ") == [
        f"uv tool install --force --python 3.13 {wheel}",
    ]


def test_old_uv_on_path_is_rejected_and_bootstrapped(tmp_path):
    # Harness's built-in PATH uv reports uv 0.0.0: below UV_MIN_VERSION.
    harness = Harness(tmp_path)
    result = harness.run("--wheel", str(harness.local_wheel()))
    assert result.returncode == 0
    assert "ignoring uv on PATH" in result.stderr
    assert any(UV_ARCHIVE_URL_SUFFIX in line for line in harness.lines_starting("curl "))
    assert harness.lines_starting("uv tool install ") != []


def test_truncated_uv_at_home_is_not_trusted_and_rerun_heals(tmp_path):
    harness = Harness(tmp_path)
    broken = harness.home / ".local" / "bin" / "uv"
    broken.parent.mkdir(parents=True)
    broken.write_text(BROKEN_UV.format(log=harness.log), encoding="utf-8")
    broken.chmod(0o755)
    result = harness.run()
    assert result.returncode == 0
    # The broken candidate was probed, reported, and ignored.
    assert "uv --version" in harness.log_lines()
    assert "ignoring" in result.stderr
    # The bootstrap replaced it with the pinned download.
    assert any(UV_ARCHIVE_URL_SUFFIX in line for line in harness.lines_starting("curl "))
    assert broken.read_text(encoding="utf-8") != BROKEN_UV.format(log=harness.log)
    assert stat.S_IMODE(broken.stat().st_mode) == 0o755
    assert harness.lines_starting("uv tool install ") != []


def test_partial_uv_file_is_replaced_without_touching_it_in_place(tmp_path):
    """A read-only partial uv is staged around, not written through.

    cp onto the destination itself would fail on an unwritable leftover; the
    write goes to a temporary name in the same directory and an atomic `mv`
    replaces the old file, so a rerun after a crashed install still heals.
    """
    harness = Harness(tmp_path)
    partial = harness.home / ".local" / "bin" / "uv"
    partial.parent.mkdir(parents=True)
    partial.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    partial.chmod(0o444)
    result = harness.run()
    assert result.returncode == 0
    assert stat.S_IMODE(partial.stat().st_mode) == 0o755
    assert partial.read_text(encoding="utf-8") != "#!/bin/sh\nexit 1\n"
    assert sorted(p.name for p in partial.parent.iterdir()) == ["uv", "uvx"]
    assert harness.lines_starting("uv tool install ") != []


def test_children_that_must_not_read_the_script_do_not(tmp_path):
    """curl downloads, uv tool install and ciao setup see no script bytes.

    When the installer is piped to sh (`curl ... | sh`), unredirected stdin
    would be the remaining script text; a canary on stdin proves these
    children read nothing. ciao service install is deliberately not
    redirected: its sudo prompt needs the terminal.
    """
    harness = Harness(tmp_path)
    canary = harness.root / "stdin.canary"
    canary.write_bytes(STDIN_CANARY)
    with canary.open("rb") as stdin_source:
        result = harness.run(stdin=stdin_source)
    assert result.returncode == 0
    log = harness.log_lines()
    assert log.count("curl-stdin:") == 2
    assert log.count("uv-stdin:") == 1
    assert log.count("ciao-stdin:") == 1
    assert "ciao-service-stdin:STDIN-CANARY" in log
    for line in log:
        if line == "ciao-service-stdin:STDIN-CANARY":
            continue
        assert not line.endswith("STDIN-CANARY"), line


def test_no_service_hint_prints_the_absolute_ciao_path(tmp_path):
    harness = Harness(tmp_path)
    result = harness.run("--no-service")
    assert result.returncode == 0
    ciao_bin = harness.root / "staged" / "uvbin" / "ciao"
    assert (
        f"      {ciao_bin} service install" in result.stdout
    ), result.stdout


def test_script_parses_with_sh_n():
    subprocess.run(["/bin/sh", "-n", str(INSTALL_SH)], check=True, capture_output=True, text=True)
    dash = shutil.which("dash")
    if dash:
        subprocess.run([dash, "-n", str(INSTALL_SH)], check=True, capture_output=True, text=True)


def _workspace_with_receipt(tmp_path, status):
    workspace = tmp_path / "ws"
    migration = workspace / ".runtime" / "migration"
    migration.mkdir(parents=True)
    (migration / "workspace-rooting.json").write_text(f'{{"status": "{status}"}}', encoding="utf-8")
    if status == "migrated":
        (migration / "logs-split.json").write_text('{"status": "migrated"}', encoding="utf-8")
    return workspace


def test_unfinished_1_10_migration_stops_the_install_before_uv_replaces_the_engine(tmp_path):
    harness = Harness(tmp_path)
    workspace = _workspace_with_receipt(tmp_path, "partial")
    result = harness.run("--wheel", str(harness.local_wheel()), "--workspace", str(workspace))
    assert result.returncode == 4
    assert "Finish the 1.10 update first" in result.stderr
    assert "workspace-unmigrated" in result.stderr
    assert harness.lines_starting("uv tool") == []
    assert harness.lines_starting("ciao setup") == []


def test_unit_workspace_is_the_one_checked_when_no_flag_is_given(tmp_path):
    harness = Harness(tmp_path, with_unit=True)
    workspace = _workspace_with_receipt(tmp_path, "refused")
    harness.unit.write_text(f"WorkingDirectory={workspace}\nExecStart=/x\n", encoding="utf-8")
    result = harness.run("--wheel", str(harness.local_wheel()))
    assert result.returncode == 4
    assert harness.lines_starting("uv tool") == []


def test_finished_1_10_migration_lets_the_install_proceed(tmp_path):
    harness = Harness(tmp_path)
    workspace = _workspace_with_receipt(tmp_path, "migrated")
    result = harness.run("--wheel", str(harness.local_wheel()), "--workspace", str(workspace), "--no-service")
    assert result.returncode == 0, result.stderr
    assert harness.lines_starting("uv tool install") != []


def test_preflight_runs_isolated_from_the_current_directory(tmp_path):
    harness = Harness(tmp_path)
    workspace = _workspace_with_receipt(tmp_path, "migrated")
    harness.run("--wheel", str(harness.local_wheel()), "--workspace", str(workspace), "--no-service")
    (line,) = harness.lines_starting("uv run ")
    assert " python -P -m ciao.upgrade_preflight " in line
    # -P keeps a ./ciao folder in the current directory from shadowing the module.
    decoy = tmp_path / "decoy"
    (decoy / "ciao").mkdir(parents=True)
    (decoy / "ciao" / "__init__.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    refused = subprocess.run(
        [sys.executable, "-P", "-m", "ciao.upgrade_preflight", str(_workspace_with_receipt(tmp_path / "x", "refused"))],
        cwd=decoy, env={"PYTHONPATH": str(REPO_ROOT)}, capture_output=True, text=True,
    )
    assert refused.returncode == 4


def test_unit_and_flag_workspaces_are_both_checked(tmp_path):
    harness = Harness(tmp_path, with_unit=True)
    unit_ws = _workspace_with_receipt(tmp_path / "a", "partial")
    flag_ws = _workspace_with_receipt(tmp_path / "b", "migrated")
    harness.unit.write_text(f"WorkingDirectory={unit_ws}\nExecStart=/x\n", encoding="utf-8")
    result = harness.run("--wheel", str(harness.local_wheel()), "--workspace", str(flag_ws), "--no-service")
    assert result.returncode == 4
    assert str(unit_ws) in result.stderr
    assert harness.lines_starting("uv tool") == []
    # And the other way round: a clean unit, an unfinished --workspace.
    harness2 = Harness(tmp_path, name="second", with_unit=True)
    clean = _workspace_with_receipt(tmp_path / "c", "migrated")
    bad = _workspace_with_receipt(tmp_path / "d", "refused")
    harness2.unit.write_text(f"WorkingDirectory={clean}\nExecStart=/x\n", encoding="utf-8")
    result = harness2.run("--wheel", str(harness2.local_wheel()), "--workspace", str(bad), "--no-service")
    assert result.returncode == 4
    assert str(bad) in result.stderr
    assert harness2.lines_starting("uv tool") == []
