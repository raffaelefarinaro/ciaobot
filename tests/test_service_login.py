"""ciao.service_login: the sign-in state is read from the machine, or not at all.

Every OS call is faked here. Nothing in this file reaches a live ``launchctl``,
``schtasks.exe`` or the operator's real ``~/Library/LaunchAgents``, because the
seams the module uses for both platforms are patched rather than invoked, and
because the plist directory the read walks is the module's own
``_plist_path``.

What these tests are actually about is the refusals:

* a state nobody proved is ``None``, not a position the UI could render;
* a definition that does not provably serve *this* workspace is reported and
  never touched, and never inferred from the environment the way
  ``discover_runtime`` would;
* a change flips one enabled bit and nothing else — no start, stop, restart,
  bootstrap, register, ``/Create``, ``/Run``, ``/End``, ``/Delete``, and no
  plist or task XML is written;
* a change only counts when the re-read confirms it, so a runner that lies
  about succeeding produces a failure rather than a claim.
"""

from __future__ import annotations

import functools
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape

import pytest

from ciao import macos_service, service_login, windows_service

UID = 501
SERVER = macos_service.SERVER_LABEL
TASK = windows_service.TASK_NAME


# --------------------------------------------------------------------------- #
# Fakes. Each records its argv, so a test asserts what was run, not just that
# something was.
# --------------------------------------------------------------------------- #


class _Launchctl:
    """A ``launchctl`` that answers ``print-disabled`` from a script."""

    def __init__(self, listing: str = "{\n}", *, returncode: int = 0) -> None:
        self.listing = listing
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        if command[:2] == ["launchctl", "print-disabled"]:
            return subprocess.CompletedProcess(
                command,
                self.returncode,
                stdout=self.listing,
                stderr="Could not print disabled: 5: Input/output error"
                if self.returncode
                else "",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    @property
    def verbs(self) -> list[list[str]]:
        return [command[1] for command in self.calls]


class _Schtasks:
    """A ``schtasks`` that answers ``/Query /XML`` from a script of documents.

    Used as the ``runner`` of the real ``windows_service`` helpers, so the argv
    a test reads is the argv the module actually builds.
    """

    def __init__(
        self, *documents: str, returncode: int = 0, change_returncode: int = 0
    ) -> None:
        self.documents = list(documents)
        self.returncode = returncode
        self.change_returncode = change_returncode
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), dict(kwargs)))
        if "/Change" in args:
            return subprocess.CompletedProcess(
                ["schtasks.exe", *args], self.change_returncode, stdout="", stderr=""
            )
        index = sum(1 for argv, _ in self.calls if "/Query" in argv) - 1
        document = self.documents[min(index, len(self.documents) - 1)] if self.documents else ""
        return subprocess.CompletedProcess(
            ["schtasks.exe", *args],
            self.returncode,
            stdout=document,
            stderr="ERROR: The system cannot find the file specified."
            if self.returncode
            else "",
        )

    @property
    def argvs(self) -> list[list[str]]:
        return [list(argv) for argv, _kwargs in self.calls]

    def queries(self) -> int:
        return sum(1 for argv, _ in self.calls if "/Query" in argv)


# --------------------------------------------------------------------------- #
# The real shape of `launchctl print-disabled gui/<uid>`
# --------------------------------------------------------------------------- #

# Captured verbatim on a real macOS host (uid 501, 2026-10-02):
#
#     $ launchctl print-disabled "gui/$(id -u)"
#
# Every character matters to the reader, which is why this is a capture and not
# something written to look like one: a blank first line, a tab-indented
# `disabled services = {` header, one entry indented a level deeper, and a
# tab-indented closing brace. The output does not begin with a `{`, so a reader
# that gates on that reports Unknown on a Mac that has answered perfectly.
PRINT_DISABLED_REAL = (
    "\n"
    "\tdisabled services = {\n"
    '\t\t"com.docker.helper" => enabled\n'
    '\t\t"com.apple.ManagedClientAgent.enrollagent" => disabled\n'
    '\t\t"com.ollama.ollama" => enabled\n'
    '\t\t"com.meta.mqrd.launcher" => enabled\n'
    '\t\t"com.ciao.menubar" => disabled\n'
    '\t\t"com.apple.Siri.agent" => enabled\n'
    '\t\t"com.apple.FolderActionsDispatcher" => disabled\n'
    '\t\t"ing.paperclip.paperclipai" => enabled\n'
    '\t\t"ai.openclaw.gateway" => disabled\n'
    '\t\t"com.openai.chat-helper" => enabled\n'
    f'\t\t"{SERVER}" => enabled\n'
    '\t\t"com.apple.appleseed.seedusaged.postinstall" => disabled\n'
    '\t\t"com.apple.ScriptMenuApp" => disabled\n'
    '\t\t"com.logi.cp-dev-mgr" => enabled\n'
    '\t\t"Ciaobot" => disabled\n'
    "\t}\n"
)

