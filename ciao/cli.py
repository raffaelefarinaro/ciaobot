"""Command-line entrypoint for the packaged Ciaobot app."""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
from importlib import resources
from pathlib import Path
import urllib.error
import urllib.request

from ciao import dev, package_smoke, public_release


def _run_server() -> int:
    from ciao.main import main as server_main

    server_main()
    return 0


def _copy_tree(src, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_dir():
            _copy_tree(item, dest / item.name)
        else:
            (dest / item.name).write_bytes(item.read_bytes())


def _copy_tree_if_missing(src, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            written.extend(_copy_tree_if_missing(item, target))
        elif not target.exists():
            target.write_bytes(item.read_bytes())
            written.append(target)
    return written


def _write_if_missing(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")


def _render_launchd_plist(
    *,
    workspace: Path,
    python_path: str,
    port: int,
    path: str = "",
) -> str:
    template = resources.files("ciao.stock").joinpath(
        "deploy", "com.ciao.server.plist.tmpl"
    ).read_text(encoding="utf-8")
    # Under launchd the default PATH omits Homebrew, so subprocess calls to
    # npm/node/git fail. Bake the user's PATH from setup time into the plist.
    resolved_path = path or os.environ.get("PATH", "")
    replacements = {
        "{{CIAO_WORKSPACE}}": html.escape(str(workspace), quote=False),
        "{{CIAO_PYTHON}}": html.escape(python_path, quote=False),
        "{{CIAO_PORT}}": html.escape(str(port), quote=False),
        "{{CIAO_PATH}}": html.escape(resolved_path, quote=False),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def _write_launchd_plist(
    *,
    workspace: Path,
    launch_agents_dir: Path,
    python_path: str,
    port: int,
    path: str = "",
) -> Path:
    plist = launch_agents_dir.expanduser() / "com.ciao.server.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        _render_launchd_plist(
            workspace=workspace,
            python_path=python_path,
            port=port,
            path=path,
        ),
        encoding="utf-8",
    )
    return plist


def _setup_token_path(workspace: Path) -> Path:
    return workspace / ".runtime" / "setup-token"


def _ensure_setup_token(workspace: Path) -> str:
    path = _setup_token_path(workspace)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    return token


def _write_app_shortcut(
    *,
    workspace: Path,
    app_dir: Path,
    port: int,
) -> Path:
    token = _ensure_setup_token(workspace)
    app_root = app_dir.expanduser() / "Ciaobot.app"
    contents = app_root / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
                '<plist version="1.0">',
                '<dict>',
                '  <key>CFBundleName</key>',
                '  <string>Ciaobot</string>',
                '  <key>CFBundleExecutable</key>',
                '  <string>Ciaobot</string>',
                '  <key>CFBundleIdentifier</key>',
                '  <string>local.ciaobot.app</string>',
                '  <key>CFBundlePackageType</key>',
                '  <string>APPL</string>',
                '</dict>',
                '</plist>',
                '',
            ]
        ),
        encoding="utf-8",
    )
    url = f"http://localhost:{port}/?setup={token}"
    executable = macos / "Ciaobot"
    executable.write_text(
        "#!/bin/sh\n"
        f'open "{url}"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return app_root


def setup_workspace(
    workspace: Path | str,
    *,
    auth_token: str | None = None,
    auth_required: bool = True,
    push_contact: str | None = None,
    vault_root: Path | str | None = None,
    vault_mode: str = "scratch",
    python_path: str | None = None,
    port: int = 8443,
    launch_agents_dir: Path | str | None = None,
    app_dir: Path | str | None = None,
) -> list[Path]:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    vault_value = str(vault_root) if vault_root is not None else "memory-vault"
    vault_path = Path(vault_value).expanduser()
    if not vault_path.is_absolute():
        vault_path = root / vault_path

    env_path = root / ".env"
    if not env_path.exists():
        token = auth_token or secrets.token_urlsafe(32)
        contact = push_contact or "mailto:you@example.com"
        lines = [
            f"PWA_AUTH_TOKEN={token}",
        ]
        if not auth_required:
            lines.append("PWA_AUTH_REQUIRED=false")
        lines.extend([
            f"CIAO_PUSH_CONTACT={contact}",
            "CIAO_WORKSPACE=.",
            f"CIAO_VAULT_ROOT={vault_value}",
            f"CIAO_VAULT_MODE={vault_mode}",
            "CIAO_RUNTIME_ROOT=.runtime",
            "",
        ])
        env_path.write_text(
            "\n".join(lines),
            encoding="utf-8",
        )
        written.append(env_path)

    stock = resources.files("ciao.stock")
    stock_agents = stock.joinpath("agents")
    stock_commands = stock.joinpath("commands")
    stock_workspace = stock.joinpath("workspace")
    _copy_tree(stock_agents, root / ".claude" / "agents")
    _copy_tree(stock_commands, root / ".claude" / "commands")
    written.extend([root / ".claude" / "agents", root / ".claude" / "commands"])
    written.extend(_copy_tree_if_missing(stock_workspace, root))

    runtime_schedules = root / ".runtime" / "schedules.json"
    _write_if_missing(
        runtime_schedules,
        stock.joinpath("schedules.json").read_text(encoding="utf-8"),
    )
    written.append(runtime_schedules)

    _write_if_missing(
        vault_path / "MEMORY.md",
        "# Memory\n\nDurable workspace memory lives here.\n",
    )
    _write_if_missing(
        vault_path / "INDEX.md",
        "# Vault Index\n\nGenerated by `ciao vault-index`.\n",
    )
    _write_if_missing(
        vault_path / "projects" / "active" / "general" / "general.md",
        "---\ntype: project\ntitle: General\ndescription: Default project.\nstatus: active\ntags: [project]\n---\n\n# General\n",
    )
    (vault_path / "Logs" / "Chats").mkdir(parents=True, exist_ok=True)
    written.append(vault_path)

    launch_dir = Path(launch_agents_dir) if launch_agents_dir is not None else Path.home() / "Library" / "LaunchAgents"
    app_root_dir = Path(app_dir) if app_dir is not None else Path.home() / "Applications"
    written.append(_write_launchd_plist(
        workspace=root,
        launch_agents_dir=launch_dir,
        python_path=python_path or sys.executable,
        port=port,
        path=os.environ.get("PATH", ""),
    ))
    written.append(_write_app_shortcut(
        workspace=root,
        app_dir=app_root_dir,
        port=port,
    ))

    return written


def _setup_command(args: argparse.Namespace) -> int:
    written = setup_workspace(
        args.workspace,
        auth_token=args.auth_token,
        push_contact=args.push_contact,
        python_path=args.python,
        port=args.port,
        launch_agents_dir=args.launch_agents_dir,
        app_dir=args.app_dir,
    )
    for path in written:
        print(path)
    plist = Path(args.launch_agents_dir).expanduser() / "com.ciao.server.plist"
    if args.load_launchd:
        subprocess.run(["launchctl", "unload", str(plist)], check=False)
        return subprocess.run(["launchctl", "load", "-w", str(plist)], check=False).returncode
    print(f"LaunchAgent not loaded. To load it: launchctl load -w {plist}")
    return 0


def _auth_command_for_provider(provider: str) -> list[str]:
    if provider == "claude":
        from ciao.providers.claude import get_bundled_claude_path

        binary = get_bundled_claude_path() or shutil.which("claude")
        if not binary:
            raise FileNotFoundError("Claude CLI not found")
        return [binary, "login"]
    if provider == "ollama":
        return ["ollama", "signin"]
    raise ValueError(f"Unknown provider '{provider}'")


def _auth_command(args: argparse.Namespace) -> int:
    try:
        command = _auth_command_for_provider(args.provider)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.print_only:
        print(" ".join(command))
        return 0
    try:
        proc = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"Error: failed to run {' '.join(command)}: {exc}", file=sys.stderr)
        return 1
    return int(proc.returncode)


