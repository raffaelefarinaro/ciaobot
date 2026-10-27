"""One fence for untrusted text that a prompt quotes (#1458).

Text that came from outside the conversation (a command's output, a log tail,
a model-written proposal) goes into a prompt between a one-line label and a
named tag. The text is escaped first, so it cannot carry its own closing tag
and have everything after it read as Ciaobot's framing.

:func:`commonmark_fence` is the fenced-code-block variant: a longer backtick
run than anything inside the text keeps an embedded ``` block (or a forged
closing fence) inside the quoted block.
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


def commonmark_fence(text: str, *, label: str) -> str:
    """``label``, then ``text`` in a CommonMark fence it cannot close.

    The opening run is one backtick longer than the longest run anywhere in
    ``text`` (at least three), so an embedded ``` block, or a forged closing
    fence, stays inside the quoted block. ``label`` is written by the caller,
    never taken from ``text``.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    # The trailing newline keeps a following sentence out of the closing line:
    # a CommonMark fence closes only when its run is alone on the line.
    return f"{label}\n{ticks}\n{text}\n{ticks}\n"
