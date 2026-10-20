"""One fence for untrusted text that a prompt quotes (#1458).

Text that came from outside the conversation (a command's output, a log tail,
a model-written proposal) goes into a prompt between a one-line label and a
named tag. The text is escaped first, so it cannot carry its own closing tag
and have everything after it read as Ciaobot's framing.
"""

from __future__ import annotations

import re


def neutralize(tag: str, text: str) -> str:
    """Escape ``<tag`` and ``</tag`` inside ``text``.

    Escaped rather than dropped: the text is still the record of what was
    quoted. The open form matches by prefix so a tag with attributes
    (``<proposal-field name="x">``) is covered too. Case-insensitive, because a
    reader does not care how the closing tag is spelled.
    """
    return re.sub(
        rf"<(/?){re.escape(tag)}",
        lambda m: f"&lt;{m.group(1)}{tag}",
        text,
        flags=re.IGNORECASE,
    )


def fence(tag: str, text: str, *, label: str, attrs: str = "") -> str:
    """``label``, then ``text`` escaped and wrapped in ``<tag>``.

    ``label`` is one line saying what the text is and that it is data, not
    instructions. ``attrs`` is the opening tag's attribute string, such as
    ``name="problem"``; it is written by the caller, never taken from ``text``.
    """
    opening = f"<{tag} {attrs}>" if attrs else f"<{tag}>"
    return f"{label}\n{opening}\n{neutralize(tag, text)}\n</{tag}>"