def _resolve_vault_root(raw: Path | str | None = None) -> Path:
    if raw is not None:
        root = Path(raw).expanduser()
    else:
        env_root = os.environ.get("CIAO_VAULT_ROOT", "").strip()
        root = Path(env_root).expanduser() if env_root else Path("memory-vault")
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def _memory_command(args: argparse.Namespace) -> int:
    from ciao.memory_tool import (
        DEFAULT_MEMORY_CHAR_LIMIT,
        DEFAULT_USER_CHAR_LIMIT,
        add_entry,
        memory_path,
        read_entries,
        remove_entry,
        replace_entry,
        user_path,
    )

    if args.target == "memory":
        path = memory_path()
        limit = int(os.environ.get("CIAO_MEMORY_CHAR_LIMIT", DEFAULT_MEMORY_CHAR_LIMIT))
    else:
        path = user_path()
        limit = int(os.environ.get("CIAO_USER_CHAR_LIMIT", DEFAULT_USER_CHAR_LIMIT))

    if args.action == "read":
        result = read_entries(path, char_limit=limit)
    elif args.action == "add":
        result = add_entry(path, args.text, char_limit=limit)
    elif args.action == "replace":
        result = replace_entry(path, args.old_text, args.new_text, char_limit=limit)
    elif args.action == "remove":
        result = remove_entry(path, args.text, char_limit=limit)
    else:
        raise SystemExit(f"unknown memory action {args.action!r}")

    if args.plain:
        if result.get("ok"):
            for key in ("added", "replaced", "removed"):
                if key in result:
                    print(
                        f"ok: {key} {result[key]!r}  "
                        f"({result.get('used_chars', '?')}/{result.get('char_limit', '?')} chars)"
                    )
                    break
            else:
                for entry in result.get("entries", []):
                    print(entry)
                    print("§")
        else:
            print(f"error: {result.get('error', 'unknown')}", file=sys.stderr)
    else:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0 if result.get("ok") else 1