# The capture above plus the second top-level section a launchd may print after
# it. The section header, its indentation and its closing brace follow the same
# convention the capture shows; the entries' values are the opaque
# shared-file-list references launchd puts there, not `enabled`/`disabled` words.
# It is the shape a slice-to-the-last-`}` reader gets wrong: that brace is the
# last one in the output and it closes a dictionary that is not ours.
PRINT_DISABLED_REAL_WITH_ASSOCIATIONS = PRINT_DISABLED_REAL + (
    "\n"
    "\tlogin item associations = {\n"
    '\t\t"com.ciao.server" => <LSSharedFileListItemSR:0x600002c0a1c2>\n'
    '\t\t"com.apple.loginwindow.autologinwindow" => <LSSharedFileListItemSR:0x600002c40d80>\n'
    "\t}\n"
)


# --------------------------------------------------------------------------- #
# Fixtures: a workspace, a plist dir, and the platform switches.
# --------------------------------------------------------------------------- #


def _plist(
    tmp_path: Path,
    *,
    label: str = SERVER,
    workspace: Path | None = None,
    run_at_load: Any = True,
    keep_alive: Any = True,
    disabled: Any = None,
    env_workspace: Path | None = None,
    drop_env: bool = False,
    drop_workdir: bool = False,
) -> Path:
    """Write a ``com.ciao.server.plist`` the way the installer writes one."""
    root = workspace if workspace is not None else tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": ["/opt/ciao/venv/bin/python", "-m", "ciao.cli", "supervise"],
        "RunAtLoad": run_at_load,
        "KeepAlive": keep_alive,
    }
    if not drop_workdir:
        body["WorkingDirectory"] = str(root)
    if not drop_env:
        body["EnvironmentVariables"] = {
            "CIAO_WORKSPACE": str(root if env_workspace is None else env_workspace),
        }
    if disabled is not None:
        body["Disabled"] = disabled
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    target = agents / f"{SERVER}.plist"
    target.write_bytes(plistlib.dumps(body))
    return target


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Path:
    """Point the live LaunchAgents dir at ``tmp_path`` and write a plist there."""
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(macos_service, "live_launch_agents_dir", lambda: agents)
    monkeypatch.setattr(service_login, "_uid", lambda: UID)
    return _plist(tmp_path, **kwargs)


def _status(
    monkeypatch: pytest.MonkeyPatch, platform: str, launchctl: Any = None, schtasks: _Schtasks | None = None
) -> Any:
    """Run ``login_status`` for *platform* with both OS seams faked.

    The Windows seam is faked one level lower — as the ``runner`` the real
    ``windows_service`` helper calls — so the argv a test sees is the one the
    module builds, not one the test supplied.
    """
    _install_schtasks(monkeypatch, schtasks or _Schtasks())
    monkeypatch.setattr(service_login, "_uid", lambda: UID)
    monkeypatch.setattr(
        service_login, "_launchctl_run", launchctl or _Launchctl("{\n}")
    )
    monkeypatch.setattr(sys, "platform", platform)
    return service_login.login_status


def _install_schtasks(monkeypatch: pytest.MonkeyPatch, schtasks: _Schtasks) -> None:
    """Route the module's Windows seams to *schtasks* through the real helpers."""
    monkeypatch.setattr(
        service_login,
        "_query_task_xml",
        functools.partial(windows_service.query_task_xml, runner=schtasks),
    )
    monkeypatch.setattr(
        service_login,
        "_set_task_enabled",
        functools.partial(windows_service.set_task_enabled, runner=schtasks),
    )


# --------------------------------------------------------------------------- #
# macOS: reading launchd's answer
# --------------------------------------------------------------------------- #


def test_a_disabled_override_is_the_next_sign_in_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    login_status = _status(
        monkeypatch, "darwin", launchctl=_Launchctl(f'{{\n\t"{SERVER}" => disabled\n}}')
    )
    workspace = tmp_path / "workspace"

    status = login_status(workspace)

    assert (status.platform, status.supported) == ("macos", True)
    assert (status.installed, status.enabled, status.can_change) == (True, False, True)
    assert status.setup_command is None
    assert "will not start" in status.reason


def test_an_enabled_override_is_the_next_sign_in_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    login_status = _status(
        monkeypatch, "darwin", launchctl=_Launchctl(f'{{\n\t"{SERVER}" => enabled\n}}')
    )

    status = login_status(tmp_path / "workspace")

    assert (status.installed, status.enabled, status.can_change) == (True, True, True)


def test_the_boolean_spelling_of_the_override_is_read_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older launchd wrote ``"label" => true``, where true means disabled."""
    _install(tmp_path, monkeypatch)
    login_status = _status(
        monkeypatch, "darwin", launchctl=_Launchctl(f'{{\n\t"{SERVER}" => true\n}}')
    )

    assert login_status(tmp_path / "workspace").enabled is False


def test_the_real_print_disabled_output_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The output this reader is written against, verbatim off a real Mac.

    launchd answers with a section — a blank line, then an indented
    `disabled services = {` header — so a reader that assumes a bare
    dictionary reports Unknown here and refuses every change, on a machine that
    has said exactly which services are disabled.
    """
    _install(tmp_path, monkeypatch)
    login_status = _status(monkeypatch, "darwin", launchctl=_Launchctl(PRINT_DISABLED_REAL))

    status = login_status(tmp_path / "workspace")

    assert (status.platform, status.installed, status.enabled, status.can_change) == (
        "macos",
        True,
        True,
        True,
    )
    assert "will start" in status.reason


