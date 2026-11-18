"""Render a system-level systemd unit without privileged side effects."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import re

from ciao.os_support.limits import SERVER_NOFILE_TARGET
from ciao.os_support.tool_path import dedupe_path


# Where docs/LINUX.md installs the unit, and the only owner whose copy is trusted.
UNIT_PATH = Path("/etc/systemd/system/ciaobot.service")
TRUSTED_UNIT_UID = 0


def _unquote(value: str) -> str:
    """Reverse :func:`_quoted`; an unquoted value is returned as written."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        escapes = {"\\\\": "\\", '\\"': '"', "%%": "%"}
        return re.sub(r'\\\\|\\"|%%', lambda match: escapes[match.group(0)], inner)
    return value


def unit_workspace(path: Path = UNIT_PATH) -> Path | None:
    """The workspace recorded in the installed unit, or ``None`` if it is not trusted.

    Read-only and independent of the caller's environment. The engine runs as an
    unprivileged account, so a unit owned by ``TRUSTED_UNIT_UID`` and writable
    by nobody else is one the agent cannot have planted or edited.
    """
    try:
        info = path.stat()
        if info.st_uid != TRUSTED_UNIT_UID or info.st_mode & 0o022:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found: str | None = None
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip() != "Environment":
            continue
        assignment = _unquote(value.strip())
        if assignment.startswith("CIAO_WORKSPACE="):
            found = assignment[len("CIAO_WORKSPACE="):]
    if not found:
        return None
    workspace = Path(found)
    return workspace if workspace.is_absolute() else None


def _quoted(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Service values must not contain control characters")
    # systemd expands specifiers even inside quotes. Environment= does not
    # expand shell variables; ExecStart= additionally needs literal dollars.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_service(*, workspace: Path, user: str, home: Path, python: Path) -> str:
    if user == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user):
        raise ValueError("Choose an unprivileged Linux service account")
    for path in (workspace, home, python):
        if not path.is_absolute():
            raise ValueError("Workspace, home, and Python paths must be absolute")
        text = str(path)
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError("Service paths must not contain control characters")
        if text != text.rstrip():
            raise ValueError("Service paths must not end with whitespace")
    if "\\" in str(workspace):
        raise ValueError(
            "Workspace path must not contain a backslash: WorkingDirectory= "
            "takes a literal path and the unit-file lexer would consume the "
            "backslash as an escape, silently changing the directory"
        )
    # Do not resolve python: resolving a virtualenv symlink would select the
    # system interpreter and lose the installed Ciaobot dependencies.
    # The interpreter's folder is often /usr/bin already, so the composed PATH is
    # deduplicated like every other PATH this package hands on.
    tool_path = dedupe_path(f"{python.parent}:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin")
    template = resources.files("ciao.stock").joinpath("deploy/ciaobot.service.tmpl").read_text(encoding="utf-8")
    return template.format(
        user=user,
        # WorkingDirectory= takes one literal absolute path: quoting is not
        # supported (systemd-analyze rejects a quoted value as "not
        # absolute"), while % specifiers are still expanded, hence %% above.
        # Quotes stay literal; backslashes are refused outright above.
        # ExecStart=/Environment= do accept quoting, so those keep _quoted().
        workspace=str(workspace).replace("%", "%%"),
        workspace_env=_quoted(f"CIAO_WORKSPACE={workspace}"),
        home_env=_quoted(f"HOME={home}"),
        path_env=_quoted(f"PATH={tool_path}"),
        python=_quoted(str(python)).replace("$", "$$"),
        nofile=SERVER_NOFILE_TARGET,
    )


INSTALL_SCRIPT = (  # constant: no user data is ever interpolated; the unit arrives on stdin
    'umask 022; stage=$(mktemp -d) || exit 1; '
    'cat > "$stage/ciaobot.service" && '
    'systemd-analyze verify "$stage/ciaobot.service" && '
    'install -m 644 "$stage/ciaobot.service" /etc/systemd/system/ciaobot.service; '
    'rc=$?; rm -rf "$stage"; exit $rc'
)


class UnitConflict(ValueError):
    """An existing unit runs another interpreter (a source install)."""


@dataclass(frozen=True)
class ServicePlan:
    """The rendered unit and the privileged commands that install it."""

    unit: str
    commands: tuple[tuple[tuple[str, ...], str | None], ...]  # (argv, stdin)


def _first_exec_token(value: str) -> str | None:
    """The value's first ExecStart token, keeping a quoted token whole."""
    if value.startswith('"'):
        escaped = False
        for index, char in enumerate(value[1:], start=1):
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return value[: index + 1]
        return None
    return next(iter(value.split()), None)


def installed_unit_python(path: Path = UNIT_PATH) -> Path | None:
    """The interpreter in the unit's ExecStart (first token, `_unquote`d, `$$`→`$`), or None."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip() != "ExecStart":
            continue
        token = _first_exec_token(value.strip())
        if token is None:
            return None
        return Path(_unquote(token).replace("$$", "$"))
    return None


def plan_install(*, workspace: Path, user: str, home: Path, python: Path,
                 replace: bool = False, start: bool = True,
                 unit_path: Path = UNIT_PATH) -> ServicePlan:
    """Render the unit for the invoking account and plan its privileged install.

    Raises :class:`UnitConflict` when ``unit_path`` already exists and its
    ``ExecStart`` interpreter is not this env's ``python`` (a source install)
    and ``replace`` is not set: taking such a unit over silently would strand
    an install people already have (#3131 D3).
    """
    unit = render_service(workspace=workspace, user=user, home=home, python=python)
    if unit_path.exists() and not replace:
        installed = installed_unit_python(unit_path)
        if installed != python:
            shown = str(installed) if installed else "an interpreter that cannot be read"
            raise UnitConflict(
                f"{unit_path} exists and runs Ciaobot with {shown}, but this "
                f"install runs {python}: that unit belongs to a source install, "
                "and installing over it would break that install. See "
                "docs/LINUX.md#existing-source-installs, or pass --replace to "
                "take the unit over."
            )
    commands: list[tuple[tuple[str, ...], str | None]] = [
        (("sudo", "sh", "-c", INSTALL_SCRIPT), unit),
        (("sudo", "systemctl", "daemon-reload"), None),
        (("sudo", "systemctl", "enable", "ciaobot.service"), None),
    ]
    if start:
        commands.append((("sudo", "systemctl", "restart", "ciaobot.service"), None))
    return ServicePlan(unit=unit, commands=tuple(commands))


def run_plan(plan: ServicePlan, *, run: Callable[[Sequence[str], str | None], None]) -> None:
    """Run the plan's commands in order; ``run`` receives ``(argv, stdin)``."""
    for argv, stdin in plan.commands:
        run(argv, stdin)
