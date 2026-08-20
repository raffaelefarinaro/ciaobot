"""Tests for the operator-action registry (P4.1) and its cheap-detection rule.

The registry lives in ``ciao/operator_actions.py``. The contract under test:
every detector reaches zero, is idempotent across identical passes, offers at
least run or chat, and never touches the vault (the ``scan_vault`` spy proves
it). The web routes are covered in ``test_web_housekeeping.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ciao.operator_actions import (
    _review_queue_depth,
    DetectionContext,
    REVIEW_QUEUE_DEPTH,
    detect_actions,
    run_action,
)


class _FakeConfig:
    """Minimal stand-in for the workspace registry surface detectors read."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        workspaces: tuple[str, ...] = ("personal",),
        vault_mode: str = "scratch",
    ) -> None:
        self.workspace_root = tmp_path
        self.vault_root = tmp_path / "memory-vault"
        self.vault_mode = vault_mode
        self._names = list(workspaces)
        self._roots = {
            name: self.vault_root / name for name in self._names
        }

    def workspace_names(self) -> list[str]:
        return list(self._names)

    def primary_workspace(self) -> str:
        # Same preference as CiaoConfig: `personal` for continuity, else the
        # first registered. Required rather than optional — the migration must
        # never guess which root inherits the guide and the skill catalog.
        if "personal" in self._names:
            return "personal"
        return self._names[0] if self._names else ""

    def workspace_vault_root(self, name: str) -> Path:
        return self._roots.get(name, self.vault_root / name)

    def canonical_workspace_vault_root(self, name: str) -> Path:
        return self.vault_root / name


class _Entry:
    def __init__(self, **kw):
        self.frequency = kw.get("frequency", "daily")
        self.enabled = kw.get("enabled", True)
        self.last_triggered_on = kw.get("last_triggered_on", "")
        self.run_at_date = kw.get("run_at_date", "")
        self.timezone_name = kw.get("timezone_name", "UTC")
        self.schedule_id = kw.get("schedule_id", "sched-1")
        self.title = kw.get("title", "")


class _Store:
    def __init__(self, entries=None):
        self._entries = entries or []

    def list_entries(self, **kwargs):
        return list(self._entries)


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _context(tmp_path: Path, **overrides) -> DetectionContext:
    config = overrides.pop("config", _FakeConfig(tmp_path))
    runtime = overrides.pop("runtime", _runtime(tmp_path))
    return DetectionContext(config=config, runtime_dir=runtime, **overrides)


def test_empty_on_a_healthy_install(tmp_path: Path) -> None:
    """A conformant install with no receipts and a shallow review queue is empty."""
    config = _FakeConfig(tmp_path)
    # Mark the vocabulary receipt present and resolved so that detector is silent.
    (tmp_path / ".runtime" / "migration").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".runtime" / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {}}), encoding="utf-8"
    )
    context = DetectionContext(
        config=config, runtime_dir=_runtime(tmp_path), schedule_store=_Store()
    )
    assert detect_actions(context) == []