def test_the_real_listing_reads_every_label_launchd_wrote() -> None:
    """Not one entry, and not just ours: a reader that drops lines is a reader
    that could drop ours."""
    listing = service_login._parse_disabled_listing(PRINT_DISABLED_REAL)

    assert listing is not None
    assert len(listing) == 15
    assert listing[SERVER] is False  # `enabled`, so not disabled
    assert listing["Ciaobot"] is True
    assert listing["com.docker.helper"] is False


def test_a_following_launchd_section_does_not_close_the_disabled_one() -> None:
    """`login item associations = { … }` may follow the section we read.

    Its closing brace is the last one in the output, and its entries name the
    same labels with opaque references rather than launchd's words. Slicing to
    the last `}` would read both dictionaries as one and reject the whole
    listing; taking the section's own closing brace reads the same 15 labels.
    """
    listing = service_login._parse_disabled_listing(PRINT_DISABLED_REAL_WITH_ASSOCIATIONS)

    assert listing == service_login._parse_disabled_listing(PRINT_DISABLED_REAL)
    assert listing is not None
    assert listing[SERVER] is False


def test_the_real_listing_disabled_is_the_next_sign_in_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same capture with our own label disabled, in the real shape."""
    _install(tmp_path, monkeypatch)
    listing = PRINT_DISABLED_REAL.replace(
        f'"{SERVER}" => enabled', f'"{SERVER}" => disabled'
    )
    login_status = _status(monkeypatch, "darwin", launchctl=_Launchctl(listing))

    status = login_status(tmp_path / "workspace")

    assert (status.installed, status.enabled, status.can_change) == (True, False, True)
    assert "will not start" in status.reason


def test_no_override_falls_back_to_the_plists_own_disabled_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent key is launchd's "not disabled", and a disabled *job* is not
    the question: the engine may well be running and still start at sign-in."""
    _install(tmp_path, monkeypatch, disabled=True)
    login_status = _status(monkeypatch, "darwin", launchctl=_Launchctl("{\n}"))

    assert login_status(tmp_path / "workspace").enabled is False


def test_no_override_and_no_plist_key_means_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    login_status = _status(monkeypatch, "darwin", launchctl=_Launchctl("{\n}"))

    assert login_status(tmp_path / "workspace").enabled is True