def _vault_search_command(args: argparse.Namespace) -> int:
    from ciao import fts_search

    vault_root = _resolve_vault_root(args.vault_root)
    db_path = fts_search.get_db_path()

    if args.rebuild and db_path.exists():
        try:
            db_path.unlink()
            print("Dropped index database for rebuild.", file=sys.stderr)
        except OSError as exc:
            print(f"Error dropping index database: {exc}", file=sys.stderr)

    conn = sqlite3.connect(db_path)
    try:
        fts_search.init_db(conn)
        if not args.query:
            vault_indexed, vault_removed = fts_search.index_vault(conn, vault_root)
            logs_indexed, logs_removed = fts_search.index_logs(conn, vault_root)
            if vault_indexed or vault_removed or logs_indexed or logs_removed:
                print(
                    "FTS Index updated: "
                    f"vault ({vault_indexed} indexed, {vault_removed} removed), "
                    f"logs ({logs_indexed} indexed, {logs_removed} removed).",
                    file=sys.stderr,
                )
            return 0

        try:
            if args.logs:
                indexed, removed = fts_search.index_logs(conn, vault_root)
                if indexed or removed:
                    print(
                        f"Transcripts index: {indexed} indexed, {removed} removed.",
                        file=sys.stderr,
                    )
            else:
                indexed, removed = fts_search.index_vault(conn, vault_root)
                if indexed or removed:
                    print(
                        f"Vault index: {indexed} indexed, {removed} removed.",
                        file=sys.stderr,
                    )
        except Exception as exc:  # noqa: BLE001 - search can still use existing index.
            print(f"Incremental indexing error: {exc}", file=sys.stderr)

        results = (
            fts_search.search_logs(conn, args.query, limit=args.limit)
            if args.logs
            else fts_search.search_vault(conn, args.query, limit=args.limit)
        )
    finally:
        conn.close()

    if not results:
        print(f"No matches found for: {args.query}")
        return 0

    print(f"### Search Results for: {args.query}\n")
    for result in results:
        abs_path = vault_root.parent / result["path"]
        link = f"file://{abs_path.as_posix()}"
        print(f"- **[{result['title']}]({link})** (rank: {result['rank']})")
        if result["snippet"]:
            snippet = result["snippet"].replace("<<<", "**`").replace(">>>", "`**")
            print(f"  *{snippet}*")
        print()
    return 0


