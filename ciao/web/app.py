"""Starlette app factory for the PWA."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse, Response

from ciao.package_version import make_cached_package_status
from ciao.mcp_server import mcp_status_endpoint, mcp_usage_endpoint
from ciao.web.auth import AuthMiddleware, make_serializer
from ciao.web.agent_assets import (
    agent_assets_endpoint,
    create_command_endpoint,
    create_subagent_endpoint,
    delete_command_endpoint,
    delete_subagent_endpoint,
    update_command_endpoint,
    update_subagent_endpoint,
    workspace_health_endpoint,
    workspace_health_fix_endpoint,
)
from ciao.web.commands import list_commands_endpoint, rate_limits_endpoint
from ciao.web.routes_api import (
    admin_add_skill,
    admin_deploy,
    admin_snapshot,
    admin_skills,
    admin_status,
    auth_check,
    auth_login,
    auth_logout,
    chat_archive,
    chat_continue,
    chat_detail,
    chat_fork,
    chat_handover,
    chat_provider_subchats_list,
    chat_provider_subchats_create,
    provider_subchat_events,
    provider_subchat_message,
    provider_subchat_close,
    provider_subchat_cancel,
    provider_subchat_extend,
    provider_subchat_permission_response,
    provider_subchat_question_response,
    chat_images,
    chat_mark_read,
    chat_messages,
    chat_retry,
    chat_prompt,
    chat_new_session,
    chat_subagents,
    chat_speak,
    chat_voice,
    chats_mark_all_read,
    cli_stats,
    file_content,
    file_history,
    file_restore,
    create_project,
    create_project_chat,
    create_schedule,
    debug_issues,
    handover_merge,
    image_blob,
    local_handback,
    local_preflight,
    local_resync,
    local_status,
    list_all_chats,
    list_models,
    delete_workspace_setting,
    gws_integration_settings,
    gws_install,
    gws_save_client_secret,
    gws_auth_url,
    gws_exchange_code,
    gws_disconnect,
    gws_relogin_start,
    gws_relogin_status,
    gws_relogin_cancel,
    provider_connection_action,
    provider_config_settings,
    settings_routines,
    setup_finish_endpoint,
    setup_list_dirs_endpoint,
    setup_mkdir_endpoint,
    setup_status_endpoint,
    list_automation,
    trigger_backfill_insights,
    list_completed_projects,
    list_projects,
    list_loops,
    list_schedules,
    list_workspaces,
    project_chats,
    project_complete,
    project_detail,
    reorder_projects,
    package_changelog_endpoint,
    package_status_endpoint,
    package_update_endpoint,
    tts_install_local_endpoint,
    voice_install_local_endpoint,
    libreoffice_status_endpoint,
    libreoffice_install_endpoint,
    apfel_install_endpoint,
    project_restore,
    project_files_list,
    project_files_upload,
    run_schedule_now,
    schedule_detail,
    create_loop,
    loop_detail,
    run_loop_now,
    startup_status_endpoint,
    active_chats_endpoint,
    open_chat_endpoint,
    status_endpoint,
    upsert_workspace_setting,
    vault_backlinks,
    vault_markdown_paths,
    workspace_binary,
    workspace_file,
    workspace_file_write,
    workspace_image,
    workspace_open,
)
from ciao.web.routes_chat import ws_chat, ws_events
from ciao.web.routes_push import (
    push_public_key,
    push_status,
    push_subscribe,
    push_subscription_check,
    push_unsubscribe,
)
from ciao.web.security import SecurityHeadersMiddleware

STATIC_DIR = Path(__file__).parent / "static"


# Hashed build assets never change under a given name, so cache them forever;
# the entry point and service worker must never be cached or clients pin a
# stale build.
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
_NO_CACHE = "no-cache, no-store, must-revalidate"


class CacheControlStaticFiles(StaticFiles):
    """StaticFiles subclass that sets long-term caching for hashed assets."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = _IMMUTABLE_CACHE
        return response


def _spa_catchall(request):
    """Serve index.html for unmatched routes (SPA client-side routing).

    Hashed ``/assets`` are served (and cached) by the mount registered before
    this catchall; here we only guard the never-cache entry points.
    """
    requested = request.path_params.get("path", "")
    if requested:
        candidate = STATIC_DIR / requested
        if candidate.is_file():
            headers = {"Cache-Control": _NO_CACHE} if requested in ("sw.js", "manifest.json") else {}
            return FileResponse(candidate, headers=headers)
    index = STATIC_DIR / "index.html"
    headers = {"Cache-Control": _NO_CACHE}
    if index.exists():
        return FileResponse(index, headers=headers)
    return FileResponse(STATIC_DIR / "index.html", status_code=404, headers=headers)


