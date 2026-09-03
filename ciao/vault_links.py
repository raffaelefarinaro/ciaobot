"""Text-level primitives for reading links out of vault markdown.

Why this module exists
----------------------
Four modules need to answer the same questions about a note's body — where does
a fenced block start, is this `[` an image or an escaped bracket, what are the
three parts of `[[ref#anchor|alias]]` — and until this module existed they
answered them by importing each other's private helpers. `vault_rehome` alone
reached into eleven underscore-prefixed names across `vault_index`,
`vault_lint`, and `vault_migrate_links`; `vault_migrate_links` reached into
`vault_lint._is_escaped`. A change to any one of them could silently corrupt
links or receipts in a module that never mentioned it.

The primitives collected here are the ones with no owner: pure functions and
patterns over markdown *text*, with no notion of a vault index, a lint finding,
or a migration receipt. Anything that needs `Entry`, a filename index, or a
receipt stays in the module that owns that concept — `vault_index` still owns
resolution (`resolve_related`, `resolve_vault_link`, `markdown_destination`),
`vault_migrate_links` still owns the receipt and the git rail.

This module imports nothing from `ciao`, so it can be imported from anywhere in
the vault stack without creating a cycle. That is the property that makes it
usable as the shared home; keep it that way.

Behaviour note
--------------
Every pattern and function here was moved verbatim from its previous home. In
particular `vault_lint` keeps its own private `_FENCE_RE`/`_INLINE_CODE_RE`/
`_FRONTMATTER_RE`, which look like duplicates of the ones below but are *not*
the same patterns — the linter's handle `~~~` fences, leading indentation, and
CRLF frontmatter. Unifying them would change what the linter flags, so they
were deliberately left alone.
"""

from __future__ import annotations

import re

# ---- block structure -------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# ---- markdown links --------------------------------------------------------

# One inline markdown link. `label` is the visible text (needed so a stripped
# link can be replaced by readable prose instead of vanishing); the destination
# is either angle-bracketed — the only form that survives a filename with a
# space — or bare, with an optional link title after it.
# Examples matched:
#   [Mo](./People/Mo.md)              -> label "Mo", bare "./People/Mo.md"
#   [Mo](<./People/Mo Salah.md>)      -> label "Mo", angle "./People/Mo Salah.md"
#   [Mo](./People/Mo.md "Tooltip")    -> title ignored
# A leading `!` (image) or backslash (escaped) is rejected by `is_link_start`,
# which has the preceding character in hand.
MARKDOWN_LINK_RE = re.compile(
    r"\[(?P<label>[^\[\]\n]*)\]\("
    r"[ \t]*(?:<(?P<angle>[^<>\n]*)>|(?P<bare>[^\s()]*))"
    r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*\)"
)

# ---- frontmatter `related:` ------------------------------------------------

# A `related:`/`relatedTo:` frontmatter key, optionally with an inline flow
# value on the same line (`related: [A, B]` or `related: People/Mo`).
FM_RELATED_KEY_RE = re.compile(r"^(related|relatedTo):[ \t]*(.*)$")
FM_LIST_ITEM_RE = re.compile(r"^(\s+)-\s?(.*)$")

# ---- wikilinks -------------------------------------------------------------

# The retired dialect. `vault_index` used to own this pattern; once the readers
# stopped parsing wikilinks it had no caller there, and the modules that still
# have to *recognise* a wikilink in order to remove or repoint it live
# elsewhere. Same shape as the pattern the readers used, so a migration
# converts exactly what the graph used to follow: group 1 is the ref (anchor and
# alias excluded), group 2 the alias. `[[#Heading]]` cannot match — group 1
# needs a non-`#` character — which is why a pure in-page anchor is left alone
# for free.
#
# `[` is excluded from the ref for the same reason `]` is: it is a bracket this
# pattern is responsible for balancing, not a character a note name has. Letting
# it in made `people: [[[People/Mo]]]` — a wikilink inside a flow sequence —
# match from the outer bracket with the ref `[People/Mo`, so the conversion ate
# the sequence's opening bracket and left `people: Mo]` behind. Excluded, the
# match starts one character later and the bracket survives.
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")


def is_escaped(text: str, index: int) -> bool:
    """True when the character at ``index`` is behind an escaping backslash.

    An odd run of backslashes escapes; an even run is literal backslashes that
    happen to precede the character. Prose documenting link syntax escapes its
    brackets, so an escaped match is a *mention* of a link, not a link.
    """
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def is_link_start(text: str, index: int) -> bool:
    """False when the `[` at ``index`` opens an image or is backslash-escaped.

    `![alt](x.png)` is an embed, not a link, and `\\[not a link](x.md)` is prose
    documenting the syntax — both would otherwise become phantom graph edges.
    """
    if index == 0:
        return True
    if text[index - 1] == "!":
        return False
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 0


def parse_wikilink(matched: str) -> tuple[str, str, str]:
    """Split a `[[ref#anchor|alias]]` match into its three parts.

    `WIKILINK_RE` captures ref and alias but leaves the anchor in a
    non-capturing group, and the anchor is exactly what a receipt has to keep.
    Parsing the already-matched text avoids a second wikilink pattern that could
    disagree with the first about what a wikilink even is.

    The alias pipe is spelled `\\|` inside a markdown table, where a bare `|`
    would close the cell instead — correct GFM, correct Obsidian, and the only
    way to write a roster table of people notes. The backslash belongs to the
    *delimiter*, so it is dropped here rather than left to trail into the ref:
    `[[People/Mo\\|Mo]]` used to parse as the ref `People/Mo\\`, which resolves to
    nothing, and the migration then emitted `./People/Mo\\.md` — a path with a
    stray backslash in it — and reported the link as one that was *already*
    dead. Every wikilink in a table cell hit this.
    """
    inner = matched[2:-2]
    alias = ""
    if "|" in inner:
        inner, alias = inner.split("|", 1)
        if inner.endswith("\\"):
            inner = inner[:-1]
    anchor = ""
    if "#" in inner:
        inner, anchor = inner.split("#", 1)
    return inner.strip(), anchor.strip(), alias.strip()


def alias_separator(matched: str) -> str:
    """How ``matched`` spelled its alias pipe, `\\|` or `|`.

    For callers that re-emit a wikilink rather than replace it (`vault_rehome`
    repoints a moved target and keeps the dialect). Re-rendering a table cell's
    `\\|` as a bare `|` would close the cell early and break the row, which is
    exactly what `parse_wikilink` dropping the backslash would otherwise cause.
    """
    head, separator, _ = matched[2:-2].partition("|")
    if not separator:
        return "|"
    return "\\|" if head.endswith("\\") else "|"
