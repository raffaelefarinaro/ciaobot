"""Copyable shell lines that put a directory on the user's ``PATH``.

Two callers want this text and they want different shapes: ``ciao setup``
prints a session-only line, the setup wizard hands out a line that survives a
new terminal. The wording and the quoting live here once and every call site
asks this module.
"""

from __future__ import annotations

import os
from pathlib import Path


def path_hint(directory: str, *, persist: bool) -> str:
    """A copyable line that puts ``directory`` on ``PATH`` in the user's shell.

    ``directory`` is an absolute directory with no trailing separator. It has
    two shapes: the short session-only ``export`` line, and, when ``persist``
    is set, an append to the rc file a login shell reads.
    """
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