def test_package_update_fires_only_on_available(tmp_path: Path) -> None:
    context = _context(tmp_path, package_status=lambda: {"update_available": False})
    assert detect_actions(context) == []
    context = _context(
        tmp_path,
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    ids = [a.id for a in detect_actions(context)]
    assert "package-update" in ids


def test_vault_location_fires_on_misplaced_vault(tmp_path: Path) -> None:
    # personal workspace vault placed outside the standard folder.
    standard = tmp_path / "memory-vault" / "personal"
    standard.mkdir(parents=True, exist_ok=True)
    actual = tmp_path / "elsewhere" / "personal"
    actual.mkdir(parents=True, exist_ok=True)

    class _Weird(_FakeConfig):
        def __init__(self):
            super().__init__(tmp_path, workspaces=("personal", "work"))
            self._roots["personal"] = actual

    context = _context(tmp_path, config=_Weird())
    ids = [a.id for a in detect_actions(context)]
    assert "vault-location:personal" in ids
    # A correctly placed vault does not fire.
    context = _context(tmp_path)
    ids = [a.id for a in detect_actions(context)]
    assert "vault-location:personal" not in ids


def test_unrehomed_people_gated_on_two_workspaces(tmp_path: Path) -> None:
    # With a single workspace there is no move to offer, so it must be silent.
    runtime = _runtime(tmp_path)
    single = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",)),
        runtime_dir=runtime,
    )
    assert [a.id for a in detect_actions(single)] == []

    # Two workspaces, no receipt yet: the tile fires.
    config = _FakeConfig(tmp_path, workspaces=("personal", "work"))
    context = DetectionContext(config=config, runtime_dir=runtime)
    ids = [a.id for a in detect_actions(context)]
    assert "vault-unrehomed-people" in ids

    # A migrated receipt clears it.
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-rehome.json").write_text(
        json.dumps({"status": "migrated", "moves": []}), encoding="utf-8"
    )
    ids = [a.id for a in detect_actions(context)]
    assert "vault-unrehomed-people" not in ids


def test_vault_vocabulary_fires_on_unresolved_only(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    config = _FakeConfig(tmp_path)
    context = DetectionContext(config=config, runtime_dir=runtime)
    assert [a.id for a in detect_actions(context)] == []

    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}),
        encoding="utf-8",
    )
    ids = [a.id for a in detect_actions(context)]
    assert "vault-vocabulary" in ids


def test_unmigrated_links_fires_only_for_existing_mode(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    context = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",), vault_mode="scratch"),
        runtime_dir=runtime,
    )
    assert "vault-unmigrated-links" not in [a.id for a in detect_actions(context)]

    config = _FakeConfig(tmp_path, workspaces=("personal",), vault_mode="existing")
    context = DetectionContext(config=config, runtime_dir=runtime)
    ids = [a.id for a in detect_actions(context)]
    assert "vault-unmigrated-links" in ids

    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-links.json").write_text(
        json.dumps({"rewrites": [], "migrated_at": "x"}), encoding="utf-8"
    )
    ids = [a.id for a in detect_actions(context)]
    assert "vault-unmigrated-links" not in ids


def test_missed_schedules_fires_collapsed_tile(tmp_path: Path) -> None:
    now = datetime(2026, 1, 20, 0, 0, tzinfo=UTC)
    store = _Store(
        [
            _Entry(
                frequency="once",
                run_at_date="2026-01-18",
                schedule_id="sched-a",
            ),
            _Entry(
                frequency="once",
                run_at_date="2026-01-19",
                schedule_id="sched-b",
            ),
        ]
    )
    context = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",)),
        runtime_dir=_runtime(tmp_path),
        schedule_store=store,
        now=now,
    )
    actions = detect_actions(context)
    tiles = [a for a in actions if a.kind == "missed-schedules"]
    assert len(tiles) == 1
    assert "2" in tiles[0].title

    # Already-triggered or not-yet-due one-timers are not missed.
    store = _Store(
        [
            _Entry(
                frequency="once",
                run_at_date="2026-01-18",
                last_triggered_on="done",
                schedule_id="sched-a",
            ),
            _Entry(
                frequency="once",
                run_at_date="2026-01-25",
                schedule_id="sched-c",
            ),
        ]
    )
    context = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",)),
        runtime_dir=_runtime(tmp_path),
        schedule_store=store,
        now=now,
    )
    tiles = [a for a in detect_actions(context) if a.kind == "missed-schedules"]
    assert tiles == []


