"""Contract tests for the packaged update-task catalog (issue #737).

Every test drives a temporary packaged root rather than a checkout-relative
path, so what is exercised here is what an installed wheel runs: `load_catalog`
and `read_prompt` both take the root they were pointed at, and the default
resolves the packaged one through `importlib.resources`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from ciao import update_task_catalog
from ciao.package_version import _version_key
from ciao.update_task_catalog import (
    CATALOG_FILENAME,
    STOCK_PACKAGE,
    UPDATE_TASKS_DIRNAME,
    Diagnostic,
    TaskCatalog,
    load_catalog,
    packaged_root,
    parse_version,
    read_prompt,
)


@pytest.fixture(autouse=True)
def registered_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the detector/completion-check names the fixtures below use.

    The shipped registries are empty because no task definition ships yet, so
    a test that wants a sound row registers its names first. A registered name
    with no implementation behind it is a `not_implemented` warning, which is
    exactly the state this child ships in.
    """
    monkeypatch.setattr(
        update_task_catalog, "DETECTORS", frozenset({"has-legacy-rows"})
    )
    monkeypatch.setattr(
        update_task_catalog, "COMPLETION_CHECKS", frozenset({"no-legacy-rows"})
    )


def _row(**overrides: Any) -> dict[str, Any]:
    """One sound catalog row, with `overrides` applied to it."""
    row: dict[str, Any] = {
        "id": "review-legacy-rows",
        "revision": 1,
        "since_version": "1.0.0",
        "scope": "workspace",
        "title": "Review legacy rows",
        "why": "Older entries need a decision before they can be indexed.",
        "detector": "has-legacy-rows",
        "completion_check": "no-legacy-rows",
        "prompt_resource": "prompts/review-legacy-rows-1.md",
        "depends_on": [],
    }
    row.update(overrides)
    return row


