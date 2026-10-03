"""Copyable shell lines that put a directory on the user's ``PATH``.

Two callers want this text and they want different shapes: ``ciao setup``
prints a session-only line, the setup wizard hands out a line that survives a
new terminal. Rendering either one per OS is easy to get wrong -- a POSIX
``export`` line does nothing in PowerShell or cmd (#696) -- so the wording and
the quoting live here once and every call site asks this module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def path_hint(directory: str, *, persist: bool) -> str:
    """A copyable line that puts ``directory`` on ``PATH`` in the user's shell.

    ``directory`` is an absolute directory with no trailing separator. POSIX
    keeps the two shapes it has always printed: the short session-only
    ``export`` line, and, when ``persist`` is set, an append to the rc file a
    login shell reads. Windows has no session-only equivalent worth typing --
    PowerShell cannot make ``ciao`` findable for the *next* terminal without
    writing the user PATH -- so both shapes are the same persistent update
    there.

    The Windows line goes through the registry rather than
    ``[Environment]::SetEnvironmentVariable`` on purpose (#854). That setter
    reads the old value through ``GetEnvironmentVariable``, which hands back
    the *expanded* string, and writes the result back as ``REG_SZ``. A user
    PATH stored as ``REG_EXPAND_SZ`` with ``%USERPROFILE%\\...`` entries
    would then be frozen to today's expanded paths and change type under every
    other tool that reads it. ``GetValue(..., 'DoNotExpandEnvironmentNames')``
    plus an explicit ``RegistryValueKind`` keeps both the entries and the kind.
    """
    if sys.platform == "win32":
        # Single-quoted so a ``$`` or a backtick in the path cannot expand;
        # doubling ``'`` is how PowerShell escapes one inside a literal.
        quoted = directory.replace("'", "''")
        return (
            "$k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("
            f"'Environment', $true); $v = $k.GetValue('Path', '', "
            "'DoNotExpandEnvironmentNames'); "
            f"$k.SetValue('Path', '{quoted}' + "
            "$(if ($v) { ';' + $v }), "
            "[Microsoft.Win32.RegistryValueKind]::ExpandString)"
        )
    if not persist:
        return f'export PATH="{directory}:$PATH"'
    home = str(Path.home())
    target = directory
    if target == home or target.startswith(home + os.sep):
        # Written into an rc file, so keep it portable across machines and
        # readable to whoever opens that file later.
        target = "$HOME" + target[len(home) :]
    # The engine usually runs under launchd, where SHELL is unset; zsh is the
    # macOS default and the shell the documented installer assumes.
    shell = os.path.basename(os.environ.get("SHELL", "") or "zsh")
    if shell == "fish":
        return f"fish_add_path {target}"
    # macOS terminals start login shells, which read ~/.bash_profile — not
    # ~/.bashrc, which would fix only the shell the user is sitting in.
    rc = "~/.bash_profile" if shell == "bash" else "~/.zshrc"
    return f"""echo 'export PATH="{target}:$PATH"' >> {rc} && source {rc}"""


def path_hint_note() -> str:
    """Extra line that belongs under ``path_hint``, or "" when there is none.

    Only Windows needs one: a persistent user-PATH write is invisible to the
    terminal that ran it, so without this the command looks like it did
    nothing.
    """
    if sys.platform == "win32":
        return "Open a new terminal for the change to take effect."
    return ""