"""Standard streams that carry UTF-8 on every OS.

Agents read the ``ciao`` CLI's replies through a pipe, and so does the engine
when it runs a ``ciao`` child; both decode them as UTF-8 (#696). POSIX streams
already follow the UTF-8 locale and are left exactly as they are. On Windows,
Python encodes a piped or redirected stream in the ANSI code page (cp1252 on
most installs), so ``ü`` reaches the reader as the single byte ``0xFC`` and a
vault snippet comes back as ``Caff� �ber``. ``use_utf8_stdio`` switches
stdin, stdout and stderr to UTF-8, keeping each stream's error handler; a
console stream is already Unicode and only changes in name.
"""

from __future__ import annotations

import io
import sys

if sys.platform == "win32":

    def use_utf8_stdio() -> None:
        """Make the standard streams UTF-8 (see the module docstring)."""
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            # pythonw has no streams, and a caller may have swapped in its own.
            if isinstance(stream, io.TextIOWrapper):
                # A new encoding alone resets errors to "strict"; keep stderr's
                # backslashreplace.
                stream.reconfigure(encoding="utf-8", errors=stream.errors)

else:

    def use_utf8_stdio() -> None:
        """Nothing to change: POSIX streams follow the locale, as before."""
