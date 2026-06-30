from __future__ import annotations

import re
from pathlib import Path


# Browser-only or internal routes that intentionally don't have a curl recipe
# in PWA_API.md → Agent recipes. Each entry needs a one-line reason so future
# contributors can decide whether their new route really belongs here.
BROWSER_OR_INTERNAL_ROUTES: dict[str, str] = {
    "/api/auth": "browser login flow; the recipe's auth step covers this",
    "/api/auth/logout": "browser logout; clears the session cookie",
    "/api/setup/finish": "browser first-run setup handoff; writes local config and requests restart",
    "/api/projects/{project_id}/files": "browser multipart upload; agents edit vault files directly",
    "/api/chats/{chat_id}/voice": "browser voice upload",
    "/api/chats/{chat_id}/images": "browser image upload",
    "/api/chats/{chat_id}/continue": "browser continuation of archived chat",
    "/api/chats/{chat_id}/prompt": "agents trigger prompts via ciao create-chat, not curl",
    "/api/schedules": "agents create/edit schedules via the ciao-schedules skill, not curl",
    "/api/schedules/{schedule_id}": "agents edit schedules via the ciao-schedules skill, not curl",
    "/api/status": "internal status PATCH",
    "/api/push/subscribe": "browser push registration",
    "/api/push/unsubscribe": "browser push registration",
    "/api/admin/snapshot": "admin internal; deploy is the agent-callable wrapper",
    "/api/package/update": "browser package update; upgrades ciao package and restarts",
}


STATE_CHANGING_METHODS = {"POST", "PATCH", "DELETE"}


def _parse_routes(app_source: str) -> list[tuple[str, frozenset[str]]]:
    """Return (path, methods) tuples for every explicit Route in app.py."""
    pattern = re.compile(
        r'Route\(\s*"([^"]+)"\s*,\s*[^,]+,\s*methods=\[([^\]]+)\]',
        re.MULTILINE,
    )
    out: list[tuple[str, frozenset[str]]] = []
    for match in pattern.finditer(app_source):
        path = match.group(1)
        methods = frozenset(
            m.strip().strip('"').strip("'") for m in match.group(2).split(",") if m.strip()
        )
        out.append((path, methods))
    return out


def test_pwa_api_documents_all_explicit_app_routes() -> None:
    repo = Path(__file__).resolve().parents[1]
    app_source = (repo / "ciao" / "web" / "app.py").read_text(encoding="utf-8")
    api_doc = (repo / "PWA_API.md").read_text(encoding="utf-8")

    route_paths = re.findall(r'(?:Route|WebSocketRoute)\("([^"]+)"', app_source)
    missing = [
        path
        for path in route_paths
        if path != "/{path:path}" and path not in api_doc
    ]

    assert missing == []


def test_state_changing_routes_have_agent_recipe_or_allowlist() -> None:
    """Every POST/PATCH/DELETE /api/... route must either appear in
    PWA_API.md → "## Agent recipes" or be allowlisted in
    BROWSER_OR_INTERNAL_ROUTES above. This forces a recipe-or-justification
    decision for every new agent-relevant action."""
    repo = Path(__file__).resolve().parents[1]
    app_source = (repo / "ciao" / "web" / "app.py").read_text(encoding="utf-8")
    api_doc = (repo / "PWA_API.md").read_text(encoding="utf-8")

    # Slice the doc to the "## Agent recipes" section so a path mention
    # elsewhere (e.g. the routes table) doesn't satisfy the check.
    sections = re.split(r"^## ", api_doc, flags=re.MULTILINE)
    recipes_blob = ""
    for sec in sections:
        if sec.startswith("Agent recipes"):
            recipes_blob = sec
            break
    assert recipes_blob, "PWA_API.md is missing the '## Agent recipes' section"

    state_changing_paths: set[str] = set()
    for path, methods in _parse_routes(app_source):
        if not path.startswith("/api/"):
            continue
        if methods & STATE_CHANGING_METHODS:
            state_changing_paths.add(path)

    missing: list[str] = []
    for path in sorted(state_changing_paths):
        if path in BROWSER_OR_INTERNAL_ROUTES:
            continue
        # Match the route shape, not literal placeholder names. A curl
        # example writes `/api/projects/$PID/chats`, not the literal
        # `/api/projects/{project_id}/chats`. Split on `{name}` placeholders,
        # escape each literal segment, and rejoin with a non-slash wildcard.
        parts = re.split(r"\{[^}]+\}", path)
        pattern = r"[^/\s'\"`]+".join(re.escape(p) for p in parts)
        if re.search(pattern, recipes_blob):
            continue
        missing.append(path)

    assert missing == [], (
        "State-changing routes missing from PWA_API.md → Agent recipes "
        "(add a curl example or allowlist them in BROWSER_OR_INTERNAL_ROUTES "
        f"with a one-line reason): {missing}"
    )

    # The allowlist must not drift either: every entry must still correspond
    # to a real state-changing route, or it's stale and should be removed.
    stale = sorted(set(BROWSER_OR_INTERNAL_ROUTES) - state_changing_paths)
    assert stale == [], (
        "BROWSER_OR_INTERNAL_ROUTES has entries for routes that no longer "
        f"exist or are no longer state-changing: {stale}"
    )