def test_other_labels_in_the_listing_are_not_our_business(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    login_status = _status(
        monkeypatch,
        "darwin",
        launchctl=_Launchctl('{\n\t"com.apple.other" => disabled\n}'),
    )

    assert login_status(tmp_path / "workspace").enabled is True


@pytest.mark.parametrize(
    "listing",
    [
        '{\n\t"com.ciao.server" => maybe\n}',   # a word we do not know
        '{\n\t"com.ciao.server" => enabled\n',  # truncated
        "com.ciao.server => enabled",             # not a dictionary
        "",                                        # nothing at all
        "{\n\tsomething unexpected here\n}",       # a line we cannot read
        '{\n\t"com.ciao.server" => enabled\n\t"com.ciao.server" => disabled\n}',
        # The same five failures in the shape launchd really prints: the section
        # is there, and it is still not something we may read.
        "\n\tdisabled services = {\n"
        f'\t\t"{SERVER}" => maybe\n'
        "\t}\n",
        "\n\tdisabled services = {\n"
        f'\t\t"{SERVER}" => enabled\n',             # never closed
        "\n\tdisabled services = {\n"
        "\t\tsomething unexpected here\n"
        "\t}\n",
        "\n\tdisabled services = {\n"
        f'\t\t"{SERVER}" => enabled\n'
        f'\t\t"{SERVER}" => disabled\n'
        "\t}\n",
        "\n\tdisabled services = {\n"
        f'\t\t"{SERVER}" => enabled\n'
        "\t} and then some prose\n",                 # not a brace, only prose
    ],
    ids=[
        "unknown-word",
        "unterminated",
        "not-a-dict",
        "empty",
        "unreadable-line",
        "contradictory",
        "section-unknown-word",
        "section-unterminated",
        "section-unreadable-line",
        "section-contradictory",
        "section-trailing-prose",
    ],
)
def test_a_print_disabled_listing_we_cannot_read_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    _install(tmp_path, monkeypatch)
    login_status = _status(monkeypatch, "darwin", launchctl=_Launchctl(listing))

    status = login_status(tmp_path / "workspace")

    assert status.enabled is None
    assert status.can_change is False
    assert "unknown" in status.reason
    assert status.setup_command is None


def test_a_failed_print_disabled_is_unknown_with_its_own_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    login_status = _status(
        monkeypatch, "darwin", launchctl=_Launchctl("", returncode=5)
    )

    status = login_status(tmp_path / "workspace")

    assert (status.installed, status.enabled, status.can_change) == (True, None, False)
    assert "Input/output error" in status.reason


def test_a_launchctl_that_cannot_be_run_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)

    def _explode(_argv: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("no launchctl here")

    login_status = _status(monkeypatch, "darwin", launchctl=_explode)

    status = login_status(tmp_path / "workspace")

    assert status.enabled is None
    assert "no launchctl here" in status.reason


def test_a_launchctl_that_times_out_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`TimeoutExpired` is not an `OSError`, so a wedged launchd would escape a
    status read as an exception — a 500 for a question the machine did not
    answer. It answers Unknown, with the bound in the reason."""
    _install(tmp_path, monkeypatch)

    def _wedged(_argv: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=["launchctl", "print-disabled", f"gui/{UID}"],
            timeout=service_login._LAUNCHCTL_TIMEOUT_S,
        )

    login_status = _status(monkeypatch, "darwin", launchctl=_wedged)

    status = login_status(tmp_path / "workspace")

    assert (status.installed, status.enabled, status.can_change) == (True, None, False)
    assert "timed out" in status.reason
    assert "unknown" in status.reason


def test_the_launchctl_seam_reports_a_timeout_as_a_command_it_could_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`macos_service.set_login_enabled` catches `OSError` and nothing else, so
    the bound has to be reported the way `_schtasks` reports its own: a failure
    the caller already knows how to refuse, never a bare `TimeoutExpired`."""
    argv = ["print-disabled", f"gui/{UID}"]

    def _never_answers(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["launchctl", *argv], timeout=10.0)

    monkeypatch.setattr(service_login.subprocess, "run", _never_answers)

    with pytest.raises(OSError) as raised:
        service_login._launchctl_run(argv)

    assert "launchctl print-disabled" in str(raised.value)
    assert f"timed out after {service_login._LAUNCHCTL_TIMEOUT_S:g}s" in str(raised.value)


def test_a_launchctl_that_wedges_during_the_change_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path has a second caller that only catches `OSError`. A
    launchctl that stops answering between the read and the change is a change
    this module could not confirm: 503, not a 500 and not a claim."""
    _install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_login, "_uid", lambda: UID)
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[list[str]] = []

    def _never_answers(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(arg) for arg in args[0]]
        calls.append(argv)
        if argv[1] != "disable":
            return subprocess.CompletedProcess(argv, 0, stdout=PRINT_DISABLED_REAL, stderr="")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10.0)

    monkeypatch.setattr(service_login.subprocess, "run", _never_answers)

    with pytest.raises(service_login.LoginUnavailable) as raised:
        service_login.set_login_enabled(tmp_path / "workspace", False)

    assert "timed out" in str(raised.value)
    assert raised.value.status is not None
    assert raised.value.status.enabled is True
    # Read, write, and the fresh read the refusal carries back. No kickstart,
    # no bootstrap, no bootout.
    assert [command[1] for command in calls] == [
        "print-disabled",
        "disable",
        "print-disabled",
    ]


def test_the_read_uses_the_live_plist_dir_not_the_test_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CIAO_LAUNCH_AGENTS_DIR` is where an install *writes*; the next sign-in
    is decided by the definition in the real per-user dir."""
    _install(tmp_path, monkeypatch)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / f"{SERVER}.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": SERVER,
                "WorkingDirectory": str(tmp_path / "somewhere-else"),
                "RunAtLoad": True,
            }
        )
    )
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(shadow))
    login_status = _status(
        monkeypatch, "darwin", launchctl=_Launchctl(f'{{\n\t"{SERVER}" => enabled\n}}')
    )

    status = login_status(tmp_path / "workspace")

    assert status.enabled is True
    assert status.can_change is True


# --------------------------------------------------------------------------- #
# macOS: ownership, and the definitions that are not ours
# --------------------------------------------------------------------------- #


def test_a_missing_plist_offers_the_setup_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    monkeypatch.setattr(macos_service, "live_launch_agents_dir", lambda: agents)
    login_status = _status(monkeypatch, "darwin")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    status = login_status(workspace)

    assert (status.installed, status.enabled, status.can_change) == (False, None, False)
    # Quoted, so a workspace with a space in it survives being pasted into a
    # shell; nothing here ever runs the command.
    assert status.setup_command == (
        f"ciao service start --workspace {shlex.quote(str(workspace))}"
    )
    spaced = tmp_path / "my workspace"
    spaced.mkdir()
    assert f"'{spaced}'" in str(login_status(spaced).setup_command)


def test_a_malformed_plist_is_unknown_not_uninstalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / f"{SERVER}.plist").write_bytes(b"<?xml version=")
    monkeypatch.setattr(macos_service, "live_launch_agents_dir", lambda: agents)
    login_status = _status(monkeypatch, "darwin")

    status = login_status(tmp_path / "workspace")

    assert (status.installed, status.enabled, status.can_change) == (None, None, False)
    assert "plist dictionary" in status.reason
    # Nothing that would overwrite a definition we could not read.
    assert status.setup_command is None


def test_a_plist_for_another_label_is_not_this_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, label="com.example.other")
    login_status = _status(monkeypatch, "darwin")

    status = login_status(tmp_path / "workspace")

    assert (status.installed, status.enabled, status.can_change) == (None, None, False)
    assert "not this engine's service" in status.reason


