"""The strict launchd override reader used only by native activation.

``service_login`` already has a lenient reader for the sign-in page: it treats
several ambiguous listings as "unknown" so the UI can say nothing. A native
activation is about to act on the answer, so the strict sibling refuses every
ambiguity the lenient one tolerated and adds two more: a label listed twice and
an entry-shaped line outside the ``disabled services`` section.
"""

from __future__ import annotations

import pytest

from ciao import macos_service, service_login

LABEL = macos_service.SERVER_LABEL


def _section(*entries: tuple[str, str]) -> str:
    body = "".join(f'\t\t"{label}" => {word}\n' for label, word in entries)
    return f"\n\tdisabled services = {{\n{body}\t}}\n"


def test_a_named_disabled_label_is_disabled() -> None:
    listing = _section((LABEL, "disabled"), ("com.example.other", "enabled"))

    assert service_login.strict_disabled_override(listing, LABEL) is True


def test_a_named_enabled_label_is_not_disabled() -> None:
    assert service_login.strict_disabled_override(_section((LABEL, "enabled")), LABEL) is False


def test_a_readable_listing_without_the_label_is_not_disabled() -> None:
    listing = _section(("com.example.other", "disabled"))

    assert service_login.strict_disabled_override(listing, LABEL) is False


def test_the_recorded_empty_section_is_a_readable_no_override_answer() -> None:
    assert service_login.strict_disabled_override(_section(), LABEL) is False


@pytest.mark.parametrize(
    "listing",
    [
        "",
        "no such section\n",
        '{\n\t"com.ciao.server" => disabled\n}\n',
        _section((LABEL, "disabled")) + _section((LABEL, "disabled")),
        _section((LABEL, "disabled"), (LABEL, "enabled")),
        _section((LABEL, "disabled")) + '\t"com.example.other" => disabled\n',
        _section((LABEL, "maybe")),
        _section((LABEL, "disabled")) + '\t"com.ciao.server" => enabled\n',
        _section((LABEL, "disabled")).replace("\t}\n", ""),
    ],
)
def test_any_listing_this_reader_cannot_account_for_is_unknown(listing: str) -> None:
    assert service_login.strict_disabled_override(listing, LABEL) is None


def test_a_duplicate_label_is_refused_even_when_the_copies_agree() -> None:
    """The strict reader does not pick one copy; it refuses to decide."""

    assert service_login.strict_disabled_override(
        _section((LABEL, "disabled"), (LABEL, "disabled")), LABEL
    ) is None
