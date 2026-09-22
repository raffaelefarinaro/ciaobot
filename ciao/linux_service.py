"""Render a system-level systemd unit without privileged side effects."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
import re


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
    tool_path = f"{python.parent}:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
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
    )