def test_a_plist_naming_no_workspace_is_not_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment fallback in `discover_runtime` would answer for this one."""
    _install(tmp_path, monkeypatch, drop_env=True, drop_workdir=True)
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    login_status = _status(monkeypatch, "darwin")

    status = login_status(workspace)

    assert (status.installed, status.enabled, status.can_change) == (True, None, False)
    assert "names no workspace" in status.reason


def test_a_plist_for_another_workspace_is_reported_not_repointed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other-workspace"
    other.mkdir()
    _install(tmp_path, monkeypatch, workspace=other)
    login_status = _status(
        monkeypatch, "darwin", launchctl=_Launchctl(f'{{\n\t"{SERVER}" => enabled\n}}')
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    status = login_status(workspace)

    assert (status.installed, status.enabled, status.can_change) == (True, None, False)
    assert str(other) in status.reason
    # No way to make this engine own that service.
    assert status.setup_command is None


def test_two_workspace_values_that_disagree_are_not_a_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other-workspace"
    other.mkdir()
    _install(
        tmp_path,
        monkeypatch,
        workspace=other,
        env_workspace=tmp_path / "workspace",
    )
    login_status = _status(monkeypatch, "darwin")

    status = login_status(tmp_path / "workspace")

    assert status.enabled is None
    assert str(other) in status.reason


def test_a_plist_without_a_login_trigger_is_a_manual_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No RunAtLoad and no KeepAlive: enabling it would not start anything at
    the next sign-in, so reporting On would be a lie about the switch."""
    _install(tmp_path, monkeypatch, run_at_load=False, keep_alive=False)
    login_status = _status(
        monkeypatch, "darwin", launchctl=_Launchctl(f'{{\n\t"{SERVER}" => enabled\n}}')
    )

    status = login_status(tmp_path / "workspace")

    assert (status.installed, status.enabled, status.can_change) == (True, None, False)
    assert "RunAtLoad" in status.reason


def test_keep_alive_alone_counts_as_a_login_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, run_at_load=False)
    login_status = _status(monkeypatch, "darwin")

    assert login_status(tmp_path / "workspace").enabled is True


def test_a_non_boolean_disabled_key_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, disabled="yes")
    login_status = _status(monkeypatch, "darwin", launchctl=_Launchctl("{\n}"))

    status = login_status(tmp_path / "workspace")

    assert status.enabled is None
    assert "not a boolean" in status.reason


def test_an_unanswerable_read_refuses_the_change_before_it_tries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled *job* is not the question, and an unreadable definition is
    not an answer. Neither may be turned into a switch."""
    _install(tmp_path, monkeypatch, disabled="yes")
    launchctl = _Launchctl("{\n}")
    _status(monkeypatch, "darwin", launchctl=launchctl)
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(service_login.LoginRefused) as raised:
        service_login.set_login_enabled(tmp_path / "workspace", True)

    assert raised.value.status is not None
    assert raised.value.status.enabled is None
    assert launchctl.verbs == ["print-disabled"]


# --------------------------------------------------------------------------- #
# macOS: the write
# --------------------------------------------------------------------------- #


def test_disabling_runs_only_launchctl_disable_and_re_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    launchctl = _Launchctl(f'{{\n\t"{SERVER}" => disabled\n}}')
    _status(monkeypatch, "darwin", launchctl=launchctl)
    monkeypatch.setattr(sys, "platform", "darwin")

    status = service_login.set_login_enabled(tmp_path / "workspace", False)

    assert status.enabled is False
    assert launchctl.calls == [
        ["launchctl", "print-disabled", f"gui/{UID}"],
        ["launchctl", "disable", f"gui/{UID}/{SERVER}"],
        ["launchctl", "print-disabled", f"gui/{UID}"],
    ]


def test_enabling_runs_only_launchctl_enable_and_re_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    states = iter(
        [
            f'{{\n\t"{SERVER}" => disabled\n}}',
            f'{{\n\t"{SERVER}" => enabled\n}}',
        ]
    )
    launchctl = _Launchctl("")
    launchctl.listing = ""  # type: ignore[assignment]

    class _Scripted(_Launchctl):
        def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            command = list(argv)
            self.calls.append(command)
            if command[:2] == ["launchctl", "print-disabled"]:
                return subprocess.CompletedProcess(
                    command, 0, stdout=next(states), stderr=""
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    scripted = _Scripted()
    monkeypatch.setattr(service_login, "_launchctl_run", scripted)
    monkeypatch.setattr(service_login, "_uid", lambda: UID)
    monkeypatch.setattr(sys, "platform", "darwin")

    status = service_login.set_login_enabled(tmp_path / "workspace", True)

    assert status.enabled is True
    assert [command[1] for command in scripted.calls] == [
        "print-disabled",
        "enable",
        "print-disabled",
    ]


def test_a_launchctl_failure_is_unavailable_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_login, "_uid", lambda: UID)
    monkeypatch.setattr(sys, "platform", "darwin")

    class _Refusing(_Launchctl):
        def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            command = list(argv)
            self.calls.append(command)
            if command[1] == "enable":
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="Could not disable service: 1: Operation not permitted"
                )
            return subprocess.CompletedProcess(
                command, 0, stdout=f'{{\n\t"{SERVER}" => enabled\n}}', stderr=""
            )

    refusing = _Refusing()
    monkeypatch.setattr(service_login, "_launchctl_run", refusing)

    with pytest.raises(service_login.LoginUnavailable) as raised:
        service_login.set_login_enabled(tmp_path / "workspace", True)

    assert "Operation not permitted" in str(raised.value)
    assert raised.value.status is not None
    assert [command[1] for command in refusing.calls] == [
        "print-disabled",
        "enable",
        "print-disabled",
    ]


def test_a_change_the_re_read_does_not_confirm_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """launchctl can exit 0 and still leave the job where it was."""
    _install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_login, "_uid", lambda: UID)
    monkeypatch.setattr(sys, "platform", "darwin")

    class _Lying(_Launchctl):
        def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            command = list(argv)
            self.calls.append(command)
            if command[1] == "print-disabled":
                # Never changes: the write's re-read still says disabled.
                return subprocess.CompletedProcess(
                    command, 0, stdout=f'{{\n\t"{SERVER}" => disabled\n}}', stderr=""
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(service_login, "_launchctl_run", _Lying())

    with pytest.raises(service_login.LoginUnavailable) as raised:
        service_login.set_login_enabled(tmp_path / "workspace", True)

    assert raised.value.status is not None
    assert raised.value.status.enabled is False


def test_a_write_over_an_unowned_plist_never_reaches_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is settled from the definition itself, so a definition for
    another workspace is refused before launchd is even asked anything."""
    other = tmp_path / "other-workspace"
    other.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _install(tmp_path, monkeypatch, workspace=other)
    launchctl = _Launchctl(f'{{\n\t"{SERVER}" => enabled\n}}')
    _status(monkeypatch, "darwin", launchctl=launchctl)
    monkeypatch.setattr(sys, "platform", "darwin")
    before = (tmp_path / "LaunchAgents" / f"{SERVER}.plist").read_bytes()

    with pytest.raises(service_login.LoginRefused) as raised:
        service_login.set_login_enabled(workspace, False)

    assert str(other) in str(raised.value)
    assert launchctl.calls == []
    assert (tmp_path / "LaunchAgents" / f"{SERVER}.plist").read_bytes() == before


