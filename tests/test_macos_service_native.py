"""The native-only launchd primitives: fresh bootstrap, strict probes.

Nothing here is wired into the legacy installer, CLI or routes. These tests pin
the new functions that the native handoff (#2339) will call once #2340 supplies
an explicit native context:

* absence is only launchd's exact qualified answer at status 113;
* a disabled override is existing configuration, read only, never cleared;
* fresh activation never unloads, boots out, reloads or kickstarts;
* the definition is digested before the first probe and immediately before the
  bootstrap.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ciao import macos_service

UID = 501
LABEL = macos_service.SERVER_LABEL
LAUNCHCTL = macos_service.LAUNCHCTL


def _absence_stderr(uid: int = UID, label: str = LABEL) -> str:
    return (
        "Bad request.\n"
        f'Could not find service "{label}" in domain for user gui: {uid}'
    )


def _listing(*entries: tuple[str, str]) -> str:
    body = "".join(f'\t\t"{label}" => {word}\n' for label, word in entries)
    return f"\n\tdisabled services = {{\n{body}\t}}\n"


def _definition(tmp_path: Path) -> tuple[Path, str]:
    definition = tmp_path / f"{LABEL}.plist"
    definition.write_bytes(b"<plist version=\"1.0\"><dict/></plist>")
    return definition, hashlib.sha256(definition.read_bytes()).hexdigest()


class _Launchctl:
    """A launchctl double that records argv and answers the three native verbs."""

    def __init__(
        self,
        *,
        absent: bool = True,
        disabled: bool = False,
        bootstrap_returncode: int = 0,
        bootstrap_stderr: str = "",
        on_print=None,
    ) -> None:
        self.absent = absent
        self.disabled = disabled
        self.bootstrap_returncode = bootstrap_returncode
        self.bootstrap_stderr = bootstrap_stderr
        self.on_print = on_print
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append(command)
        if command[:2] == [LAUNCHCTL, "print"]:
            if self.on_print is not None:
                self.on_print()
            if self.absent:
                return subprocess.CompletedProcess(
                    command, 113, stdout="", stderr=_absence_stderr()
                )
            return subprocess.CompletedProcess(
                command, 0, stdout=f"gui/{UID}/{LABEL} = {{ ... }}\n", stderr=""
            )
        if command[:2] == [LAUNCHCTL, "print-disabled"]:
            listing = _listing((LABEL, "disabled")) if self.disabled else _listing()
            return subprocess.CompletedProcess(command, 0, stdout=listing, stderr="")
        if command[:2] == [LAUNCHCTL, "bootstrap"]:
            return subprocess.CompletedProcess(
                command,
                self.bootstrap_returncode,
                stdout="",
                stderr=self.bootstrap_stderr
                or ("Bootstrap failed: 5: Input/output error" if self.bootstrap_returncode else ""),
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    @property
    def verbs(self) -> list[str]:
        return [command[1] for command in self.calls]


def _patch(monkeypatch: pytest.MonkeyPatch, launchctl: _Launchctl) -> None:
    monkeypatch.setattr(macos_service, "_getuid", lambda: UID)
    monkeypatch.setattr(macos_service.subprocess, "run", launchctl)


def test_server_job_absent_accepts_the_recorded_real_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launchctl = _Launchctl(absent=True)
    _patch(monkeypatch, launchctl)

    assert macos_service.server_job_absent() is True
    assert launchctl.calls == [[LAUNCHCTL, "print", f"gui/{UID}/{LABEL}"]]


@pytest.mark.parametrize(
    ("code", "stdout", "stderr"),
    [
        (113, "", _absence_stderr(label=f"{LABEL}X")),
        (113, "", _absence_stderr(uid=UID + 1)),
        (113, "", _absence_stderr() + "\nUnrelated failure"),
        (113, "Could not find service\n", _absence_stderr()),
        (37, "", _absence_stderr()),
        (113, "", "Bad request.\nBootstrap failed: 5: Input/output error"),
    ],
)
def test_server_job_absent_refuses_anything_but_the_qualified_answer(
    monkeypatch: pytest.MonkeyPatch, code: int, stdout: str, stderr: str
) -> None:
    monkeypatch.setattr(macos_service, "_getuid", lambda: UID)
    monkeypatch.setattr(
        macos_service.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, code, stdout=stdout, stderr=stderr
        ),
    )

    with pytest.raises(RuntimeError, match="did not confirm"):
        macos_service.server_job_absent()


def test_launchd_override_disabled_reads_the_listing_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launchctl = _Launchctl(disabled=True)
    _patch(monkeypatch, launchctl)

    assert macos_service.launchd_override_disabled() is True
    assert launchctl.calls == [[LAUNCHCTL, "print-disabled", f"gui/{UID}"]]
    assert launchctl.verbs == ["print-disabled"]


def test_launchd_override_disabled_answers_false_when_the_label_is_not_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launchctl = _Launchctl(disabled=False)
    _patch(monkeypatch, launchctl)

    assert macos_service.launchd_override_disabled() is False


@pytest.mark.parametrize(
    "completed",
    [
        subprocess.CompletedProcess([LAUNCHCTL], 5, stdout="", stderr="no answer"),
        subprocess.CompletedProcess([LAUNCHCTL], 0, stdout="not a listing", stderr=""),
        subprocess.CompletedProcess(
            [LAUNCHCTL], 0, stdout=_listing((LABEL, "maybe")), stderr=""
        ),
    ],
)
def test_launchd_override_disabled_refuses_uncertainty(
    monkeypatch: pytest.MonkeyPatch, completed: subprocess.CompletedProcess[str]
) -> None:
    monkeypatch.setattr(macos_service, "_getuid", lambda: UID)
    monkeypatch.setattr(
        macos_service.subprocess, "run", lambda command, **_kwargs: completed
    )

    with pytest.raises(RuntimeError):
        macos_service.launchd_override_disabled()


def test_launchd_override_disabled_bounds_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def fake(command, **kwargs):
        seen.append(dict(kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=_listing(), stderr="")

    monkeypatch.setattr(macos_service, "_getuid", lambda: UID)
    monkeypatch.setattr(macos_service.subprocess, "run", fake)

    assert macos_service.launchd_override_disabled() is False
    assert seen[0]["timeout"] == macos_service.JOB_PROBE_TIMEOUT_S


def test_fresh_activation_bootstraps_only_after_the_qualified_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definition, digest = _definition(tmp_path)
    launchctl = _Launchctl()
    _patch(monkeypatch, launchctl)

    assert macos_service.load_fresh_agent(definition, expected_sha256=digest) == 0
    assert launchctl.calls == [
        [LAUNCHCTL, "print", f"gui/{UID}/{LABEL}"],
        [LAUNCHCTL, "print-disabled", f"gui/{UID}"],
        [LAUNCHCTL, "bootstrap", f"gui/{UID}", str(definition)],
    ]
    assert "unload" not in launchctl.verbs
    assert "load" not in launchctl.verbs
    assert "kickstart" not in launchctl.verbs


def test_fresh_activation_refuses_an_intervening_job_without_touching_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definition, digest = _definition(tmp_path)
    launchctl = _Launchctl(absent=False)
    _patch(monkeypatch, launchctl)

    with pytest.raises(RuntimeError, match="already registered"):
        macos_service.load_fresh_agent(definition, expected_sha256=digest)

    assert launchctl.calls == [[LAUNCHCTL, "print", f"gui/{UID}/{LABEL}"]]


def test_fresh_activation_refuses_a_disabled_override_and_never_enables_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definition, digest = _definition(tmp_path)
    launchctl = _Launchctl(disabled=True)
    _patch(monkeypatch, launchctl)

    with pytest.raises(RuntimeError, match="disabled") as raised:
        macos_service.load_fresh_agent(definition, expected_sha256=digest)

    assert f"launchctl enable gui/{UID}/{LABEL}" in str(raised.value)
    assert launchctl.calls == [
        [LAUNCHCTL, "print", f"gui/{UID}/{LABEL}"],
        [LAUNCHCTL, "print-disabled", f"gui/{UID}"],
    ]
    assert "enable" not in launchctl.verbs


def test_fresh_activation_checks_the_digest_before_any_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definition, _digest = _definition(tmp_path)
    launchctl = _Launchctl()
    _patch(monkeypatch, launchctl)

    with pytest.raises(RuntimeError, match="changed after setup recorded it"):
        macos_service.load_fresh_agent(definition, expected_sha256="0" * 64)

    assert launchctl.calls == []


def test_fresh_activation_rechecks_the_digest_after_the_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definition, digest = _definition(tmp_path)

    def mutate() -> None:
        definition.write_bytes(b"<plist version=\"1.0\"><dict><key>X</key></dict></plist>")

    launchctl = _Launchctl(on_print=mutate)
    _patch(monkeypatch, launchctl)

    with pytest.raises(RuntimeError, match="changed after setup recorded it"):
        macos_service.load_fresh_agent(definition, expected_sha256=digest)

    assert launchctl.calls == [
        [LAUNCHCTL, "print", f"gui/{UID}/{LABEL}"],
        [LAUNCHCTL, "print-disabled", f"gui/{UID}"],
    ]


def test_fresh_activation_refuses_bytes_that_are_not_the_recorded_digest_format(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definition, _digest = _definition(tmp_path)
    launchctl = _Launchctl()
    _patch(monkeypatch, launchctl)

    with pytest.raises(ValueError, match="lowercase sha256"):
        macos_service.load_fresh_agent(definition, expected_sha256="A" * 64)
    assert launchctl.calls == []


def test_fresh_activation_reports_a_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definition, digest = _definition(tmp_path)
    launchctl = _Launchctl(bootstrap_returncode=5)
    _patch(monkeypatch, launchctl)

    with pytest.raises(RuntimeError, match="launchctl bootstrap failed"):
        macos_service.load_fresh_agent(definition, expected_sha256=digest)


def test_fresh_activation_reports_a_disabled_bootstrap_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even if the override query missed it, bootstrap's refusal says what to do."""
    definition, digest = _definition(tmp_path)
    launchctl = _Launchctl(
        bootstrap_returncode=125,
        bootstrap_stderr=f"Bootstrap failed: 125: Service is disabled: {LABEL}",
    )
    _patch(monkeypatch, launchctl)

    with pytest.raises(RuntimeError, match="disabled") as raised:
        macos_service.load_fresh_agent(definition, expected_sha256=digest)
    assert f"launchctl enable gui/{UID}/{LABEL}" in str(raised.value)


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_fresh_activation_refuses_a_symlinked_definition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definition, digest = _definition(tmp_path)
    link = tmp_path / "link.plist"
    link.symlink_to(definition)
    launchctl = _Launchctl()
    _patch(monkeypatch, launchctl)

    with pytest.raises(RuntimeError, match="not a regular file"):
        macos_service.load_fresh_agent(link, expected_sha256=digest)
    assert launchctl.calls == []


def test_host_interpreter_for_maps_only_the_console_name() -> None:
    assert macos_service.host_interpreter_for("ciao") == sys.executable
    assert macos_service.host_interpreter_for("/opt/venv/bin/ciao") == sys.executable
    assert macos_service.host_interpreter_for("/opt/venv/bin/python3") == "/opt/venv/bin/python3"


def test_the_native_paths_pin_the_system_launchctl() -> None:
    """A caller-controlled PATH cannot choose the launchctl the native paths run."""
    assert macos_service.LAUNCHCTL == "/bin/launchctl"
