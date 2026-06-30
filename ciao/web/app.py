"""Starlette app factory for the PWA."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

from ciao.web.auth import AuthMiddleware, make_serializer
from ciao.web.commands import list_commands_endpoint, rate_limits_endpoint
from ciao.web.routes_api import (
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
    chat_handover,
    chat_images,
    chat_mark_read,
    chat_messages,
    chat_retry,
    chat_prompt,
    chat_new_session,
    chat_subagents,
    chat_voice,
    chats_mark_all_read,
    cli_stats,
    file_content,
    file_history,
    file_restore,
    create_project,
    create_project_chat,
    create_schedule,
    handover_merge,
    image_blob,
    local_handback,
    local_preflight,
    local_resync,
    local_status,
    list_all_chats,
    list_models,
    settings_routines,
    setup_finish_endpoint,
    setup_status_endpoint,
    list_automation,
    list_completed_projects,
    list_projects,
    list_schedules,
    list_workspaces,
    project_chats,
    project_complete,
    project_detail,
    package_status_endpoint,
    package_update_endpoint,
    voice_install_local_endpoint,
    project_restore,
    project_files_list,
    project_files_upload,
    run_schedule_now,
    schedule_detail,
    startup_status_endpoint,
    status_endpoint,
    workspace_binary,
    workspace_file,
    workspace_file_write,
    workspace_image,
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


def _spa_catchall(request):
    """Serve index.html for all unmatched routes (SPA client-side routing)."""
    requested = request.path_params.get("path", "")
    if requested:
        candidate = STATIC_DIR / requested
        if candidate.is_file():
            return FileResponse(candidate)
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return FileResponse(STATIC_DIR / "index.html", status_code=404)


def create_app(config, app_settings=None) -> Starlette:
    serializer = make_serializer(config.pwa_auth_token)

    routes = [
        # Auth
        Route("/api/auth", auth_login, methods=["POST"]),
        Route("/api/auth/logout", auth_logout, methods=["POST"]),
        Route("/api/auth/check", auth_check, methods=["GET"]),
        # Projects
        Route("/api/workspaces", list_workspaces, methods=["GET"]),
        Route("/api/projects", list_projects, methods=["GET"]),
        Route("/api/projects", create_project, methods=["POST"]),
        # Literal `completed` paths must precede the {project_id} pattern so
        # they aren't captured as a project id.
        Route("/api/projects/completed", list_completed_projects, methods=["GET"]),
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
        Route("/api/chats/{chat_id}/archive", chat_archive, methods=["POST"]),
        Route("/api/chats/{chat_id}/continue", chat_continue, methods=["POST"]),
        Route("/api/chats/{chat_id}/read", chat_mark_read, methods=["POST"]),
        Route("/api/chats/{chat_id}/retry", chat_retry, methods=["POST"]),
        Route("/api/chats/{chat_id}/prompt", chat_prompt, methods=["POST"]),
        Route("/api/chats/{chat_id}/messages", chat_messages, methods=["GET"]),
        Route("/api/chats/{chat_id}/subagents", chat_subagents, methods=["GET"]),
        Route("/api/chats/{chat_id}/voice", chat_voice, methods=["POST"]),
        Route("/api/chats/{chat_id}/images", chat_images, methods=["POST"]),
        Route("/api/images/{ref}", image_blob, methods=["GET"]),
        # Workspace file viewer (read-only; bounded to config.workspace_root)
        Route("/api/workspace-file", workspace_file, methods=["GET"]),
        Route("/api/workspace-file", workspace_file_write, methods=["POST"]),
        Route("/api/workspace-image", workspace_image, methods=["GET"]),
        Route("/api/workspace-binary", workspace_binary, methods=["GET"]),
        # File snapshots — History and Diff tabs in the file viewer.
        Route("/api/file-history", file_history, methods=["GET"]),
        Route("/api/file-content", file_content, methods=["GET"]),
        Route("/api/file-restore", file_restore, methods=["POST"]),
        # Schedules
        Route("/api/schedules", list_schedules, methods=["GET"]),
        Route("/api/schedules", create_schedule, methods=["POST"]),
        Route("/api/schedule-run/{schedule_id}", run_schedule_now, methods=["POST"]),
        Route("/api/schedules/{schedule_id}", schedule_detail, methods=["PATCH", "DELETE"]),
        # Automation status (read-only) — Settings → Automation page
        Route("/api/automation", list_automation, methods=["GET"]),
        # Slash commands (project + user level)
        Route("/api/commands", list_commands_endpoint, methods=["GET"]),
        # Claude subscription rate-limit buckets (5h / weekly / overage)
        Route("/api/rate-limits", rate_limits_endpoint, methods=["GET"]),
        # Models & Status
        Route("/api/models", list_models, methods=["GET"]),
        Route("/api/settings/routines", settings_routines, methods=["GET", "PATCH"]),
        Route("/api/status", status_endpoint, methods=["GET", "PATCH"]),
        Route("/api/startup-status", startup_status_endpoint, methods=["GET"]),
        Route("/api/setup-status", setup_status_endpoint, methods=["GET"]),
        Route("/api/package/status", package_status_endpoint, methods=["GET"]),
        Route("/api/package/update", package_update_endpoint, methods=["POST"]),
        Route("/api/voice/install-local", voice_install_local_endpoint, methods=["POST"]),
        Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"]),
        Route("/api/stats", cli_stats, methods=["GET"]),
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
        # WebSocket
        WebSocketRoute("/ws/chat/{chat_id}", ws_chat),
        WebSocketRoute("/ws/events", ws_events),
    ]

    # Serve Vite build output if it exists
    if STATIC_DIR.exists():
        routes.append(Mount("/assets", app=StaticFiles(directory=STATIC_DIR / "assets"), name="assets"))
        routes.append(Route("/{path:path}", _spa_catchall))

    middleware = [
        Middleware(SecurityHeadersMiddleware),
        Middleware(AuthMiddleware, serializer=serializer, auth_required=config.pwa_auth_required),
    ]

    app = Starlette(routes=routes, middleware=middleware)
    app.state.serializer = serializer
    app.state.config = config
    # Runtime-mutable settings store (Settings → Models tab). None in
    # tests that build the app without one; the route 503s in that case.
    app.state.app_settings = app_settings

    return app