def test_review_queue_depth_counts_skill_proposal_files(tmp_path: Path) -> None:
    """Skill-proposal FILES count toward the depth, and counting them must not raise.

    The other queue-depth test writes only Memory-Proposals.md, so the
    Skill-Proposals branch never ran and a `len()` on the generator returned by
    Path.glob went unnoticed. detect_actions catches every exception from a
    detector, so that TypeError silently removed the whole review-queue tile,
    and only on an install that actually has a Skill-Proposals folder. The
    reference vault has 49 such files.
    """
    config = _FakeConfig(tmp_path, workspaces=("personal",))
    root = config.workspace_vault_root("personal")
    skills = root / "Workspace" / "Skill-Proposals"
    skills.mkdir(parents=True, exist_ok=True)
    for i in range(REVIEW_QUEUE_DEPTH):
        (skills / f"proposal-{i}.md").write_text("# proposal\n", encoding="utf-8")

    context = DetectionContext(
        config=config, runtime_dir=_runtime(tmp_path), schedule_store=_Store([])
    )

    # Depth is reached by files alone, with no bullets in the queue file at all.
    assert _review_queue_depth(context) == REVIEW_QUEUE_DEPTH
    assert "review-queue-depth" in [a.id for a in detect_actions(context)]


def test_review_queue_depth_fires_above_threshold(tmp_path: Path) -> None:
    config = _FakeConfig(tmp_path, workspaces=("personal",))
    # Fill the review queue with enough bullets and files.
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "\n".join(
            f"- [memory] Pending fact number {i}." for i in range(REVIEW_QUEUE_DEPTH)
        ),
        encoding="utf-8",
    )
    context = DetectionContext(
        config=config, runtime_dir=_runtime(tmp_path), schedule_store=_Store([])
    )
    ids = [a.id for a in detect_actions(context)]
    assert "review-queue-depth" in ids

    # Below the threshold it is silent.
    queue.write_text("- [memory] One straggler.", encoding="utf-8")
    ids = [a.id for a in detect_actions(context)]
    assert "review-queue-depth" not in ids