def _vault_lint_command(args: argparse.Namespace) -> int:
    from ciao.vault_lint import run_validation

    vault_root = _resolve_vault_root(args.vault_root)
    issues = run_validation(vault_root)

    has_issues = False
    if issues["broken_links"]:
        has_issues = True
        print("### Dead Wikilinks\n")
        for item in issues["broken_links"]:
            print(f"- `{item['source']}` links to missing `[[{item['target']}]]`")
        print()

    if issues["orphans"]:
        has_issues = True
        print("### Orphan Pages\n")
        for path in issues["orphans"]:
            print(f"- `{path}` has no incoming links and is not in MEMORY files")
        print()

    if issues["duplicates"]:
        has_issues = True
        print("### Near-Duplicate Pages\n")
        for paths in issues["duplicates"]:
            print(f"- Overlapping paths: {', '.join(f'`{p}`' for p in paths)}")
        print()

    if not has_issues:
        print("Vault is clean!")
        return 0
    return 1


def _vault_index_command(args: argparse.Namespace) -> int:
    from ciao import vault_index

    module_args: list[str] = []
    if args.workspace != "all":
        module_args.extend(["--workspace", args.workspace])
    if args.vault_root is not None:
        module_args.extend(["--vault-root", str(args.vault_root)])
    for entry_type in args.types:
        module_args.extend(["--type", entry_type])
    for tag in args.tags:
        module_args.extend(["--tag", tag])
    if args.name:
        module_args.extend(["--name", args.name])
    if args.related_to:
        module_args.extend(["--related-to", args.related_to])
    if args.neighbors:
        module_args.extend(["--neighbors", args.neighbors])
    if args.depth != 2:
        module_args.extend(["--depth", str(args.depth)])
    if args.format != "tsv":
        module_args.extend(["--format", args.format])
    if args.write:
        module_args.append("--write")
    return vault_index.main(module_args)


def _cleanup_sdk_blobs_command(args: argparse.Namespace) -> int:
    from ciao import cleanup_sdk_blobs

    module_args = ["--workspace", str(args.workspace)]
    if args.apply:
        module_args.append("--apply")
    return cleanup_sdk_blobs.main(module_args)


def _skills_sync_command(args: argparse.Namespace) -> int:
    from ciao import skills_sync

    return skills_sync.main(args.args)