def test_nothing_is_run_when_the_definition_has_no_login_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, run_at_load=False, keep_alive=False)
    launchctl = _Launchctl(f'{{\n\t"{SERVER}" => enabled\n}}')
    _status(monkeypatch, "darwin", launchctl=launchctl)
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(service_login.LoginRefused):
        service_login.set_login_enabled(tmp_path / "workspace", True)

    assert launchctl.verbs == []


# --------------------------------------------------------------------------- #
# Windows: the registered task's own document
# --------------------------------------------------------------------------- #


TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# A fixed ASCII workspace, the shape a real `C:\\...` path arrives in. It does
# not have to exist: both sides of every comparison go through the same
# resolve, and a Windows CI account whose name is not ASCII must not turn a
# workspace-identity test into a "the code page mangled it" one.
WIN_ROOT = Path("C:/ciao")
WIN_OTHER = Path("C:/ciao-other")


def _task_xml(
    workspace: Path | None = None,
    *,
    enabled: str = "true",
    logon: str = "true",
    actions: int = 1,
    workdir: str | None = None,
) -> str:
    """A task document shaped like the one `render_task_xml` registers."""
    root = workspace if workspace is not None else Path("C:/ciao")
    body = "".join(
        "    <Exec>\n      <Command>pythonw.exe</Command>\n"
        "      <Arguments>-m ciao.cli supervise</Arguments>\n"
        f"      <WorkingDirectory>{escape(workdir if workdir is not None else str(root))}"
        "</WorkingDirectory>\n    </Exec>\n"
        for _ in range(actions)
    )
    if logon == "missing":
        trigger = "    <IdleTrigger>\n      <Enabled>true</Enabled>\n    </IdleTrigger>\n"
    else:
        trigger = (
            "    <LogonTrigger>\n"
            f"      <Enabled>{logon}</Enabled>\n"
            "      <UserId>DESKTOP-1\\ada</UserId>\n"
            "    </LogonTrigger>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{TASK_NS}">\n'
        "  <RegistrationInfo><URI>\\Ciaobot\\Engine</URI></RegistrationInfo>\n"
        f"  <Triggers>\n{trigger}  </Triggers>\n"
        "  <Settings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        f"    <Enabled>{enabled}</Enabled>\n"
        "  </Settings>\n"
        f'  <Actions Context="Author">\n{body}  </Actions>\n'
        "</Task>\n"
    )


def test_a_registered_task_that_is_enabled_and_fires_at_logon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schtasks = _Schtasks(_task_xml(WIN_ROOT, enabled="true"))
    login_status = _status(monkeypatch, "win32", schtasks=schtasks)

    status = login_status(WIN_ROOT)

    assert (status.platform, status.supported) == ("windows", True)
    assert (status.installed, status.enabled, status.can_change) == (True, True, True)
    assert schtasks.argvs == [["/Query", "/TN", TASK, "/XML"]]
    assert schtasks.calls[0][1] == {"encoding": "oem"}


def test_a_registered_task_that_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_status = _status(
        monkeypatch, "win32", schtasks=_Schtasks(_task_xml(WIN_ROOT, enabled="false"))
    )

    status = login_status(WIN_ROOT)

    assert (status.installed, status.enabled, status.can_change) == (True, False, True)


@pytest.mark.parametrize("logon", ["false", "missing"])
def test_a_task_with_no_enabled_logon_trigger_offers_no_toggle(
    monkeypatch: pytest.MonkeyPatch, logon: str
) -> None:
    """`/Enable` alone would leave it still not starting at sign-in."""
    document = _task_xml(WIN_ROOT, logon=logon)
    login_status = _status(monkeypatch, "win32", schtasks=_Schtasks(document))

    status = login_status(WIN_ROOT)

    assert (status.installed, status.enabled, status.can_change) == (True, None, False)
    assert "logon trigger" in status.reason