def test_every_action_offers_run_or_chat(tmp_path: Path) -> None:
    """Contract 4: no action is a bare notice with neither a run nor a chat."""
    # Force every detector to fire so the whole registry is exercised.
    config = _FakeConfig(tmp_path, workspaces=("personal", "work"), vault_mode="existing")
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}), encoding="utf-8"
    )
    (runtime / "migration" / "vault-rehome.json").write_text(
        json.dumps({"status": "surveyed", "mechanical": 1, "needs_judgement": 1, "conflicts": 0}),
        encoding="utf-8",
    )
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "\n".join(f"- [memory] Pending {i}." for i in range(REVIEW_QUEUE_DEPTH)),
        encoding="utf-8",
    )
    now = datetime(2026, 1, 20, 0, 0, tzinfo=UTC)
    store = _Store([_Entry(frequency="once", run_at_date="2026-01-18", schedule_id="s1")])
    context = DetectionContext(
        config=config,
        runtime_dir=runtime,
        schedule_store=store,
        now=now,
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    actions = detect_actions(context)
    assert actions, "expected at least some actions to fire"
    for action in actions:
        assert action.run_label or action.chat_prompt, (
            f"action {action.id} offers neither run nor chat"
        )


def test_ids_are_stable_across_calls(tmp_path: Path) -> None:
    """Contract 3: byte-identical actions across two passes."""
    config = _FakeConfig(tmp_path, workspaces=("personal", "work"), vault_mode="existing")
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}), encoding="utf-8"
    )
    context = DetectionContext(
        config=config,
        runtime_dir=runtime,
        schedule_store=_Store([]),
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    first = detect_actions(context)
    second = detect_actions(context)
    assert [a.as_dict() for a in first] == [a.as_dict() for a in second]


def test_detector_raising_is_logged_and_skipped(tmp_path: Path) -> None:
    """A broken detector must not sink the pass."""
    from ciao.operator_actions import _DETECTORS

    broken = object.__new__(type("Broken", (), {}))

    def boom(_context):
        raise RuntimeError("boom")

    # Inject a failing detector, run, and restore.
    with patch("ciao.operator_actions._DETECTORS", _DETECTORS + [boom]):
        actions = detect_actions(
            DetectionContext(config=_FakeConfig(tmp_path), runtime_dir=_runtime(tmp_path))
        )
    assert isinstance(actions, list)


def test_scan_vault_is_never_touched(tmp_path: Path) -> None:
    """Contract 4 (cheap): no detector walks the vault."""
    from ciao.operator_actions import detect_actions
    from ciao.vault_index import scan_vault

    config = _FakeConfig(tmp_path, workspaces=("personal", "work"), vault_mode="existing")
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}), encoding="utf-8"
    )
    context = DetectionContext(
        config=config,
        runtime_dir=runtime,
        schedule_store=_Store([]),
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    with patch("ciao.vault_index.scan_vault", wraps=scan_vault) as spy:
        detect_actions(context)
    spy.assert_not_called()


def test_run_action_unknown_id_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_action("no-such-action", DetectionContext(config=_FakeConfig(tmp_path)))


def test_unmigrated_links_tile_does_not_assert_wikilinks_it_cannot_verify(
    tmp_path: Path,
) -> None:
    """The cheap predicate cannot know a wikilink exists, so it must not claim one.

    This detector fires on "vault adopted, no migration receipt", which is true
    of an adopted vault that was written in markdown links from the start and
    has nothing to convert. It runs on every app open and window focus, so it
    cannot call has_unmigrated_links, which walks the vault. The audit's notice
    does run that accurate check and may legitimately stay silent here, so the
    two surfaces disagree by design and the tile's wording has to be honest
    about what it actually knows.
    """
    config = _FakeConfig(tmp_path, workspaces=("personal",))
    config.vault_mode = "existing"
    root = config.workspace_vault_root("personal")
    root.mkdir(parents=True, exist_ok=True)
    # A markdown link only: nothing to migrate.
    (root / "Note.md").write_text("[Peter](./People/Peter.md)\n", encoding="utf-8")

    context = DetectionContext(
        config=config, runtime_dir=_runtime(tmp_path), schedule_store=_Store([])
    )
    tiles = [a for a in detect_actions(context) if a.kind == "unmigrated-links"]

    assert tiles, "the tile should still offer the preview"
    action = tiles[0]
    assert "may still" in action.title
    # It must not state as fact that wikilinks are present.
    assert "still uses the retired" not in action.title
    assert "still contains" not in action.detail



# -- the two queue tiles the operator saw on screen --------------------------


def _rehome_receipt(tmp_path: Path, payload: dict) -> None:
    import json

    path = _runtime(tmp_path) / "migration" / "vault-rehome.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _rehome_actions(tmp_path: Path) -> list:
    from ciao.operator_actions import _detect_unrehomed_people

    # Two workspaces: with one, there is nowhere to re-home TO and the detector
    # is silent by design (the single-workspace false positive fixed in P3).
    return _detect_unrehomed_people(
        _context(tmp_path, config=_FakeConfig(tmp_path, workspaces=("personal", "work")))
    )


def test_a_legacy_receipt_with_no_status_clears_the_tile(tmp_path: Path) -> None:
    """`vault_rehome` only started writing `status` when its survey mode landed.

    Every receipt written before that records a COMPLETED re-home with no status,
    and reading those as unfinished made this a permanent false positive on
    exactly the installs that had done the work. Seen on the reference install:
    87 moves and 165 link rewrites recorded, no status, tile firing a day later.
    """
    _rehome_receipt(tmp_path, {"moves": [1] * 87, "needs_judgement": [], "proposals": []})

    assert _rehome_actions(tmp_path) == []


def test_an_applied_receipt_still_surfaces_what_needs_a_decision(tmp_path: Path) -> None:
    """Treating "no status" as done must not hide outstanding judgement calls."""
    _rehome_receipt(
        tmp_path, {"moves": [1] * 87, "needs_judgement": [1] * 15, "proposals": [1]}
    )

    actions = _rehome_actions(tmp_path)

    assert len(actions) == 1
    detail = actions[0].detail
    assert "87" in detail and "16" in detail
    # The prose must read as prose, never as a dumped container.
    assert "{" not in detail and "[" not in detail


def test_the_tile_never_renders_a_container_into_its_prose(tmp_path: Path) -> None:
    """It read keys this receipt has never had, and one that is a LIST, so it
    said "the survey recorded 0 to move" while 87 were recorded as moved and
    then printed a list of dicts on screen."""
    _rehome_receipt(
        tmp_path,
        {
            "status": "surveyed",
            "moves": [{"path": "a"}] * 3,
            "needs_judgement": [{"bucket": "needs_judgement", "path": "b"}] * 15,
            "proposals": [],
        },
    )

    detail = _rehome_actions(tmp_path)[0].detail

    assert "bucket" not in detail and "{" not in detail
    assert "3 to move" in detail and "15 needing a decision" in detail


def test_a_detail_string_never_renders_a_container() -> None:
    """The tile read keys this receipt has never had, and one that is a LIST.

    So it told the operator "the survey recorded 0 to move" while 87 notes were
    recorded as moved, and then interpolated a list of dicts straight into the
    prose on screen.
    """
    from ciao.operator_actions import _count

    assert _count([{"bucket": "needs_judgement"}] * 15) == 15
    assert _count(15) == 15
    assert _count(None) == 0
    assert _count("nonsense") == 0
    assert _count(True) == 0, "a bool is not a count"


def test_both_queue_tiles_point_at_the_panel_that_has_the_buttons() -> None:
    """They offered "Review in chat" alone, so the operator was asked to work
    through 109 items in prose while the per-row accept/dismiss, the destination
    picker and the batch operations sat one route away."""
    import inspect

    from ciao import operator_actions as oa

    for detector in (oa._detect_unrehomed_people, oa._detect_review_queue):
        source = inspect.getsource(detector)
        assert 'view_route="/proposals"' in source, detector.__name__
        assert "view_label=" in source, detector.__name__


# -- the mandatory re-rooting gate -------------------------------------------


def _reroot_receipt(tmp_path: Path, payload: dict) -> None:
    import json

    path = _runtime(tmp_path) / "migration" / "workspace-rooting.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()


def _reroot_actions(tmp_path: Path, *, workspaces=("personal", "work")) -> list:
    from ciao.operator_actions import _detect_workspace_unmigrated

    (tmp_path / "memory-vault").mkdir(parents=True, exist_ok=True)
    config = _FakeConfig(tmp_path, workspaces=workspaces)
    return _detect_workspace_unmigrated(_context(tmp_path, config=config))


def test_the_gate_fires_when_the_move_has_not_run(tmp_path: Path) -> None:
    actions = _reroot_actions(tmp_path)

    assert len(actions) == 1
    action = actions[0]
    assert action.blocking is True
    assert action.run_label, "it must be actionable, not just informative"
    assert action.severity == 0, "it sorts above everything else"


def test_the_gate_names_what_blocked_it(tmp_path: Path) -> None:
    """Reaching the tile means the automatic attempt REFUSED, so the reason is
    the only useful thing it can say."""
    _reroot_receipt(tmp_path, {
        "status": "refused",
        "refusals": ["memory-vault/work/People/Mo.md has uncommitted changes"],
        "dirty_tracked": ["memory-vault/work/People/Mo.md"],
    })

    detail = _reroot_actions(tmp_path)[0].detail

    assert "uncommitted changes" in detail
    assert detail.count("memory-vault/work/People/Mo.md") == 1, (
        "the refusal already names the path; printing it twice was the first version"
    )


def test_the_gate_clears_once_the_move_succeeded(tmp_path: Path) -> None:
    _reroot_receipt(tmp_path, {"status": "migrated"})

    assert _reroot_actions(tmp_path) == []


def test_a_single_workspace_install_is_never_gated(tmp_path: Path) -> None:
    """One workspace has no second root to separate from, so the shared layout
    is already correct for it. Same rule as the re-home tile."""
    assert _reroot_actions(tmp_path, workspaces=("personal",)) == []


def test_no_shared_vault_means_nothing_to_separate(tmp_path: Path) -> None:
    from ciao.operator_actions import _detect_workspace_unmigrated
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    config = _FakeConfig(tmp_path, workspaces=("personal", "work"))
    # No memory-vault directory: already migrated, or never set up.
    assert _detect_workspace_unmigrated(_context(tmp_path, config=config)) == []


def test_the_run_button_goes_through_the_same_entry_point_as_startup(tmp_path: Path) -> None:
    """The button must not drift from what the upgrade does."""
    import inspect

    from ciao import operator_actions as oa

    source = inspect.getsource(oa._run_workspace_reroot)
    assert "migrate_if_needed" in source
    # A refusal raises, so the route renders a failed tile carrying the reason
    # instead of silently re-offering the button.
    assert "raise RuntimeError" in source


def test_the_button_reports_nothing_to_do_rather_than_pretending(tmp_path: Path) -> None:
    from ciao.operator_actions import _run_workspace_reroot

    context = _context(tmp_path, config=_FakeConfig(tmp_path, workspaces=("personal", "work")))

    # No vault on disk: there is nothing to move, and saying so beats reporting
    # a successful migration that moved nothing.
    result, summary = _run_workspace_reroot(context)

    assert result["status"] == "not_applicable"
    assert "Nothing to separate" in summary


def test_a_refusal_from_the_button_raises_with_the_reason(tmp_path: Path) -> None:
    """The route turns the raise into a failed tile carrying the reason, rather
    than silently re-offering the button as though nothing happened."""
    import pytest as _pytest

    from ciao.operator_actions import _run_workspace_reroot

    (tmp_path / "memory-vault" / "personal").mkdir(parents=True)
    (tmp_path / "memory-vault" / "work").mkdir(parents=True)
    # A non-empty destination is one of the plan's refusal conditions.
    (tmp_path / "personal").mkdir()
    (tmp_path / "personal" / "squatter.md").write_text("in the way\n", encoding="utf-8")
    context = _context(tmp_path, config=_FakeConfig(tmp_path, workspaces=("personal", "work")))

    with _pytest.raises(RuntimeError) as excinfo:
        _run_workspace_reroot(context)

    assert "personal" in str(excinfo.value)


# -- post-migration drift detectors (§11.2) -----------------------------------


class _RerootedConfig(_FakeConfig):
    """A config whose workspaces have their own agent roots, as after re-rooting."""

    def __init__(self, tmp_path: Path, *, workspaces: tuple[str, ...] = ("personal", "work")) -> None:
        super().__init__(tmp_path, workspaces=workspaces)
        self.env_source: dict[str, str] = {}

    def agent_root(self, name: str) -> Path:
        return self.workspace_root / name

    def agent_root_targets(self) -> list[tuple[Path, str]]:
        return [(self.agent_root(n), n) for n in self.workspace_names()]


def _shared_layout_config(tmp_path: Path) -> _FakeConfig:
    """Before the re-rooting every workspace resolves to the install root.

    The target is NAMED, which is what makes this a real test of the gate: an
    unnamed target is dropped by a later filter anyway, so a fixture using one
    passes whether the gate exists or not.
    """

    class _Shared(_RerootedConfig):
        def __init__(self, path: Path) -> None:
            super().__init__(path, workspaces=("personal",))

        def agent_root(self, name: str) -> Path:
            return self.workspace_root

        def agent_root_targets(self) -> list[tuple[Path, str]]:
            return [(self.workspace_root, "personal")]

    return _Shared(tmp_path)


def _kinds(context) -> set[str]:
    return {a.kind for a in detect_actions(context)}


def test_a_registered_workspace_with_no_folder_is_reported(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path)
    (tmp_path / "personal").mkdir()
    (tmp_path / "personal" / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    # 'work' has no directory at all.

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "workspace-root-missing"]

    assert [a.workspace for a in actions] == ["work"]
    assert actions[0].run_label   # a run button, because repair can fix it


def test_generated_assets_missing_beside_a_catalog_are_reported(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path, workspaces=("personal",))
    root = tmp_path / "personal"
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    # No .claude/skills, so the provider sees none of the catalog.

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "workspace-assets-stale"]

    assert len(actions) == 1
    assert ".claude/skills" in actions[0].detail