def _sync_skills_command(args: argparse.Namespace) -> int:
    from ciao import sync_skills

    return sync_skills.main(
        [
            "--workspace",
            str(args.workspace),
            *(["--skip-upstream"] if args.skip_upstream else []),
            *(["--verbose"] if args.verbose else []),
        ]
    )


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, value = cleaned.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def _make_json_request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: dict | None = None,
    method: str = "GET",
) -> dict | list:
    request = urllib.request.Request(url, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(data).encode("utf-8")
    try:
        with opener.open(request) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            payload = json.loads(body)
            message = payload.get("error", body) if isinstance(payload, dict) else body
        except json.JSONDecodeError:
            message = body
        print(f"Error {exc.code} for {method} {url}: {message}", file=sys.stderr)
        return {"_error": True}
    except OSError as exc:
        print(f"Connection error to {url}: {exc}", file=sys.stderr)
        return {"_error": True}

    if not body:
        return {}
    return json.loads(body)


def _resolve_project(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    workspace: str,
    project_arg: str | None,
) -> str | None:
    projects = _make_json_request(
        opener, f"{base_url}/api/projects?workspace={workspace}"
    )
    if isinstance(projects, dict) and projects.get("_error"):
        return None
    if not isinstance(projects, list) or not projects:
        print(f"Error: No projects found in workspace '{workspace}'.", file=sys.stderr)
        return None

    if project_arg:
        for project in projects:
            if project.get("project_id") == project_arg:
                return project_arg
        matches = [
            project
            for project in projects
            if project_arg.lower() in project.get("name", "").lower()
        ]
        if len(matches) == 1:
            return matches[0]["project_id"]
        if len(matches) > 1:
            names = ", ".join(
                f"'{project['name']}' ({project['project_id']})"
                for project in matches
            )
            print(
                f"Error: Project '{project_arg}' is ambiguous. Matches: {names}",
                file=sys.stderr,
            )
            return None
        print(f"Error: Project matching '{project_arg}' not found.", file=sys.stderr)
        return None

    env_project = os.environ.get("CIAO_ACTIVE_PROJECT")
    if env_project:
        for project in projects:
            if project.get("project_id") == env_project:
                return env_project

    for project in projects:
        if project.get("is_auto") or project.get("name") == "General":
            return project["project_id"]
    return projects[0]["project_id"]


def _create_chat_command(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    _load_env_file(workspace_root / ".env")

    host = os.environ.get("PWA_HOST", "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = os.environ.get("PWA_PORT", "8443")
    base_url = args.base_url or f"http://{host}:{port}"
    auth_token = os.environ.get("PWA_AUTH_TOKEN", "")
    if not auth_token:
        print("Error: PWA_AUTH_TOKEN not found in environment or .env file.", file=sys.stderr)
        return 1

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    auth = _make_json_request(
        opener, f"{base_url}/api/auth", data={"token": auth_token}, method="POST"
    )
    if isinstance(auth, dict) and auth.get("_error"):
        return 1

    workspace = args.workspace or os.environ.get("CIAO_ACTIVE_WORKSPACE") or "default"

    project_id = _resolve_project(opener, base_url, workspace, args.project)
    if project_id is None:
        return 1

    payload = {
        "title": args.title or "New Chat",
        "model": args.model or os.environ.get("CIAO_MODEL") or None,
        "provider": args.provider or os.environ.get("CIAO_PROVIDER") or None,
        "model_bucket": args.model_bucket or os.environ.get("CIAO_MODEL_BUCKET") or None,
    }
    chat_info = _make_json_request(
        opener,
        f"{base_url}/api/projects/{project_id}/chats",
        data={key: value for key, value in payload.items() if value is not None},
        method="POST",
    )
    if isinstance(chat_info, dict) and chat_info.get("_error"):
        return 1
    if not isinstance(chat_info, dict) or "chat_id" not in chat_info:
        print("Error: chat creation returned an unexpected response.", file=sys.stderr)
        return 1

    chat_id = chat_info["chat_id"]
    prompt_result = _make_json_request(
        opener,
        f"{base_url}/api/chats/{chat_id}/prompt",
        data={"prompt": args.prompt},
        method="POST",
    )
    if isinstance(prompt_result, dict) and prompt_result.get("_error"):
        return 1

    print(f"Success: Created chat '{chat_info['title']}' ({chat_id})")
    print(f"Workspace: {workspace} | Project: {chat_info.get('project_id')}")
    print(f"Model: {chat_info.get('model')} ({chat_info.get('provider')})")
    print(f"PWA URL: {base_url}/chat/{chat_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ciao", description="Ciaobot local assistant CLI.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the Ciaobot server.")
    run_parser.set_defaults(func=lambda _args: _run_server())

    setup_parser = subparsers.add_parser(
        "setup",
        help="Scaffold a local Ciaobot workspace from packaged stock assets.",
    )
    setup_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace directory to initialize.",
    )
    setup_parser.add_argument("--auth-token", help="PWA auth token to write when .env is new.")
    setup_parser.add_argument("--push-contact", help="Web Push contact to write when .env is new.")
    setup_parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used by the generated LaunchAgent.",
    )
    setup_parser.add_argument(
        "--port",
        type=int,
        default=8443,
        help="Localhost port used by the LaunchAgent and app shortcut.",
    )
    setup_parser.add_argument(
        "--launch-agents-dir",
        type=Path,
        default=Path.home() / "Library" / "LaunchAgents",
        help="Directory where com.ciao.server.plist is written.",
    )
    setup_parser.add_argument(
        "--app-dir",
        type=Path,
        default=Path.home() / "Applications",
        help="Directory where Ciaobot.app is written.",
    )
    setup_parser.add_argument(
        "--load-launchd",
        action="store_true",
        help="Run launchctl unload/load after writing the LaunchAgent.",
    )
    setup_parser.set_defaults(func=_setup_command)

    auth_parser = subparsers.add_parser(
        "auth",
        help="Run a provider OAuth/login command for first-run setup.",
    )
    auth_parser.add_argument("provider", choices=["claude", "ollama"])
    auth_parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the terminal command without running it.",
    )
    auth_parser.set_defaults(func=_auth_command)

    dev_parser = subparsers.add_parser(
        "dev",
        help="Run the local backend plus Vite frontend for development.",
    )
    dev_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="App checkout/workspace root. Defaults to current directory.",
    )
    dev_parser.add_argument("--backend-port", type=int, default=8543)
    dev_parser.add_argument("--frontend-port", type=int, default=5173)
    dev_parser.add_argument(
        "--no-install",
        action="store_true",
        help="Do not run npm install when web/node_modules is missing.",
    )
    dev_parser.set_defaults(
        func=lambda args: dev.main(
            [
                "--workspace",
                str(args.workspace),
                "--backend-port",
                str(args.backend_port),
                "--frontend-port",
                str(args.frontend_port),
                *(["--no-install"] if args.no_install else []),
            ]
        )
    )

    public_parser = subparsers.add_parser(
        "public-preflight",
        help="Export or scan a public Ciaobot tree.",
    )
    public_parser.add_argument("args", nargs=argparse.REMAINDER)
    public_parser.set_defaults(func=lambda args: public_release.main(args.args))

    smoke_parser = subparsers.add_parser(
        "package-smoke",
        help="Build, install, and smoke-test the Ciaobot wheel.",
    )
    smoke_parser.add_argument("args", nargs=argparse.REMAINDER)
    smoke_parser.set_defaults(func=lambda args: package_smoke.main(args.args))

    memory_parser = subparsers.add_parser(
        "memory",
        help="Read or edit bounded memory files.",
    )
    memory_parser.add_argument(
        "--plain", action="store_true", help="Human-readable output instead of JSON."
    )
    memory_sub = memory_parser.add_subparsers(dest="action", required=True)
    memory_read = memory_sub.add_parser("read", help="Return all entries and usage stats.")
    memory_read.add_argument("--target", required=True, choices=["memory", "user"])
    memory_read.set_defaults(func=_memory_command)

    memory_add = memory_sub.add_parser("add", help="Append a new entry.")
    memory_add.add_argument("--target", required=True, choices=["memory", "user"])
    memory_add.add_argument("--text", required=True)
    memory_add.set_defaults(func=_memory_command)

    memory_replace = memory_sub.add_parser("replace", help="Replace an existing entry.")
    memory_replace.add_argument("--target", required=True, choices=["memory", "user"])
    memory_replace.add_argument("--old", required=True, dest="old_text")
    memory_replace.add_argument("--new", required=True, dest="new_text")
    memory_replace.set_defaults(func=_memory_command)

    memory_remove = memory_sub.add_parser("remove", help="Remove an existing entry.")
    memory_remove.add_argument("--target", required=True, choices=["memory", "user"])
    memory_remove.add_argument("--text", required=True)
    memory_remove.set_defaults(func=_memory_command)

    search_parser = subparsers.add_parser(
        "vault-search",
        help="Full-text search over the vault or transcript logs.",
    )
    search_parser.add_argument("query", nargs="?", default=None, help="Search keywords.")
    search_parser.add_argument(
        "--logs",
        action="store_true",
        help="Search transcript and meeting logs instead of vault notes.",
    )
    search_parser.add_argument(
        "--limit", type=int, default=10, help="Maximum number of results."
    )
    search_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop and rebuild the search index before searching.",
    )
    search_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    search_parser.set_defaults(func=_vault_search_command)

    index_parser = subparsers.add_parser(
        "vault-index",
        help="Build or query the vault frontmatter/link index.",
    )
    index_parser.add_argument("--workspace", default="all")
    index_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    index_parser.add_argument("--type", dest="types", action="append", default=[])
    index_parser.add_argument("--tag", dest="tags", action="append", default=[])
    index_parser.add_argument("--name", default=None)
    index_parser.add_argument("--related-to", dest="related_to", default=None)
    index_parser.add_argument("--neighbors", default=None)
    index_parser.add_argument("--depth", type=int, default=2)
    index_parser.add_argument("--format", choices=["tsv", "md", "json"], default="tsv")
    index_parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate INDEX.md under the configured vault root.",
    )
    index_parser.set_defaults(func=_vault_index_command)

    lint_parser = subparsers.add_parser(
        "vault-lint",
        help="Run vault hygiene checks.",
        description="Vault hygiene linter for markdown files.",
    )
    lint_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root. Defaults to CIAO_VAULT_ROOT or ./memory-vault.",
    )
    lint_parser.set_defaults(func=_vault_lint_command)

    chat_parser = subparsers.add_parser(
        "create-chat",
        help="Create a chat through the running Ciaobot server and send an initial prompt.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    chat_parser.add_argument("--prompt", required=True, help="Initial prompt.")
    chat_parser.add_argument("--title", help="Chat title. Defaults to 'New Chat'.")
    chat_parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path("."),
        help="Workspace directory containing .env.",
    )
    chat_parser.add_argument(
        "--workspace",
        help="Logical chat workspace. Inherits CIAO_ACTIVE_WORKSPACE.",
    )
    chat_parser.add_argument("--project", help="Project ID or case-insensitive name.")
    chat_parser.add_argument("--model", help="Model override. Inherits CIAO_MODEL.")
    chat_parser.add_argument(
        "--provider",
        choices=["claude"],
        help="Provider override. Inherits CIAO_PROVIDER.",
    )
    chat_parser.add_argument(
        "--model-bucket",
        help="Model bucket override. Inherits CIAO_MODEL_BUCKET.",
    )
    chat_parser.add_argument(
        "--base-url",
        help="Ciaobot server URL. Defaults to PWA_HOST/PWA_PORT.",
    )
    chat_parser.set_defaults(func=_create_chat_command)

    cleanup_parser = subparsers.add_parser(
        "cleanup-sdk-blobs",
        help="Dry-run or delete archived Claude SDK JSONL blobs.",
    )
    cleanup_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root. Defaults to current directory.",
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete matching blobs. Default is dry-run.",
    )
    cleanup_parser.set_defaults(func=_cleanup_sdk_blobs_command)

    skills_sync_parser = subparsers.add_parser(
        "skills-sync",
        help="Plan or write the upstream skill update cache.",
    )
    skills_sync_parser.add_argument("args", nargs=argparse.REMAINDER)
    skills_sync_parser.set_defaults(func=_skills_sync_command)

    sync_skills_parser = subparsers.add_parser(
        "sync-skills",
        help="Install and mirror local skills, commands, and agents.",
    )
    sync_skills_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root. Defaults to current directory.",
    )
    sync_skills_parser.add_argument(
        "--skip-upstream",
        action="store_true",
        help="Skip skills-lock.json remote refresh and only mirror local catalogs.",
    )
    sync_skills_parser.add_argument("--verbose", action="store_true")
    sync_skills_parser.set_defaults(func=_sync_skills_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if argv_list[:1] == ["public-preflight"]:
        return public_release.main(argv_list[1:])
    if argv_list[:1] == ["package-smoke"]:
        return package_smoke.main(argv_list[1:])
    parser = build_parser()
    args = parser.parse_args(argv_list)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