def test_a_task_for_another_workspace_is_reported_not_repointed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = WIN_OTHER
    workspace = WIN_ROOT
    login_status = _status(monkeypatch, "win32", schtasks=_Schtasks(_task_xml(other)))

    status = login_status(workspace)

    assert (status.installed, status.enabled, status.can_change) == (True, None, False)
    assert str(other) in status.reason
    assert status.setup_command is None


@pytest.mark.parametrize(
    "workspace_text,reason",
    [
        ("C:/ciao/Łukasz", "lossy code page"),
        ("C:/ciao/�", "lossy code page"),
    ],
)
def test_a_task_workspace_the_code_page_mangled_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    workspace_text: str,
    reason: str,
) -> None:
    login_status = _status(
        monkeypatch,
        "win32",
        schtasks=_Schtasks(_task_xml(WIN_ROOT, workdir=workspace_text)),
    )

    status = login_status(WIN_ROOT)

    assert status.enabled is None
    assert reason in status.reason


def test_ambiguous_actions_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_status = _status(
        monkeypatch, "win32", schtasks=_Schtasks(_task_xml(WIN_ROOT, actions=2))
    )

    status = login_status(WIN_ROOT)

    assert (status.installed, status.enabled) == (True, None)
    assert "Exec actions" in status.reason


def test_a_task_with_no_working_directory_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_status = _status(
        monkeypatch,
        "win32",
        schtasks=_Schtasks(_task_xml(WIN_ROOT, workdir="")),
    )

    status = login_status(WIN_ROOT)

    assert status.enabled is None
    assert "working directory" in status.reason


def test_a_task_whose_enabled_key_is_not_explicit_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_status = _status(
        monkeypatch, "win32", schtasks=_Schtasks(_task_xml(WIN_ROOT, enabled="1"))
    )

    status = login_status(WIN_ROOT)

    assert status.enabled is None
    assert "explicit true or false" in status.reason


def test_a_task_with_no_settings_at_all_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _task_xml(WIN_ROOT).replace(
        "  <Settings>\n    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n  </Settings>\n",
        "",
    )
    login_status = _status(monkeypatch, "win32", schtasks=_Schtasks(document))

    assert login_status(WIN_ROOT).enabled is None


@pytest.mark.parametrize(
    "document,reason",
    [
        ("", "not XML"),
        ("<Task>", "not XML"),
        ("<NotATask/>", "not a Task Scheduler task document"),
    ],
    ids=["empty", "truncated", "wrong-root"],
)
def test_a_document_we_cannot_read_leaves_installed_unknown(
    monkeypatch: pytest.MonkeyPatch, document: str, reason: str
) -> None:
    login_status = _status(monkeypatch, "win32", schtasks=_Schtasks(document))

    status = login_status(WIN_ROOT)

    assert (status.installed, status.enabled, status.can_change) == (None, None, False)
    assert reason in status.reason
    # A failed query must not be read as "there is no task"; it says so.
    assert status.setup_command == f'schtasks.exe /Query /TN "{TASK}" /XML'


def test_a_query_that_fails_does_not_claim_a_missing_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_status = _status(
        monkeypatch, "win32", schtasks=_Schtasks("", returncode=1)
    )

    status = login_status(WIN_ROOT)

    assert (status.installed, status.enabled, status.can_change) == (None, None, False)
    assert "cannot find the file specified" in status.reason
    assert "ciao setup" in status.reason


def test_a_query_that_times_out_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout() -> subprocess.CompletedProcess[str]:
        raise windows_service.WindowsServiceError("schtasks /Query timed out after 30s")

    monkeypatch.setattr(service_login, "_query_task_xml", _timeout)
    monkeypatch.setattr(service_login, "_uid", lambda: UID)
    monkeypatch.setattr(sys, "platform", "win32")

    status = service_login.login_status(WIN_ROOT)

    assert (status.installed, status.enabled) == (None, None)
    assert "timed out" in status.reason


# --------------------------------------------------------------------------- #
# Windows: the write
# --------------------------------------------------------------------------- #


def _windows_set(
    monkeypatch: pytest.MonkeyPatch, schtasks: _Schtasks
) -> None:
    """Both Windows seams routed through the real helpers onto *schtasks*."""
    _install_schtasks(monkeypatch, schtasks)
    monkeypatch.setattr(service_login, "_uid", lambda: UID)
    monkeypatch.setattr(sys, "platform", "win32")


def test_enabling_runs_only_change_enable_and_re_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schtasks = _Schtasks(
        _task_xml(WIN_ROOT, enabled="false"),
        _task_xml(WIN_ROOT, enabled="true"),
    )
    _windows_set(monkeypatch, schtasks)

    status = service_login.set_login_enabled(WIN_ROOT, True)

    assert status.enabled is True
    # Read, write, read. Nothing that registers, starts, ends or deletes.
    assert schtasks.argvs == [
        ["/Query", "/TN", TASK, "/XML"],
        ["/Change", "/TN", TASK, "/Enable"],
        ["/Query", "/TN", TASK, "/XML"],
    ]


