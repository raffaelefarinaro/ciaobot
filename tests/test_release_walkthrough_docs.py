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

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TASKS = ROOT / "skills" / "ciao-release" / "tasks"
SETTINGS_TASK = TASKS / "05-settings-and-assets.md"
PROJECTS_TASK = TASKS / "04-projects-and-schedule.md"
CHAT_TASK = TASKS / "02-chat-and-turns.md"
HOME_TASK = TASKS / "01-boot-and-home.md"


def _flow(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _plain(path: Path) -> str:
    """The same text with Markdown emphasis and code ticks removed.

    The assertions that name a control ("Complete action", "Delete project")
    must not depend on the emphasis a writer happens to put around the name.
    """
    raw = path.read_text(encoding="utf-8").replace("*", "").replace("`", "")
    return " ".join(raw.split())


def _near(text: str, anchor: str, needle: str, window: int = 400) -> bool:
    """Whether ``needle`` appears within ``window`` characters of any ``anchor``.

    A phrase that merely occurs somewhere in the task is not enough: "absent"
    only says something about the ••• menu when it sits next to the ••• menu.
    Every occurrence of the anchor is considered, so the anchor may be named
    more than once. Matching is case-insensitive.
    """
    haystack = text.lower()
    needle = needle.lower()
    at = haystack.find(anchor.lower())
    while at >= 0:
        if needle in haystack[max(0, at - window): at + window]:
            return True
        at = haystack.find(anchor.lower(), at + 1)
    return False


def _says(text: str, pattern: str) -> bool:
    """Whether the text matches ``pattern`` in a whitespace-tolerant way.

    A regex beats a chain of literal `not in` checks here: "the partial reply
    stays", "a partial reply is kept" and "partial replies remain" are one
    obsolete claim, and a literal list would let a rephrasing through.
    """
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _omits(text: str, pattern: str) -> bool:
    """Whether the text does *not* match ``pattern``.

    The negative half of the contract: a phrase that must not reappear,
    whatever it is wrapped around.
    """
    return not _says(text, pattern)


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

    # The default route is the label the UI actually renders: "General", not
    # the internal tab id "Home" (ProjectSidebar.vue SETTINGS_NAV). The
    # checkpoint name follows the label.
    assert "`/settings` (General" in text
    assert "Settings → Home" not in text
    assert "05-settings-01-home.png" not in text
    assert "05-settings-01-general.png" in text
    # The old copy double-counted the label: the General tab "carries General".
    assert "carries General" not in text

    # The before/after half is honest on a single-workspace install.
    assert "single-workspace" in lowered and "BLOCKED" in text

    # The route-specific rail cannot be claimed where a route has none.
    assert "more than one section" in lowered


def test_project_walk_separates_app_create_from_vault_completion() -> None:
    """Sidebar creation scaffolds a vault note; completing a vault note is a separate, isolated flow.

    Since #1350 a project created from the sidebar's `+` is vault-backed: the
    server scaffolds the project's vault note and binds it, so `ProjectView`
    offers a *Complete* action and drops the ••• *Delete project* menu, which
    is rendered only for a project with no `vault_folder`. The task must
    describe that shipped UX and keep completion on an isolated synthetic
    note, gated on the server's `completable` flag, with the completed path
    and retained body verified and BLOCKED reported when no isolated
    workspace exists.
    """
    text = _flow(PROJECTS_TASK)
    plain = _plain(PROJECTS_TASK)
    lowered = plain.lower()

    # The app project is vault-backed: creation scaffolds its vault note.
    assert "scaffold" in lowered
    assert "vault note" in lowered
    assert "vault-backed" in lowered
    assert "sidebar" in lowered

    # Its page offers Complete, because it has a vault folder.
    assert "complete action" in lowered
    # The walkthrough has to *require* that action, not merely mention it:
    # "need not offer a Complete action" would leave the fixture unverified.
    assert _near(plain, "complete action", "must offer")
    # The page's ••• menu is rendered only without a vault folder, so it is
    # absent here, and the walkthrough has to look for its absence next to
    # the menu it is talking about.
    assert "•••" in text
    assert "delete project" in lowered
    assert _near(plain, "delete project", "absent")
    assert "absent" in lowered

    # The obsolete plain-project expectations are gone.
    assert "plain app project" not in lowered
    assert _omits(plain, r"no\s+complete\s+action")
    assert _omits(plain, r"offers?\s+only\s+delete\s+project")
    assert _omits(plain, r"show(?:s|n)?\s+only\s+delete\s+project")
    assert _omits(plain, r"menu\s+offers?\s+only")

    # The fixture is left alone: neither Complete nor Delete is pressed.
    assert "untouched" in lowered
    assert "do not press" in lowered

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

    # Completing the unrelated synthetic note must not remove the app project.
    assert "still in the sidebar" in lowered

    # The blanket "the sidebar row must disappear" assertion is gone.
    assert "leaves the sidebar" not in lowered
    assert "row gone from the sidebar" not in lowered
    assert "sidebar cleared" not in lowered


def test_chat_walk_checks_in_flight_stop_without_requiring_a_partial_bubble() -> None:
    """The chat shows the final reply only, so Stop must be caught while the turn runs.

    Since #1395 a turn renders one bubble — the final reply. Commentary and
    partial output live in the Activity trace, so Stop leaves no partial
    bubble behind and there is nothing to keep. The task must prove continuity
    with a reply that is allowed to finish, then stop a *separate* turn while
    it is still in flight, and report a prompt stop, a free composer and no
    leftover spinner. A reply that finished before the click proves nothing
    about Stop: that checkpoint is BLOCKED when no in-flight stop can be
    exercised.
    """
    text = _flow(CHAT_TASK)
    plain = _plain(CHAT_TASK)
    lowered = plain.lower()

    # Continuity first: the short reply is allowed to finish.
    assert "still here" in text
    assert "let it finish" in lowered

    # The stop turn is separate and has to be caught while it runs.
    assert "separate" in lowered
    assert "in flight" in lowered
    assert _near(plain, "separate", "in flight")
    # A reply that was already finished is not proof of Stop, and no fixed
    # delay stands in for an in-flight turn.
    assert "proves nothing" in lowered
    assert "retry once" in lowered
    assert "fixed" in lowered
    assert _near(plain, "fixed", "second")
    assert "BLOCKED" in text

    # The outcome: prompt stop, usable composer, no stuck spinner.
    assert "promptly" in lowered
    assert "composer" in lowered
    assert "enabled" in lowered
    assert "spinner" in lowered

    # Only the final reply is a bubble; everything else is Activity.
    assert "final reply" in lowered
    assert "activity" in lowered
    assert "no partial" in lowered

    # The obsolete expectations are gone. The prose may still have to *say*
    # there is no partial reply, so the old demands are what is rejected.
    assert _omits(plain, r"partial\s+repl(?:y|ies)\s+(?:stays|remains?|is\s+kept|kept)")
    assert _omits(plain, r"partial\s+repl(?:y|ies)\s+is\s+not\s+discarded")


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
    assert "/api/auth/check" in text
    assert "reusing" in lowered or "reused" in lowered

    # The setup card is browser/device state, not engine state.
    assert "setup card" in lowered or "device setup" in lowered
    assert "browser" in lowered

    # A complete card legitimately stays until it is touched in this session
    # (`visible = touched || !complete` in HomeSetupCard.vue), so the reporting
    # rule must carry that interaction condition rather than firing on any
    # complete card still on screen.
    assert "interacted with in this session" in lowered
    assert "touched" in lowered

    # The old unconditional assumptions are gone: the card was said to hide
    # itself for any configured install, and the update tile was assumed present.
    assert "is meant to auto-hide" not in lowered
    assert "setup card absent," not in lowered
    assert "the package-update tile with an" not in lowered
