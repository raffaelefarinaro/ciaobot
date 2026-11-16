"""Standard streams that carry UTF-8 on every OS.

Agents read the ``ciao`` CLI's replies through a pipe, and so does the engine
when it runs a ``ciao`` child; both decode them as UTF-8 (#696). POSIX streams
already follow the UTF-8 locale and are left exactly as they are.
``use_utf8_stdio`` is kept as the CLI's single call site for that guarantee
and changes nothing.
"""

from __future__ import annotations

def use_utf8_stdio() -> None:
    """Nothing to change: POSIX streams follow the locale, as before."""