def test_disabling_runs_only_change_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schtasks = _Schtasks(
        _task_xml(WIN_ROOT, enabled="true"),
        _task_xml(WIN_ROOT, enabled="false"),
    )
    _windows_set(monkeypatch, schtasks)

    status = service_login.set_login_enabled(WIN_ROOT, False)

    assert status.enabled is False
    assert [argv[3] for argv in schtasks.argvs if argv[0] == "/Change"] == ["/Disable"]


def test_a_change_the_schtasks_helper_makes_is_bounded_and_verbatim() -> None:
    """The helper keeps the fixed task name and the fixed verb, and adds no
    shell and no unbounded call of its own."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def _runner(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), dict(kwargs)))
        return subprocess.CompletedProcess(["schtasks.exe", *args], 0, stdout="", stderr="")

    windows_service.set_task_enabled(True, runner=_runner)
    windows_service.set_task_enabled(False, runner=_runner)

    assert [argv for argv, _ in calls] == [
        ["/Change", "/TN", TASK, "/Enable"],
        ["/Change", "/TN", TASK, "/Disable"],
    ]


def test_a_refused_change_raises_on_the_helpers_own_terms() -> None:
    calls: list[list[str]] = []

    def _runner(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(
            ["schtasks.exe", *args], 5, stdout="", stderr="Access is denied."
        )

    with pytest.raises(windows_service.WindowsServiceError) as raised:
        windows_service.set_task_enabled(True, runner=_runner)

    assert "Access is denied" in str(raised.value)
    assert calls == [["/Change", "/TN", TASK, "/Enable"]]


def test_a_schtasks_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schtasks = _Schtasks(
        _task_xml(WIN_ROOT, enabled="false"),
        _task_xml(WIN_ROOT, enabled="false"),
        change_returncode=5,
    )
    _windows_set(monkeypatch, schtasks)

    with pytest.raises(service_login.LoginUnavailable) as raised:
        service_login.set_login_enabled(WIN_ROOT, True)

    assert "/Change failed" in str(raised.value)
    assert raised.value.status is not None
    assert raised.value.status.enabled is False
    assert [argv[0] for argv in schtasks.argvs] == ["/Query", "/Change", "/Query"]


def test_a_change_the_re_query_does_not_confirm_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The document never changes: /Change succeeded, the task did not move.
    schtasks = _Schtasks(_task_xml(WIN_ROOT, enabled="false"))
    _windows_set(monkeypatch, schtasks)

    with pytest.raises(service_login.LoginUnavailable) as raised:
        service_login.set_login_enabled(WIN_ROOT, True)

    assert raised.value.status is not None
    assert raised.value.status.enabled is False
    assert schtasks.queries() == 2


def test_nothing_is_changed_when_the_task_is_not_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schtasks = _Schtasks(_task_xml(WIN_OTHER))
    _windows_set(monkeypatch, schtasks)

    with pytest.raises(service_login.LoginRefused) as raised:
        service_login.set_login_enabled(WIN_ROOT, False)

    assert str(WIN_OTHER) in str(raised.value)
    assert schtasks.argvs == [["/Query", "/TN", TASK, "/XML"]]


# --------------------------------------------------------------------------- #
# Linux and the rest: a stated answer, with no OS call at all
# --------------------------------------------------------------------------- #


def test_linux_is_unsupported_and_never_asks_the_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_os(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("Linux must not reach an OS command")

    monkeypatch.setattr(service_login, "_launchctl_run", _no_os)
    monkeypatch.setattr(service_login, "_query_task_xml", _no_os)
    monkeypatch.setattr(service_login, "_uid", _no_os)
    monkeypatch.setattr(sys, "platform", "linux")

    status = service_login.login_status(tmp_path / "workspace")

    assert (status.platform, status.supported) == ("linux", False)
    assert (status.installed, status.enabled, status.can_change) == (None, None, False)
    assert status.setup_command is None
    assert "docs/LINUX.md" in status.reason


def test_linux_refuses_a_change_without_touching_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_os(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("Linux must not reach an OS command")

    monkeypatch.setattr(service_login, "_launchctl_run", _no_os)
    monkeypatch.setattr(service_login, "_set_task_enabled", _no_os)
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(service_login.LoginRefused) as raised:
        service_login.set_login_enabled(tmp_path / "workspace", True)

    assert raised.value.status is not None
    assert raised.value.status.platform == "linux"


def test_a_platform_with_no_backend_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_login, "_launchctl_run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "platform", "sunos5")

    status = service_login.login_status(tmp_path / "workspace")

    assert (status.platform, status.supported) == ("other", False)
    assert "no service backend" in status.reason


# --------------------------------------------------------------------------- #
# The wire shape
# --------------------------------------------------------------------------- #


def test_as_dict_is_the_dataclass_fields_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    payload = service_login.login_status(tmp_path / "workspace").as_dict()

    assert payload == {
        "platform": "linux",
        "supported": False,
        "installed": None,
        "enabled": None,
        "can_change": False,
        "reason": payload["reason"],
        "setup_command": None,
    }
    assert set(payload) == {
        "platform",
        "supported",
        "installed",
        "enabled",
        "can_change",
        "reason",
        "setup_command",
    }