def create_app(config, app_settings=None, mcp_service=None) -> Starlette:
    serializer = make_serializer(config.pwa_auth_token)

    routes = [
        # Auth
        Route("/api/auth", auth_login, methods=["POST"]),
        Route("/api/auth/logout", auth_logout, methods=["POST"]),
        Route("/api/auth/check", auth_check, methods=["GET"]),
        # Projects
        Route("/api/workspaces", list_workspaces, methods=["GET"]),
        Route("/api/workspaces", upsert_workspace_setting, methods=["POST"]),
        Route("/api/workspaces/{name}", upsert_workspace_setting, methods=["PATCH"]),
        Route("/api/workspaces/{name}", delete_workspace_setting, methods=["DELETE"]),
        Route("/api/projects", list_projects, methods=["GET"]),
        Route("/api/projects", create_project, methods=["POST"]),
        # Literal `completed` paths must precede the {project_id} pattern so
        # they aren't captured as a project id.
        Route("/api/projects/completed", list_completed_projects, methods=["GET"]),
        Route("/api/projects/reorder", reorder_projects, methods=["POST"]),
        Route("/api/projects/completed/restore", project_restore, methods=["POST"]),
        Route("/api/projects/{project_id}", project_detail, methods=["PATCH", "DELETE"]),
        Route("/api/projects/{project_id}/complete", project_complete, methods=["POST"]),
        Route("/api/projects/{project_id}/chats", project_chats, methods=["GET"]),
        Route("/api/projects/{project_id}/chats", create_project_chat, methods=["POST"]),
        Route("/api/projects/{project_id}/files", project_files_list, methods=["GET"]),
        Route("/api/projects/{project_id}/files", project_files_upload, methods=["POST"]),
        # Chats
        Route("/api/chats", list_all_chats, methods=["GET"]),
        # /read-all must precede /{chat_id} so the literal isn't swallowed.
        Route("/api/chats/read-all", chats_mark_all_read, methods=["POST"]),
        Route("/api/chats/{chat_id}", chat_detail, methods=["PATCH", "DELETE"]),
        Route("/api/chats/{chat_id}/new", chat_new_session, methods=["POST"]),
        Route("/api/chats/{chat_id}/handover", chat_handover, methods=["POST"]),
        Route("/api/chats/{chat_id}/fork", chat_fork, methods=["POST"]),
        Route("/api/chats/{chat_id}/provider-subchats", chat_provider_subchats_list, methods=["GET"]),
        Route("/api/chats/{chat_id}/provider-subchats", chat_provider_subchats_create, methods=["POST"]),
        Route("/api/provider-subchats/{subchat_id}/events", provider_subchat_events, methods=["GET"]),
        Route("/api/provider-subchats/{subchat_id}/messages", provider_subchat_message, methods=["POST"]),
        Route("/api/provider-subchats/{subchat_id}/close", provider_subchat_close, methods=["POST"]),
        Route("/api/provider-subchats/{subchat_id}/cancel", provider_subchat_cancel, methods=["POST"]),
        Route("/api/provider-subchats/{subchat_id}/extend", provider_subchat_extend, methods=["POST"]),
        Route("/api/provider-subchats/{subchat_id}/permission-response", provider_subchat_permission_response, methods=["POST"]),
        Route("/api/provider-subchats/{subchat_id}/question-response", provider_subchat_question_response, methods=["POST"]),
        Route("/api/chats/{chat_id}/archive", chat_archive, methods=["POST"]),
        Route("/api/chats/{chat_id}/continue", chat_continue, methods=["POST"]),
        Route("/api/chats/{chat_id}/read", chat_mark_read, methods=["POST"]),
        Route("/api/chats/{chat_id}/retry", chat_retry, methods=["POST"]),
        Route("/api/chats/{chat_id}/prompt", chat_prompt, methods=["POST"]),
        Route("/api/chats/{chat_id}/messages", chat_messages, methods=["GET"]),
        Route("/api/chats/{chat_id}/subagents", chat_subagents, methods=["GET"]),
        Route("/api/chats/{chat_id}/voice", chat_voice, methods=["POST"]),
        Route("/api/chats/{chat_id}/speak", chat_speak, methods=["POST"]),
        Route("/api/chats/{chat_id}/images", chat_images, methods=["POST"]),
        Route("/api/images/{ref}", image_blob, methods=["GET"]),
        # Host file viewer/editor. Absolute paths are intentional; endpoints
        # enforce type and size allowlists rather than a workspace sandbox.
        Route("/api/workspace-file", workspace_file, methods=["GET"]),
        Route("/api/workspace-file", workspace_file_write, methods=["POST"]),
        Route("/api/vault-markdown-paths", vault_markdown_paths, methods=["GET"]),
        Route("/api/vault/backlinks", vault_backlinks, methods=["GET"]),
        Route("/api/workspace-image", workspace_image, methods=["GET"]),
        Route("/api/workspace-binary", workspace_binary, methods=["GET"]),
        Route("/api/workspace-open", workspace_open, methods=["POST"]),
        Route("/api/libreoffice-status", libreoffice_status_endpoint, methods=["GET"]),
        Route("/api/libreoffice-install", libreoffice_install_endpoint, methods=["POST"]),
        Route("/api/apfel/install", apfel_install_endpoint, methods=["POST"]),
        # File snapshots — History and Diff tabs in the file viewer.
        Route("/api/file-history", file_history, methods=["GET"]),
        Route("/api/file-content", file_content, methods=["GET"]),
        Route("/api/file-restore", file_restore, methods=["POST"]),
        # Schedules
        Route("/api/schedules", list_schedules, methods=["GET"]),
        Route("/api/schedules", create_schedule, methods=["POST"]),
        Route("/api/schedule-run/{schedule_id}", run_schedule_now, methods=["POST"]),
        Route("/api/schedules/{schedule_id}", schedule_detail, methods=["PATCH", "DELETE"]),
        # Loops — in-chat interval automations (Automations page)
        Route("/api/loops", list_loops, methods=["GET"]),
        Route("/api/loops", create_loop, methods=["POST"]),
        Route("/api/loop-run/{loop_id}", run_loop_now, methods=["POST"]),
        Route("/api/loops/{loop_id}", loop_detail, methods=["PATCH", "DELETE"]),
        # Automation status (read-only) — Settings → Automation page
        Route("/api/automation", list_automation, methods=["GET"]),
        Route("/api/automation/backfill-insights", trigger_backfill_insights, methods=["POST"]),
        # Runtime issue report (dev mode only) — Settings → Debug card
        Route("/api/debug/issues", debug_issues, methods=["GET"]),
        # Slash commands (project + user level)
        Route("/api/commands", list_commands_endpoint, methods=["GET"]),
        # Agent-facing instruction, subagent, and command assets.
        Route("/api/agent-assets", agent_assets_endpoint, methods=["GET"]),
        Route("/api/workspace-health", workspace_health_endpoint, methods=["GET"]),
        Route("/api/workspace-health/fix", workspace_health_fix_endpoint, methods=["POST"]),
        Route("/api/agent-assets/subagents", create_subagent_endpoint, methods=["POST"]),
        Route("/api/agent-assets/subagents/{name}", update_subagent_endpoint, methods=["PATCH"]),
        Route("/api/agent-assets/subagents/{name}", delete_subagent_endpoint, methods=["DELETE"]),
        Route("/api/agent-assets/commands", create_command_endpoint, methods=["POST"]),
        Route("/api/agent-assets/commands/{name}", update_command_endpoint, methods=["PATCH"]),
        Route("/api/agent-assets/commands/{name}", delete_command_endpoint, methods=["DELETE"]),
        # Claude subscription rate-limit buckets (5h / weekly / overage)
        Route("/api/rate-limits", rate_limits_endpoint, methods=["GET"]),
        # Models & Status
        Route("/api/models", list_models, methods=["GET"]),
        Route("/api/settings/routines", settings_routines, methods=["GET", "PATCH"]),
        Route("/api/settings/providers", provider_config_settings, methods=["GET", "PATCH"]),
        Route(
            "/api/settings/providers/{provider}/{action}",
            provider_connection_action,
            methods=["POST"],
        ),
        Route("/api/integrations/gws", gws_integration_settings, methods=["GET"]),
        Route("/api/integrations/gws/install", gws_install, methods=["POST"]),
        Route("/api/integrations/gws/client-secret", gws_save_client_secret, methods=["POST"]),
        Route("/api/integrations/gws/auth-url", gws_auth_url, methods=["POST"]),
        Route("/api/integrations/gws/exchange", gws_exchange_code, methods=["POST"]),
        Route("/api/integrations/gws/disconnect", gws_disconnect, methods=["POST"]),
        Route("/api/integrations/gws/relogin/start", gws_relogin_start, methods=["POST"]),
        Route("/api/integrations/gws/relogin/status", gws_relogin_status, methods=["GET"]),
        Route("/api/integrations/gws/relogin/cancel", gws_relogin_cancel, methods=["POST"]),
        Route("/api/status", status_endpoint, methods=["GET", "PATCH"]),
        Route("/api/startup-status", startup_status_endpoint, methods=["GET"]),
        Route("/api/active-chats", active_chats_endpoint, methods=["GET"]),
        Route("/api/open-chat/{chat_id}", open_chat_endpoint, methods=["GET"]),
        Route("/api/setup-status", setup_status_endpoint, methods=["GET"]),
        Route("/api/package/status", package_status_endpoint, methods=["GET"]),
        Route("/api/package/changelog", package_changelog_endpoint, methods=["GET"]),
        Route("/api/package/update", package_update_endpoint, methods=["POST"]),
        Route("/api/voice/install-local", voice_install_local_endpoint, methods=["POST"]),
        Route("/api/tts/install-local", tts_install_local_endpoint, methods=["POST"]),
        Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"]),
        Route("/api/setup/list-dirs", setup_list_dirs_endpoint, methods=["GET"]),
        Route("/api/setup/mkdir", setup_mkdir_endpoint, methods=["POST"]),
        Route("/api/stats", cli_stats, methods=["GET"]),
        Route("/api/mcp/status", mcp_status_endpoint, methods=["GET"]),
        Route("/api/mcp/usage", mcp_usage_endpoint, methods=["GET"]),
        # Push notifications
        Route("/api/push/public-key", push_public_key, methods=["GET"]),
        Route("/api/push/subscribe", push_subscribe, methods=["POST"]),
        Route("/api/push/unsubscribe", push_unsubscribe, methods=["POST"]),
        Route("/api/push/status", push_status, methods=["GET"]),
        Route("/api/push/subscription", push_subscription_check, methods=["GET"]),
        # Per-device working-branch flow: commit-to-main + agent-merged handover
        Route("/api/local/status", local_status, methods=["GET"]),
        Route("/api/local/preflight", local_preflight, methods=["GET"]),
        Route("/api/local/handback", local_handback, methods=["POST"]),
        Route("/api/local/resync", local_resync, methods=["POST"]),
        Route("/api/handover/merge", handover_merge, methods=["POST"]),
        # Admin
        Route("/api/admin/snapshot", admin_snapshot, methods=["POST"]),
        Route("/api/admin/deploy", admin_deploy, methods=["POST"]),
        Route("/api/admin/status", admin_status, methods=["GET"]),
        Route("/api/admin/skills", admin_skills, methods=["GET"]),
        Route("/api/admin/skills/add", admin_add_skill, methods=["POST"]),
        # WebSocket
        WebSocketRoute("/ws/chat/{chat_id}", ws_chat),
        WebSocketRoute("/ws/events", ws_events),
    ]

    # Agent-facing MCP is mounted before the SPA catch-all. Its own bearer
    # authentication is independent from the browser's signed session cookie.
    if mcp_service is not None:
        routes.append(Mount("/mcp", app=mcp_service.http_app, name="ciaobot-mcp"))

    # Serve Vite build output if it exists. The hashed-asset mount is only added
    # when the build output is present — assets/ is generated by `npm run build`
    # and absent in a fresh checkout / test run, where StaticFiles would raise.
    if STATIC_DIR.exists():
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.is_dir():
            routes.append(Mount("/assets", app=CacheControlStaticFiles(directory=assets_dir), name="assets"))
        routes.append(Route("/{path:path}", _spa_catchall))

    middleware = [
        Middleware(SecurityHeadersMiddleware),
        Middleware(AuthMiddleware, serializer=serializer, auth_required=config.pwa_auth_required),
    ]

    lifespan = None
    if mcp_service is not None:
        @asynccontextmanager
        async def _mcp_lifespan(_app):
            async with mcp_service.lifespan():
                yield

        lifespan = _mcp_lifespan

    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.serializer = serializer
    app.state.config = config
    # Runtime-mutable settings store (Settings → Models tab). None in
    # tests that build the app without one; the route 503s in that case.
    app.state.app_settings = app_settings
    app.state.mcp_service = mcp_service
    # Cache the GitHub release lookup so opening Settings repeatedly does not
    # exhaust the API rate limit (especially on shared/NAT egress IPs).
    app.state.package_status_fetcher = make_cached_package_status()

    return app