def test_a_root_with_no_catalog_is_not_called_stale(tmp_path: Path) -> None:
    """Nothing to generate from is not drift."""
    config = _RerootedConfig(tmp_path, workspaces=("personal",))
    root = tmp_path / "personal"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# G\n", encoding="utf-8")

    assert "workspace-assets-stale" not in _kinds(_context(tmp_path, config=config))


def test_the_drift_detectors_are_silent_before_the_re_rooting(tmp_path: Path) -> None:
    """One shared root, one guide, one catalog — 'this root has no assets' is the
    layout, not a fault."""
    config = _shared_layout_config(tmp_path)
    # A catalog at the install root and no generated dirs: exactly the shape the
    # stale-assets check looks for, which must NOT fire here.
    (tmp_path / "skills" / "demo").mkdir(parents=True)

    kinds = _kinds(_context(tmp_path, config=config))

    assert "workspace-root-missing" not in kinds
    assert "workspace-assets-stale" not in kinds


def test_pending_skill_triage_is_chat_only(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    triage = tmp_path / ".runtime" / "migration" / "skills-triage.md"
    triage.parent.mkdir(parents=True, exist_ok=True)
    triage.write_text("# Triage\n\n- skills/alpha\n- skills/beta\n", encoding="utf-8")

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "skill-triage-pending"]

    assert len(actions) == 1
    assert "2 skill" in actions[0].title
    # Moving someone's tooling between workspaces is a judgement, never a button.
    assert not actions[0].run_label
    assert actions[0].chat_prompt