def _write_prompt(root: Path, resource: str, text: str = "Do the thing.\n") -> Path:
    target = root.joinpath(*resource.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _write_catalog(root: Path, rows: Any) -> Path:
    """Write a packaged root whose catalog holds `rows` (a str is written raw)."""
    root.mkdir(parents=True, exist_ok=True)
    body = rows if isinstance(rows, str) else json.dumps(rows)
    (root / CATALOG_FILENAME).write_text(body, encoding="utf-8")
    return root


def _pack(root: Path, rows: list[Any]) -> TaskCatalog:
    """A packaged root holding `rows`, with a prompt file for each row's resource.

    Only a resource the loader would accept is written, so a fixture asking for
    an escaping or non-markdown path creates nothing on disk.
    """
    _write_catalog(root, rows)
    for row in rows:
        if not isinstance(row, dict):
            continue
        resource = row.get("prompt_resource")
        if (
            isinstance(resource, str)
            and resource.endswith(".md")
            and ".." not in resource
            and not resource.startswith(("/", "~"))
        ):
            _write_prompt(root, resource)
    return load_catalog(root=root)


def _error_codes(catalog: TaskCatalog) -> set[str]:
    return {d.code for d in catalog.diagnostics if d.severity == "error"}


def _keys(diagnostics: Sequence[Diagnostic]) -> set[tuple[str, int]]:
    return {(d.task_id, d.revision) for d in diagnostics}


# The first-task child (the legacy Learnings cleanup of #728) adds the first
# real definition, so nothing here may assume the shipped catalog is empty.
def test_loads_shipped_catalog_cleanly() -> None:
    catalog = load_catalog()

    assert not catalog.diagnostics, (
        "the packaged catalog has diagnostics: "
        f"{[(d.code, d.message) for d in catalog.diagnostics]}"
    )
    assert len({task.id for task in catalog.tasks}) == len(catalog.tasks)
    for task in catalog.tasks:
        # A row that validated must also be readable offline, through the same
        # packaged root `load_catalog` resolved.
        assert read_prompt(task).strip()
        assert parse_version(task.since_version) is not None
        assert task.scope in ("workspace", "install")


def test_unknown_detector_and_check_are_diagnostics_not_crashes(tmp_path: Path) -> None:
    unknown = _pack(
        tmp_path / "unknown",
        [
            _row(
                detector="not-registered", prompt_resource="prompts/unknown-detector.md"
            ),
            _row(
                id="other-task",
                completion_check="also-not-registered",
                prompt_resource="prompts/unknown-check.md",
            ),
        ],
    )

    assert _error_codes(unknown) == {"unknown_detector", "unknown_completion_check"}
    assert unknown.tasks == ()
    assert _keys([d for d in unknown.diagnostics if d.code == "unknown_detector"]) == {
        ("review-legacy-rows", 1)
    }

    # A registered name with no implementation yet is a warning, not a load
    # failure: the definition stays and the caller is told it cannot be applied.
    registered = _pack(tmp_path / "registered", [_row()])

    assert _error_codes(registered) == set()
    assert [task.id for task in registered.tasks] == ["review-legacy-rows"]
    unimplemented = [d for d in registered.diagnostics if d.code == "not_implemented"]
    assert _keys(unimplemented) == {("review-legacy-rows", 1)}
    assert {d.severity for d in unimplemented} == {"warning"}


def test_duplicate_ids_and_revisions_rejected(tmp_path: Path) -> None:
    catalog = _pack(
        tmp_path / "duplicates",
        [
            _row(),
            _row(),
            _row(revision=2, prompt_resource="prompts/review-legacy-rows-2.md"),
        ],
    )

    # An exact repeat and a second definition of the same id are different
    # mistakes, and each row reports the one that is its own.
    assert _keys(
        [d for d in catalog.diagnostics if d.code == "duplicate_revision"]
    ) == {("review-legacy-rows", 1)}
    assert _keys([d for d in catalog.diagnostics if d.code == "duplicate_id"]) == {
        ("review-legacy-rows", 2)
    }
    # The first definition of an id is the one that stands, which is the same
    # answer `by_id` gives; the rest are out of the catalog.
    assert [task.id for task in catalog.tasks] == ["review-legacy-rows"]
    assert catalog.by_id["review-legacy-rows"].revision == 1


def test_a_discarded_duplicate_copy_cannot_condemn_the_standing_copy(
    tmp_path: Path,
) -> None:
    # The second copy is thrown away, so its own defects are its own: a cycle or
    # a missing dependency on a discarded copy must not reach the copy that
    # stands, which is the one `load_catalog` keeps.
    self_cycle = _pack(
        tmp_path / "self-cycle-copy",
        [
            _row(id="a"),
            _row(id="a", depends_on=[{"id": "a", "revision": 1}]),
        ],
    )
    missing_dependency = _pack(
        tmp_path / "missing-dep-copy",
        [
            _row(id="a"),
            _row(id="a", depends_on=[{"id": "gone", "revision": 1}]),
        ],
    )

    for catalog in (self_cycle, missing_dependency):
        assert [task.id for task in catalog.tasks] == ["a"]
        assert [task.revision for task in catalog.tasks] == [1]
        assert _error_codes(catalog) == {"duplicate_revision"}
        assert [t.id for t in catalog.eligible("1.0.0")] == ["a"]
        assert "duplicate_revision" in {d.code for d in catalog.diagnostics}


def test_prompt_resource_confinement(tmp_path: Path) -> None:
    root = tmp_path / "confined"
    outside = tmp_path / "outside.md"
    outside.write_text("not yours\n", encoding="utf-8")
    _write_prompt(root, "prompts/empty.md", "   \n")
    _write_prompt(root, "prompts/notes.txt", "plain text\n")
    (root / "prompts" / "linked.md").symlink_to(outside)
    (root / "prompts" / "dir-link").symlink_to(tmp_path, target_is_directory=True)

    cases: dict[str, list[str]] = {
        "prompt_not_confined": [
            "/etc/passwd.md",
            "~/prompts/home.md",
            "prompts/../../../etc/passwd.md",
            "prompts/linked.md",
            "prompts/dir-link/outside.md",
            "prompts/embedded-null\0.md",
        ],
        "prompt_missing": ["prompts/absent.md"],
        "prompt_empty": ["prompts/empty.md"],
        "prompt_not_markdown": ["prompts/notes.txt"],
        # A name the filesystem cannot even stat is a diagnostic, not a raise:
        # `load_catalog` has to survive a catalog nobody wrote by hand.
        "prompt_unreadable": [f"prompts/{'a' * 300}.md"],
    }
    _write_prompt(root, "prompts/sound.md")
    rows = [
        _row(id=f"escapes-{index}", prompt_resource=resource)
        for index, resource in enumerate(
            resource for resources_list in cases.values() for resource in resources_list
        )
    ]
    rows.append(_row(id="sound", prompt_resource="prompts/sound.md"))
    _write_catalog(root, rows)

    catalog = load_catalog(root=root)

    assert _error_codes(catalog) == set(cases)
    assert [task.id for task in catalog.tasks] == ["sound"]
    for code, expected in cases.items():
        reported = {d.message for d in catalog.diagnostics if d.code == code}
        assert len(reported) == len(expected)

    # A sound resource is not merely free of diagnostics: it reads, and the read
    # is the one that would hand a caller its instructions.
    assert read_prompt(catalog.tasks[0], root=root) == "Do the thing.\n"
    # And a resource that went away after the load fails as a caller-visible
    # error, not as an OSError escaping a definition that had validated.
    with pytest.raises(ValueError):
        read_prompt(replace(catalog.tasks[0], prompt_resource="prompts/gone.md"))


def test_since_version_gating_covers_skipped_releases_and_prereleases(
    tmp_path: Path,
) -> None:
    rows = [
        _row(id="from-1-0-0", since_version="1.0.0"),
        _row(id="from-1-2-0", since_version="1.2.0"),
        _row(id="from-1-3-0", since_version="1.3.0"),
        _row(id="from-1-3-0-rc2", since_version="v1.3.0-rc.2"),
    ]
    catalog = _pack(tmp_path / "gating", rows)

    assert _error_codes(catalog) == set()
    every = [task.id for task in catalog.tasks]
    assert every == [row["id"] for row in rows]

    # A skipped release still applies: gating asks what the installed version
    # supports, never "what did the last release ship".
    assert [t.id for t in catalog.eligible("1.0.0")] == ["from-1-0-0"]
    assert [t.id for t in catalog.eligible("1.2.0")] == ["from-1-0-0", "from-1-2-0"]
    # A leading `v` is a tag spelling, not a different version.
    assert [t.id for t in catalog.eligible("v1.2.0")] == ["from-1-0-0", "from-1-2-0"]
    # This ordering is the app's release ordering, not PEP 440: a prerelease of
    # 1.3.0 sorts at or after 1.3.0, exactly as the update check orders it.
    assert [t.id for t in catalog.eligible("1.3.0rc2")] == every
    assert [t.id for t in catalog.eligible("1.3.0.dev3")] == [
        "from-1-0-0",
        "from-1-2-0",
        "from-1-3-0",
    ]
    assert parse_version("v1.3.0-rc.2") == parse_version("1.3.0rc2")

    # A downgrade excludes what the engine cannot run and keeps the definition,
    # so the task's own state survives the downgrade.
    assert [t.id for t in catalog.eligible("1.1.0")] == ["from-1-0-0"]
    assert sorted(catalog.by_id) == sorted(every)
    # An installed version that cannot be parsed supports nothing: gating cannot
    # prove support, and offering a task the engine may not run is the worse
    # failure.
    assert catalog.eligible("not-a-version") == ()

    # Gating orders versions exactly the way the update check does, or the two
    # answer different questions about the same install.
    for spelling in ("1.0.0", "1.2.0", "1.3.0", "v1.2.0", "1.3.0rc2", "1.3.0.dev3"):
        assert parse_version(spelling) == _version_key(spelling.removeprefix("v"))
    for spelling in ("", "latest", "1..0", "v", "one", "1.0.0+"):
        assert parse_version(spelling) is None

    # A build of a release is an ordinary installed version: a chained suffix
    # parses, and it still supports the release's own tasks. An installed version
    # that silently fails to parse gates every task out.
    for spelling in ("1.0.1rc1.dev0", "1.0.1.post1.dev0", "1.0.1.post1"):
        assert parse_version(spelling) == _version_key(spelling)
    assert parse_version("1.0.1") < parse_version("1.0.1rc1.dev0")
    assert parse_version("1.0.1") < parse_version("1.0.1.post1.dev0")
    assert [t.id for t in catalog.eligible("1.0.1.post1.dev0")] == ["from-1-0-0"]


def test_dependency_cycle_and_unknown_reference_diagnosed(tmp_path: Path) -> None:
    catalog = _pack(
        tmp_path / "dependencies",
        [
            _row(id="first", depends_on=[{"id": "second", "revision": 1}]),
            _row(id="second", depends_on=[{"id": "first", "revision": 1}]),
            _row(
                id="third",
                depends_on=[
                    {"id": "second", "revision": 9},
                    {"id": "absent", "revision": 1},
                ],
            ),
        ],
    )

    # Every task on the cycle is named, not only the one that closed it, so all
    # of them leave the catalog: a consumer resolving `depends_on` must not be
    # able to loop forever.
    cycles = [d for d in catalog.diagnostics if d.code == "dependency_cycle"]
    assert _keys(cycles) == {("first", 1), ("second", 1)}
    assert all("first@1 -> second@1 -> first@1" in d.message for d in cycles)

    unknown = [d for d in catalog.diagnostics if d.code == "unknown_dependency"]
    assert _keys(unknown) == {("third", 1)}
    reported = " ".join(d.message for d in unknown)
    assert "second@9" in reported
    assert "absent@1" in reported

    # A task that cannot be ordered is not an eligible task, however well formed
    # its own row is.
    assert catalog.tasks == ()
    assert catalog.eligible("1.0.0") == ()


def test_kept_task_cannot_depend_on_a_task_that_was_dropped(tmp_path: Path) -> None:
    # `broken` is dropped for an unregistered detector, so nothing may depend on
    # it: a kept task with an unresolvable dependency is a lie about its own
    # prerequisites. The strand is two deep on purpose, because dropping one
    # task strands the task that depended on *it*.
    dropped_dependency = _pack(
        tmp_path / "dropped-dependency",
        [
            _row(id="broken", detector="not-registered"),
            _row(id="dependent", depends_on=[{"id": "broken", "revision": 1}]),
            _row(id="transitive", depends_on=[{"id": "dependent", "revision": 1}]),
        ],
    )

    assert _error_codes(dropped_dependency) == {
        "unknown_detector",
        "unknown_dependency",
    }
    assert [task.id for task in dropped_dependency.tasks] == []
    assert dropped_dependency.eligible("9.9.9") == ()
    unresolved = [
        d for d in dropped_dependency.diagnostics if d.code == "unknown_dependency"
    ]
    assert _keys(unresolved) == {("dependent", 1), ("transitive", 1)}
    reported = {d.task_id: d.message for d in unresolved}
    assert "broken@1" in reported["dependent"]
    assert "dependent@1" in reported["transitive"]

    # The same rule for a duplicate id: the second copy is not a dependency
    # target, so nothing may be left depending on it — while a dependency on the
    # copy that stands is kept.
    duplicate_target = _pack(
        tmp_path / "duplicate-target",
        [
            _row(id="duplicated"),
            _row(
                id="duplicated", revision=2, prompt_resource="prompts/duplicated-2.md"
            ),
            _row(id="stranded", depends_on=[{"id": "duplicated", "revision": 2}]),
            _row(id="resolved", depends_on=[{"id": "duplicated", "revision": 1}]),
        ],
    )

    assert _error_codes(duplicate_target) == {"duplicate_id", "unknown_dependency"}
    assert sorted(task.id for task in duplicate_target.tasks) == [
        "duplicated",
        "resolved",
    ]


def test_read_prompt_works_from_packaged_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default root is installed package data, resolved through
    # importlib.resources rather than relative to this file.
    assert packaged_root() == Path(
        str(resources.files(STOCK_PACKAGE).joinpath(UPDATE_TASKS_DIRNAME))
    )
    assert (packaged_root() / CATALOG_FILENAME).is_file()
    assert load_catalog().diagnostics == ()

    root = tmp_path / "packaged"
    _write_catalog(root, [_row()])
    _write_prompt(root, "prompts/review-legacy-rows-1.md", "Packaged prompt.\n")
    task = load_catalog(root=root).tasks[0]
    assert read_prompt(task, root=root) == "Packaged prompt.\n"

    # Point the packaged root at that directory and the default read follows
    # it, which is what the installed wheel's resource read does.
    monkeypatch.setattr(update_task_catalog, "packaged_root", lambda: root)
    assert read_prompt(task) == "Packaged prompt.\n"
    assert load_catalog().tasks[0] == task

    # The confinement rule is re-applied at the read, not trusted from the load.
    with pytest.raises(ValueError):
        read_prompt(replace(task, prompt_resource="../../etc/passwd.md"))


def test_malformed_json_and_bad_types_are_diagnosed(tmp_path: Path) -> None:
    # Malformed JSON is a diagnostic, not an exception, and never a task list.
    broken = _write_catalog(tmp_path / "broken", "{ this is not json")
    assert _error_codes(load_catalog(root=broken)) == {"catalog_unreadable"}
    assert load_catalog(root=broken).tasks == ()

    # So is a catalog file that is not there at all.
    assert _error_codes(load_catalog(root=tmp_path / "absent")) == {
        "catalog_unreadable"
    }

    not_a_list = _write_catalog(tmp_path / "not-a-list", {"tasks": []})
    assert _error_codes(load_catalog(root=not_a_list)) == {"catalog_not_a_list"}

    # No exception escapes: a row of every wrong shape, a field of every wrong
    # type, and a dependency list that is not a list of objects.
    rows: list[Any] = [
        "a bare string",
        17,
        ["review-legacy-rows"],
        {
            "id": 5,
            "revision": "1",
            "since_version": [],
            "scope": 1,
            "title": None,
            "why": {},
            "detector": [],
            "completion_check": True,
            "prompt_resource": 7,
            "depends_on": "second",
        },
        {
            "id": "bad-values",
            "revision": 0,
            "since_version": "not-a-version",
            "scope": "everywhere",
            "title": "  ",
            "why": "",
            "detector": "has-legacy-rows",
            "completion_check": "no-legacy-rows",
            "prompt_resource": "prompts/bad-values-1.md",
            "depends_on": [
                "second",
                {"id": "second"},
                {"revision": 1},
                # A dependency is held to the same rule as the row itself: a
                # reference is not a string to coerce into one.
                {"id": 5, "revision": 1},
                {"id": ["second"], "revision": 1},
                {"id": "Second", "revision": 1},
                {"id": "second", "revision": 0},
            ],
        },
        _row(id="Not Kebab", prompt_resource="prompts/not-kebab-1.md"),
        # A key the loader does not know is not a key it ignores: the field it
        # was meant for would never arrive, so the row is refused.
        {
            "id": "typo",
            "revision": 1,
            "since_version": "1.0.0",
            "scope": "workspace",
            "title": "T",
            "why": "W",
            "detector": "has-legacy-rows",
            "completion_check": "no-legacy-rows",
            "prompt_resource": "prompts/typo-1.md",
            "depends_onn": [{"id": "second", "revision": 1}],
        },
    ]
    catalog = _pack(tmp_path / "bad-types", rows)

    assert catalog.tasks == ()
    assert _error_codes(catalog) == {
        "invalid_row",
        "invalid_field",
        "invalid_id",
        "invalid_revision",
        "unknown_scope",
        "bad_since_version",
        "invalid_depends_on",
        "unknown_field",
    }
    typos = [d.message for d in catalog.diagnostics if d.code == "unknown_field"]
    assert len(typos) == 1
    assert "depends_onn" in typos[0]
    # Every malformed dependency is reported on the row that carries it, rather
    # than coerced into a reference that only fails later as unresolvable.
    dependent = [d for d in catalog.diagnostics if d.code == "invalid_depends_on"]
    assert len(dependent) == 8
    # One bad row reports all of its own problems rather than only the first,
    # and a bool is not an int where a revision is expected.
    named = " ".join(d.message for d in catalog.diagnostics)
    for field in (
        "id",
        "revision",
        "since_version",
        "scope",
        "title",
        "why",
        "detector",
        "completion_check",
        "prompt_resource",
        "depends_on",
    ):
        assert field in named, f"no diagnostic named the bad {field!r}"
    assert "positive integer, got 0" in named
