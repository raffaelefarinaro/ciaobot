"""Focused tests for the shared brain content guide core.

Everything here runs in ``tmp_path``; no workspace registry, Git remote, live
vault or web route is involved.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from ciao import shared_brain_guide as guide

IDENTITY_A = "github.com/acme/notes"
IDENTITY_B = "github.com/beta/notes"


def _section(
    *,
    path: str = "notes/decisions.md",
    section: str = "Decisions",
    operation: str = "append",
    example: str = "",
) -> dict[str, str]:
    return {
        "kind": "section",
        "path": path,
        "section": section,
        "operation": operation,
        "example": example,
    }


def _collection(*, path: str = "notes/glossary", operation: str = "create") -> dict[str, str]:
    return {"kind": "collection", "path": path, "operation": operation}


def _guide_text(
    types: dict[str, object],
    *,
    version: int = 1,
    scope: str = "Project decisions and vocabulary.",
    exclusions: tuple[str, ...] = ("personal notes",),
) -> str:
    payload = {
        "version": version,
        "scope": scope,
        "exclusions": list(exclusions),
        "types": types,
    }
    return f"# Team notes\n\n{scope}\n\n```ciao-guide\n{json.dumps(payload, indent=2)}\n```\n"


def _write(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / guide.GUIDE_NAME
    path.write_text(text, encoding="utf-8")
    return path


def _load(root: Path, identity: str = IDENTITY_A) -> guide.GuideLoad:
    return guide.read_guide(root, brain_identity=identity)


def _consent(
    identity: str = IDENTITY_A,
    *,
    note_type: str = "decision",
    kind: str = "section",
    path: str = "notes/decisions.md",
    section: str = "Decisions",
    operations: tuple[str, ...] = ("append",),
) -> guide.ConsentMapping:
    grant = guide.ConsentGrant(
        note_type=note_type,
        kind=kind,  # type: ignore[arg-type]
        path=path,
        section=section,
        operations=frozenset(operations),
    )
    return guide.ConsentMapping(brain_identity=identity, grants=(grant,))


def _resolve(
    load: guide.GuideLoad,
    *,
    note_type: str = "decision",
    relative_path: str = "",
    pinned: guide.GuidePin | None = None,
    operation: str | None = None,
    consent: guide.ConsentMapping | None = None,
) -> guide.Resolution:
    return guide.resolve_destination(
        load,
        consent if consent is not None else _consent(),
        note_type=note_type,
        relative_path=relative_path,
        pinned=pinned,
        operation=operation,
    )


# ---------------------------------------------------------------------------
# Reading, bounding and pinning
# ---------------------------------------------------------------------------


def test_missing_guide_is_missing_not_malformed(tmp_path: Path) -> None:
    load = _load(tmp_path)
    assert load.status == "missing"
    assert not load.ok
    assert guide.GUIDE_NAME in load.reason
    assert _resolve(load).status == "guide_unavailable"


def test_reads_guide_and_pins_identity_version_and_exact_bytes(tmp_path: Path) -> None:
    text = _guide_text(
        {"decision": _section(example="Use UTC everywhere.")},
        scope="Platform decisions.",
        exclusions=("personal notes", "customer data"),
    )
    _write(tmp_path, text)
    load = _load(tmp_path)
    assert load.ok
    assert load.guide is not None
    assert load.guide.pin == guide.GuidePin(
        brain_identity=IDENTITY_A,
        version=1,
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    assert load.guide.scope == "Platform decisions."
    assert load.guide.exclusions == ("personal notes", "customer data")
    decision = load.guide.types["decision"]
    assert decision.destination == guide.Destination(
        kind="section",
        path="notes/decisions.md",
        section="Decisions",
        operation="append",
    )
    assert decision.example == "Use UTC everywhere."


def test_utf8_unicode_is_accepted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _guide_text(
            {"decision": _section(path="notes/décisions.md", example="Use 🕒.")}
        ),
    )
    load = _load(tmp_path)
    assert load.ok
    assert load.guide is not None
    assert load.guide.types["decision"].destination.path == "notes/décisions.md"


def test_same_bytes_in_two_brains_still_have_distinct_pins(tmp_path: Path) -> None:
    text = _guide_text({"decision": _section()})
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write(root_a, text)
    _write(root_b, text)

    pin_a = _load(root_a, IDENTITY_A).guide
    pin_b = _load(root_b, IDENTITY_B).guide
    assert pin_a is not None and pin_b is not None
    assert pin_a.pin.digest == pin_b.pin.digest
    assert pin_a.pin != pin_b.pin


def test_a_read_is_a_snapshot_even_when_the_file_is_replaced(tmp_path: Path) -> None:
    first = _guide_text({"decision": _section()})
    _write(tmp_path, first)
    load = _load(tmp_path)
    assert load.guide is not None
    old_pin = load.guide.pin

    second = _guide_text({"decision": _section(path="notes/other.md")})
    _write(tmp_path, second)
    assert load.guide.pin == old_pin

    reloaded = _load(tmp_path)
    assert reloaded.guide is not None
    assert reloaded.guide.pin.digest != old_pin.digest
    assert _resolve(reloaded, pinned=old_pin).status == "guide_changed"


def test_guide_is_bounded_before_parsing(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir()
    (root / guide.GUIDE_NAME).write_bytes(b"x" * (guide.MAX_GUIDE_BYTES + 1))
    load = _load(root)
    assert load.status == "too_large"
    assert not load.ok


def test_parse_guide_rechecks_the_byte_bound(tmp_path: Path) -> None:
    load = guide.parse_guide(b"x" * (guide.MAX_GUIDE_BYTES + 1), brain_identity=IDENTITY_A)
    assert load.status == "too_large"


def test_symlinked_guide_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.md"
    target.write_text(_guide_text({"decision": _section()}), encoding="utf-8")
    root = tmp_path / "brain"
    root.mkdir()
    os.symlink(target, root / guide.GUIDE_NAME)
    load = _load(root)
    assert load.status == "unsafe"
    assert "symlink" in load.reason


def test_directory_named_like_the_guide_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    (root / guide.GUIDE_NAME).mkdir(parents=True)
    load = _load(root)
    assert load.status == "unsafe"


def test_guide_requires_a_brain_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        guide.read_guide(tmp_path, brain_identity="")
    with pytest.raises(ValueError):
        guide.parse_guide(b"", brain_identity="bad\nidentity")
    with pytest.raises(ValueError):
        guide.read_guide(tmp_path, brain_identity="bad\ud800identity")


# ---------------------------------------------------------------------------
# Malformed guides and malformed entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("empty file", ""),
        ("no fenced block", "# Guide\n\nEverything is fine.\n"),
        ("two fenced blocks", "```ciao-guide\n{}\n```\n\n```ciao-guide\n{}\n```\n"),
        ("broken JSON", "```ciao-guide\n{not json}\n```\n"),
        ("duplicate JSON key", '```ciao-guide\n{"version": 1, "version": 1}\n```\n'),
        (
            "missing top-level key",
            '```ciao-guide\n{"version": 1, "scope": "s", "types": {}}\n```\n',
        ),
        (
            "unknown top-level key",
            '```ciao-guide\n{"version": 1, "scope": "s", "exclusions": [], "types": {}, "extra": true}\n```\n',
        ),
        (
            "types is a list",
            '```ciao-guide\n{"version": 1, "scope": "s", "exclusions": [], "types": []}\n```\n',
        ),
        (
            "scope is empty",
            '```ciao-guide\n{"version": 1, "scope": " ", "exclusions": [], "types": {}}\n```\n',
        ),
        (
            "version is text",
            '```ciao-guide\n{"version": "1", "scope": "s", "exclusions": [], "types": {}}\n```\n',
        ),
        (
            "no usable types",
            '```ciao-guide\n{"version": 1, "scope": "s", "exclusions": [], "types": {"bad": 1}}\n```\n',
        ),
    ],
)
def test_malformed_guide_defers_for_review(tmp_path: Path, label: str, text: str) -> None:
    _write(tmp_path, text)
    load = _load(tmp_path)
    assert not load.ok, label
    assert load.status == "malformed", label
    assert _resolve(load).status == "guide_unavailable"


def test_unsupported_version_is_not_silently_accepted(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}, version=2))
    load = _load(tmp_path)
    assert load.status == "unsupported"
    assert "version 2" in load.reason
    assert not load.ok


def test_crlf_guide_is_accepted(tmp_path: Path) -> None:
    text = _guide_text({"decision": _section()}).replace("\n", "\r\n")
    (tmp_path / guide.GUIDE_NAME).write_bytes(text.encode("utf-8"))
    load = _load(tmp_path)
    assert load.ok
    assert load.guide is not None
    assert "decision" in load.guide.types


@pytest.mark.parametrize(
    "exclusions",
    [
        ["one", 2],
        ["x" * (guide.MAX_EXAMPLE_CHARS + 1)],
        tuple(f"exclusion-{i}" for i in range(guide.MAX_EXCLUSIONS + 1)),
    ],
)
def test_unbounded_or_ill_typed_exclusions_make_the_guide_unusable(
    tmp_path: Path, exclusions: object
) -> None:
    payload = {
        "version": 1,
        "scope": "s",
        "exclusions": exclusions,
        "types": {"decision": _section()},
    }
    _write(tmp_path, f"```ciao-guide\n{json.dumps(payload)}\n```\n")
    assert _load(tmp_path).status == "malformed"


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("traversal", _section(path="../outside.md")),
        ("absolute", _section(path="/etc/decisions.md")),
        ("hidden", _section(path=".private/decisions.md")),
        ("backslash", _section(path="notes\\decisions.md")),
        (
            "wrong-kind",
            {"kind": "folder", "path": "notes/decisions.md", "operation": "append"},
        ),
        (
            "missing-heading",
            {"kind": "section", "path": "notes/decisions.md", "operation": "append"},
        ),
        ("collection-append", _collection(operation="append")),
        (
            "list-operation",
            {"kind": "section", "path": "notes/decisions.md", "operation": []},
        ),
        ("extra-field", {**_section(), "extra": True}),
        ("not-an-object", "notes/decisions.md"),
        ("surrogate-path", _section(path="notes/\ud800.md")),
        ("surrogate-heading", _section(section="Dec\ud800isions")),
        ("surrogate-example", _section(example="x\ud800")),
    ],
)
def test_one_malformed_entry_fails_the_whole_guide_closed(
    tmp_path: Path, label: str, entry: object
) -> None:
    name = "broken"
    types = {"decision": _section(), name: entry}
    _write(tmp_path, _guide_text(types))
    load = _load(tmp_path)
    assert load.status == "malformed", label
    assert not load.ok
    assert name in load.reason, label
    # Neither the valid sibling nor the malformed entry resolves.
    assert _resolve(load, note_type="decision").status == "guide_unavailable"
    assert _resolve(load, note_type=name).status == "guide_unavailable"
    assert sum(_resolve(load, note_type=t).ok for t in types) == 0


def test_invalid_type_name_fails_the_whole_guide_closed(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section(), "Bad-Name": _section()}))
    load = _load(tmp_path)
    assert load.status == "malformed"
    assert "Bad-Name" in load.reason
    assert sum(_resolve(load, note_type=t).ok for t in ("decision", "Bad-Name")) == 0


def test_mixed_valid_and_malformed_entries_yield_zero_resolutions(tmp_path: Path) -> None:
    types = {
        "decision": _section(),
        "glossary-term": _collection(),
        "escape": _section(path="../outside.md"),
    }
    _write(tmp_path, _guide_text(types))
    load = _load(tmp_path)
    assert load.status == "malformed"
    assert load.guide is None
    assert [t for t in types if _resolve(load, note_type=t).ok] == []


def test_surrogate_collection_path_fails_closed(tmp_path: Path) -> None:
    types = {"decision": _section(), "broken": _collection(path="notes/\ud800")}
    _write(tmp_path, _guide_text(types))
    load = _load(tmp_path)
    assert load.status == "malformed"
    assert all(not _resolve(load, note_type=t).ok for t in types)


@pytest.mark.parametrize(
    "path",
    [
        "notes/\ud800.md",
        "notes/\udfff.md",
        "notes/glossary/\ud800.md",
    ],
)
def test_surrogate_paths_fail_the_guide_with_zero_resolutions(
    tmp_path: Path, path: str
) -> None:
    types = {"decision": _section(), "broken": _section(path=path)}
    _write(tmp_path, _guide_text(types))
    load = _load(tmp_path)
    assert load.status == "malformed"
    assert "broken" in load.reason
    assert sum(_resolve(load, note_type=t).ok for t in types) == 0


def test_surrogate_metadata_fails_the_guide_closed(tmp_path: Path) -> None:
    bad_scope = {
        "version": 1,
        "scope": "Scope \ud800",
        "exclusions": [],
        "types": {"decision": _section()},
    }
    _write(tmp_path, f"# Team notes\n\n```ciao-guide\n{json.dumps(bad_scope)}\n```\n")
    assert _load(tmp_path).status == "malformed"

    bad_exclusion = {
        "version": 1,
        "scope": "Scope",
        "exclusions": ["personal \ud800"],
        "types": {"decision": _section()},
    }
    _write(tmp_path, f"# Team notes\n\n```ciao-guide\n{json.dumps(bad_exclusion)}\n```\n")
    assert _load(tmp_path).status == "malformed"


def test_surrogate_consent_and_caller_paths_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _consent(path="notes/\ud800.md")

    _write(tmp_path, _guide_text({"glossary-term": _collection()}))
    load = _load(tmp_path)
    consent = _consent(
        note_type="glossary-term",
        kind="collection",
        path="notes/glossary",
        section="",
        operations=("create",),
    )
    resolution = _resolve(
        load,
        note_type="glossary-term",
        relative_path="notes/glossary/\ud800.md",
        consent=consent,
    )
    assert resolution.status == "unsafe_path"

    _write(tmp_path, _guide_text({"decision": _section()}))
    load = _load(tmp_path)
    assert _resolve(load, relative_path="notes/\ud800.md").status == "unsafe_path"


# ---------------------------------------------------------------------------
# Consent mapping validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grant_kwargs",
    [
        {"note_type": "Bad Name"},
        {"kind": "folder"},
        {"path": "../outside.md"},
        {"path": "/etc/decisions.md"},
        {"path": "notes/decisions.txt"},
        {"path": "notes/decisions.md", "section": ""},
        {"kind": "collection", "path": "notes/glossary", "section": "Terms"},
        {"kind": "collection", "path": "notes/glossary", "operations": ("append",)},
        {"operations": ()},
        {"operations": ("delete",)},
        {"operations": ("append", "append")},
    ],
)
def test_invalid_consent_grant_is_refused(grant_kwargs: dict[str, object]) -> None:
    kwargs: dict[str, object] = {
        "note_type": "decision",
        "kind": "section",
        "path": "notes/decisions.md",
        "section": "Decisions",
        "operations": frozenset({"append"}),
    }
    kwargs.update(grant_kwargs)
    with pytest.raises(ValueError):
        guide.ConsentGrant(**kwargs)  # type: ignore[arg-type]


def test_duplicate_consent_for_one_type_is_refused() -> None:
    first = guide.ConsentGrant(
        note_type="decision",
        kind="section",
        path="notes/decisions.md",
        section="Decisions",
        operations=frozenset({"append"}),
    )
    with pytest.raises(ValueError):
        guide.ConsentMapping(brain_identity=IDENTITY_A, grants=(first, first))


def test_consent_mapping_requires_a_brain_identity() -> None:
    with pytest.raises(ValueError):
        guide.ConsentMapping(brain_identity="")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_section_destination_resolves_within_consent(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}))
    load = _load(tmp_path)
    resolved = _resolve(load)
    assert resolved.ok
    assert resolved.destination is not None
    assert resolved.destination.note_type == "decision"
    assert resolved.destination.destination == guide.Destination(
        kind="section",
        path="notes/decisions.md",
        section="Decisions",
        operation="append",
    )
    assert load.guide is not None
    assert resolved.destination.guide == load.guide.pin


def test_section_accepts_its_own_path_but_not_another(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}))
    load = _load(tmp_path)
    same = _resolve(load, relative_path="notes/decisions.md")
    assert same.ok
    other = _resolve(load, relative_path="notes/other.md")
    assert other.status == "path_mismatch"


def test_collection_destination_requires_a_path_inside_the_collection(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"glossary-term": _collection()}))
    load = _load(tmp_path)
    consent = _consent(
        note_type="glossary-term",
        kind="collection",
        path="notes/glossary",
        section="",
        operations=("create",),
    )
    resolved = _resolve(
        load,
        note_type="glossary-term",
        relative_path="notes/glossary/2026-11-25.md",
        consent=consent,
    )
    assert resolved.ok
    assert resolved.destination is not None
    assert resolved.destination.destination.path == "notes/glossary/2026-11-25.md"
    assert resolved.destination.destination.kind == "collection"


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        ("", "path_mismatch"),
        ("notes/glossary", "unsafe_path"),
        ("notes/glossary/../outside.md", "unsafe_path"),
        ("notes/glossary/../../outside.md", "unsafe_path"),
        ("/notes/glossary/x.md", "unsafe_path"),
        ("notes/glossary\\x.md", "unsafe_path"),
        ("notes/glossary/.hidden.md", "unsafe_path"),
        ("notes/glossary/x.txt", "unsafe_path"),
        ("notes/glossary/CON.md", "unsafe_path"),
        ("notes/other/x.md", "path_mismatch"),
    ],
)
def test_collection_path_is_bounded_and_confined(
    tmp_path: Path, relative_path: str, expected: str
) -> None:
    _write(tmp_path, _guide_text({"glossary-term": _collection()}))
    load = _load(tmp_path)
    consent = _consent(
        note_type="glossary-term",
        kind="collection",
        path="notes/glossary",
        section="",
        operations=("create",),
    )
    resolved = _resolve(
        load,
        note_type="glossary-term",
        relative_path=relative_path,
        consent=consent,
    )
    assert resolved.status == expected


def test_too_deep_collection_path_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"glossary-term": _collection()}))
    load = _load(tmp_path)
    consent = _consent(
        note_type="glossary-term",
        kind="collection",
        path="notes/glossary",
        section="",
        operations=("create",),
    )
    deep = "notes/glossary/" + "/".join(["d"] * guide.MAX_PATH_DEPTH) + ".md"
    assert _resolve(
        load,
        note_type="glossary-term",
        relative_path=deep,
        consent=consent,
    ).status == "unsafe_path"


def test_unknown_type_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}))
    assert _resolve(_load(tmp_path), note_type="invoice").status == "unknown_type"


def test_unconsented_type_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}))
    empty = guide.ConsentMapping(brain_identity=IDENTITY_A)
    assert _resolve(_load(tmp_path), consent=empty).status == "not_consented"


@pytest.mark.parametrize(
    ("guide_entry", "consent_kwargs", "expected"),
    [
        (
            _section(path="notes/other.md"),
            {},
            "outside_consent",
        ),
        (
            _section(section="Other"),
            {},
            "outside_consent",
        ),
        (
            _collection(),
            {"kind": "section", "path": "notes/glossary.md", "section": "Terms"},
            "outside_consent",
        ),
        (
            _section(operation="replace"),
            {"operations": ("append",)},
            "operation_not_allowed",
        ),
        (
            _section(operation="append"),
            {"operations": ("replace",)},
            "operation_not_allowed",
        ),
    ],
)
def test_guide_cannot_move_outside_independent_consent(
    tmp_path: Path,
    guide_entry: dict[str, object],
    consent_kwargs: dict[str, object],
    expected: str,
) -> None:
    _write(tmp_path, _guide_text({"decision": guide_entry}))
    kwargs: dict[str, object] = {
        "note_type": "decision",
        "kind": "section",
        "path": "notes/decisions.md",
        "section": "Decisions",
        "operations": ("append",),
    }
    kwargs.update(consent_kwargs)
    consent = _consent(**kwargs)  # type: ignore[arg-type]
    assert _resolve(_load(tmp_path), consent=consent).status == expected


def test_calling_operation_must_match_the_guide_and_the_consent(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section(operation="append")}))
    load = _load(tmp_path)
    assert _resolve(load, operation="append").ok
    assert _resolve(load, operation="replace").status == "operation_not_allowed"
    assert _resolve(load, operation="delete").status == "operation_not_allowed"


# ---------------------------------------------------------------------------
# Changed guides and two brains
# ---------------------------------------------------------------------------


def test_stored_pin_refuses_a_changed_guide(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}))
    load = _load(tmp_path)
    assert load.guide is not None
    pinned = load.guide.pin

    _write(tmp_path, _guide_text({"decision": _section(example="New example.")}))
    reloaded = _load(tmp_path)
    assert reloaded.guide is not None
    assert reloaded.guide.pin.digest != pinned.digest

    assert _resolve(reloaded, pinned=pinned).status == "guide_changed"
    assert _resolve(reloaded).ok


def test_prose_only_edit_still_requires_fresh_review(tmp_path: Path) -> None:
    text = _guide_text({"decision": _section()})
    _write(tmp_path, text)
    load = _load(tmp_path)
    assert load.guide is not None

    _write(tmp_path, text.replace("# Team notes", "# Team notes\n\nOne more sentence."))
    reloaded = _load(tmp_path)
    assert reloaded.ok
    assert reloaded.guide is not None
    assert reloaded.guide.pin.digest != load.guide.pin.digest
    assert _resolve(reloaded, pinned=load.guide.pin).status == "guide_changed"


def test_changed_guide_never_expands_an_existing_grant(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}))
    first = _load(tmp_path)
    assert first.guide is not None
    pinned = first.guide.pin
    assert _resolve(first, pinned=pinned).ok

    widened = _guide_text(
        {
            "decision": _section(path="notes/everything.md", section="Decisions"),
            "private-note": _section(path="notes/everything.md", section="Private"),
        }
    )
    _write(tmp_path, widened)
    second = _load(tmp_path)
    assert second.guide is not None

    # The old review cannot authorize the new bytes at all.
    assert _resolve(second, pinned=pinned).status == "guide_changed"
    # A fresh read still cannot exceed the operator's independent mapping.
    assert _resolve(second).status == "outside_consent"
    assert _resolve(second, note_type="private-note").status == "not_consented"


def test_changed_guide_cannot_reuse_an_old_pin_from_another_brain(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}))
    load_a = _load(tmp_path, IDENTITY_A)
    _write(tmp_path / "b", _guide_text({"decision": _section()}))
    load_b = _load(tmp_path / "b", IDENTITY_B)
    assert load_a.guide is not None and load_b.guide is not None
    assert _resolve(load_b, pinned=load_a.guide.pin).status == "guide_changed"


def test_two_brains_map_the_same_type_independently(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write(root_a, _guide_text({"decision": _section(path="decisions.md")}))
    _write(root_b, _guide_text({"decision": _section(path="project/decisions.md")}))

    load_a = _load(root_a, IDENTITY_A)
    load_b = _load(root_b, IDENTITY_B)
    assert load_a.guide is not None and load_b.guide is not None
    assert load_a.guide.pin.digest != load_b.guide.pin.digest

    consent_a = _consent(IDENTITY_A, path="decisions.md")
    consent_b = _consent(IDENTITY_B, path="project/decisions.md")

    resolved_a = _resolve(load_a, consent=consent_a)
    resolved_b = _resolve(load_b, consent=consent_b)
    assert resolved_a.ok and resolved_b.ok
    assert resolved_a.destination is not None and resolved_b.destination is not None
    assert resolved_a.destination.destination.path == "decisions.md"
    assert resolved_b.destination.destination.path == "project/decisions.md"

    # Neither brain's consent may be used for the other.
    assert _resolve(load_a, consent=consent_b).status == "consent_brain_mismatch"
    assert _resolve(load_b, consent=consent_a).status == "consent_brain_mismatch"


def test_resolution_has_no_cross_brain_state(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write(root_a, _guide_text({"decision": _section(path="decisions.md")}))
    _write(root_b, _guide_text({"decision": _section(path="project/decisions.md")}))
    load_a = _load(root_a, IDENTITY_A)
    load_b = _load(root_b, IDENTITY_B)
    consent_a = _consent(IDENTITY_A, path="decisions.md")
    consent_b = _consent(IDENTITY_B, path="project/decisions.md")

    first_a = _resolve(load_b, consent=consent_b)
    first_b = _resolve(load_a, consent=consent_a)
    second_a = _resolve(load_a, consent=consent_a)
    second_b = _resolve(load_b, consent=consent_b)

    assert first_a == second_b
    assert first_b == second_a
    assert first_a.destination is not None and first_b.destination is not None
    assert first_a.destination.destination.path == "project/decisions.md"
    assert first_b.destination.destination.path == "decisions.md"


def test_malformed_guide_cannot_be_resolved_even_with_consent(tmp_path: Path) -> None:
    _write(tmp_path, "```ciao-guide\n{bad}\n```\n")
    load = _load(tmp_path)
    assert _resolve(load, consent=_consent()).status == "guide_unavailable"


def test_publication_requires_a_stored_pin(tmp_path: Path) -> None:
    _write(tmp_path, _guide_text({"decision": _section()}))
    load = _load(tmp_path)
    assert load.guide is not None
    consent = _consent()

    without_pin = guide.resolve_for_publication(
        load, consent, note_type="decision", pinned=None
    )
    assert without_pin.status == "guide_changed"
    assert "pin" in without_pin.reason

    with_pin = guide.resolve_for_publication(
        load, consent, note_type="decision", pinned=load.guide.pin
    )
    assert with_pin.ok


def test_pin_identity_version_and_digest_all_participate_in_matching() -> None:
    pin = guide.GuidePin(
        brain_identity=IDENTITY_A,
        version=guide.GUIDE_VERSION,
        digest=hashlib.sha256(b"guide").hexdigest(),
    )
    assert pin == guide.GuidePin(
        brain_identity=IDENTITY_A,
        version=guide.GUIDE_VERSION,
        digest=pin.digest,
    )
    assert pin != guide.GuidePin(
        brain_identity=IDENTITY_B,
        version=pin.version,
        digest=pin.digest,
    )
    assert pin != guide.GuidePin(
        brain_identity=pin.brain_identity,
        version=pin.version + 1,
        digest=pin.digest,
    )
    assert pin != guide.GuidePin(
        brain_identity=pin.brain_identity,
        version=pin.version,
        digest=hashlib.sha256(b"other").hexdigest(),
    )