def test_an_empty_triage_file_is_silent(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    triage = tmp_path / ".runtime" / "migration" / "skills-triage.md"
    triage.parent.mkdir(parents=True, exist_ok=True)
    triage.write_text("# Triage\n\nNothing needed a decision.\n", encoding="utf-8")

    assert "skill-triage-pending" not in _kinds(_context(tmp_path, config=config))


def test_env_vars_the_engine_no_longer_reads_are_surfaced(tmp_path: Path) -> None:
    """A setting that is set and silently ignored is worse than one that never
    existed: the operator believes it is in effect."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"CLAUDE_DEFAULT_MODEL_WORK": "opus", "CIAO_VAULT_ROOT": "x"}

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "legacy-env-ignored"]

    assert len(actions) == 1
    assert "CLAUDE_DEFAULT_MODEL_WORK" in actions[0].detail
    # A variable that IS still read must not be dragged in.
    assert "CIAO_VAULT_ROOT" not in actions[0].detail
    assert not actions[0].run_label


def test_no_legacy_env_vars_means_no_tile(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"CIAO_VAULT_ROOT": "x"}

    assert "legacy-env-ignored" not in _kinds(_context(tmp_path, config=config))


def test_a_shared_mcp_config_no_root_inherited_is_reported(tmp_path: Path) -> None:
    """A chat runs from its agent root, so an install-root `.mcp.json` is nobody's
    cwd: every server in it went unreachable with the file still sitting there."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"notion": {"command": "x"}, "n8n": {"url": "y"}}}),
        encoding="utf-8",
    )

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "workspace-mcp-uncomposed"]

    assert len(actions) == 1
    assert "n8n" in actions[0].detail and "notion" in actions[0].detail
    # Never a copy button: .mcp.json grants credentialed access, so which root
    # may reach which server is a decision, not a mechanical fix.
    assert not actions[0].run_label
    assert actions[0].chat_prompt


def test_roots_that_already_have_their_own_mcp_config_are_silent(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
        (tmp_path / name / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"notion": {}}}', encoding="utf-8")

    assert "workspace-mcp-uncomposed" not in _kinds(_context(tmp_path, config=config))


def test_no_shared_mcp_config_means_no_tile(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")

    assert "workspace-mcp-uncomposed" not in _kinds(_context(tmp_path, config=config))
