import pytest
import yaml

from ciao.frontmatter import split_frontmatter, yaml_scalar


def test_split_ignores_dashes_inside_values():
    parts = split_frontmatter("---\ndescription: a --- b\n---\nbody")
    assert parts is not None
    block, body = parts
    assert "a --- b" in block
    assert body == "body"


def test_split_handles_crlf_bom_and_no_trailing_newline():
    parts = split_frontmatter("﻿---\r\nk: v\r\n---\r\nbody")
    assert parts is not None
    assert yaml.safe_load(parts[0]) == {"k": "v"}
    assert parts[1] == "body"
    parts = split_frontmatter("---\nk: v\n---")
    assert parts == ("k: v", "")


def test_split_empty_block():
    assert split_frontmatter("---\n---\nbody") == ("", "body")


@pytest.mark.parametrize("text", ["no fence", "----\nk: v\n---\n", "", "x\n---\nk: v\n---\n"])
def test_split_returns_none_without_opening_fence(text):
    assert split_frontmatter(text) is None


@pytest.mark.parametrize("value", ["a: b", "'quoted", "* star", "#hash", "line\nbreak", "", "Café", 5, True, "a\x05b", "a\x85b", "del\x7f", "\x9f"])
def test_yaml_scalar_round_trips(value):
    out = yaml_scalar(value)
    assert "\n" not in out
    assert yaml.safe_load(f"k: {out}") == {"k": value}


def test_yaml_scalar_round_trips_every_codepoint():
    codepoints = [
        *range(0, 0x250),
        *range(0x2000, 0x2070),
        0xFEFF, 0xFFFD, 0xFFFE, 0xD7FF, 0xE000, 0x1F600,
    ]
    failures: list[str] = []
    for cp in codepoints:
        ch = chr(cp)
        for s in (ch, f"a{ch}b", f"{ch}x", f"x{ch}"):
            try:
                out = yaml_scalar(s)
                assert "\n" not in out
                assert yaml.safe_load(f"k: {out}\n") == {"k": s}
                parts = split_frontmatter(f"---\nk: {out}\ntype: note\n---\n\nbody\n")
                assert parts is not None
                assert yaml.safe_load(parts[0]) == {"k": s, "type": "note"}
            except Exception:
                failures.append(f"U+{cp:04X} {s!r}")
    assert failures == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"say \\"hi\\": now"', 'say "hi": now'),
        ("'it''s: ok'", "it's: ok"),
        ("Use for: notes", "Use for: notes"),
        ('"', '"'),
        ("", ""),
    ],
)
def test_unquote_scalar(raw, expected):
    from ciao.frontmatter import unquote_scalar

    assert unquote_scalar(raw) == expected
