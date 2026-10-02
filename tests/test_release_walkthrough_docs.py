"""Pin the release walkthrough tasks to the UI, routes and handlers they describe (#951).

The walkthrough is prose a person follows in a browser, so the failure mode is
instruction drift: a step names a control that no longer exists, or an endpoint
whose meaning changed. These tests pin the *semantic* instructions — the routes
that must be walked, the scoped endpoints that must be compared, the
completion gate — not the whole Markdown, so wording can change while the
instruction stays honest.

Phrase matching runs against whitespace-collapsed text: the tasks are
hand-wrapped Markdown, and a phrase split across a line break must not read as
a missing instruction.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TASKS = ROOT / "skills" / "ciao-release" / "tasks"
SETTINGS_TASK = TASKS / "05-settings-and-assets.md"
PROJECTS_TASK = TASKS / "04-projects-and-schedule.md"
HOME_TASK = TASKS / "01-boot-and-home.md"


def _flow(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_settings_walk_names_current_routes_and_scoped_checks() -> None:
    """Settings is seven routes; the four asset lists are compared through the scoped API.

    The old task called Settings one scrolling page with an "On this page" rail
    and treated a bare `curl` 401 as proof the endpoint worked. Both are wrong:
    each section is its own route, and a 401 only proves the unauthenticated
    request was rejected. The copy must also stop requiring the two workspaces'
    inventories to differ, because Global rows legitimately do not.
    """
    text = _flow(SETTINGS_TASK)
    lowered = text.lower()

    # All seven supported routes, including the default.
    for route in (
        "`/settings`",
        "`/settings/workspaces`",
        "`/settings/models`",
        "`/settings/skills`",
        "`/settings/subagents`",
        "`/settings/commands`",
        "`/settings/mcp`",
    ):
        assert route in text, route

    # The workspace-scoped endpoints the app itself calls, not the unscoped ones.
    for endpoint in (
        "/api/admin/skills?workspace=",
        "/api/agent-assets?workspace=",
        "/api/mcp/status?workspace=",
    ):
        assert endpoint in text, endpoint

    # The old "one page" claim is gone; the rail is route-specific, not one scroll.
    assert "one long scrolling page" not in lowered
    assert "one scrolling page" not in lowered

    # A 401 is explicitly not a pass, and not treated as endpoint verification.
    assert "401" in text
    assert "bare `curl`" in lowered
    assert "only proves" in lowered or "not evidence" in lowered
    # The old "a 401 here is a pass" claim is gone.
    assert "a 401 here is a pass" not in lowered
    assert "401 is a pass" not in lowered

    # Global rows are explicitly allowed to be identical across workspaces.
    assert "global" in lowered
    assert "identical across workspaces" in lowered or "same global subagents" in lowered


def test_project_walk_separates_app_create_from_vault_completion() -> None:
    """A plain app project has no Complete; completing a vault note is a separate, isolated flow.

    The old task created a sidebar project, marked it Complete, and required its
    row to leave the sidebar — neither is reachable: `ProjectView` offers only
    *Delete project* on a project with no vault folder, and a completed vault
    note moves the note, not an unrelated project. The instructions must split
    the two, gate on the server's `completable` flag, verify the completed path
    and retained body, and report BLOCKED when no isolated workspace exists.
    """
    text = _flow(PROJECTS_TASK)
    lowered = text.lower()

    # The plain app project is its own smoke step, and is left intact.
    assert "plain app project" in lowered
    assert "Delete project" in text
    assert "leave this project" in lowered and "untouched" in lowered

    # Completion is a vault note under projects/, reached from To decide.
    assert "/memory/review?show=revisit" in text
    assert "completable" in text
    assert "projects/completed/" in text
    assert "status: completed" in text

    # The isolated fixture: a throwaway workspace, and BLOCKED when unavailable.
    assert "isolated" in lowered
    assert "BLOCKED" in text
    assert "real workspace" in lowered or ("real" in lowered and "workspace" in lowered)

    # Retention is checked, and cleanup is deliberately not done.
    assert "retained" in lowered
    assert "no cleanup" in lowered or "nothing at all in the real workspace" in lowered

    # The blanket "the sidebar row must disappear" assertion is gone.
    assert "leaves the sidebar" not in lowered
    assert "row gone from the sidebar" not in lowered
    assert "sidebar cleared" not in lowered


def test_home_walk_requires_update_tile_only_for_newer_published_version() -> None:
    """The update tile is conditional, the setup card is device state, and a reused session is not proof.

    A candidate ahead of the latest published release correctly shows no update
    tile, so the task must check `/api/package/status` rather than assume the
    tile is always there. It must also not treat "no login form" as proof that
    authentication is off: that can just be an existing session.
    """
    text = _flow(HOME_TASK)
    lowered = text.lower()

    # The conditional update rule, read from the packaged status endpoint.
    assert "/api/package/status" in text
    assert "update_available" in text
    assert "latest" in lowered and "published" in lowered
    assert "Update in Settings" in text

    # A reused authenticated session is explicitly not proof auth is disabled.
    assert "session" in lowered
    assert "auth_required" in text
    assert "reusing" in lowered or "reused" in lowered

    # The setup card is browser/device state, not engine state.
    assert "setup card" in lowered or "device setup" in lowered
    assert "browser" in lowered

    # The old unconditional assumptions are gone: the card was said to hide
    # itself for any configured install, and the update tile was assumed present.
    assert "is meant to auto-hide" not in lowered
    assert "setup card absent," not in lowered
    assert "the package-update tile with an" not in lowered
